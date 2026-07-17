"""Browserless behavior checks for the favorite app launcher and library."""
# ruff: noqa: E501

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).parents[1] / "src" / "reachy_controller" / "static"

_HARNESS = r'''
"use strict";
const APP_NAMES = ["reachy_chat", "reachy_dance", "reachy_lookat", "reachy_simon", "reachy_hermes"];
const DEFAULT_FAVORITES = ["reachy_chat", "reachy_dance", "reachy_lookat"];
const LOADING_FRAGMENTS = ["Booting…"];
let loadingTick = 0;
let lastAppsSig = null;
let focused = null;
const state = {
  appBusy: false,
  appActive: false,
  daemonOnline: true,
  exprBusy: false,
  mode: "schematic",
  camera: { retryTimer: null, availabilityInflight: false },
  library: {
    open: false,
    favorites: [...DEFAULT_FAVORITES],
    loaded: false,
    loading: false,
    saving: false,
    lastSig: null,
  },
};
const appState = new Map(APP_NAMES.map((name) => [name, { name, status: "stopped" }]));
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const CSS = { escape: (value) => String(value) };
const cameraShouldConnect = () => false;
const refreshCameraAvailability = () => {};
const syncAppSettings = () => {};
const patchSettingsGears = () => {};
class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.attrs = {};
    this.dataset = {};
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] || null; }
  focus() { focused = this.id; }
  querySelectorAll() { return []; }
}
class FakeList extends FakeElement {
  constructor(id) { super(id); this.buttons = []; this.spans = []; this.rebuilds = 0; }
  set innerHTML(html) {
    this.rebuilds += 1;
    this.html = String(html);
    this.buttons = [...String(html).matchAll(/data-app="([^"]+)" data-action="([^"]+)"/g)].map((m) => {
      const button = new FakeElement(`${m[2]}:${m[1]}`);
      button.dataset = { app: m[1], action: m[2] };
      button.disabled = false;
      return button;
    });
    this.spans = [...String(html).matchAll(/class="app-uptime" data-app="([^"]+)"/g)].map((m) => {
      const span = new FakeElement("uptime");
      span.dataset = { app: m[1] };
      return span;
    });
  }
  get innerHTML() { return this.html || ""; }
  querySelectorAll(selector) {
    if (String(selector).includes("app-uptime")) return this.spans;
    return this.buttons;
  }
  querySelector(selector) {
    const app = String(selector).match(/data-app="([^"]+)"/)?.[1];
    const action = String(selector).match(/data-action="([^"]+)"/)?.[1];
    return this.buttons.find((button) => button.dataset.app === app && button.dataset.action === action) || null;
  }
}
const els = Object.fromEntries([
  "apps-view", "app-settings", "app-library", "app-library-open", "app-library-back",
  "app-library-count", "app-library-dot", "app-library-state", "app-library-hint",
  "robot-dot", "robot-value", "expr-hint", "camera-stream",
].map((id) => [id, new FakeElement(id)]));
els.apps = new FakeList("apps");
els["app-library-list"] = new FakeList("app-library-list");
const $ = (id) => els[id];
const appSettingsGearMarkup = (app) => app.has_settings === true
  ? `<button class="btn gear" data-app="${escapeHtml(app.name)}" data-action="settings"></button>` : "";
function updateAppUptimes(statuses) {
  [$('apps'), $('app-library-list')].filter(Boolean).forEach((root) => root.querySelectorAll('.app-uptime').forEach((span) => {
    const app = statuses.find((item) => item.name === span.dataset.app);
    span.textContent = app && app.status === 'running' && app.start_time ? ` · ${formatUptime(Date.now() / 1000 - Number(app.start_time))}` : '';
  }));
}
let favoriteResponse = { favorites: ["reachy_lookat", "reachy_chat"] };
let getCalls = 0;
let putCalls = [];
let failPut = false;
let failGet = false;
let releaseGet;
let getGate = new Promise((resolve) => { releaseGet = resolve; });
async function apiJson(path, options = {}) {
  if (path === "/api/apps/favorites" && options.method === "PUT") {
    putCalls.push(JSON.parse(options.body));
    if (failPut) throw new Error("preference write failed");
    return { favorites: JSON.parse(options.body).favorites };
  }
  if (path === "/api/apps/favorites") {
    getCalls += 1;
    await getGate;
    if (failGet) throw new Error("favorites GET failed");
    return JSON.parse(JSON.stringify(favoriteResponse));
  }
  throw new Error(`unexpected ${path}`);
}
__SECTION__
const statuses = [
  { name: "reachy_chat", status: "stopped", display: "Reachy Chat", blurb: "Talk with Reachy.", has_settings: true },
  { name: "reachy_dance", status: "stopped", display: "Reachy Dance", blurb: "Compose movement." },
  { name: "reachy_lookat", status: "stopped", display: "Reachy Look At", blurb: "Track a target." },
  { name: "reachy_simon", status: "stopped", display: "Reachy Simon", blurb: "Play a memory game." },
  { name: "reachy_hermes", status: "stopped", display: "Hermes", blurb: "Voice-driven agent for research and longer tasks." },
];
const results = {};
renderApps(statuses);
results.favoritesOnly = {
  chat: els.apps.innerHTML.includes("reachy_chat"),
  dance: els.apps.innerHTML.includes("reachy_dance"),
  lookat: els.apps.innerHTML.includes("reachy_lookat"),
  simonHidden: !els.apps.innerHTML.includes("reachy_simon"),
};
const activeSimon = statuses.map((item) => item.name === "reachy_simon" ? { ...item, status: "running" } : item);
renderApps(activeSimon);
results.activeException = {
  visible: els.apps.innerHTML.includes("reachy_simon"),
  marker: els.apps.innerHTML.includes("active · not favorite"),
  stopAction: els.apps.innerHTML.includes('data-action="stop"'),
};
const failedHermes = statuses.map((item) => item.name === "reachy_hermes"
  ? { ...item, status: "error", error: "missing executable", start_time: 5 }
  : item);
renderApps(failedHermes);
results.errorException = {
  visible: els.apps.innerHTML.includes("reachy_hermes"),
  marker: els.apps.innerHTML.includes("error · not favorite"),
  error: els.apps.innerHTML.includes("missing executable"),
};
const errorDuringActive = statuses.map((item) => {
  if (item.name === "reachy_simon") return { ...item, status: "running" };
  if (item.name === "reachy_hermes") return { ...item, status: "error", error: "boom", start_time: 1 };
  return item;
});
renderApps(errorDuringActive);
results.errorDuringActive = {
  simonVisible: els.apps.innerHTML.includes('data-app="reachy_simon"'),
  hermesVisible: els.apps.innerHTML.includes('data-app="reachy_hermes"'),
  hermesError: els.apps.innerHTML.includes("boom"),
};
const twoFailures = statuses.map((item) => {
  if (item.name === "reachy_simon") return { ...item, status: "error", error: "simon boom", start_time: 1 };
  if (item.name === "reachy_hermes") return { ...item, status: "error", error: "hermes boom", start_time: 2 };
  return item;
});
renderApps(twoFailures);
results.multipleFailuresVisible = {
  simonVisible: els.apps.innerHTML.includes('data-app="reachy_simon"'),
  hermesVisible: els.apps.innerHTML.includes('data-app="reachy_hermes"'),
  simonError: els.apps.innerHTML.includes("simon boom"),
  hermesError: els.apps.innerHTML.includes("hermes boom"),
};
renderLibrary(statuses);
results.libraryCards = {
  allApps: APP_NAMES.every((name) => els["app-library-list"].innerHTML.includes(name)),
  metadata: els["app-library-list"].innerHTML.includes("Compose movement."),
  stars: (els["app-library-list"].innerHTML.match(/data-action="favorite"/g) || []).length === 5,
  settings: els["app-library-list"].innerHTML.includes('data-action="settings"'),
};
const beforeLoad = getCalls;
const firstLoad = loadFavorites();
const secondLoad = loadFavorites();
results.singleBootLoad = { oneInFlight: getCalls - beforeLoad === 1 };
releaseGet();
await Promise.all([firstLoad, secondLoad]);
results.loadedPayload = {
  favorites: JSON.stringify(state.library.favorites),
  panelChat: els.apps.innerHTML.includes("reachy_chat"),
  panelDance: !els.apps.innerHTML.includes("reachy_dance"),
  panelLookat: els.apps.innerHTML.includes("reachy_lookat"),
  gets: getCalls,
};
const beforeRefusal = putCalls.length;
state.library.favorites = ["reachy_chat", "reachy_dance", "reachy_lookat"];
await toggleFavorite("reachy_simon");
results.limit = {
  noPut: putCalls.length === beforeRefusal,
  hint: els["app-library-hint"].textContent,
};
await toggleFavorite("reachy_dance");
results.putContract = {
  body: putCalls.at(-1),
  removed: !state.library.favorites.includes("reachy_dance"),
};
failPut = true;
await toggleFavorite("reachy_chat");
results.rollback = {
  restored: state.library.favorites.includes("reachy_chat"),
  hint: els["app-library-hint"].textContent,
};
openAppLibrary();
const opened = { appsHidden: els["apps-view"].hidden, libraryShown: !els["app-library"].hidden, focus: focused };
failPut = false;
state.library.favorites = ["reachy_chat", "reachy_lookat"];
await toggleFavorite("reachy_hermes");
results.favoriteFocus = focused;
state.library.saving = true;
closeAppLibrary();
results.swap = {
  ...opened,
  appsShown: !els["apps-view"].hidden,
  libraryHidden: els["app-library"].hidden,
  focusRestored: focused === "app-library-open",
};
results.savingPreserved = state.library.saving;
state.library.saving = false;

// --- GET failure must not mark favorites loaded, and must stay retryable ---
state.library.loaded = false;
state.library.loading = false;
state.library.favorites = [...DEFAULT_FAVORITES];
failGet = true;
await loadFavorites();
results.failedLoad = {
  loaded: state.library.loaded,
  loading: state.library.loading,
  hint: els["app-library-hint"].textContent,
};
const putsBeforeGuard = putCalls.length;
await toggleFavorite("reachy_hermes");
results.toggleBeforeLoad = {
  noPut: putCalls.length === putsBeforeGuard,
  stillNotLoaded: !state.library.loaded,
};
failGet = false;
await toggleFavorite("reachy_hermes");
results.toggleRetriesLoad = {
  loaded: state.library.loaded,
  putHappened: putCalls.length > putsBeforeGuard,
};
console.log(JSON.stringify(results));
'''




def test_favorite_panel_and_active_exception() -> None:
    results = _run_harness()
    assert results["favoritesOnly"] == {
        "chat": True, "dance": True, "lookat": True, "simonHidden": True,
    }
    assert results["activeException"] == {
        "visible": True, "marker": True, "stopAction": True,
    }
    assert results["errorException"] == {
        "visible": True, "marker": True, "error": True,
    }
    assert results["errorDuringActive"] == {
        "simonVisible": True, "hermesVisible": True, "hermesError": True,
    }
    assert results["multipleFailuresVisible"] == {
        "simonVisible": True, "hermesVisible": True, "simonError": True, "hermesError": True,
    }


def test_library_metadata_stars_and_boot_fetch() -> None:
    results = _run_harness()
    assert results["libraryCards"] == {
        "allApps": True, "metadata": True, "stars": True, "settings": True,
    }
    assert results["singleBootLoad"] == {"oneInFlight": True}
    assert results["loadedPayload"] == {
        "favorites": '["reachy_lookat","reachy_chat"]',
        "panelChat": True, "panelDance": True, "panelLookat": True, "gets": 1,
    }


def test_favorite_limit_put_rollback_and_view_swap() -> None:
    results = _run_harness()
    assert results["limit"] == {
        "noPut": True, "hint": "Choose up to 3 favorite apps.",
    }
    assert results["putContract"] == {
        "body": {"favorites": ["reachy_chat", "reachy_lookat"]},
        "removed": True,
    }
    assert results["rollback"] == {
        "restored": True, "hint": "preference write failed",
    }
    assert results["favoriteFocus"] == "favorite:reachy_hermes"
    assert results["swap"] == {
        "appsHidden": True, "libraryShown": True, "focus": "app-library-back",
        "appsShown": True, "libraryHidden": True, "focusRestored": True,
    }
    assert results["savingPreserved"] is True


def test_favorites_fetch_failure_guards_stale_put() -> None:
    results = _run_harness()
    assert results["failedLoad"] == {
        "loaded": False, "loading": False, "hint": "favorites GET failed",
    }
    assert results["toggleBeforeLoad"] == {"noPut": True, "stillNotLoaded": True}
    assert results["toggleRetriesLoad"] == {"loaded": True, "putHappened": True}


def test_library_markup_and_endpoint_contract() -> None:
    index = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    styles = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
    script = (_STATIC_DIR / "main.js").read_text(encoding="utf-8")
    assert index.count('id="app-library"') == 1
    assert 'id="app-library-open"' in index
    assert 'id="app-library-back"' in index
    assert '/api/apps/favorites' in script
    assert 'method: "PUT"' in script
    assert '.btn.star' in styles
    assert 'min-width: 40px' in styles
    assert 'min-height: 40px' in styles


def _run_harness() -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node executable not available")
    script = _HARNESS.replace(
        "__SECTION__",
        "  function renderApps(statuses" + (
            (_STATIC_DIR / "main.js").read_text(encoding="utf-8").split(
                "  function renderApps(statuses", 1
            )[1].split("  async function pollAppsStatus", 1)[0]
        ),
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)