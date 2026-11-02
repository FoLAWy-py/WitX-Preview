/**
 * Whether every word of `query` is in one of `fields`, ignoring case: "pay wechat"
 * finds a skill named "pay" that works in WeChat. An empty query matches everything.
 */
export function matchesQuery(
  query: string,
  fields: readonly string[],
): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const text = fields.join("\n").toLowerCase();
  return words.every((w) => text.includes(w));
}
