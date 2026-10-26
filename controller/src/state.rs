//! Shared controller state and configuration.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use crate::apps::DaemonApps;
use crate::auth::{Auth, AuthConfig};
use crate::daemon::Daemon;
use crate::datasets::DatasetCache;
use crate::expressions::ExpressionPlayer;
use crate::logs::LogBroker;
use crate::routes::telemetry::Streams;

pub type Shared = Arc<AppState>;

pub struct AppState {
    pub daemon: Daemon,
    /// Held by every action that powers, wakes or sleeps the robot (#148).
    pub power_lock: tokio::sync::Mutex<()>,
    pub apps: DaemonApps,
    pub player: Arc<ExpressionPlayer>,
    pub streams: Streams,
    pub logs: Arc<LogBroker>,
    /// Probes app UIs on loopback.
    pub probe: reqwest::Client,
    pub datasets: DatasetCache,
    /// Where favorites, the dataset cache and the controller log live.
    pub state_dir: PathBuf,
    /// LAN login (#141); off unless `CONTROLLER_LAN=1`.
    pub auth: Auth,
}

impl AppState {
    pub fn new(daemon: Daemon, logs: Arc<LogBroker>, state_dir: PathBuf, poll: Duration) -> Shared {
        Self::with_auth(daemon, logs, state_dir, poll, AuthConfig::default())
    }

    pub fn with_auth(daemon: Daemon, logs: Arc<LogBroker>, state_dir: PathBuf, poll: Duration, auth: AuthConfig) -> Shared {
        Arc::new(Self {
            auth: Auth::new(auth, &state_dir),
            apps: DaemonApps::new(daemon.clone()),
            player: ExpressionPlayer::new(daemon.clone(), poll),
            daemon,
            power_lock: tokio::sync::Mutex::new(()),
            streams: Streams::default(),
            logs,
            probe: reqwest::Client::new(),
            datasets: DatasetCache::new(state_dir.join("datasets.json")),
            state_dir,
        })
    }
}

/// `$REACHY_BRIDGE_STATE_DIR`, else `$XDG_STATE_HOME/reachy-bridge`, else
/// `~/.local/state/reachy-bridge` (shared with the runtime wrapper).
pub fn state_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os("REACHY_BRIDGE_STATE_DIR").filter(|d| !d.is_empty()) {
        return PathBuf::from(dir);
    }
    let base = std::env::var_os("XDG_STATE_HOME").filter(|d| !d.is_empty()).map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/state")
    });
    base.join("reachy-bridge")
}
