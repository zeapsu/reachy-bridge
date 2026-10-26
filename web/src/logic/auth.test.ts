import { describe, expect, test } from "bun:test";
import { formatCode, needsLogin, normalizeCode, pairEndedText, pairOutcome, seenText } from "./auth.ts";

describe("needsLogin", () => {
  test("only in LAN mode without a login", () => {
    expect(needsLogin({ lan: true, authenticated: false })).toBe(true);
    expect(needsLogin({ lan: true, authenticated: true })).toBe(false);
    expect(needsLogin({ lan: false, authenticated: true })).toBe(false);
    expect(needsLogin(null)).toBe(false);
  });
});

describe("pairOutcome", () => {
  test("passes known statuses through", () => {
    for (const s of ["pending", "approved", "expired", "locked"] as const) expect(pairOutcome(200, { status: s })).toBe(s);
  });
  test("a forgotten request is expired", () => {
    expect(pairOutcome(404, { detail: "unknown pair request" })).toBe("expired");
  });
  test("other failures are errors", () => {
    expect(pairOutcome(500, null)).toBe("error");
    expect(pairOutcome(200, { status: "weird" })).toBe("error");
    expect(pairOutcome(200, "text")).toBe("error");
  });
});

describe("codes", () => {
  test("format splits in two", () => {
    expect(formatCode("123456")).toBe("123 456");
    expect(formatCode("12")).toBe("12");
  });
  test("normalize accepts spaces and dashes, only 6 digits", () => {
    expect(normalizeCode(" 123 456 ")).toBe("123456");
    expect(normalizeCode("123-456")).toBe("123456");
    expect(normalizeCode("12345")).toBeNull();
    expect(normalizeCode("12345a")).toBeNull();
  });
  test("ended text", () => {
    expect(pairEndedText("expired")).toContain("expired");
    expect(pairEndedText("locked")).toContain("wrong codes");
  });
});

describe("seenText", () => {
  const now = 1_000_000_000_000;
  const ago = (s: number) => seenText(now / 1000 - s, now);
  test("buckets", () => {
    expect(ago(30)).toBe("just now");
    expect(ago(20 * 60)).toBe("20 min ago");
    expect(ago(3 * 3600)).toBe("3 h ago");
    expect(ago(5 * 86400)).toBe("5 d ago");
  });
});
