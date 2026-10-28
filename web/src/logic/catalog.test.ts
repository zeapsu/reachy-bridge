import { describe, expect, test } from "bun:test";
import type { CatalogApp } from "../api.ts";
import { filterCatalog, jobFinished, jobLine, tokenShape, validSpaceId } from "./catalog.ts";

const app = (id: string, title: string, description = ""): CatalogApp => ({
  id,
  name: id.split("/")[1],
  title,
  description,
  likes: 0,
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
