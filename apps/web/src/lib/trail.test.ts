import { describe, expect, it } from "vitest";
import { PATH_MAX, path, sample, swipeOf, type TrailPoint } from "./trail";

const SIZE = { width: 1080, height: 2400 };

function at(x: number, y: number, t_ms: number): TrailPoint {
  return { x, y, t_ms };
}

describe("sample", () => {
  it("keeps the first point and then one every 16 ms", () => {
    const trail: TrailPoint[] = [];
    const kept = [0, 5, 10, 16, 20, 31, 32, 50].map((t) =>
      sample(trail, at(t, t, t)),
    );
    expect(kept).toEqual([true, false, false, true, false, false, true, true]);
    expect(trail.map((p) => p.t_ms)).toEqual([0, 16, 32, 50]);
  });
});

describe("path", () => {
  it("ends where the pointer was let go, with whole times that never go back", () => {
    const trail = [at(10, 10, 0), at(20.4, 12, 16.6), at(30, 14, 33.2)];
    expect(path(trail, at(40, 16, 33.1), SIZE)).toEqual([
      at(10, 10, 0),
      at(20, 12, 17),
      at(30, 14, 33),
      at(40, 16, 33),
    ]);
  });

  it("keeps a pointer dragged past the edge of the picture on its edge", () => {
    expect(path([at(-5, 10, 0)], at(1090, 2500, 100), SIZE)).toEqual([
      at(0, 10, 0),
      at(1079, 2399, 100),
    ]);
  });

  it("thins a long drag to 200 points, keeping the first and the last", () => {
    const trail = Array.from({ length: 400 }, (_, i) => at(i, 2 * i, 16 * i));
    const thinned = path(trail, at(400, 800, 6400), SIZE);
    expect(thinned).toHaveLength(PATH_MAX);
    expect(thinned[0]).toEqual(at(0, 0, 0));
    expect(thinned[PATH_MAX - 1]).toEqual(at(400, 800, 6400));
    const times = thinned.map((p) => p.t_ms);
    expect([...times].sort((a, b) => a - b)).toEqual(times);
    expect(thinned[100].t_ms).toBeGreaterThan(3000); // spread over the whole drag
  });
});

describe("swipeOf", () => {
  it("sends a curved drag with its path and the time it took", () => {
    const trail = [at(100, 300, 0), at(180, 320, 120), at(260, 290, 280)];
    expect(swipeOf(trail, at(300, 300, 400), SIZE)).toEqual({
      x1: 100,
      y1: 300,
      x2: 300,
      y2: 300,
      duration_ms: 400,
      path: [
        at(100, 300, 0),
        at(180, 320, 120),
        at(260, 290, 280),
        at(300, 300, 400),
      ],
    });
  });

  it("sends a drag that is just a line, a flick or a very long drag as a plain swipe", () => {
    const line = swipeOf([at(100, 300, 0)], at(300, 300, 400), SIZE);
    expect(line).toEqual({
      x1: 100,
      y1: 300,
      x2: 300,
      y2: 300,
      duration_ms: 400,
    });
    const flick = swipeOf(
      [at(100, 300, 0), at(150, 300, 16)],
      at(300, 300, 30),
      SIZE,
    );
    expect(flick).toEqual({
      x1: 100,
      y1: 300,
      x2: 300,
      y2: 300,
      duration_ms: 50,
    });
    const slow = swipeOf(
      [at(100, 300, 0), at(150, 300, 3000)],
      at(300, 300, 6000),
      SIZE,
    );
    expect(slow).toEqual({
      x1: 100,
      y1: 300,
      x2: 300,
      y2: 300,
      duration_ms: 5000,
    });
  });
});
