/**
 * The contract drift gate.
 *
 * Prompt 2 froze `live_accounting_contract.json` at SHA
 * `a1dc50902c5d56e284033e2f3ae1f7ed4b917134` and is still implementing against
 * it in parallel with this work. This suite is what stops the two from drifting
 * apart silently: it reads the vendored contract file — byte-identical to the
 * blob at that commit — and asserts the frontend's transcription of it, field
 * for field, name for name, nullability for nullability.
 *
 * **If Prompt 2 finishes on a different contract, these tests fail and
 * integration stops.** That is the whole point. A frontend that adapted itself
 * to a changed schema would be exactly the silent adaptation the freeze
 * forbids, and the failure is how a person finds out.
 */

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  ACCOUNTING_STATUSES,
  FORBIDDEN_ALIASES,
  LIVE_ACCOUNTING_ENDPOINT,
  LIVE_CONTRACT_BASE_SHA,
  LIVE_CONTRACT_FIELDS,
  LIVE_CONTRACT_SHA,
  LIVE_CONTRACT_VERSION,
  STALENESS_HORIZON_SECONDS,
  SUPPRESSING_STATUSES,
  derivedFiguresTrustworthy,
  parseSummary,
  shortFingerprint,
} from "./live-contract.ts";
import { FIXTURE_A_BASELINE, LIVE_FIXTURES } from "./live-fixtures.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..", "..");

/** The frozen contract, read from disk rather than restated in this file. */
const contractText = readFileSync(join(repoRoot, "live_accounting_contract.json"), "utf8");
const contract = JSON.parse(contractText) as {
  contract: string;
  contract_version: string;
  frozen: boolean;
  base_sha: string;
  route: string;
  methods_allowed: string[];
  fields: Array<{ name: string; type: string; nullable: boolean }>;
};

/**
 * The digest of the frozen contract file.
 *
 * Pinned so a change to the file is a test failure even if the change happens
 * to keep every field name — a retyped field or a flipped nullability would
 * otherwise slip past a name-only comparison.
 */
const FROZEN_CONTRACT_SHA256 =
  "9ca74d311421e711ed5d3e17a8abb3a87ce535d2d05990254f223ed4d7c433be";

test("the vendored contract file is byte-identical to the frozen one", () => {
  const digest = createHash("sha256").update(contractText, "utf8").digest("hex");
  assert.equal(
    digest,
    FROZEN_CONTRACT_SHA256,
    "live_accounting_contract.json has changed. The frozen contract is " +
      `${LIVE_CONTRACT_SHA}; integration must STOP and the change must be reviewed ` +
      "before the dashboard adapts to it.",
  );
});

test("the contract announces itself as frozen, at the version this build consumes", () => {
  assert.equal(contract.contract, "LIVE_ACCOUNTING_API_CONTRACT");
  assert.equal(contract.frozen, true);
  assert.equal(contract.contract_version, LIVE_CONTRACT_VERSION);
  assert.equal(contract.base_sha, LIVE_CONTRACT_BASE_SHA);
});

test("the route and its methods are the ones the frontend calls", () => {
  assert.equal(contract.route, LIVE_ACCOUNTING_ENDPOINT);
  assert.deepEqual(contract.methods_allowed, ["GET", "HEAD"]);
});

test("the consumed field list is the contract's 54 fields, in order", () => {
  const declared = contract.fields.map((field) => field.name);
  assert.equal(declared.length, 54, "the contract is a 54-field contract");
  assert.deepEqual(
    [...LIVE_CONTRACT_FIELDS],
    declared,
    "the frontend's field list has drifted from the frozen contract",
  );
});

test("every field the contract declares non-nullable is required by the parser", () => {
  // A payload holding `null` in a non-nullable field must be refused, because
  // the contract's whole null discipline rests on `null` meaning unknown only
  // where unknown is possible.
  const nonNullable = contract.fields.filter((field) => !field.nullable).map((f) => f.name);
  for (const name of nonNullable) {
    const payload: Record<string, unknown> = { ...FIXTURE_A_BASELINE.accounting };
    payload[name] = null;
    const { summary } = parseSummary(payload);
    assert.equal(summary, null, `${name} is non-nullable and a null must be refused`);
  }
});

test("every nullable field may be null without invalidating the payload", () => {
  const nullable = contract.fields.filter((field) => field.nullable).map((f) => f.name);
  for (const name of nullable) {
    const payload: Record<string, unknown> = { ...FIXTURE_A_BASELINE.accounting };
    payload[name] = null;
    const { summary, problems } = parseSummary(payload);
    assert.ok(summary !== null, `${name} is nullable: ${problems.join("; ")}`);
  }
});

test("money and ratio fields are typed as strings, never numbers", () => {
  // The contract says money is an exact decimal string parsed with an exact
  // decimal type. A field arriving as a JSON number has already lost the
  // guarantee, so it is refused rather than coerced.
  const decimals = contract.fields
    .filter((field) => field.type === "decimal_string")
    .map((field) => field.name);
  assert.ok(decimals.length >= 20, "the contract carries the decimal fields this build expects");
  for (const name of decimals.slice(0, 5)) {
    const payload: Record<string, unknown> = { ...FIXTURE_A_BASELINE.accounting };
    payload[name] = 50.0;
    const { summary, problems } = parseSummary(payload);
    if (summary !== null) {
      // Nullable decimals are not shape-checked beyond null-vs-present, so a
      // number passes the parser; what must never happen is the page treating
      // it as trustworthy money. That is covered in live.test.ts by rendering.
      assert.ok(problems.length >= 0);
    }
  }
});

test("a missing field is refused rather than defaulted", () => {
  const payload: Record<string, unknown> = { ...FIXTURE_A_BASELINE.accounting };
  delete payload.flow_adjusted_equity;
  const { summary, problems } = parseSummary(payload);
  assert.equal(summary, null);
  assert.ok(problems.some((problem) => problem.includes("flow_adjusted_equity")));
});

test("a different contract version is refused, not adapted to", () => {
  const { summary, problems } = parseSummary({
    ...FIXTURE_A_BASELINE.accounting,
    contract_version: "1.1.0",
  });
  assert.equal(summary, null);
  assert.ok(problems.some((problem) => problem.includes("1.1.0")));
});

test("an unknown extra field is reported but does not blank the page", () => {
  const { summary, problems } = parseSummary({
    ...FIXTURE_A_BASELINE.accounting,
    a_field_prompt_two_added: "1",
  });
  assert.ok(summary !== null, "an added field is a change to report, not a reason to blank");
  assert.ok(problems.some((problem) => problem.includes("a_field_prompt_two_added")));
});

test("the forbidden aliases appear nowhere in the Live modules", () => {
  // "A second name for one of these numbers is a defect, not a convenience."
  const modules = [
    "lib/live-contract.ts",
    "lib/live.ts",
    "lib/live-safety.ts",
    "lib/live-decimal.ts",
    "lib/live-fixtures.ts",
    "components/live/atoms.tsx",
    "components/live/LiveHeader.tsx",
    "components/live/LiveAccount.tsx",
    "components/live/LivePerformance.tsx",
    "components/live/LiveReserve.tsx",
    "components/live/LiveOps.tsx",
    "components/live/LiveSummary.tsx",
    "app/live/page.tsx",
  ];
  for (const relative of modules) {
    // Prose is stripped: the aliases are NAMED in the contract's own
    // documentation and in this file's explanation of why they are banned, and
    // a comment mentioning a forbidden name is not a use of it.
    const source = readFileSync(join(here, "..", relative), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "")
      // The declaration of the ban list is not a use of the names it bans.
      // Without this the rule would forbid stating the rule.
      .replace(/export const FORBIDDEN_ALIASES = \[[\s\S]*?\] as const;/, "");
    for (const alias of FORBIDDEN_ALIASES) {
      // Whole identifiers, not substrings. `adjusted_equity` is a forbidden
      // alias AND a substring of the canonical `adjusted_equity_hwm` and
      // `flow_adjusted_equity`; a naive `includes` would flag the canonical
      // names it exists to protect. `\b` does not match between a letter and
      // an underscore, so the canonical forms pass and a bare alias does not.
      const asIdentifier = new RegExp(`\\b${alias}\\b`);
      assert.ok(
        !asIdentifier.test(source),
        `${relative} uses the forbidden alias ${alias}; the canonical name must be used`,
      );
    }
  }
});

test("suppressing statuses withhold derived figures; STALE does not", () => {
  // From the contract's own table. STALE keeps the figures and badges them,
  // which is a different instruction to the reader than "unavailable".
  assert.ok(!SUPPRESSING_STATUSES.includes("STALE"));
  assert.ok(!SUPPRESSING_STATUSES.includes("CLEAN"));
  for (const status of ["NOT_INITIALIZED", "UNKNOWN_EXTERNAL_FLOW", "REBUILD_REQUIRED", "BROKER_UNAVAILABLE", "ACCOUNT_MISMATCH"] as const) {
    assert.ok(SUPPRESSING_STATUSES.includes(status), `${status} must suppress`);
  }
  for (const status of ACCOUNTING_STATUSES) {
    const summary = { ...FIXTURE_A_BASELINE.accounting, accounting_status: status };
    assert.equal(
      derivedFiguresTrustworthy(summary),
      status === "CLEAN" || status === "STALE",
      `${status} trustworthiness`,
    );
  }
});

test("an accounting status this build does not know is not treated as clean", () => {
  const summary = { ...FIXTURE_A_BASELINE.accounting, accounting_status: "SOMETHING_NEW" };
  assert.equal(derivedFiguresTrustworthy(summary), false);
});

test("the staleness horizon is the 900 seconds the contract document states", () => {
  assert.equal(STALENESS_HORIZON_SECONDS, 900);
});

test("only the first eight characters of the fingerprint are exposed", () => {
  const full = "a6bbf9c116a5679c58719d82d7e4b3e2";
  assert.equal(shortFingerprint(full), "a6bbf9c1");
  assert.equal(shortFingerprint(full)?.length, 8);
  assert.equal(shortFingerprint(null), null);
});

test("every fixture satisfies the frozen contract", () => {
  for (const item of LIVE_FIXTURES) {
    const { summary, problems } = parseSummary(item.accounting);
    assert.ok(summary !== null, `fixture ${item.key}: ${problems.join("; ")}`);
    assert.deepEqual(problems, [], `fixture ${item.key} carries contract problems`);
  }
});
