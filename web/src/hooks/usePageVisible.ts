import { useSyncExternalStore } from "react";

const subscribe = (callback: () => void) => {
  document.addEventListener("visibilitychange", callback);
  return () => document.removeEventListener("visibilitychange", callback);
};

/** True while the tab is visible; live connections pause while hidden. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(subscribe, () => !document.hidden);
}
