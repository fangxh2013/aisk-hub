#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish-pending retry and escalation state tests; no Git or network calls."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

KERNEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL))

from engine.worktree import publish_pending as pending  # noqa: E402


UTC = dt.timezone.utc
START = dt.datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


class FailureClassificationTests(unittest.TestCase):
    def test_known_failure_classes_and_unknown_fails_closed(self):
        cases = [
            ("network", "connection reset by peer", pending.TRANSIENT),
            (None, "! [rejected] fxh-dev -> fxh-dev (non-fast-forward)", pending.NON_FAST_FORWARD),
            (None, "Permission denied (publickey).", pending.PERMISSION),
            (None, "remote: Protected branch update failed; error: failed to push some refs",
             pending.PROTECTION),
            (None, "some unrecognized Git failure", pending.UNKNOWN),
        ]
        for kind, message, expected in cases:
            with self.subTest(kind=kind, message=message):
                self.assertEqual(pending.classify_failure(kind, message), expected)

    def test_explicit_classification_wins_over_generic_message(self):
        self.assertEqual(
            pending.classify_failure("non_fast_forward", "connection reset"),
            pending.NON_FAST_FORWARD,
        )


class PublishPendingTransitionTests(unittest.TestCase):
    def test_profile_cannot_disable_transient_only_retries_or_event_deduplication(self):
        for policy in (
            {"retry_only_transient_failures": False},
            {"notify_deduplication": False},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                pending.resolve_policy(policy)

    def test_profile_retry_policy_overrides_defaults(self):
        policy = pending.resolve_policy({
            "retry_delays_minutes": [2, 7],
            "needs_attention_after_minutes": 8,
            "needs_attention_after_attempts": 2,
            "blocked_after_minutes": 30,
            "retry_only_transient_failures": True,
            "notify_deduplication": "task_repo_remote_stage",
        })
        first = pending.record_failure(
            pending.new_state("T013/policy"), pending.TRANSIENT,
            "connection timeout", now=START, policy=policy,
        )
        self.assertEqual(first.state["next_retry_at"], "2026-09-23T10:02:00Z")

        second = pending.record_failure(
            first.state, pending.TRANSIENT, "connection timeout",
            now=START + dt.timedelta(minutes=2), policy=policy,
        )
        self.assertEqual(second.state["next_retry_at"], "2026-09-23T10:09:00Z")
        self.assertEqual(second.state["status"], pending.NEEDS_ATTENTION)
        self.assertEqual(second.events, ("needs_attention",))

        blocked = pending.advance_state(
            second.state, now=START + dt.timedelta(minutes=30), policy=policy,
        )
        self.assertEqual(blocked.state["status"], pending.BLOCKED)
        self.assertEqual(blocked.state["blocked_at"], "2026-09-23T10:30:00Z")
        self.assertEqual(blocked.state["next_retry_at"], "2026-09-23T11:30:00Z")

    def test_transient_retry_schedule_and_three_attempt_attention_threshold(self):
        state = pending.new_state("T014/backend")
        first = pending.record_failure(state, "transient", "connection reset", now=START)
        self.assertEqual(first.state["status"], pending.PUSH_PENDING)
        self.assertEqual(first.state["attempt_count"], 1)
        self.assertEqual(first.state["attempt_timestamps"], ["2026-09-23T10:00:00Z"])
        self.assertEqual(first.state["next_retry_at"], "2026-09-23T10:01:00Z")
        self.assertEqual(first.events, ())
        self.assertTrue(pending.should_retry(first.state, now=START + dt.timedelta(minutes=1)))

        second = pending.record_failure(
            first.state, "transient", "temporary transport error",
            now=START + dt.timedelta(minutes=1),
        )
        self.assertEqual(second.state["next_retry_at"], "2026-09-23T10:06:00Z")
        self.assertEqual(second.state["attempt_count"], 2)

        third = pending.record_failure(
            second.state, "transient", "temporary transport error",
            now=START + dt.timedelta(minutes=6),
        )
        self.assertEqual(third.state["status"], pending.NEEDS_ATTENTION)
        self.assertEqual(third.state["attempt_count"], 3)
        self.assertEqual(third.state["next_retry_at"], "2026-09-23T10:21:00Z")
        self.assertEqual(third.events, ("needs_attention",))
        self.assertFalse(pending.should_retry(third.state, now=START + dt.timedelta(minutes=20)))
        self.assertTrue(pending.should_retry(third.state, now=START + dt.timedelta(minutes=21)))

    def test_fifteen_minute_threshold_escalates_once(self):
        initial = pending.record_failure(
            pending.new_state("T015/frontend"), "transient", "connection timed out", now=START,
        )
        escalated = pending.advance_state(
            initial.state, now=START + dt.timedelta(minutes=15),
        )
        self.assertEqual(escalated.state["status"], pending.NEEDS_ATTENTION)
        self.assertEqual(escalated.state["needs_attention_at"], "2026-09-23T10:15:00Z")
        self.assertEqual(escalated.events, ("needs_attention",))

        repeated = pending.advance_state(
            escalated.state, now=START + dt.timedelta(minutes=20),
        )
        self.assertEqual(repeated.events, ())

    def test_non_retryable_and_unknown_failures_need_attention_immediately(self):
        for kind, message, expected_class in (
            (None, "non-fast-forward update rejected", pending.NON_FAST_FORWARD),
            (None, "permission denied", pending.PERMISSION),
            (None, "protected branch hook declined", pending.PROTECTION),
            (None, "unrecognized remote failure", pending.UNKNOWN),
        ):
            with self.subTest(message=message):
                transition = pending.record_failure(
                    pending.new_state("T016/backend"), kind, message, now=START,
                )
                self.assertEqual(transition.state["failure_class"], expected_class)
                self.assertEqual(transition.state["status"], pending.NEEDS_ATTENTION)
                self.assertIsNone(transition.state["next_retry_at"])
                self.assertEqual(transition.events, ("needs_attention",))
                self.assertFalse(pending.should_retry(transition.state, now=START + dt.timedelta(days=1)))

    def test_sixty_minute_blocked_escalation_is_deduplicated(self):
        initial = pending.record_failure(
            pending.new_state("T017/backend"), "transient", "connection reset", now=START,
        )
        self.assertFalse(pending.should_retry(
            initial.state, now=START + dt.timedelta(minutes=60),
        ))
        blocked = pending.advance_state(
            initial.state, now=START + dt.timedelta(minutes=60),
        )
        self.assertEqual(blocked.state["status"], pending.BLOCKED)
        self.assertEqual(blocked.state["needs_attention_at"], "2026-09-23T10:15:00Z")
        self.assertEqual(blocked.state["blocked_at"], "2026-09-23T11:00:00Z")
        self.assertEqual(blocked.state["next_retry_at"], "2026-09-23T12:00:00Z")
        self.assertEqual(blocked.events, ("blocked",))

        repeated = pending.advance_state(
            blocked.state, now=START + dt.timedelta(minutes=90),
        )
        self.assertEqual(repeated.events, ())
        self.assertFalse(pending.should_retry(
            repeated.state, now=START + dt.timedelta(minutes=119),
        ))
        self.assertTrue(pending.should_retry(
            repeated.state, now=START + dt.timedelta(minutes=120),
        ))
        retry_failed = pending.record_failure(
            repeated.state, pending.TRANSIENT, "connection reset",
            now=START + dt.timedelta(minutes=120),
        )
        self.assertEqual(retry_failed.state["status"], pending.BLOCKED)
        self.assertEqual(retry_failed.state["next_retry_at"], "2026-09-23T13:00:00Z")
        self.assertEqual(retry_failed.events, ())
        self.assertFalse(pending.should_retry(
            retry_failed.state, now=START + dt.timedelta(minutes=179),
        ))
        self.assertTrue(pending.should_retry(
            retry_failed.state, now=START + dt.timedelta(minutes=180),
        ))

    def test_deterministic_failures_never_enter_automatic_retry_cadence(self):
        attention = pending.record_failure(
            pending.new_state("T020/backend"), pending.PERMISSION,
            "permission denied", now=START,
        )
        blocked = pending.advance_state(
            attention.state, now=START + dt.timedelta(minutes=60),
        )
        self.assertEqual(blocked.state["status"], pending.BLOCKED)
        self.assertIsNone(blocked.state["next_retry_at"])
        self.assertFalse(pending.should_retry(
            blocked.state, now=START + dt.timedelta(days=10),
        ))

    def test_success_resolves_open_state_and_is_idempotent(self):
        attention = pending.record_failure(
            pending.new_state("T018/backend"), "protected_branch",
            "protected branch rejected the update", now=START,
        )
        resolved = pending.record_success(
            attention.state, now=START + dt.timedelta(minutes=2),
        )
        self.assertEqual(resolved.state["status"], pending.PUBLISHED)
        self.assertEqual(resolved.state["resolved_at"], "2026-09-23T10:02:00Z")
        self.assertIsNone(resolved.state["next_retry_at"])
        self.assertEqual(resolved.events, ("resolved",))
        self.assertFalse(pending.should_retry(resolved.state, now=START + dt.timedelta(days=1)))
        self.assertEqual(pending.record_success(resolved.state, now=START).events, ())


class PublishPendingPersistenceTests(unittest.TestCase):
    def test_state_and_escalation_dedupe_survive_store_reload(self):
        with tempfile.TemporaryDirectory(prefix="publish-pending-") as tmp:
            store = pending.PublishPendingStore(tmp)
            first = store.record_failure(
                "T019/backend", "transient", "connection timed out", now=START,
            )
            self.assertEqual(first.state["status"], pending.PUSH_PENDING)

            reloaded = pending.PublishPendingStore(tmp)
            persisted = reloaded.get("T019/backend")
            self.assertEqual(persisted["attempt_count"], 1)
            self.assertEqual(persisted["first_pending_at"], "2026-09-23T10:00:00Z")
            self.assertEqual(persisted["last_attempt_at"], "2026-09-23T10:00:00Z")
            self.assertEqual(persisted["next_retry_at"], "2026-09-23T10:01:00Z")

            attention = reloaded.advance(
                "T019/backend", now=START + dt.timedelta(minutes=15),
            )
            self.assertEqual(attention.events, ("needs_attention",))
            self.assertEqual(store.advance(
                "T019/backend", now=START + dt.timedelta(minutes=20),
            ).events, ())

            resolved = store.record_success(
                "T019/backend", now=START + dt.timedelta(minutes=21),
                remote_sha="c" * 40,
            )
            self.assertEqual(resolved.events, ("resolved",))
            self.assertEqual(reloaded.get("T019/backend")["status"], pending.PUBLISHED)
            self.assertEqual(reloaded.get("T019/backend")["remote_sha"], "c" * 40)


if __name__ == "__main__":
    unittest.main()
