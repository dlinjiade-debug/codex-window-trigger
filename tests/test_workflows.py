"""Validate executable workflow schemas, not source-string snapshots."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import unittest


class WorkflowSchemaTest(unittest.TestCase):
    def test_sentinel_grants_only_the_permissions_needed_for_pr_comment_delivery(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/sentinel.yml"
        lines = path.read_text(encoding="utf-8").splitlines()
        start = lines.index("permissions:") + 1
        permissions = {}
        for line in lines[start:]:
            if line and not line.startswith(" "):
                break
            match = re.fullmatch(r"  ([a-z-]+): (read|write|none)", line)
            if match:
                permissions[match.group(1)] = match.group(2)

        self.assertEqual({"contents": "write", "issues": "write", "pull-requests": "write"},
                         permissions)

    def test_actionlint_accepts_workflows(self):
        root = Path(__file__).resolve().parents[1]
        bundled = root / ".superpowers/sdd/2026-08-31-codex-five-hour-window-trigger/tools/actionlint/extracted/actionlint.exe"
        executable = os.getenv("ACTIONLINT") or shutil.which("actionlint") or (str(bundled) if bundled.exists() else None)
        if not executable:
            if os.getenv("CI") == "true":
                self.fail("actionlint is mandatory in hosted CI")
            self.skipTest("actionlint unavailable; run separately before deployment")
        result = subprocess.run([executable, "-shellcheck=", str(root / ".github/workflows/ci.yml"),
                                 str(root / ".github/workflows/sentinel.yml")], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
