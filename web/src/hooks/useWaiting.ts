import { useCallback, useState } from "react";
import { apiJson, paths } from "../api.ts";
import { isWaitingList, type WaitingDevice } from "../logic/access.ts";
import { usePoll } from "./usePoll.ts";

const WAITING_POLL_MS = 3000;

/**
 * Browsers waiting for their code to be approved (#334), read every few
 * seconds while `live`: an admin with System open. Empty when the read
 * fails or isn't this browser's to make.
 */
export function useWaiting(live: boolean): WaitingDevice[] {
  const [waiting, setWaiting] = useState<WaitingDevice[]>([]);
  const read = useCallback(async () => {
    try {
      const body = await apiJson<unknown>(paths.authPending);
      setWaiting(isWaitingList(body) ? body.pending : []);
    } catch {
      setWaiting([]);
    }
  }, []);
  usePoll(read, WAITING_POLL_MS, live);
  return live ? waiting : [];
}
