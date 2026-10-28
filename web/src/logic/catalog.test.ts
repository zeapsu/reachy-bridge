import { describe, expect, test } from "bun:test";
import type { CatalogApp } from "../api.ts";
import { appMeta, filterCatalog, jobFinished, jobLine, spaceUrl, tokenShape, validSpaceId } from "./catalog.ts";

const app = (id: string, title: string, description = ""): CatalogApp => ({
  id,
  name: id.split("/")[1],
  title,
  description,
  likes: 0,
  author: "",
  updated: null,
  private: false,
  installed: false,
});

describe("catalog", () => {
  test("space ids are owner/name", () => {
    expect(validSpaceId("pollen-robotics/reachy_mini_radio")).toBe(true);
    expect(validSpaceId(" me/app ")).toBe(true);
    expect(validSpaceId("radio")).toBe(false);
    expect(validSpaceId("a/b/c")).toBe(false);
    expect(validSpaceId("me/..")).toBe(false);
    expect(validSpaceId("me/my app")).toBe(false);
  });

  test("details link to the Space page and say who and when", () => {
    expect(spaceUrl("cdeplanne/wake_me_up")).toBe("https://huggingface.co/spaces/cdeplanne/wake_me_up");
    expect(spaceUrl("javascript:alert(1)")).toBeNull();
    expect(spaceUrl("a/b/c")).toBeNull();
    expect(spaceUrl(null)).toBeNull();
    expect(appMeta({ author: "cdeplanne", likes: 12, updated: "2026-06-09T09:02:48.000Z" })).toBe(
      "by cdeplanne · 12 likes · updated Jun 2026",
    );
    expect(appMeta({ likes: 1, updated: "nope" })).toBe("1 like");
    expect(appMeta({})).toBe("");
  });

  test("search matches every word across title, description and id", () => {
    const apps = [app("p/radio", "Radio", "Plays music"), app("p/hello", "Hello", "Says hi")];
    expect(filterCatalog(apps, "")).toHaveLength(2);
    expect(filterCatalog(apps, "music")).toEqual([apps[0]]);
    expect(filterCatalog(apps, "P/HELLO")).toEqual([apps[1]]);
    expect(filterCatalog(apps, "radio hi")).toEqual([]);
  });

  test("job progress shows the last line, shortened", () => {
    expect(jobLine(["a", "b", "  "])).toBe("b");
    expect(jobLine([])).toBe("");
    expect(jobLine(["x".repeat(120)])).toHaveLength(88);
    expect(jobFinished("done") && jobFinished("failed") && !jobFinished("in_progress")).toBe(true);
  });

  test("token shape", () => {
    expect(tokenShape("hf_abc123")).toBe(true);
    expect(tokenShape("")).toBe(false);
    expect(tokenShape("hf abc")).toBe(false);
  });
});
