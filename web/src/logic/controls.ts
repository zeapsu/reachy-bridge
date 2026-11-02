import type { Kinematics } from "../api.ts";

export const KINEMATICS_ENGINES = [
  { engine: "AnalyticalKinematics", label: "Analytical" },
  { engine: "Placo", label: "Placo" },
] as const;

export interface KinematicsView {
  buttons: { engine: string; label: string; active: boolean; disabled: boolean; title: string }[];
  gravityDisabled: boolean;
  hint: string;
}

/**
 * Kinematics segment, gravity switch and hint. Gravity compensation only works
 * on Placo, and is off while an app runs (the motors would ignore its moves),
 * except that it can always be switched off.
 */
export function kinematicsView(
  kin: Kinematics | null,
  controlsDisabled: boolean,
  appActive: boolean,
  gravityChecked: boolean,
): KinematicsView {
  const available = new Set(kin?.available ?? []);
  const placoActive = Boolean(kin && kin.active === "Placo");
  let hint = "";
  if (!kin) hint = controlsDisabled ? "" : "Kinematics unavailable";
  else if (kin.active && kin.active !== kin.engine) hint = "At next power-on";
  else if (!placoActive) hint = "Gravity needs Placo";
  return {
    buttons: KINEMATICS_ENGINES.map(({ engine, label }) => ({
      engine,
      label,
      active: kin?.engine === engine,
      disabled: controlsDisabled || !kin || !available.has(engine),
      title: available.has(engine) ? "" : "Not installed",
    })),
    gravityDisabled: controlsDisabled || !placoActive || (appActive && !gravityChecked),
    hint,
  };
}

/** Whether a kinematics click needs a switch, and whether it restarts the robot. */
export function kinematicsSwitch(kin: Kinematics, engine: string): { needed: boolean; robotOn: boolean } {
  const needed = !(engine === kin.engine && (kin.active === null || kin.active === engine));
  return { needed, robotOn: kin.active !== null };
}

export function clampVolume(value: unknown): number {
  return Math.min(100, Math.max(0, Number(value)));
}
