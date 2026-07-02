(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const TAU = Math.PI * 2;
  const APP_NAMES = ["reachy_chat", "reachy_dance", "reachy_lookat", "reachy_simon"];
  const appState = new Map(APP_NAMES.map((name) => [name, { name, status: "stopped" }]));
  const LOADING_FRAGMENTS = ["Booting…", "Resolving env…", "Connecting to daemon…", "Activating motors…", "Waking up…"];
  let loadingTick = 0;
  let lastAppsSig = null;
  const state = {
    // null = unknown until the first setDaemonOnline() call. Starting at null (not
    // false) makes `changed` true on that first call, so the boot DOM ("connecting"
    // /"waiting") is reconciled to the real state even when the daemon is offline
    // from page load and stays offline — the change-guard would otherwise suppress
    // those writes since false === false (#43).
    daemonOnline: null,
    controlsLoaded: false,
    controlBusy: false,
    appBusy: false,
    exprLoaded: false,
    exprLoading: false,
    exprBusy: false,
    mode: "schematic",
    robotControlMode: null,
    daemonStatusTimer: null,
    daemonStatusInflight: false,
    daemonStopping: false,
    sliderActive: null,
    simLoop: null,
    live: { streams: [], timers: [], active: false },
  };

  document.querySelectorAll(".glass").forEach((glass) => {
    glass.addEventListener("pointermove", (event) => {
      const rect = glass.getBoundingClientRect();
      glass.style.setProperty("--mx", `${((event.clientX - rect.left) / rect.width) * 100}%`);
      glass.style.setProperty("--my", `${((event.clientY - rect.top) / rect.height) * 100}%`);
    });
  });

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const finite = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const radiansToDegrees = (value) => (value == null ? null : value * 180 / Math.PI);
  const dash = (value, digits = 2) => (value == null ? "—" : value.toFixed(digits));
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));

  async function apiJson(path, options = {}) {
    const init = { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } };
    if (init.body !== undefined && !init.headers["Content-Type"]) {
      init.headers["Content-Type"] = "application/json";
    }

    const response = await fetch(path, init);
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_) {
        payload = text;
      }
    }
    if (!response.ok) {
      const detail = payload && typeof payload === "object" && "detail" in payload ? payload.detail : `HTTP ${response.status}`;
      throw new Error(detail);
    }
    return payload;
  }

  function readEventPayload(event) {
    try {
      return JSON.parse(event.data);
    } catch (_) {
      return null;
    }
  }

  function bindEventSource(url, handlers) {
    try {
      const source = new EventSource(url);
      Object.entries(handlers).forEach(([name, handler]) => {
        source.addEventListener(name, (event) => {
          try {
            handler(event);
          } catch (_) {
            // Bad event payloads should not take down the dashboard.
          }
        });
      });
      return source;
    } catch (_) {
      return null;
    }
  }

  function setDaemonOnline(online) {
    const changed = state.daemonOnline !== online;
    state.daemonOnline = online;
    // The telemetry "state-update" event calls this ~10 Hz with online=true. The
    // status chrome below is a pure function of `online`, so only rewrite it when
    // online actually flips — `changed` is also true on the first call (daemonOnline
    // starts null, see state init), so the boot DOM is reconciled exactly once (#43).
    // setControlsEnabled() stays unconditional — it is
    // the reconciliation point for controlBusy, which the daemon start/stop handlers
    // flip without calling it — but is itself change-guarded, so a steady-state tick
    // does zero DOM writes.
    if (changed) {
      $("daemon-dot").className = `dot ${online ? "ok" : "error"}`;
      $("daemon-value").textContent = online ? "online" : "offline";
      $("vp-rec").className = `rec ${online ? "ok" : ""}`;
      $("rig").classList.toggle("offline", !online);
      $("rig-note").hidden = online;
      $("mic-dot").className = `dot ${online ? "ok" : "error"}`;
      $("mic-state").textContent = online ? "listening" : "unavailable";
    }
    setControlsEnabled(online && !state.controlBusy);
    if (online) void loadExpressions();

    if (!online) {
      state.controlsLoaded = false;
      $("control-status").textContent = "offline";
      $("control-hint").textContent = "Controls enable when the daemon is online.";
      $("control-hint").className = "panel-hint";
      // Mirror the controls treatment: the chips dim when offline, and without
      // this the panel gives no textual cue why (loadExpressions only hides).
      $("expr-hint").hidden = false;
      $("expr-hint").textContent = "Expressions enable when the daemon is online.";
      $("expr-hint").className = "panel-hint";
      renderOfflinePose();
      renderEqualizer(false, "no audio signal");
    } else if (changed || !state.controlsLoaded) {
      $("control-status").textContent = "online";
      $("control-hint").textContent = "";
      $("control-hint").className = "panel-hint";
      // Re-hide the offline hint on reconnect: loadExpressions early-returns
      // once exprLoaded, so it won't do it for us.
      if (state.exprLoaded) $("expr-hint").hidden = true;
      void loadControls();
    }
  }

  function setControlsEnabled(enabled) {
    const disabled = !enabled;
    ["vol-spk", "vol-mic", "wobble", "testsound"].forEach((id) => {
      const element = $(id);
      // Never toggle .disabled on the slider being dragged — flipping it (even
      // re-enabling) aborts an in-progress touch drag on iOS. The release
      // handler reconciles the correct state once the drag ends (#38).
      // Write only on change: setDaemonOnline calls this ~10 Hz to reconcile
      // controlBusy, so an unconditional write would thrash .disabled every tick (#43).
      if (element && state.sliderActive !== id && element.disabled !== disabled) {
        element.disabled = disabled;
      }
    });
    document.querySelectorAll("#motor-seg button").forEach((button) => {
      if (button.disabled !== disabled) button.disabled = disabled;
    });
    // Expression chips gate on the daemon too — but never fight an in-progress
    // move (exprBusy disables the whole strip until the POST resolves).
    if (!state.exprBusy) {
      document.querySelectorAll(".chip").forEach((chip) => {
        if (chip.disabled !== disabled) chip.disabled = disabled;
      });
    }
  }

  function renderOfflinePose() {
    $("r-rpy").textContent = "—/—/—";
    $("r-yaw").textContent = "—";
    $("r-ant").textContent = "—/—";
    $("r-doa").textContent = "—°";
    $("head").removeAttribute("transform");
    $("body").removeAttribute("transform");
    $("antL").removeAttribute("transform");
    $("antR").removeAttribute("transform");
    $("doaArc").setAttribute("d", "");
    $("doaTick").setAttribute("cx", "160");
    $("doaTick").setAttribute("cy", "16");
    $("rig").classList.remove("speaking");
    // This writes the DOM directly, so invalidate the change-guard cache — otherwise a
    // reconnect with an unchanged pose makes every guard in applyRobotState skip and
    // the schematic stays blank.
    poseCache = freshPoseCache();
  }

  function poseValues(pose) {
    if (Array.isArray(pose)) return pose.slice(-3).map(finite);
    if (pose && typeof pose === "object") {
      return [pose.roll, pose.pitch, pose.yaw].map(finite);
    }
    return [null, null, null];
  }

  function antennaValues(value) {
    if (Array.isArray(value)) return [finite(value[0]), finite(value[1])];
    if (value && typeof value === "object") return [finite(value.left), finite(value.right)];
    return [null, null];
  }

  function doaDegrees(doa) {
    if (!doa || typeof doa !== "object") return null;
    const angle = finite(doa.angle);
    if (angle == null) return null;
    return Math.abs(angle) <= TAU ? radiansToDegrees(angle) : angle;
  }

  // Change-guard caches for applyRobotState. doaDeg uses `undefined` (not null) so
  // a real null DOA still differs from the initial cache and renders once. Kept in a
  // factory so renderOfflinePose can invalidate the whole cache in one place.
  function freshPoseCache() {
    return {
      rpy: null,
      yaw: null,
      ant: null,
      doa: null,
      head: null,
      body: null,
      antL: null,
      antR: null,
      speaking: null,
      doaDeg: undefined,
    };
  }
  let poseCache = freshPoseCache();

  // Guarded write: touch the DOM only when the value changed since last render.
  // Collapses the per-field if/cache/write the schematic repeated for every node.
  function guard(key, value, write) {
    if (value !== poseCache[key]) {
      poseCache[key] = value;
      write(value);
    }
  }

  function applyRobotState(payload) {
    if (!payload || typeof payload !== "object") return;
    const [rollDeg, pitchDeg, yawDeg] = poseValues(payload.head_pose).map(radiansToDegrees);
    const bodyYaw = finite(payload.body_yaw);
    const [antLeft, antRight] = antennaValues(payload.antennas_position);
    const doa = payload.doa;
    const doaDeg = doaDegrees(doa);
    const speechDetected = Boolean(doa && typeof doa === "object" && doa.speech_detected === true);
    if ("control_mode" in payload && payload.control_mode !== state.robotControlMode) {
      selectMotorMode(payload.control_mode);
    }

    const rpyText = [rollDeg, pitchDeg, yawDeg].map((value) => (value == null ? "—" : value.toFixed(0))).join("/");
    guard("rpy", rpyText, (v) => { $("r-rpy").textContent = v; });
    guard("yaw", dash(bodyYaw, 2), (v) => { $("r-yaw").textContent = v; });
    guard("ant", `${dash(antLeft, 2)}/${dash(antRight, 2)}`, (v) => { $("r-ant").textContent = v; });
    guard("doa", doaDeg == null ? "—°" : `${doaDeg.toFixed(0)}°`, (v) => { $("r-doa").textContent = v; });

    const roll = rollDeg ?? 0;
    const pitch = pitchDeg ?? 0;
    const yaw = yawDeg ?? 0;
    guard("head", `rotate(${roll.toFixed(2)} 160 104) translate(${(yaw * 0.3).toFixed(2)} ${(-pitch * 0.3).toFixed(2)})`, (v) => { $("head").setAttribute("transform", v); });
    guard("body", `rotate(${((bodyYaw ?? 0) * 12).toFixed(2)} 160 152)`, (v) => { $("body").setAttribute("transform", v); });
    guard("antL", `rotate(${((antLeft ?? 0) * 18).toFixed(2)} 132 70)`, (v) => { $("antL").setAttribute("transform", v); });
    guard("antR", `rotate(${((antRight ?? 0) * 18).toFixed(2)} 188 70)`, (v) => { $("antR").setAttribute("transform", v); });

    guard("speaking", speechDetected && doaDeg != null, (v) => { $("rig").classList.toggle("speaking", v); });
    guard("doaDeg", doaDeg, (v) => {
      if (v == null) $("doaArc").setAttribute("d", "");
      else setDoa(v);
    });
  }

  const polar = (cx, cy, radius, degrees) => {
    const radians = (degrees - 90) * Math.PI / 180;
    return [cx + radius * Math.cos(radians), cy + radius * Math.sin(radians)];
  };

  function setDoa(degrees) {
    const cx = 160;
    const cy = 108;
    const radius = 92;
    const span = 24;
    const [x1, y1] = polar(cx, cy, radius, degrees - span);
    const [x2, y2] = polar(cx, cy, radius, degrees + span);
    const [tx, ty] = polar(cx, cy, radius, degrees);
    $("doaArc").setAttribute("d", `M ${x1.toFixed(1)} ${y1.toFixed(1)} A ${radius} ${radius} 0 0 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`);
    $("doaTick").setAttribute("cx", tx.toFixed(1));
    $("doaTick").setAttribute("cy", ty.toFixed(1));
  }

  function setMode(mode) {
    if (!mode || mode === state.mode) return;
    const previous = state.mode;
    state.mode = mode;
    document.querySelectorAll(".vp-layer").forEach((layer) => {
      layer.classList.toggle("active", layer.dataset.mode === mode);
    });
    document.querySelectorAll("#vp-switch button").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === mode);
    });

    const labels = { camera: "CAMERA", sim: "SIM · MUJOCO", schematic: "SCHEMATIC" };
    const metas = {
      camera: "availability check · stream disabled",
      sim: "decorative preview · not live",
      schematic: "telemetry 10 hz",
    };
    $("vp-badge-t").textContent = labels[mode] || mode.toUpperCase();
    $("vp-name").textContent = mode === "sim" ? "simulation" : mode;
    $("vp-meta").textContent = metas[mode] || "";

    if (previous === "sim" && state.simLoop) state.simLoop.stop();
    if (mode === "sim") {
      if (!state.simLoop) state.simLoop = makeSim($("sim"));
      state.simLoop.start();
    }
    if (mode === "camera") void refreshCameraAvailability();
  }

  async function refreshCameraAvailability() {
    const placeholder = $("camera-placeholder");
    placeholder.textContent = "Checking camera availability";
    try {
      const availability = await apiJson("/api/camera/availability");
      if (availability && availability.available === true) {
        placeholder.textContent = "Camera available (live view coming soon)";
      } else if (availability && availability.reason === "in_use") {
        placeholder.textContent = `Camera in use by ${availability.holder || "another app"}`;
      } else if (availability && availability.reason === "daemon_down") {
        placeholder.textContent = "Daemon offline";
      } else {
        placeholder.textContent = "Camera unavailable";
      }
    } catch (error) {
      placeholder.textContent = error.message || "Camera status unavailable";
    }
  }

  function renderApps(statuses = Array.from(appState.values())) {
    const active = statuses.find((app) => app.status === "running" || app.status === "loading" || app.status === "stopping") || null;
    const html = APP_NAMES.map((name) => {
      const app = statuses.find((item) => item.name === name) || { name, status: "stopped" };
      const isRunning = app.status === "running";
      const isLoading = app.status === "loading";
      const isStopping = app.status === "stopping";
      const isError = app.status === "error";
      const statusClass = isRunning ? "running" : isLoading ? "loading" : isStopping ? "stopping" : isError ? "error" : "";
      const uptime = isRunning && app.start_time ? ` · ${formatUptime(Date.now() / 1000 - Number(app.start_time))}` : "";
      const pid = isRunning && app.pid ? ` · pid ${app.pid}` : "";
      const external = isRunning && app.external ? ` · external` : "";
      const error = app.error ? `<br><span class="err">${escapeHtml(app.error)}</span>` : "";
      let button;
      if (isRunning) {
        button = `<button class="btn stop" data-app="${name}" data-action="stop" ${state.appBusy ? "disabled" : ""}>Stop</button>`;
      } else if (isLoading) {
        button = `<button class="btn pending" disabled>Loading</button>`;
      } else if (isStopping) {
        button = `<button class="btn pending" disabled>Stopping</button>`;
      } else {
        button = `<button class="btn start" data-app="${name}" data-action="start" ${(active || state.appBusy) ? "disabled" : ""}>Start</button>`;
      }
      const frag = LOADING_FRAGMENTS[loadingTick % LOADING_FRAGMENTS.length];
      const spinnerMarkup = isLoading ? `<span class="spinner-glyph" aria-hidden="true"></span><span class="loading-frag">${escapeHtml(frag)}</span>` : "";
      const stoppingMarkup = isStopping ? `<span class="spinner-glyph" aria-hidden="true"></span><span class="stopping-label">Stopping…</span>` : "";
      const stateContent = isRunning ? `<span class="up">running</span>${external}${pid}${uptime}` : isLoading ? spinnerMarkup : isStopping ? stoppingMarkup : escapeHtml(app.status || "stopped");
      return `<div class="app-card ${statusClass}">
        <span class="dot ${isRunning ? "live" : isLoading ? "loading" : isStopping ? "stopping" : isError ? "error" : ""}"></span>
        <div><div class="app-name">${escapeHtml(name)}</div>
        <div class="app-state">${stateContent}${error}</div></div>
        ${button}
      </div>`;
    }).join("");

    const robotDotClass = `dot ${active ? "live" : "ok"}`;
    const robotValue = active ? active.name : "free";

    // pollAppsStatus re-renders at 1 Hz even when nothing changed; skip the
    // innerHTML rebuild (parse + reflow) + status writes when the output is
    // identical. The rendered markup IS the signature, so it is correct by
    // construction: a running app's ticking uptime changes it (rebuild, as before),
    // while an idle list is stable → zero rebuilds (#43).
    const sig = `${html} ${robotDotClass} ${robotValue}`;
    if (sig === lastAppsSig) return;
    lastAppsSig = sig;

    $("apps").innerHTML = html;
    $("robot-dot").className = robotDotClass;
    $("robot-value").textContent = robotValue;
  }

  function formatUptime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "";
    const total = Math.floor(seconds);
    const minutes = Math.floor(total / 60);
    const hours = Math.floor(minutes / 60);
    if (hours > 0) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
    return `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
  }

  async function pollAppsStatus() {
    try {
      loadingTick++;
      const statuses = await apiJson("/api/apps/status");
      if (Array.isArray(statuses)) {
        statuses.forEach((app) => appState.set(app.name, app));
        renderApps(Array.from(appState.values()));
        $("apps-message").textContent = "";
        $("apps-message").className = "panel-hint";
      }
    } catch (error) {
      $("apps-message").textContent = error.message || "App status unavailable";
      $("apps-message").className = "panel-hint error";
    }
  }

  async function performAppAction(name, action) {
    state.appBusy = true;
    $("apps-message").textContent = `${action === "start" ? "starting" : "stopping"} ${name}`;
    $("apps-message").className = "panel-hint";
    renderApps(Array.from(appState.values()));
    try {
      const appPath = action === "start" ? `/api/apps/start/${encodeURIComponent(name)}` : `/api/apps/stop/${encodeURIComponent(name)}`;
      const result = await apiJson(appPath, { method: "POST" });
      if (result && (result.status === "refused" || result.error)) {
        throw new Error(result.error || "Request refused");
      }
      await pollAppsStatus();
      $("apps-message").textContent = result && result.status ? `${name} ${result.status}` : "";
      $("apps-message").className = "panel-hint ok";
    } catch (error) {
      $("apps-message").textContent = error.message || "App request failed";
      $("apps-message").className = "panel-hint error";
      await pollAppsStatus();
    } finally {
      state.appBusy = false;
      renderApps(Array.from(appState.values()));
    }
  }

  // Expression datasets served by the daemon (via the transparent /api proxy).
  // Each strip's dataset id also lives on its container's data-dataset attribute.
  const MOVE_DATASETS = [
    ["expr-emotions", "pollen-robotics/reachy-mini-emotions-library"],
    ["expr-dances", "pollen-robotics/reachy-mini-dances-library"],
  ];

  // Populate both filmstrips from the daemon once it is reachable. Idempotent:
  // guarded by exprLoaded/exprLoading so the ~10 Hz online reconcile is a cheap
  // early return once the (static) move lists are in.
  async function loadExpressions() {
    if (state.exprLoaded || state.exprLoading) return;
    state.exprLoading = true;
    try {
      const groups = await Promise.all(
        MOVE_DATASETS.map(async ([id, dataset]) => {
          const data = await apiJson(`/api/move/recorded-move-datasets/list/${dataset}`);
          const moves = Array.isArray(data) ? data : (data && (data.moves || data.names || data.items)) || [];
          return { id, moves };
        }),
      );
      const disabled = state.daemonOnline ? "" : "disabled";
      groups.forEach(({ id, moves }) => {
        $(id).innerHTML = moves
          .map((move) => `<button class="chip" type="button" data-move="${escapeHtml(move)}" ${disabled}>${escapeHtml(move)}</button>`)
          .join("");
      });
      state.exprLoaded = true;
      $("expr-hint").hidden = true;

      // Sync-on-load: check if a move is currently active (multi-tab scenario or
      // reload mid-playback). If so, enter busy state + polling without a .playing
      // chip if the move name isn't yet rendered.
      if (state.daemonOnline) {
        try {
          const current = await apiJson("/api/expressions/current");
          if (current && typeof current === "object" && current.active === true) {
            // An expression lifecycle is active; enter busy state and start polling.
            state.exprBusy = true;
            document.querySelectorAll(".chip").forEach((c) => { c.disabled = true; });
            // Try to mark the tapped chip if it's in the strips; otherwise skip.
            const targetChip = document.querySelector(`.chip[data-move="${escapeHtml(current.move)}"]`);
            if (targetChip && current.phase === "playing") {
              targetChip.classList.add("playing");
            }
            // Show cancel only during phase === "playing"; hide during "returning_home".
            $("expr-cancel").hidden = current.phase !== "playing";
            startExpressionPoller();
          }
        } catch (_) {
          // Sync-on-load fetch failed — leave the UI in normal state; the next
          // live poll (if running) will reconcile.
        }
      }
    } catch (error) {
      // Leave the strips empty + the hint up; the next online reconcile retries.
    } finally {
      state.exprLoading = false;
    }
  }

  // Track the polling poller so only one runs at a time. If the user double-clicks
  // or rapid-retries while a lifecycle is in flight, we must not spawn concurrent
  // pollers — the guard ensures new requests are rejected (exprBusy stays true) and
  // the single poller drives the state machine to completion.
  let exprPoller = null;

  async function playExpression(dataset, move, chip) {
    if (state.exprBusy) return;
    state.exprBusy = true;
    document.querySelectorAll(".chip").forEach((c) => { c.disabled = true; });
    chip.classList.add("playing");
    $("expr-cancel").hidden = false;
    $("expr-hint").hidden = true;
    $("expr-hint").className = "panel-hint";
    try {
      const result = await apiJson(`/api/expressions/play/${dataset}/${encodeURIComponent(move)}`, { method: "POST" });
      // On 200, the backend returns { status, uuid, dataset, move }; start polling
      // until the move finishes (active === false on /api/expressions/current).
      if (result && typeof result === "object") {
        startExpressionPoller();
        return;
      }
      throw new Error("Invalid response");
    } catch (error) {
      // 409 → hint "Robot is busy with another move." (apiJson surfaces this from
      // the response's detail field). Other errors → propagate as-is.
      $("expr-hint").hidden = false;
      $("expr-hint").textContent = error.message || "Move failed";
      $("expr-hint").className = "panel-hint error";
      chip.classList.remove("playing");
      state.exprBusy = false;
      const disabled = !state.daemonOnline;
      document.querySelectorAll(".chip").forEach((c) => { c.disabled = disabled; });
      $("expr-cancel").hidden = true;
      $("expr-cancel").disabled = false;
    }
  }

  function startExpressionPoller() {
    // Guard: only one poller at a time. If one is already running (e.g. from a
    // previous lifecycle), do not spawn another — the new playExpression never gets
    // here because state.exprBusy rejects concurrent requests earlier.
    if (exprPoller !== null) return;

    exprPoller = setInterval(async () => {
      try {
        const current = await apiJson("/api/expressions/current");
        if (!current || typeof current !== "object") {
          // Malformed response — treat as inactive to stop polling.
          stopExpressionPoller(true);
          return;
        }

        if (current.active === false) {
          // Playback finished; stop polling and reconcile the UI.
          stopExpressionPoller(true);
          return;
        }

        // Move is active — update UI based on phase. Default to hiding cancel
        // unless explicitly playing; unknown phases hide the button but polling
        // continues (the session clears server-side, which flips active to false).
        if (current.phase === "playing") {
          $("expr-cancel").hidden = false;
        } else {
          $("expr-cancel").hidden = true;
        }
      } catch (_) {
        // Polling fetch failed (controller restart / network) — treat as inactive:
        // clear busy state, reconcile chips to daemon-online state, stop polling.
        // Don't spin forever; a transient network error should not block the UI.
        stopExpressionPoller(true);
      }
    }, 750);
  }

  function stopExpressionPoller(reconcile) {
    if (exprPoller !== null) {
      clearInterval(exprPoller);
      exprPoller = null;
    }
    if (reconcile) {
      // Polling complete: reconcile the UI to the daemon-online state.
      state.exprBusy = false;
      const disabled = !state.daemonOnline;
      document.querySelectorAll(".chip").forEach((c) => {
        c.disabled = disabled;
        c.classList.remove("playing");
      });
      $("expr-cancel").hidden = true;
      $("expr-hint").hidden = true;
      $("expr-hint").className = "panel-hint";
    }
  }

  function gauge(element, label, color) {
    const radius = 34;
    const circumference = TAU * radius;
    element.setAttribute("role", "img");
    element.setAttribute("aria-label", `${label} —`);
    element.innerHTML = `<svg viewBox="0 0 84 84" aria-hidden="true"><circle cx="42" cy="42" r="${radius}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="7"></circle>
      <circle cx="42" cy="42" r="${radius}" fill="none" stroke="${color}" stroke-width="7" stroke-linecap="round" transform="rotate(-90 42 42)" stroke-dasharray="${circumference}" stroke-dashoffset="${circumference}" class="arc"></circle>
      <text x="42" y="40" text-anchor="middle" class="gv">—</text><text x="42" y="55" text-anchor="middle" class="gu"></text></svg><span class="gl">${label}</span>`;
    const arc = element.querySelector(".arc");
    const value = element.querySelector(".gv");
    const unit = element.querySelector(".gu");
    return (percent, text, suffix = "") => {
      const normalized = percent == null ? 0 : clamp(Number(percent), 0, 100);
      arc.style.transition = reduceMotion ? "none" : "stroke-dashoffset .6s var(--ease)";
      arc.style.strokeDashoffset = String(circumference * (1 - normalized / 100));
      value.textContent = text;
      unit.textContent = suffix;
      element.setAttribute("aria-label", `${label} ${text}${suffix}`.trim());
    };
  }

  // One accent per panel: all three arcs follow the theme accent; the labels
  // differentiate the metrics (accent-discipline pass, #49).
  const setCpuRaw = gauge($("g-cpu"), "cpu", "var(--amber)");
  const setGpuRaw = gauge($("g-gpu"), "gpu", "var(--amber)");
  const setTempRaw = gauge($("g-temp"), "temp", "var(--amber)");

  // Change-guard caches for updateStats
  const statsCache = {
    cpu: null,
    gpu: null,
    temp: null,
    mem: null,
    memWidth: null,
    host: null,
    card: null,
    sink: null,
  };

  const makeGuardedSetter = (cacheKey, raw) => (value, text, suffix) => {
    const key = JSON.stringify({ value, text, suffix });
    if (key !== statsCache[cacheKey]) {
      statsCache[cacheKey] = key;
      raw(value, text, suffix);
    }
  };
  const setCpu = makeGuardedSetter("cpu", setCpuRaw);
  const setGpu = makeGuardedSetter("gpu", setGpuRaw);
  const setTemp = makeGuardedSetter("temp", setTempRaw);

  function updateStats(stats) {
    if (!stats || typeof stats !== "object") return;
    const cpu = finite(stats.cpu_percent);
    const gpu = finite(stats.gpu_percent);
    const temperature = finite(stats.cpu_temp_c) ?? finite(stats.gpu_temp_c);
    setCpu(cpu, cpu == null ? "—" : cpu.toFixed(0), cpu == null ? "" : "%");
    setGpu(gpu, gpu == null ? "—" : gpu.toFixed(0), gpu == null ? "" : "%");
    setTemp(temperature == null ? null : (temperature / 90) * 100, temperature == null ? "—" : temperature.toFixed(0), temperature == null ? "" : "°C");

    const memUsedMb = finite(stats.memory_used_mb);
    const memTotalMb = finite(stats.memory_total_mb);
    if (memUsedMb != null && memTotalMb) {
      const usedGb = memUsedMb / 1024;
      const totalGb = memTotalMb / 1024;
      const memText = `${usedGb.toFixed(1)} / ${totalGb.toFixed(1)} GB`;
      const memWidth = `${clamp((memUsedMb / memTotalMb) * 100, 0, 100).toFixed(0)}%`;
      if (memText !== statsCache.mem || memWidth !== statsCache.memWidth) {
        statsCache.mem = memText;
        statsCache.memWidth = memWidth;
        $("v-mem").textContent = memText;
        $("bar-mem").style.width = memWidth;
      }
    } else {
      if (statsCache.mem !== "— / — GB" || statsCache.memWidth !== "0") {
        statsCache.mem = "— / — GB";
        statsCache.memWidth = "0";
        $("v-mem").textContent = "— / — GB";
        $("bar-mem").style.width = "0";
      }
    }

    const hostText = "jetson stats";
    if (hostText !== statsCache.host) {
      statsCache.host = hostText;
      $("v-host").textContent = hostText;
    }

    const cardText = stats.alsa_card == null ? "not found" : `card ${stats.alsa_card} · PCM,1 ${stats.alsa_pcm_volume ?? "—"}%`;
    if (cardText !== statsCache.card) {
      statsCache.card = cardText;
      $("v-card").textContent = cardText;
    }

    const sinkText = stats.pipewire_sink_volume == null ? "not found" : Number(stats.pipewire_sink_volume).toFixed(2);
    if (sinkText !== statsCache.sink) {
      statsCache.sink = sinkText;
      $("v-sink").textContent = sinkText;
    }
  }

  const NBARS = 36;
  const eqEl = $("eq");
  const bars = Array.from({ length: NBARS }, () => {
    const bar = document.createElement("div");
    bar.className = "bar";
    eqEl.appendChild(bar);
    return bar;
  });
  const eqState = new Array(NBARS).fill(0);
  const eqProfile = Array.from({ length: NBARS }, (_, index) => 0.24 + Math.sin((index / (NBARS - 1)) * Math.PI) * 0.76);
  const eqLast = new Array(NBARS).fill(null);
  let eqSettled = false;
  let eqPeakLast = null;

  function renderEqualizer(speechDetected, label = null) {
    const peak = label || (speechDetected ? "speech detected" : "no audio signal");
    if (peak !== eqPeakLast) {
      eqPeakLast = peak;
      $("eq-peak").textContent = peak;
    }
    // Once idle has settled to the floor, do zero per-bar work each tick. The
    // 20 Hz audio stream otherwise rewrites 36 bar heights forever, and those
    // bars sit inside a backdrop-filtered panel that iOS re-rasterizes on every
    // write — the focused-tab battery/heat drain (#36).
    if (!speechDetected && eqSettled) return;
    eqEl.classList.toggle("idle", !speechDetected);
    const normalized = speechDetected ? 0.7 : 0.0;
    let settled = !speechDetected;
    bars.forEach((bar, index) => {
      const target = 5 + normalized * 86 * eqProfile[index];
      eqState[index] += (target - eqState[index]) * 0.42;
      if (!speechDetected && Math.abs(eqState[index] - target) < 0.1) {
        eqState[index] = target;
      } else if (!speechDetected) {
        settled = false;
      }
      const next = `${Math.max(4, eqState[index]).toFixed(1)}%`;
      if (next !== eqLast[index]) {
        eqLast[index] = next;
        bar.style.height = next;
      }
    });
    eqSettled = settled;
  }

  function syncSlider(id, value) {
    const input = $(id);
    const display = $(`${id}-v`);
    if (!input || !display || value == null) return;
    // Never clobber a slider the user is actively dragging. On touch a range
    // input never becomes document.activeElement, so the focus check alone
    // misses pointer drags — reassigning input.value mid-drag aborts the touch
    // interaction and snaps to the daemon value (#38).
    if (state.sliderActive === id || document.activeElement === input) return;
    const next = String(clamp(Number(value), 0, 100));
    input.value = next;
    display.textContent = next;
    input.style.setProperty("--fill", `${next}%`);
  }

  function updateSliderFill(id) {
    const input = $(id);
    const display = $(`${id}-v`);
    if (!input || !display) return;
    display.textContent = input.value;
    input.style.setProperty("--fill", `${input.value}%`);
  }

  async function loadControls() {
    if (!state.daemonOnline) return;
    const [speaker, microphone, motors] = await Promise.allSettled([
      apiJson("/api/volume/current"),
      apiJson("/api/volume/microphone/current"),
      apiJson("/api/motors/status"),
    ]);
    if (speaker.status === "fulfilled") syncSlider("vol-spk", speaker.value && speaker.value.volume);
    if (microphone.status === "fulfilled") syncSlider("vol-mic", microphone.value && microphone.value.volume);
    if (motors.status === "fulfilled") selectMotorMode(motors.value && motors.value.mode);
    state.controlsLoaded = true;
    setControlsEnabled(state.daemonOnline && !state.controlBusy);
  }

  function selectMotorMode(mode) {
    state.robotControlMode = typeof mode === "string" ? mode : null;
    document.querySelectorAll("#motor-seg button").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === mode);
    });
  }


  function debounce(fn, delay) {
    let timer = 0;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), delay);
    };
  }

  async function performControl(label, operation) {
    if (!state.daemonOnline) {
      $("control-status").textContent = "offline";
      $("control-hint").textContent = "Controls enable when the daemon is online.";
      return null;
    }
    state.controlBusy = true;
    setControlsEnabled(false);
    $("control-status").textContent = label;
    try {
      const result = await operation();
      $("control-status").textContent = "ok";
      $("control-hint").textContent = "";
      $("control-hint").className = "panel-hint";
      return result;
    } catch (error) {
      $("control-status").textContent = "error";
      $("control-hint").textContent = error.message || "Control request failed";
      $("control-hint").className = "panel-hint error";
      return null;
    } finally {
      state.controlBusy = false;
      setControlsEnabled(state.daemonOnline);
    }
  }

  // Lightweight volume save path (#38): POST directly without disabling the slider
  // being dragged. setControlsEnabled() is reserved for daemon-offline + discrete ops.
  const volumeSave = {
    "vol-spk": { inflight: false, dirty: false },
    "vol-mic": { inflight: false, dirty: false },
  };

  async function saveVolume(id, label, endpoint) {
    if (!state.daemonOnline) return;
    const slot = volumeSave[id];
    if (slot.inflight) {
      // A request is already in flight; remember that the value moved again and
      // re-send the freshest value once it returns (coalesce, don't queue per-event).
      slot.dirty = true;
      return;
    }
    slot.inflight = true;
    $("control-status").textContent = label;
    try {
      const value = Number($(id).value);
      await apiJson(endpoint, { method: "POST", body: JSON.stringify({ volume: value }) });
      $("control-status").textContent = "ok";
      $("control-hint").textContent = "";
      $("control-hint").className = "panel-hint";
    } catch (error) {
      $("control-status").textContent = "error";
      $("control-hint").textContent = error.message || "Volume request failed";
      $("control-hint").className = "panel-hint error";
    } finally {
      slot.inflight = false;
      if (slot.dirty) {
        slot.dirty = false;
        void saveVolume(id, label, endpoint);
      }
    }
  }

  function bindSliders() {
    const saves = {
      "vol-spk": debounce(() => void saveVolume("vol-spk", "speaker", "/api/volume/set"), 240),
      "vol-mic": debounce(() => void saveVolume("vol-mic", "microphone", "/api/volume/microphone/set"), 240),
    };

    // End of an active drag: clear the guard, reconcile the disabled state we
    // deliberately left untouched while dragging, and persist the released value.
    function endInteraction() {
      const id = state.sliderActive;
      if (id === null) return;
      state.sliderActive = null;
      setControlsEnabled(state.daemonOnline && !state.controlBusy);
      saves[id]();
    }

    Object.keys(saves).forEach((id) => {
      const input = $(id);
      updateSliderFill(id);
      input.addEventListener("pointerdown", () => { state.sliderActive = id; });
      input.addEventListener("input", () => {
        state.sliderActive = id;
        updateSliderFill(id);
        saves[id]();
      });
      input.addEventListener("change", endInteraction);
      input.addEventListener("blur", endInteraction);
    });

    // Pointer release can land off the thumb (touch drags wander), so clear the
    // guard from the window — a stuck flag would freeze daemon→slider sync.
    window.addEventListener("pointerup", endInteraction);
    window.addEventListener("pointercancel", endInteraction);
  }

  function makeSim(canvas) {
    const ctx = canvas.getContext("2d");
    let raf = 0;
    let running = false;
    let time = 0;

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * ratio));
      canvas.height = Math.max(1, Math.floor(rect.height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      return rect;
    }

    function draw() {
      const rect = resize();
      const width = rect.width;
      const height = rect.height;
      time += 0.016;
      const gradient = ctx.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, "#0a1018");
      gradient.addColorStop(1, "#05080e");
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, width, height);

      const centerX = width / 2;
      const horizon = height * 0.46;
      ctx.strokeStyle = "rgba(111,227,255,0.16)";
      ctx.lineWidth = 1;
      for (let i = -6; i <= 6; i += 1) {
        const vx = centerX + i * width * 0.5 / 6;
        ctx.beginPath();
        ctx.moveTo(centerX + i * 14, horizon);
        ctx.lineTo(vx, height);
        ctx.stroke();
      }
      for (let j = 1; j <= 7; j += 1) {
        const y = horizon + (height - horizon) * Math.pow(j / 7, 2);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
      }

      const skew = Math.sin(time * 0.4) * 5;
      const bob = Math.sin(time * 1.2) * 4;
      const x = centerX;
      const y = horizon + (height - horizon) * 0.34 + bob;
      ctx.save();
      ctx.translate(x, y);
      ctx.fillStyle = "rgba(0,0,0,0.35)";
      ctx.beginPath();
      ctx.ellipse(0, 46, 42, 10, 0, 0, TAU);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.10)";
      ctx.strokeStyle = "rgba(255,255,255,0.22)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.ellipse(0, 36, 34, 12, 0, 0, TAU);
      ctx.fill();
      ctx.stroke();
      roundedRect(ctx, -32 + skew, -34, 64, 64, 20);
      const headGradient = ctx.createLinearGradient(0, -34, 0, 30);
      headGradient.addColorStop(0, "rgba(255,255,255,0.22)");
      headGradient.addColorStop(1, "rgba(255,255,255,0.05)");
      ctx.fillStyle = headGradient;
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#05060c";
      ctx.beginPath();
      ctx.arc(-12 + skew, -2, 6, 0, TAU);
      ctx.arc(14 + skew, -2, 6, 0, TAU);
      ctx.fill();
      ctx.fillStyle = "#6fe3ff";
      ctx.beginPath();
      ctx.arc(-11 + skew, -3, 2, 0, TAU);
      ctx.arc(15 + skew, -3, 2, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = "#6fe3ff";
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(-20 + skew, -34);
      ctx.lineTo(-30 + skew + Math.sin(time * 2) * 4, -64);
      ctx.moveTo(20 + skew, -34);
      ctx.lineTo(30 + skew + Math.sin(time * 2 + 1) * 4, -64);
      ctx.stroke();
      ctx.fillStyle = "#6fe3ff";
      ctx.beginPath();
      ctx.arc(-30 + skew + Math.sin(time * 2) * 4, -64, 4, 0, TAU);
      ctx.arc(30 + skew + Math.sin(time * 2 + 1) * 4, -64, 4, 0, TAU);
      ctx.fill();
      ctx.restore();
      drawGizmo(ctx, 34, height - 34, time);

      if (running && !reduceMotion) raf = requestAnimationFrame(draw);
    }

    function roundedRect(context, x, y, width, height, radius) {
      context.beginPath();
      context.moveTo(x + radius, y);
      context.arcTo(x + width, y, x + width, y + height, radius);
      context.arcTo(x + width, y + height, x, y + height, radius);
      context.arcTo(x, y + height, x, y, radius);
      context.arcTo(x, y, x + width, y, radius);
    }

    function drawGizmo(context, x, y, timeValue) {
      const axes = [["#ff6b6b", 0, "x"], ["#5ee6c3", 2.09, "y"], ["#6fe3ff", 4.18, "z"]];
      context.lineWidth = 2;
      context.font = "9px 'JetBrains Mono'";
      axes.forEach(([color, offset, label]) => {
        const angle = offset + Math.sin(timeValue * 0.4) * 0.4;
        const endX = x + Math.cos(angle) * 16;
        const endY = y - Math.sin(angle) * 9.6;
        context.strokeStyle = color;
        context.beginPath();
        context.moveTo(x, y);
        context.lineTo(endX, endY);
        context.stroke();
        context.fillStyle = color;
        context.fillText(label, endX + 2, endY);
      });
    }

    return {
      start() {
        running = true;
        cancelAnimationFrame(raf);
        draw();
      },
      stop() {
        running = false;
        cancelAnimationFrame(raf);
      },
    };
  }

  $("vp-switch").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (button) setMode(button.dataset.mode);
  });

  $("apps").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || state.appBusy) return;
    void performAppAction(button.dataset.app, button.dataset.action);
  });

  ["expr-emotions", "expr-dances"].forEach((id) => {
    $(id).addEventListener("click", (event) => {
      const chip = event.target.closest("button.chip");
      if (!chip || chip.disabled || state.exprBusy) return;
      const strip = chip.closest(".expr-strip");
      void playExpression(strip.dataset.dataset, chip.dataset.move, chip);
    });
  });

  $("expr-cancel").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    // Disable immediately on first click; POST /api/expressions/stop is idempotent
    // server-side, so accidental double-click is safe. Polling continues until
    // the move finishes (active === false).
    button.disabled = true;
    try {
      await apiJson("/api/expressions/stop", { method: "POST" });
    } catch (error) {
      // Log silently; the next poll will reconcile the true state.
    } finally {
      button.disabled = false;
    }
  });

  bindSliders();

  $("motor-seg").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (!button) return;
    const mode = button.dataset.mode;
    if (mode === "disabled" && !window.confirm("Disable motors? The robot will go limp. Use Wake to recover.")) return;
    void performControl("motor", async () => {
      const result = await apiJson(`/api/motors/set_mode/${encodeURIComponent(mode)}`, { method: "POST" });
      selectMotorMode(mode);
      return result;
    });
  });

  $("wobble").addEventListener("change", (event) => {
    const checked = event.target.checked;
    void performControl("wobble", async () => {
      const result = await apiJson(`/api/media/wobbling/${checked ? "enable" : "disable"}`, { method: "POST" });
      return result;
    }).then((result) => {
      if (result == null) event.target.checked = !checked;
      const hintEl = $("wobble-hint");
      if (checked && result != null) {
        hintEl.removeAttribute("hidden");
      } else {
        hintEl.setAttribute("hidden", "");
      }
    });
  });

  $("testsound").addEventListener("click", () => {
    const button = $("testsound");
    button.textContent = "Playing…";
    void performControl("test sound", () => apiJson("/api/volume/test-sound", { method: "POST" })).finally(() => {
      button.textContent = "Play test sound";
    });
  });

  // Apply the daemon power-button presentation, writing only what changed. This
  // poll runs at 1 Hz and previously rewrote classList + title + aria-label +
  // disabled every tick regardless; per-write guards make a steady tick a no-op
  // while still reconciling the authoritative state every poll (#43).
  function applyDaemonPower(powerClass, title, disabled) {
    const vpPower = $("vp-power");
    const button = $("daemon-power");
    // toggle(cls, force) is a no-op (no DOM write) when the class is already in the
    // wanted state, so it self-guards — no contains() pre-check needed here.
    for (const cls of ["running", "starting", "stopping", "disabled"]) {
      vpPower.classList.toggle(cls, cls === powerClass);
    }
    if (button.title !== title) button.title = title;
    if (button.getAttribute("aria-label") !== title) button.setAttribute("aria-label", title);
    if (button.disabled !== disabled) button.disabled = disabled;
  }

  async function pollDaemonStatus() {
    if (state.daemonStatusInflight) return;
    state.daemonStatusInflight = true;
    try {
      const status = await apiJson("/api/daemon/status");

      let powerClass = "";
      let title = "Start daemon";
      let disabled = false;

      if (state.daemonStopping && status.running) {
        // Still shutting down — hold the OFF state and keep the button inert.
        powerClass = "stopping";
        title = "Daemon stopping…";
        disabled = true;
      } else {
        // Daemon is down — clear the transitional state, fall through to red default.
        if (state.daemonStopping) state.daemonStopping = false;
        if (!status.manageable) {
          powerClass = "disabled";
          title = "Daemon managed on device";
          disabled = true;
        } else if (status.running) {
          powerClass = "running";
          title = "Stop daemon";
          disabled = false;
        } else if (status.status === "starting") {
          powerClass = "starting";
          title = "Daemon starting…";
          disabled = true;
        }
        // else: defaults above (no power class, "Start daemon", enabled).
      }

      applyDaemonPower(powerClass, title, disabled);
    } catch (_) {
      // Silent fail — let the daemon online/offline mechanism handle it
    } finally {
      state.daemonStatusInflight = false;
    }
  }

  $("daemon-power").addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;

    const vpPower = $("vp-power");
    const isRunning = vpPower.classList.contains("running");

    if (isRunning) {
      state.controlBusy = true;
      state.daemonStopping = true;
      vpPower.classList.remove("running", "starting");
      vpPower.classList.add("stopping");
      button.disabled = true;
      button.title = "Daemon stopping…";
      button.setAttribute("aria-label", "Daemon stopping…");
      $("control-status").textContent = "stopping";
      try {
        await apiJson("/api/daemon/stop", { method: "POST" });
      } catch (error) {
        state.daemonStopping = false;
        vpPower.classList.remove("stopping");
        $("control-status").textContent = "error";
        $("control-hint").textContent = error.message || "Failed to stop daemon";
        $("control-hint").className = "panel-hint error";
      } finally {
        state.controlBusy = false;
        void pollDaemonStatus();
      }
    } else {
      state.controlBusy = true;
      $("control-status").textContent = "starting";
      vpPower.classList.add("starting");
      button.disabled = true;
      try {
        await apiJson("/api/daemon/start", { method: "POST" });
      } catch (error) {
        $("control-status").textContent = "error";
        $("control-hint").textContent = error.message || "Failed to start daemon";
        $("control-hint").className = "panel-hint error";
        vpPower.classList.remove("starting");
        button.disabled = false;
      } finally {
        state.controlBusy = false;
        void pollDaemonStatus();
      }
    }
  });

  // ---- Theme selector (status-bar palette button → top-layer popover) ----
  // A pure UI preference — works with the daemon offline. The saved theme is
  // already applied pre-paint by the inline <head> script; here we build the
  // menu, persist changes, and keep the popover positioned under the button.
  const THEME_SCHEMES = [
    { id: "amber", label: "Amber", sw: "#ffc857" },
    { id: "arctic", label: "Arctic", sw: "#7fe0ff" },
    { id: "emerald", label: "Emerald", sw: "#3ce3a0" },
    { id: "ultraviolet", label: "Violet", sw: "#c199ff" },
    { id: "ember", label: "Ember", sw: "#ff8a5c" },
  ];
  const CHECK_SVG =
    '<svg class="chk" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>';

  function setupThemeSelector() {
    const btn = $("theme-btn");
    const pop = $("theme-pop");
    if (!btn || !pop) return;
    const currentScheme = () => document.documentElement.dataset.scheme || "amber";

    function renderThemeOptions() {
      const cur = currentScheme();
      pop.querySelectorAll(".theme-opt").forEach((el) => el.remove());
      THEME_SCHEMES.forEach((scheme) => {
        const opt = document.createElement("button");
        opt.className = "theme-opt";
        opt.type = "button";
        opt.setAttribute("role", "menuitemradio");
        opt.dataset.scheme = scheme.id;
        opt.setAttribute("aria-checked", String(scheme.id === cur));
        opt.innerHTML =
          `<span class="sw" style="background:${scheme.sw}"></span>` +
          `<span class="nm">${scheme.label}</span>${CHECK_SVG}`;
        pop.appendChild(opt);
      });
    }

    // The popover lives in the top layer (escapes the status bar's
    // overflow:hidden), so position it under the button ourselves — correct at
    // any viewport width.
    function placePopover() {
      const rect = btn.getBoundingClientRect();
      pop.style.top = `${rect.bottom + 10}px`;
      pop.style.right = `${window.innerWidth - rect.right}px`;
      pop.style.left = "auto";
    }

    renderThemeOptions();

    pop.addEventListener("click", (event) => {
      const opt = event.target.closest(".theme-opt");
      if (!opt) return;
      const scheme = opt.dataset.scheme;
      document.documentElement.dataset.scheme = scheme;
      try { localStorage.setItem("reachy-theme", scheme); } catch (e) {}
      renderThemeOptions();
      if (pop.hidePopover) pop.hidePopover();
    });

    btn.addEventListener("click", () => requestAnimationFrame(placePopover));
    window.addEventListener("resize", () => {
      if (pop.matches && pop.matches(":popover-open")) placePopover();
    });
  }

  function startLiveConnections() {
    if (state.live.active) return;
    state.live.active = true;

    void pollDaemonStatus();
    void pollAppsStatus();
    state.live.timers.push(
      setInterval(() => void pollDaemonStatus(), 1000),
      setInterval(() => void pollAppsStatus(), 1000),
    );

    const telemetry = bindEventSource("/telemetry/state-stream", {
      "state-update": (event) => {
        const payload = readEventPayload(event);
        setDaemonOnline(true);
        applyRobotState(payload);
      },
      "daemon-offline": () => setDaemonOnline(false),
      error: () => setDaemonOnline(false),
    });
    const stats = bindEventSource("/api/system/stats-stream", {
      "stats-update": (event) => updateStats(readEventPayload(event)),
      error: () => {
        $("v-host").textContent = "stats reconnecting";
      },
    });
    const audio = bindEventSource("/api/audio/levels-stream", {
      "audio-level": (event) => {
        const payload = readEventPayload(event);
        const speechDetected = payload && payload.speech_detected === true;
        renderEqualizer(speechDetected);
      },
      "daemon-unavailable": () => renderEqualizer(false, "no audio signal"),
      error: () => renderEqualizer(false, "audio stream reconnecting"),
    });
    state.live.streams.push(telemetry, stats, audio);
  }

  function stopLiveConnections() {
    if (!state.live.active) return;
    state.live.active = false;
    state.live.timers.forEach((id) => clearInterval(id));
    state.live.timers = [];
    state.live.streams.forEach((source) => {
      if (source) source.close();
    });
    state.live.streams = [];
  }

  // Poll daemon status on the same cadence as the daemon connectivity check
  setupThemeSelector();
  renderApps();
  renderEqualizer(false, "no audio signal");
  setDaemonOnline(false);
  // Skip if we boot into an already-hidden tab — visibilitychange starts us when shown.
  if (!document.hidden) startLiveConnections();

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopLiveConnections();
    else startLiveConnections();
  });
})();
