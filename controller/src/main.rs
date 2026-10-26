//! reachy-bridge controller: one binary serving the dashboard and the
//! controller API in front of the local Reachy daemon (#144).
//!
//! Environment: `DAEMON_URL` (default `http://127.0.0.1:8000`),
//! `DAEMON_TIMEOUT` seconds (2), `CONTROLLER_HOST` (127.0.0.1),
//! `CONTROLLER_PORT` (7861), `CONTROLLER_APP_PORT` (7863: the running app's
//! UI proxy, always on loopback), `REACHY_DAEMON_UNIT`
//! (`reachy-bridge.service`), `REACHY_BRIDGE_STATE_DIR`, `RUST_LOG` (info).
//! `CONTROLLER_LAN=1` serves the dashboard on every interface behind a device
//! login; `CONTROLLER_TRUST_TAILNET=0` makes loopback peers log in too (#141).

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

/// The dashboard's bind address: `CONTROLLER_HOST` (default loopback). In
/// LAN mode every interface (`0.0.0.0`, or `::` if set): loopback must stay
/// bound for `tailscale serve` and `reachy-bridge pair`, so a single LAN
/// address is refused.
fn bind_host(lan: bool, host: Option<String>) -> Result<String, String> {
    match host {
        Some(host) if !lan => Ok(host),
        None => Ok(if lan { "0.0.0.0" } else { "127.0.0.1" }.to_string()),
        Some(host) if is_loopback_host(&host) || host == "0.0.0.0" => Ok("0.0.0.0".to_string()),
        Some(host) if host == "::" || host == "[::]" => Ok("[::]".to_string()),
        Some(host) => Err(format!(
            "CONTROLLER_LAN=1 with CONTROLLER_HOST={host} would leave loopback unbound (tailscale serve, \
             reachy-bridge pair); unset CONTROLLER_HOST or use 0.0.0.0"
        )),
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
    let daemon_url = env_or("DAEMON_URL", "http://127.0.0.1:8000");
    let daemon = Daemon::new(&daemon_url, Duration::from_secs_f64(timeout));
    let auth = AuthConfig::from_env();
    let lan = auth.lan;
    let state = AppState::with_auth(daemon, logs.clone(), state_dir, Duration::from_millis(300), auth);
    tokio::spawn(reachy_controller::datasets::prefetch(state.clone()));
    tokio::spawn(follow_journal(logs, env_or("REACHY_DAEMON_UNIT", "reachy-bridge.service")));

    let host = bind_host(lan, std::env::var("CONTROLLER_HOST").ok().filter(|h| !h.is_empty()))
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
    let address = format!("{host}:{}", env_or("CONTROLLER_PORT", "7861"));
    let listener = tokio::net::TcpListener::bind(&address).await?;
    tracing::info!("controller listening on http://{address}");
    if lan {
        tracing::info!("LAN access on: devices other than this one must pair (reachy-bridge pair <code>)");
    } else if !is_loopback_host(&host) {
        tracing::warn!("CONTROLLER_HOST={host} is not loopback and CONTROLLER_LAN is off: no login is required");
    }
    // The app UI proxy (#158) binds loopback only, whatever CONTROLLER_HOST says.
    let app_address = format!("127.0.0.1:{}", env_or("CONTROLLER_APP_PORT", "7863"));
    let app_listener = tokio::net::TcpListener::bind(&app_address).await?;
    tracing::info!("app UI proxy listening on http://{app_address}");
    // Graceful shutdown waits for every connection, and the dashboard's SSE
    // and camera streams (and app UI streams) never end on their own: give
    // both listeners 3 s, then return.
    // Ports the app proxy never forwards to, whatever an app's URL says: the
    // controller's own and the daemon's.
    let mut own_ports = vec![listener.local_addr()?.port(), app_listener.local_addr()?.port()];
    own_ports.extend(reqwest::Url::parse(&daemon_url).ok().and_then(|url| url.port_or_known_default()));
    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    let stopped = |mut rx: tokio::sync::watch::Receiver<bool>| async move {
        let _ = rx.wait_for(|stop| *stop).await;
    };
    let app = reachy_controller::app(state.clone()).into_make_service_with_connect_info::<std::net::SocketAddr>();
    let server = axum::serve(listener, app).with_graceful_shutdown(stopped(stop_rx.clone()));
    let app_proxy = reachy_controller::app_proxy::router(state, &own_ports)
        .into_make_service_with_connect_info::<std::net::SocketAddr>();
    let app_server = axum::serve(app_listener, app_proxy).with_graceful_shutdown(stopped(stop_rx.clone()));
    tokio::spawn(async move {
        shutdown_signal().await;
        tracing::info!("shutting down");
        let _ = stop_tx.send(true);
    });
    tokio::select! {
        result = async { tokio::try_join!(server.into_future(), app_server.into_future()).map(|_| ()) } => result,
        () = async {
            stopped(stop_rx).await;
            tokio::time::sleep(Duration::from_secs(3)).await;
        } => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::bind_host;

    #[test]
    fn lan_mode_keeps_loopback_bound() {
        assert_eq!(bind_host(false, None).unwrap(), "127.0.0.1");
        assert_eq!(bind_host(false, Some("0.0.0.0".into())).unwrap(), "0.0.0.0");
        assert_eq!(bind_host(true, None).unwrap(), "0.0.0.0");
        assert_eq!(bind_host(true, Some("127.0.0.1".into())).unwrap(), "0.0.0.0");
        assert_eq!(bind_host(true, Some("::".into())).unwrap(), "[::]");
        assert!(bind_host(true, Some("192.168.1.10".into())).is_err());
    }
}
