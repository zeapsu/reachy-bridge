import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { reduceMotion } from "../hooks/reduceMotion.ts";
import type { ExpressionsState } from "../hooks/useExpressions.ts";
import { MOVE_DATASETS, chipsDisabled, expressionHint } from "../logic/expressions.ts";
import { carouselMoves, moveLabel, type MoveKind } from "../logic/moves.ts";
import { Segmented } from "./Segmented.tsx";
import "./ExpressionsPanel.css";

interface Props {
  expr: ExpressionsState;
  daemonOnline: boolean;
  controlBusy: boolean;
  appActive: boolean;
}

const KINDS: { value: MoveKind; label: string }[] = [
  { value: "emotions", label: "Emotions" },
  { value: "dances", label: "Dances" },
];
const DATASET: Record<MoveKind, (typeof MOVE_DATASETS)[number]> = {
  emotions: MOVE_DATASETS[0],
  dances: MOVE_DATASETS[1],
};
/** Distance (px) from the centre slot over which items shrink and fade. */
const FALLOFF = 150;

/**
 * Expressions as an emoji carousel, like Pollen's desktop app: swipe or use
 * the arrow keys, and items grow as they reach the centre slot. Tapping the
 * centred one (or Play) plays it.
 */
export function ExpressionsPanel({ expr, daemonOnline, controlBusy, appActive }: Props) {
  const [kind, setKind] = useState<MoveKind>("emotions");
  const [centred, setCentred] = useState(0);
  const track = useRef<HTMLDivElement>(null);
  const frame = useRef(0);

  const dataset = DATASET[kind];
  const moves = useMemo(() => carouselMoves(expr.moves[dataset.id] ?? [], kind), [expr.moves, dataset.id, kind]);
  const disabled = chipsDisabled({ daemonOnline, controlBusy, appActive, exprBusy: expr.busy !== null });
  const hint = expressionHint({ daemonOnline, loaded: expr.loaded, appActive, busy: expr.busy, error: expr.error });
  const picked = moves[Math.min(centred, moves.length - 1)];

  // Items grow and brighten as they near the centre slot.
  const magnify = useCallback(() => {
    frame.current = 0;
    const el = track.current;
    // Hidden (another tab): nothing to measure; the resize on show re-runs this.
    if (!el || el.clientWidth === 0) return;
    const mid = el.scrollLeft + el.clientWidth / 2;
    let best = 0;
    let bestDistance = Infinity;
    Array.from(el.children as HTMLCollectionOf<HTMLElement>).forEach((item, i) => {
      const distance = Math.abs(item.offsetLeft + item.offsetWidth / 2 - mid);
      const t = Math.max(0, 1 - distance / FALLOFF);
      item.style.setProperty("--k", (0.72 + 0.48 * t * t).toFixed(3));
      item.style.setProperty("--o", (0.35 + 0.65 * t).toFixed(3));
      if (distance < bestDistance) {
        bestDistance = distance;
        best = i;
      }
    });
    setCentred(best);
  }, []);

  const queueMagnify = () => {
    if (!frame.current) frame.current = requestAnimationFrame(magnify);
  };
  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  // Showing the tab (or resizing) changes the geometry without a scroll.
  useEffect(() => {
    const el = track.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      if (!frame.current) frame.current = requestAnimationFrame(magnify);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [magnify]);

  // A new list (dataset switch, first load) starts at the first item.
  useLayoutEffect(() => {
    if (track.current) track.current.scrollLeft = 0;
    magnify();
  }, [moves, magnify]);

  const centre = (i: number) => {
    const el = track.current;
    const item = el?.children[i] as HTMLElement | undefined;
    if (!el || !item) return;
    el.scrollTo({
      left: item.offsetLeft - el.clientWidth / 2 + item.offsetWidth / 2,
      behavior: reduceMotion ? "auto" : "smooth",
    });
  };

  const play = () => {
    if (!picked || disabled || expr.busy !== null) return;
    expr.play(dataset.dataset, picked.name);
  };

  const onKey = (event: KeyboardEvent) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      centre(Math.max(0, Math.min(moves.length - 1, centred + (event.key === "ArrowRight" ? 1 : -1))));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      play();
    }
  };

  const playing = expr.playing;
  const showCancel = !expr.cancelHidden;
  const title = playing ? moveLabel(playing.move) : (picked?.label ?? "—");
  const meta = playing
    ? "Playing…"
    : expr.busy === "returning"
      ? "Returning home…"
      : picked
        ? `${kind === "emotions" ? "Emotion" : "Dance"} · ${centred + 1} of ${moves.length}`
        : "";

  return (
    <div className="pane expressions">
      <Segmented label="Expression set" options={KINDS} value={kind} onChange={setKind} />
      <div className="carousel">
        <div
          className="track"
          ref={track}
          role="listbox"
          aria-label={dataset.label}
          aria-activedescendant={picked ? `move-${picked.name}` : undefined}
          tabIndex={0}
          onScroll={queueMagnify}
          onKeyDown={onKey}
        >
          {moves.map((move, i) => (
            <button
              key={move.name}
              id={`move-${move.name}`}
              type="button"
              role="option"
              className={`emo${
                playing?.move === move.name && (playing.dataset === null || playing.dataset === dataset.dataset) ? " playing" : ""
              }`}
              aria-selected={i === centred}
              aria-label={move.label}
              title={move.label}
              tabIndex={-1}
              onClick={() => (i === centred ? play() : centre(i))}
            >
              {move.emoji}
            </button>
          ))}
        </div>
        {moves.length ? (
          <span className="lensring" aria-hidden="true"></span>
        ) : (
          <p className="carousel-empty">{!daemonOnline ? "Expressions appear when Reachy is on." : expr.loaded ? "No moves in this set." : "Loading expressions…"}</p>
        )}
      </div>
      <div className="picked">
        <div>
          <b>{title}</b>
          <small>{meta}</small>
        </div>
        {showCancel ? (
          <button className="btn press" type="button" id="expr-cancel" disabled={expr.cancelDisabled} onClick={expr.cancel}>
            Cancel
          </button>
        ) : (
          <button
            className="btn primary press"
            type="button"
            disabled={disabled || !picked}
            aria-busy={expr.busy !== null}
            onClick={play}
          >
            {expr.busy !== null ? <span className="spin" aria-hidden="true"></span> : null}
            Play
          </button>
        )}
      </div>
      <p className={`note${hint.error ? " error" : ""}`} id="expr-hint" hidden={hint.hidden}>
        {hint.text}
      </p>
    </div>
  );
}
