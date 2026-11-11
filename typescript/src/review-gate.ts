// Review gate for a frozen artifact, built by one owner and reviewed by a different model family.
//
// The gate does not take the reviewer's word for it. It recomputes acceptance from the evidence:
// every required check (named in the handoff, before work starts) passed against this exact digest,
// no failing result for this digest, no blocking or important finding, no open uncertainty, and an
// "accept" verdict. Any edit produces a new digest, which voids every earlier check and review.
//
// Trust boundary: digests, model families and reviewer names are inputs. freeze() computes a digest
// from file contents, but the gate trusts whoever packaged the files and reported the identities.
// Different model families reduce one kind of correlated blind spot; they do not prove independence.

import { createHash } from "node:crypto";

export type Digest = string;

export interface Handoff {
  readonly taskId: string;
  readonly owner: string; // exactly one accountable owner
  readonly contributors: readonly string[]; // may help, never own, never review
  readonly objective: string;
  readonly acceptance: readonly string[]; // observable criteria, written before work starts
  readonly writeScope: readonly string[]; // what the owner may change
  readonly requiredChecks: readonly string[]; // check ids that must pass on the frozen artifact
  readonly returnTo: string; // who receives the single result
}

export interface Candidate {
  readonly taskId: string;
  readonly digest: Digest;
  readonly author: string;
  readonly authorFamily: string;
}

export type CheckStatus = "pass" | "fail" | "not_run";

export interface CheckResult {
  readonly checkId: string;
  readonly digest: Digest; // the artifact the check actually ran against
  readonly status: CheckStatus;
}

export type Severity = "blocking" | "important" | "suggestion";

export interface Finding {
  readonly severity: Severity;
  readonly issue: string;
  readonly fix: string;
}

export type Verdict = "accept" | "revise" | "insufficient_evidence";

export interface Review {
  readonly reviewer: string;
  readonly reviewerFamily: string;
  readonly reviewedDigest: Digest; // what the reviewer read
  readonly verdict: Verdict;
  readonly findings: readonly Finding[];
  readonly uncertainties: readonly string[];
}

export interface GateInput {
  readonly handoff: Handoff;
  readonly candidate: Candidate;
  readonly checks: readonly CheckResult[];
  readonly review: Review;
  readonly round: number; // 1 for the first review; each fix is a new round
  readonly previousReviewer?: string; // required from round 2 on
}

export type Decision =
  | { readonly kind: "apply"; readonly digest: Digest }
  | { readonly kind: "revise"; readonly reasons: readonly string[] }
  | { readonly kind: "escalate_to_human"; readonly reasons: readonly string[] }
  | { readonly kind: "invalid"; readonly reasons: readonly string[] };

export const MAX_ROUNDS = 3;

const blank = (s: string): boolean => s.trim() === "";

export function validateHandoff(h: Handoff): string[] {
  const errors: string[] = [];
  if (blank(h.taskId)) errors.push("no task id");
  if (blank(h.owner)) errors.push("no owner");
  if (h.contributors.includes(h.owner)) errors.push("owner listed as a contributor");
  if (!h.objective.trim()) errors.push("no objective");
  if (h.acceptance.length === 0) errors.push("no acceptance criteria");
  if (h.writeScope.length === 0) errors.push("no write scope");
  if (h.requiredChecks.length === 0) errors.push("no required checks");
  for (const [field, list] of [["contributors", h.contributors], ["acceptance", h.acceptance],
    ["writeScope", h.writeScope], ["requiredChecks", h.requiredChecks]] as const) {
    if (list.some(blank)) errors.push(`blank entry in ${field}`);
  }
  if (new Set(h.requiredChecks).size !== h.requiredChecks.length) errors.push("duplicate required check");
  if (blank(h.returnTo)) errors.push("nobody to return the result to");
  return errors;
}

/** Digest of a set of files: path and content, sorted by path, length-prefixed so boundaries are unambiguous. */
export function freeze(files: Readonly<Record<string, string | Uint8Array>>): Digest {
  const hash = createHash("sha256");
  for (const path of Object.keys(files).sort()) {
    const content = files[path] ?? "";
    const bytes = typeof content === "string" ? Buffer.from(content, "utf8") : content;
    const name = Buffer.from(path, "utf8");
    hash.update(`${name.length}:`).update(name).update(`${bytes.length}:`).update(bytes);
  }
  return "sha256:" + hash.digest("hex");
}

function identityProblems({ handoff, candidate, review, round, previousReviewer }: GateInput): string[] {
  const reasons = validateHandoff(handoff).map((e) => `handoff: ${e}`);
  for (const [name, value] of [["candidate digest", candidate.digest], ["candidate author", candidate.author],
    ["author family", candidate.authorFamily], ["reviewer", review.reviewer], ["reviewer family", review.reviewerFamily],
    ["reviewed digest", review.reviewedDigest]] as const) {
    if (blank(value)) reasons.push(`missing ${name}`);
  }
  if (candidate.taskId !== handoff.taskId) reasons.push("candidate is for a different task");
  if (candidate.author !== handoff.owner) reasons.push("candidate was not submitted by the owner");
  if (review.reviewer === handoff.owner || handoff.contributors.includes(review.reviewer)) {
    reasons.push("reviewer helped build the candidate");
  }
  if (review.reviewerFamily === candidate.authorFamily) reasons.push("reviewer shares the author's model family");
  if (review.reviewedDigest !== candidate.digest) reasons.push("review is for a different artifact");
  if (round > 1 && previousReviewer === undefined) reasons.push("fix round without the previous reviewer on record");
  if (previousReviewer !== undefined && previousReviewer !== review.reviewer) {
    reasons.push("a fix must be rechecked by the reviewer who asked for it");
  }
  return reasons;
}

function evidenceProblems({ handoff, candidate, checks, review }: GateInput): string[] {
  const reasons: string[] = [];
  const current = checks.filter((c) => c.digest === candidate.digest); // results for other digests prove nothing
  for (const id of handoff.requiredChecks) {
    const statuses = new Set(current.filter((c) => c.checkId === id).map((c) => c.status));
    if (statuses.size === 0) reasons.push(`check ${id}: no result for this artifact`);
    else if (statuses.size > 1) reasons.push(`check ${id}: conflicting results`);
    else if (!statuses.has("pass")) reasons.push(`check ${id}: ${[...statuses][0]}`);
  }
  for (const c of current) {
    if (c.status === "fail" && !handoff.requiredChecks.includes(c.checkId)) {
      reasons.push(`check ${c.checkId} (not required): fail`);
    }
  }
  for (const f of review.findings) if (f.severity !== "suggestion") reasons.push(`${f.severity}: ${f.issue}`);
  for (const u of review.uncertainties) reasons.push(`uncertain: ${u}`);
  if (review.verdict !== "accept") reasons.push(`reviewer verdict: ${review.verdict}`);
  return reasons;
}

export function gate(input: GateInput, maxRounds: number = MAX_ROUNDS): Decision {
  if (!Number.isInteger(input.round) || input.round < 1) throw new RangeError("round must be a positive integer");
  if (!Number.isInteger(maxRounds) || maxRounds < 1) throw new RangeError("maxRounds must be a positive integer");
  const invalid = identityProblems(input);
  if (invalid.length > 0) return { kind: "invalid", reasons: invalid };

  const reasons = evidenceProblems(input);
  if (reasons.length === 0) return { kind: "apply", digest: input.candidate.digest };
  if (input.round >= maxRounds) return { kind: "escalate_to_human", reasons };
  return { kind: "revise", reasons };
}
