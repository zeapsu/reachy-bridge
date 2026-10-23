import { useEffect, useState } from "react";
import { THEME_SCHEMES, applyScheme, currentScheme } from "../logic/theme.ts";

const POPOVER_ID = "theme-pop";

function popover(): HTMLElement | null {
  return document.getElementById(POPOVER_ID);
}

/**
 * The popover lives in the top layer (escaping the status bar's
 * overflow:hidden), so it is positioned under the button by hand.
 */
function placePopover(button: HTMLElement) {
  const pop = popover();
  if (!pop) return;
  const rect = button.getBoundingClientRect();
  pop.style.top = `${rect.bottom + 10}px`;
  pop.style.right = `${window.innerWidth - rect.right}px`;
  pop.style.left = "auto";
}

export function ThemeButton() {
  return (
    <button
      className="theme-btn"
      id="theme-btn"
      type="button"
      popoverTarget={POPOVER_ID}
      aria-haspopup="menu"
      aria-label="Colour theme"
      title="Colour theme"
      onClick={(event) => {
        const button = event.currentTarget;
        requestAnimationFrame(() => placePopover(button));
      }}
    >
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <rect x="3.5" y="3.5" width="7.5" height="7.5" rx="2.2" />
        <rect x="13" y="3.5" width="7.5" height="7.5" rx="2.2" />
        <rect x="3.5" y="13" width="7.5" height="7.5" rx="2.2" />
        <rect x="13" y="13" width="7.5" height="7.5" rx="2.2" />
      </svg>
      <span className="cur"></span>
    </button>
  );
}

/** Colour theme menu. A pure UI preference: works with the daemon offline. */
export function ThemePopover() {
  // The saved theme is applied before first paint by the inline <head> script.
  const [scheme, setScheme] = useState(currentScheme);

  useEffect(() => {
    const onResize = () => {
      const pop = popover();
      const button = document.getElementById("theme-btn");
      if (pop && button && pop.matches(":popover-open")) placePopover(button);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return (
    <div className="theme-pop" id={POPOVER_ID} popover="auto" role="menu" aria-label="Colour theme">
      <div className="ttl">Colour theme</div>
      {THEME_SCHEMES.map((s) => (
        <button
          key={s.id}
          className="theme-opt"
          type="button"
          role="menuitemradio"
          data-scheme={s.id}
          aria-checked={s.id === scheme}
          onClick={() => {
            applyScheme(s.id);
            setScheme(s.id);
            popover()?.hidePopover?.();
          }}
        >
          <span className="sw" style={{ background: s.sw }}></span>
          <span className="nm">{s.label}</span>
          <svg className="chk" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M20 6 9 17l-5-5"></path>
          </svg>
        </button>
      ))}
    </div>
  );
}
