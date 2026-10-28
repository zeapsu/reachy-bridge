//! Read side of the daemon's app manager (`/api/apps/*`).
//!
//! The daemon installs community apps from Hugging Face Spaces into its own
//! `apps_venv` and runs at most one at a time. The controller only reads and
//! drives that state; it never spawns app processes itself.

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use tokio::sync::Mutex;

use crate::daemon::{Daemon, DaemonError};

/// The daemon answers start-app and stop-current-app only once the process
/// has spawned or exited (it waits up to 20 s for an exit, then returns the
/// robot home over 1 s): longer than the default request timeout.
pub const APP_CONTROL_TIMEOUT: Duration = Duration::from_secs(25);

const INSTALLED_TTL: Duration = Duration::from_secs(5);

#[derive(Default)]
struct Tracked {
    installed: Vec<Value>,
    installed_at: Option<Instant>,
    current: Option<Value>,
    current_name: Option<String>,
    current_since: Option<f64>,
}

pub struct DaemonApps {
    daemon: Daemon,
    tracked: Mutex<Tracked>,
}

fn is_active(current: Option<&Value>) -> bool {
    matches!(state_of(current), Some("starting" | "running"))
}

fn state_of(current: Option<&Value>) -> Option<&str> {
    current.and_then(|c| c.get("state")).and_then(Value::as_str)
}

fn name_of(current: Option<&Value>) -> Option<String> {
    current
        .and_then(|c| c.get("info"))
        .and_then(|i| i.get("name"))
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// Daemon app state → the card state the dashboard renders.
fn card_state(state: &str) -> &'static str {
    match state {
        "starting" => "loading",
        "running" => "running",
        "stopping" => "stopping",
        "error" => "error",
        _ => "stopped",
    }
}

pub fn display_name(name: &str) -> String {
    let normalized = name.replace('-', "_");
    let mut words: Vec<&str> = normalized.split('_').collect();
    if words.len() > 2 && words[0] == "reachy" && words[1] == "mini" {
        words.drain(..2);
    }
    words
        .iter()
        .filter(|w| !w.is_empty())
        .map(|w| {
            let mut chars = w.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().collect::<String>() + &chars.as_str().to_lowercase(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Port of an app's `custom_app_url` (for example `http://0.0.0.0:8042`).
pub fn url_port(url: &str) -> Option<u16> {
    let rest = url.split_once("://").map_or(url, |(_, rest)| rest);
    let authority = rest.split(['/', '?', '#']).next()?;
    authority.rsplit_once(':')?.1.parse().ok()
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

impl DaemonApps {
    pub fn new(daemon: Daemon) -> Self {
        Self { daemon, tracked: Mutex::new(Tracked::default()) }
    }

    pub async fn installed(&self) -> Result<Vec<Value>, DaemonError> {
        let mut tracked = self.tracked.lock().await;
        self.installed_locked(&mut tracked).await
    }

    async fn installed_locked(&self, tracked: &mut Tracked) -> Result<Vec<Value>, DaemonError> {
        if let Some(at) = tracked.installed_at
            && !tracked.installed.is_empty()
            && at.elapsed() < INSTALLED_TTL
        {
            return Ok(tracked.installed.clone());
        }
        let payload = self.daemon.get("apps/list-available/installed").await?;
        tracked.installed = payload.as_array().cloned().unwrap_or_default();
        tracked.installed_at = Some(Instant::now());
        Ok(tracked.installed.clone())
    }

    /// Re-read the installed list on the next call (after an install,
    /// update or removal).
    pub async fn forget_installed(&self) {
        self.tracked.lock().await.installed_at = None;
    }

    pub async fn installed_names(&self) -> Result<Vec<String>, DaemonError> {
        Ok(self
            .installed()
            .await?
            .iter()
            .filter_map(|item| item.get("name").and_then(Value::as_str).map(str::to_string))
            .collect())
    }

    /// The daemon's current app (`None` when there is none).
    pub async fn current(&self) -> Result<Option<Value>, DaemonError> {
        let mut tracked = self.tracked.lock().await;
        self.current_locked(&mut tracked).await
    }

    async fn current_locked(&self, tracked: &mut Tracked) -> Result<Option<Value>, DaemonError> {
        let payload = self.daemon.get("apps/current-app-status").await?;
        let current = payload.is_object().then_some(payload);
        let name = name_of(current.as_ref());
        let active = is_active(current.as_ref());
        let was_active = is_active(tracked.current.as_ref());
        // A new app, or the same app started again after it stopped.
        if name != tracked.current_name || (active && !was_active) {
            tracked.current_since = name.as_ref().map(|_| now_secs());
            tracked.current_name = name;
        }
        tracked.current = current.clone();
        Ok(current)
    }

    /// Re-read the daemon before an actuation decision; fail closed.
    pub async fn refresh_active(&self) -> bool {
        match self.current().await {
            Ok(current) => is_active(current.as_ref()),
            Err(_) => true,
        }
    }

    /// Installed apps with the daemon's current run state.
    pub async fn statuses(&self) -> Result<Vec<Value>, DaemonError> {
        let mut tracked = self.tracked.lock().await;
        let installed = self.installed_locked(&mut tracked).await?;
        let current = self.current_locked(&mut tracked).await?;
        let current_name = name_of(current.as_ref());
        let since = tracked.current_since;
        Ok(installed
            .iter()
            .filter_map(|item| {
                let name = item.get("name").and_then(Value::as_str).filter(|n| !n.is_empty())?;
                let has_ui = item
                    .get("extra")
                    .and_then(|e| e.get("custom_app_url"))
                    .is_some_and(|u| u.as_str().is_some_and(|s| !s.is_empty()));
                let mut status = "stopped";
                let mut error = Value::Null;
                let mut start_time = Value::Null;
                if current_name.as_deref() == Some(name)
                    && let Some(current) = current.as_ref()
                {
                    status = card_state(current.get("state").and_then(Value::as_str).unwrap_or(""));
                    error = current.get("error").cloned().unwrap_or(Value::Null);
                    if status == "running" {
                        start_time = since.map_or(Value::Null, Value::from);
                    }
                }
                let extra = item.get("extra");
                let card = extra.and_then(|e| e.get("cardData"));
                let blurb = [item.get("description"), card.and_then(|c| c.get("short_description"))]
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .find(|d| !d.is_empty())
                    .unwrap_or("");
                // Apps installed from a Space keep its id, for a link to its page.
                let space = extra
                    .and_then(|e| e.get("id"))
                    .and_then(Value::as_str)
                    .filter(|id| crate::routes::store::space_id(id));
                Some(json!({
                    "name": name,
                    // The Space's own title, as the catalog shows it.
                    "display": card
                        .and_then(|c| c.get("title"))
                        .and_then(Value::as_str)
                        .filter(|t| !t.is_empty())
                        .map_or_else(|| display_name(name), str::to_string),
                    "blurb": blurb,
                    "space": space,
                    "author": extra.and_then(|e| e.get("author")).and_then(Value::as_str),
                    "status": status,
                    "error": error,
                    "start_time": start_time,
                    "has_ui": has_ui,
                }))
            })
            .collect())
    }

    /// Port of the app's own web UI, from its `custom_app_url`.
    pub async fn ui_port(&self, name: &str) -> Result<Option<u16>, DaemonError> {
        Ok(self
            .installed()
            .await?
            .iter()
            .find(|item| item.get("name").and_then(Value::as_str) == Some(name))
            .and_then(|item| item.get("extra")?.get("custom_app_url")?.as_str().and_then(url_port)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_name_drops_reachy_mini_prefix() {
        assert_eq!(display_name("reachy_mini_radio"), "Radio");
        assert_eq!(display_name("reachy-mini-conversation-app"), "Conversation App");
        assert_eq!(display_name("hello_world"), "Hello World");
        assert_eq!(display_name("reachy_mini"), "Reachy Mini");
    }

    #[test]
    fn url_port_reads_the_authority() {
        assert_eq!(url_port("http://0.0.0.0:8042"), Some(8042));
        assert_eq!(url_port("http://0.0.0.0:7860/ui?x=1"), Some(7860));
        assert_eq!(url_port("0.0.0.0:8042"), Some(8042));
        assert_eq!(url_port("http://localhost/"), None);
    }
}
