"""Fail-closed command-line entry point for the Codex window trigger."""
import argparse
from datetime import UTC, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .evaluator import evaluate_sources, parse_utc
from .io import read_existing_prs, read_json
from .models import Episode
from .sources import fetch_sources
from .state import plan_transition


_GENERATED = ("next-state.json", "trigger.json", "pr-title.txt", "pr-body.md")
_OUTPUT_KEY = re.compile(r"^[a-z_]+$")
_OUTPUT_VALUE = re.compile(r"^[A-Za-z0-9_./:-]+$")
_OUTPUT_KEYS = frozenset({"action", "reason", "state_changed", "branch", "suffix"})


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="codex-window-trigger")
    parser.add_argument("--now")
    parser.add_argument("--forecast")
    parser.add_argument("--feed")
    parser.add_argument("--timeline")
    parser.add_argument("--state", default="state/state.json")
    parser.add_argument("--prs")
    parser.add_argument("--runtime-dir", default=".runtime")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def load_payloads(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    fixtures = (args.forecast, args.feed, args.timeline)
    if any(fixtures) and not all(fixtures):
        raise ValueError("fixture paths must be supplied together")
    if all(fixtures):
        values = tuple(read_json(Path(item)) for item in fixtures)
        if not all(isinstance(item, dict) for item in values):
            raise ValueError("fixture JSON must be an object")
        return values  # type: ignore[return-value]
    return fetch_sources()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_decision(runtime: Path, action: str, reason: str, episode: Episode | None = None,
                   now: datetime | None = None, dry_run: bool = False, proposed_action: str | None = None) -> None:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    value: dict[str, Any] = {"action": action, "reason": reason, "time": stamp, "score": episode.score if episode else None,
                             "key": episode.key if episode else None}
    if dry_run:
        value["dry_run"] = True
    if proposed_action is not None:
        value["proposed_action"] = proposed_action
    write_json(runtime / "decision.json", value)


def write_trigger_files(runtime: Path, episode: Episode, now: datetime) -> None:
    suffix = episode.key[:12]
    utc = now.astimezone(UTC)
    local = utc.astimezone(timezone(timedelta(hours=8)))
    payload = {"key": episode.key, "event_id": episode.event_id, "score": episode.score, "summary": episode.summary,
               "source_url": episode.source_url, "target_at": episode.target_at.isoformat() if episode.target_at else None,
               "announced_at": episode.announced_at.isoformat(), "receipt": {"utc": utc.isoformat(), "utc_plus_08": local.isoformat()}}
    write_json(runtime / "trigger.json", payload)
    (runtime / "pr-title.txt").write_text(f"[codex-5h-touch] {episode.key}\n", encoding="utf-8")
    (runtime / "pr-body.md").write_text(
        f"# Codex five-hour reset signal\n\nScore: {episode.score:g}\n\n{episode.summary}\n\nSource: {episode.source_url}\n\n"
        f"Receipt UTC: {utc.isoformat()}\n\nReceipt UTC+08:00: {local.isoformat()}\n\nBranch suffix: `{suffix}`\n", encoding="utf-8")


def emit_outputs(path: str | None, values: dict[str, str]) -> None:
    if path is None:
        return
    if not all(isinstance(key, str) and key in _OUTPUT_KEYS and _OUTPUT_KEY.fullmatch(key) and isinstance(value, str) and "\n" not in value and _OUTPUT_VALUE.fullmatch(value)
               for key, value in values.items()):
        raise ValueError("unsafe GitHub output")
    target = Path(path)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _remove_stale(runtime: Path) -> None:
    for name in _GENERATED:
        candidate = runtime / name
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink()


def _error(runtime: Path, reason: str, now: datetime | None = None) -> int:
    _remove_stale(runtime)
    try:
        write_decision(runtime, "error", reason, now=now)
    except OSError:
        pass
    return 2


def run(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        now = parse_utc(args.now) if args.now else datetime.now(UTC)
    except (ValueError, TypeError):
        return 2
    runtime = Path(args.runtime_dir)
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        forecast, feed, timeline = load_payloads(args)
        source = evaluate_sources(forecast, feed, timeline, now=now)
    except (OSError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        return _error(runtime, type(exc).__name__, now)
    if not source.valid:
        return _error(runtime, source.reason, now)
    try:
        if not args.dry_run and not args.prs:
            raise ValueError("live plan requires PR list")
        state = read_json(Path(args.state))
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        existing = read_existing_prs(Path(args.prs)) if args.prs else {}
        transition = plan_transition(state, source.episode, eligible=source.eligible, now=now, existing_prs=existing)
    except (OSError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        return _error(runtime, type(exc).__name__, now)
    if args.dry_run:
        _remove_stale(runtime)
        try:
            write_decision(runtime, "dry_run", transition.reason, source.episode, now, True, transition.action)
            emit_outputs(args.github_output, {"action": "dry_run", "reason": transition.reason, "state_changed": "false"})
        except (OSError, ValueError):
            return _error(runtime, "output_error", now)
        return 0
    if transition.action != "trigger":
        _remove_stale(runtime)
    try:
        write_decision(runtime, transition.action, transition.reason, source.episode, now)
        write_json(runtime / "next-state.json", transition.next_state)
        outputs = {"action": transition.action, "reason": transition.reason,
                   "state_changed": str(transition.next_state != state).lower()}
        if transition.action == "trigger":
            write_trigger_files(runtime, source.episode, now)  # source is eligible, so Episode is present.
            suffix = source.episode.key[:12]  # type: ignore[union-attr]
            outputs.update({"branch": f"trigger/{suffix}", "suffix": suffix})
        emit_outputs(args.github_output, outputs)
    except (OSError, ValueError, TypeError):
        return _error(runtime, "output_error", now)
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
