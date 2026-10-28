import { useCallback, useEffect, useRef, useState } from "react";
import { apiJson, errorText, paths, type AppJob, type CatalogApp, type HfLogin } from "../api.ts";
import { jobFinished, jobLine } from "../logic/catalog.ts";

const JOB_POLL_MS = 1000;

export type JobKind = "install" | "update" | "remove";

/** An install, update or removal in progress, or how the last one ended. */
export interface RowJob {
  kind: JobKind;
  status: string;
  line: string;
}

export interface CatalogState {
  /** Every Reachy Mini Space, or only Pollen's selection. */
  all: boolean;
  setAll: (all: boolean) => void;
  apps: CatalogApp[];
  loading: boolean;
  error: string | null;
  /** By app name (a private install by its Space id until it lands). */
  jobs: Record<string, RowJob>;
  /** Installed apps with a newer version on Hugging Face. */
  updates: string[];
  hf: HfLogin | null;
  hfBusy: boolean;
  hfError: string | null;
  install: (app: CatalogApp) => void;
  installPrivate: (id: string) => void;
  update: (name: string) => void;
  remove: (name: string) => void;
  saveToken: (token: string) => Promise<boolean>;
  signOut: () => void;
}

/**
 * The app catalog (#126): the listing, installs, updates and removals (each
 * a daemon job polled until it ends), and the Hugging Face login. Loads while
 * `live` (the Apps tab is open with the daemon up); `onChanged` refreshes the
 * installed list when a job ends.
 */
export function useCatalog(live: boolean, onChanged: () => void): CatalogState {
  const [all, setAll] = useState(false);
  const [apps, setApps] = useState<CatalogApp[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Record<string, RowJob>>({});
  const [updates, setUpdates] = useState<string[]>([]);
  const [hf, setHf] = useState<HfLogin | null>(null);
  const [hfBusy, setHfBusy] = useState(false);
  const [hfError, setHfError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(clearTimeout);
  }, []);

  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiJson<{ apps: CatalogApp[] }>(paths.appCatalog(all))
      .then((payload) => !cancelled && setApps(Array.isArray(payload?.apps) ? payload.apps : []))
      .catch((err) => !cancelled && setError(errorText(err, "Couldn't load the app catalog")))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [live, all, reload]);

  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    apiJson<HfLogin>(paths.hfStatus)
      .then((login) => !cancelled && setHf(login))
      .catch(() => !cancelled && setHf(null));
    apiJson<{ updates: string[] }>(paths.appUpdates)
      .then((payload) => !cancelled && setUpdates(Array.isArray(payload?.updates) ? payload.updates : []))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [live, reload]);

  const setJob = useCallback((key: string, job: RowJob | null) => {
    setJobs((current) => {
      const next = { ...current };
      if (job) next[key] = job;
      else delete next[key];
      return next;
    });
  }, []);

  const follow = useCallback(
    (key: string, kind: JobKind, id: string) => {
      const poll = async () => {
        let job: AppJob;
        try {
          job = await apiJson<AppJob>(paths.appJob(id));
        } catch (err) {
          setJob(key, { kind, status: "failed", line: errorText(err, "Lost track of the job") });
          return;
        }
        const line = jobLine(Array.isArray(job.logs) ? job.logs : []);
        if (jobFinished(job.status)) {
          if (job.status === "done") setJob(key, null);
          else setJob(key, { kind, status: "failed", line: line || `The ${kind} failed` });
          onChangedRef.current();
          setReload((n) => n + 1);
          return;
        }
        setJob(key, { kind, status: job.status, line });
        const timer = setTimeout(() => {
          timers.current.delete(timer);
          void poll();
        }, JOB_POLL_MS);
        timers.current.add(timer);
      };
      void poll();
    },
    [setJob],
  );

  const start = useCallback(
    async (key: string, kind: JobKind, path: string, body?: unknown) => {
      setJob(key, { kind, status: "pending", line: "" });
      try {
        const { job } = await apiJson<{ job: string }>(path, {
          method: "POST",
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        follow(key, kind, job);
      } catch (err) {
        setJob(key, { kind, status: "failed", line: errorText(err, `Couldn't start the ${kind}`) });
      }
    },
    [follow, setJob],
  );

  const install = useCallback((app: CatalogApp) => void start(app.name, "install", paths.appInstall, { id: app.id }), [start]);
  const installPrivate = useCallback(
    (id: string) => void start(id.trim(), "install", paths.appInstallPrivate, { id: id.trim() }),
    [start],
  );
  const update = useCallback((name: string) => void start(name, "update", paths.appUpdate(name)), [start]);
  const remove = useCallback((name: string) => void start(name, "remove", paths.appRemove(name)), [start]);

  const saveToken = useCallback(async (token: string) => {
    setHfBusy(true);
    setHfError(null);
    try {
      setHf(await apiJson<HfLogin>(paths.hfToken, { method: "PUT", body: JSON.stringify({ token: token.trim() }) }));
      setReload((n) => n + 1);
      return true;
    } catch (err) {
      setHfError(errorText(err, "Couldn't sign in"));
      return false;
    } finally {
      setHfBusy(false);
    }
  }, []);

  const signOut = useCallback(() => {
    setHfBusy(true);
    setHfError(null);
    apiJson<HfLogin>(paths.hfToken, { method: "DELETE" })
      .then((login) => {
        setHf(login);
        setReload((n) => n + 1);
      })
      .catch((err) => setHfError(errorText(err, "Couldn't sign out")))
      .finally(() => setHfBusy(false));
  }, []);

  return {
    all,
    setAll,
    apps,
    loading,
    error,
    jobs,
    updates,
    hf,
    hfBusy,
    hfError,
    install,
    installPrivate,
    update,
    remove,
    saveToken,
    signOut,
  };
}
