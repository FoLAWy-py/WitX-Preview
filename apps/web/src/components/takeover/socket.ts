import { useEffect, useEffectEvent, useRef } from "react";
import type {
  TakeoverEnded,
  TakeoverFrame,
  TakeoverInputResult,
} from "@/api/types.gen";
import { useAuth } from "@/lib/auth";

export interface TakeoverSocket {
  /** Sends a message, if the socket is open. */
  send: (message: Record<string, unknown>) => void;
  /** Whether the socket is open now. */
  open: () => boolean;
}

/**
 * The takeover's connection: pictures, the results of inputs, and its end come in;
 * inputs go out. The server ends the takeover when the socket closes, so it is opened
 * once per takeover and never again while it lasts: the handlers may change (the
 * language shown, say) without reconnecting.
 */
export function useTakeoverSocket(
  takeoverId: string,
  on: {
    frame: (frame: TakeoverFrame) => void;
    result: (result: TakeoverInputResult) => void;
    /** The server ended it, and says why. */
    ended: (reason: string) => void;
    /** The connection closed. */
    closed: () => void;
  },
): TakeoverSocket {
  const { api } = useAuth();
  const socket = useRef<WebSocket | null>(null);
  const received = useEffectEvent(
    (message: TakeoverFrame | TakeoverInputResult | TakeoverEnded) => {
      if (message.type === "frame") on.frame(message);
      else if (message.type === "input_result") on.result(message);
      else if (message.type === "ended") on.ended(message.reason);
    },
  );
  const closed = useEffectEvent(() => on.closed());

  useEffect(() => {
    // A socket opened and closed at once (React mounts twice in development) would end
    // the takeover: connect a tick later.
    let ws: WebSocket | undefined;
    const timer = setTimeout(() => {
      // The session cookie signs the socket in; nothing to send first.
      ws = new WebSocket(api.takeoverSocketUrl(takeoverId));
      socket.current = ws;
      ws.onmessage = (event: MessageEvent<string>) => {
        let message;
        try {
          message = JSON.parse(event.data) as Parameters<typeof received>[0];
        } catch {
          return; // not a message of this protocol
        }
        received(message);
      };
      ws.onclose = () => closed();
    });
    return () => {
      clearTimeout(timer);
      if (ws) {
        // Detach first: a socket closed by this cleanup must not end the takeover shown.
        ws.onmessage = null;
        ws.onclose = null;
        ws.close();
      }
    };
  }, [api, takeoverId]);

  return {
    send: (message) => socket.current?.send(JSON.stringify(message)),
    open: () => socket.current?.readyState === WebSocket.OPEN,
  };
}
