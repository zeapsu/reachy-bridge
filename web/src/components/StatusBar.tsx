import { ThemeButton } from "./ThemePicker.tsx";

interface Props {
  daemonOnline: boolean;
  /** Name of the app that owns the robot, or null when free. */
  activeApp: string | null;
}

export function StatusBar({ daemonOnline, activeApp }: Props) {
  return (
    <header className="glass statusbar instrument">
      <div className="brand" aria-label="reachy·controller">
        <span className="mark">
          reachy<b>·</b>controller
        </span>
      </div>
      <div className="pillbar">
        <span className="pill">
          <span className={`dot ${daemonOnline ? "ok" : "error"}`} id="daemon-dot"></span>daemon{" "}
          <span className="val" id="daemon-value">{daemonOnline ? "online" : "offline"}</span>
        </span>
        <span className="pill">
          <span className={`dot ${activeApp ? "live" : "ok"}`} id="robot-dot"></span>robot{" "}
          <span className="val" id="robot-value">{activeApp ?? "free"}</span>
        </span>
        <span className="pill">
          <span className="dot ok"></span>link <span className="val">tailnet</span>
        </span>
      </div>
      <ThemeButton />
    </header>
  );
}
