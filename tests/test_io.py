from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from codex_window_trigger.io import read_existing_prs, read_json


class ExistingPrTest(unittest.TestCase):
    def test_deeply_nested_json_is_a_value_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested.json"
            path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_json(path)

    def test_rest_shape_preserves_aware_created_at(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prs.json"
            key = "a" * 64
            path.write_text(json.dumps([{"title": f"[codex-5h-touch] {key}", "head": {"ref": "trigger/aaaaaaaaaaaa"}, "created_at": "2026-08-31T01:00:00+08:00"}]), encoding="utf-8")
            result = read_existing_prs(path)
            self.assertEqual("2026-08-30T17:00:00+00:00", result[key].isoformat())

    def test_malformed_matching_timestamp_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prs.json"
            key = "b" * 64
            path.write_text(json.dumps([{"title": f"[codex-5h-touch] {key}", "headRefName": "trigger/bbbbbbbbbbbb", "createdAt": "2026-08-31T01:00:00"}]), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_existing_prs(path)

    def test_malformed_matching_branch_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prs.json"
            key = "c" * 64
            path.write_text(json.dumps([{"title": f"[codex-5h-touch] {key}", "headRefName": "trigger/000000000000", "createdAt": "2026-08-31T01:00:00Z"}]), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_existing_prs(path)

    def test_unrelated_pr_is_ignored(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prs.json"
            path.write_text(json.dumps([{"title": "ordinary PR", "headRefName": "feature/example"}]), encoding="utf-8")
            self.assertEqual({}, read_existing_prs(path))

    def test_documented_canary_is_accepted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prs.json"
            path.write_text(json.dumps([{"title": "[codex-5h-touch] canary-000000000000", "headRefName": "trigger/000000000000", "createdAt": "2026-08-31T01:00:00Z"}]), encoding="utf-8")
            self.assertIn("canary-000000000000", read_existing_prs(path))
