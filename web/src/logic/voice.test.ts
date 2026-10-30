import { describe, expect, test } from "bun:test";
import type { VoiceSession } from "../api/voice.ts";
import { onTailnet, talkBlocked, voiceBadge, voiceBlocked } from "./voice.ts";

const session = (over: Partial<VoiceSession> = {}): VoiceSession => ({
  id: null,
  ref: "r1",
  person: "Sam",
  listen: true,
  talk: false,
  since: 1000,
  you: false,
  ...over,
});

describe("voiceBlocked", () => {
  test("needs a tailnet address", () => {
    expect(voiceBlocked({ available: true, session: null }, { kind: "off" }, false)).toContain("Tailscale address");
  });
  test("refuses in sim mode", () => {
    expect(voiceBlocked({ available: false, session: null }, { kind: "off" })).toContain("simulation");
  });
  test("needs control in admin mode, and nothing outside it", () => {
    const view = { available: true, session: null };
    expect(voiceBlocked(view, { kind: "off" })).toBeNull();
    expect(voiceBlocked(view, { kind: "mine" })).toBeNull();
    // Free with control to take: starting takes it.
    expect(voiceBlocked(view, { kind: "free", canTake: true })).toBeNull();
    expect(voiceBlocked(view, { kind: "free", canTake: false })).toContain("paired browser");
    expect(voiceBlocked(view, { kind: "other", person: "Kim", canTakeOver: false })).toContain("Kim is in control");
  });
});

describe("onTailnet", () => {
  test("knows tailnet IPs", () => {
    expect(onTailnet("100.64.0.1")).toBe(true);
    expect(onTailnet("100.101.2.3")).toBe(true);
    expect(onTailnet("100.127.255.255")).toBe(true);
    expect(onTailnet("[fd7a:115c:a1e0::1]")).toBe(true);
    expect(onTailnet("100.63.0.1")).toBe(false);
    expect(onTailnet("100.128.0.1")).toBe(false);
    expect(onTailnet("192.168.1.20")).toBe(false);
    expect(onTailnet("robot.example.ts.net")).toBe(false);
    expect(onTailnet("100.64.0.1.example.com")).toBe(false);
  });
});

describe("talkBlocked", () => {
  test("needs a secure page and a mic", () => {
    expect(talkBlocked(false, true)).toContain("certificate");
    expect(talkBlocked(true, false)).toContain("microphone");
    expect(talkBlocked(true, true)).toBeNull();
  });
});

describe("voiceBadge", () => {
  test("says who is live and how", () => {
    expect(voiceBadge({ available: true, session: null }, null)).toBeNull();
    expect(voiceBadge({ available: true, session: session() }, null)).toBe("Sam is listening");
    expect(voiceBadge({ available: true, session: session({ talk: true, you: true }) }, null)).toBe("You're listening and talking");
    expect(voiceBadge({ available: true, session: session({ listen: false, talk: true, person: null }) }, null)).toBe("Someone is talking");
    expect(voiceBadge({ available: true, session: session({ listen: false }) }, null)).toBe("Sam is connected, muted");
  });
  test("without logins, tells this browser's session by its ref", () => {
    const open = session({ person: null });
    expect(voiceBadge({ available: true, session: open }, "r1")).toBe("You're listening");
    expect(voiceBadge({ available: true, session: open }, null)).toBe("Someone is listening");
    expect(voiceBadge({ available: true, session: open }, "r0")).toBe("Someone is listening");
  });
});
