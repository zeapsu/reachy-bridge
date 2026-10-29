// Control time (#221): who drives the robot in admin mode.

export const controlPaths = {
  control: "/api/control",
  controlTake: "/api/control/take",
  controlRelease: "/api/control/release",
  controlRequest: "/api/control/request",
  controlRefuse: (person: string) => `/api/control/requests/${encodeURIComponent(person)}/refuse`,
  controlGrants: "/api/control/grants",
  controlGrant: (id: string) => `/api/control/grants/${encodeURIComponent(id)}`,
} as const;

export interface ControlGrant {
  id: string;
  person: string;
  /** Unix seconds. */
  from: number;
  /** Unix seconds; null for no end. */
  until: number | null;
  by: string;
}

export interface ControlAsk {
  person: string;
  device: string;
  at: number;
  /** null: no end asked for. */
  minutes: number | null;
  note: string;
}

export interface ControlHolder {
  person: string;
  /** The device's name. */
  device: string;
  admin: boolean;
  since: number;
  until: number | null;
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
    grant: ControlGrant | null;
    can_take: boolean;
    cooldown_until: number | null;
  };
  /** An admin sees every grant and request; others their own. */
  grants?: ControlGrant[];
  requests?: ControlAsk[];
}
