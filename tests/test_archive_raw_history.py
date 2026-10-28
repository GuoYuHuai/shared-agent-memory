import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "archive_raw_history.py"
ARCHIVE_HEALTH_SCRIPT = ROOT / "scripts" / "archive_health.py"


def load_module():
    name = "archive_raw_history_test_module"
    spec = importlib.util.spec_from_file_location(name, str(SCRIPT))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ARCHIVE = load_module()


def load_archive_health_module():
    name = "archive_health_test_module"
    spec = importlib.util.spec_from_file_location(name, str(ARCHIVE_HEALTH_SCRIPT))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ARCHIVE_HEALTH = load_archive_health_module()


class RawHistoryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "AI-Knowledge"
        self.home = self.base / "home"
        self.root.mkdir()
        (self.root / "MANIFEST.json").write_text(
            json.dumps({"knowledge_base_id": "archive-test-kb"}), encoding="utf-8"
        )
        (self.root / ".kb-data").mkdir()
        self.codex = self.home / ".codex" / "sessions" / "2026" / "07" / "22" / "codex.jsonl"
        self.claude = self.home / ".claude" / "projects" / "example" / "claude.jsonl"
        self.codex.parent.mkdir(parents=True)
        self.claude.parent.mkdir(parents=True)
        self.device_state = self.base / "device.json"
        self.device_state.write_text(
            json.dumps({
                "format": "ai-knowledge-local-device-v1",
                "knowledge_base_id": "archive-test-kb",
                "id": "device-test",
                "platform_fingerprint": ARCHIVE.device_identity.platform_fingerprint(),
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, action, expected=0, chunk_bytes=1024, include_device_id=True):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["AI_KNOWLEDGE_DEVICE_STATE"] = str(self.device_state)
        arguments = [
                sys.executable,
                str(SCRIPT),
                action,
                "--json",
                "--root", str(self.root),
                "--home", str(self.home),
                "--chunk-bytes", str(chunk_bytes),
            ]
        if include_device_id:
            arguments.extend(["--device-id", "device-test"])
        result = subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout), result.stdout + result.stderr

    @property
    def archive_root(self):
        return self.root / ".kb-data" / "raw-history" / "device-test"

    def object_bytes(self, digest):
        return (self.archive_root / "objects" / "sha256" / digest[:2] / (digest[2:] + ".chunk")).read_bytes()

    def test_plan_is_read_only_apply_preserves_complete_files_and_verify_is_quiet(self):
        secret_a = b'{"message":"CODEX-RAW-SECRET","tool":{"result":42}}\n'
        secret_b = b'{"message":"CLAUDE-RAW-SECRET","attachment":{"name":"brief.pdf"}}\n'
        self.codex.write_bytes(secret_a)
        self.claude.write_bytes(secret_b)

        planned, plan_output = self.run_cli("--plan")
        self.assertTrue(planned["ok"])
        self.assertEqual(planned["source_files"], 2)
        self.assertFalse(self.archive_root.exists())
        self.assertNotIn("CODEX-RAW-SECRET", plan_output)
        self.assertNotIn("CLAUDE-RAW-SECRET", plan_output)

        applied, apply_output = self.run_cli("--apply")
        self.assertEqual(applied["action"], "archived")
        self.assertEqual(applied["source_bytes"], len(secret_a) + len(secret_b))
        self.assertNotIn("RAW-SECRET", apply_output)
        manifest = json.loads((self.archive_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 2)
        restored = []
        for record in manifest["files"]:
            restored.append(b"".join(self.object_bytes(chunk["sha256"]) for chunk in record["chunks"]))
        self.assertEqual(set(restored), {secret_a, secret_b})

        verified, verify_output = self.run_cli("--verify")
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["source_matches_manifest"])
        self.assertTrue(verified["source_exact_manifest"])
        self.assertTrue(verified["source_coverage_ok"])
        self.assertEqual(verified["coverage_mode"], "exact")
        self.assertEqual(verified["verification_mode"], "full")
        self.assertTrue((self.archive_root / "verification.json").is_file())
        self.assertNotIn("RAW-SECRET", verify_output)

    def test_saved_device_identity_is_used_and_absent_cold_root_fails_closed(self):
        self.codex.write_bytes(b"stable")
        self.claude.write_bytes(b"")
        planned, _ = self.run_cli("--plan", include_device_id=False)
        self.assertEqual(planned["device_id"], "device-test")
        (self.root / ".kb-data").rmdir()
        failed, _ = self.run_cli("--plan", expected=2, include_device_id=False)
        self.assertEqual(failed["error"], "cold-data-unavailable")

    def test_append_is_incremental_content_addressed_and_apply_is_idempotent(self):
        first = b"A" * 1024 + b"B" * 1024
        self.codex.write_bytes(first)
        self.claude.write_bytes(b"")
        initial, _ = self.run_cli("--apply")
        self.assertEqual(initial["objects_written"], 2)
        first_manifest = (self.archive_root / "manifest.json").read_bytes()
        first_snapshots = set((self.archive_root / "manifests").iterdir())

        unchanged, _ = self.run_cli("--apply")
        self.assertEqual(unchanged["action"], "unchanged")
        self.assertEqual(unchanged["objects_written"], 0)
        self.assertEqual((self.archive_root / "manifest.json").read_bytes(), first_manifest)
        self.assertEqual(set((self.archive_root / "manifests").iterdir()), first_snapshots)

        self.codex.write_bytes(first + b"C" * 1024)
        appended, _ = self.run_cli("--apply")
        self.assertEqual(appended["action"], "archived")
        self.assertEqual(appended["objects_written"], 1)
        self.assertEqual(appended["bytes_written"], 1024)
        self.assertEqual(len(list((self.archive_root / "manifests").iterdir())), 2)
        verified, _ = self.run_cli("--verify")
        self.assertTrue(verified["ok"])

    def test_apply_rejects_rewritten_archived_prefix_without_publishing(self):
        original = b"append-only-history"
        self.codex.write_bytes(original)
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        prior = (self.archive_root / "manifest.json").read_bytes()

        self.codex.write_bytes(b"X" + original[1:] + b"new-tail")
        failed, _ = self.run_cli("--apply", expected=2)

        self.assertEqual(failed["error"], "source-prefix-changed-before-archive")
        self.assertEqual((self.archive_root / "manifest.json").read_bytes(), prior)

    def test_apply_allows_same_prefix_after_source_inode_replacement(self):
        original = b"stable-prefix"
        self.codex.write_bytes(original)
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        previous_inode = self.codex.stat().st_ino
        replacement = self.codex.with_suffix(".replacement")
        replacement.write_bytes(original + b"appended")
        os.replace(replacement, self.codex)
        self.assertNotEqual(self.codex.stat().st_ino, previous_inode)

        applied, _ = self.run_cli("--apply")

        self.assertEqual(applied["action"], "archived")
        verified, _ = self.run_cli("--verify")
        self.assertTrue(verified["ok"])

    def test_unchanged_apply_preserves_matching_full_verification(self):
        self.codex.write_bytes(b"stable")
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        self.run_cli("--verify")
        before = (self.archive_root / "verification.json").read_bytes()

        unchanged, _ = self.run_cli("--apply")

        self.assertEqual(unchanged["action"], "unchanged")
        self.assertEqual(unchanged["verification_mode"], "full")
        self.assertTrue(unchanged["full_verification_preserved"])
        self.assertEqual(
            (self.archive_root / "verification.json").read_bytes(), before
        )

    def test_source_change_during_copy_defers_only_that_source(self):
        self.codex.write_bytes(b"A" * 4096)
        stable = b"stable sibling"
        self.claude.write_bytes(stable)
        original = ARCHIVE.atomic_create
        changed = {"done": False}

        def mutate_after_first_object(path, payload, digest):
            result = original(path, payload, digest)
            if not changed["done"] and payload.startswith(b"A"):
                changed["done"] = True
                with self.codex.open("ab") as handle:
                    handle.write(b"late mutation")
            return result

        archive_root = self.archive_root
        with mock.patch.object(ARCHIVE, "atomic_create", side_effect=mutate_after_first_object):
            result = ARCHIVE.apply_archive(
                archive_root, "device-test", self.home, "all", 1024
            )

        self.assertEqual(result["action"], "archived")
        self.assertTrue(result["pending"])
        self.assertFalse(result["publication_complete"])
        self.assertEqual(result["deferred_files"], 1)
        self.assertEqual(
            result["deferred_reasons"], {"source-changed-during-read": 1}
        )
        # The body-safe ID is reported, never the source path or content.
        self.assertEqual(len(result["deferred_source_ids"]), 1)
        self.assertNotIn(str(self.codex), json.dumps(result))
        manifest = json.loads(
            (archive_root / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["files"][0]["source"], "claude")
        restored = b"".join(
            self.object_bytes(chunk["sha256"])
            for chunk in manifest["files"][0]["chunks"]
        )
        self.assertEqual(restored, stable)
        # A deferred new source remains visibly uncovered; no full-coverage
        # proof is minted for the partial manifest.
        verified = ARCHIVE.verify_archive(
            archive_root, "device-test", self.home, "all"
        )
        self.assertFalse(verified["ok"])
        self.assertFalse(verified["source_coverage_ok"])
        self.assertEqual(verified["verification_mode"], "coverage-failed")
        self.assertTrue(
            any(item.startswith("source-not-archived:") for item in verified["issues"])
        )

        retried = ARCHIVE.apply_archive(
            archive_root, "device-test", self.home, "all", 1024
        )
        self.assertEqual(retried["action"], "archived")
        self.assertFalse(retried["pending"])
        self.assertTrue(retried["publication_complete"])
        recovered = ARCHIVE.verify_archive(
            archive_root, "device-test", self.home, "all"
        )
        self.assertTrue(recovered["ok"])
        self.assertTrue(recovered["source_exact_manifest"])

    def test_new_source_created_during_scan_is_deferred_without_blocking_publish(self):
        self.codex.write_bytes(b"stable source")
        self.claude.write_bytes(b"")
        original = ARCHIVE.discover_source_files
        calls = {"count": 0}

        def add_source_before_final_discovery(home, source):
            calls["count"] += 1
            if calls["count"] == 2:
                late = self.codex.with_name("late-session.jsonl")
                late.write_bytes(b"late session")
            return original(home, source)

        with mock.patch.object(
            ARCHIVE, "discover_source_files", side_effect=add_source_before_final_discovery
        ):
            result = ARCHIVE.apply_archive(
                self.archive_root, "device-test", self.home, "all", 1024
            )

        self.assertEqual(result["action"], "archived")
        self.assertTrue(result["pending"])
        self.assertEqual(result["deferred_files"], 1)
        self.assertEqual(
            result["deferred_reasons"], {"source-appeared-during-scan": 1}
        )
        manifest = json.loads(
            (self.archive_root / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["files"]), 2)
        self.assertFalse(
            any(record["source_ref"].endswith("late-session.jsonl") for record in manifest["files"])
        )

    def test_source_disappearing_before_read_is_deferred_while_sibling_publishes(self):
        self.codex.write_bytes(b"ephemeral source")
        self.claude.write_bytes(b"stable source")
        signature = ARCHIVE.source_signature
        removed = {"done": False}

        def remove_one_source(path):
            if path == self.codex.resolve() and not removed["done"]:
                removed["done"] = True
                self.codex.unlink()
            return signature(path)

        with mock.patch.object(
            ARCHIVE, "source_signature", side_effect=remove_one_source
        ):
            result = ARCHIVE.apply_archive(
                self.archive_root, "device-test", self.home, "all", 1024
            )

        self.assertEqual(result["action"], "archived")
        self.assertTrue(result["pending"])
        self.assertEqual(result["deferred_files"], 1)
        self.assertEqual(
            result["deferred_reasons"],
            {
                "source-disappeared-during-scan": 1,
                "source-unreadable": 1,
            },
        )
        manifest = json.loads(
            (self.archive_root / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["files"][0]["source"], "claude")

    def test_append_after_scan_publishes_safe_high_water_and_reports_tail(self):
        original_codex = b"already archived"
        self.codex.write_bytes(original_codex)
        self.claude.write_bytes(b"old sibling")
        self.run_cli("--apply")
        self.claude.write_bytes(b"old sibling plus stable progress")

        discover = ARCHIVE.discover_source_files
        calls = {"count": 0}

        def append_before_publish_check(home, source):
            calls["count"] += 1
            if calls["count"] == 2:
                with self.codex.open("ab") as handle:
                    handle.write(b" live tail")
            return discover(home, source)

        with mock.patch.object(
            ARCHIVE,
            "discover_source_files",
            side_effect=append_before_publish_check,
        ):
            result = ARCHIVE.apply_archive(
                self.archive_root, "device-test", self.home, "all", 1024
            )

        self.assertEqual(result["action"], "archived")
        self.assertTrue(result["pending"])
        self.assertFalse(result["source_exact_manifest"])
        self.assertEqual(result["deferred_files"], 1)
        self.assertEqual(result["deferred_tail_bytes"], len(b" live tail"))
        self.assertEqual(
            result["deferred_reasons"], {"source-appended-after-scan": 1}
        )
        manifest = json.loads(
            (self.archive_root / "manifest.json").read_text(encoding="utf-8")
        )
        codex_record = next(
            record for record in manifest["files"] if record["source"] == "codex"
        )
        claude_record = next(
            record for record in manifest["files"] if record["source"] == "claude"
        )
        self.assertEqual(codex_record["size"], len(original_codex))
        self.assertEqual(
            claude_record["size"], len(b"old sibling plus stable progress")
        )
        # The manifest covers a proven prefix and advertises that it is not an
        # exact source snapshot.  Full object verification remains valid.
        verified = ARCHIVE.verify_archive(
            self.archive_root, "device-test", self.home, "all"
        )
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["source_coverage_ok"])
        self.assertFalse(verified["source_exact_manifest"])
        self.assertEqual(verified["coverage_mode"], "append-only-high-water")
        self.assertEqual(verified["unarchived_tail_bytes"], len(b" live tail"))
        self.assertEqual(verified["verification_mode"], "full")

    def test_unstable_publish_check_keeps_prior_record_and_stable_sibling_progress(self):
        original_codex = b"prior immutable evidence"
        self.codex.write_bytes(original_codex)
        self.claude.write_bytes(b"old sibling")
        self.run_cli("--apply")
        prior = json.loads(
            (self.archive_root / "manifest.json").read_text(encoding="utf-8")
        )
        prior_codex = next(
            record for record in prior["files"] if record["source"] == "codex"
        )
        with self.claude.open("ab") as handle:
            handle.write(b" plus stable progress")

        discover = ARCHIVE.discover_source_files
        calls = {"count": 0}

        def append_before_publish_check(home, source):
            calls["count"] += 1
            if calls["count"] == 2:
                with self.codex.open("ab") as handle:
                    handle.write(b" moving tail")
            return discover(home, source)

        def fail_only_moving_source(path, archived_size):
            raise ARCHIVE.ArchiveError("source-changed-during-prefix-check")

        with mock.patch.object(
            ARCHIVE,
            "discover_source_files",
            side_effect=append_before_publish_check,
        ), mock.patch.object(
            ARCHIVE,
            "hash_source_prefix",
            side_effect=fail_only_moving_source,
        ):
            result = ARCHIVE.apply_archive(
                self.archive_root, "device-test", self.home, "all", 1024
            )

        self.assertEqual(result["action"], "archived")
        self.assertTrue(result["pending"])
        self.assertEqual(
            result["deferred_reasons"],
            {"source-changed-during-prefix-check": 1},
        )
        manifest = json.loads(
            (self.archive_root / "manifest.json").read_text(encoding="utf-8")
        )
        current_codex = next(
            record for record in manifest["files"] if record["source"] == "codex"
        )
        current_claude = next(
            record for record in manifest["files"] if record["source"] == "claude"
        )
        self.assertEqual(current_codex, prior_codex)
        self.assertEqual(
            current_claude["size"], len(b"old sibling plus stable progress")
        )

    def test_verify_detects_object_corruption_and_archived_prefix_tamper(self):
        self.codex.write_bytes(b"D" * 1024)
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        manifest = json.loads((self.archive_root / "manifest.json").read_text(encoding="utf-8"))
        codex_record = next(record for record in manifest["files"] if record["source"] == "codex")
        digest = codex_record["chunks"][0]["sha256"]
        object_path = self.archive_root / "objects" / "sha256" / digest[:2] / (digest[2:] + ".chunk")
        object_path.write_bytes(b"X" * 1024)
        failed, output = self.run_cli("--verify", expected=2)
        self.assertFalse(failed["ok"])
        self.assertTrue(any(item.startswith("object-digest-mismatch") for item in failed["issues"]))
        self.assertNotIn("D" * 32, output)

        # Restore by removing the bad object and applying from the still-present source.
        object_path.unlink()
        self.run_cli("--apply")
        self.codex.write_bytes(b"X" + b"D" * 1023 + b"new")
        stale, _ = self.run_cli("--verify", expected=2)
        self.assertFalse(stale["source_matches_manifest"])
        self.assertFalse(stale["source_coverage_ok"])
        self.assertEqual(stale["coverage_mode"], "failed")
        self.assertTrue(stale["object_verification_ok"])
        self.assertEqual(stale["verification_mode"], "coverage-failed")
        self.assertTrue(any(item.startswith("source-prefix-digest-mismatch") for item in stale["issues"]))

    def test_active_append_is_covered_but_reported_as_unarchived_tail(self):
        archived = b"A" * 2048
        tail = b"new live event"
        self.codex.write_bytes(archived)
        self.claude.write_bytes(b"")
        self.run_cli("--apply")

        with self.codex.open("ab") as handle:
            handle.write(tail)
        verified, output = self.run_cli("--verify")

        self.assertTrue(verified["ok"])
        self.assertTrue(verified["source_coverage_ok"])
        self.assertFalse(verified["source_exact_manifest"])
        self.assertFalse(verified["source_matches_manifest"])
        self.assertEqual(verified["coverage_mode"], "append-only-high-water")
        self.assertEqual(verified["unarchived_tail_files"], 1)
        self.assertEqual(verified["unarchived_tail_bytes"], len(tail))
        self.assertEqual(verified["verification_mode"], "full")
        self.assertNotIn("new live event", output)

    def test_shortened_source_and_new_source_fail_coverage(self):
        self.codex.write_bytes(b"A" * 2048)
        self.claude.write_bytes(b"")
        self.run_cli("--apply")

        self.codex.write_bytes(b"A" * 1024)
        shortened, _ = self.run_cli("--verify", expected=2)
        self.assertTrue(any(item.startswith("source-shorter-than-archive") for item in shortened["issues"]))

        self.codex.write_bytes(b"A" * 2048)
        new_source = self.codex.with_name("unarchived.jsonl")
        new_source.write_bytes(b"new source")
        unarchived, _ = self.run_cli("--verify", expected=2)
        self.assertTrue(unarchived["object_verification_ok"])
        self.assertEqual(unarchived["verification_mode"], "coverage-failed")
        self.assertTrue(any(item.startswith("source-not-archived") for item in unarchived["issues"]))

    def test_retired_sources_remain_safely_covered_and_survive_apply(self):
        self.codex.write_bytes(b"archived then retired")
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        original_manifest = (self.archive_root / "manifest.json").read_bytes()
        self.codex.unlink()
        self.claude.unlink()

        verified, _ = self.run_cli("--verify")
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["source_coverage_ok"])
        self.assertFalse(verified["source_exact_manifest"])
        self.assertEqual(verified["coverage_mode"], "retired-sources-covered")
        self.assertEqual(verified["retired_source_files"], 2)
        self.assertEqual(verified["unarchived_tail_bytes"], 0)
        self.assertEqual(verified["verification_mode"], "full")

        unchanged, _ = self.run_cli("--apply")
        self.assertEqual(unchanged["action"], "unchanged")
        self.assertEqual((self.archive_root / "manifest.json").read_bytes(), original_manifest)

    def test_append_during_prefix_check_is_allowed_and_counted(self):
        archived = b"A" * 2048
        tail = b"concurrent append"
        self.codex.write_bytes(archived)
        original_fstat = os.fstat
        calls = {"count": 0}

        def append_before_post_read_fstat(file_descriptor):
            calls["count"] += 1
            if calls["count"] == 2:
                with self.codex.open("ab") as handle:
                    handle.write(tail)
            return original_fstat(file_descriptor)

        with mock.patch.object(ARCHIVE.os, "fstat", side_effect=append_before_post_read_fstat):
            digest, current_size = ARCHIVE.hash_source_prefix(self.codex, len(archived))

        self.assertEqual(digest, ARCHIVE.sha256_bytes(archived))
        self.assertEqual(current_size, len(archived) + len(tail))
        self.assertEqual(calls["count"], 4)

    def test_in_place_mutation_after_first_read_is_caught_on_stable_retry(self):
        archived = b"A" * 2048
        self.codex.write_bytes(archived)
        self.run_cli("--apply")
        original_fstat = os.fstat
        calls = {"count": 0}

        def mutate_before_first_post_read_fstat(file_descriptor):
            calls["count"] += 1
            if calls["count"] == 2:
                with self.codex.open("r+b") as handle:
                    handle.write(b"B")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(self.codex, ns=(1_000_000_000, 1_000_000_000))
            return original_fstat(file_descriptor)

        with mock.patch.object(
            ARCHIVE.os, "fstat", side_effect=mutate_before_first_post_read_fstat
        ):
            result = ARCHIVE.verify_archive(
                self.archive_root, "device-test", self.home, "all"
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["object_verification_ok"])
        self.assertEqual(result["verification_mode"], "coverage-failed")
        self.assertTrue(
            any(item.startswith("source-prefix-digest-mismatch") for item in result["issues"])
        )
        self.assertGreaterEqual(calls["count"], 4)

    def test_continuous_prefix_check_changes_fail_closed_after_bound(self):
        archived = b"A" * 2048
        self.codex.write_bytes(archived)
        original_fstat = os.fstat
        calls = {"count": 0}

        def append_before_every_post_read_fstat(file_descriptor):
            calls["count"] += 1
            if calls["count"] % 2 == 0:
                with self.codex.open("ab") as handle:
                    handle.write(b"x")
            return original_fstat(file_descriptor)

        with mock.patch.object(
            ARCHIVE.os, "fstat", side_effect=append_before_every_post_read_fstat
        ):
            with self.assertRaisesRegex(
                ARCHIVE.ArchiveError, "source-changed-during-prefix-check"
            ):
                ARCHIVE.hash_source_prefix(self.codex, len(archived))
        self.assertEqual(calls["count"], 2 * ARCHIVE.SOURCE_PREFIX_STABLE_READ_ATTEMPTS)

    def test_coverage_failure_atomically_downgrades_peer_visible_full_record(self):
        self.codex.write_bytes(b"covered source")
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        full, _ = self.run_cli("--verify")
        self.assertEqual(full["verification_mode"], "full")
        self.assertTrue(
            ARCHIVE_HEALTH.check_raw_archive_manifest(self.root, "device-test")["ok"]
        )

        self.codex.with_name("new-unarchived.jsonl").write_bytes(b"new source")
        failed, output = self.run_cli("--verify", expected=2)
        self.assertEqual(failed["verification_mode"], "coverage-failed")
        verification = json.loads(
            (self.archive_root / "verification.json").read_text(encoding="utf-8")
        )
        self.assertEqual(verification["mode"], "coverage-failed")
        self.assertEqual(verification["manifest_sha256"], failed["manifest_sha256"])
        peer_style = ARCHIVE_HEALTH.check_raw_archive_manifest(self.root, "device-test")
        self.assertFalse(peer_style["ok"])
        self.assertEqual(peer_style["status"], "full-verification-missing-stale-or-corrupt")
        self.assertNotIn("new source", output)

    def test_unchanged_apply_hashes_rotating_sample_and_rejects_corruption(self):
        self.codex.write_bytes(b"A" * 1024)
        self.claude.write_bytes(b"")
        self.run_cli("--apply")
        manifest = json.loads((self.archive_root / "manifest.json").read_text(encoding="utf-8"))
        record = next(item for item in manifest["files"] if item["chunks"])
        digest = record["chunks"][0]["sha256"]
        object_path = self.archive_root / "objects" / "sha256" / digest[:2] / (digest[2:] + ".chunk")
        object_path.write_bytes(b"Z" * 1024)
        failed, _ = self.run_cli("--apply", expected=2)
        self.assertEqual(failed["error"], "archive-object-corrupt")


if __name__ == "__main__":
    unittest.main()
