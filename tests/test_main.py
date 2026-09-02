from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from codex_window_trigger.evaluator import evaluate_sources
from codex_window_trigger.main import emit_outputs, run


FIXTURES = Path(__file__).parent / "fixtures"


def cli_args(root: Path, *, dry_run: bool = False) -> list[str]:
    args = [
        "--now", "2026-08-31T01:05:00Z",
        "--forecast", str(FIXTURES / "forecast.json"),
        "--feed", str(FIXTURES / "feed.json"),
        "--timeline", str(FIXTURES / "timeline.json"),
        "--state", str(root / "state.json"),
        "--prs", str(root / "prs.json"),
        "--runtime-dir", str(root / "runtime"),
        "--github-output", str(root / "github-output.txt"),
    ]
    return args + (["--dry-run"] if dry_run else [])


class MainTest(unittest.TestCase):
    def prepare(self, root: Path, state: dict) -> None:
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (root / "prs.json").write_text("[]", encoding="utf-8")

    def test_dry_run_writes_decision_without_mutating_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}}
            self.prepare(root, state)
            before = (root / "state.json").read_text(encoding="utf-8")
            self.assertEqual(0, run(cli_args(root, dry_run=True)))
            self.assertEqual(before, (root / "state.json").read_text(encoding="utf-8"))
            self.assertTrue((root / "runtime" / "decision.json").exists())
            self.assertFalse((root / "runtime" / "next-state.json").exists())
            self.assertFalse((root / "runtime" / "trigger.json").exists())

    def test_first_live_run_writes_baseline_next_state_and_no_trigger_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}})
            self.assertEqual(0, run(cli_args(root)))
            next_state = json.loads((root / "runtime" / "next-state.json").read_text(encoding="utf-8"))
            self.assertTrue(next_state["initialized"])
            self.assertFalse((root / "runtime" / "trigger.json").exists())

    def test_trigger_writes_hash_only_branch_name_and_json_payload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}})
            self.assertEqual(0, run(cli_args(root)))
            output = (root / "github-output.txt").read_text(encoding="utf-8")
            branch = next(line.split("=", 1)[1] for line in output.splitlines() if line.startswith("branch="))
            self.assertRegex(branch, r"^trigger/[0-9a-f]{12}$")
            payload = json.loads((root / "runtime" / "trigger.json").read_text(encoding="utf-8"))
            self.assertEqual(30, payload["score"])
            self.assertNotIn("Your Codex", output)

    def test_existing_pr_title_reconciles_instead_of_triggering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}})
            raw = [json.loads((FIXTURES / name).read_text(encoding="utf-8")) for name in ("forecast.json", "feed.json", "timeline.json")]
            key = evaluate_sources(*raw, now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)).episode.key
            (root / "prs.json").write_text(json.dumps([{"title": f"[codex-5h-touch] {key}", "headRefName": f"trigger/{key[:12]}", "createdAt": "2026-08-30T01:00:00Z"}]), encoding="utf-8")
            self.assertEqual(0, run(cli_args(root)))
            output = (root / "github-output.txt").read_text(encoding="utf-8")
            self.assertIn("action=reconcile", output)
            self.assertFalse((root / "runtime" / "trigger.json").exists())

    def test_invalid_source_returns_exit_2_and_no_trigger_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}})
            bad = root / "bad.json"
            bad.write_text("{", encoding="utf-8")
            args = cli_args(root)
            args[args.index("--forecast") + 1] = str(bad)
            self.assertEqual(2, run(args))
            self.assertFalse((root / "runtime" / "trigger.json").exists())

    def test_live_source_recursion_error_returns_exit_2_and_cleans_stale_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"; runtime.mkdir()
            (runtime / "trigger.json").write_text("stale", encoding="utf-8")
            with patch("codex_window_trigger.main.fetch_sources", side_effect=RecursionError):
                self.assertEqual(2, run(["--runtime-dir", str(runtime)]))
            self.assertFalse((runtime / "trigger.json").exists())
            self.assertEqual("error", json.loads((runtime / "decision.json").read_text(encoding="utf-8"))["action"])

    def test_partial_fixture_arguments_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}})
            self.assertEqual(2, run(["--forecast", str(FIXTURES / "forecast.json"), "--runtime-dir", str(root / "runtime")]))

    def test_live_plan_requires_pr_list(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state.json").write_text(json.dumps({"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}}), encoding="utf-8")
            args = cli_args(root)
            del args[args.index("--prs"):args.index("--prs") + 2]
            self.assertEqual(2, run(args))

    def test_missing_required_state_field_fails_closed_at_cli_boundary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": True, "episodes": {}})
            try:
                result = run(cli_args(root))
            except KeyError:
                result = "KeyError escaped"
            self.assertEqual(2, result)
            self.assertFalse((root / "runtime" / "trigger.json").exists())

    def test_stale_artifacts_are_removed_without_touching_unrelated_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}})
            runtime = root / "runtime"; runtime.mkdir()
            for name in ("next-state.json", "trigger.json", "pr-title.txt", "pr-body.md"):
                (runtime / name).write_text("stale", encoding="utf-8")
            (runtime / "keep.txt").write_text("user", encoding="utf-8")
            bad = root / "bad.json"; bad.write_text("{", encoding="utf-8")
            args = cli_args(root); args[args.index("--forecast") + 1] = str(bad)
            self.assertEqual(2, run(args))
            self.assertTrue((runtime / "keep.txt").exists())
            self.assertFalse(any((runtime / name).exists() for name in ("next-state.json", "trigger.json", "pr-title.txt", "pr-body.md")))

    def test_dry_run_removes_every_actionable_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}})
            runtime = root / "runtime"; runtime.mkdir()
            for name in ("next-state.json", "trigger.json", "pr-title.txt", "pr-body.md"):
                (runtime / name).write_text("stale", encoding="utf-8")
            self.assertEqual(0, run(cli_args(root, dry_run=True)))
            self.assertFalse(any((runtime / name).exists() for name in ("next-state.json", "trigger.json", "pr-title.txt", "pr-body.md")))

    def test_unsafe_outputs_are_all_or_nothing(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "github-output.txt"
            target.write_text("before=ok\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                emit_outputs(str(target), {"action": "trigger", "reason": "bad\nvalue"})
            self.assertEqual("before=ok\n", target.read_text(encoding="utf-8"))

    def test_unsupported_output_key_is_all_or_nothing(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "github-output.txt"
            target.write_text("before=ok\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                emit_outputs(str(target), {"action": "trigger", "unexpected": "value"})
            self.assertEqual("before=ok\n", target.read_text(encoding="utf-8"))

    def test_module_entrypoint_runs_the_cli(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}})
            process = subprocess.run([sys.executable, "-m", "codex_window_trigger.main", *cli_args(root, dry_run=True)], capture_output=True, text=True, check=False)
            self.assertEqual(0, process.returncode, process.stderr)
            self.assertTrue((root / "runtime" / "decision.json").exists())

    def test_quiet_valid_observation_baselines(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}})
            forecast = {"mode": "model", "updated_at": "2026-08-31T01:00:00Z", "probabilities": {"signal_percent": None}}
            for name, value in (("forecast.json", forecast), ("feed.json", {"source": "x-api", "stale": False, "fetched_at": "2026-08-31T01:00:00Z", "events": []}), ("timeline.json", {"events": []})):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            args = cli_args(root)
            for option, name in (("--forecast", "forecast.json"), ("--feed", "feed.json"), ("--timeline", "timeline.json")):
                args[args.index(option) + 1] = str(root / name)
            self.assertEqual(0, run(args))
            self.assertTrue(json.loads((root / "runtime" / "next-state.json").read_text(encoding="utf-8"))["initialized"])

    def test_first_valid_below_threshold_observation_baselines_without_trigger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root, {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}})
            payloads = [json.loads((FIXTURES / name).read_text(encoding="utf-8"))
                        for name in ("forecast.json", "feed.json", "timeline.json")]
            payloads[0]["probabilities"]["signal_percent"] = 29
            for name, value in zip(("forecast.json", "feed.json", "timeline.json"), payloads):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            args = cli_args(root)
            for option, name in (("--forecast", "forecast.json"), ("--feed", "feed.json"),
                                 ("--timeline", "timeline.json")):
                args[args.index(option) + 1] = str(root / name)

            self.assertEqual(0, run(args))

            next_state = json.loads((root / "runtime" / "next-state.json").read_text(encoding="utf-8"))
            self.assertTrue(next_state["initialized"])
            self.assertEqual(["baseline"], [record["status"] for record in next_state["episodes"].values()])
            self.assertFalse((root / "runtime" / "trigger.json").exists())


if __name__ == "__main__":
    unittest.main()
