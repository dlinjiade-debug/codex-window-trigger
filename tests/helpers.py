from copy import deepcopy


POST_ID = "2094144275957350900"
ALERT_ID = "signal:2094144275957350900:likely"
UPDATED = "2026-08-31T01:00:00Z"
TARGET = "2026-08-31T03:00:00Z"
SUMMARY = "Your Codex and ChatGPT Work reset will land soon."


def payloads(*, score: object = 30.0, target: str | None = TARGET) -> tuple[dict, dict, dict]:
    signal = {
        "tweet_id": POST_ID,
        "summary": SUMMARY,
        "url": f"https://x.com/thsottiaux/status/{POST_ID}",
        "alert_event_id": ALERT_ID,
        "at": UPDATED,
    }
    if target is not None:
        signal["target_at"] = target
    event = {
        "id": POST_ID,
        "type": "reset",
        "group": "reset",
        "scope": "global",
        "summary": SUMMARY,
        "url": f"https://x.com/thsottiaux/status/{POST_ID}",
        "announced_at": UPDATED,
    }
    forecast = {
        "updated_at": UPDATED,
        "probabilities": {"signal_percent": score},
        "alert_event_id": ALERT_ID,
        "official_signal": signal,
    }
    feed = {"source": "x-api", "stale": False, "fetched_at": UPDATED, "events": [event]}
    timeline = {"events": [deepcopy(event)]}
    return forecast, feed, timeline
