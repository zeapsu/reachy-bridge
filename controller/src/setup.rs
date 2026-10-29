//! The setup page (#135): a temporary page on the LAN that walks a new
//! install through the device check and makes the user's dashboard the admin.
//!
//! `reachy-bridge setup` asks for it with the local key
//! (`POST /api/setup/start`, loopback only). The controller then listens on
//! every interface on `SETUP_PORT` (7870), LAN mode or not: that is the
//! point of the page. It answers with a one-time code, which the CLI prints
//! on the terminal. The code never goes through the log: it is in that HTTP
//! response and nowhere else.
//!
//! - The code is 6 digits from the OS random generator, single use and valid
//!   for [`CODE_TTL`]. [`MAX_WRONG`] wrong codes, from anyone, lock the page
//!   until the terminal asks for a new code (`start` again).
//! - The right code gives that browser the setup cookie ([`SETUP_COOKIE`]),
//!   tied to its address, good until the page closes. Only one browser gets it.
//! - The setup listener serves the setup routes and the dashboard bundle
//!   (which shows the setup screen there) and nothing else: no controller
//!   API, no app proxy, no daemon. It answers only under the robot's own
//!   names and addresses, so a DNS-rebinding page can't reach it.
//! - It closes when setup finishes, when the terminal stops it, when the code
//!   expires unused, or [`SESSION_TTL`] after the code was used.
//!
//! **The admin is approved on the terminal.** The setup cookie crosses the
//! LAN over plain HTTP, so it can't be what grants admin. The browser only
//! *proposes* the pairing code its dashboard shows; the terminal then shows
//! who asked (the dashboard's address and the name typed) and the person at
//! the robot types `y`, which approves it with the local key exactly as
//! `reachy-bridge pair --admin` does. One admin per page; after
//! [`MAX_PROPOSALS`] proposals that match no pairing request, the setup
//! session ends.

use std::collections::HashSet;
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use axum::extract::{ConnectInfo, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, Method};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::{cookie_header, cookies, cross_site, ct_eq, is_api, random_code, random_token, sha256, AuthContext, Role};
use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

pub const SETUP_COOKIE: &str = "rb_setup";
/// How long a code is valid before it is used.
pub const CODE_TTL: Duration = Duration::from_secs(15 * 60);
/// How long the page stays open once the code is used.
pub const SESSION_TTL: Duration = Duration::from_secs(30 * 60);
/// Wrong codes, from any browser, before the page locks.
pub const MAX_WRONG: u32 = 5;
/// Admin proposals matching no pairing request before the session ends.
pub const MAX_PROPOSALS: u32 = 5;
const DEFAULT_PORT: u16 = 7870;
/// The device check reads USB, GStreamer and the disk; give it time.
const CHECK_TIMEOUT: Duration = Duration::from_secs(300);

#[derive(Default)]
pub struct Setup {
    live: Mutex<Option<Live>>,
    /// A device check is running (one at a time).
    checking: AtomicBool,
}

/// What the setup browser asked for, and what the terminal answered.
#[derive(Clone)]
struct Proposal {
    code: String,
    person: String,
    name: String,
    /// The dashboard's address (the pairing request's peer).
    peer: IpAddr,
    outcome: Outcome,
}

#[derive(Clone, PartialEq)]
enum Outcome {
    Waiting,
    Approved,
    Refused(String),
}

struct Live {
    /// Empty once used.
    code: String,
    wrong: u32,
    session_sha256: Option<[u8; 32]>,
    /// The address that used the code; the session works from there only.
    session_peer: Option<IpAddr>,
    proposal: Option<Proposal>,
    missed_proposals: u32,
    /// When the page closes, unless setup finishes first.
    ends: Instant,
    port: u16,
    /// Host names the page answers to (besides IP literals).
    names: HashSet<String>,
    stop: tokio::sync::watch::Sender<bool>,
}

impl Live {
    /// A fresh code; it also ends any setup session.
    fn new_code(&mut self, now: Instant) -> String {
        self.code = random_code();
        self.wrong = 0;
        self.end_session();
        self.ends = now + CODE_TTL;
        self.code.clone()
    }

    fn end_session(&mut self) {
        self.session_sha256 = None;
        self.session_peer = None;
        self.proposal = None;
        self.missed_proposals = 0;
    }

    fn state(&self) -> Value {
        json!({
            "open": true,
            "port": self.port,
            "used": self.code.is_empty(),
            "in_use": self.session_sha256.is_some(),
            "locked": self.wrong >= MAX_WRONG,
            "expires_in": self.ends.saturating_duration_since(Instant::now()).as_secs(),
        })
    }
}

impl Setup {
    pub fn is_open(&self) -> bool {
        self.live.lock().unwrap().is_some()
    }

    /// Close the page, if open.
    pub fn close(&self, why: &str) {
        if let Some(live) = self.live.lock().unwrap().take() {
            let _ = live.stop.send(true);
            tracing::info!("setup: page on :{} closed ({why})", live.port);
        }
    }

    fn session(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> bool {
        let live = self.live.lock().unwrap();
        let Some(live) = live.as_ref() else { return false };
        let Some(hash) = live.session_sha256 else { return false };
        peer.is_some() && peer == live.session_peer && cookies(headers, SETUP_COOKIE).take(2).any(|token| ct_eq(&sha256(token.as_bytes()), &hash))
    }
}

fn port() -> u16 {
    std::env::var("SETUP_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(DEFAULT_PORT)
}

/// This machine's host name, and `<name>.local`, lowercase.
fn host_names() -> HashSet<String> {
    let mut names = HashSet::from(["localhost".to_string()]);
    let name = std::fs::read_to_string("/proc/sys/kernel/hostname")
        .ok()
        .or_else(|| std::process::Command::new("hostname").output().ok().and_then(|o| String::from_utf8(o.stdout).ok()));
    if let Some(name) = name.map(|n| n.trim().trim_end_matches('.').to_ascii_lowercase()).filter(|n| !n.is_empty()) {
        let short = name.split('.').next().unwrap_or(&name).to_string();
        names.extend([format!("{short}.local"), short, name]);
    }
    names
}

/// The Host header names this machine: an IP literal or one of its names.
fn own_host(headers: &HeaderMap, names: &HashSet<String>) -> bool {
    let Some(host) = headers.get(header::HOST).and_then(|h| h.to_str().ok()) else { return false };
    let name = match host.rsplit_once(':') {
        Some((name, port)) if !name.ends_with(':') && port.bytes().all(|b| b.is_ascii_digit()) => name,
        _ => host,
    };
    let name = name.trim_end_matches('.').to_ascii_lowercase();
    name.trim_matches(['[', ']']).parse::<IpAddr>().is_ok() || names.contains(&name)
}

/// The terminal's side, for the local key only: open and close the page,
/// watch it, and answer the admin proposal.
pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/setup/start", post(start))
        .route("/api/setup/stop", post(stop))
        .route("/api/setup/state", get(terminal_state))
        .route("/api/setup/confirm", post(confirm))
}

fn local_only(ctx: &AuthContext) -> ApiResult<()> {
    if ctx.local {
        Ok(())
    } else {
        Err(ApiError::new(403, "only `reachy-bridge setup` on the robot can do this"))
    }
}

async fn start(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let now = Instant::now();
    {
        let mut live = state.setup.live.lock().unwrap();
        if let Some(live) = live.as_mut() {
            let code = live.new_code(now);
            tracing::info!("setup: new code issued for the page on :{}", live.port);
            return Ok(Json(json!({"code": code, "port": live.port, "expires_in": CODE_TTL.as_secs()})));
        }
    }
    // Two starts at once: the second bind fails and that start gets an error.
    let port = port();
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
        .await
        .map_err(|error| ApiError::new(500, format!("can't open the setup page on port {port}: {error}")))?;
    let port = listener.local_addr().map(|a| a.port()).unwrap_or(port);
    let (stop, mut stopped) = tokio::sync::watch::channel(false);
    let mut live = Live {
        code: String::new(),
        wrong: 0,
        session_sha256: None,
        session_peer: None,
        proposal: None,
        missed_proposals: 0,
        ends: now,
        port,
        names: host_names(),
        stop,
    };
    let code = live.new_code(now);
    *state.setup.live.lock().unwrap() = Some(live);
    let app = page_router(state.clone()).into_make_service_with_connect_info::<SocketAddr>();
    tokio::spawn(async move {
        let shutdown = async move {
            let _ = stopped.wait_for(|stop| *stop).await;
        };
        if let Err(error) = axum::serve(listener, app).with_graceful_shutdown(shutdown).await {
            tracing::error!(%error, "setup: page server failed");
        }
    });
    tokio::spawn(close_when_due(state.clone()));
    tracing::info!("setup: page open on 0.0.0.0:{port} for {} minutes", CODE_TTL.as_secs() / 60);
    Ok(Json(json!({"code": code, "port": port, "expires_in": CODE_TTL.as_secs()})))
}

async fn stop(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    state.setup.close("stopped from the terminal");
    Ok(Json(json!({"status": "closed"})))
}

/// What the terminal shows: the page's state and any admin proposal waiting
/// for an answer.
async fn terminal_state(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let live = state.setup.live.lock().unwrap();
    let Some(live) = live.as_ref() else { return Ok(Json(json!({"open": false}))) };
    let mut body = live.state();
    if let Some(p) = live.proposal.as_ref().filter(|p| p.outcome == Outcome::Waiting) {
        body["proposal"] = json!({"person": p.person, "name": p.name, "peer": p.peer.to_string()});
    }
    Ok(Json(body))
}

#[derive(Deserialize)]
struct ConfirmBody {
    approve: bool,
}

/// The terminal's answer to the waiting proposal. A yes approves it with the
/// local key, as `reachy-bridge pair --admin` does.
async fn confirm(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<ConfirmBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    local_only(&ctx)?;
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    let proposal = live.proposal.as_mut().filter(|p| p.outcome == Outcome::Waiting).ok_or_else(|| ApiError::new(409, "no admin request is waiting"))?;
    if !body.approve {
        proposal.outcome = Outcome::Refused("refused on the robot's terminal".to_string());
        tracing::warn!("setup: admin request for the browser at {} refused on the terminal", proposal.peer);
        return Ok(Json(json!({"status": "refused"})));
    }
    let result = state.auth.approve_code(&proposal.code, Some(&proposal.name), Some(&proposal.person), Role::Admin, "the setup page (confirmed on the terminal)", true);
    match result {
        Ok(reply) => {
            proposal.outcome = Outcome::Approved;
            tracing::info!("setup: the browser at {} is the admin (confirmed on the terminal)", proposal.peer);
            Ok(Json(reply))
        }
        Err(error) => {
            proposal.outcome = Outcome::Refused(error.detail.clone());
            Err(error)
        }
    }
}

/// Close the page when its time is up (the deadline moves when the code is
/// used or a new one is issued).
async fn close_when_due(state: Shared) {
    loop {
        tokio::time::sleep(Duration::from_secs(1)).await;
        let mut live = state.setup.live.lock().unwrap();
        let Some(current) = live.as_ref() else { return };
        if Instant::now() >= current.ends {
            let used = current.session_sha256.is_some();
            let current = live.take().expect("checked");
            let _ = current.stop.send(true);
            tracing::info!("setup: page on :{} closed ({})", current.port, if used { "time is up" } else { "the code expired" });
            return;
        }
    }
}

/// What the setup listener serves: the setup routes and the dashboard
/// bundle. Any other API path is 404.
fn page_router(state: Shared) -> Router {
    Router::new()
        .route("/api/setup/status", get(status))
        .route("/api/setup/login", post(login))
        .route("/api/setup/check", get(check))
        .route("/api/setup/admin", get(admin_status).post(propose_admin))
        .route("/api/setup/finish", post(finish))
        .fallback(page_fallback)
        .layer(middleware::from_fn_with_state(state.clone(), page_guard))
        .with_state(state)
}

async fn page_fallback(method: Method, uri: axum::http::Uri) -> Response {
    if is_api(uri.path()) || method != Method::GET {
        return ApiError::new(404, "not found").into_response();
    }
    crate::routes::assets::serve(uri).await
}

fn peer_of(request: &Request) -> Option<IpAddr> {
    request.extensions().get::<ConnectInfo<SocketAddr>>().map(|c| c.0.ip().to_canonical())
}

/// Only the robot's own names; no cross-site calls; the setup routes past
/// status and login need the setup cookie from the address that used the
/// code; nothing frames the page.
async fn page_guard(State(state): State<Shared>, request: Request, next: Next) -> Response {
    let peer = peer_of(&request);
    let headers = request.headers();
    let path = request.uri().path().to_string();
    {
        let live = state.setup.live.lock().unwrap();
        let Some(live) = live.as_ref() else { return ApiError::new(410, "setup is closed").into_response() };
        if !own_host(headers, &live.names) {
            tracing::warn!("setup: refused {path} for another host name from {peer:?}");
            return ApiError::new(421, "wrong host").into_response();
        }
    }
    // Only the setup routes exist here, with or without the session.
    if is_api(&path) && !path.starts_with("/api/setup/") {
        return ApiError::new(404, "not found").into_response();
    }
    let api = is_api(&path);
    if (api || !request.method().is_safe() || headers.contains_key(header::UPGRADE)) && cross_site(headers, false) {
        tracing::warn!("setup: refused cross-site {path} from {peer:?}");
        return ApiError::new(403, "cross-site request refused").into_response();
    }
    let open = path == "/api/setup/status" || path == "/api/setup/login" || !api;
    if !open && !state.setup.session(headers, peer) {
        return ApiError::new(401, "enter the setup code first").into_response();
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("frame-ancestors 'none'"));
    headers.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    if api {
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

async fn status(State(state): State<Shared>, ConnectInfo(peer): ConnectInfo<SocketAddr>, headers: HeaderMap) -> Json<Value> {
    let authenticated = state.setup.session(&headers, Some(peer.ip().to_canonical()));
    let live = state.setup.live.lock().unwrap();
    let mut body = live.as_ref().map_or_else(|| json!({"open": false}), Live::state);
    body["setup"] = json!(true);
    body["authenticated"] = json!(authenticated);
    if let Some(object) = body.as_object_mut() {
        object.remove("in_use");
        object.remove("port");
    }
    Json(body)
}

#[derive(Deserialize)]
struct LoginBody {
    code: String,
}

async fn login(
    State(state): State<Shared>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    body: Result<Json<LoginBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Response> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let peer = peer.ip().to_canonical();
    let code: String = body.code.chars().filter(|c| !c.is_whitespace()).collect();
    let now = Instant::now();
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().filter(|l| now < l.ends).ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    let new_code = "run `reachy-bridge setup` on the robot (or press Enter there) for a new one";
    if live.wrong >= MAX_WRONG {
        return Err(ApiError::new(423, format!("too many wrong codes: {new_code}")));
    }
    if live.code.is_empty() {
        return Err(ApiError::new(409, format!("this code was already used: {new_code}")));
    }
    if !ct_eq(code.as_bytes(), live.code.as_bytes()) {
        live.wrong += 1;
        let left = MAX_WRONG - live.wrong;
        tracing::warn!("setup: wrong code from {peer} ({} of {MAX_WRONG})", live.wrong);
        if left == 0 {
            return Err(ApiError::new(423, format!("too many wrong codes: {new_code}")));
        }
        return Err(ApiError::new(400, format!("wrong code; {left} tries left")));
    }
    let token = random_token::<32>();
    live.code.clear();
    live.session_sha256 = Some(sha256(token.as_bytes()));
    live.session_peer = Some(peer);
    live.ends = now + SESSION_TTL;
    tracing::info!("setup: code used by {peer}; the page stays open for {} minutes", SESSION_TTL.as_secs() / 60);
    let cookie = cookie_header(SETUP_COOKIE, &token, "/", SESSION_TTL.as_secs(), false);
    Ok(([(header::SET_COOKIE, cookie)], Json(json!({"status": "ok"}))).into_response())
}

/// The runtime's device check (`--doctor --json`): what `reachy-bridge
/// check` shows in the terminal. One at a time.
async fn check(State(state): State<Shared>) -> ApiResult<Json<Value>> {
    if state.setup.checking.swap(true, Ordering::SeqCst) {
        return Err(ApiError::new(409, "the device check is already running"));
    }
    struct Done<'a>(&'a AtomicBool);
    impl Drop for Done<'_> {
        fn drop(&mut self) {
            self.0.store(false, Ordering::SeqCst);
        }
    }
    let _done = Done(&state.setup.checking);
    let runtime = std::env::var_os("REACHY_BRIDGE_RUNTIME").filter(|p| !p.is_empty()).map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".local/share/reachy-bridge/.venv/bin/reachy-bridge-runtime")
    });
    let run = tokio::process::Command::new(&runtime).args(["--doctor", "--json"]).kill_on_drop(true).output();
    let output = tokio::time::timeout(CHECK_TIMEOUT, run)
        .await
        .map_err(|_| ApiError::new(504, "the device check took too long"))?
        .map_err(|error| ApiError::new(500, format!("can't run the device check ({}): {error}", runtime.display())))?;
    // It exits 1 when something failed; the report is on stdout either way.
    let results: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| ApiError::new(500, "the device check didn't produce a report; run `reachy-bridge check` on the robot"))?;
    Ok(Json(json!({"results": results})))
}

#[derive(Deserialize)]
struct AdminBody {
    code: String,
    person: Option<String>,
    name: Option<String>,
}

/// Ask the terminal to make the dashboard showing `code` the admin.
async fn propose_admin(State(state): State<Shared>, body: Result<Json<AdminBody>, axum::extract::rejection::JsonRejection>) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let code: String = body.code.chars().filter(|c| !c.is_whitespace()).collect();
    if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ApiError::new(400, "the code is 6 digits"));
    }
    let clean = |text: Option<&str>| -> String { text.unwrap_or("").trim().chars().filter(|c| !c.is_control()).take(64).collect() };
    let (person, name) = (clean(body.person.as_deref()), clean(body.name.as_deref()));
    let peer = state.auth.pending_peer(&code);
    let mut slot = state.setup.live.lock().unwrap();
    let live = slot.as_mut().ok_or_else(|| ApiError::new(410, "setup is closed"))?;
    match live.proposal.as_ref().map(|p| &p.outcome) {
        Some(Outcome::Approved) => return Err(ApiError::new(409, "an admin was already made from this page")),
        Some(Outcome::Waiting) => return Err(ApiError::new(409, "waiting for the robot's terminal to answer")),
        _ => {}
    }
    let Some(peer) = peer else {
        live.missed_proposals += 1;
        tracing::warn!("setup: admin request with a code that matches no pairing request ({} of {MAX_PROPOSALS})", live.missed_proposals);
        if live.missed_proposals >= MAX_PROPOSALS {
            live.end_session();
            return Err(ApiError::new(423, "too many codes that match no dashboard: enter a new setup code"));
        }
        return Err(ApiError::new(400, "no dashboard is showing that code; check it, or get a new one on the dashboard"));
    };
    live.proposal = Some(Proposal { code, person: person.clone(), name, peer, outcome: Outcome::Waiting });
    tracing::info!("setup: admin request for the browser at {peer} waits for the terminal");
    Ok(Json(json!({"status": "waiting", "peer": peer.to_string(), "person": person})))
}

/// Where the admin request stands: none, waiting, approved or refused.
async fn admin_status(State(state): State<Shared>) -> Json<Value> {
    let live = state.setup.live.lock().unwrap();
    let proposal = live.as_ref().and_then(|l| l.proposal.clone());
    Json(match proposal.map(|p| p.outcome) {
        None => json!({"status": "none"}),
        Some(Outcome::Waiting) => json!({"status": "waiting"}),
        Some(Outcome::Approved) => json!({"status": "approved"}),
        Some(Outcome::Refused(detail)) => json!({"status": "refused", "detail": detail}),
    })
}

async fn finish(State(state): State<Shared>) -> Json<Value> {
    state.setup.close("setup finished");
    Json(json!({"status": "closed"}))
}
