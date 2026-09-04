from copy import deepcopy
from datetime import UTC, datetime, timedelta
import unittest

from codex_window_trigger.models import Episode
from codex_window_trigger.state import empty_state, plan_transition


T0 = datetime(2026, 8, 31, 1, 0, tzinfo=UTC)


def episode(key: str) -> Episode:
    return Episode(
        key, f"event-{key}", 30, "Codex reset", "https://example.invalid",
        T0, T0, None, T0,
    )


class PlanTransitionTest(unittest.TestCase):
    def test_all_pages_history_does_not_preempt_new_episode_after_reconciliation(self):
        old_prs = {f"old-{i}": T0 + timedelta(minutes=i) for i in range(101)}
        state = empty_state()
        state["initialized"] = True
        now = T0 + timedelta(hours=30)
        reconciled = plan_transition(state, None, now=now, existing_prs=old_prs)
        self.assertEqual("reconcile", reconciled.action)
        self.assertEqual((T0 + timedelta(minutes=100)).isoformat(), reconciled.next_state["last_triggered_at"])
        quiet = plan_transition(reconciled.next_state, None, now=now, existing_prs=old_prs)
        self.assertEqual("skip", quiet.action)
        self.assertEqual(reconciled.next_state, quiet.next_state)
        result = plan_transition(quiet.next_state, episode("new"), now=now, existing_prs=old_prs)
        self.assertEqual("trigger", result.action)
        self.assertTrue(set(old_prs).issubset(result.next_state["handled_keys"]))
        self.assertEqual(now.isoformat(), result.next_state["last_triggered_at"])
        self.assertEqual(100, len(result.next_state["episodes"]))

    def test_first_valid_quiet_observation_initializes_without_an_episode(self):
        result = plan_transition(empty_state(), None, eligible=False, now=T0, existing_prs={})

        self.assertEqual("baseline", result.action)
        self.assertTrue(result.next_state["initialized"])
        self.assertEqual({}, result.next_state["episodes"])

    def test_first_run_marks_identified_episode_as_baseline(self):
        result = plan_transition(empty_state(), episode("a"), now=T0, existing_prs={})

        self.assertEqual("baseline", result.action)
        self.assertEqual("baseline", result.next_state["episodes"]["a"]["status"])
        self.assertEqual(["a"], result.next_state["handled_keys"])

    def test_new_episode_plans_a_trigger_after_initialization(self):
        baseline = plan_transition(empty_state(), episode("a"), now=T0, existing_prs={})
        result = plan_transition(baseline.next_state, episode("b"), now=T0 + timedelta(hours=1), existing_prs={})

        self.assertEqual("trigger", result.action)
        self.assertEqual("triggered", result.next_state["episodes"]["b"]["status"])
        self.assertEqual((T0 + timedelta(hours=1)).isoformat(), result.next_state["last_triggered_at"])

    def test_pending_episode_retries_after_cooldown_ends(self):
        state = {
            "schema_version": 1,
            "initialized": True,
            "last_triggered_at": T0.isoformat(),
            "episodes": {"b": {"status": "pending", "event_id": "event-b", "at": T0.isoformat()}},
        }
        result = plan_transition(state, episode("b"), now=T0 + timedelta(hours=24), existing_prs={})

        self.assertEqual("trigger", result.action)
        self.assertEqual("triggered", result.next_state["episodes"]["b"]["status"])

    def test_confirmed_touch_reconciles_and_uses_its_real_time_for_cooldown(self):
        state = {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}}
        existing = {"prior": T0 + timedelta(hours=3)}

        result = plan_transition(state, episode("new"), now=T0 + timedelta(hours=4), existing_prs=existing)

        self.assertEqual("reconcile", result.action)
        self.assertEqual("triggered", result.next_state["episodes"]["prior"]["status"])
        self.assertEqual((T0 + timedelta(hours=3)).isoformat(), result.next_state["last_triggered_at"])
        self.assertEqual("pending", result.next_state["episodes"]["new"]["status"])

    def test_existing_confirmed_touch_reconciles_without_rewriting_its_timestamp(self):
        created_at = T0 + timedelta(hours=3)
        state = {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": {}}
        result = plan_transition(state, episode("b"), now=T0 + timedelta(hours=4), existing_prs={"b": created_at})

        self.assertEqual("reconcile", result.action)
        self.assertEqual(created_at.isoformat(), result.next_state["episodes"]["b"]["at"])

    def test_exactly_24_hours_after_existing_trigger_permits_a_new_episode(self):
        state = {"schema_version": 1, "initialized": True, "last_triggered_at": T0.isoformat(), "episodes": {}}
        result = plan_transition(state, episode("c"), now=T0 + timedelta(hours=24), existing_prs={})

        self.assertEqual("trigger", result.action)

    def test_comment_attempt_is_cleared_only_after_that_touch_is_confirmed(self):
        state = {"schema_version": 1, "initialized": True, "last_triggered_at": None,
                 "episodes": {}, "comment_attempts": {"a": T0.isoformat()}}

        unrelated = plan_transition(
            state, None, eligible=False, now=T0 + timedelta(minutes=1),
            existing_prs={"b": T0 + timedelta(seconds=10)})
        self.assertEqual({"a": T0.isoformat()}, unrelated.next_state["comment_attempts"])

        confirmed = plan_transition(
            unrelated.next_state, None, eligible=False, now=T0 + timedelta(minutes=2),
            existing_prs={"a": T0 + timedelta(seconds=20)})
        self.assertNotIn("comment_attempts", confirmed.next_state)
        self.assertEqual(T0 + timedelta(seconds=20),
                         datetime.fromisoformat(confirmed.next_state["last_triggered_at"]))

    def test_comment_attempt_state_is_bounded_and_validated(self):
        invalid_values = (
            [],
            {"": T0.isoformat()},
            {"a": "not-a-time"},
            {"a": T0.isoformat(), "b": T0.isoformat()},
        )
        for attempts in invalid_values:
            with self.subTest(attempts=attempts):
                state = {"schema_version": 1, "initialized": True,
                         "last_triggered_at": None, "episodes": {},
                         "comment_attempts": attempts}
                with self.assertRaises(ValueError):
                    plan_transition(state, None, eligible=False, now=T0, existing_prs={})

    def test_unconfirmed_comment_attempt_blocks_planning_another_trigger(self):
        state = {"schema_version": 1, "initialized": True, "last_triggered_at": None,
                 "episodes": {}, "comment_attempts": {"a": T0.isoformat()}}

        result = plan_transition(
            state, episode("b"), now=T0 + timedelta(hours=25), existing_prs={})

        self.assertEqual("skip", result.action)
        self.assertEqual("comment_attempt_pending", result.reason)
        self.assertNotIn("b", result.next_state["episodes"])

    def test_handled_keys_prevent_retrigger_after_episode_records_are_trimmed(self):
        episodes = {
            str(i): {"status": "triggered", "event_id": str(i), "at": (T0 + timedelta(minutes=i)).isoformat()}
            for i in range(100)
        }
        state = {"schema_version": 1, "initialized": True, "last_triggered_at": None, "episodes": episodes,
                 "handled_keys": sorted(episodes)}
        bounded = plan_transition(state, episode("new"), now=T0 + timedelta(hours=30), existing_prs={}).next_state
        result = plan_transition(bounded, episode("0"), now=T0 + timedelta(hours=31), existing_prs={})

        self.assertEqual(100, len(bounded["episodes"]))
        self.assertEqual("skip", result.action)
        self.assertEqual("episode_already_handled", result.reason)

    def test_invalid_state_and_naive_time_are_rejected(self):
        with self.assertRaises(ValueError):
            plan_transition({"schema_version": 2, "initialized": False, "last_triggered_at": None, "episodes": {}}, None, now=T0, existing_prs={})
        with self.assertRaises(ValueError):
            plan_transition(empty_state(), None, now=datetime(2026, 8, 31, 1, 0), existing_prs={})
        with self.assertRaises(ValueError):
            plan_transition(
                {"schema_version": 1, "initialized": True, "last_triggered_at": None,
                 "episodes": {"bad": {"status": "pending", "event_id": "event-bad", "at": None}}},
                None, now=T0, existing_prs={},
            )

    def test_every_non_optional_state_field_is_required_with_value_error(self):
        complete = empty_state()
        for field in ("schema_version", "initialized", "last_triggered_at", "episodes"):
            with self.subTest(field=field):
                state = dict(complete)
                state.pop(field)
                try:
                    plan_transition(state, None, now=T0, existing_prs={})
                except Exception as exc:  # The public validation exception type is the assertion.
                    observed = type(exc)
                else:
                    observed = None
                self.assertIs(ValueError, observed)

    def test_does_not_mutate_input_state(self):
        state = empty_state()
        before = deepcopy(state)
        plan_transition(state, episode("a"), now=T0, existing_prs={})

        self.assertEqual(before, state)
