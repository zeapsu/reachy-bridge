//! Control (#221, simplified in #245): in admin mode one device at a time
//! drives the robot.
//!
//! Any paired browser takes control while nobody else holds it; the admin
//! can always take it over. Who may drive at all is decided when a browser
//! is approved, and revoking the browser takes that back. There are no
//! grants or requests to drive any more: the single holder is what keeps
//! two people from driving at once.
//!
//! Control ends when its holder releases it, when the admin takes over, or
//! when the holder's device has been silent for [`AWAY_AFTER`]. A holder
//! that comes back within [`AWAY_KEEP`] gets control back, unless someone
//! else took it meanwhile. A holder whose device stays but does nothing
//! with the robot for [`IDLE_AFTER`] loses control for good, so an
//! abandoned open tab doesn't keep it. Closing the dashboard tab releases
//! control right away. Ending control ends the holder's drive session
//! (which sends the robot home) and stops a playing expression; a running
//! app keeps running.
//!
//! Every action on the robot needs control in admin mode: any state-changing
//! request or WebSocket under `/api` or `/telemetry`, except the login and
//! control routes themselves and the admin-only routes (see [`guard`]).
//! Outside admin mode nothing here applies.
//!
//! The holder lives in memory and starts empty.

use std::collections::HashMap;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::{Query, Request, State};
use axum::http::header;
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::watch;

use crate::auth::{self, AuthContext, Role};
use crate::error::{ApiError, ApiResult};
use crate::expressions::ExpressionPlayer;
use crate::state::Shared;

/// A holder silent this long (no request, no open drive or voice session)
/// loses control; until then it keeps control through a network blip or a
/// locked phone, and nobody but an admin can take it. The dashboard polls
/// every 2 s; a drive session ends on its own much sooner (see
/// `routes::drive`).
pub const AWAY_AFTER: u64 = 60;
/// A holder that went silent gets control back on return only within this
/// long.
pub const AWAY_KEEP: u64 = 300;
/// A holder that does nothing with the robot this long (no robot action,
/// no open drive or voice session) loses control, with no claim to come
/// back: its next action takes control again while nobody else has it.
pub const IDLE_AFTER: u64 = 600;

#[derive(Clone, Debug, PartialEq)]
struct Holder {
    device: String,
    person: String,
    admin: bool,
    since: u64,
    /// The last robot action (unix seconds).
    active: u64,
}

#[derive(Default)]
struct Inner {
    holder: Option<Holder>,
    /// The last holder lost to silence, restored if it comes back in time.
    away: Option<Holder>,
    /// Device -> last request (unix seconds).
    seen: HashMap<String, u64>,
}

impl Inner {
    /// See [`Control::forget`].
    fn forget(&mut self, device: &str) {
        if self.holder.as_ref().is_some_and(|h| h.device == device) {
            tracing::info!("control: {device} lost control: revoked or its role changed");
            self.holder = None;
        }
        if self.away.as_ref().is_some_and(|h| h.device == device) {
            self.away = None;
        }
        self.seen.remove(device);
    }

    /// Apply the clock: end the holder's control when it went silent, and
    /// forget an away holder gone too long. Returns whether the holder
    /// changed.
    fn refresh(&mut self, now: u64) -> bool {
        // `since` of an away holder is when it went away.
        let stale = |h: &Holder| now.saturating_sub(h.since) >= AWAY_KEEP;
        if self.away.as_ref().is_some_and(stale) {
            self.away = None;
        }
        let Some(holder) = self.holder.clone() else { return false };
        let seen = self.seen.get(&holder.device).copied().unwrap_or(holder.since);
        if now.saturating_sub(seen) >= AWAY_AFTER {
            tracing::info!("control: {} ({}) lost control: device silent", holder.person, holder.device);
            self.holder = None;
            // Away since it was last seen, which may be long ago when nothing
            // ran a refresh meanwhile: a holder gone that long isn't restored.
            let away = Holder { since: seen, ..holder };
            self.away = (!stale(&away)).then_some(away);
            return true;
        }
        // After the silence check: a holder cut off by the network steps away
        // (and comes back) instead of counting as idle.
        if now.saturating_sub(holder.active) >= IDLE_AFTER {
            tracing::info!("control: {} ({}) lost control: idle", holder.person, holder.device);
            self.holder = None;
            return true;
        }
        false
    }
}

pub struct Control {
    inner: Mutex<Inner>,
    /// Bumped whenever the holder changes; [`Control::lost`] watches it.
    changed: watch::Sender<u64>,
    player: Arc<ExpressionPlayer>,
    /// Seconds added to the clock (tests).
    skew: AtomicI64,
}

fn unix_now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs())
}

impl Control {
    pub fn new(player: Arc<ExpressionPlayer>) -> Self {
        Self { inner: Mutex::new(Inner::default()), changed: watch::channel(0).0, player, skew: AtomicI64::new(0) }
    }

    pub fn now(&self) -> u64 {
        unix_now().saturating_add_signed(self.skew.load(Ordering::SeqCst))
    }

    /// Move the clock (tests).
    pub fn advance(&self, seconds: i64) {
        self.skew.fetch_add(seconds, Ordering::SeqCst);
    }

    /// Run `f` on the refreshed state; if the holder changed (here or by
    /// `f`), wake the sessions watching and stop a playing expression.
    fn with<T>(&self, f: impl FnOnce(&mut Inner, u64) -> T) -> T {
        let now = self.now();
        let mut inner = self.inner.lock().unwrap();
        let before = inner.holder.clone();
        inner.refresh(now);
        let result = f(&mut inner, now);
        let changed = inner.holder.as_ref().map(|h| &h.device) != before.as_ref().map(|h| &h.device);
        drop(inner);
        if changed {
            self.changed.send_modify(|n| *n += 1);
            if before.is_some() {
                // Manual motion stops with control; the drive session ends on
                // its own (it watches `lost`).
                let player = self.player.clone();
                if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                    runtime.spawn(async move { player.stop().await });
                }
            }
        }
        result
    }

    /// Record a request from `device`; a returning holder gets control back.
    pub fn touch(&self, device: &str) {
        self.with(|inner, now| {
            inner.seen.insert(device.to_string(), now);
            if inner.holder.is_none() && inner.away.as_ref().is_some_and(|h| h.device == device) {
                let holder = inner.away.take().expect("checked");
                tracing::info!("control: {} ({}) is back in control", holder.person, holder.device);
                // Coming back isn't a robot action: the idle clock runs on
                // from the last one, so a tab that keeps going quiet (hidden,
                // a locked phone) doesn't keep control for good.
                inner.holder = Some(Holder { since: now, ..holder });
            }
        });
    }

    /// A profile was renamed (#297): its holder, under the old name, lets go.
    pub fn forget_person(&self, person: &str) {
        self.with(|inner, _| {
            if inner.holder.as_ref().is_some_and(|h| h.person == person) {
                tracing::info!("control: {person} lost control: profile renamed");
                inner.holder = None;
            }
            if inner.away.as_ref().is_some_and(|h| h.person == person) {
                inner.away = None;
            }
        });
    }

    /// A revoked device, or one whose role changed: it loses control, and
    /// with it any claim to come back.
    pub fn forget(&self, device: &str) {
        self.with(|inner, _| inner.forget(device));
    }

    /// A role change or revoke (#341): runs `f` under the control lock with
    /// the holder's device id, then the device it returns loses control (as
    /// [`Control::forget`]) before the lock is released. So the change checks
    /// who holds control, and is made, with nobody taking control in between.
    /// `f` may lock the device list and the picks: the order `take` uses
    /// (control, then devices). It must not call back into `Control`. It
    /// holds up every request that checks control while it runs, so keep it
    /// to the change itself (a small file write is the most it does). On
    /// `Err` nothing is forgotten and nothing is rolled back: `f` leaves the
    /// state as it found it.
    pub fn change_under<T, E>(&self, f: impl FnOnce(Option<&str>) -> Result<(T, Option<String>), E>) -> Result<T, E> {
        self.with(|inner, _| {
            let holder = inner.holder.as_ref().map(|h| h.device.clone());
            let (result, forget) = f(holder.as_deref())?;
            if let Some(device) = forget {
                inner.forget(&device);
            }
            Ok(result)
        })
    }

    /// Record a robot action (or an open drive or voice session) from
    /// `device`, if it holds control: it isn't idle.
    pub fn active(&self, device: &str) {
        self.with(|inner, now| {
            if let Some(holder) = inner.holder.as_mut().filter(|h| h.device == device) {
                holder.active = now;
            }
        });
    }

    pub fn holds(&self, device: &str) -> bool {
        self.with(|inner, _| inner.holder.as_ref().is_some_and(|h| h.device == device))
    }

    /// Who holds control, for a refusal message.
    fn holder_person(&self) -> Option<String> {
        self.with(|inner, _| inner.holder.as_ref().map(|h| h.person.clone()))
    }

    /// Resolves once `device` doesn't hold control in admin mode. Checks at
    /// least every second, so the device going silent or
    /// admin mode starting is noticed without an event.
    pub async fn lost(&self, auth: &crate::auth::Auth, device: String) {
        let mut changed = self.changed.subscribe();
        while !auth.admin_mode() || self.holds(&device) {
            tokio::select! {
                _ = changed.changed() => {}
                () = tokio::time::sleep(Duration::from_secs(1)) => {}
            }
        }
    }
}

/// The refusal for a robot action from a device that doesn't hold control.
fn not_holding(control: &Control) -> ApiError {
    match control.holder_person() {
        Some(person) => ApiError::new(403, format!("{person} is in control of the robot")),
        None => ApiError::new(403, "take control of the robot first"),
    }
}

/// Needs control in admin mode: any state change or WebSocket on the API,
/// except logging in, control itself, and the admin-only routes (which the
/// auth guard already limits to admins).
fn needs_control(request: &Request) -> bool {
    let path = request.uri().path();
    (!request.method().is_safe() || request.headers().contains_key(header::UPGRADE))
        && auth::is_api(path)
        && !path.starts_with("/api/auth/")
        && path != "/api/control"
        && !path.starts_with("/api/control/")
        // A dashboard preference, not a robot action.
        && path != "/api/apps/favorites"
        && !auth::admin_only(request.method(), path)
}

/// Enforces control (see the module docs); runs after `auth::guard`.
pub async fn guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    if !state.auth.admin_mode() {
        return next.run(request).await;
    }
    let ctx = request.extensions().get::<AuthContext>().cloned();
    let device = ctx.as_ref().and_then(|c| c.device.clone());
    if let Some(device) = &device {
        state.control.touch(device);
    }
    if needs_control(&request) && !ctx.as_ref().is_some_and(|c| c.local) {
        let holds = device.as_deref().is_some_and(|d| state.control.holds(d));
        if !holds {
            return not_holding(&state.control).into_response();
        }
        if let Some(device) = &device {
            state.control.active(device);
        }
    }
    next.run(request).await
}

/// The app UI proxy's rule: in admin mode, watchers can load an app's pages
/// but only the holder (or the local key) acts through it.
pub fn app_proxy_allows(
    state: &Shared,
    method: &axum::http::Method,
    upgrade: bool,
    device: Option<&str>,
    local: bool,
) -> Result<(), String> {
    if !state.auth.admin_mode() || local {
        return Ok(());
    }
    if let Some(device) = device {
        state.control.touch(device);
    }
    if (!method.is_safe() || upgrade) && !device.is_some_and(|d| state.control.holds(d)) {
        return Err(not_holding(&state.control).detail);
    }
    if let (Some(device), true) = (device, !method.is_safe() || upgrade) {
        state.control.active(device);
    }
    Ok(())
}

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/control", get(view))
        .route("/api/control/take", post(take))
        .route("/api/control/release", post(release))
}

fn is_admin(ctx: &AuthContext) -> bool {
    ctx.local || ctx.role == Some(Role::Admin)
}

/// A logged-in device: its id and person.
fn me(ctx: &AuthContext) -> ApiResult<(String, String)> {
    match (&ctx.device, &ctx.person) {
        (Some(device), Some(person)) => Ok((device.clone(), person.clone())),
        _ => Err(ApiError::new(403, "control is per device: log in from a paired browser")),
    }
}

async fn view(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> Json<Value> {
    if !state.auth.admin_mode() {
        return Json(json!({"admin_mode": false}));
    }
    let admin = is_admin(&ctx);
    let names = state.auth.device_names();
    Json(state.control.with(|inner, now| {
        let holder = inner.holder.as_ref().map(|h| {
            json!({
                "person": h.person,
                "device": names.get(&h.device).cloned().unwrap_or_default(),
                "admin": h.admin,
                "since": h.since,
                "you": ctx.device.as_deref() == Some(h.device.as_str()),
            })
        });
        let person = ctx.person.clone().unwrap_or_default();
        // As in take: another person, or an admin when this browser isn't one.
        let held_by_other =
            inner.holder.as_ref().is_some_and(|h| h.person != person || (h.admin && ctx.role != Some(Role::Admin)));
        json!({
            "admin_mode": true,
            "now": now,
            "holder": holder,
            "you": {
                "admin": admin,
                "person": ctx.person,
                "can_take": ctx.device.is_some() && (admin || !held_by_other),
            },
        })
    }))
}

#[derive(Deserialize)]
struct TakeQuery {
    /// Only when nobody else is in control: the dashboard taking control
    /// for a robot action (#221), which must never take it from another
    /// person, admin or not, on a stale view of who has it. The same
    /// person's other device may: that moves their control, as a plain
    /// take does. A holder who stepped away
    /// isn't in control: like a plain take, this one ends their claim to
    /// come back, as the dashboard shows the robot free meanwhile.
    #[serde(default)]
    if_free: bool,
}

async fn take(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    Query(query): Query<TakeQuery>,
) -> ApiResult<Json<Value>> {
    let (device, person) = me(&ctx)?;
    let admin = ctx.role == Some(Role::Admin);
    // Nothing needs control before an admin exists; a holder recorded then
    // would outlive the logout that admin mode starts with.
    if !state.auth.admin_mode() {
        return Err(ApiError::new(409, "control applies once an admin exists"));
    }
    state.control.with(|inner, now| {
        // Under the control lock, so a revoke (which forgets the device under
        // the same lock, after removing it) can't be undone by a take that
        // passed the login check just before it.
        let Some(role) = state.auth.role_of(&device) else {
            return Err(ApiError::new(401, "this browser was logged out"));
        };
        // The role the login check read may be stale: a role change (made
        // under this lock) lands before this take or after it (#341). Whom
        // it may take control from goes by the lesser of the two roles; the
        // holder is recorded with the role it has now, so a promotion in
        // between can't leave an admin held to a member's rules.
        let now_admin = role == Role::Admin;
        let admin = admin && now_admin;
        if let Some(holder) = &inner.holder {
            if holder.device == device {
                return Ok(());
            }
            // Another device of the same person may move control over, but
            // only an admin takes it from an admin.
            let other = holder.person != person || (holder.admin && !admin);
            if (query.if_free || !admin) && other {
                return Err(ApiError::new(409, format!("{} is in control of the robot", holder.person)));
            }
        }
        let previous = inner.holder.replace(Holder {
            device: device.clone(),
            person: person.clone(),
            admin: now_admin,
            since: now,
            active: now,
        });
        inner.away = None;
        inner.seen.insert(device.clone(), now);
        match previous {
            Some(p) => tracing::warn!("control: {person} ({device}) took control from {} ({})", p.person, p.device),
            None => tracing::info!("control: {person} ({device}) took control"),
        }
        Ok(())
    })?;
    Ok(Json(json!({"status": "in_control"})))
}

async fn release(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    let (device, person) = me(&ctx)?;
    state.control.with(|inner, _| {
        if inner.holder.as_ref().is_some_and(|h| h.device == device) {
            inner.holder = None;
            tracing::info!("control: {person} ({device}) released control");
        }
        if inner.away.as_ref().is_some_and(|h| h.device == device) {
            inner.away = None;
        }
    });
    Ok(Json(json!({"status": "released"})))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_silent_holder_steps_away_and_is_forgotten_after_a_while() {
        let holder = Holder { device: "d1".into(), person: "sam".into(), admin: false, since: 100, active: 100 };
        let mut inner = Inner { holder: Some(holder), ..Inner::default() };
        inner.seen.insert("d1".into(), 100);
        assert!(!inner.refresh(105));
        assert!(inner.refresh(100 + AWAY_AFTER));
        assert!(inner.holder.is_none() && inner.away.is_some());
        // Gone too long, there's nothing to come back to.
        inner.refresh(100 + AWAY_KEEP);
        assert!(inner.away.is_none());
    }

    #[test]
    fn an_idle_holder_loses_control_with_no_claim_to_come_back() {
        let holder = Holder { device: "d1".into(), person: "sam".into(), admin: false, since: 100, active: 100 };
        let mut inner = Inner { holder: Some(holder), ..Inner::default() };
        // Present the whole time (the dashboard polls), but doing nothing.
        for t in (100..100 + IDLE_AFTER).step_by(2) {
            inner.seen.insert("d1".into(), t);
            assert!(!inner.refresh(t), "still in control at {t}");
        }
        inner.seen.insert("d1".into(), 100 + IDLE_AFTER);
        assert!(inner.refresh(100 + IDLE_AFTER));
        assert!(inner.holder.is_none() && inner.away.is_none());
    }

    #[test]
    fn an_action_keeps_the_holder_in_control() {
        let holder = Holder {
            device: "d1".into(),
            person: "sam".into(),
            admin: false,
            since: 100,
            active: 100 + IDLE_AFTER - 1,
        };
        let mut inner = Inner { holder: Some(holder), ..Inner::default() };
        inner.seen.insert("d1".into(), 100 + IDLE_AFTER);
        assert!(!inner.refresh(100 + IDLE_AFTER));
        assert!(inner.holder.is_some());
    }

    #[test]
    fn coming_back_does_not_restart_the_idle_clock() {
        let daemon = crate::daemon::Daemon::new("http://127.0.0.1:9", Duration::from_secs(1));
        let control = Control::new(ExpressionPlayer::new(daemon, Duration::from_secs(1)));
        let start = control.now();
        control.with(|inner, now| {
            inner.holder =
                Some(Holder { device: "d1".into(), person: "sam".into(), admin: false, since: now, active: now });
        });
        control.touch("d1");
        // Hidden for a while: the device goes silent and steps away, then comes back.
        control.advance(AWAY_AFTER as i64 + 1);
        assert!(!control.holds("d2"));
        control.touch("d1");
        assert!(control.holds("d1"), "back in control after {}", control.now() - start);
        // Present from then on, but doing nothing: it loses control on the
        // original clock, not 10 minutes after coming back.
        let mut t = control.now() - start;
        while t < IDLE_AFTER {
            control.advance(2);
            t += 2;
            control.touch("d1");
        }
        assert!(!control.holds("d1"), "still in control {t} s after the last action");
    }

    #[test]
    fn a_holder_cut_off_while_idle_steps_away_rather_than_losing_control() {
        let holder = Holder { device: "d1".into(), person: "sam".into(), admin: false, since: 100, active: 100 };
        let mut inner = Inner { holder: Some(holder), ..Inner::default() };
        // Polling until 100 + 500, then the network drops; nothing refreshes until it's back.
        inner.seen.insert("d1".into(), 600);
        assert!(inner.refresh(100 + IDLE_AFTER));
        assert!(inner.holder.is_none() && inner.away.is_some());
    }
}
