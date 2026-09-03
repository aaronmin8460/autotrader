/**
 * The translation layer's invariants.
 *
 * Two of these tests are about correctness of the *system*; the rest are about
 * a specific class of mistake this dashboard cannot afford. Translating an
 * authoritative runtime word — `PARTICIPATE`, `FILLED`, `PENDING_NEW`, `LONG`
 * — would change what the interface claims the system did. The catalogues are
 * therefore asserted against the identifier list directly, so a well-meaning
 * future edit that "finishes the translation" fails the build.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { en } from "./i18n/en.ts";
import { ko } from "./i18n/ko.ts";
import { dateLong, relativeTime, stamp, stampFull } from "./i18n/datetime.ts";
import { DASH, money, percent, signedMoney } from "./format.ts";
import { isLocale, LOCALES, DEFAULT_LOCALE, HTML_LANG } from "./i18n/locale.ts";

const AT = "2026-09-03T18:05:39+00:00";

test("both catalogues carry exactly the same keys", () => {
  const enKeys = Object.keys(en).sort();
  const koKeys = Object.keys(ko).sort();
  assert.deepEqual(koKeys, enKeys, "a key exists in one catalogue but not the other");
});

test("no required Korean string is left empty", () => {
  for (const [key, value] of Object.entries(ko)) {
    if (key.startsWith("gloss.")) continue; // a gloss may legitimately be absent
    assert.notEqual(value.trim(), "", `ko[${key}] is empty`);
  }
});

test("no required Korean string is still the English one", () => {
  // Identifiers, brand words and units are legitimately identical.
  const identical = new Set([
    "app.name",
    "control.language.en",
    "control.language.ko",
    "status.data.fresh",
    "status.data.stale",
    "status.data.waiting",
    "status.data.offline",
    "strategies.role.primary",
    "strategies.role.observer",
    "strategies.role.legacy",
    "strategies.mode.paper",
    "strategies.mode.shadow",
    "strategies.observationOnly",
    "strategies.capability.zeroOrders",
    "risk.status.onTarget",
    "risk.status.belowTarget",
    "risk.status.aboveTarget",
    "risk.status.nearCap",
    "risk.status.overCap",
    "risk.status.unavailable",
    "shadows.parityNA",
    "sd.codeSha",
  ]);
  const untranslated: string[] = [];
  for (const [key, value] of Object.entries(ko)) {
    if (key.startsWith("gloss.") || identical.has(key)) continue;
    if (value === (en as Record<string, string>)[key]) untranslated.push(key);
  }
  assert.deepEqual(untranslated, [], "Korean strings identical to English");
});

test("authoritative runtime identifiers are never translated away", () => {
  // Every one of these is a value a runtime, a broker or systemd produced.
  // If a Korean string ever *replaces* one, an operator reading the Korean UI
  // is reading a different claim from the one the system recorded.
  const identifiers = [
    "PARTICIPATE",
    "DEFENSIVE",
    "LONG",
    "FLAT",
    "BUY",
    "SELL",
    "FILLED",
    "PENDING_NEW",
    "NEW",
    "ACCEPTED",
    "REJECTED",
    "UNKNOWN",
    "EDA-1",
    "V3",
    "A1-B",
    "OBSERVING",
    "MASKED",
    "RUNNING",
    "CLEAN",
    "SAFE",
  ];
  for (const identifier of identifiers) {
    // The catalogues must not contain a *mapping* whose English side is only
    // the identifier: that is the shape a "translated" identifier would take.
    for (const [key, value] of Object.entries(en)) {
      if (value.trim() === identifier) {
        assert.equal(
          ((ko as Record<string, string>)[key] ?? "").trim(),
          identifier,
          `ko[${key}] translates the authoritative identifier ${identifier}`,
        );
      }
    }
  }
});

test("a gloss explains an identifier rather than replacing it", () => {
  // English shows the identifier alone, so its glosses are deliberately empty.
  for (const [key, value] of Object.entries(en)) {
    if (key.startsWith("gloss.")) assert.equal(value, "");
  }
  // Korean glosses exist and are not themselves the identifier.
  assert.equal(ko["gloss.PARTICIPATE"], "시장 참여");
  assert.equal(ko["gloss.DEFENSIVE"], "방어");
  assert.notEqual(ko["gloss.PARTICIPATE"], "PARTICIPATE");
});

test("interpolation placeholders survive translation", () => {
  const placeholders = (value: string) =>
    [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1] ?? "").sort();
  for (const [key, value] of Object.entries(en)) {
    assert.deepEqual(
      placeholders((ko as Record<string, string>)[key] ?? ""),
      placeholders(value),
      `ko[${key}] has different interpolation placeholders`,
    );
  }
});

test("locale helpers", () => {
  assert.deepEqual([...LOCALES], ["en", "ko"]);
  assert.equal(DEFAULT_LOCALE, "en");
  assert.ok(isLocale("ko"));
  assert.ok(!isLocale("jp"));
  assert.equal(HTML_LANG.ko, "ko");
});

test("currency is never converted or re-formatted by locale", () => {
  // A KRW rendering of a USD account would be a number no runtime computed.
  assert.equal(money(101995.05), "$101,995.05");
  assert.equal(signedMoney(-1917.46), "-$1,917.46");
  assert.equal(percent(0.9014552179640091, 2), "90.15%");
});

test("dates localise; the UTC clock does not", () => {
  assert.equal(dateLong(AT, "en"), "Sep 3, 2026");
  assert.equal(dateLong(AT, "ko"), "2026. 9. 3.");
  // Same instant, same clock, in both locales - and it is the UTC clock.
  assert.equal(stampFull(AT, "en"), "Sep 3, 2026 18:05:39 UTC");
  assert.equal(stampFull(AT, "ko"), "2026. 9. 3. 18:05:39 UTC");
  // Inside the reference's own UTC day the date is dropped entirely.
  assert.equal(stamp(AT, "2026-09-03T23:00:00+00:00", "en"), "18:05:39");
  assert.equal(stamp(AT, "2026-09-03T23:00:00+00:00", "ko"), "18:05:39");
  // On another day the date returns, localised, with the same clock.
  assert.ok(stamp(AT, "2026-09-04T01:00:00+00:00", "en").endsWith("18:05:39"));
  assert.ok(stamp(AT, "2026-09-04T01:00:00+00:00", "ko").endsWith("18:05:39"));
  assert.notEqual(
    stamp(AT, "2026-09-04T01:00:00+00:00", "en"),
    stamp(AT, "2026-09-04T01:00:00+00:00", "ko"),
  );
});

test("relative time is localised and keeps its direction", () => {
  const later = "2026-09-03T18:09:39+00:00";
  const past = relativeTime(AT, later, "en");
  assert.ok(past.includes("4"), past);
  assert.ok(relativeTime(AT, later, "ko").includes("4"));
  // A future stamp must not read as elapsed.
  const future = relativeTime(later, AT, "en");
  assert.notEqual(future, past);
});

test("an unreadable timestamp is a dash in both locales", () => {
  for (const locale of LOCALES) {
    assert.equal(dateLong(null, locale), "—");
    assert.equal(stampFull("not-a-date", locale), "—");
    assert.equal(stamp(undefined, AT, locale), "—");
  }
});

test("the dash restated in datetime.ts is the one lib/format uses", () => {
  // `lib/i18n/datetime.ts` cannot import it (see the comment there), so the
  // two are asserted equal rather than trusted to stay equal.
  assert.equal(dateLong(null, "en"), DASH);
});
