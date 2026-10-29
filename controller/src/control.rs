//! Control time (#221): in admin mode one device at a time drives the robot.
//!
//! The admin grants a person control time, a slot or with no end; grants
//! never overlap. A person with an active grant takes control from one of
//! their devices; anyone else can only ask the admin, one request at a time
//! and with a cooldown after a refusal or a cancel. The admin can always take
//! control, and can revoke a grant at any time.
//!
//! Control ends when the grant ends or is revoked, when the admin takes over,
//! or when the holder's device has been silent for [`AWAY_AFTER`]. A holder
//! that comes back within its grant gets control back, unless someone else
//! took it meanwhile. Ending control ends the holder's drive session (which
//! sends the robot home) and stops a playing expression; a running app keeps
//! running.
//!
//! Every action on the robot needs control in admin mode: any state-changing
//! request or WebSocket under `/api` or `/telemetry`, except the login and
//! control routes themselves and the admin-only routes (see [`guard`]).
//! Outside admin mode nothing here applies.
//!
//! Grants are kept in `control.json`; the holder, requests and cooldowns
//! live in memory and start empty.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::{Path as UrlPath, Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Extension, Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::watch;

use crate::auth::{self, AuthContext, Role};
use crate::error::{ApiError, ApiResult};
use crate::expressions::ExpressionPlayer;
use crate::state::Shared;

pub const CONTROL_FILE: &str = "control.json";

/// A holder silent this long (no request, no open drive session) loses
/// control; the dashboard polls every 2 s.
pub const AWAY_AFTER: u64 = 10;
/// An admin, who needs no grant, gets control back on return only within
/// this long; a grantee within its grant.
pub const ADMIN_AWAY_KEEP: u64 = 300;
/// After the admin refuses a request, the person waits this long to ask again.
pub const REFUSED_COOLDOWN: u64 = 600;
/// After cancelling a request: long enough that cancel and ask again isn't a
/// way to keep the admin's attention.
pub const CANCELLED_COOLDOWN: u64 = 120;
/// Longest slot that can be asked for or granted (a week); no end is separate.
const MAX_MINUTES: u64 = 7 * 24 * 60;
const MAX_NOTE: usize = 200;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Grant {
    pub id: String,
    pub person: String,
    /// Unix seconds.
    pub from: u64,
    /// None: no end.
    pub until: Option<u64>,
    /// Who granted it (a device id, or "the local key").
    pub by: String,
}

impl Grant {
    fn active(&self, now: u64) -> bool {
        self.from <= now && self.until.is_none_or(|u| now < u)
    }

    fn overlaps(&self, from: u64, until: Option<u64>) -> bool {
        self.from < until.unwrap_or(u64::MAX) && from < self.until.unwrap_or(u64::MAX)
    }
}

#[derive(Clone, Debug, PartialEq)]
struct Holder {
    device: String,
    person: String,
    admin: bool,
    since: u64,
}

#[derive(Clone, Debug, Serialize)]
struct Ask {
    person: String,
    device: String,
    at: u64,
    /// None: no end asked for.
    minutes: Option<u64>,
    note: String,
}

#[derive(Default, Serialize, Deserialize)]
struct ControlFile {
    grants: Vec<Grant>,
}

#[derive(Default)]
struct Inner {
    grants: Vec<Grant>,
    holder: Option<Holder>,
    /// The last holder lost to silence, restored if it comes back in time.
    away: Option<Holder>,
    requests: Vec<Ask>,
    /// Person -> unix seconds until they may ask again.
    cooldown: HashMap<String, u64>,
    /// Device -> last request (unix seconds).
    seen: HashMap<String, u64>,
}

impl Inner {
    fn active_grant(&self, person: &str, now: u64) -> Option<&Grant> {
        self.grants.iter().find(|g| g.person == person && g.active(now))
    }

    /// Apply the clock: drop ended grants and cooldowns, end the holder's
    /// control when its grant ended or it went silent. Returns whether the
    /// holder changed.
    fn refresh(&mut self, now: u64) -> bool {
        self.grants.retain(|g| g.until.is_none_or(|u| now < u));
        self.cooldown.retain(|_, until| now < *until);
        let entitled = |h: &Holder, inner: &Inner| h.admin || inner.active_grant(&h.person, now).is_some();
        // `since` of an away holder is when it went away.
        let stale = |h: &Holder| h.admin && now.saturating_sub(h.since) >= ADMIN_AWAY_KEEP;
        if self.away.as_ref().is_some_and(|h| !entitled(h, self) || stale(h)) {
            self.away = None;
        }
        let Some(holder) = self.holder.clone() else { return false };
        if !entitled(&holder, self) {
            tracing::info!("control: {} ({}) lost control: the grant ended", holder.person, holder.device);
            self.holder = None;
            return true;
        }
        let seen = self.seen.get(&holder.device).copied().unwrap_or(holder.since);
        if now.saturating_sub(seen) >= AWAY_AFTER {
            tracing::info!("control: {} ({}) lost control: device silent", holder.person, holder.device);
            self.holder = None;
            self.away = Some(Holder { since: now, ..holder });
            return true;
        }
        false
    }
}

pub struct Control {
    path: PathBuf,
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
    pub fn new(state_dir: &Path, player: Arc<ExpressionPlayer>) -> Self {
        let path = state_dir.join(CONTROL_FILE);
        let grants = match std::fs::read(&path) {
            Ok(bytes) => serde_json::from_slice::<ControlFile>(&bytes).map(|f| f.grants).unwrap_or_else(|error| {
                tracing::error!("control: {} is unreadable ({error}); starting with no grants", path.display());
                Vec::new()
            }),
            Err(_) => Vec::new(),
        };
        Self {
            path,
            inner: Mutex::new(Inner { grants, ..Inner::default() }),
            changed: watch::channel(0).0,
            player,
            skew: AtomicI64::new(0),
        }
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

    fn save(&self, grants: &[Grant]) -> std::io::Result<()> {
        let body = serde_json::to_vec_pretty(&json!({"grants": grants})).expect("grants serialize");
        auth::write_private(&self.path, &body)
    }

    /// Record a request from `device`; a returning holder gets control back.
    pub fn touch(&self, device: &str) {
        self.with(|inner, now| {
            inner.seen.insert(device.to_string(), now);
            if inner.holder.is_none() && inner.away.as_ref().is_some_and(|h| h.device == device) {
                let holder = inner.away.take().expect("checked");
                tracing::info!("control: {} ({}) is back in control", holder.person, holder.device);
                inner.holder = Some(Holder { since: now, ..holder });
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
    /// least every second, so a grant ending, the device going silent or
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
    }
    next.run(request).await
}

/// The app UI proxy's rule: in admin mode, watchers can load an app's pages
/// but only the holder (or the local key) acts through it.
pub fn app_proxy_allows(state: &Shared, method: &axum::http::Method, upgrade: bool, device: Option<&str>, local: bool) -> Result<(), String> {
    if !state.auth.admin_mode() || local {
        return Ok(());
    }
    if let Some(device) = device {
        state.control.touch(device);
    }
    if (!method.is_safe() || upgrade) && !device.is_some_and(|d| state.control.holds(d)) {
        return Err(not_holding(&state.control).detail);
    }
    Ok(())
}

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/control", get(view))
        .route("/api/control/take", post(take))
        .route("/api/control/release", post(release))
        .route("/api/control/request", post(ask).delete(cancel))
        .route("/api/control/requests/{person}/refuse", post(refuse))
        .route("/api/control/grants", post(grant))
        .route("/api/control/grants/{id}", delete(revoke))
}

fn is_admin(ctx: &AuthContext) -> bool {
    ctx.local || ctx.role == Some(Role::Admin)
}

fn by(ctx: &AuthContext) -> String {
    ctx.device.clone().unwrap_or_else(|| "the local key".to_string())
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
            let until = if h.admin { None } else { inner.active_grant(&h.person, now).and_then(|g| g.until) };
            json!({
                "person": h.person,
                "device": names.get(&h.device).cloned().unwrap_or_default(),
                "admin": h.admin,
                "since": h.since,
                "until": until,
                "you": ctx.device.as_deref() == Some(h.device.as_str()),
            })
        });
        let person = ctx.person.clone().unwrap_or_default();
        let mine = |p: &str| ctx.person.as_deref() == Some(p);
        let grants: Vec<&Grant> = inner.grants.iter().filter(|g| admin || mine(&g.person)).collect();
        let requests: Vec<&Ask> = inner.requests.iter().filter(|r| admin || mine(&r.person)).collect();
        let held_by_other = inner.holder.as_ref().is_some_and(|h| h.person != person);
        let grant = inner.active_grant(&person, now);
        json!({
            "admin_mode": true,
            "now": now,
            "holder": holder,
            "you": {
                "admin": admin,
                "person": ctx.person,
                "grant": grant,
                "can_take": ctx.device.is_some() && (admin || (grant.is_some() && !held_by_other)),
                "cooldown_until": inner.cooldown.get(&person),
            },
            "grants": grants,
            "requests": requests,
        })
    }))
}

async fn take(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    let (device, person) = me(&ctx)?;
    let admin = ctx.role == Some(Role::Admin);
    state.control.with(|inner, now| {
        if let Some(holder) = &inner.holder {
            if holder.device == device {
                return Ok(());
            }
            if !admin && holder.person != person {
                return Err(ApiError::new(409, format!("{} is in control of the robot", holder.person)));
            }
        }
        if !admin && inner.active_grant(&person, now).is_none() {
            return Err(ApiError::new(403, "you have no control time now; ask the admin for some"));
        }
        let previous = inner.holder.replace(Holder { device: device.clone(), person: person.clone(), admin, since: now });
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

#[derive(Deserialize)]
struct AskBody {
    /// None: no end.
    minutes: Option<u64>,
    note: Option<String>,
}

fn minutes_ok(minutes: Option<u64>) -> ApiResult<()> {
    match minutes {
        Some(m) if m == 0 || m > MAX_MINUTES => Err(ApiError::new(400, format!("minutes is 1 to {MAX_MINUTES}, or none for no end"))),
        _ => Ok(()),
    }
}

async fn ask(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<AskBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let (device, person) = me(&ctx)?;
    minutes_ok(body.minutes)?;
    let note: String = body.note.unwrap_or_default().trim().chars().filter(|c| !c.is_control()).take(MAX_NOTE).collect();
    state.control.with(|inner, now| {
        if let Some(until) = inner.cooldown.get(&person) {
            return Err(ApiError::new(429, format!("you can ask again in {} min", (until - now).div_ceil(60))));
        }
        if inner.requests.iter().any(|r| r.person == person) {
            return Err(ApiError::new(409, "you already asked; the admin hasn't answered yet"));
        }
        if inner.active_grant(&person, now).is_some() {
            return Err(ApiError::new(409, "you already have control time; take control"));
        }
        let how_long = body.minutes.map_or_else(|| "no end".to_string(), |m| format!("{m} min"));
        tracing::info!("control: {person} ({device}) asked for control time ({how_long})");
        inner.requests.push(Ask { person, device, at: now, minutes: body.minutes, note });
        Ok(())
    })?;
    Ok(Json(json!({"status": "asked"})))
}

async fn cancel(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<StatusCode> {
    let (_, person) = me(&ctx)?;
    state.control.with(|inner, now| {
        let before = inner.requests.len();
        inner.requests.retain(|r| r.person != person);
        if inner.requests.len() != before {
            inner.cooldown.insert(person, now + CANCELLED_COOLDOWN);
        }
    });
    Ok(StatusCode::NO_CONTENT)
}

async fn refuse(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    UrlPath(person): UrlPath<String>,
) -> ApiResult<StatusCode> {
    if !is_admin(&ctx) {
        return Err(ApiError::new(403, "only an admin answers requests"));
    }
    state.control.with(|inner, now| {
        let before = inner.requests.len();
        inner.requests.retain(|r| r.person != person);
        if inner.requests.len() == before {
            return Err(ApiError::new(404, "no request from that person"));
        }
        tracing::info!("control: request from {person} refused by {}", by(&ctx));
        inner.cooldown.insert(person, now + REFUSED_COOLDOWN);
        Ok(())
    })?;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Deserialize)]
struct GrantBody {
    person: String,
    /// None: no end.
    minutes: Option<u64>,
    /// Unix seconds; now when absent.
    from: Option<u64>,
}

async fn grant(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<GrantBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    if !is_admin(&ctx) {
        return Err(ApiError::new(403, "only an admin grants control time"));
    }
    minutes_ok(body.minutes)?;
    let person = body.person.trim().to_string();
    if person.is_empty() || !state.auth.has_person(&person) {
        return Err(ApiError::new(400, "no paired device belongs to that person"));
    }
    let control = &state.control;
    let granted = control.with(|inner, now| {
        // A slot starts at most MAX_MINUTES ahead; minutes is bounded above.
        let from = body.from.unwrap_or(now).clamp(now, now + MAX_MINUTES * 60);
        let until = body.minutes.map(|m| from + m * 60);
        if let Some(other) = inner.grants.iter().find(|g| g.overlaps(from, until)) {
            return Err(ApiError::new(409, format!("that overlaps {}'s control time", other.person)));
        }
        let grant = Grant { id: auth::random_hex(6), person: person.clone(), from, until, by: by(&ctx) };
        let mut grants = inner.grants.clone();
        grants.push(grant.clone());
        control.save(&grants).map_err(|error| {
            tracing::error!("control: cannot save {CONTROL_FILE} ({error})");
            ApiError::new(500, "could not save the grant")
        })?;
        inner.grants = grants;
        inner.requests.retain(|r| r.person != person);
        let how_long = body.minutes.map_or_else(|| "no end".to_string(), |m| format!("{m} min"));
        tracing::info!("control: {} granted {person} control time ({how_long}, from {from})", by(&ctx));
        Ok(grant)
    })?;
    Ok(Json(json!(granted)))
}

async fn revoke(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    UrlPath(id): UrlPath<String>,
) -> ApiResult<StatusCode> {
    if !is_admin(&ctx) {
        return Err(ApiError::new(403, "only an admin revokes control time"));
    }
    let control = &state.control;
    control.with(|inner, _| {
        let Some(index) = inner.grants.iter().position(|g| g.id == id) else {
            return Err(ApiError::new(404, "no such grant"));
        };
        let mut grants = inner.grants.clone();
        let removed = grants.remove(index);
        control.save(&grants).map_err(|error| {
            tracing::error!("control: cannot save {CONTROL_FILE} ({error})");
            ApiError::new(500, "could not save the grants")
        })?;
        inner.grants = grants;
        // Takes effect now: the holder loses control on this refresh.
        inner.refresh(control.now());
        tracing::info!("control: {}'s grant {id} revoked by {}", removed.person, by(&ctx));
        Ok(())
    })?;
    Ok(StatusCode::NO_CONTENT)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grant(person: &str, from: u64, until: Option<u64>) -> Grant {
        Grant { id: "g".into(), person: person.into(), from, until, by: "admin".into() }
    }

    #[test]
    fn grants_overlap_by_their_intervals() {
        let g = grant("sam", 100, Some(200));
        assert!(g.overlaps(150, Some(250)));
        assert!(g.overlaps(50, Some(150)));
        assert!(g.overlaps(0, None));
        assert!(!g.overlaps(200, Some(300)), "back to back");
        assert!(!g.overlaps(0, Some(100)));
        let open = grant("sam", 100, None);
        assert!(open.overlaps(10_000, Some(10_001)));
        assert!(!open.overlaps(0, Some(100)));
    }

    #[test]
    fn a_silent_holder_steps_away_and_an_ended_grant_ends_control() {
        let mut inner = Inner { grants: vec![grant("sam", 0, Some(1000))], ..Inner::default() };
        inner.holder = Some(Holder { device: "d1".into(), person: "sam".into(), admin: false, since: 100 });
        inner.seen.insert("d1".into(), 100);
        assert!(!inner.refresh(105));
        assert!(inner.refresh(100 + AWAY_AFTER));
        assert!(inner.holder.is_none() && inner.away.is_some());
        // Past the grant, there's nothing to come back to.
        inner.refresh(1000);
        assert!(inner.away.is_none() && inner.grants.is_empty());
    }
}
