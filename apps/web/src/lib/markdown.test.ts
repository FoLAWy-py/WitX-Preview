import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import Markdown from "@/components/Markdown";

describe("agent text", () => {
  it("makes bold next to Chinese punctuation bold, not literal asterisks", () => {
    const html = renderToStaticMarkup(
      createElement(Markdown, { text: "- **烟霾仍是焦点：**新加坡空气质量" }),
    );
    expect(html).toContain("<strong>烟霾仍是焦点：</strong>新加坡");
    expect(html).not.toContain("**");
  });
});
