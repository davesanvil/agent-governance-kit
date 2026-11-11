"""Rollback-first changes to an external system.

A change is a small state machine, not a script:

- The executor reads the target, captures the preimage of every key it will touch and records its intent
  *before* the first write.
- If the live state no longer matches the state the change was reviewed against, it refuses and writes nothing.
- Every write is conditional on the version it last saw, so the preimage check cannot go stale between the
  read and the write. Another writer turns the next write into a conflict, not a silent overwrite.
- The target owns expiry. A change that nobody renews disappears on its own, even if the controller dies.
- The outcome is classified only by reading the target back. A timeout is UNKNOWN, and the only way out of
  UNKNOWN is reconcile(), which reads authoritative state. Retrying blind is how you apply something twice.
- Absence is not proof that nothing happened: a write can land, time out and expire before anyone looks.
  NOT_APPLIED (retryable) needs the target to confirm that none of the change's operations ever ran.
- PARTIAL is a state that waits for an owner's decision. Nothing retries or undoes it automatically.
- Rollback validates first and mutates second: it refuses from any state but APPLIED or PARTIAL, refuses if a
  touched key has changed since the change wrote it, and restores each preimage exactly, expiry included.
- Elapsed time is never a transition guard.

Limits of this reference: the ledger lives in memory (a real one is written durably before the first side
effect), there is one executor per change, and Target is a mock. Its version counter stands in for the
compare-and-swap or conditional write a real system must provide, and its operation receipts stand in for an
idempotency-key or operation-log lookup. Without such a lookup, treat unexplained absence as PARTIAL.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    PLANNED = "planned"
    INTENT_RECORDED = "intent_recorded"
    REFUSED_DRIFT = "refused_drift"  # live state did not match the reviewed state; nothing written
    APPLIED = "applied"  # every write read back
    NOT_APPLIED = "not_applied"  # the target confirms no operation ran; may be retried with the same change id
    PARTIAL = "partial"  # something landed but not all of it is in effect; an owner decides what happens next
    UNKNOWN = "unknown"  # outcome not established; reconcile() before anything else
    ROLLED_BACK = "rolled_back"  # every touched key read back equal to its preimage


# What is missing matters: no UNKNOWN -> apply, no PARTIAL -> apply, no timer-driven transition.
ALLOWED: Mapping[State, frozenset[State]] = {
    State.PLANNED: frozenset({State.INTENT_RECORDED}),
    State.INTENT_RECORDED: frozenset(
        {State.APPLIED, State.NOT_APPLIED, State.PARTIAL, State.UNKNOWN, State.REFUSED_DRIFT}
    ),
    State.UNKNOWN: frozenset({State.APPLIED, State.NOT_APPLIED, State.PARTIAL, State.ROLLED_BACK}),
    State.NOT_APPLIED: frozenset({State.INTENT_RECORDED}),
    State.PARTIAL: frozenset({State.ROLLED_BACK, State.UNKNOWN}),
    State.APPLIED: frozenset({State.ROLLED_BACK, State.UNKNOWN}),
    State.REFUSED_DRIFT: frozenset(),  # re-plan against the new state
    State.ROLLED_BACK: frozenset(),
}


@dataclass(frozen=True)
class Item:
    value: str
    expires_at: float


def state_digest(items: Mapping[str, Item]) -> str:
    """Digest of a target state. Expiry is part of the state: same value, different expiry is a different world."""
    canon = sorted((k, v.value, v.expires_at) for k, v in items.items())
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()


# ---------------------------------------------------------------- mock target


class Refused(Exception):
    """The target said no (quota, permission). Definite: the write did not land."""


class Conflict(Exception):
    """The target moved since we read it. Definite: the write did not land."""


@dataclass
class Target:
    """Mock external system: versioned conditional writes, idempotent per operation id, owns expiry."""

    items: dict[str, Item] = field(default_factory=dict)
    refuse_after: int | None = None  # refuse every write once this many have landed
    hang_after_write: bool = False  # land the write, then time out
    version: int = 0
    writes: int = 0
    _receipts: dict[str, tuple[str, Item | None]] = field(default_factory=dict)  # op id -> what it did

    def read(self, now: float) -> tuple[int, dict[str, Item]]:
        return self.version, {k: v for k, v in self.items.items() if v.expires_at > now}

    def landed(self, op_id: str) -> bool:
        """Did this operation ever take effect? (Even if its result has since expired or been overwritten.)"""
        return op_id in self._receipts

    def write(self, key: str, item: Item | None, *, if_version: int, op_id: str) -> int:
        """Set (item) or delete (None) one key. Returns the new version."""
        if op_id in self._receipts:
            if self._receipts[op_id] != (key, item):
                raise ValueError(f"operation id {op_id} reused for a different write")
            return self.version  # replay of an operation that already landed: no-op
        if if_version != self.version:
            raise Conflict(f"expected version {if_version}, target is at {self.version}")
        if self.refuse_after is not None and self.writes >= self.refuse_after:
            raise Refused("write refused by target")
        if item is None:
            self.items.pop(key, None)
        else:
            self.items[key] = item
        self.version += 1
        self.writes += 1
        self._receipts[op_id] = (key, item)
        if self.hang_after_write:
            raise TimeoutError("no response; the write may or may not have landed")
        return self.version

    def external_write(self, key: str, item: Item | None) -> None:
        """Someone else changes the target (for tests)."""
        self.write(key, item, if_version=self.version, op_id=f"external/{self.version}")


# ---------------------------------------------------------------- executor


@dataclass(frozen=True)
class Change:
    change_id: str  # idempotency key
    writes: tuple[tuple[str, str], ...]  # (key, value)
    reviewed_digest: str  # state_digest of the target the change was reviewed against
    ttl_s: float


@dataclass(frozen=True)
class Entry:
    change_id: str
    event: str
    state: State
    detail: str = ""


@dataclass(frozen=True)
class Intent:
    preimage: Mapping[str, Item | None]  # touched key -> what was there before (None: absent)
    written: Mapping[str, Item]  # touched key -> exactly what this change writes
    phase: str  # "apply" or "rollback": what the last attempted writes were for


class IllegalTransition(RuntimeError):
    pass


class RollbackRefused(RuntimeError):
    """Rollback was refused before any write."""


class Executor:
    def __init__(self, target: Target) -> None:
        self._t = target
        self._ledger: list[Entry] = []
        self._state: dict[str, State] = {}
        self._intent: dict[str, Intent] = {}

    @property
    def history(self) -> tuple[Entry, ...]:
        return tuple(self._ledger)

    def state(self, change_id: str) -> State:
        return self._state.get(change_id, State.PLANNED)

    def _move(self, ch: Change, new: State, event: str, detail: str = "") -> State:
        old = self.state(ch.change_id)
        if new not in ALLOWED[old]:
            raise IllegalTransition(f"{ch.change_id}: {old} -> {new}")
        self._state[ch.change_id] = new
        self._ledger.append(Entry(ch.change_id, event, new, detail))
        return new

    def _note(self, ch: Change, event: str, detail: str) -> None:
        self._ledger.append(Entry(ch.change_id, event, self.state(ch.change_id), detail))

    @staticmethod
    def _op(ch: Change, phase: str, key: str) -> str:
        """Operation ids are stable per change, phase and key, so a resumed attempt reuses the right id.
        JSON-encoded so that no two (change, phase, key) triples can produce the same id."""
        return json.dumps([ch.change_id, phase, key])

    # -- apply

    def apply(self, ch: Change, now: float) -> State:
        keys = [k for k, _ in ch.writes]
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("a change writes at least one key, each at most once")
        deadline = now + ch.ttl_s
        if not (math.isfinite(now) and ch.ttl_s > 0 and math.isfinite(deadline) and deadline > now):
            raise ValueError("now must be finite, ttl_s positive, and now + ttl_s a finite later time")
        state = self.state(ch.change_id)
        if State.INTENT_RECORDED not in ALLOWED[state]:  # check before touching any record
            raise IllegalTransition(f"{ch.change_id}: cannot apply from {state}")
        version, live = self._t.read(now)
        observed = state_digest(live)
        written = {k: Item(v, deadline) for k, v in ch.writes}
        self._intent[ch.change_id] = Intent({k: live.get(k) for k in keys}, written, "apply")
        self._move(ch, State.INTENT_RECORDED, "intent", f"observed {observed[:12]}")  # before any write
        if observed != ch.reviewed_digest:
            return self._move(ch, State.REFUSED_DRIFT, "drift", "live state differs from the reviewed state")
        try:
            for k, item in written.items():
                version = self._t.write(k, item, if_version=version, op_id=self._op(ch, "apply", k))
        except TimeoutError as e:
            return self._move(ch, State.UNKNOWN, "timeout", str(e))
        except (Refused, Conflict) as e:
            # Stop at the first failed write: no retry, no automatic undo. Classify by reading back.
            self._note(ch, "write-stopped", f"{type(e).__name__}: {e}")
        return self._classify(ch, now, via="apply")

    # -- reconcile

    def reconcile(self, ch: Change, now: float) -> State:
        """Read authoritative state and classify. The only way out of UNKNOWN."""
        if self.state(ch.change_id) is not State.UNKNOWN:
            raise IllegalTransition(f"{ch.change_id}: reconcile is for UNKNOWN, state is {self.state(ch.change_id)}")
        return self._classify(ch, now, via="reconcile")

    def _classify(self, ch: Change, now: float, via: str) -> State:
        intent = self._intent[ch.change_id]
        _, live = self._t.read(now)
        ours = sum(1 for k, item in intent.written.items() if live.get(k) == item)
        if intent.phase == "rollback" and all(live.get(k) == self._original(intent, k, now) for k in intent.written):
            new = State.ROLLED_BACK
        elif ours == len(intent.written):
            new = State.APPLIED
        elif intent.phase == "apply" and not any(self._t.landed(self._op(ch, "apply", k)) for k in intent.written):
            new = State.NOT_APPLIED  # authoritative: none of this change's operations ever ran
        else:
            new = State.PARTIAL  # some landed; some are refused, overwritten or already expired
        return self._move(ch, new, via, f"{ours}/{len(intent.written)} writes present")

    @staticmethod
    def _original(intent: Intent, key: str, now: float) -> Item | None:
        """What the key should hold after rollback: its preimage, unless that has since expired on its own."""
        pre = intent.preimage[key]
        return pre if pre is not None and pre.expires_at > now else None

    # -- rollback

    def rollback(self, ch: Change, now: float) -> State:
        """Validate everything, then restore every touched key to its exact preimage."""
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        state = self.state(ch.change_id)
        if state not in (State.APPLIED, State.PARTIAL):
            raise RollbackRefused(f"{ch.change_id}: cannot roll back from {state}")
        intent = self._intent[ch.change_id]
        version, live = self._t.read(now)
        plan: list[tuple[str, Item | None]] = []
        for k, ours in intent.written.items():
            original = self._original(intent, k, now)
            ours_now = ours if ours.expires_at > now else None  # our write may have expired on its own
            if live.get(k) not in (ours_now, original):
                self._note(ch, "rollback-refused", f"key {k} changed since this change wrote it")
                raise RollbackRefused(f"{ch.change_id}: key {k} changed since this change wrote it")
            if live.get(k) != original:
                plan.append((k, original))
        self._intent[ch.change_id] = Intent(intent.preimage, intent.written, "rollback")
        self._note(ch, "rollback-intent", f"{len(plan)} key(s) to restore")
        try:
            for k, item in plan:
                version = self._t.write(k, item, if_version=version, op_id=self._op(ch, "rollback", k))
        except TimeoutError as e:
            return self._move(ch, State.UNKNOWN, "rollback-timeout", str(e))
        except (Refused, Conflict) as e:
            self._move(ch, State.UNKNOWN, "rollback-stopped", f"{type(e).__name__}: {e}")
            return self._classify(ch, now, via="reconcile")
        self._move(ch, State.UNKNOWN, "rollback-written")
        return self._classify(ch, now, via="rollback")
