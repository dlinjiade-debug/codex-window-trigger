"""Pure, fail-closed validation of public reset-source observations."""
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
import re
from typing import Any
from urllib.parse import urlsplit

from .models import Episode, SourceDecision

THRESHOLD = 30
MAX_AGE = timedelta(seconds=600)
MAX_FUTURE_SKEW = timedelta(seconds=60)
MAX_TARGET_DISTANCE = timedelta(hours=5)
_EXCLUDED = re.compile(r"\b(?:banked|credits?|referrals?|juice|incident)\b", re.I)
_COMPLETED = re.compile(r"\b(?:have now reset|has been reset|reset button pressed|reset(?:\s+\S+)*\s+applied)\b", re.I)
_COMPLETED_STATES = {"archive", "archived", "completed", "expired", "confirmed"}


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return result.astimezone(UTC)


def _reject(reason: str, episode: Episode | None = None, *, valid: bool = False) -> SourceDecision:
    return SourceDecision(False, reason, episode, valid)


def _time(value: object) -> datetime:
    return parse_utc(value)  # type: ignore[arg-type]


def _fresh(value: object, now: datetime) -> tuple[str | None, datetime | None]:
    try:
        parsed = _time(value)
    except ValueError:
        return "source_schema_invalid", None
    if parsed > now + MAX_FUTURE_SKEW:
        return "source_from_future", None
    if now - parsed > MAX_AGE:
        return "source_too_old", None
    return None, parsed


def _post_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or re.fullmatch(r"[1-9]\d{0,19}", value) is None:
        return None
    return value


def _url_is_post(value: object, post_id: str) -> bool:
    if not isinstance(value, str): return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and not parsed.username and not parsed.password and port is None
            and not parsed.query and not parsed.fragment and parsed.hostname in {"x.com", "twitter.com"}
            and re.fullmatch(rf"/[^/]+/status/{post_id}/?", parsed.path) is not None)


def _structured_tags_valid(item: dict[str, Any]) -> bool:
    for name in ("reason_tags", "tags"):
        if name not in item:
            continue
        value = item[name]
        if (not isinstance(value, list) or any(not isinstance(tag, str) or not tag.strip()
                                               for tag in value)):
            return False
    return True


def _text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for name in ("summary", "text", "title", "message", "reason"):
        value = item.get(name)
        if isinstance(value, str): parts.append(value)
    for name in ("reason_tags", "tags"):
        value = item.get(name)
        if isinstance(value, list):
            parts.extend(re.sub(r"[_-]+", " ", tag) for tag in value)
    return " ".join(parts)


def _completed(item: dict[str, Any]) -> bool:
    states = (item.get("status"), item.get("state"), item.get("reset_verification_status"), item.get("verification_status"))
    time_kind = item.get("time_kind")
    return (any(isinstance(value, str) and value.lower() in _COMPLETED_STATES for value in states)
            or isinstance(time_kind, str) and time_kind.lower() in {"confirmation", "confirmed"}
            or bool(_COMPLETED.search(_text(item))))


def _targets(item: dict[str, Any]) -> tuple[list[datetime], list[tuple[datetime | None, datetime | None]]]:
    exact: list[datetime] = []
    windows: list[tuple[datetime | None, datetime | None]] = []
    for field in ("target_at", "effective_at"):
        if field in item and item[field] is not None: exact.append(_time(item[field]))
    for field in ("window", "official_window"):
        if field not in item or item[field] is None: continue
        window = item[field]
        if not isinstance(window, dict): raise ValueError("window invalid")
        values: dict[str, datetime] = {}
        for key in ("target_at", "start_at", "end_at", "start", "end"):
            if key in window and window[key] is not None: values[key] = _time(window[key])
        start, end = values.get("start_at", values.get("start")), values.get("end_at", values.get("end"))
        if start and end and start > end: raise ValueError("interval invalid")
        if "target_at" in values: exact.append(values["target_at"])
        windows.append((start, end))
    return exact, windows


def _target(*items: dict[str, Any]) -> datetime | None:
    exact: list[datetime] = []; windows: list[tuple[datetime | None, datetime | None]] = []
    for item in items:
        values, ranges = _targets(item); exact.extend(values); windows.extend(ranges)
    if exact and any(value != exact[0] for value in exact): raise ValueError("conflicting target")
    result = exact[0] if exact else None
    if result:
        if any((start and result < start) or (end and result > end) for start, end in windows): raise ValueError("target interval conflict")
        return result
    ends = [end for _, end in windows if end]
    if ends and any(end != ends[0] for end in ends): raise ValueError("conflicting deadline")
    return ends[0] if ends else None


def _event(events: list[Any], post_id: str) -> dict[str, Any] | None:
    matches = [x for x in events if isinstance(x, dict) and _post_id(x.get("id")) == post_id]
    return matches[0] if len(matches) == 1 else None


def _quiet_history(forecast: dict[str, Any], feed: dict[str, Any], timeline: dict[str, Any], now: datetime) -> str | None:
    last_reset: datetime | None = None
    if forecast.get("last_reset_at") is not None:
        try:
            last_reset = _time(forecast["last_reset_at"])
        except ValueError:
            return "source_schema_invalid"
        if last_reset > now:
            return "source_from_future"
    boundary: datetime | None = None
    latest_alert = forecast.get("latest_alert")
    if latest_alert is not None:
        if not isinstance(latest_alert, dict):
            return "source_schema_invalid"
        latest_id = _post_id(latest_alert.get("id"))
        if latest_id is None or latest_alert.get("kind") != "reset":
            return "source_schema_invalid"
        if latest_alert.get("state") != "confirmed":
            return "official_signal_missing"
        try:
            latest_at = _time(latest_alert.get("source_at"))
        except ValueError:
            return "source_schema_invalid"
        if latest_at > now:
            return "source_from_future"
        if not _url_is_post(latest_alert.get("url"), latest_id):
            return "source_schema_invalid"
        feed_event, timeline_event = _event(feed["events"], latest_id), _event(timeline["events"], latest_id)
        if not feed_event or not timeline_event:
            return "source_schema_invalid"
        if any((event.get("type"), event.get("group"), event.get("scope")) != ("reset", "reset", "global") for event in (feed_event, timeline_event)):
            return "source_schema_invalid"
        try:
            if any(_time(event.get("announced_at")) != latest_at for event in (feed_event, timeline_event)):
                return "source_schema_invalid"
        except ValueError:
            return "source_schema_invalid"
        boundary = latest_at
    for events in (feed["events"], timeline["events"]):
        for item in events:
            if not isinstance(item, dict):
                continue
            if (item.get("type"), item.get("group"), item.get("scope")) != ("reset", "reset", "global"):
                continue
            try:
                announced_at = _time(item.get("announced_at"))
            except ValueError:
                return "source_schema_invalid"
            if announced_at > now:
                return "source_from_future"
            if boundary is not None and announced_at > boundary:
                return "completed_reset"
            if not _completed(item) and boundary is None:
                return "source_schema_invalid"
    return None


def evaluate_sources(forecast: dict[str, Any], feed: dict[str, Any], timeline: dict[str, Any], *, now: datetime) -> SourceDecision:
    if not all(isinstance(x, dict) for x in (forecast, feed, timeline)): return _reject("source_schema_invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None: return _reject("now_invalid")
    now = now.astimezone(UTC)
    if not isinstance(feed.get("events"), list) or not isinstance(timeline.get("events"), list): return _reject("source_schema_invalid")
    if any(isinstance(item, dict) and not _structured_tags_valid(item)
           for item in (*feed["events"], *timeline["events"])):
        return _reject("source_schema_invalid")
    if feed.get("stale") is not False: return _reject("feed_stale")
    if feed.get("source") != "x-api": return _reject("feed_source_invalid")
    parsed: dict[str, datetime] = {}
    for name, value in (("forecast", forecast.get("updated_at")), ("feed", feed.get("fetched_at")), ("timeline_updated", timeline.get("updated_at")), ("timeline_fetched", timeline.get("fetched_at"))):
        if value is None and name.startswith("timeline"): continue
        reason, value_at = _fresh(value, now)
        if reason: return _reject(reason)
        parsed[name] = value_at  # type: ignore[assignment]
    probabilities = forecast.get("probabilities")
    score = probabilities.get("signal_percent") if isinstance(probabilities, dict) else None
    if (forecast.get("mode") == "model" and forecast.get("official_signal") is None
            and forecast.get("alert_event_id") is None and forecast.get("signal_tier") is None
            and isinstance(probabilities, dict) and "signal_percent" in probabilities and score is None):
        reason = _quiet_history(forecast, feed, timeline, now)
        return _reject(reason) if reason else _reject("no_signal", valid=True)
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score) or not 0 <= score <= 100: return _reject("source_schema_invalid")
    signal = forecast.get("official_signal")
    if signal is None and not feed["events"] and not timeline["events"]: return _reject("no_signal", valid=True)
    if not isinstance(signal, dict): return _reject("official_signal_missing")
    if not _structured_tags_valid(signal): return _reject("source_schema_invalid")
    post_id = _post_id(signal.get("tweet_id"))
    if post_id is None: return _reject("source_schema_invalid")
    feed_event, timeline_event = _event(feed["events"], post_id), _event(timeline["events"], post_id)
    if not feed_event or not timeline_event: return _reject("event_missing")
    if _post_id(feed_event.get("id")) != post_id or _post_id(timeline_event.get("id")) != post_id: return _reject("event_conflict")
    for key, expected in (("type", "reset"), ("group", "reset"), ("scope", "global")):
        if feed_event.get(key) != expected or timeline_event.get(key) != expected: return _reject("event_conflict")
    for event in (feed_event, timeline_event):
        if "preview" in event and not isinstance(event["preview"], bool): return _reject("source_schema_invalid")
        if event.get("url") is not None and not _url_is_post(event["url"], post_id): return _reject("event_url_invalid")
    if not _url_is_post(signal.get("url"), post_id): return _reject("signal_url_invalid")
    alert_values = [x for x in (forecast.get("alert_event_id"), signal.get("alert_event_id")) if x is not None]
    if len(alert_values) == 2 and alert_values[0] != alert_values[1]: return _reject("alert_id_conflict")
    alert_id = alert_values[0] if alert_values else None
    if alert_id is not None and (not isinstance(alert_id, str) or not alert_id.strip() or len(alert_id) > 256): return _reject("source_schema_invalid")
    publications = [x for x in (signal.get("at"), feed_event.get("announced_at"), timeline_event.get("announced_at")) if x is not None]
    try:
        if not publications: return _reject("publication_time_invalid")
        announced_at = _time(publications[0])
        if any(_time(x) != announced_at for x in publications): return _reject("publication_time_conflict")
        target = _target(signal, feed_event, timeline_event)
    except ValueError:
        return _reject("target_invalid")
    if target is not None and not now < target <= now + MAX_TARGET_DISTANCE: return _reject("target_outside_window")
    content = " ".join((_text(signal), _text(feed_event), _text(timeline_event)))
    if _EXCLUDED.search(content): return _reject("excluded_signal", valid=True)
    lower = content.lower()
    if "reset" not in lower or ("codex" not in lower and "chatgpt work" not in lower): return _reject("reset_semantics_missing", valid=True)
    if _completed(feed_event) or _completed(timeline_event): return _reject("completed_reset")
    for events in (feed["events"], timeline["events"]):
        for item in events:
            if not isinstance(item, dict) or _post_id(item.get("id")) == post_id: continue
            if item.get("type") == item.get("group") == "reset" and item.get("scope") == "global" and _completed(item):
                try:
                    if _time(item.get("announced_at")) >= announced_at: return _reject("completed_reset")
                except ValueError: return _reject("source_schema_invalid")
    if forecast.get("last_reset_at") is not None:
        try:
            if _time(forecast["last_reset_at"]) >= announced_at: return _reject("completed_reset")
        except ValueError: return _reject("source_schema_invalid")
    material = alert_id or f"{post_id}|{target.isoformat() if target else ''}|{announced_at.date().isoformat()}"
    episode = Episode(sha256(material.encode()).hexdigest(), alert_id or post_id, float(score), str(signal.get("summary", ""))[:1000], str(signal["url"]), parsed["forecast"], parsed["feed"], target, announced_at)
    if score < THRESHOLD: return _reject("score_below_threshold", episode, valid=True)
    return SourceDecision(True, "eligible", episode, True)
