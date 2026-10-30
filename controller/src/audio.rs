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
//! It goes with the robot: Start is refused while the robot is off, and
//! turning it off (or its daemon stopping) ends the session.
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
use crate::daemon::{backend_up, robot_state};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

pub const DEFAULT_URL: &str = "http://127.0.0.1:8002";
pub const KEY_FILE: &str = "audio.key";
/// `<unix seconds>.<nonce>.<mac>` on every request, where mac is
/// `sign(key, "request", "<seconds>:<nonce>:<METHOD>:<path>:<sha256(body)>")`:
/// bound to the request, and the gateway takes each nonce once within a
/// short window, so a captured header can't be reused.
pub const AUTH_HEADER: &str = "x-audio-auth";
/// `sign(key, "reply", nonce)` on every reply.
pub const PROOF_HEADER: &str = "x-audio-proof";

/// HMAC-SHA256 of `"<what>:<nonce>"`, in hex.
pub fn sign(key: &str, what: &str, nonce: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key.as_bytes()));
    } else {
        block[..key.len()].copy_from_slice(key.as_bytes());
    }
    let pad = |byte: u8| block.map(|b| b ^ byte);
    let inner = Sha256::new().chain_update(pad(0x36)).chain_update(format!("{what}:{nonce}")).finalize();
    let outer = Sha256::new().chain_update(pad(0x5c)).chain_update(inner).finalize();
    outer.iter().map(|b| format!("{b:02x}")).collect()
}
const TIMEOUT: Duration = Duration::from_secs(15);
/// How often a session's watcher checks the gateway.
const WATCH: Duration = Duration::from_secs(2);
/// Failed checks in a row after which the gateway counts as gone (10 s).
const GATEWAY_GONE: u32 = 5;
/// Checks in a row without a robot that's on (no answer, a crashed backend)
/// after which the session ends (6 s); a robot that says it's off ends it at once.
const ROBOT_GONE: u32 = 3;
/// How long a freshly started gateway gets to answer.
const START_WAIT: Duration = Duration::from_secs(10);

#[derive(Clone)]
struct Owner {
    /// The gateway's id: only the owner's browser learns it.
    id: String,
    /// Shown to every browser, so the owner can tell its session apart
    /// without logins (where browsers have no device).
    public: String,
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
    /// Shared with the gateway through `audio.key` in the state dir (0600).
    /// Requests and replies are signed with it, never carry it: whoever
    /// answers on the gateway's port learns nothing, and a stand-in that
    /// can't sign its replies isn't believed (#219).
    key: Option<String>,
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
            key: None,
            owner: Mutex::new(None),
            opening: tokio::sync::Mutex::new(()),
        }
    }

    /// `AUDIO_GATEWAY_URL` (`http://127.0.0.1:8002`) and `AUDIO_GATEWAY_UNIT`
    /// (`reachy-bridge-audio.service`; empty leaves starting it to someone else).
    pub fn from_env() -> Self {
        let url = std::env::var("AUDIO_GATEWAY_URL").ok().filter(|u| !u.is_empty());
        let unit = std::env::var("AUDIO_GATEWAY_UNIT").unwrap_or_else(|_| "reachy-bridge-audio.service".into());
        Self::new(url.as_deref().unwrap_or(DEFAULT_URL), Some(unit))
    }

    /// Write a fresh `audio.key` for the gateway (it reads it per request).
    pub fn with_key_file(mut self, state_dir: &std::path::Path) -> Self {
        let key = crate::auth::random_hex(32);
        match crate::auth::write_private(&state_dir.join(KEY_FILE), format!("{key}\n").as_bytes()) {
            Ok(()) => self.key = Some(key),
            Err(error) => tracing::error!("audio: cannot write {KEY_FILE} ({error}); listen and talk won't work"),
        }
        self
    }

    /// Use the gateway at `url` (tests).
    pub fn set_url(&self, url: &str) {
        *self.url.lock().unwrap() = url.trim_end_matches('/').to_string();
    }

    async fn call(&self, method: reqwest::Method, path: &str, body: Option<Value>) -> Result<Option<Value>, ApiError> {
        let url = format!("{}{path}", self.url.lock().unwrap());
        let Some(key) = &self.key else {
            return Err(ApiError::new(503, format!("No {KEY_FILE}; listen and talk are off")));
        };
        let nonce = crate::auth::random_hex(16);
        let bytes = body.map(|b| serde_json::to_vec(&b).expect("json")).unwrap_or_default();
        let digest: String = {
            use sha2::{Digest, Sha256};
            Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect()
        };
        let seconds = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        let mac = sign(key, "request", &format!("{seconds}:{nonce}:{method}:{path}:{digest}"));
        let mut request = self.http.request(method, url).header(AUTH_HEADER, format!("{seconds}.{nonce}.{mac}"));
        if !bytes.is_empty() {
            request = request.header(reqwest::header::CONTENT_TYPE, "application/json").body(bytes);
        }
        let response = request.send().await.map_err(|_| ApiError::new(503, "The audio gateway isn't answering"))?;
        let proof = response.headers().get(PROOF_HEADER).and_then(|v| v.to_str().ok()).unwrap_or("");
        if !crate::auth::ct_eq(proof.as_bytes(), sign(key, "reply", &nonce).as_bytes()) {
            if response.status() == reqwest::StatusCode::FORBIDDEN {
                return Err(ApiError::new(503, format!("The audio gateway refused the controller's {KEY_FILE}; do both use the same state dir?")));
            }
            tracing::warn!("audio: something unsigned answered on the gateway's port; not using it");
            return Err(ApiError::new(503, "Something other than the audio gateway is on its port"));
        }
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
        // Something listening that isn't a working gateway (a wrong key, a
        // squatter): say so instead of starting a unit that can't bind.
        if self.listening().await {
            return self.status().await.map(|_| ());
        }
        let Some(unit) = &self.unit else {
            return Err(ApiError::new(503, "The audio gateway isn't running"));
        };
        crate::sim::systemctl(&self.systemctl, "start", unit)
            .await
            .map_err(|error| ApiError::new(503, format!("Couldn't start the audio gateway: {error}")))?;
        let deadline = tokio::time::Instant::now() + START_WAIT;
        while tokio::time::Instant::now() < deadline {
            // Up: its answer decides (a wrong key is reported as such).
            if self.listening().await {
                return self.status().await.map(|_| ());
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        Err(ApiError::new(503, "The audio gateway didn't start; see `reachy-bridge logs audio` (and `reachy-bridge check`)"))
    }

    async fn listening(&self) -> bool {
        let url = self.url.lock().unwrap().clone();
        let Some(address) = reqwest::Url::parse(&url).ok().and_then(|u| Some(format!("{}:{}", u.host_str()?, u.port_or_known_default()?))) else {
            return false;
        };
        matches!(tokio::time::timeout(Duration::from_secs(2), tokio::net::TcpStream::connect(address)).await, Ok(Ok(_)))
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
        tracing::info!("audio: ending a session left by an earlier controller");
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
/// owner's login gets the id; without logins (no device) each browser tells
/// its own by the `ref` it was given when it opened it.
async fn view(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> Json<Value> {
    let session = state.audio.owner().map(|o| {
        let you = o.device.is_some() && o.device == ctx.device;
        json!({
            "id": if you { Some(o.id.clone()) } else { None },
            "ref": o.public,
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

/// The robot is powered on (asleep counts: its motors are off, not it).
fn robot_on(status: &Value) -> bool {
    let robot = robot_state(status);
    matches!(robot.as_str(), "running" | "asleep") || (robot == "error" && backend_up(status))
}

/// The robot is definitely off: its backend is gone and the daemon says so.
fn robot_off(status: &Value) -> bool {
    !backend_up(status) && matches!(robot_state(status).as_str(), "stopped" | "not_initialized")
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
    // Listening and talking go with the robot: off means off.
    match state.daemon.status().await {
        None => return Err(ApiError::new(503, "The robot's daemon isn't answering")),
        Some(s) if robot_state(&s) == "starting" => {
            return Err(ApiError::new(409, "The robot is still starting; try again in a moment"));
        }
        Some(s) if !robot_on(&s) => return Err(ApiError::new(409, "Turn the robot on to listen or talk")),
        Some(_) => {}
    }
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
        public: crate::auth::random_hex(8),
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
    // The log is readable from every dashboard: name the public ref, never the id.
    tracing::info!(
        "audio: session {} for {} (listen={}, talk={})",
        owner.public,
        ctx.person.as_deref().or(ctx.device.as_deref()).unwrap_or("this host"),
        body.listen,
        body.talk
    );
    let credential = ctx.credential.clone().unwrap_or(Credential::Trusted);
    let public = owner.public.clone();
    tokio::spawn(watch(state.clone(), owner, credential, ctx.local));
    Ok(Json(json!({"id": id, "ref": public, "offer": offer})))
}

/// Keep a session tied to its browser: end it when the browser's login or
/// control ends, sim mode starts, or it's replaced; while it's connected,
/// count the browser as present, even if its page is hidden.
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
        let mut errors = 0;
        let mut robot_gone = 0;
        loop {
            tokio::time::sleep(WATCH).await;
            if state.sim.active() {
                return "sim mode started";
            }
            match state.daemon.status().await {
                Some(status) if robot_off(&status) => return "the robot was turned off",
                Some(status) if robot_on(&status) => robot_gone = 0,
                // No answer, or a robot in between (its backend crashed, or it's
                // stopping): no robot either, unless it's a blip.
                _ => {
                    robot_gone += 1;
                    if robot_gone >= ROBOT_GONE {
                        return "the robot stopped answering";
                    }
                }
            }
            let status = audio.status().await;
            errors = if status.is_err() { errors + 1 } else { 0 };
            match status {
                Ok(session) if session["id"].as_str() == Some(id.as_str()) => {
                    if session["connected"] == true
                        && let Some(device) = &owner.device
                    {
                        state.control.touch(device);
                    }
                }
                Ok(_) => return "gone from the gateway",
                // A blip, unless it lasts: then the gateway is gone, and its session with it.
                Err(_) if errors >= GATEWAY_GONE => return "the gateway stopped answering",
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
    tracing::info!("audio: session {} ended: {why}", owner.public);
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
    // Gone for every dashboard now; the watcher still ends it on the gateway.
    state.audio.forget(&id);
    Ok(Json(json!({"status": "closed"})))
}
