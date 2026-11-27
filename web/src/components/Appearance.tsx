import { useId, type CSSProperties } from "react";
import type { Appearance as AppearanceState } from "../hooks/useAppearance.ts";
import { DIM_MAX, SCHEMES, WALLS, type Mode } from "../logic/appearance.ts";
import { Panel } from "./Panel.tsx";
import { Segmented } from "./Segmented.tsx";
import { ROW_ICONS, SettingsRow } from "./SettingsRow.tsx";
import "./Appearance.css";

const MODES: { value: Mode; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

/** The page's wallpaper: a built-in gradient or the viewer's photo, dimmed. */
export function Wallpaper({ look }: { look: AppearanceState }) {
  const preset = WALLS.find((w) => w.id === look.wall);
  const style = {
    "--wall-img": look.wall === "photo" && look.photo ? `url("${look.photo}")` : "none",
    ...(preset?.css ? { "--wall": preset.css } : {}),
    "--dim-a": look.dim / 100,
  } as CSSProperties;
  return <div className="wall" style={style} aria-hidden="true"></div>;
}

/** This browser's look, one setting per row. */
export function AppearanceRows({ look }: { look: AppearanceState }) {
  const dimLabel = useId();
  return (
    <>
      <ul className="set-list">
        <SettingsRow icon={ROW_ICONS.mode} label="Light or dark" stack>
          <Segmented label="Light or dark" options={MODES} value={look.mode} onChange={look.setMode} />
        </SettingsRow>
        <SettingsRow icon={ROW_ICONS.accent} label="Accent">
          <div className="accents" role="group" aria-label="Accent colour">
            {SCHEMES.map((s) => (
              <button
                key={s.id}
                type="button"
                className="accent press"
                style={{ background: s.swatch }}
                aria-pressed={look.scheme === s.id}
                aria-label={s.label}
                title={s.label}
                onClick={() => look.setScheme(s.id)}
              ></button>
            ))}
          </div>
        </SettingsRow>
        <SettingsRow icon={ROW_ICONS.background} label="Background" stack>
          <div className="walls" role="group" aria-label="Background">
            {WALLS.map((w) => (
              <button
                key={w.id}
                type="button"
                className={`swatch press wall-${w.id}`}
                style={w.css ? { background: w.css } : undefined}
                aria-pressed={look.wall === w.id}
                aria-label={w.label}
                title={w.label}
                onClick={() => look.setWall(w.id)}
              ></button>
            ))}
            {look.photo ? (
              <button
                type="button"
                className="swatch press"
                style={{ backgroundImage: `url("${look.photo}")` }}
                aria-pressed={look.wall === "photo"}
                aria-label="Your photo"
                title="Your photo"
                onClick={() => look.setWall("photo")}
              ></button>
            ) : null}
            <label className="swatch add press" title="Use your own photo">
              <span aria-hidden="true">+</span>
              <input
                type="file"
                accept="image/*"
                aria-label="Use your own photo"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) look.choosePhoto(file);
                  e.target.value = "";
                }}
              />
            </label>
          </div>
        </SettingsRow>
        <SettingsRow icon={ROW_ICONS.dim} label="Dim background" labelId={dimLabel} stack>
          <span className="set-dim">
            <output>{look.dim}</output>
            <input
              type="range"
              min="0"
              max={DIM_MAX}
              value={look.dim}
              aria-labelledby={dimLabel}
              style={{ "--fill": `${(look.dim / DIM_MAX) * 100}%` } as CSSProperties}
              onChange={(e) => look.setDim(Number(e.target.value))}
            />
          </span>
        </SettingsRow>
      </ul>
      <p className="note error" role="alert">
        {look.photoError}
      </p>
    </>
  );
}

/** Appearance as a sheet of its own (the profile chip's sheet). */
export function AppearancePanel({ look }: { look: AppearanceState }) {
  const title = useId();
  return (
    <Panel title="Appearance" titleId={title}>
      <AppearanceRows look={look} />
    </Panel>
  );
}
