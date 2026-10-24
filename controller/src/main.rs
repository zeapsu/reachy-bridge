//! reachy-bridge controller: one binary serving the dashboard and the
//! controller API in front of the local Reachy daemon (#144).
//!
//! Environment: `DAEMON_URL` (default `http://127.0.0.1:8000`),
//! `DAEMON_TIMEOUT` seconds (2), `CONTROLLER_HOST` (127.0.0.1),
//! `CONTROLLER_PORT` (7861), `REACHY_DAEMON_UNIT` (`reachy-bridge.service`),
//! `REACHY_BRIDGE_STATE_DIR`, `RUST_LOG` (info). `CONTROLLER_LAN=1` serves
//! the dashboard on the LAN behind a device login (`0.0.0.0`, or a
//! non-loopback `CONTROLLER_HOST`); `CONTROLLER_TRUST_TAILNET=0` makes
//! loopback peers log in too (#141).

use std::time::Duration;

use reachy_controller::auth::AuthConfig;
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::{follow_journal, ControllerLogLayer, LogBroker};
use reachy_controller::state::{state_dir, AppState};
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::EnvFilter;

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

fn is_loopback_host(host: &str) -> bool {
    host == "localhost" || host.trim_matches(['[', ']']).parse::<std::net::IpAddr>().is_ok_and(|ip| ip.to_canonical().is_loopback())
}

/// The dashboard's bind address: `CONTROLLER_HOST` (default loopback); in
/// LAN mode a non-loopback `CONTROLLER_HOST`, else every IPv4 interface.
fn bind_host(lan: bool, host: Option<String>) -> String {
    match host {
        Some(host) if !lan || !is_loopback_host(&host) => host,
        _ if lan => "0.0.0.0".to_string(),
        _ => "127.0.0.1".to_string(),
    }
}

async fn shutdown_signal() {
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let logs = LogBroker::new();
    let state_dir = state_dir();
    tracing_subscriber::registry()
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .with(tracing_subscriber::fmt::layer())
        .with(ControllerLogLayer::new(logs.clone(), Some(state_dir.join("logs/controller.log"))))
        .init();

    let timeout = env_or("DAEMON_TIMEOUT", "2.0").parse::<f64>().unwrap_or(2.0);
    let daemon = Daemon::new(&env_or("DAEMON_URL", "http://127.0.0.1:8000"), Duration::from_secs_f64(timeout));
    let auth = AuthConfig::from_env();
    let lan = auth.lan;
    let state = AppState::with_auth(daemon, logs.clone(), state_dir, Duration::from_millis(300), auth);
    tokio::spawn(reachy_controller::datasets::prefetch(state.clone()));
    tokio::spawn(follow_journal(logs, env_or("REACHY_DAEMON_UNIT", "reachy-bridge.service")));

    let host = bind_host(lan, std::env::var("CONTROLLER_HOST").ok().filter(|h| !h.is_empty()));
    let address = format!("{host}:{}", env_or("CONTROLLER_PORT", "7861"));
    let listener = tokio::net::TcpListener::bind(&address).await?;
    tracing::info!("controller listening on http://{address}");
    if lan {
        tracing::info!("LAN access on: devices other than this one must pair (reachy-bridge pair <code>)");
    } else if !is_loopback_host(&host) {
        tracing::warn!("CONTROLLER_HOST={host} is not loopback and CONTROLLER_LAN is off: no login is required");
    }
    // Graceful shutdown waits for every connection, and the dashboard's SSE
    // and camera streams never end on their own: give them 3 s, then return.
    let stopping = std::sync::Arc::new(tokio::sync::Notify::new());
    let signalled = stopping.clone();
    let app = reachy_controller::app(state).into_make_service_with_connect_info::<std::net::SocketAddr>();
    let server = axum::serve(listener, app).with_graceful_shutdown(async move {
        shutdown_signal().await;
        tracing::info!("shutting down");
        signalled.notify_one();
    });
    tokio::select! {
        result = server => result,
        () = async {
            stopping.notified().await;
            tokio::time::sleep(Duration::from_secs(3)).await;
        } => Ok(()),
    }
}
