/**
 * Math as models write it, in the form the Markdown renderer reads: \[…\] and a line
 * that is only $$…$$ become display blocks, \(…\) becomes inline $…$. Code (fenced or
 * inline) is left as it is.
 */
export function normalizeMath(text: string): string {
  return text
    .split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((part, i) => {
      if (i % 2 === 1) return part; // code
      return part
        .replace(
          /\\\[([\s\S]+?)\\\]/g,
          (_, m: string) => `\n$$\n${m.trim()}\n$$\n`,
        )
        .replace(/\\\(([\s\S]+?)\\\)/g, (_, m: string) => `$${m.trim()}$`)
        .replace(
          /^[ \t]*\$\$([^\n]+?)\$\$[ \t]*$/gm,
          (_, m: string) => `$$\n${m.trim()}\n$$`,
        );
    })
    .join("");
}
