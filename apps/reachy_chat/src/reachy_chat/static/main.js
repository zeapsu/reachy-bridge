const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchWithTimeout(url, options = {}, timeoutMs = 2000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(id);
  }
}

async function requestJson(url, options = {}) {
  const response = await fetchWithTimeout(url, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && (data.error || data.status);
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return data;
}

function conciseError(error) {
  if (error && error.name === "AbortError") return "request timed out";
  const message = String(error && error.message ? error.message : error || "request failed")
    .replace(/\s+/g, " ")
    .trim();
  if (!message) return "request failed";
  return message.length > 100 ? `${message.slice(0, 97)}…` : message;
}

function normalizeVoices(value) {
  if (!Array.isArray(value)) {
    throw new Error("invalid voice list");
  }
  const voices = [];
  const seen = new Set();
  for (const candidate of value) {
    if (typeof candidate !== "string" || !candidate.trim()) {
      throw new Error("invalid voice list");
    }
    const voice = candidate.trim();
    if (!seen.has(voice)) {
      seen.add(voice);
      voices.push(voice);
    }
  }
  return voices;
}

function normalizeCurrentVoice(value) {
  if (!value || typeof value.voice !== "string" || !value.voice.trim()) {
    throw new Error("invalid current voice");
  }
  return value.voice.trim();
}

async function waitForStatus(timeoutMs = 15000) {
  const loadingText = document.querySelector("#loading p");
  let attempts = 0;
  const deadline = Date.now() + timeoutMs;
  while (true) {
    attempts += 1;
    try {
      const url = new URL("/status", window.location.origin);
      url.searchParams.set("_", Date.now().toString());
      const resp = await fetchWithTimeout(url, {}, 2000);
      if (resp.ok) return await resp.json();
    } catch (e) {}
    if (loadingText) {
      loadingText.textContent = attempts > 8 ? "Starting backend…" : "Loading…";
    }
    if (Date.now() >= deadline) return null;
    await sleep(500);
  }
}

async function validateKey(key) {
  const body = { openai_api_key: key };
  const resp = await fetch("/validate_api_key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || "validation_failed");
  }
  return data;
}

async function saveKey(key) {
  const body = { openai_api_key: key };
  const resp = await fetch("/openai_api_key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.error || "save_failed");
  }
  return await resp.json();
}

function show(el, flag) {
  el.classList.toggle("hidden", !flag);
}

async function init() {
  const loading = document.getElementById("loading");
  const statusEl = document.getElementById("status");
  const formPanel = document.getElementById("form-panel");
  const configuredPanel = document.getElementById("configured");
  const saveBtn = document.getElementById("save-btn");
  const changeKeyBtn = document.getElementById("change-key-btn");
  const input = document.getElementById("api-key");
  const voicePanel = document.getElementById("voice-panel");
  const voiceSelect = document.getElementById("voice-select");
  const voiceApply = document.getElementById("voice-apply");
  const voiceRetry = document.getElementById("voice-retry");
  const voiceStatus = document.getElementById("voice-status");
  const voiceState = {
    generation: 0,
    loading: false,
    applying: false,
    voices: [],
    knownCurrent: null,
  };

  show(loading, true);
  show(formPanel, false);
  show(configuredPanel, false);
  show(voicePanel, true);

  function setVoiceStatus(message, tone = "") {
    voiceStatus.textContent = message;
    voiceStatus.className = tone ? `status ${tone}` : "status";
  }

  function syncVoiceControls() {
    const disabled = voiceState.loading || voiceState.applying || voiceState.voices.length === 0;
    voiceSelect.disabled = disabled;
    voiceApply.disabled =
      disabled || !voiceSelect.value || voiceSelect.value === voiceState.knownCurrent;
  }

  function populateVoiceOptions(voices) {
    voiceSelect.replaceChildren();
    for (const voice of voices) {
      voiceSelect.appendChild(new Option(voice, voice));
    }
  }

  async function loadVoices() {
    const generation = ++voiceState.generation;
    voiceState.loading = true;
    voiceState.applying = false;
    voiceState.voices = [];
    voiceState.knownCurrent = null;
    populateVoiceOptions([]);
    show(voiceRetry, false);
    syncVoiceControls();
    setVoiceStatus("Loading voices…");
    try {
      const [voiceList, currentData] = await Promise.all([
        requestJson("/voices"),
        requestJson("/voices/current"),
      ]);
      const voices = normalizeVoices(voiceList);
      const current = normalizeCurrentVoice(currentData);
      if (generation !== voiceState.generation) return;

      voiceState.voices = voices;
      voiceState.knownCurrent = current;
      populateVoiceOptions(voices);
      if (voices.length === 0) {
        setVoiceStatus("No voices available for this backend.", "warn");
      } else if (voices.includes(current)) {
        voiceSelect.value = current;
        setVoiceStatus(`Current voice · ${current}`, "");
      } else {
        voiceSelect.value = voices[0];
        setVoiceStatus(
          `Current voice “${current}” isn't available for this backend. Choose and apply a replacement.`,
          "warn",
        );
      }
    } catch (error) {
      if (generation !== voiceState.generation) return;
      voiceState.voices = [];
      voiceState.knownCurrent = null;
      populateVoiceOptions([]);
      setVoiceStatus(`Voices unavailable · ${conciseError(error)}`, "error");
      show(voiceRetry, true);
    } finally {
      if (generation === voiceState.generation) {
        voiceState.loading = false;
        syncVoiceControls();
      }
    }
  }

  async function applyVoice() {
    if (
      voiceState.loading ||
      voiceState.applying ||
      !voiceSelect.value ||
      voiceSelect.value === voiceState.knownCurrent
    ) {
      return;
    }
    const requestedVoice = voiceSelect.value;
    const generation = ++voiceState.generation;
    voiceState.applying = true;
    show(voiceRetry, false);
    syncVoiceControls();
    setVoiceStatus(`Applying ${requestedVoice}…`);
    try {
      const response = await requestJson("/voices/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice: requestedVoice }),
      });
      if (!response || response.ok !== true) {
        throw new Error((response && (response.error || response.status)) || "apply_failed");
      }
      const current = normalizeCurrentVoice(await requestJson("/voices/current"));
      if (generation !== voiceState.generation) return;
      if (!voiceState.voices.includes(current)) {
        throw new Error(`backend returned unsupported voice “${current}”`);
      }
      voiceState.knownCurrent = current;
      voiceSelect.value = current;
      setVoiceStatus(`Voice applied · ${current}`, "ok");
    } catch (error) {
      if (generation !== voiceState.generation) return;
      voiceSelect.value = requestedVoice;
      setVoiceStatus(`Voice apply failed · ${conciseError(error)}`, "error");
    } finally {
      if (generation === voiceState.generation) {
        voiceState.applying = false;
        syncVoiceControls();
      }
    }
  }

  voiceSelect.addEventListener("change", () => {
    if (voiceState.loading || voiceState.applying || !voiceSelect.value) return;
    if (voiceSelect.value === voiceState.knownCurrent) {
      setVoiceStatus(`Current voice · ${voiceSelect.value}`);
    } else {
      setVoiceStatus(`Ready to apply · ${voiceSelect.value}`);
    }
    syncVoiceControls();
  });
  voiceApply.addEventListener("click", applyVoice);
  voiceRetry.addEventListener("click", () => {
    void loadVoices();
  });

  void loadVoices();
  const st = (await waitForStatus()) || { has_key: false };

  if (st.has_key) {
    show(configuredPanel, true);
  } else {
    show(formPanel, true);
  }
  show(loading, false);

  changeKeyBtn.addEventListener("click", () => {
    show(configuredPanel, false);
    show(formPanel, true);
    input.value = "";
    statusEl.textContent = "";
    statusEl.className = "status";
  });

  input.addEventListener("input", () => {
    input.classList.remove("error");
  });

  saveBtn.addEventListener("click", async () => {
    const key = input.value.trim();
    if (!key) {
      statusEl.textContent = "Please enter a valid key.";
      statusEl.className = "status warn";
      input.classList.add("error");
      return;
    }
    statusEl.textContent = "Validating API key...";
    statusEl.className = "status";
    input.classList.remove("error");
    try {
      const validation = await validateKey(key);
      if (!validation.valid) {
        statusEl.textContent = "Invalid API key. Please check your key and try again.";
        statusEl.className = "status error";
        input.classList.add("error");
        return;
      }
      statusEl.textContent = "Key valid! Saving...";
      statusEl.className = "status ok";
      await saveKey(key);
      statusEl.textContent = "Saved. Reloading…";
      statusEl.className = "status ok";
      window.location.reload();
    } catch (e) {
      input.classList.add("error");
      if (e.message === "invalid_api_key") {
        statusEl.textContent = "Invalid API key. Please check your key and try again.";
      } else {
        statusEl.textContent = "Failed to validate/save key. Please try again.";
      }
      statusEl.className = "status error";
    }
  });
}

window.addEventListener("DOMContentLoaded", init);