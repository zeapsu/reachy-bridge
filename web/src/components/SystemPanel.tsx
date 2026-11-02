import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { apiJson, paths, type PlatformInfo, type SystemStats } from "../api.ts";
import { useLens } from "../glass.ts";
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
import {
  STATS_IDLE,
  gaugeOffset,
  platformLabel,
  statsDisplay,
  type GaugeValue,
  type StatsDisplay,
} from "../logic/stats.ts";
import "./SystemPanel.css";

const RADIUS = 34;
const CIRCUMFERENCE = Math.PI * 2 * RADIUS;

function Gauge({ id, label, value }: { id: string; label: string; value: GaugeValue }) {
  return (
    <div className="gauge" id={id} role="img" aria-label={`${label} ${value.text}${value.suffix}`.trim()}>
      <svg viewBox="0 0 84 84" aria-hidden="true">
        <circle cx="42" cy="42" r={RADIUS} fill="none" stroke="var(--well)" strokeWidth="7"></circle>
        <circle
          cx="42"
          cy="42"
          r={RADIUS}
          fill="none"
          stroke="var(--accent)"
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

/** The detected board, read once each time the daemon comes up (#140). */
function usePlatform(daemonOnline: boolean): PlatformInfo | null {
  const [info, setInfo] = useState<PlatformInfo | null>(null);
  useEffect(() => {
    if (!daemonOnline) return;
    let cancelled = false;
    apiJson<PlatformInfo>(paths.platform)
      .then((payload) => {
        if (!cancelled) setInfo(payload);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [daemonOnline]);
  return info;
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
        <select className="field" id="log-source" aria-label="Log source" value={log.source} onChange={(e) => onSource(e.target.value)}>
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
          className="btn log-follow press"
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

/** Board stats: CPU, GPU, temperature, memory and the audio route. */
export function StatsPanel({ daemonOnline, live }: { daemonOnline: boolean; live: boolean }) {
  const sheet = useLens<HTMLElement>();
  const stats = useStats(daemonOnline, live);
  const board = platformLabel(usePlatform(daemonOnline));
  return (
    <section className="glass sheet" data-lens ref={sheet} id="system-panel" aria-labelledby="stats-title">
      <header>
        <h2 id="stats-title">Board</h2>
        <span className="chip" id="stats-pill">
          <span className={`dot ${daemonOnline ? "ok" : "error"}`} id="stats-dot"></span>
          <span id="v-host" title={board?.title}>
            {daemonOnline && board ? board.text : stats.host}
          </span>
        </span>
      </header>
      <StatsView stats={stats} />
    </section>
  );
}

/** The live log of the daemon, the controller or an app. */
export function LogsPanel({ live }: { live: boolean }) {
  const sheet = useLens<HTMLElement>();
  const { log, selectSource } = useLogs(live);
  const logDisplay = logStatusDisplay(log.status);
  return (
    <section className="glass sheet" data-lens ref={sheet} aria-labelledby="logs-title">
      <header>
        <h2 id="logs-title">Logs</h2>
        <span className="chip" id="logs-pill">
          <span className={`dot ${logDisplay.dot}`} id="log-dot"></span>
          <span id="log-state">{logDisplay.text}</span>
        </span>
      </header>
      <LogsView log={log} onSource={selectSource} />
      <a className="btn quiet press" href="https://github.com/zeapsu/reachy-bridge/blob/main/docs/manual.md" target="_blank" rel="noopener noreferrer">
        User manual ↗
      </a>
    </section>
  );
}
