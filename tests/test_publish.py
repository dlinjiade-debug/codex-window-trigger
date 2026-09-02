"""Real Git integration; only the external GitHub API is simulated."""
from copy import deepcopy
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_window_trigger.main import write_json
from codex_window_trigger.state import empty_state
from scripts.publish import GitHub, main, publish
from tests.helpers import payloads


NOW = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
REPOSITORY = "example/codex-window-trigger"


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise AssertionError(f"fixture Git failed: {args!r}: {result.stderr}")
    return result.stdout.strip()


class FakeGitHub:
    """Models raw REST pages and PR creation, not publisher decisions or Git."""
    def __init__(self, remote):
        self.metadata = {"full_name": REPOSITORY, "visibility": "public", "private": False,
                         "default_branch": "main",
                         "html_url": f"https://github.com/{REPOSITORY}",
                         "clone_url": str(remote), "ssh_url": str(remote)}
        self.pages = [[]]
        self.created_at = "2026-08-31T01:00:07Z"
        self.create_mode = "success"
        self.created = 0

    def repository(self, repository):
        if repository != REPOSITORY:
            raise AssertionError("wrong repository")
        return deepcopy(self.metadata)

    def list_prs(self, repository):
        if repository != REPOSITORY:
            raise AssertionError("wrong repository")
        return deepcopy(self.pages)

    def create_pr(self, repository, base, branch, title, body_file):
        if repository != REPOSITORY or base != "main":
            raise AssertionError("wrong PR destination")
        if self.create_mode == "fail":
            raise RuntimeError("simulated create failure")
        self.created += 1
        self.pages[-1].append(raw_pr(title, branch, self.created_at))
        if self.create_mode == "uncertain":
            raise RuntimeError("simulated lost success response")
        return "https://github.com/example/codex-window-trigger/pull/1"


def raw_pr(title, branch, created_at, *, author="github-actions[bot]", head_repo=REPOSITORY):
    return {"title": title, "created_at": created_at, "user": {"login": author},
            "head": {"ref": branch, "repo": {"full_name": head_repo}},
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}}}


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.remote = self.base / "remote.git"
        self.root = self.base / "checkout"
        self.root.mkdir()
        git(self.base, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
        write_json(self.root / "state/state.json", empty_state())
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "initial")
        git(self.root, "remote", "add", "origin", str(self.remote))
        git(self.root, "push", "origin", "HEAD:refs/heads/main")
        self.github = FakeGitHub(self.remote)
        self.fixtures = []
        for name, value in zip(("forecast", "feed", "timeline"), payloads()):
            path = self.root / ".runtime" / f"{name}.json"
            write_json(path, value)
            self.fixtures.append(path)

    def run_publish(self, **kwargs):
        options = dict(repository=REPOSITORY, github=self.github, now=NOW, fixtures=self.fixtures)
        options.update(kwargs)
        try:
            return publish(self.root, **options)
        except subprocess.CalledProcessError as exc:
            exc.add_note(f"temporary-repository Git stderr: {exc.stderr}")
            raise

    def remote_state(self):
        return json.loads(git(self.remote, "show", "refs/heads/main:state/state.json"))

    def initialize(self):
        state = empty_state()
        state["initialized"] = True
        write_json(self.root / "state/state.json", state)
        git(self.root, "add", "state/state.json")
        git(self.root, "commit", "-m", "initialized")
        git(self.root, "push", "origin", "HEAD:refs/heads/main")

    def test_manual_baseline_initializes_while_schedule_stays_disabled(self):
        result = self.run_publish(operation="baseline", dry_run=False)
        self.assertEqual("baseline", result)
        self.assertTrue(self.remote_state()["initialized"])
        self.assertEqual(0, self.github.created)
        self.assertEqual("refs/heads/main", git(self.remote, "for-each-ref", "--format=%(refname)"))

    def test_dry_run_wins_for_every_operation_and_enablement(self):
        before = git(self.remote, "show-ref")
        local = git(self.root, "rev-parse", "HEAD")
        for operation in ("poll", "baseline", "canary"):
            for enabled in (False, True):
                for approved in (False, True):
                    with self.subTest(operation=operation, enabled=enabled, approved=approved):
                        self.assertEqual("dry_run", self.run_publish(operation=operation, enabled=enabled,
                            canary_approved=approved, dry_run=True))
                        self.assertEqual(before, git(self.remote, "show-ref"))
                        self.assertEqual(local, git(self.root, "rev-parse", "HEAD"))
                        self.assertFalse(self.remote_state()["initialized"])
        self.assertEqual(0, self.github.created)

    def test_live_gate_matrix_cannot_be_bypassed(self):
        before = git(self.remote, "show-ref")
        for event in ("schedule", "workflow_dispatch", "pull_request"):
            for operation in ("poll", "baseline", "canary"):
                for enabled in (False, True):
                    for approved in (False, True):
                        allowed = ((operation == "poll" and enabled and event in ("schedule", "workflow_dispatch"))
                                   or (operation == "baseline" and event == "workflow_dispatch"))
                        # Canary is never allowed on uninitialized state.
                        if allowed:
                            continue
                        with self.subTest(event=event, operation=operation, enabled=enabled, approved=approved):
                            with self.assertRaises(ValueError):
                                self.run_publish(event_name=event, operation=operation, enabled=enabled,
                                                 canary_approved=approved, dry_run=False)
                            self.assertEqual(before, git(self.remote, "show-ref"))
        self.assertEqual(0, self.github.created)

    def test_initialized_baseline_is_noop_even_when_source_would_trigger(self):
        self.initialize()
        before = git(self.remote, "show-ref")
        self.assertEqual("skip", self.run_publish(operation="baseline", dry_run=False))
        self.assertEqual(before, git(self.remote, "show-ref"))
        self.assertEqual(0, self.github.created)

    def test_live_poll_creates_one_pr_then_records_actual_pr_time(self):
        self.initialize()
        self.assertEqual("trigger", self.run_publish(enabled=True, dry_run=False))
        state = self.remote_state()
        self.assertEqual("2026-08-31T01:00:07+00:00", state["last_triggered_at"])
        self.assertEqual(1, len(state["handled_keys"]))
        self.assertEqual("main", git(self.root, "branch", "--show-current"))
        self.assertEqual("skip", self.run_publish(enabled=True, dry_run=False))
        self.assertEqual(1, self.github.created)

    def test_trusted_same_repo_bot_only_and_every_page(self):
        self.initialize()
        self.github.pages = [
            [raw_pr("[codex-5h-touch] malformed", "trigger/000000000000", "invalid", author="outsider"),
             raw_pr("[codex-5h-touch] malformed", "trigger/000000000000", "invalid", head_repo="outsider/fork")],
            [raw_pr("[codex-5h-touch] " + "b" * 64, "trigger/" + "b" * 12, "2026-08-31T00:30:00Z")],
        ]
        self.assertEqual("reconcile", self.run_publish(enabled=True, dry_run=False))
        self.assertEqual(["b" * 64], self.remote_state()["handled_keys"])
        self.assertEqual("2026-08-31T00:30:00+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(0, self.github.created)

    def test_private_or_wrong_repository_or_origin_is_rejected_before_write(self):
        before = git(self.remote, "show-ref")
        for field, value in (("visibility", "private"), ("full_name", "other/repository"), ("private", True),
                             ("html_url", f"https://github.com/{REPOSITORY}.example.invalid")):
            with self.subTest(field=field):
                original = self.github.metadata[field]
                self.github.metadata[field] = value
                with self.assertRaises(ValueError):
                    self.run_publish(operation="baseline", dry_run=False)
                self.github.metadata[field] = original
        git(self.root, "remote", "set-url", "--push", "origin", str(self.base / "wrong.git"))
        with self.assertRaises(ValueError):
            self.run_publish(operation="baseline", dry_run=False)
        self.assertEqual(before, git(self.remote, "show-ref"))

    def test_actions_checkout_html_url_without_dot_git_is_accepted_for_fetch_and_push(self):
        checkout_url = f"https://github.com/{REPOSITORY}"
        git(self.root, "remote", "set-url", "origin", checkout_url)
        git(self.root, "remote", "set-url", "--push", "origin", checkout_url)

        try:
            result = self.run_publish(operation="poll", dry_run=True)
        except ValueError as exc:
            result = f"refused: {exc}"
        self.assertEqual("dry_run", result)

    def test_github_html_url_lookalike_origin_is_rejected_before_write(self):
        lookalike = f"https://github.com/{REPOSITORY}.example.invalid"
        git(self.root, "remote", "set-url", "origin", lookalike)
        git(self.root, "remote", "set-url", "--push", "origin", lookalike)
        before = git(self.remote, "show-ref")

        with self.assertRaises(ValueError):
            self.run_publish(operation="baseline", dry_run=False)

        self.assertEqual(before, git(self.remote, "show-ref"))

    def test_dirty_tracked_tree_and_untrusted_ref_are_rejected(self):
        (self.root / ".gitignore").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_publish(operation="baseline", dry_run=False)
        (self.root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
        git(self.root, "switch", "-c", "outsider")
        with self.assertRaises(ValueError):
            self.run_publish(operation="baseline", dry_run=False)

    def test_uncertain_create_requeries_and_never_duplicates(self):
        self.initialize()
        self.github.create_mode = "uncertain"
        self.assertEqual("trigger", self.run_publish(enabled=True, dry_run=False))
        self.assertEqual(1, self.github.created)
        self.assertEqual("skip", self.run_publish(enabled=True, dry_run=False))
        self.assertEqual(1, self.github.created)

    def test_failed_create_reuses_immutable_branch_from_earlier_poll(self):
        self.initialize()
        before = self.remote_state()
        self.github.create_mode = "fail"
        with self.assertRaises(RuntimeError):
            self.run_publish(enabled=True, dry_run=False)
        self.assertEqual(before, self.remote_state())
        branch = git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/trigger/")
        original = git(self.remote, "rev-parse", branch)
        payload = git(self.remote, "show", f"{branch}:triggers/{branch.rsplit('/', 1)[-1]}.json")
        self.github.create_mode = "success"
        later = datetime(2026, 8, 31, 1, 5, tzinfo=UTC)
        self.github.created_at = "2026-08-31T01:05:07Z"
        self.assertEqual("trigger", self.run_publish(enabled=True, dry_run=False, now=later))
        self.assertEqual(original, git(self.remote, "rev-parse", branch))
        self.assertEqual(payload, git(self.remote, "show", f"{branch}:triggers/{branch.rsplit('/', 1)[-1]}.json"))
        self.assertEqual("2026-08-31T01:05:07+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(1, self.github.created)

    def test_unknown_existing_branch_is_never_overwritten_or_used(self):
        self.initialize()
        self.github.create_mode = "fail"
        with self.assertRaises(RuntimeError):
            self.run_publish(enabled=True, dry_run=False)
        branch = git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads/trigger/")
        git(self.root, "fetch", "origin", branch)
        git(self.root, "switch", "--detach", "FETCH_HEAD")
        (self.root / "unexpected.txt").write_text("not our commit", encoding="utf-8")
        git(self.root, "add", "unexpected.txt")
        git(self.root, "commit", "-m", "unknown content")
        git(self.root, "push", "origin", f"HEAD:{branch}")
        git(self.root, "switch", "main")
        original = git(self.remote, "rev-parse", branch)
        self.github.create_mode = "success"
        with self.assertRaises(ValueError):
            self.run_publish(enabled=True, dry_run=False)
        self.assertEqual(original, git(self.remote, "rev-parse", branch))
        self.assertEqual(0, self.github.created)

    def test_failed_main_push_reconciles_existing_pr_on_fresh_checkout(self):
        self.initialize()
        old = git(self.root, "rev-parse", "HEAD")
        # Receive hook rejects only main, after branch push and PR succeed.
        hook = self.remote / "hooks" / "update"
        hook.write_text('#!/bin/sh\n[ "$1" != "refs/heads/main" ]\n', encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_publish(enabled=True, dry_run=False)
        self.assertEqual(old, git(self.remote, "rev-parse", "refs/heads/main"))
        self.assertEqual(1, self.github.created)
        hook.unlink()
        # A fresh Actions checkout starts at remote main, not the failed local commit.
        fresh = self.base / "retry"
        git(self.base, "clone", "-b", "main", str(self.remote), str(fresh))
        self.root = fresh
        self.assertEqual("reconcile", self.run_publish(enabled=True, dry_run=False))
        self.assertEqual("2026-08-31T01:00:07+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(1, self.github.created)

    def test_canary_requires_approval_and_is_exact_once_with_original_time(self):
        self.initialize()
        for event in ("workflow_dispatch", "schedule"):
            for enabled in (False, True):
                for approved in (False, True):
                    if event == "workflow_dispatch" and approved:
                        continue
                    with self.assertRaises(ValueError):
                        self.run_publish(operation="canary", event_name=event, enabled=enabled,
                                         canary_approved=approved, dry_run=False)
        self.github.create_mode = "uncertain"
        self.assertEqual("canary", self.run_publish(operation="canary", canary_approved=True, dry_run=False))
        self.assertEqual("[codex-5h-touch] canary-000000000000", self.github.pages[-1][-1]["title"])
        self.assertEqual("trigger/000000000000", self.github.pages[-1][-1]["head"]["ref"])
        self.assertEqual("skip", self.run_publish(operation="canary", canary_approved=True,
                         dry_run=False, now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual("2026-08-31T01:00:07+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(1, self.github.created)

    def test_recent_trusted_pr_blocks_approved_canary(self):
        self.initialize()
        self.github.pages[-1].append(raw_pr("[codex-5h-touch] " + "d" * 64,
                                            "trigger/" + "d" * 12, "2026-08-31T00:59:00Z"))

        self.assertEqual("skip", self.run_publish(operation="canary", canary_approved=True, dry_run=False))

        self.assertEqual(0, self.github.created)
        self.assertEqual("2026-08-31T00:59:00+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual("refs/heads/main", git(self.remote, "for-each-ref", "--format=%(refname)"))

    def test_trusted_pr_arriving_before_canary_mutation_blocks_canary(self):
        self.initialize()
        initial_list = self.github.list_prs
        calls = 0

        def list_with_late_pr(repository):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.github.pages[-1].append(raw_pr("[codex-5h-touch] " + "e" * 64,
                                                    "trigger/" + "e" * 12, "2026-08-31T00:59:00Z"))
            return initial_list(repository)

        self.github.list_prs = list_with_late_pr
        self.assertEqual("skip", self.run_publish(operation="canary", canary_approved=True, dry_run=False))

        self.assertEqual(0, self.github.created)
        self.assertEqual("2026-08-31T00:59:00+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(["refs/heads/main", "refs/heads/trigger/000000000000"],
                         git(self.remote, "for-each-ref", "--format=%(refname)").splitlines())

    def test_manual_environment_dry_run_cannot_be_overridden_by_live_flag(self):
        self.initialize()
        before = git(self.remote, "show-ref")
        env = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
               "SENTINEL_ENABLED": "true", "SENTINEL_OPERATION": "canary",
               "SENTINEL_CANARY_APPROVED": "true", "SENTINEL_DRY_RUN": "true"}
        with patch.dict("os.environ", env, clear=True), patch("scripts.publish.GitHub", return_value=self.github), \
                patch("sys.argv", ["publish", "--root", str(self.root), "--live"]):
            main()
        self.assertEqual(before, git(self.remote, "show-ref"))
        self.assertEqual(0, self.github.created)

    def test_second_pr_arriving_after_plan_obeys_cooldown(self):
        self.initialize()
        initial_list = self.github.list_prs
        calls = 0

        def list_with_new_pr(repository):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.github.pages[-1].append(raw_pr("[codex-5h-touch] " + "c" * 64,
                                                   "trigger/" + "c" * 12, "2026-08-31T00:59:00Z"))
            return initial_list(repository)

        self.github.list_prs = list_with_new_pr
        self.assertEqual("reconcile", self.run_publish(enabled=True, dry_run=False))
        self.assertEqual(0, self.github.created)
        self.assertEqual("2026-08-31T00:59:00+00:00", self.remote_state()["last_triggered_at"])

    def test_scheduled_live_poll_is_allowed_only_when_enabled(self):
        self.initialize()
        self.assertEqual("trigger", self.run_publish(event_name="schedule", enabled=True, dry_run=False))
        self.assertEqual(1, self.github.created)

    def test_poll_logs_only_short_public_decision_metadata(self):
        stream = io.StringIO()
        with patch("sys.stdout", stream):
            self.run_publish(dry_run=True)
        line = stream.getvalue().strip()
        self.assertTrue(line.startswith("decision: "), line)
        decision = json.loads(line.removeprefix("decision: "))
        self.assertEqual("dry_run", decision["action"])
        self.assertEqual(30.0, decision["score"])
        self.assertEqual("2026-08-31T01:00:00+00:00", decision["time"])
        self.assertEqual({"action", "reason", "time", "score", "key"}, set(decision))

    def test_canary_failed_create_reuses_earlier_payload(self):
        self.initialize()
        self.github.create_mode = "fail"
        with self.assertRaises(RuntimeError):
            self.run_publish(operation="canary", canary_approved=True, dry_run=False)
        original = git(self.remote, "rev-parse", "refs/heads/trigger/000000000000")
        self.github.create_mode = "success"
        self.github.created_at = "2026-08-31T01:05:07Z"
        self.assertEqual("canary", self.run_publish(operation="canary", enabled=True, canary_approved=True,
                         dry_run=False, now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual(original, git(self.remote, "rev-parse", "refs/heads/trigger/000000000000"))
        self.assertEqual("2026-08-31T01:05:07+00:00", self.remote_state()["last_triggered_at"])


class GitHubTransportTest(unittest.TestCase):
    def test_all_pages_and_untrusted_pr_text_use_literal_argument_boundaries(self):
        calls = []
        title = "[codex-5h-touch] literal $(do-not-execute)"

        def external(args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ["gh", "api"]:
                return subprocess.CompletedProcess(args, 0, stdout='[[{"title":"page-one"}],[{"title":"page-two"}]]')
            return subprocess.CompletedProcess(args, 0, stdout="https://example.invalid/pr/1")

        with patch("scripts.publish.subprocess.run", side_effect=external):
            pages = GitHub().list_prs(REPOSITORY)
            GitHub().create_pr(REPOSITORY, "main", "trigger/123456789abc", title, Path("literal body.md"))
        self.assertEqual([[{"title": "page-one"}], [{"title": "page-two"}]], pages)
        self.assertEqual(["gh", "api", "--paginate", "--slurp",
                          "repos/example/codex-window-trigger/pulls?state=all&per_page=100"], calls[0][0])
        self.assertEqual(["gh", "pr", "create", "--repo", REPOSITORY, "--base", "main", "--head",
                          "trigger/123456789abc", "--title", title, "--body-file", "literal body.md"], calls[1][0])
        self.assertTrue(all(not kwargs.get("shell", False) for _, kwargs in calls))


if __name__ == "__main__":
    unittest.main()
