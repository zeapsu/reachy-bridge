"""Browserless behavior checks for the live camera viewport lifecycle."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).parents[1] / "src" / "reachy_controller" / "static"

_SECTION_START = "  const CAMERA_FIRST_FRAME_TIMEOUT_MS"
_SECTION_END = "  function setMode"

_HARNESS = r"""
"use strict";
const state = {
  mode: "camera",
  daemonOnline: true,
  cameraAvailabilityRequest: 0,
  camera: {
    streamGeneration: 0,
    firstFrameTimer: null,
    retryTimer: null,
    availabilityInflight: false,
    loadHandler: null,
    errorHandler: null,
  },
};
class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.naturalWidth = 0;
    this.naturalHeight = 0;
    this._src = "";
    this.srcAssignments = 0;
    this.listeners = new Map();
    this.classList = {
      values: new Set(),
      toggle: (name, force) => {
        if (force) this.classList.values.add(name);
        else this.classList.values.delete(name);
      },
      remove: (name) => this.classList.values.delete(name),
    };
  }
  getAttribute(name) {
    return name === "src" ? this._src : null;
  }
  removeAttribute(name) {
    if (name === "src") this._src = "";
  }
  addEventListener(name, handler) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(handler);
  }
  removeEventListener(name, handler) {
    this.listeners.get(name)?.delete(handler);
  }
  dispatch(name) {
    for (const handler of [...(this.listeners.get(name) || [])]) handler();
  }
  set src(value) {
    this._src = value;
    this.naturalWidth = 0;
    this.naturalHeight = 0;
    this.srcAssignments += 1;
  }
  get src() {
    return this._src;
  }
}
const els = {
  "camera-stream": new FakeElement("camera-stream"),
  "camera-placeholder": new FakeElement("camera-placeholder"),
  "vp-meta": new FakeElement("vp-meta"),
};
const $ = (id) => els[id];
const document = { hidden: false };
let availabilityChecks = 0;
async function refreshCameraAvailability() {
  availabilityChecks += 1;
  applyCameraAvailability({ available: true });
}
__SECTION__
const image = els["camera-stream"];
const placeholder = els["camera-placeholder"];
const meta = els["vp-meta"];
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const results = {};

applyCameraAvailability({ available: true });
image.naturalWidth = 1920;
image.naturalHeight = 1080;
await wait(CAMERA_FIRST_FRAME_CHECK_MS + 20);
results.firstFrameWithoutLoad = {
  live: meta.textContent === "live camera · live",
  imageVisible: image.hidden === false,
  placeholderHidden: placeholder.hidden === true,
};

image.dispatch("error");
results.errorState = {
  sourceCleared: image.getAttribute("src") === "",
  placeholder: placeholder.textContent,
  meta: meta.textContent,
};
const checksBeforeRetry = availabilityChecks;
await wait(CAMERA_RETRY_DELAY_MS + 40);
results.errorRetry = {
  checksIncreased: availabilityChecks === checksBeforeRetry + 1,
  secondStreamRequest: image.srcAssignments === 2,
};

const checksBeforeTimeout = availabilityChecks;
await wait(CAMERA_FIRST_FRAME_TIMEOUT_MS + CAMERA_RETRY_DELAY_MS + 60);
results.timeoutRetry = {
  checksIncreased: availabilityChecks === checksBeforeTimeout + 1,
  thirdStreamRequest: image.srcAssignments === 3,
};
image.dispatch("error");
const checksBeforeDisconnect = availabilityChecks;
document.hidden = true;
disconnectCameraStream("Camera hidden");
await wait(CAMERA_RETRY_DELAY_MS + 40);
results.disconnectCancelsRetry = {
  noAdditionalCheck: availabilityChecks === checksBeforeDisconnect,
  noSource: image.getAttribute("src") === "",
};

console.log(JSON.stringify(results));
"""


_RENDER_APPS_HARNESS = r"""
"use strict";
const state = {
  mode: "camera",
  daemonOnline: true,
  appActive: false,
  exprBusy: false,
  appBusy: false,
  camera: {
    retryTimer: null,
    availabilityInflight: false,
  },
};
let APP_NAMES = ["future_vision"];
const LOADING_FRAGMENTS = ["Booting…"];
let loadingTick = 0;
let lastAppsSig = null;
const document = { hidden: false };
class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this._src = "";
  }
  getAttribute(name) {
    return name === "src" ? this._src : null;
  }
  removeAttribute(name) {
    if (name === "src") this._src = "";
  }
  set src(value) {
    this._src = value;
  }
  get src() {
    return this._src;
  }
  set innerHTML(_) {}
}
const els = Object.fromEntries([
  "camera-stream",
  "expr-hint",
  "apps",
  "robot-dot",
  "robot-value",
].map((id) => [id, new FakeElement(id)]));
const $ = (id) => els[id];
const cameraShouldConnect = () => (
  state.mode === "camera" && state.daemonOnline === true && !document.hidden
);
let disconnects = 0;
let availabilityChecks = 0;
function disconnectCameraStream() {
  disconnects += 1;
  $("camera-stream").removeAttribute("src");
}
async function refreshCameraAvailability() {
  availabilityChecks += 1;
}
const escapeHtml = (value) => String(value ?? "");
const updateAppUptimes = () => {};
const appState = new Map();
__RENDER_APPS__
const image = els["camera-stream"];
const results = {};

image.src = "/api/camera/stream?ts=1";
renderApps([{ name: "future_vision", status: "running", uses_camera: true }]);
results.runningVisionAppKeepsActiveStream = {
  disconnects: disconnects === 0,
  sourceKept: image.getAttribute("src") === "/api/camera/stream?ts=1",
  noAvailabilityCheck: availabilityChecks === 0,
};

image.removeAttribute("src");
disconnects = 0;
availabilityChecks = 0;
renderApps([{ name: "future_vision", status: "running", uses_camera: true }]);
await Promise.resolve();
results.runningVisionAppStartsAbsentStream = {
  disconnects: disconnects === 0,
  availabilityCheckStarted: availabilityChecks === 1,
};

console.log(JSON.stringify(results));
"""


def _extract_render_apps() -> str:
    script = (_STATIC_DIR / "main.js").read_text(encoding="utf-8")
    start = script.index("  function renderApps(statuses")
    end = script.index("  function formatUptime", start)
    return script[start:end]


def _run_render_apps_harness() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable not available")
    script = _RENDER_APPS_HARNESS.replace("__RENDER_APPS__", _extract_render_apps())
    proc = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _extract_camera_section() -> str:
    script = (_STATIC_DIR / "main.js").read_text(encoding="utf-8")
    start = script.index(_SECTION_START)
    end = script.index(_SECTION_END, start)
    section = script[start:end]
    return section.replace(
        "const CAMERA_FIRST_FRAME_TIMEOUT_MS = 9000;",
        "const CAMERA_FIRST_FRAME_TIMEOUT_MS = 40;",
    )


def _run_harness() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable not available")
    script = _HARNESS.replace("__SECTION__", _extract_camera_section())
    proc = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_camera_first_frame_and_retry_lifecycle() -> None:
    results = _run_harness()
    assert results["firstFrameWithoutLoad"] == {
        "live": True,
        "imageVisible": True,
        "placeholderHidden": True,
    }
    assert results["errorState"] == {
        "sourceCleared": True,
        "placeholder": "Live camera unavailable",
        "meta": "live camera · error",
    }
    assert results["errorRetry"] == {
        "checksIncreased": True,
        "secondStreamRequest": True,
    }
    assert results["timeoutRetry"] == {
        "checksIncreased": True,
        "thirdStreamRequest": True,
    }
    assert results["disconnectCancelsRetry"] == {
        "noAdditionalCheck": True,
        "noSource": True,
    }


def test_render_apps_does_not_arbitrate_dashboard_camera_stream() -> None:
    results = _run_render_apps_harness()
    assert results["runningVisionAppKeepsActiveStream"] == {
        "disconnects": True,
        "sourceKept": True,
        "noAvailabilityCheck": True,
    }
    assert results["runningVisionAppStartsAbsentStream"] == {
        "disconnects": True,
        "availabilityCheckStarted": True,
    }

    script = (_STATIC_DIR / "main.js").read_text(encoding="utf-8")
    assert "cameraOwner" not in script
    assert 'reason === "in_use"' not in script
    assert '["reachy_chat", "reachy_lookat"]' not in script
