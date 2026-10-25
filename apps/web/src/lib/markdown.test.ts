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

  it("does not turn a javascript: link, image or autolink into something to open", () => {
    const html = renderToStaticMarkup(
      createElement(Markdown, {
        text: [
          "[x](javascript:alert(1))",
          "![y](javascript:alert(2))",
          "<javascript:alert(3)>",
          "[z](JaVaScRiPt:alert(4))",
        ].join("\n\n"),
      }),
    );
    expect(html).toContain(">x<");
    expect(html.toLowerCase()).not.toMatch(/(href|src)="\s*javascript:/);
  });
});
