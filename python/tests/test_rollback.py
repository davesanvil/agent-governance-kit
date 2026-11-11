import unittest

from govkit.rollback import (
    Change,
    Executor,
    IllegalTransition,
    Item,
    RollbackRefused,
    State,
    Target,
    state_digest,
)

NOW = 1000.0


def change_for(target: Target, *writes: tuple[str, str], ttl_s: float = 60, change_id: str = "c1") -> Change:
    """A change reviewed against the target as it is right now."""
    _, live = target.read(NOW)
    return Change(change_id, tuple(writes), state_digest(live), ttl_s)


class ApplyTest(unittest.TestCase):
    def test_apply_then_verified_rollback(self) -> None:
        t = Target()
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ("b", "2"))
        self.assertIs(ex.apply(ch, NOW), State.APPLIED)
        self.assertIs(ex.rollback(ch, NOW), State.ROLLED_BACK)
        self.assertEqual(t.read(NOW)[1], {})

    def test_intent_is_recorded_before_any_write(self) -> None:
        t = Target()
        ex = Executor(t)
        ex.apply(change_for(t, ("a", "1")), NOW)
        self.assertEqual(ex.history[0].event, "intent")

    def test_drift_refuses_and_writes_nothing(self) -> None:
        t = Target()
        ch = change_for(t, ("a", "1"))
        t.external_write("x", Item("0", 10_000))  # the world moved after review
        writes_before = t.writes
        self.assertIs(Executor(t).apply(ch, NOW), State.REFUSED_DRIFT)
        self.assertEqual(t.writes, writes_before)

    def test_refusal_mid_change_is_partial_and_not_retried(self) -> None:
        t = Target(refuse_after=2)
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ("b", "2"), ("c", "3"))
        self.assertIs(ex.apply(ch, NOW), State.PARTIAL)
        self.assertEqual(t.writes, 2)
        with self.assertRaises(IllegalTransition):
            ex.apply(ch, NOW)  # PARTIAL waits for an owner; it is never re-applied

    def test_refusal_on_first_write_is_not_applied_and_retryable(self) -> None:
        t = Target(refuse_after=0)
        ex = Executor(t)
        ch = change_for(t, ("a", "1"))
        self.assertIs(ex.apply(ch, NOW), State.NOT_APPLIED)
        t.refuse_after = None
        self.assertIs(ex.apply(ch, NOW), State.APPLIED)

    def test_another_writer_mid_change_is_a_conflict_not_an_overwrite(self) -> None:
        class RacingTarget(Target):
            def write(self, key: str, item: Item | None, *, if_version: int, op_id: str) -> int:
                version = super().write(key, item, if_version=if_version, op_id=op_id)
                if key == "a" and item == Item("1", NOW + 60):  # right after our first write
                    self.external_write("b", Item("theirs", 10_000))
                return version

        t = RacingTarget()
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ("b", "2"))
        self.assertIs(ex.apply(ch, NOW), State.PARTIAL)
        self.assertEqual(t.read(NOW)[1]["b"].value, "theirs")

    def test_malformed_changes_rejected_before_anything_is_recorded(self) -> None:
        t = Target()
        for writes, ttl in (((("a", "1"), ("a", "2")), 60.0), ((), 60.0), ((("a", "1"),), float("inf")),
                            ((("a", "1"),), float("nan")), ((("a", "1"),), 0.0), ((("a", "1"),), -5.0),
                            ((("a", "1"),), 1e308)):  # last: finite TTL whose deadline overflows
            ex = Executor(t)
            now = 1e308 if ttl == 1e308 else NOW
            with self.subTest(writes=writes, ttl=ttl), self.assertRaises(ValueError):
                ex.apply(Change("c", writes, state_digest({}), ttl), now)
            self.assertEqual(ex.history, ())
        self.assertEqual(t.writes, 0)

    def test_rejected_reapply_keeps_the_original_preimage(self) -> None:
        # A rejected apply() must leave the recorded preimage untouched.
        t = Target(items={"a": Item("old-a", 10_000), "b": Item("old-b", 10_000)})
        ex = Executor(t)
        ch = change_for(t, ("a", "new-a"), ("b", "new-b"))
        ex.apply(ch, NOW)
        with self.assertRaises(IllegalTransition):
            ex.apply(ch, NOW + 1)
        self.assertIs(ex.rollback(ch, NOW + 2), State.ROLLED_BACK)
        self.assertEqual(t.items, {"a": Item("old-a", 10_000), "b": Item("old-b", 10_000)})


class UnknownTest(unittest.TestCase):
    def test_timeout_is_unknown_and_only_reconcile_leaves_it(self) -> None:
        t = Target(hang_after_write=True)
        ex = Executor(t)
        ch = change_for(t, ("a", "1"))
        self.assertIs(ex.apply(ch, NOW), State.UNKNOWN)
        with self.assertRaises(IllegalTransition):
            ex.apply(ch, NOW)  # a blind retry would apply twice
        self.assertIs(ex.reconcile(ch, NOW), State.APPLIED)
        self.assertEqual(t.writes, 1)

    def test_reconcile_outside_unknown_is_illegal(self) -> None:
        t = Target()
        ex = Executor(t)
        ch = change_for(t, ("a", "1"))
        ex.apply(ch, NOW)
        with self.assertRaises(IllegalTransition):
            ex.reconcile(ch, NOW)

    def test_rejected_apply_while_unknown_keeps_the_preimage(self) -> None:
        t = Target(items={"a": Item("old", 10_000)}, hang_after_write=True)
        ex = Executor(t)
        ch = change_for(t, ("a", "1"))
        ex.apply(ch, NOW)
        with self.assertRaises(IllegalTransition):
            ex.apply(ch, NOW + 1)
        self.assertIs(ex.reconcile(ch, NOW + 1), State.APPLIED)
        t.hang_after_write = False
        self.assertIs(ex.rollback(ch, NOW + 2), State.ROLLED_BACK)
        self.assertEqual(t.items["a"], Item("old", 10_000))

    def test_landed_then_expired_is_partial_not_retryable(self) -> None:
        # Absence after expiry is not "never ran": a retry here would be silently ignored by the target.
        t = Target(hang_after_write=True)
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ttl_s=60)
        self.assertIs(ex.apply(ch, NOW), State.UNKNOWN)
        self.assertIs(ex.reconcile(ch, NOW + 120), State.PARTIAL)
        with self.assertRaises(IllegalTransition):
            ex.apply(ch, NOW + 120)
        t.hang_after_write = False
        self.assertIs(ex.rollback(ch, NOW + 120), State.ROLLED_BACK)

    def test_replayed_operation_id_is_a_no_op(self) -> None:
        t = Target()
        v = t.write("a", Item("1", 2000), if_version=0, op_id="op-1")
        self.assertEqual(t.write("a", Item("1", 2000), if_version=0, op_id="op-1"), v)
        self.assertEqual(t.writes, 1)

    def test_operation_ids_never_collide_across_changes(self) -> None:
        # Naive "change/phase/key" ids would make these two distinct changes share one id.
        t = Target()
        first = change_for(t, ("a/apply/b", "one"), change_id="c")
        ex = Executor(t)
        self.assertIs(ex.apply(first, NOW), State.APPLIED)
        second = change_for(t, ("b", "two"), change_id="c/apply/a")
        self.assertIs(ex.apply(second, NOW), State.APPLIED)
        self.assertIs(ex.rollback(second, NOW), State.ROLLED_BACK)
        self.assertIs(ex.rollback(first, NOW), State.ROLLED_BACK)
        self.assertEqual(t.read(NOW)[1], {})

    def test_operation_id_reused_for_a_different_write_is_rejected(self) -> None:
        t = Target()
        t.write("a", Item("1", 2000), if_version=0, op_id="op-1")
        with self.assertRaises(ValueError):
            t.write("b", Item("1", 2000), if_version=1, op_id="op-1")


class ExpiryTest(unittest.TestCase):
    def test_target_owns_expiry(self) -> None:
        t = Target()
        Executor(t).apply(change_for(t, ("a", "1"), ttl_s=60), NOW)
        self.assertEqual(t.read(NOW + 61)[1], {})

    def test_digest_includes_expiry(self) -> None:
        # Hashing values alone would let a materially different state pass as "restored".
        self.assertNotEqual(state_digest({"k": Item("v", 1100)}), state_digest({"k": Item("v", 2000)}))


class RollbackTest(unittest.TestCase):
    def test_rollback_restores_original_expiry_exactly(self) -> None:
        # Restoring the value with a fresh expiry is not restoring the original.
        t = Target(items={"k": Item("old", 1100)})
        ex = Executor(t)
        ch = change_for(t, ("k", "new"))
        ex.apply(ch, NOW)
        self.assertIs(ex.rollback(ch, NOW), State.ROLLED_BACK)
        self.assertEqual(t.items["k"], Item("old", 1100))

    def test_rollback_before_apply_refuses_and_touches_nothing(self) -> None:
        # Rollback validates before it writes: nothing to undo means nothing is touched.
        t = Target(items={"k": Item("live", 10_000)})
        ch = change_for(t, ("k", "new"))
        with self.assertRaises(RollbackRefused):
            Executor(t).rollback(ch, NOW)
        self.assertEqual(t.items, {"k": Item("live", 10_000)})
        self.assertEqual(t.writes, 0)

    def test_rollback_after_drift_refuses_and_touches_nothing(self) -> None:
        t = Target(items={"k": Item("old", 10_000)})
        ex = Executor(t)
        ch = change_for(t, ("k", "new"))
        ex.apply(ch, NOW)
        t.external_write("k", Item("someone-else", 10_000))
        writes = t.writes
        with self.assertRaises(RollbackRefused):
            ex.rollback(ch, NOW)
        self.assertEqual(t.items["k"].value, "someone-else")
        self.assertEqual(t.writes, writes)
        self.assertIs(ex.state(ch.change_id), State.APPLIED)
        self.assertEqual(ex.history[-1].event, "rollback-refused")

    def test_rollback_from_partial_restores_only_what_landed(self) -> None:
        t = Target(items={"a": Item("old-a", 10_000)}, refuse_after=1)
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ("b", "2"))
        self.assertIs(ex.apply(ch, NOW), State.PARTIAL)
        t.refuse_after = None
        self.assertIs(ex.rollback(ch, NOW), State.ROLLED_BACK)
        self.assertEqual(t.read(NOW)[1], {"a": Item("old-a", 10_000)})

    def test_rollback_twice_is_refused(self) -> None:
        t = Target()
        ex = Executor(t)
        ch = change_for(t, ("a", "1"))
        ex.apply(ch, NOW)
        ex.rollback(ch, NOW)
        with self.assertRaises(RollbackRefused):
            ex.rollback(ch, NOW)

    def test_rollback_timeout_is_unknown_then_reconciled(self) -> None:
        t = Target(items={"a": Item("old", 10_000)})
        ex = Executor(t)
        ch = change_for(t, ("a", "1"))
        ex.apply(ch, NOW)
        t.hang_after_write = True
        self.assertIs(ex.rollback(ch, NOW), State.UNKNOWN)
        self.assertIs(ex.reconcile(ch, NOW), State.ROLLED_BACK)

    def test_rollback_refused_midway_then_resumed(self) -> None:
        # A resumed rollback must not reuse an operation id that belonged to another key.
        t = Target(items={"a": Item("old-a", 10_000), "b": Item("old-b", 10_000)})
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ("b", "2"))
        ex.apply(ch, NOW)
        t.refuse_after = t.writes + 1  # first undo lands, second is refused
        self.assertIs(ex.rollback(ch, NOW), State.PARTIAL)
        t.refuse_after = None
        self.assertIs(ex.rollback(ch, NOW), State.ROLLED_BACK)
        self.assertEqual(t.items, {"a": Item("old-a", 10_000), "b": Item("old-b", 10_000)})

    def test_rollback_timeout_midway_then_reconciled_and_resumed(self) -> None:
        class HangOnce(Target):
            def write(self, key: str, item: Item | None, *, if_version: int, op_id: str) -> int:
                version = super().write(key, item, if_version=if_version, op_id=op_id)
                if key == "a" and item == Item("old-a", 10_000):  # the first undo lands, then hangs
                    raise TimeoutError("no response")
                return version

        t = HangOnce(items={"a": Item("old-a", 10_000), "b": Item("old-b", 10_000)})
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ("b", "2"))
        ex.apply(ch, NOW)
        self.assertIs(ex.rollback(ch, NOW), State.UNKNOWN)
        self.assertIs(ex.reconcile(ch, NOW), State.PARTIAL)
        self.assertIs(ex.rollback(ch, NOW), State.ROLLED_BACK)
        self.assertEqual(t.items, {"a": Item("old-a", 10_000), "b": Item("old-b", 10_000)})

    def test_rollback_after_our_write_expired(self) -> None:
        t = Target()
        ex = Executor(t)
        ch = change_for(t, ("a", "1"), ttl_s=60)
        ex.apply(ch, NOW)
        self.assertIs(ex.rollback(ch, NOW + 120), State.ROLLED_BACK)


if __name__ == "__main__":
    unittest.main()
