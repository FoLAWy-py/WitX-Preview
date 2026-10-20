/** How often the device panel asks for a live picture of the task's phone. */
export const LIVE_MS = 1_000;
/** Asks in a row without a picture before the panel says there is none. */
export const LIVE_GIVE_UP = 3;
/** The longest wait between asks while pictures do not come. */
export const LIVE_MAX_MS = 10_000;

/** How long to wait before asking for the next live picture after `misses` asks in a row
 * brought none: every second at first, then twice as long each time up to LIVE_MAX_MS,
 * so a phone that sends nothing is not asked every second for nothing. */
export function liveDelay(misses: number): number {
  if (misses < LIVE_GIVE_UP) return LIVE_MS;
  return Math.min(LIVE_MAX_MS, LIVE_MS * 2 ** (misses - LIVE_GIVE_UP + 1));
}

/** The bytes of a base64 string (a takeover picture arrives as base64 in JSON). */
export function base64Bytes(text: string): Uint8Array<ArrayBuffer> {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
