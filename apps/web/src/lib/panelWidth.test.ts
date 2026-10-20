import { describe, expect, it } from "vitest";
import { clampPanel } from "./panelWidth";

describe("clampPanel", () => {
  it("keeps the side panel between 300 px and half the window, at most 720 px", () => {
    expect(clampPanel(200, 1400)).toBe(300);
    expect(clampPanel(500, 1400)).toBe(500);
    expect(clampPanel(900, 1400)).toBe(700);
    expect(clampPanel(900, 2000)).toBe(720);
    expect(clampPanel(400, 500)).toBe(300); // a narrow window still gets the minimum
  });
});
