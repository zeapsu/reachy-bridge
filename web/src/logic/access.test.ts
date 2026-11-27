import { describe, expect, test } from "bun:test";
import type { PairedDevice, ProfileItem } from "./auth.ts";
import { accessList, approveAdminQuestion, isLastAdmin, isWaitingList, makeAdminQuestion, waitingText } from "./access.ts";

const device = (id: string, extra: Partial<PairedDevice> = {}): PairedDevice => ({
  id,
  name: id,
  created: 0,
  last_seen: 0,
  peer: "192.168.1.2",
  role: "member",
  current: false,
  ...extra,
});
const profile = (name: string, devices: string[], admin = false): ProfileItem => ({ name, admin, devices });

describe("accessList", () => {
  test("one admin and nothing else is a plain device list", () => {
    const devices = [device("laptop", { role: "admin" }), device("phone", { role: "admin" })];
    const list = accessList([profile("Andry", ["laptop", "phone"], true)], devices);
    expect(list.solo).toBe(true);
    expect(list.people[0].devices.map((d) => d.id)).toEqual(["laptop", "phone"]);
  });

  test("a second person, a profile without devices or a shared tablet shows the people", () => {
    const admin = profile("Andry", ["laptop"], true);
    const laptop = device("laptop", { role: "admin" });
    expect(accessList([admin, profile("Mia", [])], [laptop]).solo).toBe(false);
    expect(accessList([admin, profile("Sam", ["phone"], true)], [laptop, device("phone", { role: "admin" })]).solo).toBe(false);
    const tablet = device("tablet", { shared: true });
    const list = accessList([admin], [laptop, tablet]);
    expect(list.solo).toBe(false);
    expect(list.shared).toEqual([tablet]);
    expect(list.people[0].devices).toEqual([laptop]);
  });

  test("a shared tablet is never listed under a person", () => {
    const tablet = device("tablet", { shared: true });
    const list = accessList([profile("Mia", ["tablet", "ipad"])], [tablet, device("ipad")]);
    expect(list.people[0].devices.map((d) => d.id)).toEqual(["ipad"]);
  });
});

describe("isLastAdmin", () => {
  test("only the single admin device", () => {
    const one = [device("a", { role: "admin" }), device("b")];
    expect(isLastAdmin(one, one[0])).toBe(true);
    expect(isLastAdmin(one, one[1])).toBe(false);
    const two = [device("a", { role: "admin" }), device("b", { role: "admin" })];
    expect(isLastAdmin(two, two[0])).toBe(false);
  });
});

describe("waiting", () => {
  test("says how long a browser has been asking", () => {
    expect(waitingText(0)).toBe("asking now");
    expect(waitingText(9)).toBe("asking now");
    expect(waitingText(47)).toBe("asking for 40 s");
    expect(waitingText(60)).toBe("asking for 1 min");
    expect(waitingText(299)).toBe("asking for 4 min");
  });

  test("accepts only a list of addresses and ages", () => {
    expect(isWaitingList({ pending: [] })).toBe(true);
    expect(isWaitingList({ pending: [{ peer: "10.0.0.2", age: 3 }] })).toBe(true);
    expect(isWaitingList({ pending: [{ peer: "10.0.0.2" }] })).toBe(false);
    expect(isWaitingList({ detail: "only an admin can see who is waiting" })).toBe(false);
    expect(isWaitingList(null)).toBe(false);
  });
});

describe("approveAdminQuestion", () => {
  test("names the device when the admin typed a name", () => {
    expect(approveAdminQuestion("Phone")).toBe("Approve Phone as an admin? It can then open System, approve devices and install apps.");
    expect(approveAdminQuestion("")).toContain("Approve this device as an admin?");
  });
});

describe("makeAdminQuestion", () => {
  test("names what an admin can do, and the profile it lifts", () => {
    const ipad = device("Mia's iPad");
    expect(makeAdminQuestion(ipad, "Mia", false)).toBe(
      "Make Mia's iPad an admin? It can then open System, approve devices and install apps.",
    );
    expect(makeAdminQuestion(ipad, "Mia", true)).toContain("Mia's profile stops limiting every device of Mia's.");
  });
});
