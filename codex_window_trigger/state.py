"""Pure planning for the reset-trigger persistent state."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .evaluator import parse_utc
from .models import Episode

Action = Literal["baseline", "skip", "trigger", "reconcile"]
_COOLDOWN = timedelta(hours=24)
_HANDLED_STATUSES = {"baseline", "triggered"}
_STATUSES = _HANDLED_STATUSES | {"pending"}


@dataclass(frozen=True)
class Transition:
    action: Action
    reason: str
    next_state: dict[str, Any]


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "initialized": False, "last_triggered_at": None, "episodes": {}}


def plan_transition(state: dict[str, Any], episode: Episode | None, *, eligible: bool = True,
                    now: datetime, existing_prs: dict[str, datetime]) -> Transition:
    """Plan one valid source observation without mutating persisted state."""
    _validate_state(state)
    now = _utc_datetime(now, "now")
    _validate_existing_prs(existing_prs)
    if episode is not None and not isinstance(episode, Episode):
        raise ValueError("episode must be an Episode or None")
    if not isinstance(eligible, bool):
        raise ValueError("eligible must be a bool")

    next_state = deepcopy(state)
    episodes: dict[str, dict[str, str]] = next_state["episodes"]
    handled = set(next_state.get("handled_keys", ()))
    handled.update(key for key, record in episodes.items() if record["status"] in _HANDLED_STATUSES)

    reconciled = _reconcile_existing_prs(episodes, handled, existing_prs)
    attempts = next_state.get("comment_attempts")
    if attempts and any(key in existing_prs for key in attempts):
        next_state.pop("comment_attempts")
        reconciled = True
    newest_pr = max((_utc_datetime(value, "confirmed touch timestamp") for value in existing_prs.values()), default=None)
    previous_last = _parse_optional_timestamp(next_state["last_triggered_at"], "last_triggered_at")
    effective_last = max((value for value in (previous_last, newest_pr) if value is not None), default=None)
    if effective_last is not None and next_state["last_triggered_at"] != effective_last.isoformat():
        next_state["last_triggered_at"] = effective_last.isoformat()
        reconciled = True

    if not next_state["initialized"]:
        next_state["initialized"] = True
        if episode is not None and episode.key not in existing_prs:
            _record(episodes, episode, "baseline", now)
            handled.add(episode.key)
        _finish(next_state, handled)
        return Transition("reconcile" if reconciled else "baseline", "existing_pr" if reconciled else "first_valid_observation", next_state)

    if reconciled:
        if episode is not None and episode.key not in handled and episode.key not in existing_prs:
            _mark_pending_if_cooling_down(episodes, episode, now, effective_last)
        _finish(next_state, handled)
        return Transition("reconcile", "existing_pr", next_state)

    if next_state.get("comment_attempts"):
        _finish(next_state, handled)
        return Transition("skip", "comment_attempt_pending", next_state)
    if not eligible or episode is None:
        _finish(next_state, handled)
        return Transition("skip", "ineligible_observation", next_state)
    if episode.key in handled:
        _finish(next_state, handled)
        return Transition("skip", "episode_already_handled", next_state)
    if _cooldown_active(now, effective_last):
        _mark_pending_if_cooling_down(episodes, episode, now, effective_last)
        _finish(next_state, handled)
        return Transition("skip", "cooldown_active", next_state)

    _record(episodes, episode, "triggered", now)
    handled.add(episode.key)
    next_state["last_triggered_at"] = now.isoformat()
    _finish(next_state, handled)
    return Transition("trigger", "new_eligible_episode", next_state)


def _validate_state(state: object) -> None:
    if not isinstance(state, dict) or set(state) - {"schema_version", "initialized", "last_triggered_at", "episodes", "handled_keys", "comment_attempts"}:
        raise ValueError("invalid state")
    if not {"schema_version", "initialized", "last_triggered_at", "episodes"}.issubset(state):
        raise ValueError("missing required state field")
    if state.get("schema_version") != 1 or isinstance(state.get("schema_version"), bool):
        raise ValueError("unsupported schema version")
    if not isinstance(state.get("initialized"), bool) or not isinstance(state.get("episodes"), dict):
        raise ValueError("invalid state")
    _parse_optional_timestamp(state.get("last_triggered_at"), "last_triggered_at")
    for key, record in state["episodes"].items():
        if not isinstance(key, str) or not key or not isinstance(record, dict):
            raise ValueError("invalid episode record")
        if record.get("status") not in _STATUSES or not isinstance(record.get("event_id"), str):
            raise ValueError("invalid episode record")
        if not isinstance(record.get("at"), str):
            raise ValueError("invalid episode record")
        parse_utc(record["at"])
    if "handled_keys" in state:
        keys = state["handled_keys"]
        if not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys) or keys != sorted(set(keys)):
            raise ValueError("invalid handled keys")
    if "comment_attempts" in state:
        attempts = state["comment_attempts"]
        if (not isinstance(attempts, dict) or len(attempts) > 1
                or any(not isinstance(key, str) or not key or not isinstance(value, str)
                       for key, value in attempts.items())):
            raise ValueError("invalid comment attempts")
        for value in attempts.values():
            parse_utc(value)


def _validate_existing_prs(existing_prs: object) -> None:
    if not isinstance(existing_prs, dict):
        raise ValueError("existing_prs must be a dictionary")
    for key, created_at in existing_prs.items():
        if not isinstance(key, str) or not key:
            raise ValueError("invalid PR key")
        _utc_datetime(created_at, "confirmed touch timestamp")


def _utc_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be an aware datetime")
    return value.astimezone(UTC)


def _parse_optional_timestamp(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timestamp or null")
    return parse_utc(value)


def _record(episodes: dict[str, dict[str, str]], episode: Episode, status: str, at: datetime) -> None:
    episodes[episode.key] = {"status": status, "event_id": episode.event_id, "at": at.isoformat()}


def _reconcile_existing_prs(episodes: dict[str, dict[str, str]], handled: set[str], existing_prs: dict[str, datetime]) -> bool:
    changed = False
    for key, created_at in existing_prs.items():
        at = _utc_datetime(created_at, "confirmed touch timestamp").isoformat()
        current = episodes.get(key)
        # A previously reconciled record may have been trimmed. Its handled
        # identity remains durable; the caller still considers every touch time
        # when computing cooldown. Do not re-add and trim it on every poll.
        if current is None and key in handled:
            continue
        replacement = {"status": "triggered", "event_id": current["event_id"] if current else key, "at": at}
        if current != replacement:
            episodes[key] = replacement
            changed = True
        if key not in handled:
            handled.add(key)
            changed = True
    return changed


def _cooldown_active(now: datetime, last_triggered_at: datetime | None) -> bool:
    return last_triggered_at is not None and now - last_triggered_at < _COOLDOWN


def _mark_pending_if_cooling_down(episodes: dict[str, dict[str, str]], episode: Episode,
                                  now: datetime, last_triggered_at: datetime | None) -> None:
    if _cooldown_active(now, last_triggered_at) and episode.key not in episodes:
        _record(episodes, episode, "pending", now)


def _finish(state: dict[str, Any], handled: set[str]) -> None:
    records = sorted(state["episodes"].items(), key=lambda item: parse_utc(item[1]["at"]), reverse=True)[:100]
    state["episodes"] = dict(records)
    state["handled_keys"] = sorted(handled)
