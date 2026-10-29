import { describe, expect, test } from "bun:test";
import { checkDot, checkSummary, isSetupStatus, timeLeft } from "./setup.ts";

describe("isSetupStatus", () => {
  test("only the setup page's reply", () => {
    expect(isSetupStatus({ setup: true, authenticated: false, locked: false, used: false, expires_in: 900 })).toBe(true);
    expect(isSetupStatus({ detail: "login required" })).toBe(false);
    expect(isSetupStatus("<!doctype html>")).toBe(false);
    expect(isSetupStatus(null)).toBe(false);
  });
});

describe("checkSummary", () => {
  test("counts pass, warnings and failures", () => {
    const r = (status: "pass" | "warn" | "fail" | "skip") => ({ name: "x", status, detail: "" });
    expect(checkSummary([r("pass"), r("pass"), r("warn"), r("skip")])).toBe("2 pass · 1 warning");
    expect(checkSummary([r("fail"), r("warn"), r("warn")])).toBe("2 warnings · 1 failed");
    expect(checkSummary([])).toBe("nothing checked");
  });
  test("dots follow the dashboard's colours", () => {
    expect(checkDot("pass")).toBe("ok");
    expect(checkDot("warn")).toBe("loading");
    expect(checkDot("fail")).toBe("error");
    expect(checkDot("skip")).toBe("");
  });
});

describe("timeLeft", () => {
  test("minutes and seconds", () => {
    expect(timeLeft(892)).toBe("14:52 left");
    expect(timeLeft(5)).toBe("0:05 left");
    expect(timeLeft(-3)).toBe("0:00 left");
  });
});
