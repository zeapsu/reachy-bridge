//! Family mode (#297): what a paired person may do in admin mode.
//!
//! A profile is a person (the `person` every paired device has, #221) plus
//! what the admin allows them: the apps they see and start (all, or a list),
//! and four switches: robot power, drive and expressions (with turning the
//! motors on, which they need), voice, and the camera. A kid profile also
//! loses the Controls pane: volume, sounds, and any other motor mode. The System tab (board stats, logs,
//! the microphone meter, the sim switch) is the admin's only, for every
//! profile.
//!
//! A person without a stored profile is a grown-up with everything
//! allowed, so devices paired before #297 keep working as they did, minus
//! System. Admin devices, the local key, and every request outside admin
//! mode have [`Access::Full`].
//!
//! A shared device (a family tablet) belongs to nobody: whoever uses it picks
//! a profile from a list that never offers a person with an admin device.
//! Until someone picks, it can only read what the dashboard needs to draw.
//!
//! Everything here is enforced by the controller ([`check`] in the auth
//! guard; app names in the app routes and the app UI proxy). The dashboard
//! only mirrors it.

use std::sync::Arc;

use axum::http::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::error::ApiError;

/// Kids lose the Controls pane; otherwise the same rules as grown-ups.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    #[default]
    Grown,
    Kid,
}

fn yes() -> bool {
    true
}

/// What a profile may do besides apps. Missing switches are on, so a file
/// written before a switch existed keeps allowing it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Can {
    #[serde(default = "yes")]
    pub power: bool,
    /// Drive and expressions (and the motor mode they need).
    #[serde(default = "yes")]
    pub drive: bool,
    /// Listen, talk and hear (audio sessions).
    #[serde(default = "yes")]
    pub voice: bool,
    #[serde(default = "yes")]
    pub camera: bool,
}

impl Default for Can {
    fn default() -> Self {
        Self { power: true, drive: true, voice: true, camera: true }
    }
}

impl Can {
    const NOTHING: Self = Self { power: false, drive: false, voice: false, camera: false };
}

/// A stored profile, keyed by person name.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile {
    pub name: String,
    #[serde(default)]
    pub kind: Kind,
    /// `None`: every installed app.
    #[serde(default)]
    pub apps: Option<Vec<String>>,
    #[serde(default)]
    pub can: Can,
}

impl Profile {
    /// A person nobody has restricted: a grown-up with everything.
    pub fn open(name: &str) -> Self {
        Self { name: name.to_string(), kind: Kind::Grown, apps: None, can: Can::default() }
    }

    pub fn allows_app(&self, app: &str) -> bool {
        self.apps.as_ref().is_none_or(|apps| apps.iter().any(|a| a == app))
    }
}

/// What a request may do (see the module docs).
#[derive(Clone, Debug)]
pub enum Access {
    /// The admin, the CLI, or no admin mode.
    Full,
    /// A person's profile.
    Limited(Arc<Profile>),
    /// A shared device nobody has picked a profile on yet.
    Unpicked,
}

impl Access {
    pub fn allows_app(&self, app: &str) -> bool {
        match self {
            Self::Full => true,
            Self::Limited(profile) => profile.allows_app(app),
            Self::Unpicked => false,
        }
    }

    fn can(&self) -> Can {
        match self {
            Self::Full => Can::default(),
            Self::Limited(profile) => profile.can,
            Self::Unpicked => Can::NOTHING,
        }
    }

    fn kid(&self) -> bool {
        match self {
            Self::Full => false,
            Self::Limited(profile) => profile.kind == Kind::Kid,
            Self::Unpicked => true,
        }
    }

    /// What the dashboard shows and dims, for `/api/auth/status`.
    pub fn view(&self) -> Value {
        match self {
            Self::Full => json!({"admin": true, "kind": "grown", "apps": null, "can": Can::default()}),
            Self::Limited(profile) => {
                json!({"admin": false, "kind": profile.kind, "apps": profile.apps, "can": profile.can})
            }
            Self::Unpicked => json!({"admin": false, "kind": "kid", "apps": [], "can": Can::NOTHING}),
        }
    }
}

fn under(path: &str, prefix: &str) -> bool {
    path == prefix || path.starts_with(&format!("{prefix}/"))
}

/// The System tab's data and actions: the admin's only, reads included.
fn system(path: &str) -> bool {
    under(path, "/api/system")
        || under(path, "/api/logs")
        || path == "/api/audio/levels-stream"
        || path == "/api/sim/enter"
        || path == "/api/sim/exit"
        || path == "/api/sim/sounds-stream"
}

/// The Controls pane's changes (a kid's profile can't make them).
fn controls(method: &Method, path: &str) -> bool {
    !method.is_safe()
        && (under(path, "/api/volume")
            || under(path, "/api/media/wobbling")
            || path == "/api/media/play_sound"
            || path == "/api/media/stop-sound")
}

/// Refuse what `access` doesn't allow, by route. App names are checked by
/// the app routes and the app UI proxy, which know them.
pub fn check(method: &Method, path: &str, access: &Access) -> Result<(), ApiError> {
    if matches!(access, Access::Full) {
        return Ok(());
    }
    let refuse = |what: &str| Err(ApiError::new(403, format!("{what} is off for this profile")));
    if system(path) {
        return Err(ApiError::new(403, "only an admin can open System"));
    }
    if matches!(access, Access::Unpicked) && under(path, "/api/control/take") {
        return Err(ApiError::new(403, "pick a profile first"));
    }
    let can = access.can();
    let acts = !method.is_safe();
    if acts && (path == "/api/daemon/start" || path == "/api/daemon/stop") && !can.power {
        return refuse("Power");
    }
    let drives = path == "/api/drive/ws"
        || (acts && (under(path, "/api/expressions/play") || path == "/api/expressions/stop"))
        || (acts && under(path, "/api/motors/set_mode"));
    if drives && !can.drive {
        return refuse("Drive");
    }
    // Driving needs the motors on; only grown-ups make the robot go limp.
    if acts && access.kid() && under(path, "/api/motors/set_mode") && path != "/api/motors/set_mode/enabled" {
        return refuse("Controls");
    }
    if under(path, "/api/audio/session") && !can.voice {
        return refuse("Voice");
    }
    if path == "/api/camera/stream" && !can.camera {
        return refuse("Camera");
    }
    if access.kid() && controls(method, path) {
        return refuse("Controls");
    }
    Ok(())
}

/// A profile as the admin sent it: names trimmed, apps deduplicated.
pub fn cleaned(mut profile: Profile) -> Profile {
    if let Some(apps) = &mut profile.apps {
        let mut seen = std::collections::HashSet::new();
        apps.retain(|a| !a.is_empty() && a.len() <= 128 && seen.insert(a.clone()));
        apps.truncate(256);
    }
    profile
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kid(can: Can, apps: &[&str]) -> Access {
        Access::Limited(Arc::new(Profile {
            name: "Mia".into(),
            kind: Kind::Kid,
            apps: Some(apps.iter().map(|a| a.to_string()).collect()),
            can,
        }))
    }

    #[test]
    fn full_access_passes_everything() {
        assert!(check(&Method::GET, "/api/logs/sources", &Access::Full).is_ok());
        assert!(check(&Method::POST, "/api/sim/enter", &Access::Full).is_ok());
    }

    #[test]
    fn system_is_the_admins_for_every_profile() {
        let grown = Access::Limited(Arc::new(Profile::open("Sam")));
        for path in [
            "/api/system/stats",
            "/api/system/stats-stream",
            "/api/system/platform",
            "/api/logs/sources",
            "/api/logs/stream",
            "/api/audio/levels-stream",
            "/api/sim/sounds-stream",
        ] {
            assert!(check(&Method::GET, path, &grown).is_err(), "{path}");
        }
        assert!(check(&Method::POST, "/api/sim/enter", &grown).is_err());
        assert!(check(&Method::POST, "/api/sim/exit", &grown).is_err());
        // What the other tabs need still reads.
        for path in ["/api/sim", "/api/daemon/status", "/api/apps/status", "/api/control", "/api/systemd"] {
            assert!(check(&Method::GET, path, &grown).is_ok(), "{path}");
        }
    }

    #[test]
    fn switches_refuse_their_routes() {
        let none = kid(Can::NOTHING, &[]);
        for (method, path) in [
            (Method::POST, "/api/daemon/start"),
            (Method::POST, "/api/daemon/stop"),
            (Method::GET, "/api/drive/ws"),
            (Method::POST, "/api/expressions/play/emotions/happy"),
            (Method::POST, "/api/expressions/stop"),
            (Method::POST, "/api/motors/set_mode/enabled"),
            (Method::POST, "/api/audio/session"),
            (Method::DELETE, "/api/audio/session/abc"),
            (Method::GET, "/api/camera/stream"),
            (Method::POST, "/api/volume/set"),
            (Method::POST, "/api/media/play_sound"),
        ] {
            assert!(check(&method, path, &none).is_err(), "{method} {path}");
        }
        // Reads the dashboard draws from stay open.
        for path in ["/api/daemon/status", "/api/audio", "/api/camera/availability", "/api/expressions/current"] {
            assert!(check(&Method::GET, path, &none).is_ok(), "{path}");
        }
        let all = kid(Can::default(), &[]);
        assert!(check(&Method::POST, "/api/daemon/start", &all).is_ok());
        assert!(check(&Method::GET, "/api/camera/stream", &all).is_ok());
        // Kids never get Controls; grown-ups do.
        assert!(check(&Method::POST, "/api/volume/set", &all).is_err());
        assert!(check(&Method::POST, "/api/motors/set_mode/enabled", &all).is_ok());
        assert!(check(&Method::POST, "/api/motors/set_mode/disabled", &all).is_err());
        let grown = Access::Limited(Arc::new(Profile::open("Sam")));
        assert!(check(&Method::POST, "/api/volume/set", &grown).is_ok());
    }

    #[test]
    fn an_unpicked_shared_device_can_only_look() {
        assert!(check(&Method::POST, "/api/control/take", &Access::Unpicked).is_err());
        assert!(check(&Method::POST, "/api/daemon/start", &Access::Unpicked).is_err());
        assert!(check(&Method::GET, "/api/daemon/status", &Access::Unpicked).is_ok());
        assert!(!Access::Unpicked.allows_app("anything"));
        assert!(check(&Method::POST, "/api/control/take", &kid(Can::NOTHING, &[])).is_ok());
    }

    #[test]
    fn app_lists_and_old_files() {
        let mia = kid(Can::default(), &["dance"]);
        assert!(mia.allows_app("dance"));
        assert!(!mia.allows_app("dj_reactor"));
        assert!(Access::Limited(Arc::new(Profile::open("Sam"))).allows_app("dj_reactor"));
        // Switches a file doesn't name are on.
        let p: Profile = serde_json::from_value(json!({"name": "Leo", "can": {"power": false}})).unwrap();
        assert_eq!(p.can, Can { power: false, ..Can::default() });
        assert_eq!((p.kind, p.apps), (Kind::Grown, None));
    }
}
