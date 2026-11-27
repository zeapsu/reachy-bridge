import type { ReactNode } from "react";
import "./SettingsRow.css";

const stroke = { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;

export const ROW_ICONS = {
  sim: (
    <svg {...stroke}>
      <path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z" />
      <path d="M4 7.5 12 12l8-4.5M12 12v9" />
    </svg>
  ),
  sound: (
    <svg {...stroke}>
      <path d="M4 9v6h4l5 4V5L8 9H4Z" />
      <path d="M17 9a4 4 0 0 1 0 6" />
    </svg>
  ),
  mode: (
    <svg {...stroke}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 3.5a8.5 8.5 0 0 1 0 17Z" fill="currentColor" stroke="none" />
    </svg>
  ),
  accent: (
    <svg {...stroke}>
      <path d="M12 3.5c3.5 4 6 6.8 6 10a6 6 0 0 1-12 0c0-3.2 2.5-6 6-10Z" />
    </svg>
  ),
  background: (
    <svg {...stroke}>
      <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
      <circle cx="9" cy="10" r="1.5" />
      <path d="m5 17 5-4.5 3.5 3 2.5-2 3.5 3.5" />
    </svg>
  ),
  dim: (
    <svg {...stroke}>
      <circle cx="12" cy="12" r="3.5" />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4" />
    </svg>
  ),
  warn: (
    <svg {...stroke} strokeWidth={2.2}>
      <path d="M12 4 2.5 20h19L12 4Z" />
      <path d="M12 10v4.5M12 17.2v.3" />
    </svg>
  ),
  manual: (
    <svg {...stroke}>
      <path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5v-15Z" />
      <path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19" />
    </svg>
  ),
} as const;

interface Props {
  icon: ReactNode;
  label: string;
  /** The label's id, for the control it names. */
  labelId?: string;
  /** A short second line: why it's dimmed, what is happening. */
  sub?: ReactNode;
  subId?: string;
  /** The second line is a warning. */
  warn?: boolean;
  /** Unavailable right now: the icon and label dim. */
  off?: boolean;
  /** On a phone the control goes under the label. */
  stack?: boolean;
  children: ReactNode;
}

/** One setting: an icon, its name, and its control on the right. */
export function SettingsRow({ icon, label, labelId, sub, subId, warn, off, stack, children }: Props) {
  return (
    <li className={`set-row${off ? " off" : ""}${stack ? " stack" : ""}`}>
      <span className="set-ico">{icon}</span>
      <span className="set-label">
        <span className="set-name" id={labelId}>
          {label}
        </span>
        {sub ? (
          <span className={`set-sub${warn ? " warn" : ""}`} id={subId}>
            {sub}
          </span>
        ) : null}
      </span>
      <span className="set-trail">{children}</span>
    </li>
  );
}
