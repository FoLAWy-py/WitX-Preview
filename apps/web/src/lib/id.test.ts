import { describe, expect, it, vi } from "vitest";
import { newId } from "./id";

describe("IDs", () => {
  it("are random version 4 UUIDs", () => {
    const ids = new Set(Array.from({ length: 1000 }, newId));
    expect(ids.size).toBe(1000);
    for (const id of ids)
      expect(id).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      );
  });

  it("are made on plain http pages, where crypto.randomUUID is missing", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: crypto.getRandomValues.bind(crypto),
    });
    try {
      expect(newId()).toHaveLength(36);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
