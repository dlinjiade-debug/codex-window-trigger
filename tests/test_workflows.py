"""Validate executable workflow schemas, not source-string snapshots."""
from pathlib import Path
import os
import shutil
import subprocess
import unittest


class WorkflowSchemaTest(unittest.TestCase):
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
