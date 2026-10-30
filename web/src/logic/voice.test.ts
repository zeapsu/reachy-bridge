import { describe, expect, test } from "bun:test";
import type { VoiceSession } from "../api/voice.ts";
import { talkBlocked, voiceBadge, voiceBlocked } from "./voice.ts";

const session = (over: Partial<VoiceSession> = {}): VoiceSession => ({
  id: null,
  ref: "r1",
  person: "Sam",
  listen: true,
  talk: false,
  hear: false,
  since: 1000,
  you: false,
  ...over,
});

describe("voiceBlocked", () => {
  const asking = { kind: "can_ask" } as const;
  test("needs the https (tailnet) address", () => {
    expect(voiceBlocked({ available: true, session: null }, { kind: "off" }, false)).toContain("https");
  });
  test("refuses in sim mode", () => {
    expect(voiceBlocked({ available: false, session: null }, { kind: "off" })).toContain("simulation");
  });
  test("needs control in admin mode, and nothing outside it", () => {
    const view = { available: true, session: null };
    expect(voiceBlocked(view, { kind: "off" })).toBeNull();
    expect(voiceBlocked(view, { kind: "mine", until: null, endingSoon: false })).toBeNull();
    // Free with control to take: starting takes it.
    expect(voiceBlocked(view, { kind: "free", canTake: true, asking })).toBeNull();
    expect(voiceBlocked(view, { kind: "free", canTake: false, asking })).toContain("Ask for control");
    expect(voiceBlocked(view, { kind: "other", person: "Kim", until: null, canTakeOver: false, asking })).toContain("Kim is in control");
  });
});

describe("talkBlocked", () => {
  test("needs a secure page and a mic", () => {
    expect(talkBlocked(false, true)).toContain("https");
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
    expect(voiceBadge({ available: true, session: session({ listen: false, hear: true }) }, null)).toBe("Sam is hearing what Reachy says");
    expect(voiceBadge({ available: true, session: session({ hear: true, talk: true, you: true }) }, null)).toBe(
      "You're listening, hearing what Reachy says and talking",
    );
  });
  test("without logins, tells this browser's session by its ref", () => {
    const open = session({ person: null });
    expect(voiceBadge({ available: true, session: open }, "r1")).toBe("You're listening");
    expect(voiceBadge({ available: true, session: open }, null)).toBe("Someone is listening");
    expect(voiceBadge({ available: true, session: open }, "r0")).toBe("Someone is listening");
  });
});
