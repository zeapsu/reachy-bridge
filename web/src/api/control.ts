// Control (#221, #245): who drives the robot in admin mode. Any paired
// browser takes control while nobody has it; the admin can take it over.

export const controlPaths = {
  control: "/api/control",
  controlTake: "/api/control/take",
  /** Takes control only while nobody has it (the dashboard's take for a robot action). */
  controlTakeIfFree: "/api/control/take?if_free=true",
  controlRelease: "/api/control/release",
} as const;

export interface ControlHolder {
  person: string;
  /** The device's name. */
  device: string;
  admin: boolean;
  since: number;
  /** This browser holds control. */
  you: boolean;
}

/** GET /api/control */
export interface ControlView {
  admin_mode: boolean;
  now?: number;
  holder?: ControlHolder | null;
  you?: {
    admin: boolean;
    person: string | null;
    can_take: boolean;
  };
}
