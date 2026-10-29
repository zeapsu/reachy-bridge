//! Controller login for LAN access (#141) and the admin (#221).
//!
//! Off by default: with `CONTROLLER_LAN` unset and no admin there is no
//! login, and the guard only refuses cross-site state changes. In LAN mode a
//! request is authenticated when its TCP peer is loopback (`tailscale serve`
//! and local tools; `CONTROLLER_TRUST_TAILNET=0` turns that off), when it
//! carries a paired device's `rb_device` cookie, or when a loopback peer
//! sends the local key the controller writes to
//! `$REACHY_BRIDGE_STATE_DIR/controller.key` (how `reachy-bridge pair` works
//! when loopback isn't trusted). Unauthenticated API calls get 401; pages are
//! served so the dashboard can show its login screen.
//!
//! Admin mode (#221) starts when the first admin device pairs, which only the
//! local key can approve (`reachy-bridge pair --admin`). From then on every
//! browser logs in, LAN mode or not, and loopback is no longer trusted: a
//! request through `tailscale serve` carries no identity, and control time is
//! granted per person. Only an admin (or the local key) approves and revokes
//! devices; a member can log out its own. Admin mode is kept in
//! `devices.json`, so it stays on when the last admin device expires or is
//! revoked; the CLI pairs a new one.
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

/// What a paired device may do (#221).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    /// Approves and revokes devices, and (with #221) grants control time.
    Admin,
    #[default]
    Member,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Device {
    id: String,
    name: String,
    /// Who uses the device: control time is granted per person, and one
    /// person can have several devices. Older files have none: the name.
    #[serde(default)]
    person: String,
    #[serde(default)]
    role: Role,
    /// Hex SHA-256 of the cookie token; the token itself is never stored.
    token_sha256: String,
    created: u64,
    last_seen: u64,
    /// Peer address at pairing, then at the last recorded visit.
    peer: String,
}

#[derive(Default, Serialize, Deserialize)]
struct DevicesFile {
    /// Set once an admin has paired; never cleared by the controller.
    #[serde(default)]
    admin_mode: bool,
    devices: Vec<Device>,
}

/// An approved pairing request, waiting for its browser to collect the token.
#[derive(Clone)]
struct Approval {
    name: String,
    person: String,
    role: Role,
}

struct PairRequest {
    id: String,
    code: String,
    secret_sha256: [u8; 32],
    peer: IpAddr,
    expires: Instant,
    attempts: u32,
    approved: Option<Approval>,
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
    admin_mode: std::sync::atomic::AtomicBool,
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
    /// That device's person and role.
    pub person: Option<String>,
    pub role: Option<Role>,
    /// A loopback peer sent the local key (the CLI on the robot).
    pub local: bool,
    /// What authenticated the request, for ending its long-lived connections.
    pub credential: Option<Credential>,
    /// The request reached `tailscale serve` over HTTPS.
    pub https: bool,
}

/// What a connection was authenticated with. A stream or WebSocket ends as
/// soon as that stops counting: the device is revoked, or (for a loopback
/// visitor trusted without a device) admin mode turns on.
#[derive(Clone, Debug)]
pub enum Credential {
    /// The local key: the CLI on the robot.
    Local,
    Device(String),
    /// No login needed, or loopback trusted without one.
    Trusted,
}

impl AuthContext {
    /// May approve and revoke devices: the CLI, an admin device, or anyone
    /// authenticated before an admin exists (the #141 rule).
    fn manages_devices(&self, admin_mode: bool) -> bool {
        self.local || self.role == Some(Role::Admin) || (!admin_mode && self.authenticated)
    }

    fn by(&self) -> String {
        match (&self.device, self.local) {
            (Some(d), _) => format!("device {d}"),
            (None, true) => "the local key".to_string(),
            (None, false) => peer_text(self.peer),
        }
    }
}

/// A paired device, as a request's cookie identifies it.
struct Paired {
    id: String,
    person: String,
    role: Role,
}

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs())
}

fn random_bytes<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    getrandom::fill(&mut bytes).expect("operating system random number generator");
    bytes
}

pub(crate) fn random_token<const N: usize>() -> String {
    URL_SAFE_NO_PAD.encode(random_bytes::<N>())
}

/// A uniform 6-digit code (rejection sampling avoids modulo bias).
pub(crate) fn random_code() -> String {
    const LIMIT: u32 = u32::MAX - u32::MAX % 1_000_000;
    loop {
        let n = u32::from_le_bytes(random_bytes::<4>());
        if n < LIMIT {
            return format!("{:06}", n % 1_000_000);
        }
    }
}

/// A random id of `n` bytes, as hex.
pub fn random_hex(n: usize) -> String {
    let mut bytes = vec![0u8; n];
    getrandom::fill(&mut bytes).expect("operating system random number generator");
    hex(&bytes)
}

pub(crate) fn sha256(data: &[u8]) -> [u8; 32] {
    Sha256::digest(data).into()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Equal-length comparison whose time doesn't depend on where bytes differ.
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Write `bytes` to `path` atomically (temp file, fsync, rename), mode 0600.
pub fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
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

/// Whether a (possibly damaged) devices file says admin mode was on.
fn mentions_admin(bytes: &[u8]) -> bool {
    let compact: Vec<u8> = bytes.iter().copied().filter(|b| !b.is_ascii_whitespace()).collect();
    [&b"\"role\":\"admin\""[..], b"\"admin_mode\":true"].iter().any(|needle| compact.windows(needle.len()).any(|w| w == *needle))
}

/// Every value of cookie `name` across the request's `Cookie` headers.
pub(crate) fn cookies<'a>(headers: &'a HeaderMap, name: &'a str) -> impl Iterator<Item = &'a str> + 'a {
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
/// that isn't text is dropped too, and so is a nameless cookie
/// (`=rb_device=x`): browsers send it back as `rb_device=x`.
pub fn strip_credential_cookies(headers: &mut HeaderMap) {
    let kept: Vec<HeaderValue> = headers
        .get_all(header::SET_COOKIE)
        .iter()
        .filter(|v| {
            let pair = v.to_str().ok().and_then(|v| v.split(';').next()).unwrap_or("");
            let name = pair.split_once('=').map_or("", |(name, _)| name).trim();
            !name.is_empty() && name != DEVICE_COOKIE && name != PAIR_COOKIE && name != crate::setup::SETUP_COOKIE
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
            !pair.is_empty() && name != DEVICE_COOKIE && name != PAIR_COOKIE && name != crate::setup::SETUP_COOKIE
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

pub(crate) fn cookie_header(name: &str, value: &str, path: &str, max_age: u64, https: bool) -> HeaderValue {
    let secure = if https { "; Secure" } else { "" };
    HeaderValue::from_str(&format!("{name}={value}; HttpOnly; SameSite=Strict; Path={path}; Max-Age={max_age}{secure}"))
        .expect("cookie values are base64url")
}

impl Auth {
    pub fn new(config: AuthConfig, state_dir: &Path) -> Self {
        let devices_path = state_dir.join(DEVICES_FILE);
        let file = match std::fs::read(&devices_path) {
            Ok(bytes) => match serde_json::from_slice::<DevicesFile>(&bytes) {
                Ok(file) => file,
                Err(error) => {
                    let aside = devices_path.with_extension("json.bad");
                    let _ = std::fs::rename(&devices_path, &aside);
                    tracing::error!(
                        "auth: {} is unreadable ({error}); moved it to {} and starting with no paired devices",
                        devices_path.display(),
                        aside.display()
                    );
                    // A file that may have held an admin keeps admin mode on.
                    DevicesFile { admin_mode: mentions_admin(&bytes), devices: Vec::new() }
                }
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => DevicesFile::default(),
            Err(error) => {
                tracing::error!("auth: cannot read {} ({error}); starting with no paired devices", devices_path.display());
                DevicesFile { admin_mode: true, devices: Vec::new() }
            }
        };
        let admin_mode = file.admin_mode || file.devices.iter().any(|d| d.role == Role::Admin);
        // Written in every mode: `reachy-bridge pair --admin` needs it.
        let key = random_token::<32>();
        let local_key = match write_private(&state_dir.join(LOCAL_KEY_FILE), format!("{key}\n").as_bytes()) {
            Ok(()) => Some(key),
            Err(error) => {
                tracing::error!("auth: cannot write {LOCAL_KEY_FILE} ({error}); `reachy-bridge pair` needs a trusted loopback");
                None
            }
        };
        Self {
            config,
            devices_path,
            devices: Mutex::new(file.devices),
            pending: Mutex::new(Pending::default()),
            local_key,
            admin_mode: admin_mode.into(),
            revoked: tokio::sync::watch::channel(0).0,
        }
    }

    /// An admin has paired (see the module docs).
    pub fn admin_mode(&self) -> bool {
        self.admin_mode.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// Every request logs in: LAN mode, or admin mode.
    pub fn login_required(&self) -> bool {
        self.config.lan || self.admin_mode()
    }

    fn save(&self, devices: &[Device]) -> std::io::Result<()> {
        let body = serde_json::to_vec_pretty(&json!({"admin_mode": self.admin_mode(), "devices": devices})).expect("devices serialize");
        write_private(&self.devices_path, &body)
    }

    /// The device a cookie token belongs to; refreshes its `last_seen`.
    fn device_for(&self, token: &str, peer: Option<IpAddr>) -> Option<Paired> {
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
            // Its open streams and sockets end too.
            self.revoked.send_modify(|n| *n += 1);
            return None;
        }
        let device = &mut devices[index];
        // Devices paired before #221 have no person: the name stands in.
        let person = if device.person.is_empty() { device.name.clone() } else { device.person.clone() };
        let paired = Paired { id: device.id.clone(), person, role: device.role };
        if now.saturating_sub(device.last_seen) >= LAST_SEEN_STEP {
            device.last_seen = now;
            if let Some(peer) = peer {
                device.peer = peer.to_string();
            }
            if let Err(error) = self.save(&devices) {
                tracing::warn!("auth: cannot record last_seen ({error})");
            }
        }
        Some(paired)
    }

    /// Device id -> name, for showing who holds control.
    pub fn device_names(&self) -> HashMap<String, String> {
        self.devices.lock().unwrap().iter().map(|d| (d.id.clone(), d.name.clone())).collect()
    }

    /// Some paired device belongs to `person`.
    pub fn has_person(&self, person: &str) -> bool {
        self.devices.lock().unwrap().iter().any(|d| d.person == person || (d.person.is_empty() && d.name == person))
    }

    fn has_device(&self, id: &str) -> bool {
        self.devices.lock().unwrap().iter().any(|d| d.id == id)
    }

    /// Loopback (tailscale serve) logs in without a device: only before an
    /// admin exists, and unless `CONTROLLER_TRUST_TAILNET=0`.
    fn trusts_loopback(&self) -> bool {
        self.config.trust_loopback && !self.admin_mode()
    }

    fn local_key_matches(&self, headers: &HeaderMap) -> bool {
        let (Some(key), Some(sent)) = (&self.local_key, headers.get(LOCAL_KEY_HEADER)) else {
            return false;
        };
        ct_eq(key.as_bytes(), sent.as_bytes())
    }

    /// Whether the app UI proxy may serve a request, by the dashboard's rule.
    /// Without LAN mode its listener is loopback only and, before an admin
    /// exists, everyone is trusted. In LAN mode it listens on every interface
    /// (#186): loopback peers (tailscale serve) are trusted unless
    /// `CONTROLLER_TRUST_TAILNET=0` or admin mode is on, and only under a
    /// loopback or tailnet Host; everyone else needs a paired device, or the
    /// local key from loopback.
    pub fn app_proxy_allowed(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> bool {
        self.app_proxy_credential(headers, peer).is_some()
    }

    /// [`Self::app_proxy_allowed`], with what let the request in.
    pub fn app_proxy_credential(&self, headers: &HeaderMap, peer: Option<IpAddr>) -> Option<Credential> {
        if !self.login_required() {
            return Some(Credential::Trusted);
        }
        let loopback = peer.is_some_and(|p| p.is_loopback());
        if loopback && self.local_key_matches(headers) {
            return Some(Credential::Local);
        }
        if let Some(device) = cookies(headers, DEVICE_COOKIE).take(MAX_COOKIE_VALUES).find_map(|t| self.device_for(t, peer)) {
            return Some(Credential::Device(device.id));
        }
        (loopback && self.trusts_loopback() && loopback_host(headers)).then_some(Credential::Trusted)
    }

    fn still_counts(&self, credential: &Credential) -> bool {
        match credential {
            Credential::Local => true,
            Credential::Device(id) => self.has_device(id),
            Credential::Trusted => !self.admin_mode(),
        }
    }

    /// Resolves once `credential` stops counting (see [`Credential`]).
    pub async fn ended(&self, credential: Credential) {
        let mut changed = self.revoked.subscribe();
        while self.still_counts(&credential) {
            if changed.changed().await.is_err() {
                std::future::pending::<()>().await;
            }
        }
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

pub fn is_api(path: &str) -> bool {
    path == "/api" || path.starts_with("/api/") || path == "/telemetry" || path.starts_with("/telemetry/")
}

/// Changes only an admin (or the CLI) may make in admin mode: installing
/// code (an app runs as the bridge's user and can read the local key, so a
/// member installing one could make itself admin), the Hugging Face token,
/// and the kinematics engine.
pub fn admin_only(method: &axum::http::Method, path: &str) -> bool {
    if method.is_safe() {
        return false;
    }
    [
        "/api/apps/install",
        "/api/apps/install-private",
        "/api/apps/update/",
        "/api/apps/remove/",
        "/api/hf/token",
        "/api/daemon/kinematics",
    ]
    .iter()
    .any(|p| if p.ends_with('/') { path.starts_with(p) } else { path == *p })
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

    let local = loopback && auth.local_key_matches(headers);
    if !auth.login_required() {
        // No login, but another site still can't make a tailnet browser act
        // on the robot (a plain form POST to /api/daemon/start).
        if (!request.method().is_safe() || headers.contains_key(header::UPGRADE)) && cross_site(headers, loopback) {
            return forbidden(&path, peer);
        }
        // A DNS-rebinding page reaches loopback same-origin under its own
        // name, so the API answers loopback only under a name such a page
        // can't produce (installing apps made this matter, #126).
        if loopback && headers.contains_key(header::HOST) && !loopback_host(headers) && (is_api(&path) || headers.contains_key(header::UPGRADE)) {
            return forbidden(&path, peer);
        }
        let credential = if local { Credential::Local } else { Credential::Trusted };
        let ctx = AuthContext { peer, authenticated: true, device: None, person: None, role: None, local, credential: Some(credential.clone()), https };
        request.extensions_mut().insert(ctx);
        let response = next.run(request).await;
        return end_with(state.clone(), credential, response);
    }

    let device = cookies(headers, DEVICE_COOKIE).take(MAX_COOKIE_VALUES).find_map(|token| auth.device_for(token, peer));
    let trusted = loopback && auth.trusts_loopback() && loopback_host(headers);
    let authenticated = trusted || local || device.is_some();
    let credential = match &device {
        _ if local => Some(Credential::Local),
        Some(device) => Some(Credential::Device(device.id.clone())),
        None => trusted.then_some(Credential::Trusted),
    };
    let upgrade = headers.contains_key(header::UPGRADE);
    if (!request.method().is_safe() || upgrade) && cross_site(headers, loopback) {
        return forbidden(&path, peer);
    }
    if !authenticated && is_api(&path) && !is_open(&path) {
        return ApiError::new(401, "login required").into_response();
    }
    if auth.admin_mode() && !local && device.as_ref().is_none_or(|d| d.role != Role::Admin) && admin_only(request.method(), &path) {
        tracing::warn!("auth: refused {path} from {}: admin only", peer_text(peer));
        return ApiError::new(403, "only an admin can do this").into_response();
    }
    request.extensions_mut().insert(AuthContext {
        peer,
        authenticated,
        device: device.as_ref().map(|d| d.id.clone()),
        person: device.as_ref().map(|d| d.person.clone()),
        role: device.as_ref().map(|d| d.role),
        local,
        credential: credential.clone(),
        https,
    });
    let mut response = next.run(request).await;
    // Other ports on this host (app UIs) are same-site: don't let them frame the dashboard.
    let headers = response.headers_mut();
    headers.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("frame-ancestors 'none'"));
    match credential {
        Some(credential) => end_with(state.clone(), credential, response),
        None => response,
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

/// End an SSE or camera stream (any body of unknown length) as soon as the
/// credential it was opened with stops counting.
pub fn end_with(state: Shared, credential: Credential, response: Response) -> Response {
    if matches!(credential, Credential::Local) || !is_stream(&response) {
        return response;
    }
    let gone = async move { state.auth.ended(credential).await };
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
    let auth = &state.auth;
    Json(json!({
        "lan": auth.config.lan,
        "login_required": auth.login_required(),
        "admin_mode": auth.admin_mode(),
        "authenticated": ctx.authenticated,
        "device": ctx.device,
        "person": ctx.person,
        "role": ctx.role,
    }))
}

async fn pair(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> ApiResult<Response> {
    // Open in every mode: the admin pairs first, before any login (#221).
    let auth = &state.auth;
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
    let approval = request.approved.clone().expect("approved");
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
        name: approval.name,
        person: approval.person,
        role: approval.role,
        token_sha256: hex(&sha256(token.as_bytes())),
        created,
        last_seen: created,
        peer: request.peer.to_string(),
    });
    // The first admin turns admin mode on, for good (see the module docs),
    // and logs out every device paired before it: anyone could approve those.
    let was_admin_mode = auth.admin_mode.swap(auth.admin_mode() || approval.role == Role::Admin, std::sync::atomic::Ordering::SeqCst);
    let before = devices.clone();
    if !was_admin_mode && approval.role == Role::Admin {
        devices.retain(|d| d.id == device_id);
    }
    if let Err(error) = auth.save(&devices) {
        *devices = before;
        devices.pop();
        auth.admin_mode.store(was_admin_mode, std::sync::atomic::Ordering::SeqCst);
        tracing::error!("auth: cannot save {DEVICES_FILE} ({error})");
        return Err(ApiError::new(500, "could not save the paired device"));
    }
    drop(devices);
    let request = pending.requests.remove(index);
    if !was_admin_mode && approval.role == Role::Admin {
        // Requests approved before the admin existed (by anyone) can't be
        // collected any more; their browsers get a new code.
        pending.requests.clear();
    }
    drop(pending);
    tracing::info!("auth: device {device_id} paired from {} as {:?}", request.peer, approval.role);
    if !was_admin_mode && approval.role == Role::Admin {
        // Ends streams and sockets opened without a device, and those of the
        // devices just logged out.
        auth.revoked.send_modify(|n| *n += 1);
        tracing::warn!(
            "auth: admin mode is on: every browser now logs in, tailnet included; {} earlier device(s) logged out",
            before.len() - 1
        );
    }
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
    /// Defaults to the device name.
    person: Option<String>,
    #[serde(default)]
    role: Role,
}

/// A name or person as typed: trimmed, no control characters, at most 64.
fn label(text: Option<&str>) -> String {
    text.map(str::trim).unwrap_or("").chars().filter(|c| !c.is_control()).take(64).collect()
}

async fn approve(
    State(state): State<Shared>,
    Extension(ctx): Extension<AuthContext>,
    body: Result<Json<ApproveBody>, axum::extract::rejection::JsonRejection>,
) -> ApiResult<Json<Value>> {
    let Json(body) = body.map_err(|_| ApiError::bad_body())?;
    let by = ctx.by();
    // An admin only through the CLI on the robot: a stolen admin browser
    // can't mint more admins, and the first admin needs the robot's shell.
    if body.role == Role::Admin && !ctx.local {
        tracing::warn!("auth: admin approval refused for {by}: needs the local key");
        return Err(ApiError::new(403, "only `reachy-bridge pair --admin` on the robot can approve an admin"));
    }
    if !ctx.manages_devices(state.auth.admin_mode()) {
        tracing::warn!("auth: approval refused for {by}: not an admin");
        return Err(ApiError::new(403, "only an admin can approve devices"));
    }
    state.auth.approve_code(&body.code, body.name.as_deref(), body.person.as_deref(), body.role, &by, ctx.local).map(Json)
}

impl Auth {
    /// The address of the open (pending, or locked by wrong codes) pair
    /// request with `code`, without approving it or counting a try: the
    /// setup page shows it on the terminal before anyone approves (#135).
    pub fn pending_peer(&self, code: &str) -> Option<IpAddr> {
        let now = Instant::now();
        let mut pending = self.pending.lock().unwrap();
        pending.prune(now);
        let mut found = None;
        for request in &pending.requests {
            if ct_eq(request.code.as_bytes(), code.trim().as_bytes()) && matches!(request.status(now), "pending" | "locked") {
                found = Some(request.peer);
            }
        }
        found
    }

    /// Approve the open pair request with `code` as `role`. `holds_key`: the
    /// caller proved it is on the robot (the local key, or the setup page's
    /// terminal code, #135), so it can't be guessing: it may approve a
    /// request others locked with wrong codes, and its typos lock nothing.
    /// Who may approve, and an admin, is the caller's check.
    pub fn approve_code(&self, code: &str, name: Option<&str>, person: Option<&str>, role: Role, by: &str, holds_key: bool) -> ApiResult<Value> {
        let code = code.trim();
        if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
            return Err(ApiError::new(400, "the code is 6 digits"));
        }
        let now = Instant::now();
        let mut pending = self.pending.lock().unwrap();
        pending.prune(now);
        // Check every open request, without stopping early, against the code.
        let mut matched = None;
        let open = |r: &PairRequest| matches!(r.status(now), "pending") || (holds_key && r.status(now) == "locked");
        for (index, request) in pending.requests.iter().enumerate() {
            if ct_eq(request.code.as_bytes(), code.as_bytes()) && open(request) {
                matched = Some(index);
            }
        }
        let Some(index) = matched else {
            if holds_key {
                return Err(ApiError::new(400, "no pending pairing request has that code"));
            }
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
        let name = label(name);
        let name = if name.is_empty() { format!("device at {}", request.peer) } else { name };
        let person = label(person);
        let person = if person.is_empty() { name.clone() } else { person };
        request.approved = Some(Approval { name: name.clone(), person: person.clone(), role });
        request.attempts = 0;
        request.expires = request.expires.max(now + APPROVED_GRACE);
        tracing::info!("auth: pair request {} from {} approved by {by} as {name:?} ({person:?}, {role:?})", request.id, request.peer);
        Ok(json!({"status": "approved", "name": name, "person": person, "role": role, "peer": request.peer.to_string()}))
    }
}

async fn list_devices(State(state): State<Shared>, Extension(ctx): Extension<AuthContext>) -> Json<Value> {
    // A member sees its own device only: the list carries peer addresses.
    let all = ctx.manages_devices(state.auth.admin_mode());
    let devices = state.auth.devices.lock().unwrap();
    let list: Vec<Value> = devices
        .iter()
        .filter(|d| all || ctx.device.as_deref() == Some(d.id.as_str()))
        .map(|d| {
            json!({
                "id": d.id,
                "name": d.name,
                "person": if d.person.is_empty() { &d.name } else { &d.person },
                "role": d.role,
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
    let admin_mode = auth.admin_mode();
    let own = ctx.device.as_deref() == Some(id.as_str());
    if !own && !ctx.manages_devices(admin_mode) {
        return Err(ApiError::new(403, "only an admin can revoke other devices"));
    }
    let mut devices = auth.devices.lock().unwrap();
    let Some(index) = devices.iter().position(|d| d.id == id) else {
        return Err(ApiError::new(404, "unknown device"));
    };
    // The dashboard can't lock everyone out; the CLI can (and re-pairs).
    let admins = devices.iter().filter(|d| d.role == Role::Admin).count();
    if devices[index].role == Role::Admin && admins == 1 && !ctx.local {
        return Err(ApiError::new(409, "this is the last admin device; pair another admin first, or revoke it with `reachy-bridge devices revoke` on the robot"));
    }
    let removed = devices.remove(index);
    if let Err(error) = auth.save(&devices) {
        devices.insert(index, removed);
        tracing::error!("auth: cannot save {DEVICES_FILE} ({error})");
        return Err(ApiError::new(500, "could not save the device list"));
    }
    drop(devices);
    auth.revoked.send_modify(|n| *n += 1);
    let by = ctx.by();
    tracing::info!("auth: device {id} ({}) revoked by {by}", removed.name);
    Ok(StatusCode::NO_CONTENT)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// LAN-mode auth with one paired device whose cookie token is "paired".
    fn lan_auth(trust_loopback: bool) -> (Auth, PathBuf) {
        let dir = std::env::temp_dir().join(format!("rc-auth-unit-{}-{}", std::process::id(), random_token::<8>()));
        std::fs::create_dir_all(&dir).unwrap();
        let auth = Auth::new(AuthConfig { lan: true, trust_loopback, ..AuthConfig::default() }, &dir);
        auth.devices.lock().unwrap().push(Device {
            id: "d1".into(),
            name: "phone".into(),
            person: "phone".into(),
            role: Role::Member,
            token_sha256: hex(&sha256(b"paired")),
            created: now_unix(),
            last_seen: now_unix(),
            peer: "192.168.1.20".into(),
        });
        (auth, dir)
    }

    fn request(host: &str, cookie: Option<&str>, key: Option<&str>) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert(header::HOST, host.parse().unwrap());
        if let Some(cookie) = cookie {
            h.insert(header::COOKIE, format!("{DEVICE_COOKIE}={cookie}").parse().unwrap());
        }
        if let Some(key) = key {
            h.insert(LOCAL_KEY_HEADER, key.parse().unwrap());
        }
        h
    }

    #[test]
    fn app_proxy_on_the_lan_needs_a_paired_device() {
        let (auth, dir) = lan_auth(true);
        let lan: IpAddr = "192.168.1.20".parse().unwrap();
        let loopback: IpAddr = "127.0.0.1".parse().unwrap();
        let key = auth.local_key.clone().unwrap();
        // An unpaired LAN visitor is refused, even with a loopback Host or the key.
        assert!(!auth.app_proxy_allowed(&request("192.168.1.5:7863", None, None), Some(lan)));
        assert!(!auth.app_proxy_allowed(&request("127.0.0.1:7863", None, None), Some(lan)));
        assert!(!auth.app_proxy_allowed(&request("192.168.1.5:7863", None, Some(&key)), Some(lan)));
        assert!(!auth.app_proxy_allowed(&request("192.168.1.5:7863", Some("wrong"), None), Some(lan)));
        assert!(!auth.app_proxy_allowed(&request("192.168.1.5:7863", None, None), None));
        // A paired device is let in from anywhere.
        assert!(auth.app_proxy_allowed(&request("192.168.1.5:7863", Some("paired"), None), Some(lan)));
        // tailscale serve (loopback) is trusted under a tailnet or loopback Host only.
        assert!(auth.app_proxy_allowed(&request("robot.tail1234.ts.net:8043", None, None), Some(loopback)));
        assert!(auth.app_proxy_allowed(&request("127.0.0.1:7863", None, None), Some(loopback)));
        assert!(!auth.app_proxy_allowed(&request("evil.example:7863", None, None), Some(loopback)));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn app_proxy_with_an_untrusted_tailnet_needs_a_login_from_loopback_too() {
        let (auth, dir) = lan_auth(false);
        let loopback: IpAddr = "127.0.0.1".parse().unwrap();
        let key = auth.local_key.clone().unwrap();
        assert!(!auth.app_proxy_allowed(&request("robot.tail1234.ts.net:8043", None, None), Some(loopback)));
        assert!(auth.app_proxy_allowed(&request("127.0.0.1:7863", None, Some(&key)), Some(loopback)));
        assert!(auth.app_proxy_allowed(&request("robot.tail1234.ts.net:8043", Some("paired"), None), Some(loopback)));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn app_proxy_without_lan_mode_is_open_on_its_loopback_listener() {
        let dir = std::env::temp_dir().join(format!("rc-auth-unit-off-{}", std::process::id()));
        let auth = Auth::new(AuthConfig::default(), &dir);
        assert!(auth.app_proxy_allowed(&request("127.0.0.1:7863", None, None), Some("127.0.0.1".parse().unwrap())));
    }

    #[test]
    fn a_damaged_file_keeps_admin_mode_only_if_it_had_one() {
        assert!(mentions_admin(b"{\"admin_mode\": true, \"devices\": [{\"x"));
        assert!(mentions_admin(b"{\"admin_mode\": false, \"devices\": [{\"role\": \"admin\", \"x"));
        assert!(!mentions_admin(b"{\"admin_mode\": false, \"devices\": [{\"role\": \"member\", \"x"));
        assert!(!mentions_admin(b"{not json"));
    }

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
        h.append(header::SET_COOKIE, "=rb_device=evil; Path=/api".parse().unwrap());
        h.append(header::SET_COOKIE, "rb_device; Path=/api/x".parse().unwrap());
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
