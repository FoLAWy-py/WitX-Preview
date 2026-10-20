import { describe, expect, it } from "vitest";
import { gestureOf, sameGeometry, wheelSwipeOf } from "./gesture";
import type { TrailPoint } from "./trail";

const SIZE = { width: 1080, height: 2400 };

function at(x: number, y: number, t_ms: number): TrailPoint {
  return { x, y, t_ms };
}

describe("gestureOf", () => {
  const press = {
    from: { x: 100, y: 200 },
    to: { x: 103, y: 202 },
    trail: [at(100, 200, 0)],
    size: SIZE,
  };

  it("is a tap where the press began when it moved less than 8 page pixels", () => {
    expect(gestureOf({ ...press, moved: 7.9, held: 120 })).toEqual({
      kind: "tap",
      x: 100,
      y: 200,
    });
  });

  it("is a long press when held still for 500 ms, at most 15 s", () => {
    expect(gestureOf({ ...press, moved: 3, held: 500 })).toEqual({
      kind: "long_press",
      x: 100,
      y: 200,
      duration_ms: 500,
    });
    expect(gestureOf({ ...press, moved: 3, held: 20_000 })).toMatchObject({
      duration_ms: 15000,
    });
  });

  it("is a swipe along the way the pointer took, ending where it was let go", () => {
    const trail = [at(100, 200, 0), at(150, 260, 100), at(200, 300, 200)];
    expect(
      gestureOf({
        from: { x: 100, y: 200 },
        to: { x: 250, y: 320 },
        moved: 90,
        held: 300,
        trail,
        size: SIZE,
      }),
    ).toEqual({
      kind: "swipe",
      x1: 100,
      y1: 200,
      x2: 250,
      y2: 320,
      duration_ms: 300,
      path: [...trail, at(250, 320, 300)],
    });
  });

  it("is a swipe however briefly a drag of 8 page pixels or more was held", () => {
    expect(gestureOf({ ...press, moved: 8, held: 900 }).kind).toBe("swipe");
  });
});

describe("wheelSwipeOf", () => {
  it("moves the finger up for the wheel turned down, by as much of the screen as of the picture", () => {
    // 100 page pixels on a picture shown 600 high: a sixth of the screen.
    expect(wheelSwipeOf(100, { x: 500, y: 1200 }, SIZE, 600)).toEqual({
      kind: "swipe",
      x1: 500,
      y1: 1200,
      x2: 500,
      y2: 800,
      duration_ms: 250,
    });
  });

  it("goes at most 40% of the screen, and never off it", () => {
    expect(wheelSwipeOf(-10_000, { x: 500, y: 1200 }, SIZE, 600).y2).toBe(2160);
    expect(wheelSwipeOf(300, { x: 500, y: 100 }, SIZE, 600).y2).toBe(1);
  });
});

describe("sameGeometry", () => {
  const upright = { width: 1080, height: 2400, rotation: 0 };

  it("holds for pictures of the same size turned the same way", () => {
    expect(sameGeometry(upright, { ...upright })).toBe(true);
  });

  it("does not hold for another size, nor for a phone turned right round at the same size", () => {
    expect(sameGeometry(upright, { ...upright, rotation: 180 })).toBe(false);
    expect(
      sameGeometry(upright, { ...upright, width: 2400, height: 1080 }),
    ).toBe(false);
  });
});
