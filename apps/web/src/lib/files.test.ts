import { describe, expect, it } from "vitest";
import type { FileView, TaskDetail, TaskInputView } from "@/api/types.gen";
import {
  byteSize,
  filesOf,
  fitFiles,
  MAX_FILE_BYTES,
  MAX_FILE_SIZE,
} from "./files";

const input = (seq: number, kind: TaskInputView["kind"]) =>
  ({ seq, kind, text: "", created_at: "" }) as TaskInputView;
const file = (name: string, input_seq: number | null) =>
  ({
    id: name,
    name,
    size: 1,
    content_type: "",
    input_seq,
    created_at: "",
  }) as FileView;

describe("files", () => {
  it("reads a size as a phone shows it", () => {
    expect(byteSize(512)).toBe("512 B");
    expect(byteSize(10_240)).toBe("10.2 KB");
    expect(byteSize(30_000_000)).toBe("30.0 MB");
  });

  it("shows a file with its message, and those of the first message with the request", () => {
    const inputs = [input(1, "goal"), input(2, "note"), input(3, "goal")];
    const task = {
      inputs,
      files: [file("brief.txt", null), file("photo.jpg", 2)],
    } as unknown as TaskDetail;
    expect(filesOf(task, inputs[0]).map((f) => f.name)).toEqual(["brief.txt"]);
    expect(filesOf(task, inputs[1]).map((f) => f.name)).toEqual(["photo.jpg"]);
    expect(filesOf(task, inputs[2])).toEqual([]);
  });

  it("leaves out a file over the size limit and any past the per-message count", () => {
    const big = { name: "video.mov", size: MAX_FILE_BYTES + 1 };
    const small = (name: string) => ({ name, size: 10 });
    const fit = fitFiles([small("a"), big, small("b"), small("c")], 8);
    expect(fit.take.map((f) => f.name)).toEqual(["a", "b"]);
    expect(fit.tooLarge.map((f) => f.name)).toEqual(["video.mov"]);
    expect(fit.over).toBe(1);
    expect(
      fitFiles([{ name: "edge", size: MAX_FILE_BYTES }], 0).take,
    ).toHaveLength(1);
    expect(MAX_FILE_SIZE).toBe("100 MB");
  });
});
