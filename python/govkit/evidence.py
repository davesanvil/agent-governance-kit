"""Evidence-graded status reports.

A report answers one question: did every required check pass, as observed first-hand?

Rules:
- Each check has at most one claim per report. A required check with no claim is NOT_RUN: silence is not success.
- A claim of VERIFIED counts only with an evidence pointer, a stated method and first-hand observation.
  Anything weaker is downgraded to UNKNOWN, with the reason kept.
- Two claims for the same check are rejected. "It failed, then it passed" is two reports: rerun the check and
  report again. Silently letting the last claim win is how a failure turns green.
- Any observed failure makes the report RED, even for a check nobody required: an observed failure always counts.
  GREEN needs every required check VERIFIED. Everything else is INCOMPLETE, including an empty requirement list.
- Claims for checks nobody required are graded and shown, but only a failure among them changes the verdict.

Limits: evidence pointers are opaque strings. This module checks that they exist, not that they are true.
Pair it with something that can open the pointer (a log path, an artifact hash, a test run id).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    VERIFIED = "verified"  # observed directly, evidence attached
    FAILED = "failed"  # observed, and it is wrong
    NOT_RUN = "not_run"  # the check exists but did not execute
    UNKNOWN = "unknown"  # ran, but the outcome could not be established (timeout, ambiguous, weak evidence)


class Overall(StrEnum):
    GREEN = "green"
    RED = "red"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class Claim:
    check_id: str
    status: Status
    method: str  # how it was observed: "read-back", "test run", "probe"
    evidence: str | None = None  # pointer to raw output: log path, digest, run id
    firsthand: bool = True  # False when relayed from someone else's report


@dataclass(frozen=True)
class Result:
    check_id: str
    status: Status
    reason: str = ""  # why the status differs from what was claimed, if it does


@dataclass(frozen=True)
class Report:
    overall: Overall
    results: tuple[Result, ...]
    unrequired: tuple[Result, ...] = field(default=())  # graded claims for checks nobody required


class DuplicateClaim(ValueError):
    pass


def grade(claim: Claim) -> Result:
    """Turn one claim into what the evidence supports."""
    if claim.status is not Status.VERIFIED:
        return Result(claim.check_id, claim.status)
    missing = [
        why
        for why, bad in (
            ("no evidence pointer", not (claim.evidence or "").strip()),
            ("no method", not claim.method.strip()),
            ("second-hand report", not claim.firsthand),
        )
        if bad
    ]
    if missing:
        return Result(claim.check_id, Status.UNKNOWN, "claimed verified: " + ", ".join(missing))
    return Result(claim.check_id, Status.VERIFIED)


def rollup(required: Iterable[str], claims: Iterable[Claim]) -> Report:
    required = list(required)
    claims = list(claims)
    if len(set(required)) != len(required):
        raise ValueError("required check ids must be unique")
    dupes = sorted(cid for cid, n in Counter(c.check_id for c in claims).items() if n > 1)
    if dupes:
        raise DuplicateClaim(f"more than one claim for: {', '.join(dupes)}")

    by_id = {c.check_id: c for c in claims}
    results = tuple(
        grade(by_id[cid]) if cid in by_id else Result(cid, Status.NOT_RUN, "no claim")
        for cid in required
    )
    unrequired = tuple(grade(by_id[cid]) for cid in sorted(set(by_id) - set(required)))
    statuses = {r.status for r in results}
    if Status.FAILED in statuses or any(r.status is Status.FAILED for r in unrequired):
        overall = Overall.RED
    elif results and statuses == {Status.VERIFIED}:
        overall = Overall.GREEN
    else:
        overall = Overall.INCOMPLETE
    return Report(overall, results, unrequired)


def render(report: Report) -> str:
    """Plain text: the verdict first, then every required check grouped by status, then unrequired claims."""
    lines = [f"overall: {report.overall}"]
    for status in Status:
        group = [r for r in report.results if r.status is status]
        if group:
            items = ", ".join(r.check_id + (f" ({r.reason})" if r.reason else "") for r in group)
            lines.append(f"{status}: {items}")
    if report.unrequired:
        lines.append("not required: " + ", ".join(f"{r.check_id} ({r.status})" for r in report.unrequired))
    return "\n".join(lines)
