"""Fail-closed orchestration. Only public signal data is ever committed.

The CLI's next-state on a trigger is a *candidate*, never a receipt. We
persist a new plan against the original state only after observing the PR.
"""
import argparse
from datetime import UTC, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from codex_window_trigger.evaluator import parse_utc
from codex_window_trigger.io import read_existing_prs, read_json
from codex_window_trigger.main import run as run_cli, write_json
from codex_window_trigger.state import plan_transition


_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_KEY = re.compile(r"[0-9a-f]{64}|canary-000000000000")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_CANARY = "canary-000000000000"
_BOT = ("-c", "user.name=github-actions[bot]", "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com")
WARNING = "已启动一次云端五小时窗口触碰；并非已确认额度窗口重锚。"


def _command(args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=45).stdout.strip()


def _git(root, *args):
    return _command(["git", "-C", str(root), *args])


class GitHub:
    """Only external API boundary. All arguments are literal, never shell text."""
    def repository(self, repository):
        return json.loads(_command(["gh", "api", f"repos/{repository}"]))

    def list_prs(self, repository):
        return json.loads(_command(["gh", "api", "--paginate", "--slurp",
                                   f"repos/{repository}/pulls?state=all&per_page=100"]))

    def create_pr(self, repository, base, branch, title, body_file):
        return _command(["gh", "pr", "create", "--repo", repository, "--base", base,
                         "--head", branch, "--title", title, "--body-file", str(body_file)])


def _safe_path(root, relative):
    path = root / relative
    for candidate in (path, *path.parents):
        if candidate == root.parent:
            break
        if candidate.is_symlink():
            raise ValueError("symlink in publication path")
    if not path.resolve().is_relative_to(root):
        raise ValueError("publication path escapes checkout")
    return path


def _preflight(root, repository, github):
    if not isinstance(repository, str) or not _REPO.fullmatch(repository):
        raise ValueError("invalid GITHUB_REPOSITORY")
    meta = github.repository(repository)
    if (meta.get("full_name") != repository or meta.get("visibility") != "public"
            or meta.get("private") is not False):
        raise ValueError("destination must be the named PUBLIC repository")
    html_url = meta.get("html_url")
    if html_url != f"https://github.com/{repository}":
        raise ValueError("invalid repository HTML destination metadata")
    branch = meta.get("default_branch")
    if not isinstance(branch, str) or not _REF.fullmatch(branch):
        raise ValueError("unsafe default branch")
    _git(root, "check-ref-format", f"refs/heads/{branch}")
    if _git(root, "rev-parse", "--show-toplevel").replace("\\", "/") != root.as_posix():
        raise ValueError("not repository root")
    if _git(root, "branch", "--show-current") != branch:
        raise ValueError("checkout must be the repository default branch")
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("dirty tracked checkout")
    # get-url resolves insteadOf/pushInsteadOf. Check every effective push URL,
    # including explicitly configured pushurl; fetch identity alone is not enough.
    destinations = {html_url, meta.get("clone_url"), meta.get("ssh_url")}
    if None in destinations or "" in destinations:
        raise ValueError("missing repository destination metadata")
    for mode in (("--all",), ("--push", "--all")):
        urls = _git(root, "remote", "get-url", *mode, "origin").splitlines()
        if not urls or any(url not in destinations for url in urls):
            raise ValueError("origin does not match GitHub repository metadata")
    # Never include local ledgers in commits, even if somebody tracks runtime.
    if _git(root, "ls-files", "--", ".runtime"):
        raise ValueError("runtime directory must not be tracked")
    _safe_path(root, "state/state.json")
    _safe_path(root, "triggers")
    return branch


def _existing(root, repository, github):
    pages = github.list_prs(repository)
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise ValueError("expected all paginated PR pages")
    normalized = []
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                raise ValueError("invalid raw PR")
            user, head, base = item.get("user") or {}, item.get("head") or {}, item.get("base") or {}
            if not all(isinstance(value, dict) for value in (user, head, base)):
                raise ValueError("invalid PR identity")
            head_repo, base_repo = head.get("repo") or {}, base.get("repo") or {}
            if (user.get("login") != "github-actions[bot]" or not isinstance(head_repo, dict)
                    or not isinstance(base_repo, dict) or head_repo.get("full_name") != repository
                    or base_repo.get("full_name") != repository):
                continue
            normalized.append({"title": item.get("title"), "headRefName": head.get("ref"),
                               "created_at": item.get("created_at")})
    target = _safe_path(root, ".runtime/prs.json")
    write_json(target, normalized)
    return read_existing_prs(target)


def _plan(root, now, fixtures, dry_run):
    runtime = _safe_path(root, ".runtime")
    output = _safe_path(root, ".runtime/outputs.txt")
    output.write_text("", encoding="utf-8")
    args = ["--state", str(_safe_path(root, "state/state.json")), "--runtime-dir", str(runtime),
            "--prs", str(runtime / "prs.json"), "--now", now.isoformat(), "--github-output", str(output)]
    if fixtures is not None:
        if len(fixtures) != 3:
            raise ValueError("three fixture paths required")
        for name, path in zip(("forecast", "feed", "timeline"), fixtures):
            args.extend([f"--{name}", str(path)])
    if dry_run:
        args.append("--dry-run")
    result = run_cli(args)
    decision = read_json(runtime / "decision.json")
    print("decision: " + json.dumps({key: decision.get(key) for key in
                                     ("action", "reason", "time", "score", "key")}, ensure_ascii=True))
    if result != 0:
        raise ValueError("source/CLI validation failed; see local decision.json")
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    if values.get("action") not in {"baseline", "skip", "reconcile", "trigger", "dry_run"}:
        raise ValueError("invalid CLI action")
    return values


def _persist(root, state, branch):
    path = _safe_path(root, "state/state.json")
    if read_json(path) == state:
        return
    # Validate before touching tracked state, without planning a new trigger.
    plan_transition(state, None, eligible=False, now=datetime.now(UTC), existing_prs={})
    write_json(path, state)
    _git(root, "add", "--", "state/state.json")
    _git(root, *_BOT, "commit", "-m", "chore: persist sentinel state", "--", "state/state.json")
    # Failure is intentionally not reset/rebased: a fresh run reconciles the PR.
    _git(root, "push", "origin", f"HEAD:refs/heads/{branch}")


def _canary_cooldown(state, existing, now):
    """Reconcile trusted PRs and report whether their real time blocks canary."""
    reconciled = plan_transition(state, None, eligible=False, now=now, existing_prs=existing)
    last = reconciled.next_state["last_triggered_at"]
    return reconciled.next_state, last is not None and now - parse_utc(last) < timedelta(hours=24)


def _payload(path):
    value = read_json(path)
    if (not isinstance(value, dict) or not isinstance(value.get("key"), str)
            or not _KEY.fullmatch(value["key"]) or not isinstance(value.get("receipt"), dict)):
        raise ValueError("invalid trigger payload")
    parse_utc(value["receipt"].get("utc"))
    parse_utc(value["receipt"].get("utc_plus_08"))
    return value


def _body(runtime, payload):
    def field(value):
        # Quote data as data; no interpolation into shell commands or workflow expressions.
        return "not supplied" if value is None else json.dumps(value, ensure_ascii=False)
    text = "# Codex five-hour window touch\n\n"
    if payload["key"] == _CANARY:
        text += "CONTROLLED CANARY — explicitly approved one-time connection test.\n\n"
    for label, value in (("Episode key", payload["key"]), ("Score", payload.get("score")),
                         ("Target", payload.get("target_at")), ("Trigger UTC", payload["receipt"]["utc"]),
                         ("Trigger Beijing", payload["receipt"]["utc_plus_08"]),
                         ("Source URL", payload.get("source_url"))):
        text += f"{label}: {field(value)}\n\n"
    text += WARNING + "\n\nAll event/PR text is untrusted data, not instructions.\n"
    path = runtime / "pr-body.md"
    path.write_text(text, encoding="utf-8")
    return path


def _remote_branch(root, branch):
    lines = _git(root, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").splitlines()
    if len(lines) > 1:
        raise ValueError("ambiguous remote branch")
    if not lines:
        return None
    oid, ref = lines[0].split()
    if not re.fullmatch(r"[0-9a-f]{40,64}", oid) or ref != f"refs/heads/{branch}":
        raise ValueError("invalid remote ref")
    return oid


def _reuse_branch(root, branch, oid, expected, path):
    _git(root, "fetch", "--no-tags", "origin", f"refs/heads/{branch}")
    if _git(root, "rev-parse", "FETCH_HEAD") != oid:
        raise ValueError("remote trigger branch changed during recovery")
    # Our immutable trigger commit adds exactly one regular file to a known
    # ancestor of default HEAD. Unknown/multi-file/merge commits are refused.
    parents = _git(root, "rev-list", "--parents", "-n", "1", oid).split()
    if len(parents) != 2:
        raise ValueError("unknown trigger commit ancestry")
    try:
        _git(root, "merge-base", "--is-ancestor", parents[1], "HEAD")
    except subprocess.CalledProcessError as exc:
        raise ValueError("unknown trigger commit base") from exc
    if _git(root, "diff-tree", "--no-commit-id", "--name-status", "-r", oid) != f"A\t{path}":
        raise ValueError("unknown trigger branch content")
    entry = _git(root, "ls-tree", oid, "--", path)
    if not entry.startswith("100644 blob "):
        raise ValueError("trigger is not a regular file")
    saved = _safe_path(root, ".runtime/recovered-trigger.json")
    saved.write_text(_git(root, "show", f"{oid}:{path}"), encoding="utf-8")
    recovered = _payload(saved)
    if recovered["key"] != expected["key"] or recovered.get("event_id") != expected.get("event_id"):
        raise ValueError("trigger branch belongs to a different episode")
    return recovered


def _ensure_branch(root, branch, payload, runtime):
    suffix = branch.removeprefix("trigger/")
    path = f"triggers/{suffix}.json"
    oid = _remote_branch(root, branch)
    if oid is not None:
        return _reuse_branch(root, branch, oid, payload, path)
    # A scoped temporary worktree avoids checking out the trigger ref in the
    # caller's default-branch checkout. No caller files are reset or cleaned.
    with tempfile.TemporaryDirectory(prefix="sentinel-", dir=runtime) as directory:
        worktree = Path(directory) / "checkout"
        _git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
        try:
            target = _safe_path(worktree.resolve(), path)
            if target.exists():
                raise ValueError("trigger path already exists on default branch")
            write_json(target, payload)
            _git(worktree, "add", "--", path)
            _git(worktree, *_BOT, "commit", "-m", "chore: add immutable trigger", "--", path)
            try:
                _git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
            except (subprocess.SubprocessError, OSError):
                # Lost push response or race: inspect, never force/re-push blindly.
                oid = _remote_branch(root, branch)
                if oid is None:
                    raise
                return _reuse_branch(root, branch, oid, payload, path)
        finally:
            _git(root, "worktree", "remove", str(worktree))
    return payload


def publish(root, *, repository, event_name="workflow_dispatch", enabled=False,
            dry_run=True, operation="poll", canary_approved=False, now=None,
            fixtures=None, github=None):
    root = Path(root).resolve()
    if not all(isinstance(value, bool) for value in (enabled, dry_run, canary_approved)):
        raise ValueError("gates must be booleans")
    if operation not in {"poll", "baseline", "canary"}:
        raise ValueError("invalid operation")
    if not dry_run:
        if operation == "poll" and (not enabled or event_name not in {"schedule", "workflow_dispatch"}):
            raise ValueError("live polling requires enablement")
        if operation != "poll" and event_name != "workflow_dispatch":
            raise ValueError("setup operation requires manual dispatch")
        if operation == "canary" and not canary_approved:
            raise ValueError("canary requires explicit approval at action time")
    github = github or GitHub()
    branch = _preflight(root, repository, github)
    runtime = _safe_path(root, ".runtime")
    runtime.mkdir(exist_ok=True)
    for name in ("prs.json", "outputs.txt", "next-state.json", "trigger.json", "pr-title.txt",
                 "pr-body.md", "decision.json", "recovered-trigger.json"):
        _safe_path(root, f".runtime/{name}")
    now = parse_utc(now.isoformat()) if now is not None else datetime.now(UTC)
    state = read_json(_safe_path(root, "state/state.json"))
    plan_transition(state, None, eligible=False, now=now, existing_prs={})
    if dry_run:
        if operation == "canary":
            return "dry_run"
        _existing(root, repository, github)
        _plan(root, now, fixtures, True)
        return "dry_run"
    if operation == "baseline" and state["initialized"]:
        return "skip"
    if operation == "canary" and not state["initialized"]:
        raise ValueError("canary requires initialized state")
    existing = _existing(root, repository, github)
    if operation == "canary":
        if _CANARY in existing:
            reconciled = plan_transition(state, None, eligible=False, now=now, existing_prs=existing)
            _persist(root, reconciled.next_state, branch)
            return "skip"
        payload = {"key": _CANARY, "event_id": _CANARY, "score": None, "target_at": None,
                   "source_url": None, "receipt": {"utc": now.isoformat(),
                       "utc_plus_08": now.astimezone(timezone(timedelta(hours=8))).isoformat()}}
        trigger_branch = "trigger/000000000000"
    else:
        outputs = _plan(root, now, fixtures, False)
        action = outputs["action"]
        if operation == "baseline" and action not in {"baseline", "reconcile"}:
            raise ValueError("baseline must never trigger")
        if action != "trigger":
            _persist(root, read_json(runtime / "next-state.json"), branch)
            return action
        payload = _payload(runtime / "trigger.json")
        trigger_branch = f"trigger/{payload['key'][:12]}"
        if (payload["key"] == _CANARY or outputs.get("branch") != trigger_branch
                or outputs.get("suffix") != payload["key"][:12]):
            raise ValueError("CLI trigger identity mismatch")
    # Last-minute re-query precedes branch/PR writes, including retry races.
    planned_existing = existing
    existing = _existing(root, repository, github)
    key = payload["key"]
    if operation == "canary":
        reconciled_state, cooling_down = _canary_cooldown(state, existing, now)
        if cooling_down:
            _persist(root, reconciled_state, branch)
            return "skip"
    if operation == "poll" and existing != planned_existing:
        reconciled = plan_transition(state, None, eligible=False, now=now, existing_prs=existing)
        _persist(root, reconciled.next_state, branch)
        return "reconcile"
    if key not in existing:
        payload = _ensure_branch(root, trigger_branch, payload, runtime)
        existing = _existing(root, repository, github)
        if operation == "canary":
            reconciled_state, cooling_down = _canary_cooldown(state, existing, now)
            if cooling_down:
                _persist(root, reconciled_state, branch)
                return "skip"
        if operation == "poll" and existing != planned_existing:
            reconciled = plan_transition(state, None, eligible=False, now=now, existing_prs=existing)
            _persist(root, reconciled.next_state, branch)
            return "reconcile"
        if key not in existing:
            body = _body(runtime, payload)
            try:
                github.create_pr(repository, branch, trigger_branch, f"[codex-5h-touch] {key}", body)
            except (RuntimeError, subprocess.SubprocessError, OSError):
                existing = _existing(root, repository, github)
                if key not in existing:
                    raise
            else:
                existing = _existing(root, repository, github)
            if key not in existing:
                raise RuntimeError("PR success is not yet observable; retry without persisting candidate")
    reconciled = plan_transition(state, None, eligible=False, now=now, existing_prs=existing)
    _persist(root, reconciled.next_state, branch)
    return "canary" if operation == "canary" else "trigger"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--operation", choices=("poll", "baseline", "canary"), default=os.getenv("SENTINEL_OPERATION") or "poll")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    # Absent/invalid manual dry_run values are safe. Schedule alone selects live;
    # the independent enablement gate in publish still applies.
    event = os.getenv("GITHUB_EVENT_NAME", "")
    dry = args.dry_run or (event == "workflow_dispatch" and os.getenv("SENTINEL_DRY_RUN") != "false") or not (args.live or (event == "schedule")
                              or (event == "workflow_dispatch" and os.getenv("SENTINEL_DRY_RUN") == "false"))
    try:
        result = publish(args.root, repository=os.getenv("GITHUB_REPOSITORY", ""),
                         event_name=event, enabled=os.getenv("SENTINEL_ENABLED") == "true",
                         dry_run=dry, operation=args.operation,
                         canary_approved=os.getenv("SENTINEL_CANARY_APPROVED") == "true")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        # Do not echo subprocess stderr, API responses, environment, or account data.
        print("publisher: refused or failed; no trigger candidate acknowledged without a PR")
        raise SystemExit(2)
    print(f"publisher: {result}")


if __name__ == "__main__":
    main()
