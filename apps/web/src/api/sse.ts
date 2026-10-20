/** A minimal server-sent events parser (we read the stream with fetch, so the
 * Authorization header can be sent; EventSource cannot do that). */

export interface SseMessage {
  id?: string;
  event?: string;
  data: string;
}

/** Splits complete messages off the buffer; returns them and the unfinished rest. */
export function parseSse(buffer: string): {
  messages: SseMessage[];
  rest: string;
} {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const messages: SseMessage[] = [];
  for (const block of blocks) {
    const message: SseMessage = { data: "" };
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "data") data.push(value);
      else if (field === "id") message.id = value;
      else if (field === "event") message.event = value;
    }
    if (data.length === 0) continue;
    message.data = data.join("\n");
    messages.push(message);
  }
  return { messages, rest };
}
