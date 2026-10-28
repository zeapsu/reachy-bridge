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
 * Shows the dashboard, or the login screen when LAN mode is on and this
 * browser isn't paired (#141). Any 401 from the API re-checks.
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
  | { kind: "waiting"; id: string; code: string }
  | { kind: "ended"; reason: "expired" | "locked" }
  | { kind: "error"; text: string };

export function Login() {
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
        else if (outcome === "expired" || outcome === "locked") setPhase({ kind: "ended", reason: outcome });
      } catch {
        // Network blip: try again on the next tick.
      } finally {
        inflight = false;
      }
    };
    const timer = setInterval(() => void tick(), PAIR_POLL_MS);
    return () => clearInterval(timer);
  }, [waitingId]);

  const retry = () => setAttempt((n) => n + 1);

  return (
    <main className="login-screen">
      <section className="glass login-card" aria-labelledby="login-title">
        <h1 id="login-title">reachy-bridge</h1>
        <p className="login-lead">This browser isn't paired with the robot yet.</p>
        {phase.kind === "starting" && <p className="login-note">Getting a code…</p>}
        {phase.kind === "waiting" && (
          <>
            <p className="login-code" id="login-code" aria-label={`Pairing code ${phase.code.split("").join(" ")}`}>
              {formatCode(phase.code)}
            </p>
            <p className="login-note">
              Run <code>reachy-bridge pair {phase.code}</code> on the robot, or approve this code from a paired
              dashboard (System · devices). The code is valid for 5 minutes.
            </p>
            <p className="login-wait" aria-live="polite">
              waiting for approval…
            </p>
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
      </section>
    </main>
  );
}
