import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  type CheckResult,
  type Decision,
  freeze,
  gate,
  type GateInput,
  type Handoff,
  validateHandoff,
} from "../src/review-gate.ts";

const handoff: Handoff = {
  taskId: "t1",
  owner: "builder",
  contributors: ["helper"],
  objective: "return 404 for unknown paths",
  acceptance: ["unknown path returns 404"],
  writeScope: ["src/router"],
  requiredChecks: ["unit", "lint"],
  returnTo: "lead",
};

const digest = freeze({ "src/router.ts": "export const notFound = 404;\n" });

const passing: CheckResult[] = [
  { checkId: "unit", digest, status: "pass" },
  { checkId: "lint", digest, status: "pass" },
];

const base: GateInput = {
  handoff,
  candidate: { taskId: "t1", digest, author: "builder", authorFamily: "family-a" },
  checks: passing,
  review: {
    reviewer: "reviewer",
    reviewerFamily: "family-b",
    reviewedDigest: digest,
    verdict: "accept",
    findings: [],
    uncertainties: [],
  },
  round: 1,
};

function reasons(d: Decision): readonly string[] {
  return d.kind === "apply" ? [] : d.reasons;
}

describe("freeze", () => {
  it("changes with any edit", () => {
    assert.notEqual(freeze({ a: "1" }), freeze({ a: "2" }));
    assert.notEqual(freeze({ a: "1" }), freeze({ b: "1" }));
  });
  it("ignores insertion order", () => {
    assert.equal(freeze({ a: "1", b: "2" }), freeze({ b: "2", a: "1" }));
  });
  it("keeps file boundaries unambiguous", () => {
    assert.notEqual(freeze({ a: "bc" }), freeze({ ab: "c" }));
  });
});

describe("validateHandoff", () => {
  it("accepts a complete handoff", () => assert.deepEqual(validateHandoff(handoff), []));
  it("rejects an owner who is also a contributor", () => {
    assert.deepEqual(validateHandoff({ ...handoff, contributors: ["builder"] }), ["owner listed as a contributor"]);
  });
  it("requires at least one required check", () => {
    assert.deepEqual(validateHandoff({ ...handoff, requiredChecks: [] }), ["no required checks"]);
  });
});

describe("gate: acceptance is computed", () => {
  it("applies a clean, fully checked candidate", () => {
    assert.deepEqual(gate(base), { kind: "apply", digest });
  });

  it("one passing check cannot stand in for the others", () => {
    const d = gate({ ...base, checks: [passing[0]!] });
    assert.equal(d.kind, "revise");
    assert.deepEqual(reasons(d), ["check lint: no result for this artifact"]);
  });

  it("checks run against another artifact do not count", () => {
    const stale = passing.map((c) => ({ ...c, digest: freeze({ "src/router.ts": "old" }) }));
    assert.equal(gate({ ...base, checks: stale }).kind, "revise");
  });

  it("blocks on not_run, conflicting results and unrequired failures", () => {
    const notRun = gate({ ...base, checks: [passing[0]!, { checkId: "lint", digest, status: "not_run" }] });
    assert.deepEqual(reasons(notRun), ["check lint: not_run"]);
    const conflicting = gate({ ...base, checks: [...passing, { checkId: "unit", digest, status: "fail" }] });
    assert.deepEqual(reasons(conflicting), ["check unit: conflicting results"]);
    const extra = gate({ ...base, checks: [...passing, { checkId: "e2e", digest, status: "fail" }] });
    assert.deepEqual(reasons(extra), ["check e2e (not required): fail"]);
  });

  it("an accept verdict does not override an open uncertainty or finding", () => {
    const uncertain = gate({ ...base, review: { ...base.review, uncertainties: ["quota unknown"] } });
    assert.deepEqual(reasons(uncertain), ["uncertain: quota unknown"]);
    const finding = { severity: "important" as const, issue: "redirect not bound", fix: "bind it" };
    assert.equal(gate({ ...base, review: { ...base.review, findings: [finding] } }).kind, "revise");
    const suggestion = { ...finding, severity: "suggestion" as const };
    assert.equal(gate({ ...base, review: { ...base.review, findings: [suggestion] } }).kind, "apply");
  });

  it("insufficient_evidence is a verdict, not an approval", () => {
    assert.equal(gate({ ...base, review: { ...base.review, verdict: "insufficient_evidence" } }).kind, "revise");
  });

  it("escalates to a person at the round cap", () => {
    const d = gate({ ...base, round: 3, previousReviewer: "reviewer", review: { ...base.review, verdict: "revise" } });
    assert.equal(d.kind, "escalate_to_human");
  });
});

describe("gate: who may review what", () => {
  const invalid = (input: Partial<GateInput>, why: string) => {
    const d = gate({ ...base, ...input });
    assert.equal(d.kind, "invalid");
    assert.ok(reasons(d).includes(why), `expected "${why}" in ${JSON.stringify(reasons(d))}`);
  };

  it("the gate validates the handoff", () => {
    invalid({ handoff: { ...handoff, requiredChecks: [] } }, "handoff: no required checks");
  });
  it("rejects a same-family reviewer", () => {
    invalid({ review: { ...base.review, reviewerFamily: "family-a" } }, "reviewer shares the author's model family");
  });
  it("rejects a review of a different digest", () => {
    invalid({ review: { ...base.review, reviewedDigest: "sha256:other" } }, "review is for a different artifact");
  });
  it("rejects a candidate from someone other than the owner", () => {
    invalid({ candidate: { ...base.candidate, author: "helper" } }, "candidate was not submitted by the owner");
  });
  it("rejects a contributor reviewing their own work", () => {
    invalid({ review: { ...base.review, reviewer: "helper" } }, "reviewer helped build the candidate");
  });
  it("requires the same reviewer for a fix round", () => {
    invalid({ round: 2, previousReviewer: "someone-else" }, "a fix must be rechecked by the reviewer who asked for it");
    invalid({ round: 2 }, "fix round without the previous reviewer on record");
  });
  it("rejects a blank reviewer identity", () => {
    invalid({ review: { ...base.review, reviewer: " ", reviewerFamily: "" } }, "missing reviewer");
    invalid({ review: { ...base.review, reviewerFamily: "" } }, "missing reviewer family");
  });
  it("rejects blank handoff entries", () => {
    invalid({ handoff: { ...handoff, requiredChecks: ["unit", ""] } }, "handoff: blank entry in requiredChecks");
  });
  it("rejects a nonsense round number or cap", () => {
    assert.throws(() => gate({ ...base, round: 0 }), RangeError);
    assert.throws(() => gate(base, Number.NaN), RangeError);
    assert.throws(() => gate(base, 0), RangeError);
  });
});
