import { describe, expect, test } from "bun:test";
import { loadingHash, loadingPageUrl, parseLoadingHash } from "./loading.ts";

describe("parseLoadingHash", () => {
  test("steps by name, default waking", () => {
    expect(parseLoadingHash("")).toEqual({ kind: "step", index: 0 });
    expect(parseLoadingHash("#waking")).toEqual({ kind: "step", index: 0 });
    expect(parseLoadingHash("#starting")).toEqual({ kind: "step", index: 1 });
    expect(parseLoadingHash("#opening")).toEqual({ kind: "step", index: 2 });
  });
  test("unknown step falls back to the first", () => {
    expect(parseLoadingHash("#nonsense")).toEqual({ kind: "step", index: 0 });
  });
  test("failure carries the decoded message", () => {
    expect(parseLoadingHash(`#${loadingHash("failed:Robot did not wake up in time")}`)).toEqual({
      kind: "failed",
      message: "Robot did not wake up in time",
    });
  });
  test("failure without a message gets a default", () => {
    expect(parseLoadingHash("#failed")).toEqual({ kind: "failed", message: "The app didn't start." });
    expect(parseLoadingHash("#failed:")).toEqual({ kind: "failed", message: "The app didn't start." });
  });
  test("malformed escapes do not throw", () => {
    expect(parseLoadingHash("#failed:100%")).toEqual({ kind: "failed", message: "100%" });
  });
  test("round-trips messages with colons, hashes and unicode", () => {
    const msg = "failed:x: #1 — didn't start";
    expect(parseLoadingHash(loadingHash(msg))).toEqual({ kind: "failed", message: "x: #1 — didn't start" });
  });
});

test("loading page URL", () => {
  expect(loadingPageUrl("/app-loading.html", "Look & Talk")).toBe("/app-loading.html?app=Look%20%26%20Talk#waking");
});
