//! Listen and talk from the dashboard (#219): sessions on the audio gateway.
//!
//! The gateway (`reachy-bridge-audio-gateway`, loopback only) streams the
//! robot mic to one browser and plays that browser's mic on the robot
//! speaker, over WebRTC. The controller is its only client: it starts the
//! gateway's unit on demand, opens a session for the browser that asks,
//! passes its answer on, and ends the session when the browser's login or,
//! in admin mode, its control (#221) ends. Starting a session is a robot
//! action, so in admin mode only the device in control can (control::guard);
//! a connected session counts as that device being present.
//!
//! One session at a time: a new one replaces the old. Sim mode has no
//! audio (its daemon has no media), so the routes refuse there instead of
//! opening the real robot's mic.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::auth::{AuthContext, Credential};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

const TIMEOUT: Duration = Duration::from_secs(15);
/// How often a session's watcher checks the gateway.
const WATCH: Duration = Duration::from_secs(2);
/// How long a freshly started gateway gets to answer.
const START_WAIT: Duration = Duration::from_secs(10);

#[derive(Clone)]
struct Owner {
    id: String,
    device: Option<String>,
    person: Option<String>,
    listen: bool,
    talk: bool,
    since: u64,
    end: Arc<Notify>,
}

pub struct Audio {
    url: Mutex<String>,
    /// The gateway's systemd user unit; `None` when something else runs it.
    unit: Option<String>,
    systemctl: String,
    http: reqwest::Client,
    owner: Mutex<Option<Owner>>,
    /// One open at a time, so `owner` names the gateway's latest session.
    opening: tokio::sync::Mutex<()>,
}

impl Audio {
    pub fn new(url: &str, unit: Option<String>) -> Self {
        Self {
            url: Mutex::new(url.trim_end_matches('/').to_string()),
            unit: unit.filter(|u| !u.is_empty()),
            systemctl: "systemctl".into(),
            http: reqwest::Client::builder().timeout(TIMEOUT).build().expect("http client"),
            owner: Mutex::new(None),
            opening: tokio::sync::Mutex::new(()),
        }
    }

    /// `AUDIO_GATEWAY_URL` (`http://127.0.0.1:8002`) and `AUDIO_GATEWAY_UNIT`
    /// (`reachy-bridge-audio.service`; empty leaves starting it to someone else).
    pub fn from_env() -> Self {
        let url = std::env::var("AUDIO_GATEWAY_URL").ok().filter(|u| !u.is_empty());
        let unit = std::env::var("AUDIO_GATEWAY_UNIT").unwrap_or_else(|_| "reachy-bridge-audio.service".into());
        Self::new(url.as_deref().unwrap_or("http://127.0.0.1:8002"), Some(unit))
    }

    /// Use the gateway at `url` (tests).
    pub fn set_url(&self, url: &str) {
        *self.url.lock().unwrap() = url.trim_end_matches('/').to_string();
    }

    async fn call(&self, method: reqwest::Method, path: &str, body: Option<Value>) -> Result<Option<Value>, ApiError> {
        let url = format!("{}{path}", self.url.lock().unwrap());
        let mut request = self.http.request(method, url);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request.send().await.map_err(|_| ApiError::new(503, "The audio gateway isn't answering"))?;
        let status = response.status();
        let value: Option<Value> = response.json().await.ok();
        if status.is_success() {
            return Ok(value);
        }
        let detail = value.as_ref().and_then(|v| v["detail"].as_str()).unwrap_or("audio gateway error").to_string();
        Err(ApiError::new(if status.as_u16() == 404 { 404 } else { 502 }, detail))
    }

    /// The gateway's session, or None.
    async fn status(&self) -> Result<Value, ApiError> {
        Ok(self.call(reqwest::Method::GET, "/status", None).await?.map(|v| v["session"].clone()).unwrap_or(Value::Null))
    }

    /// Start the gateway's unit unless it already answers.
    async fn ensure_running(&self) -> ApiResult<()> {
        if self.status().await.is_ok() {
            return Ok(());
        }
        let Some(unit) = &self.unit else {
            return Err(ApiError::new(503, "The audio gateway isn't running"));
        };
        crate::sim::systemctl(&self.systemctl, "start", unit)
            .await
            .map_err(|error| ApiError::new(503, format!("Couldn't start the audio gateway: {error}")))?;
        let deadline = tokio::time::Instant::now() + START_WAIT;
        while tokio::time::Instant::now() < deadline {
            if self.status().await.is_ok() {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        Err(ApiError::new(503, "The audio gateway didn't start"))
    }

    fn owner(&self) -> Option<Owner> {
        self.owner.lock().unwrap().clone()
    }

    /// Forget the session `id` (ended, replaced or gone).
    fn forget(&self, id: &str) {
        let mut owner = self.owner.lock().unwrap();
        if owner.as_ref().is_some_and(|o| o.id == id) {
            *owner = None;
        }
    }
}

/// At startup: end a session a previous controller left open. Nothing
/// watches it any more, so nothing would end it on logout or lost control,
/// and no dashboard would show its mic as live.
pub async fn end_leftover(state: Shared) {
    let audio = &state.audio;
    if let Ok(session) = audio.status().await
        && let Some(id) = session["id"].as_str()
    {
        tracing::info!("audio: ending session {id} left by an earlier controller");
        let _ = audio.call(reqwest::Method::DELETE, &format!("/session/{id}"), None).await;
    }
}

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/audio", get(view))
        .route("/api/audio/session", post(open))
        .route("/api/audio/session/{id}", axum::routing::delete(close))
        .route("/api/audio/session/{id}/answer", post(answer))
        .route("/api/audio/session/{id}/route", post(route))
}

/// The open session, for every dashboard's mic-live indicator. Only its
/// owner gets the id; without logins (no device) every browser is equally
/// trusted, so all get it and each tells its own by the id it opened.
async fn view(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> Json<Value> {
    let session = state.audio.owner().map(|o| {
        let you = o.device.is_some() && o.device == ctx.device;
        json!({
            "id": if you || o.device.is_none() { Some(o.id.clone()) } else { None },
            "person": o.person,
            "listen": o.listen,
            "talk": o.talk,
            "since": o.since,
            "you": you,
        })
    });
    Json(json!({"available": !state.sim.active(), "session": session}))
}

#[derive(Deserialize)]
struct Route {
    listen: bool,
    talk: bool,
}

fn not_sim(state: &Shared) -> ApiResult<()> {
    if state.sim.active() {
        return Err(ApiError::new(409, "The simulation has no microphone or speaker; leave sim mode to listen or talk"));
    }
    Ok(())
}

async fn open(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<Route>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    not_sim(&state)?;
    let audio = &state.audio;
    let _opening = audio.opening.lock().await;
    audio.ensure_running().await?;
    let opened = audio
        .call(reqwest::Method::POST, "/session", Some(json!({"listen": body.listen, "talk": body.talk})))
        .await?
        .unwrap_or(Value::Null);
    let (Some(id), Some(offer)) = (opened["id"].as_str(), opened["offer"].as_str()) else {
        return Err(ApiError::new(502, "The audio gateway sent no offer"));
    };
    let owner = Owner {
        id: id.to_string(),
        device: ctx.device.clone(),
        person: ctx.person.clone(),
        listen: body.listen,
        talk: body.talk,
        since: state.control.now(),
        end: Arc::new(Notify::new()),
    };
    // The gateway replaced any earlier session; end its watcher too.
    if let Some(previous) = audio.owner.lock().unwrap().replace(owner.clone()) {
        previous.end.notify_one();
    }
    tracing::info!(
        "audio: session {id} for {} (listen={}, talk={})",
        ctx.person.as_deref().or(ctx.device.as_deref()).unwrap_or("this host"),
        body.listen,
        body.talk
    );
    let credential = ctx.credential.clone().unwrap_or(Credential::Trusted);
    tokio::spawn(watch(state.clone(), owner, credential, ctx.local));
    Ok(Json(json!({"id": id, "offer": offer})))
}

/// Keep a session tied to its browser: end it when the browser's login or
/// control ends, sim mode starts, or it's replaced; while it's connected,
/// count the browser as present (a locked phone still talking holds on).
async fn watch(state: Shared, owner: Owner, credential: Credential, local: bool) {
    let audio = &state.audio;
    let id = owner.id.clone();
    let lost = async {
        match (&owner.device, local) {
            (Some(device), false) => state.control.lost(&state.auth, device.clone()).await,
            _ => std::future::pending().await,
        }
    };
    let ticks = async {
        loop {
            tokio::time::sleep(WATCH).await;
            if state.sim.active() {
                return "sim mode started";
            }
            match audio.status().await {
                Ok(session) if session["id"].as_str() == Some(id.as_str()) => {
                    if session["connected"] == true
                        && let Some(device) = &owner.device
                    {
                        state.control.touch(device);
                    }
                }
                Ok(_) => return "gone from the gateway",
                // A blip; the gateway drops a session that never connects.
                Err(_) => {}
            }
        }
    };
    let why = tokio::select! {
        () = owner.end.notified() => "replaced or closed",
        () = state.auth.ended(credential) => "its browser logged out",
        () = lost => "its device lost control",
        why = ticks => why,
    };
    tracing::info!("audio: session {id} ended: {why}");
    // Best effort: the gateway may already have dropped it.
    let _ = audio.call(reqwest::Method::DELETE, &format!("/session/{id}"), None).await;
    audio.forget(&id);
}

/// The open session, if `ctx` owns it (or is the CLI's key).
fn owned(state: &Shared, ctx: &AuthContext, id: &str) -> ApiResult<Owner> {
    match state.audio.owner() {
        Some(owner) if owner.id == id && (ctx.local || owner.device == ctx.device) => Ok(owner),
        Some(owner) if owner.id == id => Err(ApiError::new(403, "another browser's audio session")),
        _ => Err(ApiError::new(404, "no such audio session")),
    }
}

#[derive(Deserialize)]
struct Answer {
    sdp: String,
}

async fn answer(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    Path(id): Path<String>,
    body: Result<Json<Answer>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    owned(&state, &ctx, &id)?;
    if body.sdp.len() > 32_768 {
        return Err(ApiError::new(400, "answer too large"));
    }
    state.audio.call(reqwest::Method::POST, &format!("/session/{id}/answer"), Some(json!({"sdp": body.sdp}))).await?;
    Ok(Json(json!({"status": "answered"})))
}

async fn route(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    Path(id): Path<String>,
    body: Result<Json<Route>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    owned(&state, &ctx, &id)?;
    let route = json!({"listen": body.listen, "talk": body.talk});
    state.audio.call(reqwest::Method::POST, &format!("/session/{id}/route"), Some(route)).await?;
    if let Some(owner) = state.audio.owner.lock().unwrap().as_mut().filter(|o| o.id == id) {
        owner.listen = body.listen;
        owner.talk = body.talk;
    }
    Ok(Json(json!({"listen": body.listen, "talk": body.talk})))
}

/// End a session: its own browser. (An admin ends someone else's by taking
/// control.)
async fn close(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>, Path(id): Path<String>) -> ApiResult<Json<Value>> {
    owned(&state, &ctx, &id)?.end.notify_one();
    Ok(Json(json!({"status": "closed"})))
}
