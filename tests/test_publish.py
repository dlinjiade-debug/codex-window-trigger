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

from codex_window_trigger.io import read_json
from codex_window_trigger.main import write_json
from codex_window_trigger.state import empty_state
from scripts.publish import GitHub, main, publish
from tests.helpers import payloads


NOW = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)
REPOSITORY = "example/codex-window-trigger"
FIXTURE_KEY = "563920b58ca7ca37a1e0a80649ba7479c8cfa9f5ecfb9ff99a4259b6bbeee757"


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
        self.comment_mode = "success"
        self.comment_list_mode = "success"
        self.comment_created_at = "2026-08-31T01:00:08Z"
        self.created = 0
        self.commented = 0
        self.comment_pages = {}

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
        self.pages[-1].append(raw_pr(title, branch, self.created_at, number=self.created))
        if self.create_mode == "uncertain":
            raise RuntimeError("simulated lost success response")
        return "https://github.com/example/codex-window-trigger/pull/1"

    def list_issue_comments(self, repository, number):
        if repository != REPOSITORY or not isinstance(number, int) or number <= 0:
            raise AssertionError("wrong comment destination")
        if self.comment_list_mode == "fail":
            raise RuntimeError("simulated comment-list failure")
        return deepcopy(self.comment_pages.get(number, [[]]))

    def create_issue_comment(self, repository, number, body_file):
        if repository != REPOSITORY or not isinstance(number, int) or number <= 0:
            raise AssertionError("wrong comment destination")
        if self.comment_mode == "fail":
            raise RuntimeError("simulated comment failure")
        value = json.loads(Path(body_file).read_text(encoding="utf-8"))
        if set(value) != {"body"} or not isinstance(value["body"], str):
            raise AssertionError("comment transport must use one JSON body field")
        self.commented += 1
        comment = raw_comment(value["body"], created_at=self.comment_created_at)
        if self.comment_mode not in {"delayed", "lost_delayed"}:
            self.comment_pages.setdefault(number, [[]])[-1].append(comment)
        if self.comment_mode in {"uncertain", "lost_delayed"}:
            raise RuntimeError("simulated lost comment response")
        return deepcopy(comment)


def raw_pr(title, branch, created_at, *, number=1, author="github-actions[bot]", head_repo=REPOSITORY):
    return {"number": number, "title": title, "created_at": created_at, "user": {"login": author},
            "head": {"ref": branch, "repo": {"full_name": head_repo}},
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}}}


def raw_comment(body, *, author="github-actions[bot]", created_at=None):
    if created_at is None:
        created_at = next(line.removeprefix("pr_created_utc=") for line in body.splitlines()
                          if line.startswith("pr_created_utc="))
    return {"id": 1, "body": body, "user": {"login": author},
            "created_at": created_at,
            "html_url": f"https://github.com/{REPOSITORY}/pull/1#issuecomment-1"}


def expected_comment(key, created_at):
    return (
        "@codex Do not inspect files, run commands, modify the repository, create commits, "
        "or post follow-ups. Reply with exactly the receipt block below and then stop.\n\n"
        "codex-window-trigger receipt\n"
        f"key={key}\n"
        f"pr_created_utc={created_at}\n"
        "warning=已启动一次云端五小时窗口触碰；并非已确认额度窗口重锚。\n\n"
        f"<!-- codex-window-trigger:{key} -->\n"
    )


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

    def test_baseline_refuses_uncommented_trigger_pr_without_mutating_anything(self):
        key = "a" * 64
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {key}", f"trigger/{key[:12]}",
            "2026-08-31T00:59:00Z", number=8)]]
        before = self.remote_state()

        with self.assertRaises(ValueError):
            self.run_publish(operation="baseline", dry_run=False)

        self.assertEqual(0, self.github.commented)
        self.assertEqual(before, self.remote_state())

    def test_live_poll_creates_one_pr_then_records_actual_comment_time(self):
        self.initialize()
        self.assertEqual("trigger", self.run_publish(enabled=True, dry_run=False))
        state = self.remote_state()
        self.assertEqual("2026-08-31T01:00:08+00:00", state["last_triggered_at"])
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
        self.assertEqual("2026-08-31T01:00:08+00:00", self.remote_state()["last_triggered_at"])
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
        self.github.comment_created_at = "2026-08-31T01:05:08Z"
        self.assertEqual("trigger", self.run_publish(enabled=True, dry_run=False, now=later))
        self.assertEqual(original, git(self.remote, "rev-parse", branch))
        self.assertEqual(payload, git(self.remote, "show", f"{branch}:triggers/{branch.rsplit('/', 1)[-1]}.json"))
        self.assertEqual("2026-08-31T01:05:08+00:00", self.remote_state()["last_triggered_at"])
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

    def test_failed_receipt_push_reconciles_comment_time_on_fresh_checkout(self):
        self.initialize()
        # Allow the pre-POST attempt marker, then reject the final receipt push.
        hook = self.remote / "hooks" / "update"
        hook.write_text(
            '#!/bin/sh\n'
            '[ "$1" != "refs/heads/main" ] && exit 0\n'
            'git show "$3:state/state.json" | grep -q \'"comment_attempts"\'\n',
            encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_publish(enabled=True, dry_run=False)
        self.assertIn(FIXTURE_KEY, self.remote_state()["comment_attempts"])
        self.assertEqual(1, self.github.created)
        self.assertEqual(1, self.github.commented)
        hook.unlink()
        # A fresh Actions checkout starts at remote main, not the failed local commit.
        fresh = self.base / "retry"
        git(self.base, "clone", "-b", "main", str(self.remote), str(fresh))
        self.root = fresh
        self.assertEqual("reconcile", self.run_publish(enabled=True, dry_run=False))
        self.assertNotIn("comment_attempts", self.remote_state())
        self.assertEqual("2026-08-31T01:00:08+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(1, self.github.created)
        self.assertEqual(1, self.github.commented)

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
        comment = expected_comment("canary-000000000000", "2026-08-31T01:00:07+00:00")
        self.assertEqual([[raw_comment(comment, created_at="2026-08-31T01:00:08Z")]],
                         self.github.comment_pages.get(1, []))
        self.assertEqual("skip", self.run_publish(operation="canary", canary_approved=True,
                         dry_run=False, now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual("2026-08-31T01:00:08+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual(1, self.github.created)
        self.assertEqual(1, self.github.commented)

    def test_lost_comment_response_is_requeried_before_state_is_persisted(self):
        self.initialize()
        self.github.comment_mode = "uncertain"

        try:
            result = self.run_publish(operation="canary", canary_approved=True, dry_run=False)
        except RuntimeError as exc:
            result = f"raised: {exc}"
        self.assertEqual("canary", result)

        self.assertEqual(1, self.github.created)
        self.assertEqual(1, self.github.commented)
        self.assertIn("canary-000000000000", self.remote_state()["handled_keys"])

    def test_failed_comment_never_persists_a_trigger_receipt(self):
        self.initialize()
        self.github.comment_mode = "fail"

        with self.assertRaises(RuntimeError):
            self.run_publish(operation="canary", canary_approved=True, dry_run=False)

        self.assertEqual(1, self.github.created)
        self.assertEqual(0, self.github.commented)
        state = self.remote_state()
        self.assertEqual(
            {"canary-000000000000": "2026-08-31T01:00:00+00:00"},
            state["comment_attempts"])
        self.assertNotIn("canary-000000000000", state.get("handled_keys", []))
        self.assertIsNone(state["last_triggered_at"])

    def test_delayed_comment_visibility_is_reconciled_on_the_next_run(self):
        self.initialize()
        self.github.comment_mode = "delayed"

        self.assertEqual("canary", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False))

        state = self.remote_state()
        self.assertIn("canary-000000000000", state["handled_keys"])
        self.assertEqual("2026-08-31T01:00:08+00:00", state["last_triggered_at"])
        self.assertEqual(1, self.github.created)
        self.assertEqual(1, self.github.commented)

        self.assertEqual("skip", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False,
            now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual(1, self.github.created)
        self.assertEqual(1, self.github.commented)

    def test_lost_response_stays_read_only_until_the_comment_is_visible(self):
        self.initialize()
        self.github.comment_mode = "lost_delayed"

        with self.assertRaises(RuntimeError):
            self.run_publish(operation="canary", canary_approved=True, dry_run=False)

        attempted = self.remote_state()
        self.assertEqual(
            {"canary-000000000000": "2026-08-31T01:00:00+00:00"},
            attempted["comment_attempts"])
        self.assertNotIn("canary-000000000000", attempted.get("handled_keys", []))
        self.assertIsNone(attempted["last_triggered_at"])
        self.assertEqual(1, self.github.commented)

        self.assertEqual("skip", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False,
            now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual(1, self.github.commented)
        self.assertEqual(attempted, self.remote_state())

        self.github.comment_pages[1] = [[raw_comment(
            expected_comment("canary-000000000000", "2026-08-31T01:00:07+00:00"),
            created_at=self.github.comment_created_at)]]
        self.assertEqual("skip", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False,
            now=datetime(2026, 8, 31, 1, 10, tzinfo=UTC)))
        recovered = self.remote_state()
        self.assertNotIn("comment_attempts", recovered)
        self.assertIn("canary-000000000000", recovered["handled_keys"])
        self.assertEqual("2026-08-31T01:00:08+00:00", recovered["last_triggered_at"])
        self.assertEqual(1, self.github.commented)

    def test_orphaned_comment_attempt_blocks_canary_before_pr_creation(self):
        self.initialize()
        state = read_json(self.root / "state/state.json")
        state["comment_attempts"] = {"d" * 64: "2026-08-30T00:00:00+00:00"}
        state["handled_keys"] = []
        write_json(self.root / "state/state.json", state)
        git(self.root, "add", "state/state.json")
        git(self.root, "commit", "-m", "unconfirmed comment attempt")
        git(self.root, "push", "origin", "HEAD:refs/heads/main")

        self.assertEqual("skip", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False))

        self.assertEqual(0, self.github.created)
        self.assertEqual(0, self.github.commented)
        self.assertEqual(state, self.remote_state())

    def test_only_exact_bot_comment_on_any_page_satisfies_idempotency(self):
        self.initialize()
        key = "canary-000000000000"
        created = "2026-08-31T00:59:00+00:00"
        comment = expected_comment(key, created)
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {key}", "trigger/000000000000",
            "2026-08-31T00:59:00Z", number=19)]]
        self.github.comment_pages[19] = [
            [raw_comment(comment, author="outsider"), raw_comment(comment + " ")],
            [raw_comment(comment)],
        ]

        self.assertEqual("skip", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False))

        self.assertEqual(0, self.github.created)
        self.assertEqual(0, self.github.commented)
        self.assertIn(key, self.remote_state()["handled_keys"])

    def test_existing_canary_pr_without_comment_is_completed_not_acknowledged_early(self):
        self.initialize()
        self.github.pages = [[raw_pr(
            "[codex-5h-touch] canary-000000000000", "trigger/000000000000",
            "2026-08-31T00:59:00Z", number=17)]]

        result = self.run_publish(operation="canary", canary_approved=True, dry_run=False)

        self.assertEqual("canary", result)
        self.assertEqual(0, self.github.created)
        self.assertEqual(1, self.github.commented)
        self.assertIn("canary-000000000000", self.remote_state()["handled_keys"])

    def test_existing_poll_pr_without_comment_is_repaired_before_reconciliation(self):
        self.initialize()
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
            "2026-08-31T00:59:00Z", number=23)]]

        result = self.run_publish(enabled=True, dry_run=False)

        self.assertEqual("reconcile", result)
        self.assertEqual(0, self.github.created)
        self.assertEqual(1, self.github.commented)
        self.assertIn(FIXTURE_KEY, self.remote_state()["handled_keys"])

    def test_repairing_an_old_pr_starts_cooldown_at_the_comment_time(self):
        self.initialize()
        old = "d" * 64
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {old}", f"trigger/{old[:12]}",
            "2026-08-29T00:58:00Z", number=24)]]

        self.assertEqual("reconcile", self.run_publish(enabled=True, dry_run=False))

        state = self.remote_state()
        self.assertEqual("2026-08-31T01:00:08+00:00", state["last_triggered_at"])
        self.assertIn(old, state["handled_keys"])
        self.assertNotIn(FIXTURE_KEY, state["handled_keys"])
        self.assertEqual(1, self.github.commented)

        self.assertEqual("skip", self.run_publish(
            enabled=True, dry_run=False,
            now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual(0, self.github.created)
        self.assertEqual(1, self.github.commented)

    def test_recent_commented_pr_blocks_repairing_a_second_poll_pr(self):
        self.initialize()
        completed = "d" * 64
        self.github.pages = [[
            raw_pr(f"[codex-5h-touch] {completed}", f"trigger/{completed[:12]}",
                   "2026-08-31T00:59:00Z", number=24),
            raw_pr(f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
                   "2026-08-31T00:58:00Z", number=23),
        ]]
        self.github.comment_pages[24] = [[raw_comment(
            expected_comment(completed, "2026-08-31T00:59:00+00:00"))]]

        self.assertEqual("reconcile", self.run_publish(enabled=True, dry_run=False))

        state = self.remote_state()
        self.assertEqual(0, self.github.commented)
        self.assertIn(completed, state["handled_keys"])
        self.assertNotIn(FIXTURE_KEY, state["handled_keys"])
        self.assertEqual("2026-08-31T00:59:00+00:00", state["last_triggered_at"])

    def test_finalized_history_does_not_depend_on_comment_api_availability(self):
        self.initialize()
        state = read_json(self.root / "state/state.json")
        state["handled_keys"] = [FIXTURE_KEY]
        state["last_triggered_at"] = "2026-08-31T00:59:00+00:00"
        write_json(self.root / "state/state.json", state)
        git(self.root, "add", "state/state.json")
        git(self.root, "commit", "-m", "finalized trigger")
        git(self.root, "push", "origin", "HEAD:refs/heads/main")
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
            "2026-08-31T00:59:00Z", number=23)]]
        self.github.comment_list_mode = "fail"

        try:
            result = self.run_publish(enabled=True, dry_run=False)
        except RuntimeError as exc:
            result = f"raised: {exc}"

        self.assertEqual("skip", result)
        self.assertEqual(0, self.github.commented)

    def test_finalized_history_still_rejects_an_invalid_pr_number(self):
        self.initialize()
        state = read_json(self.root / "state/state.json")
        state["handled_keys"] = [FIXTURE_KEY]
        state["last_triggered_at"] = "2026-08-31T00:59:00+00:00"
        write_json(self.root / "state/state.json", state)
        git(self.root, "add", "state/state.json")
        git(self.root, "commit", "-m", "finalized trigger")
        git(self.root, "push", "origin", "HEAD:refs/heads/main")
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
            "2026-08-31T00:59:00Z", number=0)]]

        with self.assertRaises(ValueError):
            self.run_publish(enabled=True, dry_run=False)

        self.assertEqual(0, self.github.commented)

    def test_finalized_history_still_rejects_duplicate_pr_numbers_for_one_key(self):
        self.initialize()
        state = read_json(self.root / "state/state.json")
        state["handled_keys"] = [FIXTURE_KEY]
        state["last_triggered_at"] = "2026-08-31T00:59:00+00:00"
        write_json(self.root / "state/state.json", state)
        git(self.root, "add", "state/state.json")
        git(self.root, "commit", "-m", "finalized trigger")
        git(self.root, "push", "origin", "HEAD:refs/heads/main")
        self.github.pages = [[
            raw_pr(f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
                   "2026-08-31T00:59:00Z", number=40),
            raw_pr(f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
                   "2026-08-31T00:59:00Z", number=41),
        ]]

        with self.assertRaises(ValueError):
            self.run_publish(enabled=True, dry_run=False)

        self.assertEqual(0, self.github.commented)

    def test_multiple_uncommented_prs_fail_before_any_cloud_touch(self):
        self.initialize()
        before = self.remote_state()
        other = "b" * 64
        self.github.pages = [[
            raw_pr(f"[codex-5h-touch] {FIXTURE_KEY}", f"trigger/{FIXTURE_KEY[:12]}",
                   "2026-08-31T00:58:00Z", number=23),
            raw_pr(f"[codex-5h-touch] {other}", f"trigger/{other[:12]}",
                   "2026-08-31T00:59:00Z", number=24),
        ]]

        with self.assertRaises(ValueError):
            self.run_publish(enabled=True, dry_run=False)

        self.assertEqual(0, self.github.commented)
        self.assertEqual(before, self.remote_state())

    def test_canary_approval_never_repairs_a_different_uncommented_pr(self):
        self.initialize()
        before = self.remote_state()
        other = "d" * 64
        self.github.pages = [[raw_pr(
            f"[codex-5h-touch] {other}", f"trigger/{other[:12]}",
            "2026-08-31T00:59:00Z", number=31)]]

        with self.assertRaises(ValueError):
            self.run_publish(operation="canary", canary_approved=True, dry_run=False)

        self.assertEqual(0, self.github.created)
        self.assertEqual(0, self.github.commented)
        self.assertEqual(before, self.remote_state())

    def test_recent_trusted_pr_blocks_approved_canary(self):
        self.initialize()
        key = "d" * 64
        self.github.pages[-1].append(raw_pr(f"[codex-5h-touch] {key}",
                                            f"trigger/{key[:12]}", "2026-08-31T00:59:00Z",
                                            number=30))
        self.github.comment_pages[30] = [[raw_comment(
            expected_comment(key, "2026-08-31T00:59:00+00:00"))]]

        self.assertEqual("skip", self.run_publish(operation="canary", canary_approved=True, dry_run=False))

        self.assertEqual(0, self.github.created)
        self.assertEqual("2026-08-31T00:59:00+00:00", self.remote_state()["last_triggered_at"])
        self.assertEqual("refs/heads/main", git(self.remote, "for-each-ref", "--format=%(refname)"))

    def test_recent_commented_pr_blocks_repairing_an_existing_canary(self):
        self.initialize()
        completed = "d" * 64
        self.github.pages = [[
            raw_pr(f"[codex-5h-touch] {completed}", f"trigger/{completed[:12]}",
                   "2026-08-31T00:59:00Z", number=30),
            raw_pr("[codex-5h-touch] canary-000000000000", "trigger/000000000000",
                   "2026-08-31T00:58:00Z", number=31),
        ]]
        self.github.comment_pages[30] = [[raw_comment(
            expected_comment(completed, "2026-08-31T00:59:00+00:00"))]]

        self.assertEqual("skip", self.run_publish(
            operation="canary", canary_approved=True, dry_run=False))

        state = self.remote_state()
        self.assertEqual(0, self.github.created)
        self.assertEqual(0, self.github.commented)
        self.assertIn(completed, state["handled_keys"])
        self.assertNotIn("canary-000000000000", state["handled_keys"])
        self.assertEqual("2026-08-31T00:59:00+00:00", state["last_triggered_at"])

    def test_uncommented_pr_arriving_before_canary_mutation_fails_closed(self):
        self.initialize()
        before = self.remote_state()
        initial_list = self.github.list_prs
        calls = 0

        def list_with_late_pr(repository):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.github.pages[-1].append(raw_pr("[codex-5h-touch] " + "e" * 64,
                                                    "trigger/" + "e" * 12, "2026-08-31T00:59:00Z",
                                                    number=32))
            return initial_list(repository)

        self.github.list_prs = list_with_late_pr
        with self.assertRaises(ValueError):
            self.run_publish(operation="canary", canary_approved=True, dry_run=False)

        self.assertEqual(0, self.github.created)
        self.assertEqual(0, self.github.commented)
        self.assertEqual(before, self.remote_state())
        self.assertEqual(["refs/heads/main", "refs/heads/trigger/000000000000"],
                         git(self.remote, "for-each-ref", "--format=%(refname)").splitlines())

    def test_uncommented_pr_arriving_with_created_canary_fails_before_comment(self):
        self.initialize()
        before = self.remote_state()
        initial_list = self.github.list_prs
        calls = 0

        def list_with_final_race(repository):
            nonlocal calls
            calls += 1
            if calls == 4:
                other = "f" * 64
                self.github.pages[-1].append(raw_pr(
                    f"[codex-5h-touch] {other}", f"trigger/{other[:12]}",
                    "2026-08-31T00:59:00Z", number=55))
            return initial_list(repository)

        self.github.list_prs = list_with_final_race
        with self.assertRaises(ValueError):
            self.run_publish(operation="canary", canary_approved=True, dry_run=False)

        self.assertEqual(1, self.github.created)
        self.assertEqual(0, self.github.commented)
        self.assertEqual(before, self.remote_state())

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
        self.assertEqual(1, self.github.commented)
        self.assertEqual("2026-08-31T01:00:08+00:00", self.remote_state()["last_triggered_at"])

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
        self.github.comment_created_at = "2026-08-31T01:05:08Z"
        self.assertEqual("canary", self.run_publish(operation="canary", enabled=True, canary_approved=True,
                         dry_run=False, now=datetime(2026, 8, 31, 1, 5, tzinfo=UTC)))
        self.assertEqual(original, git(self.remote, "rev-parse", "refs/heads/trigger/000000000000"))
        self.assertEqual("2026-08-31T01:05:08+00:00", self.remote_state()["last_triggered_at"])


class GitHubTransportTest(unittest.TestCase):
    def test_all_pages_and_untrusted_text_use_literal_argument_boundaries(self):
        calls = []
        title = "[codex-5h-touch] literal $(do-not-execute)"

        def external(args, **kwargs):
            calls.append((args, kwargs))
            if "pulls?state=all" in args[-1]:
                return subprocess.CompletedProcess(args, 0, stdout='[[{"title":"page-one"}],[{"title":"page-two"}]]')
            if "comments?per_page=100" in args[-1]:
                return subprocess.CompletedProcess(args, 0, stdout='[[{"body":"page-one"}],[{"body":"page-two"}]]')
            if args[:4] == ["gh", "api", "--method", "POST"]:
                return subprocess.CompletedProcess(args, 0, stdout='{"html_url":"https://example.invalid/comment/1"}')
            return subprocess.CompletedProcess(args, 0, stdout="https://example.invalid/pr/1")

        with patch("scripts.publish.subprocess.run", side_effect=external):
            pages = GitHub().list_prs(REPOSITORY)
            GitHub().create_pr(REPOSITORY, "main", "trigger/123456789abc", title, Path("literal body.md"))
            comments = GitHub().list_issue_comments(REPOSITORY, 7)
            created = GitHub().create_issue_comment(REPOSITORY, 7, Path("literal comment.json"))
        self.assertEqual([[{"title": "page-one"}], [{"title": "page-two"}]], pages)
        self.assertEqual([[{"body": "page-one"}], [{"body": "page-two"}]], comments)
        self.assertEqual("https://example.invalid/comment/1", created["html_url"])
        self.assertEqual(["gh", "api", "--paginate", "--slurp",
                          "repos/example/codex-window-trigger/pulls?state=all&per_page=100"], calls[0][0])
        self.assertEqual(["gh", "pr", "create", "--repo", REPOSITORY, "--base", "main", "--head",
                          "trigger/123456789abc", "--title", title, "--body-file", "literal body.md"], calls[1][0])
        self.assertEqual(["gh", "api", "--paginate", "--slurp",
                          "repos/example/codex-window-trigger/issues/7/comments?per_page=100"], calls[2][0])
        self.assertEqual(["gh", "api", "--method", "POST",
                          "repos/example/codex-window-trigger/issues/7/comments",
                          "--input", "literal comment.json"], calls[3][0])
        self.assertTrue(all(not kwargs.get("shell", False) for _, kwargs in calls))


if __name__ == "__main__":
    unittest.main()
