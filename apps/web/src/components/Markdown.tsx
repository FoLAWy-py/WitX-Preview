import { useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { Check, Copy, ExternalLink } from "lucide-react";
import "katex/dist/katex.min.css";
import { useI18n } from "@/i18n";
import { normalizeMath } from "@/lib/math";

/** The text of a React subtree, for copying a code block. */
function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node)
    return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const code = Array.isArray(children) ? children[0] : children;
  const className =
    (code && typeof code === "object" && "props" in code
      ? (code.props as { className?: string }).className
      : "") ?? "";
  const language = /language-([\w+#-]+)/.exec(className)?.[1];
  return (
    <div className="md-code group/code my-3 overflow-hidden rounded-lg border border-line bg-sunken">
      <div className="flex h-8 items-center justify-between border-b border-line px-3 text-[12px] text-faint">
        <span className="font-mono">{language ?? t("md.code")}</span>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard?.writeText(textOf(children)).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-line hover:text-fg"
        >
          {copied ? (
            <Check size={13} aria-hidden />
          ) : (
            <Copy size={13} aria-hidden />
          )}
          {copied ? t("md.copied") : t("md.copy")}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 text-[13px] leading-relaxed">
        {children}
      </pre>
    </div>
  );
}

const components: Components = {
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
      <ExternalLink
        size={11}
        className="ml-0.5 inline align-baseline"
        aria-hidden
      />
    </a>
  ),
  // The agent's text is not trusted: no image is fetched from wherever it points.
  img: ({ src, alt }) =>
    typeof src === "string" ? (
      <a href={src} target="_blank" rel="noopener noreferrer nofollow">
        {alt || src}
      </a>
    ) : null,
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table>{children}</table>
    </div>
  ),
};

/**
 * Text written by the agent: Markdown with tables, code blocks highlighted in many
 * languages, and math ($…$, $$…$$). Raw HTML in the text is not rendered.
 */
export default function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[
          remarkGfm,
          // Bold and italics next to Chinese punctuation ("**焦点：**新加坡").
          remarkCjkFriendly,
          [remarkMath, { singleDollarTextMath: true }],
        ]}
        rehypePlugins={[rehypeKatex, [rehypeHighlight, { detect: true }]]}
        components={components}
      >
        {normalizeMath(text)}
      </ReactMarkdown>
    </div>
  );
}
