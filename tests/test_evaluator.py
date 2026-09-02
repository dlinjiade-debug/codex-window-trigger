from datetime import UTC, datetime
import unittest

from codex_window_trigger.evaluator import evaluate_sources, parse_utc
from tests.helpers import payloads


class EvaluateSourcesTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 31, 1, 5, tzinfo=UTC)

    def live_quiet_sources(self):
        """Observed model-mode quiet response with retained reset history."""
        forecast, feed, timeline = payloads()
        historical = dict(
            feed["events"][0],
            id="2094144275957350899",
            announced_at="2026-08-31T00:30:00Z",
            status="confirmed",
        )
        historical["url"] = f"https://x.com/thsottiaux/status/{historical['id']}"
        feed["events"] = [historical]
        timeline["events"] = [dict(historical)]
        forecast.update({
            "mode": "model",
            "official_signal": None,
            "alert_event_id": None,
            "signal_tier": None,
            "probabilities": {"signal_percent": None, "rounded_48h": 45},
            "latest_alert": {
                "id": historical["id"],
                "kind": "reset",
                "state": "confirmed",
                "source_at": "2026-08-31T00:30:00Z",
                "url": historical["url"],
            },
            "last_reset_at": "2026-08-31T00:30:00Z",
        })
        return forecast, feed, timeline

    def test_threshold_score_qualifies_with_a_valid_episode(self):
        decision = evaluate_sources(*payloads(score=30.0), now=self.now)
        self.assertTrue(decision.valid)
        self.assertTrue(decision.eligible)
        self.assertEqual(30.0, decision.episode.score)

    def test_live_shaped_decimal_string_post_id_qualifies(self):
        forecast, feed, timeline = payloads()
        self.assertIsInstance(forecast["official_signal"]["tweet_id"], str)
        self.assertIsInstance(feed["events"][0]["id"], str)
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.eligible)

    def test_below_threshold_is_valid_and_retains_episode(self):
        decision = evaluate_sources(*payloads(score=29.0), now=self.now)
        self.assertTrue(decision.valid)
        self.assertFalse(decision.eligible)
        self.assertEqual("score_below_threshold", decision.reason)
        self.assertEqual(29.0, decision.episode.score)

    def test_callers_cannot_lower_the_fixed_threshold(self):
        with self.assertRaises(TypeError):
            evaluate_sources(*payloads(score=29.0), now=self.now, threshold=0)

    def test_quiet_matching_sources_are_valid_but_ineligible(self):
        forecast, feed, timeline = payloads()
        forecast["official_signal"] = None
        feed["events"] = []
        timeline["events"] = []
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.valid)
        self.assertFalse(decision.eligible)
        self.assertIsNone(decision.episode)
        self.assertEqual("no_signal", decision.reason)

    def test_live_model_quiet_history_with_null_score_is_valid_without_cadence_fallback(self):
        forecast, feed, timeline = self.live_quiet_sources()
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.valid)
        self.assertFalse(decision.eligible)
        self.assertIsNone(decision.episode)
        self.assertEqual("no_signal", decision.reason)

    def test_live_quiet_shape_still_checks_feed_staleness(self):
        forecast, feed, timeline = self.live_quiet_sources()
        feed["stale"] = True
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("feed_stale", decision.reason)

    def test_live_quiet_rejects_active_forecast_fields(self):
        for field, value in (("alert_event_id", "active"), ("signal_tier", "likely")):
            with self.subTest(field=field):
                forecast, feed, timeline = self.live_quiet_sources()
                forecast[field] = value
                decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                self.assertFalse(decision.valid)
                self.assertEqual("source_schema_invalid", decision.reason)

    def test_live_quiet_requires_model_mode_and_explicit_null_score(self):
        forecast, feed, timeline = self.live_quiet_sources()
        forecast["mode"] = "announced"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)
        forecast, feed, timeline = self.live_quiet_sources()
        forecast["probabilities"] = {"rounded_48h": 45}
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_live_quiet_rejects_future_or_malformed_reset_history(self):
        forecast, feed, timeline = self.live_quiet_sources()
        forecast["last_reset_at"] = "2026-08-31T01:05:01Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_from_future", decision.reason)
        forecast, feed, timeline = self.live_quiet_sources()
        forecast["last_reset_at"] = "not-a-timestamp"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)
        forecast, feed, timeline = self.live_quiet_sources()
        feed["events"][0]["announced_at"] = "not-a-timestamp"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_live_quiet_rejects_an_active_or_unconfirmed_latest_alert(self):
        forecast, feed, timeline = self.live_quiet_sources()
        forecast["latest_alert"]["state"] = "pending"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("official_signal_missing", decision.reason)

    def test_live_quiet_accepts_pending_history_superseded_by_confirmed_global_reset(self):
        forecast, feed, timeline = self.live_quiet_sources()
        pending = dict(
            feed["events"][0],
            id="2094144275957350897",
            announced_at="2026-08-31T00:20:00Z",
            status="pending",
        )
        feed["events"].insert(0, pending)
        timeline["events"].insert(0, dict(pending))
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.valid)
        self.assertFalse(decision.eligible)
        self.assertEqual("no_signal", decision.reason)

    def test_live_quiet_rejects_unconfirmed_global_history_without_verified_boundary(self):
        for source in ("feed", "timeline"):
            with self.subTest(source=source):
                forecast, feed, timeline = self.live_quiet_sources()
                forecast.pop("latest_alert")
                forecast.pop("last_reset_at")
                {"feed": feed, "timeline": timeline}[source]["events"][0]["status"] = "pending"
                decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                self.assertFalse(decision.valid)
                self.assertEqual("source_schema_invalid", decision.reason)

    def test_live_quiet_does_not_extend_verified_boundary_with_last_reset_timestamp(self):
        forecast, feed, timeline = self.live_quiet_sources()
        forecast["last_reset_at"] = "2026-08-31T00:40:00Z"
        pending = dict(
            feed["events"][0],
            id="2094144275957350897",
            announced_at="2026-08-31T00:35:00Z",
            status="pending",
        )
        feed["events"].append(pending)
        timeline["events"].append(dict(pending))
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("completed_reset", decision.reason)

    def test_live_quiet_rejects_latest_alert_with_conflicting_associated_timestamps(self):
        forecast, feed, timeline = self.live_quiet_sources()
        feed["events"][0]["announced_at"] = "2026-08-31T00:29:00Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_live_quiet_rejects_timeline_only_latest_alert_timestamp_conflict(self):
        forecast, feed, timeline = self.live_quiet_sources()
        timeline["events"][0]["announced_at"] = "2026-08-31T00:29:00Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_live_quiet_rejects_newer_global_reset_evidence(self):
        forecast, feed, timeline = self.live_quiet_sources()
        newer = dict(feed["events"][0], id="2094144275957350898", announced_at="2026-08-31T00:31:00Z")
        feed["events"].append(newer)
        timeline["events"].append(dict(newer))
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("completed_reset", decision.reason)

    def test_positive_upcoming_episode_still_requires_numeric_score(self):
        forecast, feed, timeline = payloads(score=None)
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_exact_freshness_boundary_is_accepted(self):
        forecast, feed, timeline = payloads()
        forecast["updated_at"] = feed["fetched_at"] = "2026-08-31T00:55:00Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.valid)

    def test_optional_timeline_timestamp_is_checked_when_present(self):
        forecast, feed, timeline = payloads()
        timeline["updated_at"] = "2026-08-31T00:54:59Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_too_old", decision.reason)

    def test_future_timestamp_allows_exactly_sixty_seconds_only(self):
        forecast, feed, timeline = payloads()
        forecast["updated_at"] = feed["fetched_at"] = "2026-08-31T01:06:00Z"
        self.assertTrue(evaluate_sources(forecast, feed, timeline, now=self.now).valid)
        forecast["updated_at"] = "2026-08-31T01:06:01Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_from_future", decision.reason)

    def test_preview_true_is_a_valid_upcoming_reset(self):
        forecast, feed, timeline = payloads()
        feed["events"][0]["preview"] = True
        timeline["events"][0]["preview"] = True
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.eligible)

    def test_conflicting_feed_event_is_invalid(self):
        forecast, feed, timeline = payloads()
        timeline["events"][0]["scope"] = "account"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("event_conflict", decision.reason)

    def test_completed_reset_vetoes_forecast(self):
        forecast, feed, timeline = payloads()
        completed = dict(feed["events"][0], id=999, summary="Codex resets have now reset")
        completed["announced_at"] = "2026-08-31T01:01:00Z"
        feed["events"].append(completed)
        timeline["events"].append(dict(completed))
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("completed_reset", decision.reason)

    def test_reset_applied_wording_is_always_completed(self):
        for wording in (
            "Codex reset applied",
            "Codex reset was applied",
            "Codex reset has now been successfully applied",
        ):
            with self.subTest(wording=wording):
                forecast, feed, timeline = payloads()
                feed["events"][0]["summary"] = wording
                timeline["events"][0]["summary"] = wording
                decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                self.assertFalse(decision.eligible)
                self.assertEqual("completed_reset", decision.reason)

    def test_excluded_categories_are_ineligible(self):
        for word in ("banked", "credits", "referrals", "Juice", "incident"):
            with self.subTest(word=word):
                forecast, feed, timeline = payloads()
                forecast["official_signal"]["summary"] = f"Codex reset {word} available"
                decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                self.assertTrue(decision.valid)
                self.assertEqual("excluded_signal", decision.reason)

    def test_structured_exclusion_tags_normalize_identifier_separators(self):
        for category in ("banked", "credit", "referral", "juice", "incident"):
            for separator in ("_", "-"):
                with self.subTest(category=category, separator=separator):
                    forecast, feed, timeline = payloads()
                    forecast["official_signal"]["reason_tags"] = [f"{category}{separator}reset"]
                    decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                    self.assertTrue(decision.valid)
                    self.assertFalse(decision.eligible)
                    self.assertEqual("excluded_signal", decision.reason)

    def test_malformed_structured_tags_fail_closed(self):
        for field, value in (
            ("reason_tags", "global_reset"),
            ("tags", ["global_reset", 7]),
            ("reason_tags", {"kind": "reset"}),
            ("tags", None),
        ):
            with self.subTest(field=field, value=value):
                forecast, feed, timeline = payloads()
                forecast["official_signal"][field] = value
                decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                self.assertFalse(decision.valid)
                self.assertFalse(decision.eligible)
                self.assertEqual("source_schema_invalid", decision.reason)

    def test_malformed_quiet_history_tags_fail_closed_without_raising(self):
        forecast, feed, timeline = self.live_quiet_sources()
        timeline["events"][0]["tags"] = ["global_reset", 7]
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertFalse(decision.eligible)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_bad_url_and_bad_score_types_are_invalid(self):
        for score in ("30", True, float("nan"), float("inf"), -1, 101):
            with self.subTest(score=repr(score)):
                decision = evaluate_sources(*payloads(score=score), now=self.now)
                self.assertFalse(decision.valid)
                self.assertEqual("source_schema_invalid", decision.reason)
        forecast, feed, timeline = payloads()
        forecast["official_signal"]["url"] = "https://x.com/u/status/2094144275957350900?bad=1"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("signal_url_invalid", decision.reason)

    def test_malformed_url_authority_and_fragment_are_safe_rejections(self):
        for url in (
            "https://user@x.com/u/status/2094144275957350900",
            "https://x.com:bad/u/status/2094144275957350900",
            "https://x.com/u/status/2094144275957350900#later",
        ):
            with self.subTest(url=url):
                forecast, feed, timeline = payloads()
                forecast["official_signal"]["url"] = url
                decision = evaluate_sources(forecast, feed, timeline, now=self.now)
                self.assertFalse(decision.valid)
                self.assertEqual("signal_url_invalid", decision.reason)

    def test_missing_or_conflicting_publication_time_is_invalid(self):
        forecast, feed, timeline = payloads()
        forecast["official_signal"].pop("at")
        feed["events"][0].pop("announced_at")
        timeline["events"][0].pop("announced_at")
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("publication_time_invalid", decision.reason)
        forecast, feed, timeline = payloads()
        feed["events"][0]["announced_at"] = "2026-08-31T01:01:00Z"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("publication_time_conflict", decision.reason)

    def test_alert_id_conflict_and_bounds_are_invalid(self):
        forecast, feed, timeline = payloads()
        forecast["alert_event_id"] = "a-different-alert"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("alert_id_conflict", decision.reason)
        forecast, feed, timeline = payloads()
        forecast["alert_event_id"] = forecast["official_signal"]["alert_event_id"] = "x" * 257
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("source_schema_invalid", decision.reason)

    def test_fallback_identity_uses_post_target_and_publication_date(self):
        forecast, feed, timeline = payloads()
        forecast.pop("alert_event_id")
        forecast["official_signal"].pop("alert_event_id")
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertTrue(decision.eligible)
        self.assertEqual("112c682ace2e919e238c4fd6ae3b7ac613652ad9e5d19393dcda783fe0cc823e", decision.episode.key)

    def test_completed_state_is_not_hidden_by_pending_status(self):
        forecast, feed, timeline = payloads()
        feed["events"][0]["status"] = "pending"
        feed["events"][0]["state"] = "completed"
        timeline["events"][0]["status"] = "pending"
        timeline["events"][0]["state"] = "completed"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("completed_reset", decision.reason)

    def test_expired_named_verification_status_vetoes_reset(self):
        forecast, feed, timeline = payloads()
        feed["events"][0]["reset_verification_status"] = "expired"
        timeline["events"][0]["reset_verification_status"] = "expired"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("completed_reset", decision.reason)

    def test_confirmation_time_kind_vetoes_reset(self):
        forecast, feed, timeline = payloads()
        feed["events"][0]["time_kind"] = "confirmation"
        timeline["events"][0]["time_kind"] = "confirmation"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertFalse(decision.valid)
        self.assertEqual("completed_reset", decision.reason)

    def test_all_structured_targets_must_be_consistent_and_within_window(self):
        forecast, feed, timeline = payloads(target="2026-08-31T06:05:01Z")
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertEqual("target_outside_window", decision.reason)
        forecast, feed, timeline = payloads()
        feed["events"][0]["effective_at"] = "not-a-timestamp"
        timeline["events"][0]["effective_at"] = "not-a-timestamp"
        decision = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertEqual("target_invalid", decision.reason)

    def test_alert_identity_is_stable_when_non_identity_fields_change(self):
        forecast, feed, timeline = payloads()
        first = evaluate_sources(forecast, feed, timeline, now=self.now)
        forecast["probabilities"]["signal_percent"] = 85.0
        forecast["official_signal"]["summary"] = "Codex and ChatGPT Work reset expected soon."
        forecast["official_signal"]["target_at"] = "2026-08-31T04:00:00Z"
        second = evaluate_sources(forecast, feed, timeline, now=self.now)
        self.assertEqual("563920b58ca7ca37a1e0a80649ba7479c8cfa9f5ecfb9ff99a4259b6bbeee757", first.episode.key)
        self.assertEqual(first.episode.key, second.episode.key)


class ParseUtcTest(unittest.TestCase):
    def test_invalid_timestamp_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_utc("not-a-timestamp")
