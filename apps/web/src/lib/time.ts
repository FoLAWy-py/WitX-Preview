/** Moments on the wire are ISO strings that may be written in different zones (a
 * message in the server's, a result in UTC): they are compared as moments, never as
 * text. */

export function byTime(a: string, b: string): number {
  return Date.parse(a) - Date.parse(b);
}

export function isAfter(a: string, b: string): boolean {
  return Date.parse(a) > Date.parse(b);
}
