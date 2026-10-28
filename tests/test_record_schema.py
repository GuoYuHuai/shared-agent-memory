"""Enforce schema/record.schema.json against every committed record.

record.schema.json used to be an unexecuted draft that drifted from
reality (object source, observed_at, closed kind/authority enums no code
ever checked). This test makes the schema an executed contract: every
JSON record under ledger/ and inbox/candidates/ must validate, and a
freshly captured kb.py event must validate too. If capture output or the
schema changes unilaterally, this test fails.
"""

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "record.schema.json"
KB = ROOT / "scripts" / "kb.py"


def type_matches(value, expected):
    mapping = {
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    if isinstance(expected, list):
        return any(type_matches(value, item) for item in expected)
    python_type = mapping[expected]
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, python_type)


def validation_errors(record, schema):
    errors = []
    for field in schema["required"]:
        if field not in record:
            errors.append("missing required field %r" % field)
    for field, rules in schema["properties"].items():
        if field not in record:
            continue
        value = record[field]
        expected = rules.get("type")
        if expected and not type_matches(value, expected):
            errors.append("field %r has type %s" % (field, type(value).__name__))
            continue
        if isinstance(value, str):
            minimum = rules.get("minLength")
            if minimum and len(value.strip()) < minimum:
                errors.append("field %r shorter than minLength" % field)
            pattern = rules.get("pattern")
            if pattern and not re.match(pattern, value):
                errors.append("field %r does not match pattern" % field)
        if isinstance(value, list) and rules.get("items", {}).get("type"):
            item_type = rules["items"]["type"]
            for item in value:
                if not type_matches(item, item_type):
                    errors.append("field %r has non-%s item" % (field, item_type))
                    break
    return errors


class RecordSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def iter_records(self):
        for zone in ("ledger", "inbox/candidates", "examples/records"):
            base = ROOT / zone
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.json")):
                yield path, json.loads(path.read_text(encoding="utf-8"))

    def test_every_committed_record_validates(self):
        checked = 0
        for path, record in self.iter_records():
            errors = validation_errors(record, self.schema)
            self.assertEqual(
                errors, [], "%s: %s" % (path.relative_to(ROOT), "; ".join(errors))
            )
            checked += 1
        self.assertGreater(checked, 0, "no records found to validate")

    def test_fresh_capture_output_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("profile", "projects", "resources", "playbooks", "inbox", "ledger"):
                (root / name).mkdir()
            (root / "schema").mkdir()
            (root / "schema" / "knowledge.sql").write_text(
                (ROOT / "schema" / "knowledge.sql").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "config").mkdir()
            (root / "config" / "kb.json").write_text(
                (ROOT / "config" / "kb.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "schema@example.test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Schema Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "schema-root"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(KB),
                    "--root",
                    str(root),
                    "capture",
                    "--kind",
                    "fact",
                    "--scope",
                    "project:schema-contract",
                    "--statement",
                    "schema contract capture probe",
                    "--source",
                    "test:record-schema",
                    "--authority",
                    "user-explicit",
                    "--status",
                    "candidate",
                    "--source-device-id",
                    "schema-test-device",
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            ledger_record = json.loads(
                (root / payload["ledger"]).read_text(encoding="utf-8")
            )
            candidate_record = json.loads(
                (root / payload["candidate"]).read_text(encoding="utf-8")
            )
            self.assertEqual(validation_errors(ledger_record, self.schema), [])
            self.assertEqual(validation_errors(candidate_record, self.schema), [])

    def test_schema_required_set_matches_runtime_validator(self):
        # kb.py event_validation_errors is the runtime gate; the schema must
        # not promise a different required set than the code enforces.
        source = (ROOT / "scripts" / "kb.py").read_text(encoding="utf-8")
        match = re.search(
            r"for field in \(([^)]*)\):\s*\n\s*if not clean_scalar\(event\.get\(field\)\)",
            source,
        )
        self.assertIsNotNone(match, "runtime required-field loop not found")
        runtime_required = set(re.findall(r"\"([a-z_]+)\"", match.group(1)))
        self.assertEqual(set(self.schema["required"]), runtime_required)


if __name__ == "__main__":
    unittest.main()
