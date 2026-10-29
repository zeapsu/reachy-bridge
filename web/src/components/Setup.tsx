import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, apiJson, errorText, paths, type AdminRequest, type CheckResult, type SetupStatus } from "../api.ts";
import { useAppearance } from "../hooks/useAppearance.ts";
import { normalizeCode } from "../logic/auth.ts";
import { SETUP_STEPS, checkDot, checkSummary, timeLeft } from "../logic/setup.ts";
import { Wallpaper } from "./Appearance.tsx";
import "./Login.css";
import "./Setup.css";

/**
 * The setup page (#135): served on its own port while `reachy-bridge setup`
 * runs on the robot. The terminal's one-time code opens it; then a few steps
 * check the device and make the user's dashboard the admin.
 */

/** The page closed or this browser's setup session ended: re-read the status. */
const lostSession = (e: unknown) => e instanceof ApiError && [401, 410, 423].includes(e.status);
export function Setup({ initial }: { initial: SetupStatus }) {
  const look = useAppearance();
  const [status, setStatus] = useState(initial);
  const [left, setLeft] = useState(initial.expires_in);
  const [closed, setClosed] = useState(false);

  const refresh = useCallback(async (retry = true): Promise<void> => {
    try {
      const next = await apiJson<SetupStatus>(paths.setupStatus);
      setStatus(next);
      setLeft(next.expires_in);
    } catch (e) {
      if (e instanceof ApiError && e.status !== 410) return;
      // 410, or nothing listening: the page closed. One dropped request isn't
      // that, so ask once more first.
      if (!(e instanceof ApiError) && retry) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        return refresh(false);
      }
      setClosed(true);
    }
  }, []);
  const onError = useCallback(
    (e: unknown) => {
      if (lostSession(e)) void refresh();
    },
    [refresh],
  );

  useEffect(() => {
    const timer = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000);
    // The terminal can issue a new code or close the page; keep in step with it.
    const sync = setInterval(() => void refresh(), 15000);
    return () => {
      clearInterval(timer);
      clearInterval(sync);
    };
  }, [refresh]);
  useEffect(() => {
    if (left === 0) void refresh();
  }, [left, refresh]);

  return (
    <>
      <Wallpaper look={look} />
      <main className="login-screen setup-screen">
        <section className="glass login-card setup-card" aria-labelledby="setup-title">
          <header className="setup-head">
            <h1 id="setup-title">reachy-bridge setup</h1>
            {!closed && <span className="chip mono">{timeLeft(left)}</span>}
          </header>
          {closed ? (
            <Closed />
          ) : status.authenticated ? (
            <Wizard onFinished={() => setClosed(true)} onError={onError} />
          ) : (
            <CodeEntry status={status} onLoggedIn={() => void refresh()} />
          )}
        </section>
      </main>
    </>
  );
}

function Closed() {
  return (
    <>
      <p className="login-lead">The setup page is closed.</p>
      <p className="login-note">
        To open it again, run <code>reachy-bridge setup</code> on the robot.
      </p>
    </>
  );
}

function CodeEntry({ status, onLoggedIn }: { status: SetupStatus; onLoggedIn: () => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const blocked = status.locked || status.used;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const typed = normalizeCode(code);
    if (!typed) {
      setError("The code is 6 digits.");
      return;
    }
    setBusy(true);
    try {
      await apiJson(paths.setupLogin, { method: "POST", body: JSON.stringify({ code: typed }) });
      onLoggedIn();
    } catch (e) {
      setError(errorText(e, "That didn't work."));
      setBusy(false);
    }
  };

  if (blocked) {
    return (
      <>
        <p className="login-lead">{status.locked ? "Too many wrong codes." : "This code was used, or its setup session ended."}</p>
        <p className="login-note">
          Press Enter in the robot's terminal (where <code>reachy-bridge setup</code> runs) for a new code, then reload
          this page.
        </p>
      </>
    );
  }
  return (
    <form className="setup-code" onSubmit={(event) => void submit(event)}>
      <p className="login-lead">Enter the code shown in the robot's terminal.</p>
      <input
        className="field setup-code-field mono"
        aria-label="Setup code"
        inputMode="numeric"
        autoComplete="one-time-code"
        autoFocus
        placeholder="123 456"
        value={code}
        onChange={(e) => setCode(e.target.value)}
      />
      {error && (
        <p className="login-note login-error" role="alert">
          {error}
        </p>
      )}
      <button className="btn primary press" type="submit" disabled={busy}>
        Continue
      </button>
      <p className="login-note">
        The code is single use and crosses your network unencrypted, which is why it only works for 15 minutes.
      </p>
    </form>
  );
}

interface StepProps {
  /** Every failed call goes here too, so a closed page or lost session is noticed. */
  onError: (e: unknown) => void;
}

function Wizard({ onFinished, onError }: { onFinished: () => void } & StepProps) {
  const [step, setStep] = useState(0);
  const next = () => setStep((s) => Math.min(s + 1, SETUP_STEPS.length - 1));
  return (
    <div className="setup-wizard">
      <ol className="setup-steps" aria-label="Setup steps">
        {SETUP_STEPS.map((name, index) => (
          <li key={name} className={index < step ? "done" : index === step ? "current" : ""} aria-current={index === step ? "step" : undefined}>
            <span className="setup-bar" />
            <span className="setup-step-name">{name}</span>
          </li>
        ))}
      </ol>
      {step === 0 && <DeviceCheck onContinue={next} onError={onError} />}
      {step === 1 && <MakeAdmin onContinue={next} onError={onError} />}
      {step === 2 && <Finish onFinished={onFinished} onError={onError} />}
    </div>
  );
}

function DeviceCheck({ onContinue, onError }: { onContinue: () => void } & StepProps) {
  const [results, setResults] = useState<CheckResult[] | null>(null);
  const [error, setError] = useState("");
  const [run, setRun] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setResults(null);
    setError("");
    apiJson<{ results: CheckResult[] }>(paths.setupCheck)
      .then((body) => !cancelled && setResults(body.results))
      .catch((e) => {
        if (cancelled) return;
        // Another check (an earlier load of this page) is still running: wait for it.
        if (e instanceof ApiError && e.status === 409) {
          setTimeout(() => !cancelled && setRun((n) => n + 1), 3000);
          return;
        }
        setError(errorText(e, "The device check failed."));
        onError(e);
      });
    return () => {
      cancelled = true;
    };
  }, [run, onError]);

  const failed = results?.some((r) => r.status === "fail") ?? false;
  return (
    <div className="group setup-step">
      <p className="login-lead">Can this device run the robot?</p>
      {!results && !error && (
        <p className="login-note setup-wait">
          <span className="spin" aria-hidden="true" /> Checking the device. This can take a minute.
        </p>
      )}
      {error && (
        <p className="login-note login-error" role="alert">
          {error}
        </p>
      )}
      {results && (
        <>
          <p className="login-note">{checkSummary(results)}</p>
          <ul className="setup-checks">
            {results.map((r) => (
              <li key={r.name} className="setup-check">
                <span className="setup-check-name">{r.name}</span>
                <span className="chip">
                  <span className={`dot ${checkDot(r.status)}`} />
                  {r.status}
                </span>
                <span className="setup-check-detail">{r.detail}</span>
              </li>
            ))}
          </ul>
          {failed && <p className="note error">Fix what failed and check again, or continue and fix it later.</p>}
        </>
      )}
      <div className="setup-actions">
        <button className="btn quiet press" type="button" onClick={() => setRun((n) => n + 1)} disabled={!results && !error}>
          Check again
        </button>
        <button className="btn primary press" type="button" onClick={onContinue} disabled={!results}>
          Continue
        </button>
      </div>
    </div>
  );
}

function MakeAdmin({ onContinue, onError }: { onContinue: () => void } & StepProps) {
  const [person, setPerson] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [request, setRequest] = useState<AdminRequest["status"]>("none");

  const poll = useCallback(async () => {
    try {
      const body = await apiJson<AdminRequest>(paths.setupAdmin);
      setRequest(body.status);
      if (body.status === "refused") setError(body.detail ? `Not approved: ${body.detail}.` : "Not approved.");
    } catch (e) {
      onError(e);
    }
  }, [onError]);

  // A reload while the terminal is asked picks the request up again.
  useEffect(() => {
    void poll();
  }, [poll]);
  useEffect(() => {
    if (request !== "waiting") return;
    const timer = setInterval(() => void poll(), 2000);
    return () => clearInterval(timer);
  }, [request, poll]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const typed = normalizeCode(code);
    if (!typed) {
      setError("The dashboard's code is 6 digits.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await apiJson(paths.setupAdmin, { method: "POST", body: JSON.stringify({ code: typed, person: person.trim() }) });
      setRequest("waiting");
    } catch (e) {
      setError(errorText(e, "Couldn't send that code."));
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  if (request === "approved") {
    return (
      <div className="group setup-step">
        <p className="login-lead">Your dashboard is the admin.</p>
        <p className="login-note">
          It logs in within a few seconds. From now on every other browser needs a code you approve from that dashboard
          (System · devices).
        </p>
        <div className="setup-actions">
          <button className="btn primary press" type="button" onClick={onContinue}>
            Continue
          </button>
        </div>
      </div>
    );
  }
  if (request === "waiting") {
    return (
      <div className="group setup-step">
        <p className="login-lead">Confirm on the robot.</p>
        <p className="login-note setup-wait">
          <span className="spin" aria-hidden="true" />
          <span>
            The terminal running <code>reachy-bridge setup</code> asks whether to make your dashboard the admin. Type
            the code your dashboard shows there.
          </span>
        </p>
        <p className="login-note">
          This step happens on the robot because this page crosses your network unencrypted.
        </p>
      </div>
    );
  }
  return (
    <form className="group setup-step" onSubmit={(event) => void submit(event)}>
      <p className="login-lead">Make your dashboard the admin.</p>
      <p className="login-note">
        On the computer or phone you'll use, open the dashboard: over Tailscale after{" "}
        <code>reachy-bridge serve</code> on the robot, or on this network with LAN access on. It shows a pairing code
        (on its login screen, or under System · devices · Make this browser the admin). Type that code here, then
        confirm on the robot's terminal.
      </p>
      <input
        className="field"
        aria-label="Your name"
        autoComplete="name"
        placeholder="Your name"
        value={person}
        onChange={(e) => setPerson(e.target.value)}
      />
      <input
        className="field mono"
        aria-label="The dashboard's code"
        inputMode="numeric"
        autoComplete="off"
        placeholder="123 456"
        value={code}
        onChange={(e) => setCode(e.target.value)}
      />
      {error && (
        <p className="login-note login-error" role="alert">
          {error}
        </p>
      )}
      <div className="setup-actions">
        <button className="btn quiet press" type="button" onClick={onContinue}>
          Skip
        </button>
        <button className="btn primary press" type="submit" disabled={busy}>
          Make admin
        </button>
      </div>
    </form>
  );
}

function Finish({ onFinished, onError }: { onFinished: () => void } & StepProps) {
  const [error, setError] = useState("");
  const finish = async () => {
    try {
      await apiJson(paths.setupFinish, { method: "POST" });
      onFinished();
    } catch (e) {
      setError(errorText(e, "Couldn't close the setup page."));
      onError(e);
    }
  };
  return (
    <div className="group setup-step">
      <p className="login-lead">Setup is done.</p>
      <p className="login-note">
        Finishing closes this page. The robot starts off; switch it on from the dashboard. Run{" "}
        <code>reachy-bridge check</code> on the robot any time to repeat the device check.
      </p>
      {error && (
        <p className="login-note login-error" role="alert">
          {error}
        </p>
      )}
      <div className="setup-actions">
        <button className="btn primary press" type="button" onClick={() => void finish()}>
          Finish
        </button>
      </div>
    </div>
  );
}
