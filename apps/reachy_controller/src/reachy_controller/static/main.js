(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const TAU = Math.PI * 2;
  const APP_NAMES = ["reachy_chat", "reachy_dance", "reachy_lookat", "reachy_simon"];
  const appState = new Map(APP_NAMES.map((name) => [name, { name, status: "stopped" }]));
  const state = {
    daemonOnline: false,
    controlsLoaded: false,
    controlBusy: false,
    appBusy: false,
    mode: "schematic",
    robotControlMode: null,
    movePollTimer: null,
    movePollInflight: false,
    movePollActive: false,
    simLoop: null,
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
    $("daemon-dot").className = `dot ${online ? "ok" : "error"}`;
    $("daemon-value").textContent = online ? "online" : "offline";
    $("vp-rec").className = `rec ${online ? "ok" : ""}`;
    $("rig").classList.toggle("offline", !online);
    $("rig-note").hidden = online;
    $("mic-dot").className = `dot ${online ? "ok" : "error"}`;
    $("mic-state").textContent = online ? "listening" : "unavailable";
    setControlsEnabled(online && !state.controlBusy);

    if (!online) {
      state.controlsLoaded = false;
      stopMovePolling();
      setPowerState("idle", "");
      $("control-status").textContent = "offline";
      $("control-hint").textContent = "Controls enable when the daemon is online.";
      $("control-hint").className = "panel-hint";
      renderOfflinePose();
      renderEqualizer(false, "no audio signal");
    } else if (changed || !state.controlsLoaded) {
      $("control-status").textContent = "online";
      $("control-hint").textContent = "";
      $("control-hint").className = "panel-hint";
      void loadControls();
    }
  }

  function setControlsEnabled(enabled) {
    ["vol-spk", "vol-mic", "wobble", "testsound"].forEach((id) => {
      const element = $(id);
      if (element) element.disabled = !enabled;
    });
    document.querySelectorAll("#motor-seg button, #power-seg button").forEach((button) => {
      button.disabled = !enabled;
    });
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

    $("r-rpy").textContent = [rollDeg, pitchDeg, yawDeg].map((value) => (value == null ? "—" : value.toFixed(0))).join("/");
    $("r-yaw").textContent = dash(bodyYaw, 2);
    $("r-ant").textContent = `${dash(antLeft, 2)}/${dash(antRight, 2)}`;
    $("r-doa").textContent = doaDeg == null ? "—°" : `${doaDeg.toFixed(0)}°`;

    const roll = rollDeg ?? 0;
    const pitch = pitchDeg ?? 0;
    const yaw = yawDeg ?? 0;
    $("head").setAttribute("transform", `rotate(${roll.toFixed(2)} 160 104) translate(${(yaw * 0.3).toFixed(2)} ${(-pitch * 0.3).toFixed(2)})`);
    $("body").setAttribute("transform", `rotate(${((bodyYaw ?? 0) * 12).toFixed(2)} 160 152)`);
    $("antL").setAttribute("transform", `rotate(${((antLeft ?? 0) * 18).toFixed(2)} 132 70)`);
    $("antR").setAttribute("transform", `rotate(${((antRight ?? 0) * 18).toFixed(2)} 188 70)`);
    $("rig").classList.toggle("speaking", speechDetected && doaDeg != null);

    if (doaDeg == null) {
      $("doaArc").setAttribute("d", "");
    } else {
      setDoa(doaDeg);
    }
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
    const running = statuses.find((app) => app.status === "running") || null;
    $("apps").innerHTML = APP_NAMES.map((name) => {
      const app = statuses.find((item) => item.name === name) || { name, status: "stopped" };
      const isRunning = app.status === "running";
      const isError = app.status === "error";
      const statusClass = isRunning ? "running" : isError ? "error" : "";
      const uptime = isRunning && app.start_time ? ` · ${formatUptime(Date.now() / 1000 - Number(app.start_time))}` : "";
      const pid = isRunning && app.pid ? ` · pid ${app.pid}` : "";
      const error = app.error ? `<br><span class="err">${escapeHtml(app.error)}</span>` : "";
      const button = isRunning
        ? `<button class="btn stop" data-app="${name}" data-action="stop" ${state.appBusy ? "disabled" : ""}>Stop</button>`
        : `<button class="btn start" data-app="${name}" data-action="start" ${(running || state.appBusy) ? "disabled" : ""}>Start</button>`;
      return `<div class="app-card ${statusClass}">
        <span class="dot ${isRunning ? "live" : isError ? "error" : ""}"></span>
        <div><div class="app-name">${escapeHtml(name)}</div>
        <div class="app-state">${isRunning ? `<span class="up">running</span>${pid}${uptime}` : escapeHtml(app.status || "stopped")}${error}</div></div>
        ${button}
      </div>`;
    }).join("");

    $("robot-dot").className = `dot ${running ? "live" : "ok"}`;
    $("robot-value").textContent = running ? running.name : "free";
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

  const setCpu = gauge($("g-cpu"), "cpu", "var(--cyan)");
  const setGpu = gauge($("g-gpu"), "gpu", "var(--mint)");
  const setTemp = gauge($("g-temp"), "temp", "var(--amber)");

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
      $("v-mem").textContent = `${usedGb.toFixed(1)} / ${totalGb.toFixed(1)} GB`;
      $("bar-mem").style.width = `${clamp((memUsedMb / memTotalMb) * 100, 0, 100).toFixed(0)}%`;
    } else {
      $("v-mem").textContent = "— / — GB";
      $("bar-mem").style.width = "0";
    }

    $("v-host").textContent = "jetson stats";
    $("v-card").textContent = stats.alsa_card == null ? "not found" : `card ${stats.alsa_card} · PCM,1 ${stats.alsa_pcm_volume ?? "—"}%`;
    $("v-sink").textContent = stats.pipewire_sink_volume == null ? "not found" : Number(stats.pipewire_sink_volume).toFixed(2);
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

  function renderEqualizer(speechDetected, label = null) {
    const normalized = speechDetected ? 0.7 : 0.0;
    eqEl.classList.toggle("idle", !speechDetected);
    bars.forEach((bar, index) => {
      const target = 5 + normalized * 86 * eqProfile[index];
      eqState[index] += (target - eqState[index]) * 0.42;
      bar.style.height = `${Math.max(4, eqState[index]).toFixed(1)}%`;
    });
    $("eq-peak").textContent = label || (speechDetected ? "speech detected" : "no audio signal");
  }

  function syncSlider(id, value) {
    const input = $(id);
    const display = $(`${id}-v`);
    if (!input || !display || value == null || document.activeElement === input) return;
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
    const hint = $("motor-hint");
    if (hint) hint.hidden = mode !== "disabled";
  }

  function setPowerState(label, dotClass = "") {
    $("power-label").textContent = label;
    $("power-dot").className = `dot ${dotClass}`.trim();
  }

  function runningMoveNames(payload) {
    if (!payload) return [];
    if (Array.isArray(payload)) return payload.filter(Boolean).map(String);
    if (typeof payload !== "object") return [];

    const running = payload.running;
    if (Array.isArray(running)) return running.filter(Boolean).map(String);
    if (typeof running === "string") return running ? [running] : [];
    if (running && typeof running === "object") {
      const name = running.name || running.move || running.id;
      return name ? [String(name)] : ["running"];
    }
    if (running === true) {
      const name = payload.name || payload.move || payload.current_move;
      return name ? [String(name)] : ["running"];
    }

    const current = payload.current_move || payload.move || payload.name;
    if (current && running !== false) return [String(current)];
    return [];
  }

  function stopMovePolling() {
    state.movePollActive = false;
    if (state.movePollTimer) {
      window.clearTimeout(state.movePollTimer);
      state.movePollTimer = null;
    }
  }

  function scheduleMovePoll(delay = 500) {
    if (!state.movePollActive) return;
    if (state.movePollTimer) window.clearTimeout(state.movePollTimer);
    state.movePollTimer = window.setTimeout(() => {
      state.movePollTimer = null;
      void pollMoveTick();
    }, delay);
  }

  async function pollMoveTick() {
    if (!state.movePollActive) return;
    if (state.movePollInflight) {
      scheduleMovePoll();
      return;
    }

    state.movePollInflight = true;
    try {
      const payload = await apiJson("/api/move/running");
      const running = runningMoveNames(payload);
      if (running.length === 0) {
        stopMovePolling();
        setPowerState("idle", "ok");
      } else {
        setPowerState("moving…", "live");
        scheduleMovePoll();
      }
    } catch (_) {
      stopMovePolling();
      setPowerState("unavailable", "error");
    } finally {
      state.movePollInflight = false;
    }
  }

  function pollMoveRunning() {
    state.movePollActive = true;
    setPowerState("moving…", "live");
    void pollMoveTick();
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

  function bindSliders() {
    const speakerSave = debounce(() => {
      const value = Number($("vol-spk").value);
      void performControl("speaker", () => apiJson("/api/volume/set", { method: "POST", body: JSON.stringify({ volume: value }) }));
    }, 240);
    const micSave = debounce(() => {
      const value = Number($("vol-mic").value);
      void performControl("microphone", () => apiJson("/api/volume/microphone/set", { method: "POST", body: JSON.stringify({ volume: value }) }));
    }, 240);

    [["vol-spk", speakerSave], ["vol-mic", micSave]].forEach(([id, save]) => {
      updateSliderFill(id);
      $(id).addEventListener("input", () => {
        updateSliderFill(id);
        save();
      });
    });
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

  $("power-seg").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-move]");
    if (!button) return;
    const move = button.dataset.move;
    void performControl("power", async () => {
      if (move === "wake_up") {
        await apiJson("/api/motors/set_mode/enabled", { method: "POST" });
      }
      const result = await apiJson(`/api/move/play/${encodeURIComponent(move)}`, { method: "POST" });
      pollMoveRunning();
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

  bindEventSource("/telemetry/state-stream", {
    "state-update": (event) => {
      const payload = readEventPayload(event);
      setDaemonOnline(true);
      applyRobotState(payload);
    },
    "daemon-offline": () => setDaemonOnline(false),
    error: () => setDaemonOnline(false),
  });

  bindEventSource("/api/system/stats-stream", {
    "stats-update": (event) => updateStats(readEventPayload(event)),
    error: () => {
      $("v-host").textContent = "stats reconnecting";
    },
  });

  bindEventSource("/api/audio/levels-stream", {
    "audio-level": (event) => {
      const payload = readEventPayload(event);
      const speechDetected = payload && payload.speech_detected === true;
      renderEqualizer(speechDetected);
    },
    "daemon-unavailable": () => renderEqualizer(false, "no audio signal"),
    error: () => renderEqualizer(false, "audio stream reconnecting"),
  });

  renderApps();
  renderEqualizer(false, "no audio signal");
  setDaemonOnline(false);
  void pollAppsStatus();
  setInterval(() => void pollAppsStatus(), 1000);
})();
