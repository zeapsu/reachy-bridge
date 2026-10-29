//! Sim mode (#149): try the controller on a simulated robot, with no hardware.
//!
//! An on-demand second daemon (`reachy-bridge-sim.service`: MuJoCo, no media,
//! its own apps folder and loopback port) takes the robot daemon's place.
//! Entering sim mode stops the robot's daemon (the robot is confirmed off
//! first), so nothing a sim app does can reach the robot even past the
//! runtime's port shim (sim_apps, #218); leaving starts it again. Entering sim mode
//! starts it and points the controller's [`Daemon`] client at it; leaving
//! points the client back at the robot and stops it.
//!
//! Sim mode is only entered with the robot confirmed off, and nothing in the
//! controller can switch the robot on while it lasts (every action goes to
//! the sim). A switch waits for every action in flight to finish, and actions
//! wait for a switch, so an action checked against one target never runs
//! against the other. The dashboard sends the target it believes in
//! (`X-Reachy-Target`, or `?target=` on the drive socket), and an action from
//! a tab that missed a switch is refused rather than carried out on the other
//! robot.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use axum::extract::{Request, State};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use crate::daemon::{backend_up, robot_state, Daemon};
use crate::error::{ApiError, ApiResult};
use crate::routes::power::wait_until;
use crate::state::Shared;

/// The header the dashboard puts its believed target in.
pub const TARGET_HEADER: &str = "x-reachy-target";
/// MuJoCo and the daemon take a while to import on the Jetson.
const SIM_START_TIMEOUT: Duration = Duration::from_secs(90);
/// The unit's own `TimeoutStopSec` bounds a stop; this bounds `systemctl`.
const SYSTEMCTL_TIMEOUT: Duration = Duration::from_secs(40);
/// How long leaving waits for sim moves to stop.
const MOVES_STOP_TIMEOUT: Duration = Duration::from_secs(2);
/// The robot's daemon after a restart, as on the Jetson.
const ROBOT_START_TIMEOUT: Duration = Duration::from_secs(60);
/// Stopping the sim's app on exit, which holds both locks.
const APP_STOP_TIMEOUT: Duration = Duration::from_secs(5);
/// How long a switch waits for actions in flight before answering busy:
/// queued, it would hold every new action (power off included) behind them.
const SWITCH_WAIT: Duration = Duration::from_secs(3);
/// The volume and sound routes: the sim daemon would set the host's real
/// speaker and microphone (except [`SIM_SOUNDS`]).
/// The Hugging Face login is the host's, shared with the robot's daemon.
const SIM_REFUSED: [(&str, &str); 3] = [
    ("/api/volume/", "Sound is off in sim mode"),
    ("/api/media/", "Sound is off in sim mode"),
    ("/api/hf/token", "Sign in to Hugging Face from the robot, not the simulation"),
];

/// Sound routes that only play or stop a built-in sound: with no media the
/// sim daemon plays nothing on the host, and the dashboard plays it instead
/// (#219). `media/play_sound` stays refused: it takes any path, and in sim
/// mode the file would come back to the browser.
const SIM_SOUNDS: [&str; 2] = ["/api/volume/test-sound", "/api/media/stop-sound"];

pub struct SimMode {
    robot_url: String,
    sim_url: String,
    /// The systemd user unit to start and stop; `None` when something else
    /// runs the sim daemon (tests, `tools/sim`).
    unit: Option<String>,
    /// The robot daemon's unit, stopped for the whole of sim mode; only set
    /// together with `unit`.
    robot_unit: Option<String>,
    /// The `systemctl` to run (tests point it at a fake).
    systemctl: String,
    active: AtomicBool,
    /// Actions hold it shared, a switch exclusively.
    switch: tokio::sync::RwLock<()>,
}

impl SimMode {
    /// `robot_unit` only counts together with `unit`.
    pub fn new(robot_url: &str, sim_url: &str, unit: Option<String>, robot_unit: Option<String>) -> Self {
        let unit = unit.filter(|u| !u.is_empty());
        Self {
            robot_url: robot_url.to_string(),
            sim_url: sim_url.to_string(),
            robot_unit: robot_unit.filter(|u| !u.is_empty() && unit.is_some()),
            unit,
            systemctl: "systemctl".into(),
            active: AtomicBool::new(false),
            switch: tokio::sync::RwLock::new(()),
        }
    }

    /// `SIM_DAEMON_URL` (default `http://127.0.0.1:8001`) and `SIM_DAEMON_UNIT`
    /// (`reachy-bridge-sim.service`; empty leaves starting it, and stopping
    /// the robot's daemon, to someone else). The robot daemon's unit is
    /// `REACHY_DAEMON_UNIT` (`reachy-bridge.service`).
    pub fn from_env(robot_url: &str) -> Self {
        let sim_url = std::env::var("SIM_DAEMON_URL").ok().filter(|v| !v.is_empty());
        let unit = std::env::var("SIM_DAEMON_UNIT").unwrap_or_else(|_| "reachy-bridge-sim.service".into());
        let robot_unit = std::env::var("REACHY_DAEMON_UNIT").ok().filter(|u| !u.is_empty());
        let robot_unit = robot_unit.unwrap_or_else(|| "reachy-bridge.service".into());
        Self::new(robot_url, sim_url.as_deref().unwrap_or("http://127.0.0.1:8001"), Some(unit), Some(robot_unit))
    }

    /// Run this program instead of `systemctl` (tests).
    pub fn with_systemctl(mut self, program: &str) -> Self {
        self.systemctl = program.to_string();
        self
    }

    async fn systemctl(&self, action: &str, unit: &str) -> Result<(), String> {
        systemctl(&self.systemctl, action, unit).await
    }

    pub fn active(&self) -> bool {
        self.active.load(Ordering::SeqCst)
    }

    pub fn target(&self) -> &'static str {
        if self.active() { "sim" } else { "robot" }
    }

    pub fn sim_url(&self) -> &str {
        &self.sim_url
    }

}

/// Undo a sim mode an earlier controller left behind (a crash or restart
/// mid-sim): stop the sim daemon and start the robot's again. Under
/// `power_lock`, so a sim mode entered meanwhile isn't undone under it.
pub async fn stop_leftover(state: Shared) {
    let _power = state.power_lock.lock().await;
    if state.sim.active() {
        return;
    }
    if let Some(unit) = &state.sim.unit
        && let Err(error) = state.sim.systemctl("stop", unit).await
    {
        tracing::debug!(%error, "sim daemon not stopped");
    }
    if let Some(robot) = &state.sim.robot_unit
        && let Err(error) = state.sim.systemctl("start", robot).await
    {
        tracing::error!(%error, "the robot's daemon didn't start");
    }
}

pub(crate) async fn systemctl(program: &str, action: &str, unit: &str) -> Result<(), String> {
    let run = tokio::process::Command::new(program)
        .args(["--user", action, unit])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .output();
    match tokio::time::timeout(SYSTEMCTL_TIMEOUT, run).await {
        Ok(Ok(output)) if output.status.success() => Ok(()),
        Ok(Ok(output)) => Err(String::from_utf8_lossy(&output.stderr).trim().chars().take(300).collect()),
        Ok(Err(error)) => Err(error.to_string()),
        Err(_) => Err(format!("systemctl {action} {unit} timed out")),
    }
}

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/sim", get(status))
        .route("/api/sim/enter", post(enter))
        .route("/api/sim/exit", post(exit))
}

async fn switch_lock(state: &Shared) -> ApiResult<tokio::sync::RwLockWriteGuard<'_, ()>> {
    tokio::time::timeout(SWITCH_WAIT, state.sim.switch.write())
        .await
        .map_err(|_| ApiError::new(409, "The robot is busy with an action; try again when it finishes"))
}

async fn status(State(state): State<Shared>) -> Json<Value> {
    Json(json!({"active": state.sim.active(), "target": state.sim.target()}))
}

/// Start the sim daemon and switch the controller to it. Refused unless the
/// robot is confirmed off: a daemon that doesn't answer may be busy with a
/// move (daemon-contract.md), not down.
async fn enter(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let _switch = switch_lock(&state).await?;
    let _power = state.power_lock.lock().await;
    let sim = &state.sim;
    if sim.active() {
        return Ok(Json(json!({"active": true, "target": "sim"})));
    }
    let Some(status) = state.daemon.status().await else {
        return Err(ApiError::new(503, "Can't confirm the robot is off; try again shortly"));
    };
    let robot = robot_state(&status);
    if backend_up(&status) || matches!(robot.as_str(), "running" | "asleep" | "starting" | "stopping") {
        return Err(ApiError::new(409, "Switch the robot off before starting the simulation"));
    }
    if state.player.active() || state.apps.refresh_active().await {
        return Err(ApiError::new(409, "Stop the app or expression before starting the simulation"));
    }
    // Nothing listens on the robot daemon's port while the sim runs, so a
    // sim app's connection the port shim misses finds no robot. The robot is
    // off (checked above, under power_lock), so stopping its daemon moves
    // nothing.
    if let Some(robot) = &sim.robot_unit
        && let Err(error) = sim.systemctl("stop", robot).await
    {
        // It may have stopped anyway: don't leave the robot without one.
        let _ = restart_robot_daemon(&state).await;
        return Err(ApiError::new(502, format!("Couldn't stop the robot's daemon: {error}")));
    }
    let started = match &sim.unit {
        Some(unit) => sim.systemctl("start", unit).await.map_err(|error| format!("Couldn't start the simulation: {error}")),
        None => Ok(()),
    };
    let probe = Daemon::new(&sim.sim_url, Duration::from_secs(2));
    let failure = match started {
        Err(error) => Some(ApiError::new(502, error)),
        Ok(()) if !wait_until(SIM_START_TIMEOUT, || async { probe.status().await.is_some() }).await => {
            Some(ApiError::new(504, "The simulation didn't start in time"))
        }
        Ok(()) => None,
    };
    if let Some(error) = failure {
        if let Some(unit) = &sim.unit {
            let _ = state.sim.systemctl("stop", unit).await;
        }
        let _ = restart_robot_daemon(&state).await;
        return Err(error);
    }
    state.player.abort_drive();
    state.daemon.retarget(&sim.sim_url);
    sim.active.store(true, Ordering::SeqCst);
    state.apps.forget_installed().await;
    tracing::info!("sim mode on: the controller drives the sim daemon at {}", sim.sim_url);
    Ok(Json(json!({"active": true, "target": "sim"})))
}

/// Switch the controller back to the robot (still off) and stop the sim.
async fn exit(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let switch = switch_lock(&state).await?;
    let power = state.power_lock.lock().await;
    let sim = &state.sim;
    if !sim.active() {
        // A previous exit whose restart failed is retried here.
        if sim.robot_unit.is_some() && state.daemon.status().await.is_none() {
            restart_robot_daemon(&state).await?;
        }
        return Ok(Json(json!({"active": false, "target": "robot"})));
    }
    // Nothing started on the sim may carry on against the robot. Its app
    // stops with the sim daemon too; stopping it here also covers a sim
    // daemon that isn't a unit, and frees the app UI port first.
    if state.apps.refresh_active().await {
        let _ = state.daemon.post_slow("apps/stop-current-app", APP_STOP_TIMEOUT).await;
    }
    state.player.abort_drive();
    state.player.halt(MOVES_STOP_TIMEOUT).await;
    state.daemon.retarget(&sim.robot_url);
    sim.active.store(false, Ordering::SeqCst);
    // The robot's pose is unknown to the player: no expression before a
    // power-on (or a return home) puts it somewhere known.
    state.player.set_needs_home(true);
    state.apps.forget_installed().await;
    tracing::info!("sim mode off: the controller drives the robot's daemon again");
    // Nothing points at the sim any more: let actions through while it
    // stops, but keep power_lock, which enter also takes, so a new sim mode
    // can't start on a daemon this stop is about to kill.
    drop(switch);
    if let Some(unit) = &sim.unit
        && let Err(error) = state.sim.systemctl("stop", unit).await
    {
        tracing::warn!(%error, "sim daemon not stopped");
    }
    let restarted = restart_robot_daemon(&state).await;
    drop(power);
    restarted?;
    Ok(Json(json!({"active": false, "target": "robot"})))
}

/// Start the robot's daemon (robot off: it starts with --no-autostart) and
/// wait for it to answer.
async fn restart_robot_daemon(state: &Shared) -> ApiResult<()> {
    let Some(robot) = &state.sim.robot_unit else {
        return Ok(());
    };
    if let Err(error) = state.sim.systemctl("start", robot).await {
        tracing::error!(%error, "the robot's daemon didn't start");
        return Err(ApiError::new(502, format!("The robot's daemon didn't start: {error}")));
    }
    let robot_daemon = Daemon::new(&state.sim.robot_url, Duration::from_secs(2));
    if !wait_until(ROBOT_START_TIMEOUT, || async { robot_daemon.status().await.is_some() }).await {
        return Err(ApiError::new(504, "The robot's daemon didn't answer in time"));
    }
    Ok(())
}

/// The target a request believes in: the header, or `?target=` on the drive
/// socket (a browser can't set headers on a WebSocket).
fn believed(request: &Request) -> Option<String> {
    if let Some(header) = request.headers().get(TARGET_HEADER) {
        return header.to_str().ok().map(str::to_string);
    }
    request.uri().query()?.split('&').find_map(|pair| pair.strip_prefix("target=")).map(str::to_string)
}

/// Hold actions off while a switch runs (and a switch off while actions
/// run). Refuse app installs and runs in sim mode, and any action from a tab
/// that believes in the other target.
pub async fn guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    let path = request.uri().path();
    let action = (!request.method().is_safe() || path == "/api/drive/ws")
        && path.starts_with("/api/")
        && !path.starts_with("/api/sim")
        && !path.starts_with("/api/auth/");
    if action {
        let state = state.clone();
        let _switch = state.sim.switch.read().await;
        let believed = believed(&request);
        let target = state.sim.target();
        if believed.is_some_and(|b| b != target) {
            let detail = if target == "sim" {
                "The controller switched to the simulation; reload the page"
            } else {
                "The controller switched back to the robot; reload the page"
            };
            return ApiError::new(409, detail).into_response();
        }
        if state.sim.active()
            && !SIM_SOUNDS.contains(&path)
            && let Some((_, detail)) = SIM_REFUSED.iter().find(|(prefix, _)| path.starts_with(prefix))
        {
            return ApiError::new(409, *detail).into_response();
        }
        return next.run(request).await;
    }
    next.run(request).await
}
