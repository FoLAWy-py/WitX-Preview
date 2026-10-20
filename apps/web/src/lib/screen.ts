/** How a phone's picture is drawn in a takeover: as large as the room allows, never
 * larger than the picture itself, inside a bezel whose thickness and corners follow the
 * size it is drawn at, with room on both sides for the side keys when they are drawn on
 * the phone's edges. */

export type Box = { width: number; height: number };

export type Fitted = {
  /** The picture, in page pixels. */
  width: number;
  height: number;
  /** The bezel around it, and the picture's corner radius. */
  bezel: number;
  radius: number;
};

/** How far the side keys stick out beyond the bezel, on each side. */
export const KEY_ROOM = 16;

const BEZEL_MIN = 5;
const BEZEL_MAX = 12;

function bezelFor(width: number, height: number): number {
  const short = Math.min(width, height);
  return Math.max(BEZEL_MIN, Math.min(BEZEL_MAX, Math.round(short * 0.028)));
}

/** The largest size, at the picture's own shape, that fits `room` with its bezel (and
 * the side keys' room when `keys`). */
export function fitScreen(picture: Box, room: Box, keys: boolean): Fitted {
  const side = keys ? KEY_ROOM : 0;
  let bezel = BEZEL_MAX;
  let scale = 0;
  // The bezel depends on the size, and the size on the bezel: twice settles it.
  for (let pass = 0; pass < 2; pass += 1) {
    scale = Math.max(
      0,
      Math.min(
        (room.width - 2 * side - 2 * bezel) / picture.width,
        (room.height - 2 * bezel) / picture.height,
        1,
      ),
    );
    bezel = bezelFor(picture.width * scale, picture.height * scale);
  }
  const width = Math.floor(picture.width * scale);
  const height = Math.floor(picture.height * scale);
  return {
    width,
    height,
    bezel,
    radius: Math.round(Math.min(width, height) * 0.11),
  };
}
