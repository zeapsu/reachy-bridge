//! Conversation on wake (#334): one app the controller starts when the
//! dashboard powers the robot on.
//!
//! The daemon has a startup app of its own, and the bridge can't use it. The
//! daemon runs with `--no-autostart`, and a backend started over REST
//! (`daemon/start`) gets no wake hook, so a saved startup app never starts
//! on power-on. Setting it on a running backend arms an antenna watcher
//! instead, which checks the daemon's app slot and none of the controller's
//! guards (docs/daemon-contract.md, "Apps" and rule 9). So the choice is
//! kept here, and the app starts the way a start from the Apps tab does,
//! except that this start never wakes the robot or switches its motors on:
//! the power-on did that, and whoever let the motors go since meant it.

use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;

use axum::body::Bytes;
use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::auth::AuthContext;
use crate::daemon::robot_state;
use crate::error::{ApiError, ApiResult};
use crate::routes::apps::start_locked;
use crate::routes::power::{WAKE_TIMEOUT, wait_until};
use crate::state::Shared;

/// The only app the switch is for.
const CONVERSATION_APP: &str = "reachy_mini_conversation_app";

/// `REACHY_WAKE_APP` names another app: the sim stack has no Conversation
/// app, so its tests use one of theirs.
pub fn app_from_env() -> String {
    std::env::var("REACHY_WAKE_APP").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| CONVERSATION_APP.to_string())
}

pub fn router() -> Router<Shared> {
    Router::new().route("/api/system/wake-app", get(read).put(write))
}

/// The sim's apps aren't the robot's, so neither is its switch (as favorites).
fn path(dir: &Path, sim: bool) -> PathBuf {
    dir.join(if sim { "wake-app-sim.json" } else { "wake-app.json" })
}

fn enabled(state: &Shared) -> bool {
    std::fs::read_to_string(path(&state.state_dir, state.sim.active()))
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|v| v.get("enabled")?.as_bool())
        .unwrap_or(false)
}

fn save(state: &Shared, enabled: bool) -> std::io::Result<()> {
    let dir = &state.state_dir;
    std::fs::create_dir_all(dir)?;
    let target = path(dir, state.sim.active());
    let tmp = target.with_extension("json.tmp");
    std::fs::write(&tmp, json!({"enabled": enabled}).to_string())?;
    std::fs::rename(tmp, target)
}

/// Whether the app is installed; `None` while the daemon can't say.
async fn installed(state: &Shared) -> Option<bool> {
    let names = state.apps.installed_names().await.ok()?;
    Some(names.contains(&state.wake_app))
}

/// `installed` is null while the daemon can't say.
fn view(state: &Shared, installed: Option<bool>) -> Json<Value> {
    Json(json!({
        "app": state.wake_app,
        "name": crate::apps::display_name(&state.wake_app),
        "installed": installed,
        "enabled": enabled(state),
    }))
}

async fn read(State(state): State<Shared>) -> Json<Value> {
    view(&state, installed(&state).await)
}

async fn write(State(state): State<Shared>, body: Bytes) -> ApiResult<Json<Value>> {
    let on = serde_json::from_slice::<Value>(&body)
        .ok()
        .and_then(|v| v.get("enabled")?.as_bool())
        .ok_or_else(|| ApiError::new(422, r#"Body must be {"enabled": true or false}"#))?;
    // Off needs nothing from the daemon: it works while the daemon is down.
    let installed = installed(&state).await;
    if on {
        match installed {
            None => return Err(ApiError::unavailable()),
            Some(false) => return Err(ApiError::new(409, "Install the app first")),
            Some(true) => {}
        }
    }
    save(&state, on).map_err(|error| {
        tracing::error!(%error, "could not save the wake app switch");
        ApiError::new(500, "Could not save the setting")
    })?;
    Ok(view(&state, installed))
}

/// The app is being removed: it must not start by itself once it is back.
/// Only here, where someone asked for the removal: an app that is merely
/// not listed (an update reinstalls it) isn't started, and the switch stays.
pub fn removed(state: &Shared, name: &str) {
    if name == state.wake_app
        && enabled(state)
        && let Err(error) = save(state, false)
    {
        tracing::error!(%error, "could not switch the wake app off");
    }
}

/// The dashboard's power-on woke the robot: start the app once it is up.
/// A profile the app is off for (#297) doesn't get it by powering on.
/// `epoch` is the `power_epoch` that power-on made: the start is dropped if
/// the robot was switched off, had its motors let go or was switched on
/// again since, so it never acts for a later power-on, which may be someone
/// else's.
pub fn after_power_on(state: &Shared, ctx: Option<&AuthContext>, epoch: u64) {
    let name = state.wake_app.clone();
    if !enabled(state) || ctx.is_some_and(|c| !c.access.allows_app(&name)) {
        return;
    }
    let device = ctx.and_then(|c| c.device.clone());
    let state = state.clone();
    tokio::spawn(async move {
        match start_when_awake(&state, &name, epoch, device.as_deref()).await {
            Ok(()) => tracing::info!("started {name} on wake"),
            Err(why) => tracing::warn!("{name} not started on wake: {why}"),
        }
    });
}

/// `device`: the paired device that powered the robot on, if it was one.
async fn start_when_awake(state: &Shared, name: &str, epoch: u64, device: Option<&str>) -> Result<(), String> {
    let daemon = &state.daemon;
    let on = || async { daemon.status().await.is_some_and(|s| robot_state(&s) == "running") };
    let same_power_on = || state.power_epoch.load(Ordering::SeqCst) == epoch;
    // The wake is waited out before the locks, so a power off during it
    // isn't held up. Only the app's own start runs under them, as a start
    // from the Apps tab does.
    if !wait_until(WAKE_TIMEOUT, || async { !same_power_on() || on().await }).await {
        return Err("the robot did not wake up in time".into());
    }
    // From asleep the robot is "running" at once, with the wake move still
    // playing. Like `ensure_awake`, carry on if the moves never clear.
    wait_until(WAKE_TIMEOUT, || async {
        !same_power_on() || daemon.running_moves().await.is_some_and(|m| m.is_empty())
    })
    .await;
    let stale = "the robot was switched off, had its motors let go or was switched on again meanwhile";
    // Before queueing on the locks: a start nobody wants any more holds nothing up.
    if !same_power_on() {
        return Err(stale.into());
    }
    // As a request's action: a switch to or from the simulation waits for it.
    let _switch = state.sim.acting().await;
    let _power = state.power_lock.lock().await;
    if !same_power_on() || !on().await {
        return Err(stale.into());
    }
    if !enabled(state) {
        return Err("switched off meanwhile".into());
    }
    // Revoked, or the app taken off its profile, during the wake.
    if device.is_some_and(|id| !state.auth.access_for_device(id).allows_app(name)) {
        return Err("the device that switched the robot on may no longer start it".into());
    }
    let installed = state.apps.installed_names().await.map_err(|_| "robot service down")?;
    if !installed.iter().any(|n| n == name) {
        return Err("not installed".into());
    }
    // Never waking: a robot whose motors were let go a moment ago stays as it is.
    let started = start_locked(state, name, false).await.map_err(|error| error.detail)?;
    state.apps.refresh_active().await;
    started
}
