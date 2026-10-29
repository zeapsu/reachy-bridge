import { useCallback, useEffect, useState, type ReactNode } from "react";
import { apiJson, errorText, paths } from "../api.ts";
import {
  LOGIN_REQUIRED_EVENT,
  PAIR_POLL_MS,
  formatCode,
  needsLogin,
  pairEndedText,
  pairOutcome,
  type AuthStatus,
} from "../logic/auth.ts";
import "./Login.css";

type Gate = "checking" | "login" | "open";

/**
 * Shows the dashboard, or the login screen when a login is required (LAN
 * mode, #141, or admin mode, #221) and this browser isn't paired. Any 401
 * from the API re-checks.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>("checking");

  const check = useCallback(async () => {
    try {
      const status = await apiJson<AuthStatus>(paths.authStatus);
      setGate(needsLogin(status) ? "login" : "open");
    } catch {
      // A controller without the auth routes has no login.
      setGate((current) => (current === "checking" ? "open" : current));
    }
  }, []);

  useEffect(() => {
    void check();
    const onLoginRequired = () => void check();
    window.addEventListener(LOGIN_REQUIRED_EVENT, onLoginRequired);
    return () => window.removeEventListener(LOGIN_REQUIRED_EVENT, onLoginRequired);
  }, [check]);

  if (gate === "checking") return null;
  if (gate === "login") return <Login />;
  return <>{children}</>;
}

type Phase =
  | { kind: "starting" }
  | { kind: "waiting"; id: string; code: string; locked?: boolean }
  | { kind: "ended"; reason: "expired" | "locked" }
  | { kind: "error"; text: string };

/** Asks the controller for a pairing code and reloads once it's approved. */
function usePairing(): { phase: Phase; retry: () => void } {
  const [phase, setPhase] = useState<Phase>({ kind: "starting" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPhase({ kind: "starting" });
    apiJson<{ id: string; code: string }>(paths.authPair, { method: "POST" })
      .then(({ id, code }) => !cancelled && setPhase({ kind: "waiting", id, code }))
      .catch((error) => !cancelled && setPhase({ kind: "error", text: errorText(error, "Couldn't start pairing.") }));
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const waitingId = phase.kind === "waiting" ? phase.id : null;
  useEffect(() => {
    if (!waitingId) return;
    let inflight = false;
    const tick = async () => {
      if (inflight) return;
      inflight = true;
      try {
        const response = await fetch(paths.authPairPoll(waitingId), { headers: { Accept: "application/json" } });
        const body: unknown = await response.json().catch(() => null);
        const outcome = pairOutcome(response.status, body);
        if (outcome === "approved") window.location.reload();
        else if (outcome === "expired") setPhase({ kind: "ended", reason: outcome });
        // Locked by wrong codes: only the CLI on the robot can still approve
        // it (#221), so keep polling until it expires.
        else if (outcome === "locked") setPhase((p) => (p.kind === "waiting" && p.id === waitingId && !p.locked ? { ...p, locked: true } : p));
      } catch {
        // Network blip: try again on the next tick.
      } finally {
        inflight = false;
      }
    };
    const timer = setInterval(() => void tick(), PAIR_POLL_MS);
    return () => clearInterval(timer);
  }, [waitingId]);

  return { phase, retry: () => setAttempt((n) => n + 1) };
}

/**
 * A pairing code and how to approve it. `admin`: the first admin's code,
 * which only `reachy-bridge pair --admin` on the robot approves (#221).
 */
export function PairCode({ admin = false }: { admin?: boolean }) {
  const { phase, retry } = usePairing();
  return (
    <>
      {phase.kind === "starting" && <p className="login-note">Getting a code…</p>}
      {phase.kind === "waiting" && (
        <>
          <p className="login-code" id="login-code" aria-label={`Pairing code ${phase.code.split("").join(" ")}`}>
            {formatCode(phase.code)}
          </p>
          {admin ? (
            <p className="login-note">
              Run <code className="login-cmd">reachy-bridge pair --admin --person=&lt;your name&gt; {phase.code}</code> on the robot,
              or type the code into the setup page. The code is valid for 5 minutes.
            </p>
          ) : (
            <p className="login-note">
              Run <code className="login-cmd">reachy-bridge pair {phase.code}</code> on the robot, ask an admin to approve this code
              from their dashboard (System · devices), or, while setting up, type it into the setup page. The code is
              valid for 5 minutes.
            </p>
          )}
          {phase.locked && (
            <p className="login-note login-error" role="alert">
              {pairEndedText("locked")} Only <code>reachy-bridge pair</code> on the robot can approve this code now.
            </p>
          )}
          <p className="login-wait" aria-live="polite">
            waiting for approval…
          </p>
          {phase.locked && (
            <button className="btn primary press" type="button" onClick={retry}>
              get a new code
            </button>
          )}
        </>
      )}
      {phase.kind === "ended" && (
        <>
          <p className="login-note login-error" role="alert">
            {pairEndedText(phase.reason)}
          </p>
          <button className="btn primary press" type="button" onClick={retry}>
            get a new code
          </button>
        </>
      )}
      {phase.kind === "error" && (
        <>
          <p className="login-note login-error" role="alert">
            {phase.text}
          </p>
          <button className="btn primary press" type="button" onClick={retry}>
            try again
          </button>
        </>
      )}
    </>
  );
}

export function Login() {
  return (
    <main className="login-screen">
      <section className="glass login-card" aria-labelledby="login-title">
        <h1 id="login-title">reachy-bridge</h1>
        <p className="login-lead">This browser isn't paired with the robot yet.</p>
        <PairCode />
      </section>
    </main>
  );
}
