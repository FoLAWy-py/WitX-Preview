import { describe, expect, it } from "vitest";
import { fitScreen, KEY_ROOM } from "./screen";

const PHONE = { width: 1080, height: 2400 };

describe("fitScreen", () => {
  it("draws the picture as large as the room allows, at its own shape", () => {
    const fit = fitScreen(PHONE, { width: 600, height: 700 }, false);
    expect(fit.height + 2 * fit.bezel).toBeLessThanOrEqual(700);
    expect(fit.height + 2 * fit.bezel).toBeGreaterThan(690);
    expect(Math.abs(fit.width / fit.height - 1080 / 2400)).toBeLessThan(0.01);
  });

  it("leaves room on both sides for the side keys", () => {
    const fit = fitScreen(PHONE, { width: 300, height: 2000 }, true);
    expect(fit.width + 2 * fit.bezel + 2 * KEY_ROOM).toBeLessThanOrEqual(300);
  });

  it("never draws the picture larger than it is", () => {
    const fit = fitScreen(
      { width: 400, height: 800 },
      { width: 3000, height: 3000 },
      true,
    );
    expect(fit).toMatchObject({ width: 400, height: 800 });
  });

  it("gives a small picture a thin bezel and a large one a thicker one", () => {
    const small = fitScreen(PHONE, { width: 200, height: 300 }, false);
    const large = fitScreen(PHONE, { width: 800, height: 1000 }, false);
    expect(small.bezel).toBeLessThan(large.bezel);
  });
});
