import { useEffect, useRef, useState } from "react";
import { paths } from "../api.ts";
import { readEventPayload, useEventSource } from "../hooks/useEventSource.ts";
import { reduceMotion } from "../hooks/reduceMotion.ts";
import { Panel } from "./Panel.tsx";
import { EqModel, NBARS, audioLevelsFromPayload, formatPeak, formatSampleRate, type AudioLevels } from "../logic/audio.ts";

type MicStatus = "waiting" | "listening" | "reconnecting" | "unavailable";

const micDot = (status: MicStatus) =>
  status === "listening" ? "dot ok" : status === "reconnecting" ? "dot loading" : status === "unavailable" ? "dot error" : "dot";

const BAR_INDEXES = Array.from({ length: NBARS }, (_, i) => i);

/**
 * Microphone spectrum, then where the robot's sound goes (`card`, `sink`,
 * from the board's stats). The 10 Hz meter is painted imperatively from EqModel,
 * with per-element change guards so a settled quiet stream does no DOM writes.
 */
export function AudioPanel({ daemonOnline, live, card, sink }: { daemonOnline: boolean; live: boolean; card: string; sink: string }) {
  const [status, setStatus] = useState<MicStatus>("waiting");
  const eq = useRef<HTMLDivElement>(null);
  const rmsEl = useRef<HTMLDivElement>(null);
  const inputEl = useRef<HTMLSpanElement>(null);
  const peakEl = useRef<HTMLSpanElement>(null);
  const rateEl = useRef<HTMLSpanElement>(null);
  const model = useRef(new EqModel(reduceMotion));
  const last = useRef({
    bars: new Array<string | null>(NBARS).fill(null),
    rms: null as string | null,
    idle: null as boolean | null,
    peak: null as string | null,
    source: null as string | null,
    rate: null as number | null,
    reducedEvents: 0,
  });

  const commit = (levels: AudioLevels | null, label: string | null = null) => {
    const m = model.current;
    const l = last.current;
    const setPeak = (text: string) => {
      if (text !== l.peak && peakEl.current) {
        l.peak = text;
        peakEl.current.textContent = text;
      }
    };
    if (levels) {
      if (m.source && m.source !== l.source && inputEl.current) {
        l.source = m.source;
        inputEl.current.textContent = m.source;
      }
      if (m.rateHz && m.rateHz > 0 && m.rateHz !== l.rate && rateEl.current) {
        l.rate = m.rateHz;
        rateEl.current.textContent = formatSampleRate(m.rateHz);
      }
      if (m.peakDb != null) setPeak(formatPeak(m.peakDb));
    } else {
      setPeak(label || "no audio signal");
    }
    if (m.quiet !== l.idle && eq.current) {
      l.idle = m.quiet;
      eq.current.classList.toggle("idle", m.quiet);
    }
    const bars = eq.current?.children;
    if (bars) {
      for (let i = 0; i < NBARS; i += 1) {
        const next = `${m.bars[i].toFixed(1)}%`;
        if (next !== l.bars[i]) {
          l.bars[i] = next;
          (bars[i] as HTMLElement).style.height = next;
        }
      }
    }
    const rmsNext = `${(Math.round(m.rms * 2) / 2).toFixed(1)}%`;
    if (rmsNext !== l.rms && rmsEl.current) {
      l.rms = rmsNext;
      rmsEl.current.style.width = rmsNext;
    }
  };

  const renderLevel = (levels: AudioLevels) => {
    // Reduced motion paints every 5th sample, but the model sees them all.
    const shouldPaint = !reduceMotion || last.current.reducedEvents % 5 === 0;
    last.current.reducedEvents += 1;
    model.current.advance(levels);
    if (shouldPaint) commit(levels);
  };

  const renderUnavailable = (label: string) => {
    last.current.reducedEvents = 0;
    model.current.reset();
    commit(null, label);
  };

  // The daemon going offline settles the meter (main.js renderEqualizer(null)).
  useEffect(() => {
    if (daemonOnline) return;
    model.current.reset();
    commit(null, "no audio signal");
  }, [daemonOnline]);

  useEventSource(live ? paths.audioStream : null, {
    "audio-level": (event) => {
      const levels = audioLevelsFromPayload(readEventPayload(event));
      if (levels) {
        setStatus("listening");
        renderLevel(levels);
      } else {
        setStatus("unavailable");
        renderUnavailable("no audio signal");
      }
    },
    "daemon-unavailable": () => {
      setStatus("unavailable");
      renderUnavailable("no audio signal");
    },
    error: () => {
      setStatus("reconnecting");
      renderUnavailable("audio stream reconnecting");
    },
  });

  return (
    <Panel
      inset
      title="Audio"
      titleId="audio-title"
      aside={
        <span className="chip">
          <span className={micDot(status)} id="mic-dot"></span>
          <span id="mic-state">{status}</span>
        </span>
      }
    >
      <div className="eq" id="eq" ref={eq} aria-hidden="true">
        {BAR_INDEXES.map((i) => (
          <div className="bar" key={i}></div>
        ))}
      </div>
      <div className="eq-rms" id="eq-rms" ref={rmsEl} aria-hidden="true"></div>
      <div className="eq-bands" aria-hidden="true">
        <span className="b-low">low</span>
        <span className="b-mid">mid</span>
        <span className="b-high">high</span>
      </div>
      <div className="eq-foot">
        <span>
          input · <span className="v" id="eq-input" ref={inputEl}>XVF3800</span>
        </span>
        <span>
          peak <span className="v" id="eq-peak" ref={peakEl}>no audio signal</span>
        </span>
        <span>
          rate <span className="v" id="eq-rate" ref={rateEl}>16 kHz</span>
        </span>
      </div>
      <div className="route">
        <div className="row">
          <span>reachy alsa card</span>
          <span id="v-card">{card}</span>
        </div>
        <div className="row">
          <span>pipewire sink</span>
          <span id="v-sink">{sink}</span>
        </div>
      </div>
    </Panel>
  );
}
