import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { apiJson, paths, type SystemStats } from "../api.ts";
import { readEventPayload, useEventSource } from "../hooks/useEventSource.ts";
import { reduceMotion } from "../hooks/reduceMotion.ts";
import {
  LOG_CAP,
  LOG_DEFAULT_SOURCE,
  appendCapped,
  emptyLogMessage,
  logStatusDisplay,
  parseLogSources,
  pickLogSource,
  type LogLine,
  type LogStatus,
} from "../logic/logs.ts";
import { STATS_IDLE, gaugeOffset, statsDisplay, type GaugeValue, type StatsDisplay } from "../logic/stats.ts";

type View = "stats" | "logs";

const RADIUS = 34;
const CIRCUMFERENCE = Math.PI * 2 * RADIUS;

function Gauge({ id, label, value }: { id: string; label: string; value: GaugeValue }) {
  return (
    <div className="gauge" id={id} role="img" aria-label={`${label} ${value.text}${value.suffix}`.trim()}>
      <svg viewBox="0 0 84 84" aria-hidden="true">
        <circle cx="42" cy="42" r={RADIUS} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="7"></circle>
        <circle
          cx="42"
          cy="42"
          r={RADIUS}
          fill="none"
          stroke="var(--amber)"
          strokeWidth="7"
          strokeLinecap="round"
          transform="rotate(-90 42 42)"
          strokeDasharray={CIRCUMFERENCE}
          className="arc"
          style={{
            strokeDashoffset: String(gaugeOffset(value.percent, CIRCUMFERENCE)),
            transition: reduceMotion ? "none" : "stroke-dashoffset .6s var(--ease)",
          }}
        ></circle>
        <text x="42" y="40" textAnchor="middle" className="gv">{value.text}</text>
        <text x="42" y="55" textAnchor="middle" className="gu">{value.suffix}</text>
      </svg>
      <span className="gl">{label}</span>
    </div>
  );
}

function useStats(daemonOnline: boolean, live: boolean): StatsDisplay {
  const [stats, setStats] = useState<StatsDisplay>(STATS_IDLE);
  const onlineRef = useRef(daemonOnline);
  onlineRef.current = daemonOnline;

  // Stats follow the daemon: the backend stops sampling while it is offline.
  useEffect(() => {
    if (!daemonOnline) setStats(STATS_IDLE);
  }, [daemonOnline]);

  useEventSource(live ? paths.statsStream : null, {
    "stats-update": (event) => {
      const payload = readEventPayload<SystemStats>(event);
      // Drop updates racing the offline flip.
      if (!payload || typeof payload !== "object" || !onlineRef.current) return;
      setStats(statsDisplay(payload));
    },
    error: () => setStats((current) => ({ ...current, host: "stats reconnecting" })),
  });
  return stats;
}

function StatsView({ stats }: { stats: StatsDisplay }) {
  return (
    <>
      <div className="gauges">
        <Gauge id="g-cpu" label="cpu" value={stats.cpu} />
        <Gauge id="g-gpu" label="gpu" value={stats.gpu} />
        <Gauge id="g-temp" label="temp" value={stats.temp} />
      </div>
      <div className="meminfo">
        <div className="bar-row">
          <span>memory</span>
          <span id="v-mem">{stats.mem}</span>
        </div>
        <div className="barline">
          <span id="bar-mem" style={{ width: stats.memWidth }}></span>
        </div>
      </div>
      <div className="route">
        <div className="row">
          <span>reachy alsa card</span>
          <span id="v-card">{stats.card}</span>
        </div>
        <div className="row">
          <span>pipewire sink</span>
          <span id="v-sink">{stats.sink}</span>
        </div>
      </div>
    </>
  );
}

interface LogState {
  status: LogStatus;
  source: string;
  sources: string[];
  lines: LogLine[];
  empty: string;
  /** Bumped when the output is replaced (reset, source switch): follow again. */
  resets: number;
}

function useLogs(active: boolean) {
  const [log, setLog] = useState<LogState>({
    status: "idle",
    source: LOG_DEFAULT_SOURCE,
    sources: [],
    lines: [],
    empty: "connecting…",
    resets: 0,
  });
  // The stream opens only once the sources fetch settled for this source.
  const [streamSource, setStreamSource] = useState<string | null>(null);
  const nextId = useRef(0);
  const sourceRef = useRef(log.source);
  sourceRef.current = log.source;

  useEffect(() => {
    if (!active) {
      setStreamSource(null);
      setLog((s) => ({ ...s, status: "idle" }));
      return;
    }
    const abort = new AbortController();
    setLog((s) => ({ ...s, status: "connecting" }));
    void (async () => {
      let offered: string[] | null;
      try {
        offered = parseLogSources(await apiJson(paths.logSources, { signal: abort.signal }));
      } catch {
        offered = null;
      }
      if (abort.signal.aborted) return;
      const selected = sourceRef.current;
      const effective = offered === null ? selected : pickLogSource(selected, offered);
      setLog((s) => ({ ...s, sources: offered ?? [LOG_DEFAULT_SOURCE], source: effective }));
      // A changed source re-runs this effect, which opens the stream.
      if (effective === selected) setStreamSource(effective);
    })();
    return () => abort.abort();
  }, [active, log.source]);

  useEventSource(active && streamSource ? paths.logStream(streamSource) : null, {
    "log-reset": (event) => {
      const payload = readEventPayload<{ status?: string; lines?: unknown[] }>(event) ?? {};
      const status = payload.status || "empty";
      const next = appendCapped([], payload.lines, nextId.current);
      nextId.current = next.nextId;
      setLog((s) => ({ ...s, lines: next.lines, status, empty: emptyLogMessage(status), resets: s.resets + 1 }));
    },
    "log-lines": (event) => {
      const incoming = readEventPayload<{ lines?: unknown[] }>(event)?.lines;
      if (!Array.isArray(incoming) || incoming.length === 0) return;
      const start = nextId.current;
      nextId.current += incoming.length;
      setLog((s) => ({ ...s, lines: appendCapped(s.lines, incoming, start).lines }));
    },
    "source-status": (event) => {
      const status = readEventPayload<{ status?: string }>(event)?.status || "empty";
      setLog((s) => ({ ...s, status, empty: s.lines.length === 0 ? emptyLogMessage(status) : s.empty }));
    },
    error: () => setLog((s) => ({ ...s, status: "reconnecting" })),
  });

  const selectSource = (source: string) => {
    const next = source || LOG_DEFAULT_SOURCE;
    setStreamSource(null);
    setLog((s) => ({ ...s, source: next, lines: [], empty: `${next} hasn't run yet`, resets: s.resets + 1 }));
  };

  return { log, selectSource };
}

function LogsView({ log, onSource }: { log: LogState; onSource: (source: string) => void }) {
  const scroll = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const followRef = useRef(true);

  const setFollowing = (value: boolean) => {
    followRef.current = value;
    setFollow(value);
  };

  // A reset or source switch starts following again.
  useEffect(() => setFollowing(true), [log.resets]);

  useLayoutEffect(() => {
    if (followRef.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [log.lines]);

  return (
    <>
      <div className="log-bar">
        <select id="log-source" aria-label="Log source" value={log.source} onChange={(e) => onSource(e.target.value)}>
          {log.sources.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </select>
      </div>
      <div className="log-body">
        <div
          className="log-scroll"
          id="log-scroll"
          ref={scroll}
          role="log"
          aria-live="off"
          aria-label="Live log output"
          tabIndex={0}
          onScroll={() => {
            const el = scroll.current;
            if (el) setFollowing(el.scrollTop + el.clientHeight >= el.scrollHeight - 8);
          }}
        >
          <pre className="log-out" id="log-out">
            {log.lines.map((line) => (
              <span className="ln" key={line.id}>
                {line.text}
              </span>
            ))}
          </pre>
          <p className="log-empty" id="log-empty" hidden={log.lines.length > 0}>
            {log.empty}
          </p>
        </div>
        <button
          className="log-follow"
          id="log-follow"
          type="button"
          hidden={follow}
          onClick={() => {
            if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
            setFollowing(true);
          }}
        >
          follow live
        </button>
      </div>
      <div className="log-foot">
        <span>
          source · <span className="v" id="log-src-v">{log.source}</span>
        </span>
        <span id="log-count">
          {log.lines.length} lines · cap {LOG_CAP}
        </span>
      </div>
    </>
  );
}

export function SystemPanel({ daemonOnline, live }: { daemonOnline: boolean; live: boolean }) {
  const [view, setView] = useState<View>("stats");
  const stats = useStats(daemonOnline, live && view === "stats");
  const { log, selectSource } = useLogs(live && view === "logs");
  const statsTab = useRef<HTMLButtonElement>(null);
  const logsTab = useRef<HTMLButtonElement>(null);
  const logDisplay = logStatusDisplay(log.status);

  const onTabKey = (event: KeyboardEvent) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    // Two tabs: either arrow moves to the other one.
    const next = view === "stats" ? "logs" : "stats";
    (next === "stats" ? statsTab : logsTab).current?.focus();
    setView(next);
  };

  return (
    <section className="glass pad instrument" id="system-panel">
      <div className="head">
        <h2>system</h2>
        <div className="rule"></div>
        <div className="viewtabs" id="sys-tabs" role="tablist" aria-label="System panel view" onKeyDown={onTabKey}>
          {(["stats", "logs"] as const).map((tab) => (
            <button
              key={tab}
              ref={tab === "stats" ? statsTab : logsTab}
              type="button"
              role="tab"
              id={`sys-tab-${tab}`}
              aria-selected={view === tab}
              aria-controls={`sys-view-${tab}`}
              tabIndex={view === tab ? 0 : -1}
              data-view={tab}
              onClick={() => setView(tab)}
            >
              {tab}
            </button>
          ))}
        </div>
        <span className="pill" id="stats-pill" hidden={view !== "stats"}>
          <span className={`dot ${daemonOnline ? "ok" : "error"}`} id="stats-dot"></span>
          <span id="v-host">{stats.host}</span>
        </span>
        <span className="pill" id="logs-pill" hidden={view !== "logs"}>
          <span className={`dot ${logDisplay.dot}`} id="log-dot"></span>
          <span id="log-state">{logDisplay.text}</span>
        </span>
      </div>
      <div className="sys-view" id="sys-view-stats" role="tabpanel" aria-labelledby="sys-tab-stats" hidden={view !== "stats"}>
        <StatsView stats={stats} />
      </div>
      <div className="sys-view" id="sys-view-logs" role="tabpanel" aria-labelledby="sys-tab-logs" hidden={view !== "logs"}>
        <LogsView log={log} onSource={selectSource} />
      </div>
    </section>
  );
}
