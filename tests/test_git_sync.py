import importlib.util
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "git_sync.py"
    spec = importlib.util.spec_from_file_location("git_sync_under_test", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This suite exercises real Git against disposable local bare remotes.
    # Production transports allow only HTTPS/SSH; transport-policy regression
    # tests live separately and use the unmodified environment.
    original_environment = module.git_environment
    module.git_environment = lambda: dict(original_environment(), GIT_ALLOW_PROTOCOL="https:ssh:file")
    return module


def load_hot_sync():
    path = ROOT / "scripts" / "hot_sync.py"
    spec = importlib.util.spec_from_file_location("hot_sync_under_test", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command(cwd, *args):
    return subprocess.run(
        list(args), cwd=str(cwd), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=True,
    ).stdout.strip()


class GitSyncTests(unittest.TestCase):
    def write_policy(self, repo: Path) -> None:
        # All fixture knowledge repositories use the same versioned identity.
        manifest = repo / "MANIFEST.json"
        manifest.write_text(json.dumps({"schema_version": "1", "knowledge_base_id": "fictional-knowledge"}))
        command(repo, "git", "add", "MANIFEST.json")
        path = repo / "config" / "kb.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "hot_sync": {
                        "expected_repository": "example/knowledge",
                        "expected_branch": "main",
                        "allowed_ssh_hosts": ["github.com", "github-ai-knowledge"],
                        "bot_user_name": "AI Knowledge Bot",
                        "bot_user_email": "ai-knowledge-bot@localhost",
                    }
                }
            ),
            encoding="utf-8",
        )

    def initialize_clone(self, root: Path, remote: Path, name: str) -> Path:
        clone = root / name
        command(root, "git", "clone", str(remote), str(clone))
        command(clone, "git", "config", "user.name", "AI Knowledge Test")
        command(clone, "git", "config", "user.email", "test@localhost")
        return clone

    def pending_first_push(self, root: Path):
        remote = root / "empty.git"
        command(root, "git", "init", "--bare", "--initial-branch=main", str(remote))
        local = self.initialize_clone(root, remote, "local")
        self.write_policy(local)
        (local / "local-fact.txt").write_text("fictional local fact\n")
        command(local, "git", "add", ".")
        command(local, "git", "commit", "-m", "local knowledge")
        return remote, local

    @contextmanager
    def allow_fixture_transport(self, module):
        # Real local Git fetch/push/rebase run against disposable bare repos.
        # Only GitHub identity/privacy checks are substituted; these tests do
        # not claim authenticated GitHub service compatibility.
        with mock.patch.object(module, "configured_origin_status", return_value={"ok":True, "status":"ready"}), mock.patch.object(module, "repository_privacy_status", return_value={"ok":True, "private":True, "status":"repository-private"}):
            yield

    def independent_writer(self, root, remote):
        other = self.initialize_clone(root, remote, "independent")
        command(other, "git", "checkout", "--orphan", "independent-history")
        command(other, "git", "rm", "-rf", "--ignore-unmatch", ".")
        (other / "other-fact.txt").write_text("fictional independent history\n")
        command(other, "git", "add", ".")
        command(other, "git", "commit", "-m", "independent knowledge")
        return other

    def test_first_push_creates_only_expected_branch_and_is_not_repeated(self):
        for pull in (False, True):
            with self.subTest(pull=pull), tempfile.TemporaryDirectory() as directory:
                module = load_module()
                root = Path(directory)
                remote, local = self.pending_first_push(root)
                original = command(local, "git", "rev-parse", "HEAD")
                with self.allow_fixture_transport(module), mock.patch.object(module, "git", wraps=module.git) as invoked:
                    first = module.sync("origin", pull=pull, push=True, root=local)
                    again = module.sync("origin", pull=True, push=True, root=local)
                self.assertTrue(first["ok"], first)
                self.assertTrue(first["pushed"])
                self.assertFalse(first["pulled"])
                self.assertTrue(first["remote_branch_created"])
                self.assertTrue(again["ok"], again)
                self.assertFalse(again["remote_branch_created"])
                self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"), original)
                self.assertEqual(command(root, "git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"), "refs/heads/main")
                pushes = [call.args[0] for call in invoked.call_args_list if call.args[0][0] == "push"]
                self.assertTrue(pushes)
                self.assertFalse(any(any(arg.startswith("--force") or arg.startswith("+") for arg in push) for push in pushes))

    def test_pull_only_does_not_initialize_an_empty_remote(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            with self.allow_fixture_transport(module), mock.patch.object(module, "git", wraps=module.git) as invoked:
                result = module.sync("origin", pull=True, push=False, root=local)
            self.assertEqual(result["error"], "remote-branch-missing")
            self.assertFalse(any(call.args[0][0] == "push" for call in invoked.call_args_list))
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "for-each-ref"), "")

    def test_missing_expected_branch_rejects_other_branches_tags_and_notes(self):
        for reference in ("refs/heads/other", "refs/tags/fictional-v1", "refs/notes/fictional"):
            with self.subTest(reference=reference), tempfile.TemporaryDirectory() as directory:
                module = load_module()
                root = Path(directory)
                remote, local = self.pending_first_push(root)
                command(local, "git", "push", "origin", "HEAD:" + reference)
                before = command(root, "git", "--git-dir", str(remote), "show-ref")
                with self.allow_fixture_transport(module), mock.patch.object(module, "git", wraps=module.git) as invoked:
                    result = module.sync("origin", pull=True, push=True, root=local)
                self.assertEqual(result["error"], "remote-not-empty")
                self.assertFalse(result["retryable"])
                self.assertFalse(any(call.args[0][0] == "push" for call in invoked.call_args_list))
                self.assertEqual(command(root, "git", "--git-dir", str(remote), "show-ref"), before)

    def test_failed_empty_ref_check_cannot_start_a_push(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            actual_git = module.git
            calls = []
            def unavailable(arguments, **kwargs):
                calls.append(arguments)
                if arguments == ["ls-remote", "--refs", "origin"]:
                    return subprocess.CompletedProcess(arguments, 128, "", "fictional connection failure")
                return actual_git(arguments, **kwargs)
            with self.allow_fixture_transport(module), mock.patch.object(module, "git", side_effect=unavailable):
                result = module.sync("origin", pull=True, push=True, root=local)
            self.assertEqual(result["error"], "remote-refs-unavailable")
            self.assertTrue(result["retryable"])
            self.assertFalse(any(args[0] == "push" for args in calls))
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "for-each-ref"), "")

    def test_first_push_race_preserves_both_independent_histories_without_rebase(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            other = self.independent_writer(root, remote)
            local_head = command(local, "git", "rev-parse", "HEAD")
            other_head = command(other, "git", "rev-parse", "HEAD")
            actual_git = module.git
            pushes = []
            def race(arguments, **kwargs):
                if arguments[0] == "push":
                    pushes.append(arguments)
                    command(other, "git", "push", "origin", "HEAD:refs/heads/main")
                return actual_git(arguments, **kwargs)
            with self.allow_fixture_transport(module), mock.patch.object(module, "git", side_effect=race), mock.patch.object(module, "rebase_onto", wraps=module.rebase_onto) as rebase:
                result = module.sync("origin", pull=True, push=True, root=local)
            self.assertEqual(result["error"], "first-push-failed")
            self.assertEqual(len(pushes), 1)
            rebase.assert_not_called()
            self.assertEqual(command(local, "git", "rev-parse", "HEAD"), local_head)
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "main"), other_head)
            self.assertEqual(command(local, "git", "status", "--porcelain"), "")

    def test_same_head_race_does_not_claim_this_operation_created_the_branch(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            actual_git = module.git
            def race(arguments, **kwargs):
                if arguments[0] == "push":
                    command(local, "git", "push", "origin", "HEAD:refs/heads/main")
                return actual_git(arguments, **kwargs)
            with self.allow_fixture_transport(module), mock.patch.object(module, "git", side_effect=race):
                result = module.sync("origin", pull=True, push=True, root=local)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["pushed"])
            self.assertFalse(result["remote_branch_created"])
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "main"), command(local, "git", "rev-parse", "HEAD"))

    def test_unrelated_remote_history_is_never_rebased_for_pull_or_push_only(self):
        for pull in (True, False):
            with self.subTest(pull=pull), tempfile.TemporaryDirectory() as directory:
                module = load_module()
                root = Path(directory)
                remote, local = self.pending_first_push(root)
                other = self.independent_writer(root, remote)
                command(other, "git", "push", "origin", "HEAD:refs/heads/main")
                local_head = command(local, "git", "rev-parse", "HEAD")
                other_head = command(other, "git", "rev-parse", "HEAD")
                with self.allow_fixture_transport(module), mock.patch.object(module, "rebase_onto", wraps=module.rebase_onto) as rebase:
                    result = module.sync("origin", pull=pull, push=True, root=local)
                self.assertEqual(result["error"], "unrelated-histories")
                self.assertFalse(result["retryable"])
                rebase.assert_not_called()
                self.assertEqual(command(local, "git", "rev-parse", "HEAD"), local_head)
                self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "main"), other_head)
                self.assertEqual(command(local, "git", "status", "--porcelain"), "")

    def test_remote_replaced_after_fetch_is_rejected_before_retry_rebase(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            command(local, "git", "push", "origin", "main")
            other = self.independent_writer(root, remote)
            (local / "later.txt").write_text("fictional later fact\n")
            command(local, "git", "add", ".")
            command(local, "git", "commit", "-m", "later local fact")
            local_head = command(local, "git", "rev-parse", "HEAD")
            other_head = command(other, "git", "rev-parse", "HEAD")
            actual_git = module.git
            pushes = []
            def replaced(arguments, **kwargs):
                if arguments[0] == "push":
                    pushes.append(arguments)
                    # Simulate an independent administrator replacing the
                    # disposable remote. The synchronizer itself never forces.
                    command(other, "git", "push", "--force", "origin", "HEAD:refs/heads/main")
                return actual_git(arguments, **kwargs)
            with self.allow_fixture_transport(module), mock.patch.object(module, "git", side_effect=replaced), mock.patch.object(module, "rebase_onto", wraps=module.rebase_onto) as rebase:
                result = module.sync("origin", pull=True, push=True, root=local)
            self.assertEqual(result["error"], "unrelated-histories")
            self.assertEqual(result["phase"], "push-race")
            self.assertEqual(len(pushes), 1)
            rebase.assert_not_called()
            self.assertEqual(command(local, "git", "rev-parse", "HEAD"), local_head)
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "main"), other_head)
            self.assertEqual(command(local, "git", "status", "--porcelain"), "")

    def test_two_devices_rebase_nonconflicting_commits_without_force(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "shared.git"
            command(root, "git", "init", "--bare", "--initial-branch=main", str(remote))
            first = self.initialize_clone(root, remote, "first")
            self.write_policy(first)
            (first / "base.txt").write_text("base\n", encoding="utf-8")
            command(first, "git", "add", "base.txt", "config/kb.json")
            command(first, "git", "commit", "-m", "base")
            command(first, "git", "push", "origin", "main")
            second = self.initialize_clone(root, remote, "second")

            (first / "one.txt").write_text("one\n", encoding="utf-8")
            command(first, "git", "add", "one.txt")
            command(first, "git", "commit", "-m", "one")
            (second / "two.txt").write_text("two\n", encoding="utf-8")
            command(second, "git", "add", "two.txt")
            command(second, "git", "commit", "-m", "two")

            accepted = {
                "ok": True,
                "configured": True,
                "identity_valid": True,
                "status": "ready",
            }
            private = {
                "ok": True,
                "private": True,
                "status": "repository-private",
                "retryable": False,
            }
            with mock.patch.object(
                module, "configured_origin_status", return_value=accepted
            ), mock.patch.object(
                module, "repository_privacy_status", return_value=private
            ):
                module.ROOT = first
                module.LOCK = first / ".git" / "ai-knowledge-sync.lock"
                self.assertTrue(module.sync("origin", pull=True, push=True)["ok"])
                module.ROOT = second
                module.LOCK = second / ".git" / "ai-knowledge-sync.lock"
                result = module.sync("origin", pull=True, push=True)
            self.assertTrue(result["ok"])
            self.assertTrue((second / "one.txt").is_file())
            command(first, "git", "pull", "--rebase", "origin", "main")
            self.assertTrue((first / "two.txt").is_file())

    def test_incompatible_peer_is_rejected_without_changing_either_copy(self):
        variants = {
            "future-schema": ({"schema_version": "2", "knowledge_base_id": "fictional-knowledge"}, "remote-knowledge-schema-unsupported"),
            "different-library": ({"schema_version": "1", "knowledge_base_id": "another-library"}, "knowledge-base-mismatch"),
            "missing-identity": ({"schema_version": "1"}, "remote-knowledge-identity-missing"),
            "invalid-json": ("{", "remote-knowledge-manifest-invalid"),
            "oversized": (" " * 65537, "remote-knowledge-manifest-invalid"),
            "missing": (None, "remote-knowledge-manifest-missing-or-unsafe"),
            "symlink": (None, "remote-knowledge-manifest-missing-or-unsafe"),
        }
        for name, (payload, expected) in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                module = load_module()
                root = Path(directory)
                remote, local = self.pending_first_push(root)
                command(local, "git", "push", "origin", "main")
                peer = self.initialize_clone(root, remote, "peer")
                manifest = peer / "MANIFEST.json"
                if name in {"missing", "symlink"}:
                    manifest.unlink()
                    if name == "symlink":
                        manifest.symlink_to("local-fact.txt")
                else:
                    manifest.write_text(json.dumps(payload) if isinstance(payload, dict) else payload)
                command(peer, "git", "add", ".")
                command(peer, "git", "commit", "-m", "peer format change")
                command(peer, "git", "push", "origin", "main")
                original = command(local, "git", "rev-parse", "HEAD")
                peer_head = command(peer, "git", "rev-parse", "HEAD")
                original_manifest = (local / "MANIFEST.json").read_bytes()
                with self.allow_fixture_transport(module), mock.patch.object(module, "rebase_onto", wraps=module.rebase_onto) as rebase:
                    result = module.sync("origin", pull=True, push=True, root=local)
                self.assertEqual(result["error"], expected, result)
                self.assertFalse(result["retryable"])
                self.assertEqual(result["phase"], "compatibility")
                rebase.assert_not_called()
                self.assertEqual(command(local, "git", "rev-parse", "HEAD"), original)
                self.assertEqual((local / "MANIFEST.json").read_bytes(), original_manifest)
                self.assertEqual(command(local, "git", "status", "--porcelain"), "")
                self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "main"), peer_head)

    def test_tool_version_difference_does_not_block_compatible_data(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            command(local, "git", "push", "origin", "main")
            peer = self.initialize_clone(root, remote, "peer")
            manifest = peer / "MANIFEST.json"
            payload = json.loads(manifest.read_text())
            payload["tool_version"] = "fictional-later-tool"
            manifest.write_text(json.dumps(payload))
            command(peer, "git", "add", ".")
            command(peer, "git", "commit", "-m", "compatible tool upgrade")
            command(peer, "git", "push", "origin", "main")
            with self.allow_fixture_transport(module):
                result = module.sync("origin", pull=True, push=False, root=local)
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["pulled"])
            self.assertTrue(result["compatibility"]["ok"])
            self.assertEqual(command(local, "git", "rev-parse", "HEAD"), command(peer, "git", "rev-parse", "HEAD"))

    def test_first_push_rejects_unsupported_local_data(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            (local / "MANIFEST.json").write_text(json.dumps({"schema_version": "future", "knowledge_base_id": "fictional-knowledge"}))
            command(local, "git", "add", ".")
            command(local, "git", "commit", "-m", "unsupported format")
            with self.allow_fixture_transport(module):
                result = module.sync("origin", pull=True, push=True, root=local)
            self.assertEqual(result["error"], "local-knowledge-schema-unsupported")
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "for-each-ref"), "")

    def test_schema_change_during_push_race_is_rejected_before_rebase(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, local = self.pending_first_push(root)
            command(local, "git", "push", "origin", "main")
            peer = self.initialize_clone(root, remote, "peer")
            (peer / "MANIFEST.json").write_text(json.dumps({"schema_version": "future", "knowledge_base_id": "fictional-knowledge"}))
            command(peer, "git", "add", ".")
            command(peer, "git", "commit", "-m", "peer format upgrade")
            (local / "new.txt").write_text("fictional later fact")
            command(local, "git", "add", ".")
            command(local, "git", "commit", "-m", "local new fact")
            original = command(local, "git", "rev-parse", "HEAD")
            actual_git = module.git
            pushes = []
            def race(arguments, **kwargs):
                if arguments[0] == "push":
                    pushes.append(arguments)
                    command(peer, "git", "push", "origin", "main")
                return actual_git(arguments, **kwargs)
            with self.allow_fixture_transport(module), mock.patch.object(module, "git", side_effect=race), mock.patch.object(module, "rebase_onto", wraps=module.rebase_onto) as rebase:
                result = module.sync("origin", pull=True, push=True, root=local)
            self.assertEqual(result["error"], "remote-knowledge-schema-unsupported", result)
            self.assertEqual(result["phase"], "push-race-compatibility")
            self.assertEqual(len(pushes), 1)
            rebase.assert_not_called()
            self.assertEqual(command(local, "git", "rev-parse", "HEAD"), original)
            self.assertEqual(command(root, "git", "--git-dir", str(remote), "rev-parse", "main"), command(peer, "git", "rev-parse", "HEAD"))

    def test_conflict_is_aborted_recorded_and_not_retried_for_unchanged_heads(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "shared.git"
            command(root, "git", "init", "--bare", "--initial-branch=main", str(remote))
            first = self.initialize_clone(root, remote, "first")
            self.write_policy(first)
            fact = first / "profile" / "fact.md"
            fact.parent.mkdir()
            fact.write_text("base fact\n", encoding="utf-8")
            command(first, "git", "add", "profile/fact.md", "config/kb.json")
            command(first, "git", "commit", "-m", "base")
            command(first, "git", "push", "origin", "main")
            second = self.initialize_clone(root, remote, "second")

            fact.write_text("device one fact\n", encoding="utf-8")
            command(first, "git", "add", "profile/fact.md")
            command(first, "git", "commit", "-m", "device one")
            command(first, "git", "push", "origin", "main")
            second_fact = second / "profile" / "fact.md"
            second_fact.write_text("device two fact\n", encoding="utf-8")
            command(second, "git", "add", "profile/fact.md")
            command(second, "git", "commit", "-m", "device two")
            local_head = command(second, "git", "rev-parse", "HEAD")

            accepted = {
                "ok": True,
                "configured": True,
                "identity_valid": True,
                "status": "ready",
            }
            private = {
                "ok": True,
                "private": True,
                "status": "repository-private",
                "retryable": False,
            }
            with mock.patch.object(
                module, "configured_origin_status", return_value=accepted
            ), mock.patch.object(
                module, "repository_privacy_status", return_value=private
            ):
                first_result = module.sync(
                    "origin", pull=True, push=True, root=second
                )
                with mock.patch.object(
                    module, "rebase_onto", wraps=module.rebase_onto
                ) as rebased:
                    repeated = module.sync(
                        "origin", pull=True, push=True, root=second
                    )

            self.assertFalse(first_result["ok"])
            self.assertEqual(first_result["error"], "rebase-conflict")
            self.assertEqual(first_result["conflict_files"], 1)
            incident = second / first_result["incident"]
            self.assertTrue(incident.is_file())
            evidence = json.loads(incident.read_text(encoding="utf-8"))
            self.assertEqual(evidence["local_head"], local_head)
            self.assertEqual(evidence["resolution"], "unresolved")
            self.assertEqual(
                evidence["cleanup"],
                {
                    "abort_required": True,
                    "abort_succeeded": True,
                    "head_restored": True,
                    "index_clean": True,
                    "rebase_state_absent": True,
                    "worktree_clean": True,
                },
            )
            self.assertEqual(command(second, "git", "rev-parse", "HEAD"), local_head)
            self.assertEqual(command(second, "git", "status", "--porcelain"), "")
            self.assertFalse((second / ".git" / "rebase-merge").exists())
            command(second, "git", "cat-file", "-e", local_head)

            self.assertFalse(repeated["ok"])
            self.assertEqual(
                repeated["error"], "rebase-conflict-unchanged"
            )
            self.assertTrue(repeated["repeated_without_head_change"])
            rebased.assert_not_called()

    def test_conflict_incident_identity_is_phase_independent(self):
        module = load_module()
        pull = module.conflict_incident_key(
            "rebase-conflict", "main", "a" * 40, "b" * 40
        )
        push = module.conflict_incident_key(
            "push-race-conflict", "main", "a" * 40, "b" * 40
        )
        self.assertEqual(pull, push)

    def test_generic_failure_without_rebase_state_is_cleanly_retryable(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            command(repo, "git", "init", "--initial-branch=main")
            command(repo, "git", "config", "user.name", "AI Knowledge Test")
            command(repo, "git", "config", "user.email", "test@localhost")
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            command(repo, "git", "add", "base.txt")
            command(repo, "git", "commit", "-m", "base")

            failed = module.rebase_onto("missing", "main", root=repo)

            self.assertFalse(failed["ok"])
            self.assertEqual(failed["failure_kind"], "rebase-failed")
            self.assertTrue(failed["cleanup_verified"])
            self.assertFalse(failed["cleanup"]["abort_required"])
            self.assertFalse(failed["cleanup"]["abort_succeeded"])

    def test_active_rebase_abort_failure_is_not_cleanup_verified(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "shared.git"
            command(root, "git", "init", "--bare", "--initial-branch=main", str(remote))
            first = self.initialize_clone(root, remote, "first")
            (first / "fact.txt").write_text("base\n", encoding="utf-8")
            command(first, "git", "add", "fact.txt")
            command(first, "git", "commit", "-m", "base")
            command(first, "git", "push", "origin", "main")
            second = self.initialize_clone(root, remote, "second")
            (first / "fact.txt").write_text("remote\n", encoding="utf-8")
            command(first, "git", "add", "fact.txt")
            command(first, "git", "commit", "-m", "remote")
            command(first, "git", "push", "origin", "main")
            (second / "fact.txt").write_text("local\n", encoding="utf-8")
            command(second, "git", "add", "fact.txt")
            command(second, "git", "commit", "-m", "local")
            command(second, "git", "fetch", "origin")
            real_git = module.git

            def fail_abort(arguments, timeout=180, root=None):
                if arguments == ["rebase", "--abort"]:
                    return subprocess.CompletedProcess(
                        ["git", *arguments], 1, stdout="", stderr="abort failed"
                    )
                return real_git(arguments, timeout=timeout, root=root)

            with mock.patch.object(module, "git", side_effect=fail_abort):
                failed = module.rebase_onto("origin", "main", root=second)

            self.assertEqual(failed["failure_kind"], "conflict")
            self.assertTrue(failed["cleanup"]["abort_required"])
            self.assertFalse(failed["cleanup"]["abort_succeeded"])
            self.assertFalse(failed["cleanup_verified"])

    def test_generic_rebase_failure_is_retryable_and_not_an_incident(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "shared.git"
            command(root, "git", "init", "--bare", "--initial-branch=main", str(remote))
            first = self.initialize_clone(root, remote, "first")
            self.write_policy(first)
            (first / "fact.txt").write_text("base\n", encoding="utf-8")
            command(first, "git", "add", "fact.txt", "config/kb.json")
            command(first, "git", "commit", "-m", "base")
            command(first, "git", "push", "origin", "main")
            second = self.initialize_clone(root, remote, "second")
            (first / "fact.txt").write_text("remote\n", encoding="utf-8")
            command(first, "git", "add", "fact.txt")
            command(first, "git", "commit", "-m", "remote")
            command(first, "git", "push", "origin", "main")
            (second / "fact.txt").write_text("local\n", encoding="utf-8")
            command(second, "git", "add", "fact.txt")
            command(second, "git", "commit", "-m", "local")
            accepted = {"ok": True, "status": "ready"}
            private = {"ok": True, "private": True, "status": "repository-private"}
            generic = {
                "ok": False,
                "failure_kind": "rebase-failed",
                "cleanup_verified": True,
                "cleanup": {
                    "abort_required": False,
                    "abort_succeeded": True,
                    "head_restored": True,
                    "index_clean": True,
                    "worktree_clean": True,
                    "rebase_state_absent": True,
                },
                "conflicts": [],
            }
            with mock.patch.object(
                module, "configured_origin_status", return_value=accepted
            ), mock.patch.object(
                module, "repository_privacy_status", return_value=private
            ), mock.patch.object(module, "rebase_onto", return_value=generic):
                result = module.sync("origin", pull=True, push=False, root=second)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "rebase-failed")
            self.assertTrue(result["retryable"])
            self.assertFalse(
                (second / ".git" / "ai-knowledge-sync-incidents").exists()
            )

    def test_missing_remote_is_degraded_not_a_healthy_skip(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            command(repo, "git", "init", "--initial-branch=main")
            command(repo, "git", "config", "user.name", "AI Knowledge Test")
            command(repo, "git", "config", "user.email", "test@localhost")
            self.write_policy(repo)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            command(repo, "git", "add", "base.txt", "config/kb.json")
            command(repo, "git", "commit", "-m", "base")
            module.ROOT = repo
            module.LOCK = repo / ".git" / "ai-knowledge-sync.lock"
            result = module.sync("origin", pull=True, push=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "origin-missing")
            self.assertEqual(result["status"], "degraded")

    def test_wrong_origin_and_dirty_worktree_fail_closed_before_network(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            command(repo, "git", "init", "--initial-branch=main")
            command(repo, "git", "config", "user.name", "AI Knowledge Test")
            command(repo, "git", "config", "user.email", "test@localhost")
            self.write_policy(repo)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            command(repo, "git", "add", ".")
            command(repo, "git", "commit", "-m", "base")
            command(repo, "git", "remote", "add", "origin", "git@github.com:Other/Repo.git")
            module.ROOT = repo
            wrong = module.sync("origin", pull=True, push=True)
            self.assertFalse(wrong["ok"])
            self.assertEqual(wrong["error"], "origin-identity-mismatch")

            command(
                repo,
                "git",
                "remote",
                "set-url",
                "origin",
                "git@github-ai-knowledge:example/knowledge.git",
            )
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            dirty = module.sync("origin", pull=True, push=True)
            self.assertFalse(dirty["ok"])
            self.assertEqual(dirty["error"], "dirty-worktree")
            self.assertEqual(dirty["status"], "pending")

    def test_remote_identity_accepts_only_configured_repository_and_aliases(self):
        module = load_module()
        policy = {
            "expected_repository": "example/knowledge",
            "allowed_ssh_hosts": ["github.com", "github-ai-knowledge"],
        }
        accepted = (
            "https://github.com/example/knowledge.git",
            "ssh://git@github.com/example/knowledge.git",
            "git@github.com:example/knowledge.git",
            "git@github-ai-knowledge:example/knowledge.git",
        )
        for remote in accepted:
            with self.subTest(remote=remote):
                self.assertTrue(module.remote_matches_policy(remote, policy))
        rejected = (
            "https://github.com/Other/AI-Knowledge.git",
            "git@unconfigured-alias:example/knowledge.git",
            "/tmp/local.git",
        )
        for remote in rejected:
            with self.subTest(remote=remote):
                self.assertFalse(module.remote_matches_policy(remote, policy))

    def test_anonymous_probe_uses_bodyless_curl_and_classifies_private_public_unknown(self):
        module = load_module()
        policy = {
            "expected_repository": "example/knowledge",
            "expected_branch": "main",
        }
        arguments = module.anonymous_repository_probe_arguments(policy)
        rendered = " ".join(arguments)
        self.assertIn(Path(arguments[0]).name.casefold(), {"curl", "curl.exe"})
        self.assertEqual(arguments[1], "--disable")
        self.assertIn("--output", arguments)
        self.assertIn(module.os.devnull, arguments)
        self.assertEqual(arguments[arguments.index("--connect-timeout") + 1], "12")
        self.assertEqual(arguments[arguments.index("--max-time") + 1], "30")
        self.assertNotIn("authorization", rendered.casefold())
        self.assertNotIn("REMOTE-SECRET", rendered)

        public = subprocess.CompletedProcess(arguments, 0, "200", "")
        private = subprocess.CompletedProcess(arguments, 0, "404", "")
        unknown = subprocess.CompletedProcess(arguments, 28, "000", "timeout")
        self.assertEqual(module.classify_anonymous_probe(public)["status"], "repository-public")
        self.assertEqual(module.classify_anonymous_probe(private)["status"], "repository-private")
        self.assertEqual(
            module.classify_anonymous_probe(unknown)["status"],
            "repository-privacy-unknown",
        )

    def test_anonymous_probe_uses_curl_discovered_on_windows(self):
        module = load_module()
        policy = {
            "expected_repository": "example/knowledge",
            "expected_branch": "main",
        }
        windows_curl = r"C:\Windows\System32\curl.exe"
        with mock.patch.object(module.shutil, "which", return_value=windows_curl):
            arguments = module.anonymous_repository_probe_arguments(policy)
        self.assertEqual(arguments[0], windows_curl)
        self.assertEqual(arguments[1], "--disable")
        self.assertIn(module.os.devnull, arguments)

    def test_credential_free_git_fallback_classifies_public_private_and_unknown(self):
        module = load_module()
        policy = {
            "expected_repository": "example/knowledge",
            "expected_branch": "main",
        }
        arguments = module.anonymous_git_probe_arguments(policy)
        rendered = " ".join(arguments)
        self.assertIn(Path(arguments[0]).name.casefold(), {"git", "git.exe"})
        self.assertIn("credential.helper=", arguments)
        self.assertIn("credential.interactive=never", arguments)
        self.assertNotIn("authorization", rendered.casefold())

        public = subprocess.CompletedProcess(arguments, 0, "ref", "")
        private = subprocess.CompletedProcess(
            arguments, 128, "", "fatal: unable to get password from user"
        )
        unknown = subprocess.CompletedProcess(arguments, 128, "", "TLS timeout")
        self.assertEqual(
            module.classify_anonymous_git_probe(public)["status"],
            "repository-public",
        )
        self.assertEqual(
            module.classify_anonymous_git_probe(private)["status"],
            "repository-private",
        )
        self.assertEqual(
            module.classify_anonymous_git_probe(unknown)["status"],
            "repository-privacy-unknown",
        )

    def test_repository_privacy_falls_back_when_api_probe_is_unknown(self):
        module = load_module()
        policy = {
            "expected_repository": "example/knowledge",
            "expected_branch": "main",
        }
        api_unknown = subprocess.CompletedProcess([], 28, "000", "timeout")
        git_private = subprocess.CompletedProcess(
            [], 128, "", "fatal: unable to get password from user"
        )
        with mock.patch.object(
            module.subprocess, "run", side_effect=[api_unknown, git_private]
        ):
            status = module.repository_privacy_status(ROOT, policy)
        self.assertTrue(status["ok"])
        self.assertTrue(status["private"])
        self.assertEqual(status["status"], "repository-private")

    def test_repository_privacy_retries_unknown_git_fallback(self):
        module = load_module()
        policy = {
            "expected_repository": "example/knowledge",
            "expected_branch": "main",
        }
        api_unknown = subprocess.CompletedProcess([], 28, "000", "timeout")
        git_unknown = subprocess.CompletedProcess([], 128, "", "TLS timeout")
        git_private = subprocess.CompletedProcess(
            [], 128, "", "fatal: unable to get password from user"
        )
        with mock.patch.object(
            module.subprocess,
            "run",
            side_effect=[api_unknown, git_unknown, git_unknown, git_private],
        ):
            status = module.repository_privacy_status(ROOT, policy)
        self.assertTrue(status["ok"])
        self.assertTrue(status["private"])

    def test_sync_rejects_public_repository_before_fetch_or_push(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            command(repo, "git", "init", "--initial-branch=main")
            command(repo, "git", "config", "user.name", "AI Knowledge Test")
            command(repo, "git", "config", "user.email", "test@localhost")
            self.write_policy(repo)
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            command(repo, "git", "add", ".")
            command(repo, "git", "commit", "-m", "base")
            command(
                repo,
                "git",
                "remote",
                "add",
                "origin",
                "git@github.com:example/knowledge.git",
            )
            public = {
                "ok": False,
                "private": False,
                "status": "repository-public",
                "retryable": False,
            }
            with mock.patch.object(
                module, "repository_privacy_status", return_value=public
            ), mock.patch.object(module, "git", wraps=module.git) as invoked:
                result = module.sync("origin", pull=True, push=True, root=repo)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "repository-public")
            network_commands = [
                call.args[0]
                for call in invoked.call_args_list
                if call.args and call.args[0] and call.args[0][0] in {"fetch", "push"}
            ]
            self.assertEqual(network_commands, [])

    def test_missing_local_identity_gets_stable_repo_local_bot_values(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            command(repo, "git", "init", "--initial-branch=main")
            self.write_policy(repo)
            configured = module.ensure_local_bot_identity(repo)
            self.assertTrue(configured["ok"])
            self.assertEqual(
                command(repo, "git", "config", "--local", "user.name"),
                "AI Knowledge Bot",
            )
            self.assertEqual(
                command(repo, "git", "config", "--local", "user.email"),
                "ai-knowledge-bot@localhost",
            )
            self.assertEqual(
                command(repo, "git", "config", "--local", "commit.gpgsign"),
                "false",
            )

    def test_hot_sync_reconciles_cold_index_after_every_online_sync(self):
        module = load_hot_sync()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.ROOT = root
            with mock.patch.object(module, "run") as invoked:
                deferred = module.refresh_index_after_sync({"pulled": True})
            self.assertTrue(deferred["ok"])
            self.assertEqual(deferred["status"], "deferred-cold-data-unavailable")
            invoked.assert_not_called()

            (root / ".kb-data").mkdir()
            completed = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"ok": True, "mode": "incremental"}),
                stderr="",
            )
            with mock.patch.object(module, "run", return_value=completed) as invoked:
                updated = module.refresh_index_after_sync({"pulled": False})
            self.assertTrue(updated["ok"])
            self.assertTrue(updated["updated"])
            self.assertFalse(updated["rebuilt"])
            self.assertEqual(updated["mode"], "incremental")
            self.assertEqual(updated["status"], "index-updated-after-sync")
            arguments = invoked.call_args.args[0]
            self.assertIn("update-index", arguments)
            self.assertIn(str(root), arguments)

    def test_hot_sync_commit_is_path_scoped_and_preserves_unrelated_index(self):
        module = load_hot_sync()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command(root, "git", "init", "--initial-branch=main")
            command(root, "git", "config", "user.name", "AI Knowledge Test")
            command(root, "git", "config", "user.email", "test@localhost")
            (root / "profile").mkdir()
            canonical = root / "profile" / "facts.md"
            unrelated = root / "unrelated.txt"
            canonical.write_text("old fact\n", encoding="utf-8")
            unrelated.write_text("old unrelated\n", encoding="utf-8")
            command(root, "git", "add", ".")
            command(root, "git", "commit", "-m", "base")

            canonical.write_text("new fact\n", encoding="utf-8")
            unrelated.write_text("new unrelated\n", encoding="utf-8")
            command(root, "git", "add", "unrelated.txt")
            module.ROOT = root
            module.KNOWLEDGE_PATHS = ("profile",)
            with mock.patch.object(module, "device_id", return_value="device-test"):
                result = module.commit_canonical()

            self.assertTrue(result["committed"])
            committed_paths = command(
                root, "git", "show", "--pretty=format:", "--name-only", "HEAD"
            ).splitlines()
            self.assertEqual(committed_paths, ["profile/facts.md"])
            self.assertEqual(
                command(root, "git", "diff", "--cached", "--name-only"),
                "unrelated.txt",
            )
            self.assertEqual(
                command(root, "git", "show", "HEAD:unrelated.txt"),
                "old unrelated",
            )

    def test_hot_sync_workspace_contention_is_pending_and_refreshes_dashboard(self):
        module = load_hot_sync()
        busy = module.workspace_lock.WorkspaceBusy("busy")
        refreshed = {"ok": True, "status": "dashboard-refreshed"}
        with mock.patch.object(
            module.workspace_lock, "acquire", side_effect=busy
        ), mock.patch.object(
            module, "refresh_dashboard", return_value=refreshed
        ) as dashboard:
            result = module.execute()

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["skipped"], "workspace-mutation-running")
        self.assertEqual(result["dashboard"], refreshed)
        dashboard.assert_called_once_with(module.ROOT)

    def test_hot_sync_storage_contention_is_pending_for_safe_eject(self):
        module = load_hot_sync()
        busy = module.storage_lock.StorageBusy("busy")
        refreshed = {"ok": True, "status": "dashboard-refreshed"}
        with mock.patch.object(
            module.storage_lock, "acquire", side_effect=busy
        ), mock.patch.object(
            module, "perform"
        ) as perform, mock.patch.object(
            module, "refresh_dashboard", return_value=refreshed
        ):
            result = module.execute()

        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertEqual(
            result["skipped"], "storage-operation-running"
        )
        self.assertEqual(result["dashboard"], refreshed)
        perform.assert_not_called()

    def test_windows_hot_sync_skips_main_mac_dashboard(self):
        module = load_hot_sync()
        with mock.patch.object(module.os, "name", "nt"):
            result = module.refresh_dashboard()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dashboard-main-mac-only")

    def test_main_mac_dashboard_is_manual_by_default(self):
        module = load_hot_sync()
        with mock.patch.object(module.os, "name", "posix"), mock.patch.dict(
            module.os.environ,
            {},
            clear=True,
        ), mock.patch.object(module.subprocess, "run") as launched:
            result = module.refresh_dashboard()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "dashboard-refresh-manual")
        launched.assert_not_called()

    def test_dashboard_result_never_hides_or_creates_sync_failure(self):
        module = load_hot_sync()
        failed_sync = {
            "ok": False,
            "action": "hot-sync",
            "status": "degraded",
            "error": "fetch-failed",
        }
        with mock.patch.object(
            module, "perform", return_value=failed_sync
        ), mock.patch.object(
            module,
            "refresh_dashboard",
            return_value={"ok": True, "status": "dashboard-refreshed"},
        ):
            failed = module.execute()
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "fetch-failed")
        self.assertTrue(failed["dashboard"]["ok"])

        with mock.patch.object(
            module,
            "perform",
            return_value={"ok": True, "action": "hot-sync", "status": "ready"},
        ), mock.patch.object(
            module,
            "refresh_dashboard",
            return_value={"ok": False, "status": "dashboard-refresh-failed"},
        ):
            succeeded = module.execute()
        self.assertTrue(succeeded["ok"])
        self.assertFalse(succeeded["dashboard"]["ok"])


if __name__ == "__main__":
    unittest.main()
