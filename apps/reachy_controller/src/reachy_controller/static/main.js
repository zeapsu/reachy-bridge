(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const appNames = ["reachy_chat", "reachy_dance", "reachy_lookat", "reachy_simon"];
  let currentApp = null;
  let requestBusy = false;

  document.querySelectorAll(".glass").forEach((glass) => glass.addEventListener("pointermove", (event) => {
    const rect = glass.getBoundingClientRect();
    glass.style.setProperty("--mx", `${(event.clientX - rect.left) / rect.width * 100}%`);
    glass.style.setProperty("--my", `${(event.clientY - rect.top) / rect.height * 100}%`);
  }));

  async function api(path, options = {}) {
    const response = await fetch(`/api/${path}`, {
      ...options,
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* response was not JSON */ }
      throw new Error(detail);
    }
    if (response.status === 204 || response.headers.get("content-length") === "0") return null;
    return response.json();
  }

  function setConnection(online) {
    $("daemon-dot").className = `dot ${online ? "ok" : "error"}`;
    $("daemon-value").textContent = online ? "online" : "offline";
    $("mic-dot").className = `dot ${online ? "ok" : "error"}`;
    $("mic-state").textContent = online ? "mic live" : "unavailable";
    document.querySelectorAll("button, input").forEach((element) => {
      if (!element.closest(".vp-switch")) element.disabled = !online || requestBusy;
    });
  }

  function renderApps() {
    const currentName = currentApp?.info?.name || currentApp?.name || currentApp?.app_name || null;
    $("apps").innerHTML = appNames.map((name) => {
      const running = currentName === name;
      const status = running ? (currentApp.state || currentApp.status || "running") : "stopped";
      const pid = running && currentApp.pid ? ` · pid ${currentApp.pid}` : "";
      return `<div class="app-card ${running ? "running" : ""}">
        <span class="dot ${running ? "live" : ""}"></span>
        <div><div class="app-name">${name}</div><div class="app-state">${running ? `<span class="up">${status}</span>${pid}` : status}</div></div>
        <button class="btn ${running ? "stop" : "start"}" data-app="${name}" data-action="${running ? "stop" : "start"}" ${currentName && !running ? "disabled" : ""}>${running ? "Stop" : "Start"}</button>
      </div>`;
    }).join("");
    $("robot-dot").className = `dot ${currentName ? "live" : "ok"}`;
    $("robot-value").textContent = currentName || "free";
  }

  async function pollAppsStatus() {
    try {
      const apps = await api("apps/status");
      if (Array.isArray(apps)) {
        apps.forEach((app) => {
          const elem = document.getElementById(`status-${app.name}`);
          if (elem) {
            elem.textContent = app.status === "running" ? `running (${app.pid})` : app.status;
            elem.className = `app-status ${app.status}`;
          }
        });
      }
    } catch (_) {
      // Silently fail if supervisor not available
    }
  }

  $("apps").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || requestBusy) return;
    await perform(async () => {
      if (button.dataset.action === "start") await api(`apps/start/${encodeURIComponent(button.dataset.app)}`, {method: "POST"});
      else await api(`apps/stop/${encodeURIComponent(button.dataset.app)}`, {method: "POST"});
      await pollAppsStatus();
    }, button.dataset.action === "start" ? "starting app" : "stopping app");
  });

  function poseValues(pose) {
    if (Array.isArray(pose)) return pose.slice(-3);
    if (pose && typeof pose === "object") return [pose.roll, pose.pitch, pose.yaw];
    return [];
  }

  const degrees = (radians) => Number.isFinite(Number(radians)) ? Number(radians) * 180 / Math.PI : null;
  const fixed = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";

  function updateRobot(state) {
    if (!state) return;
    const [roll, pitch, yaw] = poseValues(state.head_pose).map(degrees);
    const antennas = state.antennas_position || [];
    const doa = state.doa?.angle == null ? null : degrees(state.doa.angle);
    $("read-rpy").textContent = [roll, pitch, yaw].map((v) => v == null ? "—" : v.toFixed(0)).join("/");
    $("read-yaw").textContent = fixed(state.body_yaw);
    $("read-ant").textContent = `${fixed(antennas[0])}/${fixed(antennas[1])}`;
    $("read-doa").textContent = doa == null ? "—°" : `${doa.toFixed(0)}°`;
    $("rig-head").setAttribute("transform", `rotate(${fixed(roll, 1)} 160 104) translate(${fixed((yaw || 0) * .3, 1)} ${fixed(-(pitch || 0) * .3, 1)})`);
    $("rig-body").setAttribute("transform", `rotate(${fixed(Number(state.body_yaw || 0) * 12, 1)} 160 152)`);
    $("ant-left").setAttribute("transform", `rotate(${fixed(Number(antennas[0] || 0) * 18, 1)} 132 70)`);
    $("ant-right").setAttribute("transform", `rotate(${fixed(Number(antennas[1] || 0) * 18, 1)} 188 70)`);
    if (doa != null) setDoa(doa);
  }

  function setDoa(angle) {
    const polar = (degreesValue) => {
      const radians = (degreesValue - 90) * Math.PI / 180;
      return [160 + 92 * Math.cos(radians), 108 + 92 * Math.sin(radians)];
    };
    const [x1, y1] = polar(angle - 24), [x2, y2] = polar(angle + 24), [tx, ty] = polar(angle);
    $("doa-arc").setAttribute("d", `M ${x1} ${y1} A 92 92 0 0 1 ${x2} ${y2}`);
    $("doa-tick").setAttribute("cx", tx); $("doa-tick").setAttribute("cy", ty);
  }

  function createGauge(id, label, color) {
    const circumference = 2 * Math.PI * 34;
    $(id).innerHTML = `<svg viewBox="0 0 84 84"><circle cx="42" cy="42" r="34" fill="none" stroke="rgba(255,255,255,.08)" stroke-width="7"/><circle class="arc" cx="42" cy="42" r="34" fill="none" stroke="${color}" stroke-width="7" stroke-linecap="round" transform="rotate(-90 42 42)" stroke-dasharray="${circumference}"/><text class="gauge-value" x="42" y="46" text-anchor="middle">—</text></svg><span class="gauge-label">${label}</span>`;
    const arc = $(id).querySelector(".arc"), value = $(id).querySelector(".gauge-value");
    return (percent, text) => {
      const normalized = Number.isFinite(Number(percent)) ? Math.max(0, Math.min(100, Number(percent))) : 0;
      arc.style.strokeDashoffset = circumference * (1 - normalized / 100);
      value.textContent = text;
    };
  }
  const setCpu = createGauge("gauge-cpu", "cpu", "var(--cyan)");
  const setGpu = createGauge("gauge-gpu", "gpu", "var(--mint)");
  const setTemp = createGauge("gauge-temp", "temp", "var(--amber)");

  function updateHost(host) {
    $("host-name").textContent = host.hostname || "jetson";
    setCpu(host.cpu_percent, host.cpu_percent == null ? "—" : `${Math.round(host.cpu_percent)}%`);
    setGpu(host.gpu_percent, host.gpu_percent == null ? "—" : `${Math.round(host.gpu_percent)}%`);
    setTemp(Number(host.temperature_c) / 90 * 100, host.temperature_c == null ? "—" : `${Math.round(host.temperature_c)}°`);
    const used = Number(host.memory_used_gb), total = Number(host.memory_total_gb);
    $("memory-value").textContent = total ? `${used.toFixed(1)} / ${total.toFixed(1)} GB` : "— / — GB";
    $("memory-bar").style.width = total ? `${used / total * 100}%` : "0";
  }

  function selectMotorMode(mode) {
    document.querySelectorAll("#motor-mode button").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  }

  async function pollTelemetry() {
    try {
      const telemetry = await api("telemetry");
      setConnection(telemetry.daemon.online);
      currentApp = telemetry.app;
      renderApps();
      updateRobot(telemetry.robot.state);
      updateHost(telemetry.host);
      selectMotorMode(telemetry.robot.motor_mode);
      syncVolume("speaker", telemetry.audio.speaker_volume);
      syncVolume("microphone", telemetry.audio.microphone_volume);
    } catch (_) {
      setConnection(false);
    }
  }

  function syncVolume(kind, value) {
    if (value == null || document.activeElement === $(`${kind}-volume`)) return;
    $(`${kind}-volume`).value = value;
    updateSlider(kind);
  }

  function updateSlider(kind) {
    const input = $(`${kind}-volume`);
    $(`${kind}-value`).textContent = input.value;
    input.style.setProperty("--fill", `${input.value}%`);
  }

  function debounce(fn, wait) {
    let timeout;
    return (...args) => { clearTimeout(timeout); timeout = setTimeout(() => fn(...args), wait); };
  }

  ["speaker", "microphone"].forEach((kind) => {
    const path = kind === "speaker" ? "volume/set" : "volume/microphone/set";
    const input = $(`${kind}-volume`);
    const save = debounce(() => perform(() => api(path, {method: "POST", body: JSON.stringify({volume: Number(input.value)})}), `setting ${kind} volume`), 250);
    input.addEventListener("input", () => { updateSlider(kind); save(); });
    updateSlider(kind);
  });

  $("motor-mode").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (button) perform(() => api(`motors/set_mode/${button.dataset.mode}`, {method: "POST"}), "setting motor mode").then(pollTelemetry);
  });
  $("wobble").addEventListener("change", (event) => perform(() => api(`media/wobbling/${event.target.checked ? "enable" : "disable"}`, {method: "POST"}), "setting wobble"));
  $("test-sound").addEventListener("click", (event) => perform(() => api("volume/test-sound", {method: "POST"}), "playing sound", event.target));

  async function perform(operation, label, button = null) {
    requestBusy = true; if (button) button.textContent = "Working…";
    $("control-status").textContent = label;
    try { return await operation(); }
    catch (error) { $("control-status").textContent = error.message; }
    finally { requestBusy = false; if (button) button.textContent = "Play test sound"; setTimeout(() => { $("control-status").textContent = ""; }, 1800); }
  }

  $("viewport-switch").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]"); if (!button) return;
    const mode = button.dataset.mode;
    document.querySelectorAll(".vp-layer").forEach((layer) => layer.classList.toggle("active", layer.dataset.mode === mode));
    document.querySelectorAll("#viewport-switch button").forEach((item) => item.classList.toggle("active", item === button));
    $("viewport-badge").textContent = mode === "sim" ? "SIM · MUJOCO" : mode.toUpperCase();
    $("viewport-name").textContent = mode === "sim" ? "simulation" : mode;
    $("viewport-meta").textContent = mode === "schematic" ? "telemetry 10 hz" : mode === "sim" ? "mujoco preview" : "Milestone 2";
  });

  const bars = Array.from({length: 36}, () => {
    const bar = document.createElement("span"); bar.className = "bar"; $("equalizer").appendChild(bar); return bar;
  });
  function animateBars(time = 0) {
    bars.forEach((bar, index) => { bar.style.height = `${8 + Math.abs(Math.sin(time / 700 + index * .42)) * 58 * Math.sin(index / bars.length * Math.PI)}%`; });
    if (!reduceMotion) requestAnimationFrame(animateBars);
  }
  animateBars();
  renderApps();
  pollTelemetry();
  pollAppsStatus();
  setInterval(pollTelemetry, 1000);
  setInterval(pollAppsStatus, 1000);

  // Subscribe to system stats stream
  const statsSource = new EventSource('/api/system/stats-stream');
  statsSource.addEventListener('stats-update', (event) => {
    const stats = JSON.parse(event.data);

    // Update gauges
    const cpuPercent = stats.cpu_percent ?? 0;
    const gpuPercent = stats.gpu_percent ?? 0;
    const cpuTemp = stats.cpu_temp_c ?? 0;

    $('gauge-cpu').style.width = cpuPercent + '%';
    $('gauge-gpu').style.width = gpuPercent + '%';
    $('gauge-temp').style.width = (cpuTemp / 100 * 100) + '%';

    // Update memory bar
    const memUsed = stats.memory_used_mb ?? 0;
    const memTotal = stats.memory_total_mb ?? 1;
    const memPercent = (memUsed / memTotal) * 100;
    $('memory-value').textContent = (memUsed / 1024).toFixed(1) + ' / ' + (memTotal / 1024).toFixed(1) + ' GB';
    $('memory-bar').style.width = memPercent + '%';

    // Update host info
    const alsaInfo = stats.alsa_card ? 'card ' + stats.alsa_card : 'n/a';
    const pwInfo = stats.pipewire_sink_volume ? (stats.pipewire_sink_volume * 100).toFixed(0) + '%' : 'n/a';
    $('host-name').textContent = 'jetson · alsa ' + alsaInfo + ' · pipewire ' + pwInfo;
  });
  statsSource.addEventListener('error', () => {
    statsSource.close();
    console.error('Stats stream disconnected');
  });
})();
