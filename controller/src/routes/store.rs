//! The app catalog (#126): browse Hugging Face Spaces apps, install, update
//! and remove them through the daemon's app manager, and the Hugging Face
//! login that private Spaces need.
//!
//! Only these daemon routes are reachable from here: `list-available`,
//! `install`, `install-private-space`, `job-status`, `remove`, `update`,
//! `check-updates` and `hf-auth/{status,save-token,token}`. The browser never
//! sends an app's metadata: it names a Space by id, and the controller installs
//! the entry the daemon itself listed, so the download URL and the `private`
//! flag come from Hugging Face, not from the page.
//!
//! The Hugging Face token is write-only. It is forwarded once to the daemon,
//! which validates and stores it; the controller never logs, stores or
//! returns it, and job logs are scrubbed of anything token-shaped.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::{Path as UrlPath, Query, State};
use axum::routing::{get, post, put};
use axum::{Extension, Json, Router};
use reqwest::Method;
use serde_json::{Value, json};
use tokio::sync::Mutex;

use crate::auth::AuthContext;
use crate::daemon::{DaemonError, safe_segment};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

/// The daemon lists Spaces from the Hugging Face API; keep a listing this long.
const CATALOG_TTL: Duration = Duration::from_secs(300);
/// Listing every Space (up to 500, with full metadata) can take a while.
const CATALOG_TIMEOUT: Duration = Duration::from_secs(30);
/// Validating a token calls Hugging Face.
const HF_TIMEOUT: Duration = Duration::from_secs(15);
/// The daemon asks Hugging Face who's logged in; keep the answer this long.
const HF_STATUS_TTL: Duration = Duration::from_secs(60);
/// Job log lines sent to the page.
const JOB_LOG_LINES: usize = 40;
const MAX_TOKEN_LEN: usize = 256;

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/apps/catalog", get(catalog))
        .route("/api/apps/install", post(install))
        .route("/api/apps/install-private", post(install_private))
        .route("/api/apps/jobs/{id}", get(job))
        .route("/api/apps/remove/{name}", post(remove))
        .route("/api/apps/updates", get(updates))
        .route("/api/apps/update/{name}", post(update))
        .route("/api/hf/status", get(hf_status))
        .route("/api/hf/token", put(hf_save).delete(hf_delete))
}

/// Which listing: Pollen's curated selection, or every Reachy Mini Space.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Source {
    Curated,
    All,
}

impl Source {
    fn daemon_path(self) -> &'static str {
        match self {
            Source::Curated => "apps/list-available/dashboard_selection",
            Source::All => "apps/list-available/hf_space",
        }
    }
}

/// The daemon's listings, kept so an install can look its Space up, and the
/// Hugging Face login.
#[derive(Default)]
pub struct Catalog {
    listings: Mutex<HashMap<Source, (Instant, Vec<Value>)>>,
    hf: Mutex<Option<(Instant, Value)>>,
    /// Bumped by `forget`, so an answer fetched across a login change isn't kept.
    generation: AtomicU64,
}

impl Catalog {
    async fn listing(&self, state: &Shared, source: Source) -> ApiResult<Vec<Value>> {
        if let Some((at, apps)) = self.listings.lock().await.get(&source)
            && at.elapsed() < CATALOG_TTL
        {
            return Ok(apps.clone());
        }
        // Not under the lock: listing every Space can take most of the timeout.
        let generation = self.generation.load(Ordering::Acquire);
        let payload = state.daemon.call(Method::GET, source.daemon_path(), &[], None, Some(CATALOG_TIMEOUT)).await?;
        let apps = payload.as_array().cloned().unwrap_or_default();
        // The daemon answers an empty list when Hugging Face is unreachable.
        if apps.is_empty() {
            return Err(ApiError::new(502, "Hugging Face didn't list any apps. Try again in a moment."));
        }
        let mut listings = self.listings.lock().await;
        if self.generation.load(Ordering::Acquire) == generation {
            listings.insert(source, (Instant::now(), apps.clone()));
        }
        Ok(apps)
    }

    /// A login or logout changes which private Spaces the full listing has.
    async fn forget(&self) {
        self.generation.fetch_add(1, Ordering::AcqRel);
        self.listings.lock().await.clear();
        *self.hf.lock().await = None;
    }
}

/// `owner/name`, each part a Hugging Face repo name.
pub fn space_id(id: &str) -> bool {
    let mut parts = id.split('/');
    let (Some(owner), Some(name), None) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    [owner, name].iter().all(|p| p.len() <= 96 && safe_segment(p))
}

fn entry_id(entry: &Value) -> Option<&str> {
    entry.get("extra")?.get("id")?.as_str()
}

/// Installed apps by name and by Space id. An installed app is named after
/// its entry point, which needn't match the Space's name.
struct Installed(Vec<Value>);

impl Installed {
    fn has(&self, entry: &Value) -> bool {
        let name = entry.get("name").and_then(Value::as_str);
        let id = entry_id(entry);
        self.0.iter().any(|app| {
            let app_id = entry_id(app);
            (app_id.is_some() && app_id == id) || (name.is_some() && app.get("name").and_then(Value::as_str) == name)
        })
    }
}

fn card(entry: &Value, installed: &Installed) -> Option<Value> {
    let id = entry_id(entry)?;
    let name = entry.get("name").and_then(Value::as_str)?;
    let extra = &entry["extra"];
    let title = extra
        .get("cardData")
        .and_then(|c| c.get("title"))
        .and_then(Value::as_str)
        .filter(|t| !t.is_empty())
        .map_or_else(|| crate::apps::display_name(name), str::to_string);
    Some(json!({
        "id": id,
        "name": name,
        "title": title,
        "description": entry.get("description").and_then(Value::as_str).unwrap_or(""),
        "likes": extra.get("likes").and_then(Value::as_u64).unwrap_or(0),
        "author": extra.get("author").and_then(Value::as_str).unwrap_or(""),
        "updated": extra.get("lastModified").and_then(Value::as_str),
        "private": extra.get("private").and_then(Value::as_bool).unwrap_or(false),
        "installed": installed.has(entry),
    }))
}

#[derive(serde::Deserialize)]
struct CatalogQuery {
    #[serde(default)]
    all: bool,
}

async fn installed_names(state: &Shared) -> ApiResult<Vec<String>> {
    state.apps.installed_names().await.map_err(|_| ApiError::unavailable())
}

async fn installed(state: &Shared) -> ApiResult<Installed> {
    state.apps.installed().await.map(Installed).map_err(|_| ApiError::unavailable())
}

async fn catalog(State(state): State<Shared>, Query(query): Query<CatalogQuery>) -> ApiResult<Json<Value>> {
    let source = if query.all { Source::All } else { Source::Curated };
    let listing = state.catalog.listing(&state, source).await?;
    let installed = installed(&state).await?;
    let apps: Vec<Value> = listing.iter().filter_map(|e| card(e, &installed)).collect();
    Ok(Json(json!({"apps": apps})))
}

fn body_id(body: &Bytes) -> ApiResult<String> {
    let payload: Value = serde_json::from_slice(body).map_err(|_| ApiError::bad_body())?;
    let id = payload.get("id").and_then(Value::as_str).ok_or_else(ApiError::bad_body)?;
    if !space_id(id) {
        return Err(ApiError::new(422, "Not a Hugging Face Space id (owner/name)"));
    }
    Ok(id.to_string())
}

fn job_started(payload: Value) -> ApiResult<Json<Value>> {
    let job =
        payload.get("job_id").and_then(Value::as_str).filter(|j| safe_segment(j)).ok_or_else(ApiError::unavailable)?;
    Ok(Json(json!({"job": job})))
}

/// Install a listed Space: the entry the daemon listed, never one from the page.
async fn install(State(state): State<Shared>, body: Bytes) -> ApiResult<Json<Value>> {
    let id = body_id(&body)?;
    let mut found = None;
    for source in [Source::Curated, Source::All] {
        // Featured comes from Pollen, All from Hugging Face: one can fail
        // while the other answers, so only the last error counts.
        let listing = match state.catalog.listing(&state, source).await {
            Ok(listing) => listing,
            Err(_) if source == Source::Curated => continue,
            Err(err) => return Err(err),
        };
        found = listing.into_iter().find(|e| entry_id(e) == Some(id.as_str()));
        if found.is_some() {
            break;
        }
    }
    let entry = found.ok_or_else(|| ApiError::new(404, "That Space isn't in the app catalog"))?;
    if installed(&state).await?.has(&entry) {
        return Err(ApiError::new(409, "Already installed"));
    }
    let started = state.daemon.post_json("apps/install", &entry).await?;
    state.apps.forget_installed().await;
    job_started(started)
}

/// Install a private Space by id, with the stored Hugging Face login. Only
/// one the full listing shows as private to this login: the daemon would
/// otherwise fetch any Space id with the token.
async fn install_private(State(state): State<Shared>, body: Bytes) -> ApiResult<Json<Value>> {
    let id = body_id(&body)?;
    let listing = state.catalog.listing(&state, Source::All).await?;
    let entry = listing
        .iter()
        .find(|e| entry_id(e) == Some(id.as_str()) && e["extra"]["private"].as_bool() == Some(true))
        .ok_or_else(|| {
            ApiError::new(404, "No private Space with that id for this login. It needs the reachy_mini_python_app tag.")
        })?;
    if installed(&state).await?.has(entry) {
        return Err(ApiError::new(409, "Already installed"));
    }
    let started = state.daemon.post_json("apps/install-private-space", &json!({"space_id": id})).await?;
    state.apps.forget_installed().await;
    job_started(started)
}

/// Drop anything shaped like a Hugging Face token (`hf_…`, `hf_oauth_…`)
/// from a log line, but not identifiers such as `hf_hub_download`.
pub fn scrub(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut rest = line;
    while let Some(at) = rest.find("hf_") {
        out.push_str(&rest[..at]);
        let tail = &rest[at + 3..];
        let len = tail.find(|c: char| !c.is_ascii_alphanumeric() && c != '_').unwrap_or(tail.len());
        let run = &tail[..len];
        let mixed = run.bytes().any(|b| b.is_ascii_digit() || b.is_ascii_uppercase());
        if len >= 8 && (mixed || len >= 16) {
            out.push_str("hf_***");
        } else {
            out.push_str(&rest[at..at + 3 + len]);
        }
        rest = &tail[len..];
    }
    out.push_str(rest);
    out
}

/// A job's state and the tail of its log.
async fn job(State(state): State<Shared>, UrlPath(id): UrlPath<String>) -> ApiResult<Json<Value>> {
    if !safe_segment(&id) {
        return Err(ApiError::new(404, "Unknown job"));
    }
    let info = state.daemon.get(&format!("apps/job-status/{id}")).await.map_err(|e| match e {
        DaemonError::Refused { status: 404, .. } => ApiError::new(404, "Unknown job"),
        other => other.into(),
    })?;
    let status = info.get("status").and_then(Value::as_str).unwrap_or("unknown");
    if matches!(status, "done" | "failed") {
        state.apps.forget_installed().await;
    }
    let logs = info.get("logs").and_then(Value::as_array).cloned().unwrap_or_default();
    let tail: Vec<String> =
        logs.iter().skip(logs.len().saturating_sub(JOB_LOG_LINES)).filter_map(Value::as_str).map(scrub).collect();
    Ok(Json(json!({"status": status, "logs": tail})))
}

/// An installed app that isn't the one running (the daemon would remove a
/// running app's files from under it).
async fn idle_installed(state: &Shared, name: &str) -> ApiResult<()> {
    if !safe_segment(name) || !installed_names(state).await?.iter().any(|n| n == name) {
        return Err(ApiError::new(404, "Unknown app"));
    }
    let current = state.apps.current().await.map_err(|_| ApiError::unavailable())?;
    let running = current.as_ref().is_some_and(|c| {
        c.get("info").and_then(|i| i.get("name")).and_then(Value::as_str) == Some(name)
            && matches!(c.get("state").and_then(Value::as_str), Some("starting" | "running" | "stopping"))
    });
    if running {
        return Err(ApiError::new(409, "Stop the app first"));
    }
    Ok(())
}

async fn remove(State(state): State<Shared>, UrlPath(name): UrlPath<String>) -> ApiResult<Json<Value>> {
    idle_installed(&state, &name).await?;
    let started = state.daemon.post(&format!("apps/remove/{name}"), &[]).await?;
    state.apps.forget_installed().await;
    job_started(started)
}

async fn update(State(state): State<Shared>, UrlPath(name): UrlPath<String>) -> ApiResult<Json<Value>> {
    idle_installed(&state, &name).await?;
    let started = state.daemon.post(&format!("apps/update/{name}"), &[]).await?;
    job_started(started)
}

/// Installed apps with a newer version on Hugging Face (the daemon caches
/// this for 5 minutes).
async fn updates(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    let payload = state.daemon.call(Method::GET, "apps/check-updates", &[], None, Some(CATALOG_TIMEOUT)).await?;
    let names: Vec<&str> = payload
        .get("apps_with_updates")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|u| u.get("app_name").and_then(Value::as_str)).collect())
        .unwrap_or_default();
    Ok(Json(json!({"updates": names})))
}

fn hf_login(payload: &Value) -> Value {
    let logged_in = payload.get("is_logged_in").and_then(Value::as_bool).unwrap_or(false);
    let username = payload.get("username").and_then(Value::as_str).filter(|u| logged_in && !u.is_empty());
    json!({"logged_in": logged_in, "username": username})
}

async fn hf_status(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    if let Some((at, login)) = state.catalog.hf.lock().await.as_ref()
        && at.elapsed() < HF_STATUS_TTL
    {
        return Ok(Json(login.clone()));
    }
    let generation = state.catalog.generation.load(Ordering::Acquire);
    let payload = state.daemon.call(Method::GET, "hf-auth/status", &[], None, Some(HF_TIMEOUT)).await?;
    let login = hf_login(&payload);
    let mut cached = state.catalog.hf.lock().await;
    if state.catalog.generation.load(Ordering::Acquire) == generation {
        *cached = Some((Instant::now(), login.clone()));
    }
    Ok(Json(login))
}

/// A token travels only over HTTPS or loopback: plain HTTP on the LAN
/// (`CONTROLLER_LAN`) would send it in the clear.
fn secure(ctx: &AuthContext) -> bool {
    ctx.https || ctx.peer.is_some_and(|p| p.is_loopback())
}

async fn hf_save(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Bytes,
) -> ApiResult<Json<Value>> {
    if !secure(&ctx) {
        return Err(ApiError::new(403, "Sign in to Hugging Face over the HTTPS (tailnet) address"));
    }
    let payload: Value = serde_json::from_slice(&body).map_err(|_| ApiError::bad_body())?;
    let token = payload.get("token").and_then(Value::as_str).map(str::trim).unwrap_or_default();
    if token.is_empty()
        || token.len() > MAX_TOKEN_LEN
        || !token.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err(ApiError::new(422, "That doesn't look like a Hugging Face access token"));
    }
    let saved = state
        .daemon
        .call(Method::POST, "hf-auth/save-token", &[], Some(&json!({"token": token})), Some(HF_TIMEOUT))
        .await
        // The daemon's refusal text never includes the token, but don't pass
        // anything through on this route.
        .map_err(|e| match e {
            DaemonError::Refused { status: 400, .. } => ApiError::new(400, "Hugging Face didn't accept that token"),
            DaemonError::Refused { .. } => ApiError::new(502, "Couldn't save the token"),
            DaemonError::Unavailable => ApiError::unavailable(),
        })?;
    state.catalog.forget().await;
    let username = saved.get("username").and_then(Value::as_str).filter(|u| !u.is_empty());
    Ok(Json(json!({"logged_in": true, "username": username})))
}

async fn hf_delete(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    state.daemon.call(Method::DELETE, "hf-auth/token", &[], None, None).await?;
    state.catalog.forget().await;
    Ok(Json(json!({"logged_in": false, "username": null})))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn space_ids_are_owner_and_name() {
        assert!(space_id("pollen-robotics/reachy_mini_radio"));
        assert!(!space_id("radio"));
        assert!(!space_id("a/b/c"));
        assert!(!space_id("../etc"));
        assert!(!space_id("owner/.."));
        assert!(!space_id("owner/na me"));
        assert!(!space_id("owner/"));
    }

    #[test]
    fn scrub_hides_tokens_only() {
        assert_eq!(scrub("token hf_abcdefghij1234 used"), "token hf_*** used");
        assert_eq!(scrub("hf_abcdefghij1234"), "hf_***");
        assert_eq!(scrub("Downloading hf_space foo"), "Downloading hf_space foo");
        assert_eq!(scrub("hf_oauth_abcDEF123456 ok"), "hf_*** ok");
        assert_eq!(scrub("calling hf_hub_download"), "calling hf_hub_download");
        assert_eq!(scrub("no token here"), "no token here");
    }
}
