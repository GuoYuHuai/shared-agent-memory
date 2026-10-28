import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ingest_history.py"


def load_ingest_module():
    spec = importlib.util.spec_from_file_location("ingest_history_for_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INGEST = load_ingest_module()


def json_line(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def codex_message(role, text, timestamp, event_id=None):
    payload = {
        "type": "message",
        "role": role,
        "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": text}
        ],
    }
    if event_id:
        payload["id"] = event_id
    return {"type": "response_item", "timestamp": timestamp, "payload": payload}


def codex_tool_result(output, timestamp, call_id, result_type="function_call_output"):
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {
            "type": result_type,
            "call_id": call_id,
            "output": output,
        },
    }


class IngestHistoryTests(unittest.TestCase):
    def test_tool_evidence_truncation_is_bounded_and_fail_closed(self):
        prefix = "EXAMPLE_VALUE_PREFIX="
        content, complete = INGEST.evidence_text_from_value(
            prefix + ("x" * (INGEST.EVIDENCE_TEXT_MAX_BYTES * 2))
        )

        self.assertFalse(complete)
        self.assertTrue(content.startswith(prefix))
        self.assertTrue(content.endswith(INGEST.EVIDENCE_TRUNCATION_MARKER))
        self.assertLessEqual(
            len(content.encode("utf-8")),
            INGEST.EVIDENCE_TEXT_MAX_BYTES,
        )

    def test_mixed_nested_binary_structure_preserves_text_but_is_incomplete(self):
        binary_run = "A" * 2048
        cases = (
            (
                {
                    "text": "visible direct text",
                    "image_url": "data:image/png;base64," + binary_run,
                },
                "visible direct text",
            ),
            (
                {
                    "content": [
                        {"text": "first nested text"},
                        {
                            "output": {
                                "text": "second nested text",
                                "metadata": {
                                    "layers": [
                                        {
                                            "image_url": (
                                                "data:image/png;base64,"
                                                + binary_run
                                            )
                                        }
                                    ]
                                },
                            }
                        },
                    ]
                },
                "first nested text\nsecond nested text",
            ),
            (
                {
                    "text": "visible camel-case text",
                    "metadata": {
                        "mimeType": "image/png",
                        "imageUrl": "data:image/png;base64," + binary_run,
                    },
                },
                "visible camel-case text",
            ),
        )
        for value, expected_text in cases:
            with self.subTest(expected_text=expected_text):
                content, complete = INGEST.evidence_text_from_value(value)
                self.assertEqual(content, expected_text)
                self.assertFalse(complete)
                self.assertNotIn(binary_run, content)

    def test_multiple_claude_tool_results_remain_distinct_evidence_events(self):
        source = INGEST.SourceFile(
            "claude",
            Path("claude-session.jsonl"),
            "thread-multiple-results",
            "",
        )
        item = {
            "type": "user",
            "uuid": "claude-result-envelope",
            "parentUuid": "claude-tool-call",
            "sessionId": "thread-multiple-results",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-first",
                        "content": "first textual result",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-second",
                        "content": [{"type": "text", "text": "second textual result"}],
                    },
                ],
            },
        }

        classification, events, reason = INGEST.classify_record(item, source)

        self.assertEqual(classification, "events")
        self.assertIsNone(reason)
        self.assertEqual(
            [event["content"] for event in events],
            ["first textual result", "second textual result"],
        )
        self.assertEqual(
            [event["parent_event_id"] for event in events],
            ["toolu-first", "toolu-second"],
        )
        self.assertTrue(
            all(
                event["role"] == "assistant"
                and event["event_type"] == INGEST.TOOL_RESULT_EVIDENCE_TYPE
                and event["evidence_content_complete"] is True
                for event in events
            )
        )

    def test_codex_image_input_is_preserved_without_indexing_image_bytes(self):
        source = INGEST.SourceFile(
            "codex",
            Path("rollout-019e630b-899e-73e0-95c2-86a6cf825985.jsonl"),
            "019e630b-899e-73e0-95c2-86a6cf825985",
            "",
        )
        item = {
            "type": "response_item",
            "timestamp": "2026-05-26T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>internal</environment_context>",
                    },
                    {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": "data:image/png;base64,very-large-image-data",
                    },
                ],
            },
        }

        classification, event, reason = INGEST.classify_record(item, source)

        self.assertEqual(classification, "event")
        self.assertIsNone(reason)
        self.assertEqual(event["content"], "[non-text input: image]")
        self.assertNotIn("very-large-image-data", event["content"])

    def test_windows_extended_path_alias_has_one_source_identity(self):
        ordinary = r"C:\Users\Admin\.codex\sessions\rollout.jsonl"
        extended = r"\\?\C:\Users\Admin\.codex\sessions\rollout.jsonl"
        self.assertEqual(
            INGEST.source_path_identity(ordinary),
            INGEST.source_path_identity(extended),
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, home, db, checkpoints, *arguments):
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        # pathlib.Path.home() follows USERPROFILE on native Windows even when
        # HOME is set, so isolate both variables from the real user history.
        environment["USERPROFILE"] = str(home)
        command = [
            sys.executable,
            str(SCRIPT),
            *arguments,
            "--db",
            str(db),
            "--checkpoint-dir",
            str(checkpoints),
        ]
        result = subprocess.run(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())
        return json.loads(result.stdout)

    def make_codex_home(self, home, file_name, thread_id, events, partial=b"", cwd="/work/project"):
        sessions = home / ".codex" / "sessions" / "2026" / "07" / "22"
        sessions.mkdir(parents=True, exist_ok=True)
        rollout = sessions / file_name
        with rollout.open("wb") as handle:
            handle.write(json_line({
                "type": "session_meta",
                "timestamp": "2026-07-22T00:00:00Z",
                "payload": {"id": thread_id, "cwd": cwd},
            }))
            for event in events:
                handle.write(json_line(event))
            handle.write(partial)
        state = home / ".codex" / "state_5.sqlite"
        state.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(state))
        connection.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, cwd TEXT)")
        connection.execute(
            "INSERT INTO threads(id, rollout_path, cwd) VALUES (?, ?, ?)",
            (thread_id, str(rollout), cwd),
        )
        connection.commit()
        connection.close()
        return rollout

    def seeded_export_database(self, name, content):
        root = self.base / name
        home = root / "home"
        db = root / "history.sqlite"
        checkpoints = root / "checkpoints"
        self.make_codex_home(
            home,
            "export-source.jsonl",
            "export-thread",
            [codex_message("user", content, "2026-07-22T03:00:00Z")],
        )
        self.run_cli(home, db, checkpoints, "--init-db", "--device-id", "export-device")
        scanned = self.run_cli(
            home,
            db,
            checkpoints,
            "--scan",
            "--source",
            "codex",
            "--device-id",
            "export-device",
        )
        self.assertEqual(scanned["inserted"], 1)
        connection = sqlite3.connect(str(db))
        connection.row_factory = sqlite3.Row
        return home, db, checkpoints, connection

    def export_cursor(self, connection):
        row = connection.execute(
            "SELECT last_observation_id FROM export_cursors WHERE source_device_id=?",
            ("export-device",),
        ).fetchone()
        return None if row is None else int(row[0])

    def relation_export_cursor(self, connection, device_id="export-device"):
        row = connection.execute(
            """
            SELECT last_relation_observation_id
            FROM relation_export_cursors WHERE source_device_id=?
            """,
            (device_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def test_discovery_report_distinguishes_missing_empty_and_present_sources(self):
        # Missing roots, empty roots, and roots with session files must stay
        # distinguishable; silent empty lists made "no history" and "wrong
        # path" identical for Windows onboarding diagnostics.
        bare_home = self.base / "bare-home"
        bare_home.mkdir()
        self.assertEqual(
            INGEST.discovery_report(bare_home),
            {
                "codex": {"root_exists": False, "session_files": 0},
                "claude": {"root_exists": False, "session_files": 0},
            },
        )

        empty_home = self.base / "empty-home"
        (empty_home / ".codex" / "sessions").mkdir(parents=True)
        (empty_home / ".claude" / "projects").mkdir(parents=True)
        self.assertEqual(
            INGEST.discovery_report(empty_home),
            {
                "codex": {"root_exists": True, "session_files": 0},
                "claude": {"root_exists": True, "session_files": 0},
            },
        )

        full_home = self.base / "full-home"
        self.make_codex_home(
            full_home,
            "discovery-source.jsonl",
            "discovery-thread",
            [codex_message("user", "discovery probe", "2026-07-23T00:00:00Z")],
        )
        claude_file = full_home / ".claude" / "projects" / "sample" / "session.jsonl"
        claude_file.parent.mkdir(parents=True)
        claude_file.write_bytes(
            json_line(
                {
                    "type": "user",
                    "uuid": "discovery-u1",
                    "sessionId": "discovery-claude",
                    "message": {"role": "user", "content": "discovery probe"},
                }
            )
        )
        report = INGEST.discovery_report(full_home)
        self.assertEqual(
            report["codex"], {"root_exists": True, "session_files": 1}
        )
        self.assertEqual(
            report["claude"], {"root_exists": True, "session_files": 1}
        )

    def test_stats_output_adds_discovery_without_breaking_existing_fields(self):
        home = self.base / "stats-home"
        home.mkdir()
        db = self.base / "stats-history.sqlite"
        checkpoints = self.base / "stats-checkpoints"
        self.run_cli(home, db, checkpoints, "--init-db", "--device-id", "stats-device")
        stats = self.run_cli(home, db, checkpoints, "--stats", "--device-id", "stats-device")
        for field in (
            "events",
            "by_source",
            "by_role",
            "source_files",
            "checkpoint_coverage",
            "initial_scan_proven",
        ):
            self.assertIn(field, stats)
        self.assertEqual(
            stats["discovery"],
            {
                "codex": {"root_exists": False, "session_files": 0},
                "claude": {"root_exists": False, "session_files": 0},
            },
        )

    def test_streaming_filters_full_text_fts_and_checkpoint_resume(self):
        home = self.base / "home"
        db = self.base / "history.sqlite"
        checkpoints = self.base / "checkpoints"
        codex_secret = "CODEX_KEY=ultraSecretCodex"
        claude_secret = "CLAUDE_KEY=ultraSecretClaude"
        late_event = codex_message(
            "user", "LATE_TOKEN=lateSecretValue", "2026-07-22T00:00:04Z"
        )
        late_bytes = json.dumps(late_event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        rollout = self.make_codex_home(
            home,
            "rollout-shared.jsonl",
            "thread-codex",
            [
                {
                    "type": "response_item",
                    "timestamp": "2026-07-22T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "SYSTEM_NOISE"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-22T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": "<codex_internal_context>INTERNAL_NOISE</codex_internal_context>",
                        }],
                    },
                },
                codex_message("user", codex_secret, "2026-07-22T00:00:01Z"),
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-22T00:00:01Z",
                    "payload": {"type": "user_message", "message": codex_secret},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-22T00:00:02Z",
                    "payload": {"type": "reasoning", "summary": ["TOKEN_NOISE"]},
                },
                codex_message(
                    "assistant", "Assistant exact answer", "2026-07-22T00:00:03Z", "msg-a"
                ),
            ],
            partial=b"not-json\n" + late_bytes[:-1],
        )

        claude_file = home / ".claude" / "projects" / "sample" / "session.jsonl"
        claude_file.parent.mkdir(parents=True, exist_ok=True)
        claude_events = [
            {
                "type": "user",
                "uuid": "claude-u1",
                "sessionId": "thread-claude",
                "timestamp": "2026-07-22T01:00:00Z",
                "cwd": "/claude/project",
                "message": {"role": "user", "content": claude_secret},
            },
            {
                "type": "assistant",
                "uuid": "claude-a1",
                "parentUuid": "claude-u1",
                "sessionId": "thread-claude",
                "timestamp": "2026-07-22T01:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "PRIVATE_REASONING"},
                        {"type": "text", "text": "Claude exact answer"},
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "claude-meta",
                "sessionId": "thread-claude",
                "isMeta": True,
                "message": {"role": "user", "content": "META_SYSTEM_NOISE"},
            },
            {
                "type": "user",
                "uuid": "claude-tool",
                "sessionId": "thread-claude",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "TOOL_RESULT_NOISE"}],
                },
            },
        ]
        with claude_file.open("wb") as handle:
            for event in claude_events:
                handle.write(json_line(event))

        initialized = self.run_cli(
            home, db, checkpoints, "--init-db", "--device-id", "device-one"
        )
        self.assertEqual(initialized["events_schema_version"], 6)
        first = self.run_cli(
            home, db, checkpoints, "--scan", "--source", "all", "--device-id", "device-one"
        )
        self.assertEqual(first["inserted"], 5)
        self.assertEqual(first["evidence"], 1)
        self.assertEqual(first["evidence_incomplete"], 0)
        self.assertEqual(first["malformed"], 1)
        self.assertEqual(first["unparsed_new"], 1)
        self.assertEqual(first["unparsed_pending"], 1)
        self.assertEqual(first["incomplete"], 1)
        rendered_output = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(codex_secret, rendered_output)
        self.assertNotIn(claude_secret, rendered_output)

        connection = sqlite3.connect(str(db))
        self.assertEqual(connection.execute("SELECT count(*) FROM source_events").fetchone()[0], 5)
        quarantined = connection.execute(
            """
            SELECT reason, raw_record, status
            FROM unparsed_source_records
            """
        ).fetchone()
        self.assertEqual(
            quarantined,
            ("invalid-json-or-utf8", b"not-json", "pending"),
        )
        self.assertEqual(
            connection.execute(
                "SELECT content FROM source_events WHERE role='user' ORDER BY source"
            ).fetchall(),
            [(claude_secret,), (codex_secret,)],
        )
        raw = connection.execute(
            "SELECT raw_json FROM source_events WHERE content=?", (codex_secret,)
        ).fetchone()[0]
        self.assertIn(codex_secret, raw)
        self.assertNotIn("SYSTEM_NOISE", "\n".join(
            row[0] for row in connection.execute("SELECT content FROM source_events")
        ))
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM source_events_fts WHERE source_events_fts MATCH 'ultraSecretCodex'"
            ).fetchone()[0],
            1,
        )
        chinese = "没有我的允许暂时不要连接真机测试"
        connection.execute(
            """
            INSERT INTO source_events(
                event_hash, source_device_id, source, source_path, source_offset,
                source_line, thread_id, role, event_type, content, raw_json,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chinese-retrieval-event", "device-one", "codex", "/tmp/source",
                0, 1, "thread", "user", "message", chinese, "{}",
                "2026-07-22T00:00:00Z",
            ),
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM source_events_fts WHERE source_events_fts MATCH ?",
                ('"不要连接真机"',),
            ).fetchone()[0],
            1,
        )
        connection.close()

        checkpoint_files = sorted((checkpoints / "device-one").rglob("*.json"))
        self.assertEqual(len(checkpoint_files), 2)
        for path in checkpoint_files:
            value = json.loads(path.read_text(encoding="utf-8"))
            for field in ("inode", "offset", "size", "mtime_ns", "prefix_hash"):
                self.assertIn(field, value)
            self.assertEqual(value["source_device_id"], "device-one")

        with rollout.open("ab") as handle:
            handle.write(late_bytes[-1:] + b"\n")
        second = self.run_cli(
            home, db, checkpoints, "--scan", "--source", "codex", "--device-id", "device-one"
        )
        self.assertEqual(second["inserted"], 1)
        third = self.run_cli(
            home, db, checkpoints, "--scan", "--source", "codex", "--device-id", "device-one"
        )
        self.assertEqual(third["inserted"], 0)
        connection = sqlite3.connect(str(db))
        self.assertEqual(
            connection.execute(
                "SELECT content FROM source_events WHERE content LIKE 'LATE_TOKEN=%'"
            ).fetchone()[0],
            "LATE_TOKEN=lateSecretValue",
        )
        connection.close()

    def test_tool_results_are_linked_controlled_evidence_end_to_end(self):
        home = self.base / "evidence-home"
        db = self.base / "evidence-history.sqlite"
        imported_db = self.base / "evidence-imported.sqlite"
        checkpoints = self.base / "evidence-checkpoints"
        codex_value = "API_TOKEN=EXAMPLE_CODEX_VALUE_DO_NOT_USE"
        claude_value = "DB_PASSWORD=EXAMPLE_CLAUDE_VALUE_DO_NOT_USE"
        binary_run = "A" * 12000
        self.make_codex_home(
            home,
            "evidence-rollout.jsonl",
            "thread-codex-evidence",
            [
                codex_message(
                    "user",
                    "Read the local example config",
                    "2026-07-24T01:00:00Z",
                    "codex-user-goal",
                ),
                codex_tool_result(
                    codex_value,
                    "2026-07-24T01:00:01Z",
                    "call-codex-text",
                ),
                codex_tool_result(
                    [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + binary_run,
                        },
                        {"type": "input_text", "text": "visible image caption"},
                    ],
                    "2026-07-24T01:00:02Z",
                    "call-codex-image",
                ),
            ],
        )
        claude_file = (
            home
            / ".claude"
            / "projects"
            / "evidence-project"
            / "claude-evidence.jsonl"
        )
        claude_file.parent.mkdir(parents=True)
        claude_records = [
            {
                "type": "user",
                "uuid": "claude-user-goal",
                "sessionId": "thread-claude-evidence",
                "timestamp": "2026-07-24T02:00:00Z",
                "message": {
                    "role": "user",
                    "content": "Read the example database config",
                },
            },
            {
                "type": "assistant",
                "uuid": "claude-tool-call",
                "parentUuid": "claude-user-goal",
                "sessionId": "thread-claude-evidence",
                "timestamp": "2026-07-24T02:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-example-read",
                            "name": "Read",
                            "input": {"file_path": "example.env"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "claude-tool-result",
                "parentUuid": "claude-tool-call",
                "sessionId": "thread-claude-evidence",
                "timestamp": "2026-07-24T02:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-example-read",
                            "content": claude_value,
                        }
                    ],
                },
            },
        ]
        with claude_file.open("wb") as handle:
            for record in claude_records:
                handle.write(json_line(record))

        self.run_cli(
            home, db, checkpoints, "--init-db", "--device-id", "evidence-device"
        )
        scanned = self.run_cli(
            home,
            db,
            checkpoints,
            "--scan",
            "--source",
            "all",
            "--device-id",
            "evidence-device",
        )
        self.assertEqual(scanned["inserted"], 5)
        self.assertEqual(scanned["evidence"], 3)
        self.assertEqual(scanned["evidence_incomplete"], 1)
        rendered = json.dumps(scanned, ensure_ascii=False)
        self.assertNotIn(codex_value, rendered)
        self.assertNotIn(claude_value, rendered)
        self.assertNotIn(binary_run, rendered)

        connection = sqlite3.connect(str(db))
        connection.row_factory = sqlite3.Row
        user_hashes = {
            row["thread_id"]: row["event_hash"]
            for row in connection.execute(
                """
                SELECT thread_id, event_hash
                FROM source_events
                WHERE role='user' AND event_type='message'
                """
            )
        }
        evidence = list(
            connection.execute(
                """
                SELECT source, parent_event_id, related_user_event_hash, role,
                       event_type, content, evidence_content_complete, raw_json
                FROM source_events
                WHERE event_type=?
                ORDER BY source, source_line
                """,
                (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
            )
        )
        self.assertEqual(len(evidence), 3)
        self.assertTrue(all(row["role"] == "assistant" for row in evidence))
        self.assertTrue(
            all(
                row["event_type"] == INGEST.TOOL_RESULT_EVIDENCE_TYPE
                for row in evidence
            )
        )
        codex_rows = [row for row in evidence if row["source"] == "codex"]
        claude_row = next(row for row in evidence if row["source"] == "claude")
        self.assertEqual(codex_rows[0]["content"], codex_value)
        self.assertEqual(codex_rows[0]["parent_event_id"], "call-codex-text")
        self.assertEqual(
            codex_rows[0]["related_user_event_hash"],
            user_hashes["thread-codex-evidence"],
        )
        self.assertEqual(codex_rows[0]["evidence_content_complete"], 1)
        self.assertEqual(claude_row["content"], claude_value)
        self.assertEqual(claude_row["parent_event_id"], "toolu-example-read")
        self.assertEqual(
            claude_row["related_user_event_hash"],
            user_hashes["thread-claude-evidence"],
        )
        self.assertEqual(claude_row["evidence_content_complete"], 1)

        image_row = codex_rows[1]
        self.assertEqual(image_row["evidence_content_complete"], 0)
        self.assertIn(INGEST.EVIDENCE_BINARY_MARKER, image_row["content"])
        self.assertIn("visible image caption", image_row["content"])
        self.assertNotIn(binary_run, image_row["content"])
        # The exact source record remains in the local cold layer, while the
        # FTS-indexed content contains only the controlled placeholder.
        self.assertIn(binary_run, image_row["raw_json"])
        self.assertEqual(
            connection.execute(
                """
                SELECT count(*) FROM source_events_fts
                WHERE source_events_fts MATCH ?
                """,
                ('"' + binary_run[:32] + '"',),
            ).fetchone()[0],
            0,
        )
        connection.close()

        stats = self.run_cli(
            home, db, checkpoints, "--stats", "--device-id", "evidence-device"
        )
        self.assertEqual(stats["evidence_events"], 3)
        self.assertEqual(stats["evidence_incomplete"], 1)
        bundle = self.base / "evidence.capture.bundle"
        self.run_cli(
            home,
            db,
            checkpoints,
            "--export-bundle",
            str(bundle),
            "--device-id",
            "evidence-device",
        )
        self.run_cli(
            home,
            imported_db,
            checkpoints,
            "--init-db",
            "--device-id",
            "receiver-device",
        )
        self.run_cli(
            home,
            imported_db,
            checkpoints,
            "--import-bundle",
            str(bundle),
            "--device-id",
            "receiver-device",
        )
        imported = sqlite3.connect(str(imported_db))
        imported.row_factory = sqlite3.Row
        imported_rows = list(
            imported.execute(
                """
                SELECT content, related_user_event_hash, evidence_content_complete
                FROM source_events WHERE event_type=? ORDER BY source, source_line
                """,
                (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
            )
        )
        self.assertEqual(
            [
                (
                    row["content"],
                    row["related_user_event_hash"],
                    row["evidence_content_complete"],
                )
                for row in imported_rows
            ],
            [
                (
                    row["content"],
                    row["related_user_event_hash"],
                    row["evidence_content_complete"],
                )
                for row in evidence
            ],
        )
        imported.close()

    def test_conflicting_evidence_relations_are_preserved_and_fail_closed(self):
        db = self.base / "relation-conflict.sqlite"
        connection = INGEST.initialize_database(db)
        first_relation = "a" * 64
        second_relation = "b" * 64
        event = {
            "source": "codex",
            "thread_id": "thread-replayed-evidence",
            "event_id": "tool-result-stable-id",
            "parent_event_id": "tool-call-id",
            "related_user_event_hash": first_relation,
            "role": "assistant",
            "event_type": INGEST.TOOL_RESULT_EVIDENCE_TYPE,
            "event_timestamp": "2026-08-17T00:00:00Z",
            "cwd": "/project",
            "content": "body retained but not attached to either target",
            "evidence_content_complete": True,
        }
        event_hash = INGEST.canonical_event_hash(event)
        self.assertTrue(
            INGEST.insert_event(
                connection,
                event,
                event_hash,
                "device-test",
                "/history/one.jsonl",
                10,
                1,
                "{}",
                "2026-08-17T00:00:01Z",
            )
        )
        replay = dict(event, related_user_event_hash=second_relation)
        self.assertFalse(
            INGEST.insert_event(
                connection,
                replay,
                event_hash,
                "device-test",
                "/history/one.jsonl",
                20,
                2,
                "{}",
                "2026-08-17T00:00:02Z",
            )
        )
        # Replaying either observation never reactivates the ambiguous scalar.
        self.assertFalse(
            INGEST.insert_event(
                connection,
                event,
                event_hash,
                "device-test",
                "/history/one.jsonl",
                30,
                3,
                "{}",
                "2026-08-17T00:00:03Z",
            )
        )
        row = connection.execute(
            "SELECT related_user_event_hash,evidence_content_complete,"
            "semantic_revision,content FROM source_events WHERE event_hash=?",
            (event_hash,),
        ).fetchone()
        self.assertIsNone(row["related_user_event_hash"])
        self.assertEqual(row["evidence_content_complete"], 1)
        self.assertEqual(row["semantic_revision"], 2)
        self.assertEqual(row["content"], event["content"])
        relations = {
            value[0]
            for value in connection.execute(
                "SELECT related_user_event_hash "
                "FROM source_event_user_relations WHERE event_hash=?",
                (event_hash,),
            )
        }
        self.assertEqual(relations, {first_relation, second_relation})
        observations = list(
            connection.execute(
                """
                SELECT related_user_event_hash,source_device_id,source_path,
                       source_offset,source_line,provenance_kind
                FROM source_event_user_relation_observations
                WHERE event_hash=? ORDER BY id
                """,
                (event_hash,),
            )
        )
        self.assertEqual(len(observations), 3)
        self.assertTrue(
            all(value[1] == "device-test" for value in observations)
        )
        self.assertEqual([value[3] for value in observations], [10, 20, 30])
        self.assertTrue(
            all(value[5] == "source-history" for value in observations)
        )
        stats = INGEST.database_stats(
            connection, "device-test", self.base / "relation-checkpoints"
        )
        self.assertEqual(stats["evidence_relation_conflicts"], 1)
        bundle = self.base / "relation-conflict.zip"
        exported = INGEST.export_bundle(
            connection, bundle, "device-test"
        )
        self.assertEqual(exported["event_count"], 1)
        self.assertEqual(exported["relation_observation_count"], 3)
        connection.close()

        peer_db = self.base / "relation-conflict-peer.sqlite"
        peer = INGEST.initialize_database(peer_db)
        imported = INGEST.import_bundle(peer, bundle)
        self.assertEqual(imported["events"], 1)
        peer_row = peer.execute(
            "SELECT related_user_event_hash,evidence_content_complete "
            "FROM source_events WHERE event_hash=?",
            (event_hash,),
        ).fetchone()
        self.assertIsNone(peer_row["related_user_event_hash"])
        self.assertEqual(peer_row["evidence_content_complete"], 1)
        peer_relations = {
            value[0]
            for value in peer.execute(
                "SELECT related_user_event_hash "
                "FROM source_event_user_relations WHERE event_hash=?",
                (event_hash,),
            )
        }
        self.assertEqual(peer_relations, {first_relation, second_relation})
        self.assertEqual(
            peer.execute(
                """
                SELECT count(*) FROM source_event_user_relation_observations
                WHERE event_hash=?
                """,
                (event_hash,),
            ).fetchone()[0],
            3,
        )
        peer.close()

    def test_relation_added_after_event_cursor_exports_independent_delta(self):
        db = self.base / "relation-delta.sqlite"
        connection = INGEST.initialize_database(db)
        first_relation = "c" * 64
        second_relation = "d" * 64
        event = {
            "source": "codex",
            "thread_id": "thread-relation-delta",
            "event_id": "stable-result-id",
            "parent_event_id": "stable-call-id",
            "related_user_event_hash": first_relation,
            "role": "assistant",
            "event_type": INGEST.TOOL_RESULT_EVIDENCE_TYPE,
            "event_timestamp": "2026-08-17T01:00:00Z",
            "cwd": "/project",
            "content": "same immutable result",
            "evidence_content_complete": True,
        }
        event_hash = INGEST.canonical_event_hash(event)
        self.assertTrue(
            INGEST.insert_event(
                connection,
                event,
                event_hash,
                "delta-device",
                "/history/first.jsonl",
                10,
                1,
                "{}",
                "2026-08-17T01:00:01Z",
            )
        )
        first_bundle = self.base / "relation-before-cursor.zip"
        first_export = INGEST.export_bundle(
            connection, first_bundle, "delta-device"
        )
        self.assertEqual(first_export["event_count"], 1)
        self.assertEqual(first_export["relation_observation_count"], 1)
        self.assertEqual(first_export["cursor_from"], 0)
        self.assertGreater(first_export["cursor_to"], 0)
        self.assertEqual(first_export["relation_cursor_from"], 0)
        self.assertGreater(first_export["relation_cursor_to"], 0)

        peer = INGEST.initialize_database(self.base / "relation-delta-peer.sqlite")
        first_import = INGEST.import_bundle(peer, first_bundle)
        self.assertEqual(first_import["inserted"], 1)
        self.assertEqual(first_import["relation_observations"], 1)
        initial_peer = peer.execute(
            """
            SELECT related_user_event_hash,evidence_content_complete
            FROM source_events WHERE event_hash=?
            """,
            (event_hash,),
        ).fetchone()
        self.assertEqual(initial_peer["related_user_event_hash"], first_relation)
        self.assertEqual(initial_peer["evidence_content_complete"], 1)

        replay = dict(event, related_user_event_hash=second_relation)
        self.assertFalse(
            INGEST.insert_event(
                connection,
                replay,
                event_hash,
                "delta-device",
                "/history/fork.jsonl",
                20,
                2,
                "{}",
                "2026-08-17T01:00:02Z",
            )
        )
        event_cursor_before = first_export["cursor_to"]
        relation_cursor_before = first_export["relation_cursor_to"]
        second_bundle = self.base / "relation-after-cursor.zip"
        second_export = INGEST.export_bundle(
            connection, second_bundle, "delta-device"
        )
        self.assertEqual(second_export["event_count"], 0)
        self.assertEqual(second_export["cursor_from"], event_cursor_before)
        self.assertEqual(second_export["cursor_to"], event_cursor_before)
        self.assertEqual(second_export["relation_observation_count"], 1)
        self.assertEqual(
            second_export["relation_cursor_from"], relation_cursor_before
        )
        self.assertGreater(
            second_export["relation_cursor_to"], relation_cursor_before
        )
        with zipfile.ZipFile(str(second_bundle), "r") as archive:
            self.assertEqual(archive.read("events.jsonl"), b"")
            relation_records = archive.read("relations.jsonl").splitlines()
            self.assertEqual(len(relation_records), 1)
            relation_record = json.loads(relation_records[0])
            self.assertEqual(
                relation_record["related_user_event_hash"], second_relation
            )
            self.assertEqual(relation_record["source_ref"], "fork.jsonl")

        second_import = INGEST.import_bundle(peer, second_bundle)
        self.assertEqual(second_import["events"], 0)
        self.assertEqual(second_import["relation_observations"], 1)
        self.assertEqual(second_import["relation_candidates_inserted"], 1)
        peer_projection = peer.execute(
            """
            SELECT related_user_event_hash,evidence_content_complete
            FROM source_events WHERE event_hash=?
            """,
            (event_hash,),
        ).fetchone()
        self.assertIsNone(peer_projection["related_user_event_hash"])
        self.assertEqual(peer_projection["evidence_content_complete"], 1)
        self.assertEqual(
            {
                value[0]
                for value in peer.execute(
                    """
                    SELECT related_user_event_hash
                    FROM source_event_user_relations WHERE event_hash=?
                    """,
                    (event_hash,),
                )
            },
            {first_relation, second_relation},
        )
        self.assertEqual(
            peer.execute(
                """
                SELECT count(*) FROM source_event_user_relation_observations
                WHERE event_hash=?
                """,
                (event_hash,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.relation_export_cursor(connection, "delta-device"),
            second_export["relation_cursor_to"],
        )
        connection.close()
        peer.close()

    def test_v3_checkpoint_backfill_recovers_previously_ignored_tool_result(self):
        home = self.base / "backfill-home"
        db = self.base / "backfill-history.sqlite"
        checkpoints = self.base / "backfill-checkpoints"
        exact_value = "TOKEN=EXAMPLE_BACKFILL_VALUE_DO_NOT_USE"
        self.make_codex_home(
            home,
            "backfill-rollout.jsonl",
            "thread-backfill",
            [
                codex_message(
                    "user",
                    "Read the backfill fixture",
                    "2026-07-24T03:00:00Z",
                    "backfill-user",
                ),
                codex_tool_result(
                    exact_value,
                    "2026-07-24T03:00:01Z",
                    "call-backfill",
                ),
            ],
        )
        connection = INGEST.initialize_database(db)
        first = INGEST.scan_history(
            connection,
            "codex",
            "backfill-device",
            checkpoints,
            home=home,
        )
        self.assertEqual(first["inserted"], 2)
        connection.execute(
            "DELETE FROM source_events WHERE event_type=?",
            (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
        )
        connection.commit()

        checkpoint_file = next(
            (checkpoints / "backfill-device" / "codex").glob("*.json")
        )
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        checkpoint["parser_version"] = INGEST.CHECKPOINT_PARSER_VERSION - 1
        checkpoint_file.write_text(
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        backfilled = INGEST.scan_history(
            connection,
            "codex",
            "backfill-device",
            checkpoints,
            home=home,
        )
        self.assertEqual(backfilled["inserted"], 1)
        self.assertEqual(backfilled["duplicates"], 1)
        self.assertEqual(backfilled["evidence"], 1)
        row = connection.execute(
            """
            SELECT e.content, e.related_user_event_hash,
                   e.evidence_content_complete, u.event_hash
            FROM source_events AS e
            JOIN source_events AS u
              ON u.thread_id=e.thread_id
             AND u.role='user'
             AND u.event_type='message'
            WHERE e.event_type=?
            """,
            (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
        ).fetchone()
        self.assertEqual(row[0], exact_value)
        self.assertEqual(row[1], row[3])
        self.assertEqual(row[2], 1)
        updated_checkpoint = json.loads(
            checkpoint_file.read_text(encoding="utf-8")
        )
        self.assertEqual(
            updated_checkpoint["parser_version"],
            INGEST.CHECKPOINT_PARSER_VERSION,
        )
        connection.close()

    def test_v3_replay_downgrades_mixed_evidence_and_never_promotes_it(self):
        home = self.base / "completeness-home"
        db = self.base / "completeness-history.sqlite"
        checkpoints = self.base / "completeness-checkpoints"
        visible_text = "EXAMPLE_MIXED_RESULT_VALUE_DO_NOT_USE"
        binary_run = "B" * 4096
        mixed_output = {
            "text": visible_text,
            "metadata": {
                "nested": [
                    {
                        "image_url": "data:image/png;base64," + binary_run,
                    }
                ]
            },
        }
        self.make_codex_home(
            home,
            "completeness-rollout.jsonl",
            "thread-completeness",
            [
                codex_message(
                    "user",
                    "Inspect the mixed example result",
                    "2026-07-24T04:00:00Z",
                    "completeness-user",
                ),
                codex_tool_result(
                    mixed_output,
                    "2026-07-24T04:00:01Z",
                    "call-completeness",
                ),
            ],
        )
        connection = INGEST.initialize_database(db)
        # Simulate the v3 parser bug: it followed the first text field and
        # failed to notice the nested image, then published a v3 checkpoint.
        with mock.patch.object(INGEST, "CHECKPOINT_PARSER_VERSION", 3), mock.patch.object(
            INGEST,
            "_evidence_structure_is_complete",
            return_value=True,
        ):
            first = INGEST.scan_history(
                connection,
                "codex",
                "completeness-device",
                checkpoints,
                home=home,
            )
        self.assertEqual(first["inserted"], 2)
        checkpoint_file = next(
            (checkpoints / "completeness-device" / "codex").glob("*.json")
        )
        self.assertEqual(
            json.loads(checkpoint_file.read_text(encoding="utf-8"))[
                "parser_version"
            ],
            3,
        )
        self.assertEqual(
            connection.execute(
                """
                SELECT evidence_content_complete
                FROM source_events WHERE event_type=?
                """,
                (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
            ).fetchone()[0],
            1,
        )

        replayed = INGEST.scan_history(
            connection,
            "codex",
            "completeness-device",
            checkpoints,
            home=home,
        )
        self.assertEqual(replayed["inserted"], 0)
        self.assertEqual(replayed["duplicates"], 2)
        row = connection.execute(
            """
            SELECT content, evidence_content_complete, raw_json
            FROM source_events WHERE event_type=?
            """,
            (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
        ).fetchone()
        self.assertEqual(row[0], visible_text)
        self.assertEqual(row[1], 0)
        self.assertIn(binary_run, row[2])
        self.assertNotIn(binary_run, row[0])
        self.assertEqual(
            json.loads(checkpoint_file.read_text(encoding="utf-8"))[
                "parser_version"
            ],
            INGEST.CHECKPOINT_PARSER_VERSION,
        )

        # Even a later legacy observation claiming completeness cannot
        # promote the global event projection from 0 back to 1.
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        checkpoint["parser_version"] = 3
        checkpoint_file.write_text(
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with mock.patch.object(
            INGEST,
            "_evidence_structure_is_complete",
            return_value=True,
        ):
            legacy_replay = INGEST.scan_history(
                connection,
                "codex",
                "completeness-device",
                checkpoints,
                home=home,
            )
        self.assertEqual(legacy_replay["duplicates"], 2)
        self.assertEqual(
            connection.execute(
                """
                SELECT evidence_content_complete
                FROM source_events WHERE event_type=?
                """,
                (INGEST.TOOL_RESULT_EVIDENCE_TYPE,),
            ).fetchone()[0],
            0,
        )
        connection.close()

    def test_evidence_schema_migration_is_atomic_and_retryable(self):
        db = self.base / "legacy-evidence-schema.sqlite"
        legacy_schema = INGEST.SCHEMA_PATH.read_text(encoding="utf-8")
        legacy_schema = legacy_schema.replace(
            "VALUES ('events_schema_version', '0');",
            "VALUES ('events_schema_version', '2');",
        )
        legacy_schema = legacy_schema.replace(
            "    related_user_event_hash TEXT,\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "    evidence_content_complete INTEGER NOT NULL DEFAULT 1\n"
            "        CHECK (evidence_content_complete IN (0, 1)),\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "    semantic_revision INTEGER NOT NULL DEFAULT 1,\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "CREATE INDEX IF NOT EXISTS source_events_linked_evidence_idx\n"
            "ON source_events(\n"
            "    source, thread_id, related_user_event_hash, role, event_type\n"
            ");\n\n",
            "",
        )
        evidence_changes_start = legacy_schema.index(
            "CREATE TABLE IF NOT EXISTS semantic_evidence_changes ("
        )
        evidence_changes_end = legacy_schema.index(
            "-- Complete JSONL records", evidence_changes_start
        )
        legacy_schema = (
            legacy_schema[:evidence_changes_start]
            + legacy_schema[evidence_changes_end:]
        )
        legacy = sqlite3.connect(str(db))
        legacy.executescript(legacy_schema)
        legacy.execute(
            """
            INSERT INTO source_events(
                event_hash, source_device_id, source, source_path, source_offset,
                source_line, thread_id, role, event_type, content, raw_json,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-event",
                "legacy-device",
                "codex",
                "legacy.jsonl",
                0,
                1,
                "legacy-thread",
                "user",
                "message",
                "legacy content",
                "{}",
                "2026-07-24T00:00:00Z",
            ),
        )
        legacy.commit()
        legacy.close()

        real_connect = sqlite3.connect

        class FailedEvidenceMigrationConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                normalized = " ".join(str(sql).split()).lower()
                if "add column evidence_content_complete" in normalized:
                    raise sqlite3.OperationalError("simulated migration failure")
                return super().execute(sql, parameters)

        def connect_with_failed_migration(*args, **kwargs):
            kwargs["factory"] = FailedEvidenceMigrationConnection
            return real_connect(*args, **kwargs)

        with mock.patch.object(
            INGEST.sqlite3,
            "connect",
            side_effect=connect_with_failed_migration,
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "simulated migration failure",
            ):
                INGEST.initialize_database(db)

        rolled_back = sqlite3.connect(str(db))
        try:
            columns = {
                row[1]
                for row in rolled_back.execute("PRAGMA table_info(source_events)")
            }
            self.assertNotIn("related_user_event_hash", columns)
            self.assertNotIn("evidence_content_complete", columns)
            self.assertNotIn("semantic_revision", columns)
            self.assertIsNone(
                rolled_back.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='semantic_evidence_changes'"
                ).fetchone()
            )
            self.assertEqual(
                rolled_back.execute(
                    """
                    SELECT value FROM schema_meta
                    WHERE key='events_schema_version'
                    """
                ).fetchone()[0],
                "2",
            )
            self.assertEqual(
                rolled_back.execute(
                    "SELECT count(*) FROM source_events"
                ).fetchone()[0],
                1,
            )
        finally:
            rolled_back.close()

        migrated = INGEST.initialize_database(db)
        try:
            columns = {
                row[1]
                for row in migrated.execute("PRAGMA table_info(source_events)")
            }
            self.assertIn("related_user_event_hash", columns)
            self.assertIn("evidence_content_complete", columns)
            self.assertIn("semantic_revision", columns)
            self.assertIsNotNone(
                migrated.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='semantic_evidence_changes'"
                ).fetchone()
            )
            self.assertIsNotNone(
                migrated.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='source_events_linked_evidence_idx'"
                ).fetchone()
            )
            self.assertEqual(
                migrated.execute(
                    """
                    SELECT value FROM schema_meta
                    WHERE key='events_schema_version'
                    """
                ).fetchone()[0],
                "6",
            )
            self.assertEqual(
                migrated.execute(
                    """
                    SELECT evidence_content_complete
                    FROM source_events WHERE event_hash='legacy-event'
                    """
                ).fetchone()[0],
                1,
            )
        finally:
            migrated.close()

        # Reopening a current large database must not replay migration
        # backfill.  Those INSERT ... SELECT statements are intentionally
        # limited to the one atomic v2-to-v6 transition.
        current = sqlite3.connect(str(db))
        statements = []
        current.set_trace_callback(statements.append)
        try:
            INGEST.migrate_evidence_schema(current)
        finally:
            current.close()
        rendered_statements = "\n".join(statements).lower()
        self.assertNotIn(
            "insert or ignore into source_event_user_relations",
            rendered_statements,
        )
        self.assertNotIn(
            "insert or ignore into semantic_evidence_changes",
            rendered_statements,
        )
        self.assertNotIn(
            "insert or ignore into source_event_user_relation_observations",
            rendered_statements,
        )

    def test_future_schema_version_fails_closed_without_writes(self):
        db = self.base / "future-schema.sqlite"
        connection = sqlite3.connect(str(db))
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO schema_meta(key,value)
            VALUES ('events_schema_version','999');
            CREATE TABLE source_events(id INTEGER PRIMARY KEY);
            CREATE TABLE future_only_evidence(marker TEXT NOT NULL);
            INSERT INTO future_only_evidence(marker) VALUES ('preserve-me');
            """
        )
        connection.commit()
        connection.close()
        before = db.read_bytes()

        with self.assertRaisesRegex(
            INGEST.IngestError, "unsupported-events-schema-version"
        ):
            INGEST.initialize_database(db)

        self.assertEqual(db.read_bytes(), before)
        preserved = sqlite3.connect(str(db))
        try:
            self.assertEqual(
                preserved.execute(
                    """
                    SELECT value FROM schema_meta
                    WHERE key='events_schema_version'
                    """
                ).fetchone()[0],
                "999",
            )
            self.assertEqual(
                preserved.execute(
                    "SELECT marker FROM future_only_evidence"
                ).fetchone()[0],
                "preserve-me",
            )
            self.assertIsNone(
                preserved.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table'
                      AND name='source_event_user_relation_observations'
                    """
                ).fetchone()
            )
        finally:
            preserved.close()

    def test_unknown_and_malformed_complete_records_are_durable_and_replayable(self):
        module = load_ingest_module()
        home = self.base / "future-home"
        db = self.base / "future-history.sqlite"
        checkpoints = self.base / "future-checkpoints"
        future_secret = "FUTURE_ACCOUNT=future-value"
        future_record = {
            "type": "future_user_message_v2",
            "timestamp": "2026-07-23T03:00:00Z",
            "payload": {"role": "user", "text": future_secret},
        }
        source_path = self.make_codex_home(
            home,
            "future-source.jsonl",
            "future-thread",
            [future_record],
            partial=b"\xffmalformed-but-complete\n",
        )
        connection = module.initialize_database(db)
        first = module.scan_history(
            connection,
            "codex",
            "future-device",
            checkpoints,
            home=home,
        )
        self.assertEqual(first["inserted"], 0)
        self.assertEqual(first["unsupported"], 1)
        self.assertEqual(first["malformed"], 1)
        self.assertEqual(first["unparsed_new"], 2)
        self.assertEqual(first["unparsed_pending"], 2)

        checkpoint_file = next(
            (checkpoints / "future-device" / "codex").glob("*.json")
        )
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["offset"], source_path.stat().st_size)
        quarantined = list(
            connection.execute(
                """
                SELECT reason, raw_record, status
                FROM unparsed_source_records
                ORDER BY source_line
                """
            )
        )
        self.assertEqual(quarantined[0][0], "codex-unknown-record-type")
        self.assertEqual(quarantined[0][1], json_line(future_record).rstrip(b"\n"))
        self.assertEqual(quarantined[1][1], b"\xffmalformed-but-complete")
        self.assertTrue(all(row[2] == "pending" for row in quarantined))

        original_classifier = module.classify_record

        def upgraded_classifier(item, source_file):
            if item.get("type") == "future_user_message_v2":
                return (
                    "event",
                    {
                        "source": "codex",
                        "thread_id": source_file.thread_id,
                        "event_id": None,
                        "parent_event_id": None,
                        "role": "user",
                        "event_type": "message",
                        "event_timestamp": item.get("timestamp"),
                        "cwd": source_file.cwd or None,
                        "content": item["payload"]["text"],
                    },
                    None,
                )
            return original_classifier(item, source_file)

        module.UNPARSED_PARSER_VERSION += 1
        with mock.patch.object(
            module, "classify_record", side_effect=upgraded_classifier
        ):
            second = module.scan_history(
                connection,
                "codex",
                "future-device",
                checkpoints,
                home=home,
            )
        self.assertEqual(second["unparsed_retried"], 2)
        self.assertEqual(second["unparsed_resolved"], 1)
        self.assertEqual(second["unparsed_pending"], 1)
        self.assertEqual(
            connection.execute(
                "SELECT content FROM source_events WHERE role='user'"
            ).fetchone()[0],
            future_secret,
        )
        statuses = [
            tuple(row)
            for row in
            connection.execute(
                "SELECT status FROM unparsed_source_records ORDER BY source_line"
            )
        ]
        self.assertEqual(statuses, [("resolved",), ("pending",)])
        connection.close()

    def test_checkpoint_visibility_implies_main_database_durability(self):
        # Power-loss invariant: once a per-file checkpoint JSON exists, the
        # events it covers must survive losing the -wal file (WAL commits with
        # synchronous=NORMAL are not durable on their own). A copy of the main
        # database taken WITHOUT its -wal/-shm siblings simulates the disk
        # state after power failure.
        module = load_ingest_module()
        home = self.base / "home"
        db = self.base / "kb" / "history.sqlite"
        checkpoints = self.base / "kb" / "checkpoints"
        self.make_codex_home(
            home,
            "rollout-durability.jsonl",
            "thread-durability",
            [
                codex_message("user", "durability probe %d" % index, "2026-07-22T01:00:0%dZ" % index)
                for index in range(5)
            ],
        )
        db.parent.mkdir(parents=True, exist_ok=True)
        connection = module.initialize_database(db)
        # Snapshot the bare main database file (no -wal/-shm) at the exact
        # moment the checkpoint JSON becomes visible; a clean process exit
        # would auto-checkpoint the WAL and hide the vulnerable window.
        survivor = self.base / "kb" / "history-at-checkpoint-write.sqlite"
        original_write = module.atomic_write_json

        def intercepted(path, value):
            if not survivor.exists() and str(path).startswith(str(checkpoints)):
                shutil.copyfile(db, survivor)
            original_write(path, value)

        try:
            with mock.patch.object(module, "atomic_write_json", side_effect=intercepted):
                totals = module.scan_history(
                    connection, "codex", "device-durability", checkpoints, home=home
                )
        finally:
            connection.close()
        self.assertGreaterEqual(totals["inserted"], 5)
        self.assertTrue(survivor.exists())

        snapshot = sqlite3.connect(str(survivor))
        try:
            count = snapshot.execute(
                "SELECT count(*) FROM source_events WHERE source_device_id='device-durability'"
            ).fetchone()[0]
        finally:
            snapshot.close()
        self.assertGreaterEqual(count, 5)

    def test_busy_wal_checkpoint_never_publishes_capture_cursor(self):
        module = load_ingest_module()
        home = self.base / "busy-home"
        db = self.base / "busy-kb" / "history.sqlite"
        checkpoints = self.base / "busy-kb" / "checkpoints"
        self.make_codex_home(
            home,
            "rollout-busy-reader.jsonl",
            "thread-busy-reader",
            [codex_message("user", "busy checkpoint probe", "2026-07-22T01:30:00Z")],
        )
        db.parent.mkdir(parents=True, exist_ok=True)
        writer = module.initialize_database(db)
        reader = sqlite3.connect(str(db), timeout=1)
        # Establish a read snapshot before the writer creates new WAL frames.
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM source_events").fetchone()
        try:
            with self.assertRaisesRegex(module.IngestError, "wal-checkpoint-busy"):
                module.scan_history(
                    writer, "codex", "device-busy", checkpoints, home=home
                )
            self.assertEqual(
                list((checkpoints / "device-busy").rglob("*.json"))
                if (checkpoints / "device-busy").exists()
                else [],
                [],
            )
        finally:
            reader.rollback()
            reader.close()

        # The committed WAL rows are revisited from offset zero and deduplicated;
        # once the blocker is gone the durable cursor may safely become visible.
        retried = module.scan_history(
            writer, "codex", "device-busy", checkpoints, home=home
        )
        self.assertEqual(retried["inserted"], 0)
        self.assertGreaterEqual(retried["duplicates"], 1)
        self.assertEqual(
            writer.execute(
                "SELECT count(*) FROM source_events WHERE source_device_id='device-busy'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            len(list((checkpoints / "device-busy").rglob("*.json"))),
            1,
        )
        writer.close()

    def test_existing_unicode61_fts_migrates_without_losing_events(self):
        db = self.base / "legacy-history.sqlite"
        legacy_schema = INGEST.SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "tokenize='trigram'",
            "tokenize='unicode61'",
        )
        connection = sqlite3.connect(str(db))
        connection.executescript(legacy_schema)
        chinese = "没有我的允许暂时不要连接真机测试"
        connection.execute(
            """
            INSERT INTO source_events(
                event_hash, source_device_id, source, source_path, source_offset,
                source_line, thread_id, role, event_type, content, raw_json,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-chinese-event", "device-one", "codex", "/tmp/source",
                0, 1, "thread", "user", "message", chinese, "{}",
                "2026-07-22T00:00:00Z",
            ),
        )
        connection.commit()
        connection.close()

        migrated = INGEST.initialize_database(db)
        try:
            fts_sql = migrated.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_events_fts'"
            ).fetchone()[0]
            self.assertIn("tokenize='trigram'", fts_sql.replace(" ", ""))
            self.assertEqual(
                migrated.execute("SELECT count(*) FROM source_events").fetchone()[0],
                1,
            )
            self.assertEqual(
                migrated.execute(
                    "SELECT count(*) FROM source_events_fts WHERE source_events_fts MATCH ?",
                    ('"不要连接真机"',),
                ).fetchone()[0],
                1,
            )
        finally:
            migrated.close()

    def test_unicode61_migration_rolls_back_failed_rebuild_and_retries(self):
        db = self.base / "failed-legacy-migration.sqlite"
        legacy_schema = INGEST.SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "tokenize='trigram'",
            "tokenize='unicode61'",
        )
        connection = sqlite3.connect(str(db))
        connection.executescript(legacy_schema)
        chinese = "没有我的允许暂时不要连接真机测试"
        connection.execute(
            """
            INSERT INTO source_events(
                event_hash, source_device_id, source, source_path, source_offset,
                source_line, thread_id, role, event_type, content, raw_json,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "failed-migration-event", "device-one", "codex", "/tmp/source",
                0, 1, "thread", "user", "message", chinese, "{}",
                "2026-07-22T00:00:00Z",
            ),
        )
        connection.commit()
        connection.close()

        real_connect = sqlite3.connect

        class FailedRebuildConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                normalized = " ".join(str(sql).split()).lower()
                if "values('rebuild')" in normalized:
                    raise sqlite3.OperationalError("database or disk is full")
                return super().execute(sql, parameters)

        def connect_with_failed_rebuild(*args, **kwargs):
            kwargs["factory"] = FailedRebuildConnection
            return real_connect(*args, **kwargs)

        with mock.patch.object(
            INGEST.sqlite3, "connect", side_effect=connect_with_failed_rebuild
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk is full"):
                INGEST.initialize_database(db)

        rolled_back = sqlite3.connect(str(db))
        try:
            fts_sql = rolled_back.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_events_fts'"
            ).fetchone()[0]
            self.assertIn("tokenize='unicode61'", fts_sql.replace(" ", ""))
            self.assertEqual(
                rolled_back.execute("SELECT count(*) FROM source_events").fetchone()[0],
                1,
            )
            self.assertEqual(
                rolled_back.execute(
                    "SELECT count(*) FROM source_events_fts WHERE source_events_fts MATCH ?",
                    (chinese,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                rolled_back.execute(
                    """
                    SELECT count(*) FROM sqlite_master
                    WHERE type='trigger' AND name LIKE 'source_events_a%'
                    """
                ).fetchone()[0],
                3,
            )
        finally:
            rolled_back.close()

        retried = INGEST.initialize_database(db)
        try:
            fts_sql = retried.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_events_fts'"
            ).fetchone()[0]
            self.assertIn("tokenize='trigram'", fts_sql.replace(" ", ""))
            self.assertEqual(
                retried.execute(
                    "SELECT count(*) FROM source_events_fts WHERE source_events_fts MATCH ?",
                    ('"不要连接真机"',),
                ).fetchone()[0],
                1,
            )
        finally:
            retried.close()

    def test_two_device_bundles_dedupe_same_event_and_keep_different_event(self):
        home_a = self.base / "home-a"
        home_b = self.base / "home-b"
        db_a = self.base / "a.sqlite"
        db_b = self.base / "b.sqlite"
        checkpoints = self.base / "checkpoints"
        same = codex_message("user", "same portable fact", "2026-07-22T02:00:00Z")
        different = codex_message("user", "different device fact", "2026-07-22T02:00:01Z")
        self.make_codex_home(
            home_a, "machine-a.jsonl", "portable-thread", [same], cwd="/machine/a/project"
        )
        rollout_b = self.make_codex_home(
            home_b, "machine-b.jsonl", "portable-thread", [same], cwd="/machine/b/project"
        )

        self.run_cli(home_a, db_a, checkpoints, "--init-db", "--device-id", "device-a")
        self.run_cli(
            home_a, db_a, checkpoints, "--scan", "--source", "codex", "--device-id", "device-a"
        )
        bundle_a = self.base / "device-a.capture.bundle"
        exported_a = self.run_cli(
            home_a,
            db_a,
            checkpoints,
            "--export-bundle",
            str(bundle_a),
            "--device-id",
            "device-a",
        )
        self.assertEqual(exported_a["event_count"], 1)
        with zipfile.ZipFile(str(bundle_a), "r") as archive:
            payload = archive.read("events.jsonl").decode("utf-8")
            record = json.loads(payload)
            self.assertNotIn("source_path", record)
            self.assertEqual(record["source_ref"], "machine-a.jsonl")
            self.assertNotIn(str(home_a), payload)

        self.run_cli(home_b, db_b, checkpoints, "--init-db", "--device-id", "device-b")
        imported = self.run_cli(
            home_b, db_b, checkpoints, "--import-bundle", str(bundle_a), "--device-id", "device-b"
        )
        self.assertEqual(imported["inserted"], 1)
        imported_again = self.run_cli(
            home_b, db_b, checkpoints, "--import-bundle", str(bundle_a), "--device-id", "device-b"
        )
        self.assertTrue(imported_again["already_imported"])

        duplicate_scan = self.run_cli(
            home_b, db_b, checkpoints, "--scan", "--source", "codex", "--device-id", "device-b"
        )
        self.assertEqual(duplicate_scan["inserted"], 0)
        self.assertEqual(duplicate_scan["duplicates"], 1)
        connection = sqlite3.connect(str(db_b))
        self.assertEqual(connection.execute("SELECT count(*) FROM source_events").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT count(*) FROM event_observations").fetchone()[0], 2)
        connection.close()

        with rollout_b.open("ab") as handle:
            handle.write(json_line(different))
        different_scan = self.run_cli(
            home_b, db_b, checkpoints, "--scan", "--source", "codex", "--device-id", "device-b"
        )
        self.assertEqual(different_scan["inserted"], 1)
        connection = sqlite3.connect(str(db_b))
        self.assertEqual(connection.execute("SELECT count(*) FROM source_events").fetchone()[0], 2)
        connection.close()

        bundle_b = self.base / "device-b.capture.bundle"
        exported_b = self.run_cli(
            home_b,
            db_b,
            checkpoints,
            "--export-bundle",
            str(bundle_b),
            "--device-id",
            "device-b",
        )
        self.assertEqual(exported_b["event_count"], 2)
        imported_b = self.run_cli(
            home_a, db_a, checkpoints, "--import-bundle", str(bundle_b), "--device-id", "device-a"
        )
        self.assertEqual(imported_b["inserted"], 1)
        self.assertEqual(imported_b["duplicates"], 1)
        connection = sqlite3.connect(str(db_a))
        self.assertEqual(connection.execute("SELECT count(*) FROM source_events").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT count(*) FROM event_observations").fetchone()[0], 3)
        connection.close()

    def test_export_fsyncs_file_and_parent_before_advancing_cursor(self):
        secret = "EXPORT_BODY_MUST_NOT_BE_PRINTED"
        _, _, _, connection = self.seeded_export_database("durable-order", secret)
        destination = self.base / "durable-order.bundle"
        calls = []
        real_replace = INGEST.os.replace
        real_fsync_file = INGEST.fsync_file
        real_fsync_directory = INGEST.fsync_directory

        def observed_replace(source, target):
            self.assertIsNone(self.export_cursor(connection))
            calls.append("replace")
            return real_replace(source, target)

        def observed_file(path):
            self.assertIsNone(self.export_cursor(connection))
            calls.append("file-fsync")
            return real_fsync_file(path)

        def observed_directory(path):
            self.assertIsNone(self.export_cursor(connection))
            calls.append("directory-fsync")
            return real_fsync_directory(path)

        with mock.patch.object(INGEST.os, "replace", side_effect=observed_replace), mock.patch.object(
            INGEST, "fsync_file", side_effect=observed_file
        ), mock.patch.object(INGEST, "fsync_directory", side_effect=observed_directory):
            report = INGEST.export_bundle(connection, destination, "export-device")

        self.assertEqual(calls, ["replace", "file-fsync", "directory-fsync"])
        self.assertEqual(self.export_cursor(connection), report["cursor_to"])
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))
        connection.close()

    def test_replace_failure_keeps_cursor_and_same_path_retry_exports_events(self):
        secret = "REPLACE_FAILURE_BODY_MUST_NOT_BE_PRINTED"
        _, _, _, connection = self.seeded_export_database("replace-failure", secret)
        destination = self.base / "replace-failure.bundle"
        with mock.patch.object(INGEST.os, "replace", side_effect=OSError("replace-failed")):
            with self.assertRaises(OSError):
                INGEST.export_bundle(connection, destination, "export-device")

        self.assertIsNone(self.export_cursor(connection))
        self.assertFalse(destination.exists())
        report = INGEST.export_bundle(connection, destination, "export-device")
        self.assertEqual(report["cursor_from"], 0)
        self.assertEqual(report["event_count"], 1)
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))
        connection.close()

    def test_parent_fsync_failure_keeps_cursor_and_retry_bundle_is_idempotent(self):
        secret = "FSYNC_FAILURE_BODY_MUST_NOT_BE_PRINTED"
        home, _, checkpoints, connection = self.seeded_export_database("fsync-failure", secret)
        uncertain = self.base / "fsync-uncertain.bundle"
        with mock.patch.object(
            INGEST, "fsync_directory", side_effect=OSError("directory-fsync-failed")
        ):
            with self.assertRaises(OSError):
                INGEST.export_bundle(connection, uncertain, "export-device")

        self.assertTrue(uncertain.exists())
        self.assertIsNone(self.export_cursor(connection))
        retry = self.base / "fsync-retry.bundle"
        report = INGEST.export_bundle(connection, retry, "export-device")
        self.assertEqual(report["cursor_from"], 0)
        self.assertEqual(report["event_count"], 1)
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))
        connection.close()

        receiver = self.base / "fsync-receiver.sqlite"
        self.run_cli(
            home, receiver, checkpoints, "--init-db", "--device-id", "receiver-device"
        )
        first = self.run_cli(
            home,
            receiver,
            checkpoints,
            "--import-bundle",
            str(uncertain),
            "--device-id",
            "receiver-device",
        )
        second = self.run_cli(
            home,
            receiver,
            checkpoints,
            "--import-bundle",
            str(retry),
            "--device-id",
            "receiver-device",
        )
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertNotIn(secret, json.dumps([first, second], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
