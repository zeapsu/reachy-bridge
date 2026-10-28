//! Controller login for LAN access (#141).
//!
//! Off by default: with `CONTROLLER_LAN` unset there is no login, and the
//! guard only refuses cross-site state changes. In LAN mode a request is authenticated when its TCP
//! peer is loopback (`tailscale serve` and local tools; `CONTROLLER_TRUST_TAILNET=0`
//! turns that off), when it carries a paired device's `rb_device` cookie, or
//! when a loopback peer sends the local key the controller writes to
//! `$REACHY_BRIDGE_STATE_DIR/controller.key` (how `reachy-bridge pair` works
//! when loopback isn't trusted). Unauthenticated API calls get 401; pages are
//! served so the dashboard can show its login screen.
//!
//! The peer is always the socket address from `ConnectInfo`, never a header.
//!
//! The app UI proxy (:8043) applies the same rule through
//! [`Auth::app_proxy_allowed`] and strips these credentials before a request
//! reaches the app.

use std::collections::{HashMap, VecDeque};
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::extract::{ConnectInfo, Path as UrlPath, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Extension, Json, Router};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::{ApiError, ApiResult};
use crate::state::Shared;

pub const DEVICE_COOKIE: &str = "rb_device";
pub const PAIR_COOKIE: &str = "rb_pair";
/// Header carrying the local key from `controller.key` (loopback only).
pub const LOCAL_KEY_HEADER: &str = "x-reachy-bridge-key";
pub const DEVICES_FILE: &str = "devices.json";
pub const LOCAL_KEY_FILE: &str = "controller.key";

const DEVICE_MAX_AGE: u64 = 31_536_000;
const MAX_ATTEMPTS: u32 = 5;
const PER_PEER_LIMIT: usize = 5;
const PER_PEER_WINDOW: Duration = Duration::from_secs(600);
const MAX_PENDING: usize = 20;
/// Loopback visitors (tailscale serve with CONTROLLER_TRUST_TAILNET=0) share
/// one address, so they get a larger bucket and aren't counted in `MAX_PENDING`.
const LOOPBACK_PEER_LIMIT: usize = 20;
/// Cookie values checked per request.
const MAX_COOKIE_VALUES: usize = 2;
/// A device unseen this long is logged out (limits a sniffed or leaked token).
const DEVICE_IDLE_EXPIRY: u64 = 30 * 86_400;
/// An approved request stays collectable at least this long.
const APPROVED_GRACE: Duration = Duration::from_secs(60);
/// Expired requests answer "expired" this long before they are forgotten.
const FORGET_AFTER: Duration = Duration::from_secs(60);
/// `last_seen` is written to disk at most this often per device.
const LAST_SEEN_STEP: u64 = 600;

#[derive(Clone, Debug)]
pub struct AuthConfig {
    /// `CONTROLLER_LAN=1`: require a login from non-loopback peers.
    pub lan: bool,
    /// Loopback peers are trusted unless `CONTROLLER_TRUST_TAILNET=0`.
    pub trust_loopback: bool,
    /// How long a pairing code is valid (5 minutes).
    pub pair_ttl: Duration,
}

impl Default for AuthConfig {
    fn default() -> Self {
        Self { lan: false, trust_loopback: true, pair_ttl: Duration::from_secs(300) }
    }
}

fn env_flag(name: &str) -> Option<bool> {
    let value = std::env::var(name).ok()?;
    match value.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

impl AuthConfig {
    pub fn from_env() -> Self {
        Self {
            lan: env_flag("CONTROLLER_LAN").unwrap_or(false),
            trust_loopback: env_flag("CONTROLLER_TRUST_TAILNET").unwrap_or(true),
            ..Self::default()
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Device {
    id: String,
    name: String,
    /// Hex SHA-256 of the cookie token; the token itself is never stored.
    token_sha256: String,
    created: u64,
    last_seen: u64,
    /// Peer address at pairing, then at the last recorded visit.
    peer: String,
}

#[derive(Default, Serialize, Deserialize)]
struct DevicesFile {
    devices: Vec<Device>,
}

struct PairRequest {
    id: String,
    code: String,
    secret_sha256: [u8; 32],
    peer: IpAddr,
    expires: Instant,
    attempts: u32,
    /// Device name, once approved.
    approved: Option<String>,
}

impl PairRequest {
    fn status(&self, now: Instant) -> &'static str {
        if now >= self.expires {
            "expired"
        } else if self.approved.is_some() {
            "approved"
        } else if self.attempts >= MAX_ATTEMPTS {
            "locked"
        } else {
            "pending"
        }
    }
}

#[derive(Default)]
struct Pending {
    requests: Vec<PairRequest>,
    /// Pair requests created per peer within the last `PER_PEER_WINDOW`.
    recent: HashMap<IpAddr, VecDeque<Instant>>,
}

impl Pending {
    fn prune(&mut self, now: Instant) {
        self.requests.retain(|r| now < r.expires + FORGET_AFTER);
        self.recent.retain(|_, times| {
            while times.front().is_some_and(|t| now.duration_since(*t) >= PER_PEER_WINDOW) {
                times.pop_front();
            }
            !times.is_empty()
        });
    }

    /// Open requests from LAN peers (loopback has its own bucket).
    fn live(&self, now: Instant) -> usize {
        self.requests.iter().filter(|r| now < r.expires && !r.peer.is_loopback()).count()
    }
}

pub struct Auth {
    pub config: AuthConfig,
    devices_path: PathBuf,
    devices: Mutex<Vec<Device>>,
    pending: Mutex<Pending>,
    local_key: Option<String>,
    /// Bumped on every revocation; long-lived streams watch it.
    revoked: tokio::sync::watch::Sender<u64>,
}

/// Who a request comes from, as decided by [`guard`].
#[derive(Clone, Debug)]
pub struct AuthContext {
    pub peer: Option<IpAddr>,
    pub authenticated: bool,
    /// The paired device, when the cookie authenticated the request.
    pub device: Option<String>,
    /// The request reached `tailscale serve` over HTTPS.
    pub https: bool,
}

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs())
}

fn random_bytes<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    getrandom::fill(&mut bytes).expect("operating system random number generator");
    bytes
}

fn random_token<const N: usize>() -> String {
    URL_SAFE_NO_PAD.encode(random_bytes::<N>())
}

/// A uniform 6-digit code (rejection sampling avoids modulo bias).
fn random_code() -> String {
    const LIMIT: u32 = u32::MAX - u32::MAX % 1_000_000;
    loop {
        let n = u32::from_le_bytes(random_bytes::<4>());
        if n < LIMIT {
            return format!("{:06}", n % 1_000_000);
        }
    }
}

fn sha256(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Equal-length comparison whose time doesn't depend on where bytes differ.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Write `bytes` to `path` atomically (temp file, fsync, rename), mode 0600.
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(format!(".tmp{}", std::process::id()));
    let tmp = PathBuf::from(tmp);
    let _ = std::fs::remove_file(&tmp);
    let result = (|| {
        let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&tmp)?;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

/// Every value of cookie `name` across the request's `Cookie` headers.
fn cookies<'a>(headers: &'a HeaderMap, name: &'a str) -> impl Iterator<Item = &'a str> + 'a {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .filter_map(move |pair| {
            let (key, value) = pair.trim().split_once('=')?;
            (key == name).then_some(value.trim())
        })
}

/// Drop any `Set-Cookie` for the controller's own cookies from an app's
/// response: same host means same cookie jar, so an app could otherwise log
/// the browser out of the dashboard, or swap in a device it paired. A value
/// that isn't text is dropped too.
pub fn strip_credential_cookies(headers: &mut HeaderMap) {
    let kept: Vec<HeaderValue> = headers
        .get_all(header::SET_COOKIE)
        .iter()
        .filter(|v| {
            let name = v.to_str().ok().and_then(|v| v.split(['=', ';']).next()).map(str::trim);
            name.is_some_and(|name| name != DEVICE_COOKIE && name != PAIR_COOKIE)
        })
        .cloned()
        .collect();
    headers.remove(header::SET_COOKIE);
    for value in kept {
        headers.append(header::SET_COOKIE, value);
    }
}

/// Drop the controller's credentials from a request bound for an app (the
/// app UI proxy): browsers scope cookies by host, not port, so `rb_device`
/// set on the dashboard also comes with every request to :8043.
pub fn strip_credentials(headers: &mut HeaderMap) {
    headers.remove(LOCAL_KEY_HEADER);
    let kept: Vec<String> = headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .map(str::trim)
        .filter(|pair| {
            let name = pair.split_once('=').map_or(*pair, |(name, _)| name).trim();
            !pair.is_empty() && name != DEVICE_COOKIE && name != PAIR_COOKIE
        })
        .map(str::to_string)
        .collect();
    headers.remove(header::COOKIE);
    if let Ok(value) = HeaderValue::from_str(&kept.join("; "))
        && !kept.is_empty()
    {
        headers.insert(header::COOKIE, value);
    }
}

fn cookie_header(name: &str, value: &str, path: &str, max_age: u64, https: bool) -> HeaderValue {
    let secure = if https { "; Secure" } else { "" };
    HeaderValue::from_str(&format!("{name}={value}; HttpOnly; SameSite=Strict; Path={path}; Max-Age={max_age}{secure}"))
        .expect("cookie values are base64url")
}

impl Auth {
    pub fn new(config: AuthConfig, state_dir: &Path) -> Self {
        let devices_path = state_dir.join(DEVICES_FILE);
        let devices = match std::fs::read(&devices_path) {
            Ok(bytes) => match serde_json::from_slice::<DevicesFile>(&bytes) {
                Ok(file) => file.devices,
                Err(error) => {
                    let aside = devices_path.with_extension("json.bad");
                    let _ = std::fs::rename(&devices_path, &aside);
                    tracing::error!(
                        "auth: {} is unreadable ({error}); moved it to {} and starting with no paired devices",
                        devices_path.display(),
                        aside.display()
                    );
                    Vec::new()
                }
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(error) => {
                tracing::error!("auth: cannot read {} ({error}); starting with no paired devices", devices_path.display());
                Vec::new()
            }
        };
        let local_key = config.lan.then(|| {
            let key = random_token::<32>();
            if let Err(error) = write_private(&state_dir.join(LOCAL_KEY_FILE), format!("{key}\n").as_bytes()) {
                tracing::error!("auth: cannot write {LOCAL_KEY_FILE} ({error}); `reachy-bridge pair` needs a trusted loopback");
            }
            key
        });
        Self {
            config,
            devices_path,
            devices: Mutex::new(devices),
            pending: Mutex::new(Pending::default()),
            local_key,
            revoked: tokio::sync::watch::channel(0).0,
        }
    }

    fn save(&self, devices: &[Device]) -> std::io::Result<()> {
        let body = serde_json::to_vec_pretty(&json!({"devices": devices})).expect("devices serialize");
        write_private(&self.devices_path, &body)
    }

    /// The device a cookie token belongs to; refreshes its `last_seen`.
    fn device_for(&self, token: &str, peer: Option<IpAddr>) -> Option<String> {
        if token.is_empty() || token.len() > 128 {
            return None;
        }
        let hash = hex(&sha256(token.as_bytes()));
        let mut devices = self.devices.lock().unwrap();
        // Compare against every device so the scan time doesn't depend on a match.
        let mut found = None;
        for (index, device) in devices.iter().enumerate() {
            if ct_eq(device.token_sha256.as_bytes(), hash.as_bytes()) {
                found = Some(index);
            }
        }
        let index = found?;
        let now = now_unix();
        if now.saturating_sub(devices[index].last_seen) >= DEVICE_IDLE_EXPIRY {
            let device = devices.remove(index);
            tracing::info!("auth: device {} ({}) expired after 30 days unused", device.id, device.name);
            if let Err(error) = self.save(&devices) {
                tracing::warn!("auth: cannot save {DEVICES_FILE} ({error})");
            }
            return None;
        }
        let device = &mut devices[index];
        let id = device.id.clone();
        if now.saturating_sub(device.last_seen) >= LAST_SEEN_STEP {
            device.last_seen = now;
            if let Some(peer) = peer {
                device.peer = peer.to_string();
            }
            if let Err(error) = self.save(&devices) {
                tracing::warn!("auth: cannot record last_seen ({error})");
            }
        }
        Some(id)
    }

    fn has_device(&self, id: &str) -> bool {
        self.devices.lock().unwrap().iter().any(|d| d.id == id)
    }

    fn local_key_matches(&self, headers: &HeaderMap) -> bool {
        let (Some(key), Some(sent)) = (&self.local_key, headers.get(LOCAL_KEY_HEADER)) else {
            return false;
        };
        ct_eq(key.as_bytes(), sent.as_bytes())
    }

    /// Whether the app UI proxy may serve a request. Its listener is loopback
    /// only, so visitors come through tailscale serve: trusted, unless LAN
    /// mode runs with `CONTROLLER_TRUST_TAILNET=0`, when they need a paired
    /// device (or the local key) as on the dashboard.
    pub fn app_proxy_allowed(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> bool {
        if !self.config.lan || self.config.trust_loopback {
            return true;
        }
        self.local_key_matches(headers)
            || cookies(headers, DEVICE_COOKIE).take(MAX_COOKIE_VALUES).any(|token| self.device_for(token, peer).is_some())
    }
}

/// A browser-sent request from another origin (or another port of the same
/// host, such as an app UI). Requests without either header (curl, the CLI)
/// are not cross-site.
pub fn cross_site(headers: &HeaderMap, loopback: bool) -> bool {
    if let Some(site) = headers.get("sec-fetch-site") {
        return !matches!(site.to_str(), Ok("same-origin" | "none"));
    }
    let Some(origin) = headers.get(header::ORIGIN).and_then(|o| o.to_str().ok()) else {
        return false;
    };
    let Some((_, authority)) = origin.split_once("://") else {
        return true;
    };
    let host = headers.get(header::HOST).and_then(|h| h.to_str().ok());
    // tailscale serve keeps Host; X-Forwarded-Host is only believed from loopback.
    let forwarded = loopback.then(|| headers.get("x-forwarded-host").and_then(|h| h.to_str().ok())).flatten();
    ![host, forwarded].into_iter().flatten().any(|h| h.eq_ignore_ascii_case(authority))
}

/// Loopback trust needs a Host a DNS-rebinding page can't produce: a
/// loopback address, `localhost`, or a tailnet (`*.ts.net`) name.
fn loopback_host(headers: &HeaderMap) -> bool {
    let Some(host) = headers.get(header::HOST).and_then(|h| h.to_str().ok()) else {
        return false;
    };
    let name = match host.rsplit_once(':') {
        Some((name, port)) if !name.ends_with(':') && port.bytes().all(|b| b.is_ascii_digit()) => name,
        _ => host,
    };
    let name = name.trim_end_matches('.').to_ascii_lowercase();
    name == "localhost"
        || name.ends_with(".ts.net")
        || name.trim_matches(['[', ']']).parse::<IpAddr>().is_ok_and(|ip| ip.to_canonical().is_loopback())
}

/// The rate-limit key: the address, or its /64 for IPv6.
fn rate_key(peer: IpAddr) -> IpAddr {
    match peer {
        IpAddr::V6(v6) => IpAddr::V6((u128::from(v6) & !((1u128 << 64) - 1)).into()),
        v4 => v4,
    }
}

fn is_api(path: &str) -> bool {
    path == "/api" || path.starts_with("/api/") || path == "/telemetry" || path.starts_with("/telemetry/")
}

fn is_open(path: &str) -> bool {
    path == "/api/auth/status" || path == "/api/auth/pair" || path.starts_with("/api/auth/pair/")
}

/// The auth middleware (see the module docs).
pub async fn guard(State(state): State<Shared>, mut request: Request, next: Next) -> Response {
    let auth = &state.auth;
    let peer = request.extensions().get::<ConnectInfo<SocketAddr>>().map(|c| c.0.ip().to_canonical());
    let loopback = peer.is_some_and(|p| p.is_loopback());
    let headers = request.headers();
    let https = loopback
        && headers.get("x-forwarded-proto").and_then(|v| v.to_str().ok()).is_some_and(|v| v.eq_ignore_ascii_case("https"));
    let path = request.uri().path().to_string();

    if !auth.config.lan {
        // No login, but another site still can't make a tailnet browser act
        // on the robot (a plain form POST to /api/daemon/start).
        if (!request.method().is_safe() || headers.contains_key(header::UPGRADE)) && cross_site(headers, loopback) {
            return forbidden(&path, peer);
        }
        request.extensions_mut().insert(AuthContext { peer, authenticated: true, device: None, https });
        return next.run(request).await;
    }

    let device = cookies(headers, DEVICE_COOKIE).take(MAX_COOKIE_VALUES).find_map(|token| auth.device_for(token, peer));
    let local = loopback && auth.local_key_matches(headers);
    let authenticated = (loopback && auth.config.trust_loopback && loopback_host(headers)) || local || device.is_some();
    let upgrade = headers.contains_key(header::UPGRADE);
    if (!request.method().is_safe() || upgrade) && cross_site(headers, loopback) {
        return forbidden(&path, peer);
    }
    if !authenticated && is_api(&path) && !is_open(&path) {
        return ApiError::new(401, "login required").into_response();
    }
    request.extensions_mut().insert(AuthContext { peer, authenticated, device: device.clone(), https });
    let mut response = next.run(request).await;
    // Other ports on this host (app UIs) are same-site: don't let them frame the dashboard.
    let headers = response.headers_mut();
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("frame-ancestors 'none'"));
    match device {
        Some(id) if is_stream(&response) => end_on_revoke(state.clone(), id, response),
        _ => response,
    }
}

fn forbidden(path: &str, peer: Option<IpAddr>) -> Response {
    tracing::warn!("auth: refused cross-site {path} from {}", peer_text(peer));
    ApiError::new(403, "cross-site request refused").into_response()
}

fn peer_text(peer: Option<IpAddr>) -> String {
    peer.map_or_else(|| "unknown peer".to_string(), |p| p.to_string())
}

/// A body of unknown length: SSE, the camera stream (whatever its type).
fn is_stream(response: &Response) -> bool {
    use axum::body::HttpBody;
    response.body().size_hint().exact().is_none()
}

/// End a device's SSE or camera stream as soon as the device is revoked.
fn end_on_revoke(state: Shared, id: String, response: Response) -> Response {
    let mut revoked = state.auth.revoked.subscribe();
    let gone = async move {
        while state.auth.has_device(&id) {
            if revoked.changed().await.is_err() {
                std::future::pending::<()>().await;
            }
        }
    };
    let (parts, body) = response.into_parts();
    Response::from_parts(parts, Body::from_stream(body.into_data_stream().take_until(Box::pin(gone))))
}

pub fn router() -> Router<Shared> {
    Router::new()
        .route("/api/auth/status", get(status))
        .route("/api/auth/pair", post(pair))
        .route("/api/auth/pair/{id}", get(poll))
        .route("/api/auth/approve", post(approve))
        .route("/api/auth/devices", get(list_devices))
        .route("/api/auth/devices/{id}", delete(revoke))
}

async fn status(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> Json<Value> {
    Json(json!({"lan": state.auth.config.lan, "authenticated": ctx.authenticated, "device": ctx.device}))
}

async fn pair(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Response> {
    let auth = &state.auth;
    if !auth.config.lan {
        return Err(ApiError::new(409, "LAN access is off"));
    }
    let peer = ctx.peer.unwrap_or(IpAddr::V4(Ipv4Addr::UNSPECIFIED));
    let loopback = peer.is_loopback();
    let limit = if loopback { LOOPBACK_PEER_LIMIT } else { PER_PEER_LIMIT };
    let now = Instant::now();
    let mut pending = auth.pending.lock().unwrap();
    pending.prune(now);
    if pending.recent.get(&rate_key(peer)).is_some_and(|t| t.len() >= limit) {
        tracing::warn!("auth: pair request from {peer} refused: {limit} in the last 10 minutes");
        return Err(ApiError::new(429, "too many pairing requests; try again later"));
    }
    if !loopback && pending.live(now) >= MAX_PENDING {
        tracing::warn!("auth: pair request from {peer} refused: {MAX_PENDING} requests pending");
        return Err(ApiError::new(429, "too many pairing requests pending; try again later"));
    }
    let code = loop {
        let code = random_code();
        if !pending.requests.iter().any(|r| r.code == code) {
            break code;
        }
    };
    let id = random_token::<16>();
    let secret = random_token::<32>();
    pending.requests.push(PairRequest {
        id: id.clone(),
        code: code.clone(),
        secret_sha256: sha256(secret.as_bytes()),
        peer,
        expires: now + auth.config.pair_ttl,
        attempts: 0,
        approved: None,
    });
    pending.recent.entry(rate_key(peer)).or_default().push_back(now);
    drop(pending);
    tracing::info!("auth: pair request {id} from {peer}");
    let ttl = auth.config.pair_ttl.as_secs();
    let cookie = cookie_header(PAIR_COOKIE, &secret, &format!("/api/auth/pair/{id}"), ttl + 120, ctx.https);
    Ok(([(header::SET_COOKIE, cookie)], Json(json!({"id": id, "code": code, "expires_in": ttl}))).into_response())
}

async fn poll(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    UrlPath(id): UrlPath<String>,
    headers: HeaderMap,
) -> ApiResult<Response> {
    let auth = &state.auth;
    let now = Instant::now();
    let mut pending = auth.pending.lock().unwrap();
    pending.prune(now);
    let index = pending.requests.iter().position(|r| {
        r.id == id && cookies(&headers, PAIR_COOKIE).any(|s| ct_eq(&sha256(s.as_bytes()), &r.secret_sha256))
    });
    let Some(index) = index else {
        tracing::warn!("auth: poll for unknown pair request (or without its browser secret) from {}", peer_text(ctx.peer));
        return Err(ApiError::new(404, "unknown pair request"));
    };
    let status = pending.requests[index].status(now);
    if status != "approved" {
        return Ok(Json(json!({"status": status})).into_response());
    }
    // First poll after approval: issue the device token to this browser. The
    // pending lock is held until the device is saved, so one token at most.
    let request = &pending.requests[index];
    let token = random_token::<32>();
    let created = now_unix();
    let mut devices = auth.devices.lock().unwrap();
    let device_id = loop {
        let candidate = hex(&random_bytes::<6>());
        if !devices.iter().any(|d| d.id == candidate) {
            break candidate;
        }
    };
    devices.push(Device {
        id: device_id.clone(),
        name: request.approved.clone().unwrap_or_default(),
        token_sha256: hex(&sha256(token.as_bytes())),
        created,
        last_seen: created,
        peer: request.peer.to_string(),
    });
    if let Err(error) = auth.save(&devices) {
        devices.pop();
        tracing::error!("auth: cannot save {DEVICES_FILE} ({error})");
        return Err(ApiError::new(500, "could not save the paired device"));
    }
    drop(devices);
    let request = pending.requests.remove(index);
    drop(pending);
    tracing::info!("auth: device {device_id} paired from {}", request.peer);
    let mut response = Json(json!({"status": "approved", "device": device_id})).into_response();
    let cookies = response.headers_mut();
    cookies.append(header::SET_COOKIE, cookie_header(DEVICE_COOKIE, &token, "/", DEVICE_MAX_AGE, ctx.https));
    cookies.append(header::SET_COOKIE, cookie_header(PAIR_COOKIE, "", &format!("/api/auth/pair/{id}"), 0, ctx.https));
    Ok(response)
}

#[derive(Deserialize)]
struct ApproveBody {
    code: String,
    name: Option<String>,
}

async fn approve(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<ApproveBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let by = ctx.device.as_deref().map_or_else(|| peer_text(ctx.peer), |d| format!("device {d}"));
    let code = body.code.trim();
    if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ApiError::new(400, "the code is 6 digits"));
    }
    let now = Instant::now();
    let mut pending = state.auth.pending.lock().unwrap();
    pending.prune(now);
    // Check every open request, without stopping early, against the code.
    let mut matched = None;
    for (index, request) in pending.requests.iter().enumerate() {
        if ct_eq(request.code.as_bytes(), code.as_bytes()) && request.status(now) == "pending" {
            matched = Some(index);
        }
    }
    let Some(index) = matched else {
        // The code could have been meant for any open request: count it against all.
        for request in pending.requests.iter_mut().filter(|r| r.status(now) == "pending") {
            request.attempts += 1;
            if request.attempts >= MAX_ATTEMPTS {
                tracing::warn!("auth: pair request {} locked after {MAX_ATTEMPTS} wrong codes", request.id);
            }
        }
        tracing::warn!("auth: wrong pairing code from {by}");
        return Err(ApiError::new(400, "no pending pairing request has that code"));
    };
    let request = &mut pending.requests[index];
    let name: String = body.name.as_deref().map(str::trim).unwrap_or("").chars().filter(|c| !c.is_control()).take(64).collect();
    let name = if name.is_empty() { format!("device at {}", request.peer) } else { name };
    request.approved = Some(name.clone());
    request.expires = request.expires.max(now + APPROVED_GRACE);
    tracing::info!("auth: pair request {} from {} approved by {by} as {name:?}", request.id, request.peer);
    Ok(Json(json!({"status": "approved", "name": name, "peer": request.peer.to_string()})))
}

async fn list_devices(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> Json<Value> {
    let devices = state.auth.devices.lock().unwrap();
    let list: Vec<Value> = devices
        .iter()
        .map(|d| {
            json!({
                "id": d.id,
                "name": d.name,
                "created": d.created,
                "last_seen": d.last_seen,
                "peer": d.peer,
                "current": ctx.device.as_deref() == Some(d.id.as_str()),
            })
        })
        .collect();
    Json(json!({"devices": list}))
}

async fn revoke(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    UrlPath(id): UrlPath<String>,
) -> ApiResult<StatusCode> {
    let auth = &state.auth;
    let mut devices = auth.devices.lock().unwrap();
    let Some(index) = devices.iter().position(|d| d.id == id) else {
        return Err(ApiError::new(404, "unknown device"));
    };
    let removed = devices.remove(index);
    if let Err(error) = auth.save(&devices) {
        devices.insert(index, removed);
        tracing::error!("auth: cannot save {DEVICES_FILE} ({error})");
        return Err(ApiError::new(500, "could not save the device list"));
    }
    drop(devices);
    auth.revoked.send_modify(|n| *n += 1);
    let by = ctx.device.as_deref().map_or_else(|| peer_text(ctx.peer), |d| format!("device {d}"));
    tracing::info!("auth: device {id} ({}) revoked by {by}", removed.name);
    Ok(StatusCode::NO_CONTENT)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_six_digits() {
        for _ in 0..1000 {
            let code = random_code();
            assert_eq!(code.len(), 6);
            assert!(code.bytes().all(|b| b.is_ascii_digit()));
        }
    }

    #[test]
    fn apps_cannot_set_the_controllers_cookies() {
        let mut h = HeaderMap::new();
        h.append(header::SET_COOKIE, "theme=dark; Path=/".parse().unwrap());
        h.append(header::SET_COOKIE, "rb_device=evil; Path=/; Max-Age=999".parse().unwrap());
        h.append(header::SET_COOKIE, " rb_pair=x".parse().unwrap());
        strip_credential_cookies(&mut h);
        assert_eq!(h.get_all(header::SET_COOKIE).iter().collect::<Vec<_>>(), ["theme=dark; Path=/"]);
    }

    #[test]
    fn credentials_are_stripped_for_apps() {
        let mut h = HeaderMap::new();
        h.append(header::COOKIE, "a=1; rb_device=t; b=2".parse().unwrap());
        h.append(header::COOKIE, "rb_pair=p".parse().unwrap());
        h.insert(LOCAL_KEY_HEADER, "k".parse().unwrap());
        strip_credentials(&mut h);
        assert_eq!(h.get_all(header::COOKIE).iter().collect::<Vec<_>>(), ["a=1; b=2"]);
        assert!(!h.contains_key(LOCAL_KEY_HEADER));
        let mut only = HeaderMap::new();
        only.insert(header::COOKIE, "rb_device=t".parse().unwrap());
        strip_credentials(&mut only);
        assert!(!only.contains_key(header::COOKIE));
    }

    #[test]
    fn cross_site_rules() {
        let mut h = HeaderMap::new();
        assert!(!cross_site(&h, false), "no browser headers: curl or the CLI");
        h.insert(header::HOST, "10.0.0.2:7861".parse().unwrap());
        h.insert(header::ORIGIN, "http://10.0.0.2:7861".parse().unwrap());
        assert!(!cross_site(&h, false));
        h.insert(header::ORIGIN, "http://10.0.0.2:8042".parse().unwrap());
        assert!(cross_site(&h, false), "an app UI on another port");
        h.insert("x-forwarded-host", "10.0.0.2:8042".parse().unwrap());
        assert!(cross_site(&h, false), "X-Forwarded-Host is ignored from a LAN peer");
        h.insert("sec-fetch-site", "same-origin".parse().unwrap());
        assert!(!cross_site(&h, false));
        h.insert("sec-fetch-site", "same-site".parse().unwrap());
        assert!(cross_site(&h, false));
    }

    #[test]
    fn ipv4_mapped_loopback_is_loopback() {
        let mapped: IpAddr = "::ffff:127.0.0.1".parse().unwrap();
        assert!(mapped.to_canonical().is_loopback());
        let mapped_lan: IpAddr = "::ffff:192.168.1.5".parse().unwrap();
        assert!(!mapped_lan.to_canonical().is_loopback());
    }
}
