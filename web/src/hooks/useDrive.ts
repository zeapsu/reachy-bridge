import { useCallback, useEffect, useRef, useState } from "react";
import {
  CENTRED,
  DEFAULT_LIMITS,
  HOME,
  driveMessage,
  parseLimits,
  samePose,
  stepToward,
  stickPose,
  type Pose,
  type Sticks,
} from "../logic/drive.ts";

const TICK_MS = 33;
/** Sticks centred and the robot home this long ends the session. */
const IDLE_MS = 800;

export type DrivePhase = "idle" | "connecting" | "live";

export interface DriveState {
  phase: DrivePhase;
  error: string | null;
  /** A pad or slider (by name) is held: open the session if none is open. */
  hold: (control: string) => void;
  /** That control was let go. */
  release: (control: string) => void;
  /** Change part of the sticks; the robot follows at its speed limit. */
  move: (change: Partial<Sticks>) => void;
  /** Centre everything and end the session; the controller homes the robot. */
  home: () => void;
}

const wsUrl = () => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/drive/ws`;

/**
 * One drive session over `/api/drive/ws` (#152). It opens on the first touch,
 * streams the stick pose at ~30 Hz while it changes, and closes once nothing
 * has been held and the robot has been at the home pose for IDLE_MS, which
 * hands it to the controller's return home. It also closes when `allowed`
 * goes false.
 */
export function useDrive(allowed: boolean): DriveState {
  const [phase, setPhase] = useState<DrivePhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const sticks = useRef<Sticks>(CENTRED);
  const pose = useRef<Pose>(HOME);
  const sent = useRef<Pose | null>(null);
  const limits = useRef(DEFAULT_LIMITS);
  /** Targets wait for the `ready` message, which carries the limits. */
  const limitsRead = useRef(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const last = useRef(0);
  const idleSince = useRef(0);
  const held = useRef(new Set<string>());

  const close = useCallback(() => {
    if (timer.current !== null) clearInterval(timer.current);
    timer.current = null;
    const ws = socket.current;
    socket.current = null;
    if (ws) {
      ws.onclose = ws.onmessage = ws.onerror = null;
      ws.close();
    }
    sticks.current = CENTRED;
    pose.current = HOME;
    held.current.clear();
    sent.current = null;
    setPhase("idle");
  }, []);

  const tick = useCallback(() => {
    const now = performance.now();
    const ws = socket.current;
    // Nothing ramps until the controller is ready: the first target sent is
    // one step from home, however long the connection took.
    if (!ws || ws.readyState !== WebSocket.OPEN || !limitsRead.current) {
      last.current = now;
      return;
    }
    const goal = stickPose(sticks.current, limits.current);
    pose.current = stepToward(pose.current, goal, (now - last.current) / 1000);
    last.current = now;
    if (!sent.current || !samePose(sent.current, pose.current)) {
      ws.send(driveMessage(pose.current));
      sent.current = pose.current;
    }
    if (!held.current.size && samePose(pose.current, HOME)) {
      if (!idleSince.current) idleSince.current = now;
      else if (now - idleSince.current > IDLE_MS) close();
    } else {
      idleSince.current = 0;
    }
  }, [close]);

  const hold = useCallback((control: string) => {
    if (!allowed) return;
    held.current.add(control);
    idleSince.current = 0;
    if (socket.current) return;
    setError(null);
    setPhase("connecting");
    limitsRead.current = false;
    const ws = new WebSocket(wsUrl());
    socket.current = ws;
    let opened = false;
    ws.onopen = () => {
      opened = true;
    };
    ws.onmessage = (event) => {
      let message: { type?: string; detail?: unknown } | null = null;
      try {
        message = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (message?.type === "ready") {
        limits.current = parseLimits(message);
        limitsRead.current = true;
        pose.current = HOME;
        last.current = performance.now();
        setPhase("live");
      } else if (message?.type === "error") {
        setError(typeof message.detail === "string" ? message.detail : "Drive stopped.");
      }
    };
    ws.onclose = () => {
      // A refused upgrade gives the page no status to read.
      if (!opened) setError("Couldn't start driving. If the robot is still going home, try again in a moment.");
      socket.current = null;
      close();
    };
    last.current = performance.now();
    if (timer.current === null) timer.current = setInterval(tick, TICK_MS);
  }, [allowed, close, tick]);

  const release = useCallback((control: string) => {
    held.current.delete(control);
  }, []);

  const move = useCallback((change: Partial<Sticks>) => {
    sticks.current = { ...sticks.current, ...change };
  }, []);

  useEffect(() => {
    if (!allowed) close();
  }, [allowed, close]);
  useEffect(() => close, [close]);

  return { phase, error, hold, release, move, home: close };
}
