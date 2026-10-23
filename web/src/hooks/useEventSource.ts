import { useEffect, useRef } from "react";

export type SseHandlers = Record<string, (event: MessageEvent) => void> & {
  /** Called on every transport error (the browser retries on its own). */
  error?: () => void;
};

/** Delay before re-creating a source the browser gave up on (non-2xx, bad type). */
export const SSE_RECONNECT_MS = 2000;

/** Parse an SSE `data:` field as JSON; null on bad payloads. */
export function readEventPayload<T = unknown>(event: MessageEvent): T | null {
  try {
    return JSON.parse(event.data) as T;
  } catch {
    return null;
  }
}

/**
 * Subscribe to a Server-Sent Events stream while `url` is non-null.
 *
 * Handlers are read through a ref, so they may change every render without
 * reopening the stream. EventSource reconnects by itself after a dropped
 * connection; when it gives up (readyState CLOSED, e.g. the controller
 * answered 5xx), this hook opens a fresh one after SSE_RECONNECT_MS.
 * A handler that throws never takes the stream down.
 */
export function useEventSource(url: string | null, handlers: SseHandlers): void {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const events = Object.keys(handlers)
    .filter((name) => name !== "error")
    .sort()
    .join(",");

  useEffect(() => {
    if (!url) return;
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const open = () => {
      retry = null;
      if (disposed) return;
      try {
        source = new EventSource(url);
      } catch {
        handlersRef.current.error?.();
        retry = setTimeout(open, SSE_RECONNECT_MS);
        return;
      }
      for (const name of events ? events.split(",") : []) {
        source.addEventListener(name, (event) => {
          try {
            handlersRef.current[name]?.(event as MessageEvent);
          } catch {
            // Bad event payloads should not take down the dashboard.
          }
        });
      }
      const current = source;
      current.addEventListener("error", () => {
        try {
          handlersRef.current.error?.();
        } catch {
          // ignore
        }
        if (current.readyState === EventSource.CLOSED && !disposed && retry === null) {
          current.close();
          retry = setTimeout(open, SSE_RECONNECT_MS);
        }
      });
    };

    open();
    return () => {
      disposed = true;
      if (retry !== null) clearTimeout(retry);
      source?.close();
    };
  }, [url, events]);
}
