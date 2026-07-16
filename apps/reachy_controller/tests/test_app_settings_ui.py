"""Browserless behavior checks for the generic app-settings instrument."""
# ruff: noqa: E501

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).parents[1] / "src" / "reachy_controller" / "static"

_SECTION_START = "  // ── App settings (generic, schema-driven)"
_SECTION_END = "  // ── end app settings"

_HARNESS = r'''
"use strict";
const state = {
  settings: {
    app: null,
    schema: null,
    loadKey: null,
    phase: "idle",
    inflight: false,
    retryTimer: null,
    retryCount: 0,
    saving: false,
  },
};
class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.innerHTML = "";
  }
}
class FakeGear {
  constructor(app) {
    this.dataset = { app, action: "settings" };
    this.attrs = { "aria-expanded": "false" };
    this.classes = new Set();
    this.classList = {
      toggle: (name, force) => {
        if (force) this.classes.add(name);
        else this.classes.delete(name);
      },
    };
  }
  setAttribute(name, value) { this.attrs[name] = value; }
}
class FakeApps extends FakeElement {
  constructor() {
    super("apps");
    this.gears = [];
    this.rebuilds = 0;
  }
  set innerHTML(_) { this.rebuilds += 1; }
  get innerHTML() { return ""; }
  querySelectorAll() { return this.gears; }
}
const appsEl = new FakeApps();
const gearChat = new FakeGear("reachy_chat");
appsEl.gears = [gearChat];
const els = Object.fromEntries([
  "app-settings",
  "app-settings-app",
  "app-settings-dot",
  "app-settings-state",
  "app-settings-hint",
  "app-settings-fields",
].map((id) => [id, new FakeElement(id)]));
els["apps"] = appsEl;
els["app-settings"].hidden = true;
const $ = (id) => els[id];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
}[char]));
const appState = new Map();

let getAttempts = 0;
let failGets = 0;
let failPost = null;
let postGate = null;
const postCalls = [];
const schemaFixture = {
  app: "reachy_chat",
  title: "Reachy Chat",
  fields: [
    { key: "api_key", label: "OpenAI API key", type: "password", configured: false,
      help: "Stored in the app's .env; never shown again." },
    { key: "voice", label: "Voice", type: "select", value: "alloy",
      options: [{ value: "alloy", label: "Alloy" }, { value: "verse", label: "Verse" }] },
  ],
};
async function apiJson(path, options = {}) {
  if (options && options.method === "POST") {
    postCalls.push({ path, body: options.body });
    if (postGate) await postGate.promise;
    if (failPost) throw new Error(failPost);
    return { status: "ok", configured: true };
  }
  getAttempts += 1;
  if (failGets > 0) {
    failGets -= 1;
    throw new Error("settings service is not reachable");
  }
  return JSON.parse(JSON.stringify(schemaFixture));
}
__SECTION__
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const statuses = () => Array.from(appState.values());
const results = {};

// 1. Gear markup is neutral (state applied by patch, never by rebuild) and
// gated purely on has_settings, name-agnostic.
results.gear = {
  present: appSettingsGearMarkup({ name: "reachy_chat", has_settings: true }).includes('data-action="settings"'),
  otherAppToo: appSettingsGearMarkup({ name: "some_future_app", has_settings: true }).includes('data-action="settings"'),
  absentWithout: appSettingsGearMarkup({ name: "reachy_dance" }) === "",
  neutral: !appSettingsGearMarkup({ name: "reachy_chat", has_settings: true }).includes('aria-expanded="true"'),
};

// 2. Open while the app is stopped: honest state, zero fetches, and the gear
// is patched in place — #apps is never rebuilt, so keyboard focus survives.
appState.set("reachy_chat", { name: "reachy_chat", status: "stopped", has_settings: true });
const focusedBefore = gearChat; // stands in for document.activeElement
openAppSettings("reachy_chat");
results.stopped = {
  visible: els["app-settings"].hidden === false,
  state: els["app-settings-state"].textContent,
  hint: els["app-settings-hint"].textContent,
  noFetch: getAttempts === 0,
  fieldsEmpty: els["app-settings-fields"].innerHTML === "",
};
results.focus = {
  noRebuild: appsEl.rebuilds === 0,
  sameElement: appsEl.gears[0] === focusedBefore,
  patchedOpen: gearChat.classes.has("open"),
  ariaExpanded: gearChat.attrs["aria-expanded"] === "true",
};

// 3. App comes up; settings port lags → bounded retry to ready. Repeated
// 1 Hz syncs while a retry is pending must not fan out extra loads.
failGets = 2;
appState.set("reachy_chat", { name: "reachy_chat", status: "running", pid: 4242, has_settings: true });
syncAppSettings(statuses());
await wait(5);
const attemptsAfterFirst = getAttempts;
syncAppSettings(statuses());
syncAppSettings(statuses());
await wait(5);
results.retrying = {
  connecting: els["app-settings-state"].textContent === "connecting",
  firstAttemptOnly: attemptsAfterFirst === 1,
  noFanOut: getAttempts === 1,
};
await wait(120);
results.ready = {
  state: els["app-settings-state"].textContent,
  attempts: getAttempts,
  title: els["app-settings-app"].textContent,
  passwordRendered: els["app-settings-fields"].innerHTML.includes('type="password"'),
  passwordUnconfigured: els["app-settings-fields"].innerHTML.includes('placeholder="not configured"'),
  selectRendered: els["app-settings-fields"].innerHTML.includes('value="alloy" selected'),
  helpRendered: els["app-settings-fields"].innerHTML.includes("never shown again"),
};

// 4. Same incarnation on later polls: no reload.
syncAppSettings(statuses());
await wait(30);
results.keyedSingleFlight = getAttempts === 3;

// 5. New pid (restart) reloads once.
appState.set("reachy_chat", { name: "reachy_chat", status: "running", pid: 4243, has_settings: true });
syncAppSettings(statuses());
await wait(30);
results.pidReload = getAttempts === 4;

// 6. loading→running with the SAME pid re-arms the bounded load: "running" is
// fresh evidence the port should answer, even after loading-phase failures.
failGets = 999;
appState.set("reachy_chat", { name: "reachy_chat", status: "loading", pid: 4243, has_settings: true });
syncAppSettings(statuses());
await wait(50);
const attemptsWhileLoading = getAttempts;
failGets = 0;
appState.set("reachy_chat", { name: "reachy_chat", status: "running", pid: 4243, has_settings: true });
syncAppSettings(statuses());
await wait(30);
results.rearm = {
  retriedWhileLoading: attemptsWhileLoading > 4,
  freshAttemptOnFlip: getAttempts === attemptsWhileLoading + 1,
  readyAfterFlip: els["app-settings-state"].textContent === "ready",
};

// 7. Password save: blank rejected client-side, valid POSTs, secret never echoed.
const blankOk = await submitAppSetting("api_key", "   ");
results.blankRejected = {
  refused: blankOk === false,
  noPost: postCalls.length === 0,
  errorHint: els["app-settings-hint"].className.includes("error"),
};
const saveOk = await submitAppSetting("api_key", "sk-secret-123");
results.keySaved = {
  ok: saveOk === true,
  path: postCalls[0].path,
  body: postCalls[0].body,
  noEcho: !els["app-settings-fields"].innerHTML.includes("sk-secret-123"),
  configured: els["app-settings-fields"].innerHTML.includes("configured — enter new value to replace"),
  okHint: els["app-settings-hint"].className.includes("ok"),
};

// 8. Select apply updates the known current value.
const voiceOk = await submitAppSetting("voice", "verse");
results.voiceApplied = {
  ok: voiceOk === true,
  path: postCalls[1].path,
  body: postCalls[1].body,
  modelValue: state.settings.schema.fields[1].value === "verse",
  selected: els["app-settings-fields"].innerHTML.includes('value="verse" selected'),
};

// 9. Upstream save failure surfaces the detail; typed value is not wiped.
failPost = "upstream rejected the key";
const failOk = await submitAppSetting("api_key", "sk-bad");
results.saveFailure = {
  refused: failOk === false,
  hint: els["app-settings-hint"].textContent,
  errorHint: els["app-settings-hint"].className.includes("error"),
};
failPost = null;

// 10. A current select value missing from options renders a selected disabled
// placeholder — Apply cannot silently submit options[0], and the blank
// placeholder value trips the client-side guard.
state.settings.schema.fields[1].value = "legacy";
renderSettingsFields();
const mismatchHtml = els["app-settings-fields"].innerHTML;
const postsBeforeMismatch = postCalls.length;
const mismatchOk = await submitAppSetting("voice", "");
results.selectMismatch = {
  placeholder: mismatchHtml.includes('<option value="" selected disabled>'),
  warning: mismatchHtml.includes("not in this list"),
  noRealOptionSelected: !mismatchHtml.includes('value="alloy" selected') && !mismatchHtml.includes('value="verse" selected'),
  applyRefused: mismatchOk === false && postCalls.length === postsBeforeMismatch,
  errorHint: els["app-settings-hint"].className.includes("error"),
};
state.settings.schema.fields[1].value = "verse";
renderSettingsFields();

// 11. A save resolving after the panel changed must not overwrite the new
// state: close mid-flight, then let the POST finish.
let releasePost;
postGate = { promise: new Promise((resolve) => { releasePost = resolve; }) };
const voiceField = state.settings.schema.fields[1];
const stalePromise = submitAppSetting("voice", "alloy");
closeAppSettings();
const hintAfterClose = els["app-settings-hint"].textContent;
releasePost();
postGate = null;
const staleOk = await stalePromise;
results.staleSave = {
  refused: staleOk === false,
  modelUntouched: voiceField.value === "verse",
  hintUntouched: els["app-settings-hint"].textContent === hintAfterClose,
  savingReset: state.settings.saving === false,
};

// 12. Close hides the panel, drops the schema, and un-patches the gear.
results.closed = {
  hidden: els["app-settings"].hidden === true,
  schemaDropped: state.settings.schema === null,
  fieldsCleared: els["app-settings-fields"].innerHTML === "",
  gearUnpatched: !gearChat.classes.has("open") && gearChat.attrs["aria-expanded"] === "false",
  stillNoRebuild: appsEl.rebuilds === 0,
};

// 13. Stop cancels pending retries and falls back to the honest state.
openAppSettings("reachy_chat");
failGets = 999;
appState.set("reachy_chat", { name: "reachy_chat", status: "running", pid: 4244, has_settings: true });
syncAppSettings(statuses());
await wait(5);
appState.set("reachy_chat", { name: "reachy_chat", status: "stopped", has_settings: true });
syncAppSettings(statuses());
const attemptsAtStop = getAttempts;
await wait(120);
results.stopCancels = {
  noMoreAttempts: getAttempts === attemptsAtStop,
  state: els["app-settings-state"].textContent,
  hint: els["app-settings-hint"].textContent,
  fieldsCleared: els["app-settings-fields"].innerHTML === "",
};

// 14. Retries are bounded: an incarnation that never answers goes unavailable.
const attemptsBeforeExhaust = getAttempts;
appState.set("reachy_chat", { name: "reachy_chat", status: "running", pid: 4245, has_settings: true });
syncAppSettings(statuses());
await wait(400);
const attemptsAfterExhaust = getAttempts - attemptsBeforeExhaust;
await wait(120);
results.bounded = {
  attempts: attemptsAfterExhaust,
  stopsForGood: getAttempts - attemptsBeforeExhaust === attemptsAfterExhaust,
  state: els["app-settings-state"].textContent,
  errorHint: els["app-settings-hint"].className.includes("error"),
};
closeAppSettings();

console.log(JSON.stringify(results));
'''


def _extract_settings_section() -> str:
    script = (_STATIC_DIR / "main.js").read_text(encoding="utf-8")
    start = script.index(_SECTION_START)
    end = script.index(_SECTION_END, start)
    section = script[start:end]
    return section.replace(
        "const SETTINGS_RETRY_DELAY_MS = 1500;",
        "const SETTINGS_RETRY_DELAY_MS = 20;",
    )


def _run_harness() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable not available")
    script = _HARNESS.replace("__SECTION__", _extract_settings_section())
    proc = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_app_settings_lifecycle() -> None:
    results = _run_harness()
    assert results["gear"] == {
        "present": True,
        "otherAppToo": True,
        "absentWithout": True,
        "neutral": True,
    }
    assert results["stopped"] == {
        "visible": True,
        "state": "app stopped",
        "hint": "Start reachy_chat to configure.",
        "noFetch": True,
        "fieldsEmpty": True,
    }
    assert results["retrying"] == {
        "connecting": True,
        "firstAttemptOnly": True,
        "noFanOut": True,
    }
    assert results["ready"] == {
        "state": "ready",
        "attempts": 3,
        "title": "Reachy Chat",
        "passwordRendered": True,
        "passwordUnconfigured": True,
        "selectRendered": True,
        "helpRendered": True,
    }
    assert results["keyedSingleFlight"] is True
    assert results["pidReload"] is True
    assert results["rearm"] == {
        "retriedWhileLoading": True,
        "freshAttemptOnFlip": True,
        "readyAfterFlip": True,
    }


def test_app_settings_focus_preserved() -> None:
    results = _run_harness()
    assert results["focus"] == {
        "noRebuild": True,
        "sameElement": True,
        "patchedOpen": True,
        "ariaExpanded": True,
    }
    assert results["closed"]["gearUnpatched"] is True
    assert results["closed"]["stillNoRebuild"] is True


def test_app_settings_save_flows() -> None:
    results = _run_harness()
    assert results["blankRejected"] == {
        "refused": True,
        "noPost": True,
        "errorHint": True,
    }
    assert results["keySaved"] == {
        "ok": True,
        "path": "/api/apps/reachy_chat/settings/api_key",
        "body": json.dumps({"value": "sk-secret-123"}, separators=(",", ":")),
        "noEcho": True,
        "configured": True,
        "okHint": True,
    }
    assert results["voiceApplied"] == {
        "ok": True,
        "path": "/api/apps/reachy_chat/settings/voice",
        "body": json.dumps({"value": "verse"}, separators=(",", ":")),
        "modelValue": True,
        "selected": True,
    }
    assert results["saveFailure"] == {
        "refused": True,
        "hint": "upstream rejected the key",
        "errorHint": True,
    }
    assert results["selectMismatch"] == {
        "placeholder": True,
        "warning": True,
        "noRealOptionSelected": True,
        "applyRefused": True,
        "errorHint": True,
    }
    assert results["staleSave"] == {
        "refused": True,
        "modelUntouched": True,
        "hintUntouched": True,
        "savingReset": True,
    }


def test_app_settings_cancellation_and_bounds() -> None:
    results = _run_harness()
    assert results["closed"] == {
        "hidden": True,
        "schemaDropped": True,
        "fieldsCleared": True,
        "gearUnpatched": True,
        "stillNoRebuild": True,
    }
    assert results["stopCancels"] == {
        "noMoreAttempts": True,
        "state": "app stopped",
        "hint": "Start reachy_chat to configure.",
        "fieldsCleared": True,
    }
    assert results["bounded"]["attempts"] == 8
    assert results["bounded"]["stopsForGood"] is True
    assert results["bounded"]["state"] == "unavailable"
    assert results["bounded"]["errorHint"] is True
