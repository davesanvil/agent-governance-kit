# agent-governance-kit

Three small guardrails for AI agents that make real changes, written as typed reference code with tests.

This is not a framework and there is nothing to install. Each pattern is one short file you can read in a
few minutes. Copy the idea, not the code.

## The problem

Getting an agent to do the work is the easy part. The hard part is knowing when to believe it. Once an agent
can change a config, merge code or call an API with side effects, the same three failures keep showing up:

1. **Review approves the wrong thing.** The reviewer reads a description or a branch that keeps moving, says
   "looks good", and that sentence becomes the approval. Often the reviewer is the same model that wrote the
   change, with the same blind spots.
2. **Reports turn silence into success.** A check that never ran drops out of the summary. "Verified" gets
   repeated from someone else's message. One failure is followed by one pass, and the pass is what gets
   reported.
3. **Errors become retries.** A timeout on a write is treated as a failure, so the agent tries again and
   applies the change twice. Or it "fixes" a value someone else changed in the meantime. Or its undo step
   puts back something close to the original, not the original.

Each pattern below moves one of these from "the prompt tells the model to be careful" into code that can be
tested.

## The patterns

| Pattern | Language | Code | Tests |
|---|---|---|---|
| Review gate over a frozen artifact | TypeScript | [`typescript/src/review-gate.ts`](typescript/src/review-gate.ts) | [`review-gate.test.ts`](typescript/test/review-gate.test.ts) |
| Evidence-graded reports | Python | [`python/govkit/evidence.py`](python/govkit/evidence.py) | [`test_evidence.py`](python/tests/test_evidence.py) |
| Rollback-first changes | Python | [`python/govkit/rollback.py`](python/govkit/rollback.py) | [`test_rollback.py`](python/tests/test_rollback.py) |

### 1. Review gate over a frozen artifact

One owner builds the change and writes a handoff before starting: objective, acceptance criteria, write
scope, the checks that must pass, and who gets the result. The candidate is frozen into a digest (`freeze()`
hashes paths and contents). A reviewer from a different model family reviews that digest and returns a
verdict, findings and open uncertainties.

The gate does not take the verdict on trust. It recomputes acceptance:

- every check the handoff named passed, against this exact digest (a pass on an older build counts for
  nothing, and conflicting results for one check block);
- no failing result for this digest, even from a check nobody required;
- no blocking or important finding and no open uncertainty, whatever the verdict says;
- the reviewer is not the owner or a contributor and is from a different model family;
- a fix is rechecked by the reviewer who asked for it.

Any edit changes the digest, which voids every earlier check and review. After three rounds without
acceptance it stops and hands the decision to a person. `insufficient_evidence` is a first-class verdict, so a
reviewer with nothing to run can say so instead of approving.

### 2. Evidence-graded reports

Every claim about a check carries a status (`verified`, `failed`, `not_run`, `unknown`), a method and a
pointer to the evidence. The rollup is strict:

- a required check with no claim is `not_run`: a missing check is not a pass;
- `verified` with no evidence, no method, or relayed second-hand is downgraded to `unknown`, and the reason
  is kept in the output;
- two claims for the same check are rejected. If a check failed and then passed, that is two runs: report
  the second run on its own. Letting the last claim win is how a failure turns green;
- any observed failure makes the report red, even for a check nobody required; green needs every required
  check verified first-hand; an empty requirement list is never green.

`render()` prints every required check grouped by status, then any unrequired claims with their status, so
nothing quietly drops out of the summary.

### 3. Rollback-first changes

A change to an external system runs as a small state machine:

```
PLANNED -> INTENT_RECORDED -> APPLIED | NOT_APPLIED | PARTIAL | UNKNOWN | REFUSED_DRIFT
UNKNOWN -> (reconcile only) APPLIED | NOT_APPLIED | PARTIAL | ROLLED_BACK
APPLIED, PARTIAL -> ROLLED_BACK
```

- Intent, including the exact preimage of every key it will touch, is recorded before the first write.
- If the live state does not match the state the change was reviewed against, it refuses and writes nothing.
- Every write is conditional on the version it last saw, so another writer causes a conflict, not a silent
  overwrite.
- The target owns expiry. A change nobody renews goes away on its own, even if the controller dies.
- The outcome is decided by reading the target back. A timeout is `UNKNOWN`, and the only way out of
  `UNKNOWN` is `reconcile()`. There is no retry from `UNKNOWN` or `PARTIAL`.
- Absence is not proof that nothing happened: a write can land, time out and expire before anyone looks.
  `NOT_APPLIED`, the only retryable failure, needs the target to confirm that none of the change's
  operations ever ran. Anything else that is not fully in effect is `PARTIAL`.
- Operation ids are stable per change and key, and the target rejects an id reused for a different write,
  so a resumed apply or rollback can never be mistaken for one that already happened.
- Rollback checks before it touches anything: it refuses from any state but `APPLIED` or `PARTIAL`, refuses
  if a key changed since this change wrote it, and restores each preimage exactly, expiry included.
- Time passing is never a reason to move to another state.

The target in this file is a mock. Its version counter stands in for the compare-and-swap or conditional
write a real system has to provide, and its operation receipts stand in for an idempotency-key or
operation-log lookup. If the real system cannot answer "did operation X ever run?", treat unexplained
absence as `PARTIAL`.

## Trade-offs

- **The gate trusts its inputs.** Digests, model families and reviewer names come from whoever packaged the
  candidate. That is fine when packaging is a trusted step; it is not identity attestation. Different model
  families cut one kind of correlated error. They do not prove two reviews are independent.
- **Review is not testing, and testing is not production.** A gate can only judge the checks it is given.
  Something none of them exercise, like a quota on the real system, gets through.
- **Strict reports cost ceremony.** Rejecting duplicate claims means a rerun has to be reported as a new
  report. I think that is the right price for never seeing a failure overwritten by a pass. The evidence
  pointer is checked for presence, not truth; pair it with something that can open it.
- **Rollback-first is slower, and it can lock itself out.** A change pinned to a preimage refuses to run once
  the world has moved, including its own rollback. That is the point, but it means you need a documented
  manual path for the day it refuses.
- **No automatic recovery from `PARTIAL`.** A person or owning process chooses to roll back or forward. Agents
  that stop and ask are more useful than agents that retry with confidence.
- **This is reference code.** The ledger is in memory, there is one executor per change, and nothing here is
  crash-safe. A real version writes intent to durable storage before the first side effect.

## Run it

Python 3.11+ (standard library only; mypy for type checks):

```sh
cd python
python3 -m unittest discover -s tests
python3 -m pip install mypy && mypy govkit tests
```

Node.js 22.18+ (runs the TypeScript directly; `tsc` only type-checks):

```sh
cd typescript
npm ci --ignore-scripts
npm run typecheck
npm test
```

## Repository hygiene

`tools/hygiene_check.py` runs in CI on every push. It scans the current content of every text file git
tracks, and every commit's metadata, for things that should not be in a public repo: non-documentation IP addresses, personal emails, URLs to
unlisted hosts, home-directory paths, secret-shaped strings, non-noreply commit identities and co-author
trailers. It accepts an optional private denylist kept outside the repo. It does not read binary files or
older revisions, and it catches mechanical leaks, not meaning, so a person still reads what gets published.

```sh
python3 tools/hygiene_check.py
```

## License

MIT
