import { useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from "react";

export interface SegmentOption<T extends string> {
  value: T;
  label: ReactNode;
  /** Accessible name when the label is an icon. */
  title?: string;
  disabled?: boolean;
}

interface Props<T extends string> {
  options: readonly SegmentOption<T>[];
  value: T | null;
  onChange: (value: T) => void;
  /** "tabs" renders a tablist (arrow keys move between tabs). */
  role?: "group" | "tabs";
  label: string;
  className?: string;
  disabled?: boolean;
  id?: string;
  /** For tabs: the id of the panel each tab controls, by value. */
  panelId?: (value: T) => string;
  /** Clicking the selected option calls onChange again (motors: re-enable homes). */
  reselect?: boolean;
}

/**
 * A segmented control whose selection is a glass droplet that slides between
 * options. Also the tab bars. Nothing is highlighted while `value` is null.
 */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  role = "group",
  label,
  className,
  disabled,
  id,
  panelId,
  reselect,
}: Props<T>) {
  const group = useRef<HTMLDivElement>(null);
  const droplet = useRef<HTMLSpanElement>(null);
  const tabs = role === "tabs";

  useLayoutEffect(() => {
    const el = group.current;
    const drop = droplet.current;
    if (!el || !drop) return;
    const place = () => {
      const on = el.querySelector<HTMLElement>('[data-on="true"]');
      drop.style.opacity = on ? "1" : "0";
      if (!on || !on.offsetWidth) return;
      drop.style.width = `${on.offsetWidth}px`;
      drop.style.transform = `translateX(${on.offsetLeft}px)`;
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(el);
    return () => observer.disconnect();
  }, [value, options.length]);

  const onKey = (event: KeyboardEvent) => {
    if (!tabs || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
    event.preventDefault();
    const enabled = options.filter((o) => !o.disabled);
    const at = enabled.findIndex((o) => o.value === value);
    const next = enabled[(at + (event.key === "ArrowRight" ? 1 : enabled.length - 1)) % enabled.length];
    if (!next) return;
    onChange(next.value);
    group.current?.querySelector<HTMLElement>(`[data-value="${next.value}"]`)?.focus();
  };

  return (
    <div
      ref={group}
      id={id}
      className={`seg${className ? ` ${className}` : ""}`}
      role={tabs ? "tablist" : "group"}
      aria-label={label}
      onKeyDown={onKey}
    >
      <span className="droplet" ref={droplet} aria-hidden="true"></span>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            data-value={o.value}
            data-on={on}
            role={tabs ? "tab" : undefined}
            aria-selected={tabs ? on : undefined}
            aria-controls={tabs && panelId ? panelId(o.value) : undefined}
            aria-pressed={tabs ? undefined : on}
            tabIndex={tabs && !on ? -1 : undefined}
            aria-label={o.title}
            title={o.title}
            disabled={disabled || o.disabled}
            onClick={() => {
              if (!on || reselect) onChange(o.value);
            }}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
