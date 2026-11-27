import { afterEach, describe, expect, test } from "bun:test";
import { sendTold } from "./auth.ts";
import { ApiError } from "./client.ts";

const WARNING = "Phone is in control: the robot may move back home.";
const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Answers each request with the next status; records the URLs asked for. */
function controller(...statuses: number[]): string[] {
  const urls: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    urls.push(String(input));
    const status = statuses.shift() ?? 500;
    const body = status === 428 ? { detail: WARNING } : status === 409 ? { detail: "this is the last admin device" } : {};
    return new Response(JSON.stringify(body), { status });
  }) as typeof fetch;
  return urls;
}

describe("sendTold (#341)", () => {
  test("a question that warned sends the change told", async () => {
    const urls = controller(200);
    expect(await sendTold("/api/auth/devices/a", { method: "DELETE" }, true, () => false)).toBe(true);
    expect(urls).toEqual(["/api/auth/devices/a?motion=ok"]);
  });

  test("nobody driving: one request, no second question", async () => {
    const urls = controller(204);
    let asked = 0;
    expect(await sendTold("/api/auth/devices/a", { method: "DELETE" }, false, () => (asked++, true))).toBe(true);
    expect([urls, asked]).toEqual([["/api/auth/devices/a"], 0]);
  });

  test("a refusal asks with the controller's warning, and a yes sends it told", async () => {
    const urls = controller(428, 204);
    const asked: string[] = [];
    expect(await sendTold("/api/auth/devices/a", { method: "DELETE" }, false, (w) => (asked.push(w), true))).toBe(true);
    expect(asked).toEqual([WARNING]);
    expect(urls).toEqual(["/api/auth/devices/a", "/api/auth/devices/a?motion=ok"]);
  });

  test("a no changes nothing", async () => {
    const urls = controller(428);
    expect(await sendTold("/api/auth/devices/a", { method: "DELETE" }, false, () => false)).toBe(false);
    expect(urls).toHaveLength(1);
  });

  test("other refusals are errors, with no second question", async () => {
    controller(409);
    let asked = 0;
    const sent = sendTold("/api/auth/devices/a/role", { method: "PUT", body: "{}" }, false, () => (asked++, true));
    await expect(sent).rejects.toBeInstanceOf(ApiError);
    expect(asked).toBe(0);
  });
});
