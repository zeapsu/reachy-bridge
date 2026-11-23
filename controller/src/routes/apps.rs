//! App routes over the daemon's Hugging Face app manager: list, start/stop
//! with the controller's guards, the app UI probe, and up to three favorites
//! for the dashboard panel.

use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Path as UrlPath, State};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde_json::{Value, json};

use crate::apps::APP_CONTROL_TIMEOUT;
use crate::auth::AuthContext;
use crate::error::{ApiError, ApiResult};
use crate::routes::power::{ensure_awake, settle_idle_reset};
use crate::state::Shared;

pub const MAX_FAVORITES: usize = 3;

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/apps/status", get(status))
        .route("/api/apps/favorites", get(get_favorites).put(put_favorites))
        .route("/api/apps/start/{name}", post(start))
        .route("/api/apps/stop/{name}", post(stop))
        .route("/api/apps/{name}/ui", get(ui))
}

async fn status(State(state): State<Shared>) -> ApiResult<Json<Vec<Value>>> {
    state.apps.statuses().await.map(Json).map_err(|_| ApiError::unavailable())
}

async fn installed_names(state: &Shared) -> ApiResult<Vec<String>> {
    state.apps.installed_names().await.map_err(|_| ApiError::unavailable())
}

/// The sim's apps aren't the robot's, so neither are its pins (#218).
fn favorites_path(dir: &Path, sim: bool) -> PathBuf {
    dir.join(if sim { "favorites-sim.json" } else { "favorites.json" })
}

fn read_favorites_file(dir: &Path, sim: bool) -> Value {
    std::fs::read_to_string(favorites_path(dir, sim))
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

/// The pins: `person`'s own (#297), else the shared list.
fn load_favorites(dir: &Path, sim: bool, person: Option<&str>) -> Option<Vec<String>> {
    let payload = read_favorites_file(dir, sim);
    let list =
        person.and_then(|p| payload.get("people")?.get(p).cloned()).or_else(|| payload.get("favorites").cloned())?;
    let mut favorites: Vec<String> = serde_json::from_value(list).ok()?;
    favorites.truncate(MAX_FAVORITES);
    Some(favorites)
}

/// Everyone's pins share one file: its read-modify-writes take turns.
static PINS: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Write atomically: a temp file in the same directory, fsync, rename. A
/// person's pins go under `people`; without one, the shared list.
fn save_favorites(dir: &Path, sim: bool, person: Option<&str>, favorites: &[String]) -> std::io::Result<()> {
    let _turn = PINS.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    std::fs::create_dir_all(dir)?;
    let mut payload = read_favorites_file(dir, sim);
    match person {
        Some(person) => {
            if !payload.get("people").is_some_and(Value::is_object) {
                payload["people"] = json!({});
            }
            payload["people"][person] = json!(favorites);
        }
        None => payload["favorites"] = json!(favorites),
    }
    write_favorites_file(dir, sim, &payload)
}

fn write_favorites_file(dir: &Path, sim: bool, payload: &Value) -> std::io::Result<()> {
    use std::io::Write;
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let tmp = dir.join(format!(".favorites.{}.{n}.tmp", std::process::id()));
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(payload.to_string().as_bytes())?;
    file.sync_all()?;
    std::fs::rename(&tmp, favorites_path(dir, sim))
}

/// A profile was renamed (#297): its pins move with it, robot and sim.
pub fn rename_pins(dir: &Path, from: &str, to: &str) {
    let _turn = PINS.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    for sim in [false, true] {
        let mut payload = read_favorites_file(dir, sim);
        let Some(people) = payload.get_mut("people").and_then(Value::as_object_mut) else {
            continue;
        };
        let Some(pins) = people.remove(from) else {
            continue;
        };
        people.insert(to.to_string(), pins);
        if let Err(error) = write_favorites_file(dir, sim, &payload) {
            tracing::warn!(%error, "could not move pins from {from:?} to {to:?}");
        }
    }
}

/// Whose pins a request reads and writes: the acting person in admin mode.
fn pins_owner<'a>(state: &Shared, ctx: Option<&'a AuthContext>) -> Option<&'a str> {
    if !state.auth.admin_mode() {
        return None;
    }
    ctx.and_then(|c| c.person.as_deref())
}

async fn get_favorites(State(state): State<Shared>, ctx: Option<Extension<AuthContext>>) -> ApiResult<Json<Value>> {
    let ctx = ctx.map(|Extension(c)| c);
    let installed = installed_names(&state).await?;
    let allowed = |name: &String| installed.contains(name) && ctx.as_ref().is_none_or(|c| c.access.allows_app(name));
    let favorites = load_favorites(&state.state_dir, state.sim.active(), pins_owner(&state, ctx.as_ref()))
        .unwrap_or_else(|| installed.iter().filter(|n| allowed(n)).take(MAX_FAVORITES).cloned().collect());
    let favorites: Vec<String> = favorites.into_iter().filter(|name| allowed(name)).collect();
    Ok(Json(json!({"favorites": favorites})))
}

async fn put_favorites(
    State(state): State<Shared>,
    ctx: Option<Extension<AuthContext>>,
    body: Bytes,
) -> ApiResult<Json<Value>> {
    let ctx = ctx.map(|Extension(c)| c);
    // An unpicked shared tablet has no pins of its own to write (#297).
    if ctx.as_ref().is_some_and(|c| matches!(c.access, crate::profiles::Access::Unpicked)) {
        return Err(ApiError::new(403, "pick a profile first"));
    }
    let invalid = || ApiError::new(422, "Invalid favorites payload");
    let payload: Value = serde_json::from_slice(&body).map_err(|_| invalid())?;
    let favorites: Vec<String> =
        serde_json::from_value(payload.get("favorites").cloned().ok_or_else(invalid)?).map_err(|_| invalid())?;
    let mut seen = std::collections::HashSet::new();
    if !favorites.iter().all(|name| seen.insert(name)) {
        return Err(ApiError::new(422, "Duplicate favorite app"));
    }
    if favorites.len() > MAX_FAVORITES {
        return Err(ApiError::new(422, "Too many favorites (maximum 3)"));
    }
    let installed = installed_names(&state).await?;
    let allowed = |name: &String| installed.contains(name) && ctx.as_ref().is_none_or(|c| c.access.allows_app(name));
    if let Some(unknown) = favorites.iter().find(|name| !allowed(name)) {
        return Err(ApiError::new(422, format!("Unknown favorite app: {unknown}")));
    }
    save_favorites(&state.state_dir, state.sim.active(), pins_owner(&state, ctx.as_ref()), &favorites).map_err(
        |error| {
            tracing::error!(%error, "could not save favorites");
            ApiError::new(500, "Could not save favorites")
        },
    )?;
    Ok(Json(json!({"favorites": favorites})))
}

/// Refused while the daemon's idle reset is still putting the robot to sleep.
const GOING_TO_SLEEP: &str = "Robot is going to sleep; try again shortly";

fn refused(name: &str, error: impl Into<String>, status: &str) -> Json<Value> {
    Json(json!({"error": error.into(), "name": name, "status": status}))
}

/// Profiles (#297): an app outside the acting profile's list is refused.
fn app_allowed(ctx: Option<&Extension<AuthContext>>, name: &str) -> ApiResult<()> {
    match ctx {
        Some(Extension(ctx)) if !ctx.access.allows_app(name) => {
            Err(ApiError::new(403, "this app is off for this profile"))
        }
        _ => Ok(()),
    }
}

/// Start `name` with the controller's guards: no second app, no expression
/// or drive session, the idle reset waited out, a crashed app cleared, the
/// robot awake. `Err` is a refusal to show. Call with `power_lock` held.
pub async fn start_locked(state: &Shared, name: &str) -> ApiResult<Result<(), String>> {
    let daemon = &state.daemon;
    let refuse = |why: &str| Ok(Err(why.to_string()));
    // A drive session claims the robot under this lock, so this check
    // can't race one (#152).
    if state.player.active() {
        return refuse("Robot busy");
    }
    // Read the app first: an exit seen here has already scheduled the
    // daemon's idle reset, so the wait below can't miss it.
    let current = state.apps.current().await.ok().flatten();
    // The daemon refuses a second app anyway; refuse before waking the
    // robot or holding the lock through the waits below.
    if crate::apps::is_active(current.as_ref()) {
        return refuse("An app is already running");
    }
    // After an app exits on its own the daemon puts the robot to sleep;
    // clearing the app or waking the robot during that would stop the
    // sleep move part-way (#292).
    if !settle_idle_reset(state).await {
        return refuse(GOING_TO_SLEEP);
    }
    // An expression may have started during the wait, and a remote
    // session may have changed the app.
    if state.player.active() {
        return refuse("Robot busy");
    }
    let current = state.apps.current().await.ok().flatten();
    if crate::apps::is_active(current.as_ref()) {
        return refuse("An app is already running");
    }
    // The daemon keeps a crashed app in its slot (state "error") and
    // refuses every start until it is cleared.
    if current.as_ref().and_then(|c| c.get("state")).and_then(Value::as_str) == Some("error") {
        let _ = daemon.post_slow("apps/stop-current-app", APP_CONTROL_TIMEOUT).await;
        // reachy-mini 1.14.0 falls back to an idle reset when the clear's
        // return home fails.
        if !settle_idle_reset(state).await {
            return refuse(GOING_TO_SLEEP);
        }
        // As after the first wait.
        if state.player.active() {
            return refuse("Robot busy");
        }
        if crate::apps::is_active(state.apps.current().await.ok().flatten().as_ref()) {
            return refuse("An app is already running");
        }
    }
    ensure_awake(state).await?;
    // The wait for the wake move is long enough for an expression to begin.
    if state.player.active() {
        return refuse("Robot busy");
    }
    match daemon.post_slow(&format!("apps/start-app/{name}"), APP_CONTROL_TIMEOUT).await {
        Ok(_) => Ok(Ok(())),
        Err(crate::daemon::DaemonError::Unavailable) => Err(ApiError::unavailable()),
        Err(crate::daemon::DaemonError::Refused { detail, .. }) => {
            Ok(Err(detail.unwrap_or_else(|| "App refused".into())))
        }
    }
}

/// Start an installed app: clear a crashed app, wake the robot, then start.
async fn start(
    State(state): State<Shared>,
    ctx: Option<Extension<AuthContext>>,
    UrlPath(name): UrlPath<String>,
) -> ApiResult<Json<Value>> {
    app_allowed(ctx.as_ref(), &name)?;
    // Check-then-act across app and expression state isn't atomic; single-user UI.
    if state.player.active() {
        return Ok(refused(&name, "Expression playing", "refused"));
    }
    if !installed_names(&state).await?.contains(&name) {
        return Err(ApiError::new(404, "Unknown app"));
    }
    let started = {
        let _power = state.power_lock.lock().await;
        start_locked(&state, &name).await?
    };
    if let Err(why) = started {
        return Ok(refused(&name, why, "refused"));
    }
    state.apps.refresh_active().await;
    let port = state.apps.ui_port(&name).await.ok().flatten();
    Ok(Json(json!({"name": name, "status": "loading", "ui_port": port})))
}

/// Stop the daemon's current app (it runs one at a time). A profile stops
/// only an app on its list: the one running, whatever the URL names.
async fn stop(
    State(state): State<Shared>,
    ctx: Option<Extension<AuthContext>>,
    UrlPath(name): UrlPath<String>,
) -> ApiResult<Json<Value>> {
    let stopped = {
        // Under the power lock, so a stop can't interleave with a power off or
        // an app start, and the app checked is the one stopped.
        let _power = state.power_lock.lock().await;
        let mut current = state.apps.current().await;
        // Only clearing a crashed app can cut short the idle reset its exit
        // started (#292): a running app holds the robot, and with no app or
        // a `done` one the daemon stops nothing. An unreadable app may be
        // the crashed one. After a wait, read the app again: it is the one
        // checked and stopped.
        let crashed = current
            .as_ref()
            .map_or(true, |c| c.as_ref().and_then(|c| c.get("state")).and_then(Value::as_str) == Some("error"));
        if crashed {
            if !settle_idle_reset(&state).await {
                return Ok(refused(&name, GOING_TO_SLEEP, "refused"));
            }
            current = state.apps.current().await;
        }
        // Full access stops whatever runs, so an unreadable app is no reason
        // to refuse; a profile needs the app's name.
        if ctx.as_ref().is_some_and(|Extension(c)| !matches!(c.access, crate::profiles::Access::Full)) {
            let current = current.as_ref().map_err(|_| ApiError::unavailable())?;
            let running = current.as_ref().and_then(|c| c.get("info")?.get("name")?.as_str().map(str::to_string));
            if let Some(running) = running {
                app_allowed(ctx.as_ref(), &running)?;
            }
        }
        state.daemon.post_slow("apps/stop-current-app", APP_CONTROL_TIMEOUT).await
    };
    match stopped {
        Ok(_) => {}
        Err(crate::daemon::DaemonError::Unavailable) => return Err(ApiError::unavailable()),
        Err(crate::daemon::DaemonError::Refused { .. }) => {
            return Ok(refused(&name, "No app is running", "stopped"));
        }
    }
    state.apps.refresh_active().await;
    Ok(Json(json!({"name": name, "status": "stopping"})))
}

/// Port of the app's own web UI and whether it answers yet. Apps start their
/// web server a few seconds after the process; the dashboard waits for
/// `ready` so it never lands on a connection-refused page. Browsers open it
/// through the app proxy (`proxy_port` over plain http, #186): the app
/// itself listens on loopback.
async fn ui(
    State(state): State<Shared>,
    ctx: Option<Extension<AuthContext>>,
    UrlPath(name): UrlPath<String>,
) -> ApiResult<Json<Value>> {
    app_allowed(ctx.as_ref(), &name)?;
    let port = state.apps.ui_port(&name).await.map_err(|_| ApiError::unavailable())?;
    let ready = match port {
        Some(port) => {
            state.probe.get(format!("http://127.0.0.1:{port}/")).timeout(Duration::from_secs(1)).send().await.is_ok()
        }
        None => false,
    };
    Ok(Json(json!({"name": name, "port": port, "ready": ready, "proxy_port": crate::app_proxy::listening_port()})))
}
