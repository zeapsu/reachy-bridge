//! reachy-bridge controller: one binary serving the dashboard and the
//! controller API in front of the local Reachy daemon (#144).
//!
//! Environment: `DAEMON_URL` (default `http://127.0.0.1:8000`),
//! `DAEMON_TIMEOUT` seconds (2), `CONTROLLER_HOST` (127.0.0.1),
//! `CONTROLLER_PORT` (7861), `CONTROLLER_APP_PORT` (7863: the running app's
//! UI proxy, on loopback, or every interface in LAN mode), `REACHY_DAEMON_UNIT`
//! (`reachy-bridge.service`), `REACHY_BRIDGE_STATE_DIR`, `RUST_LOG` (info).
//! `SIM_DAEMON_URL` (`http://127.0.0.1:8001`) and `SIM_DAEMON_UNIT`
//! (`reachy-bridge-sim.service`) for sim mode (#149).
//! `CONTROLLER_LAN=1` serves the dashboard and the app UI proxy on every
//! interface behind a device login; `CONTROLLER_TRUST_TAILNET=0` makes loopback peers log in too (#141).

use std::time::Duration;

use reachy_controller::auth::AuthConfig;
use reachy_controller::daemon::Daemon;
use reachy_controller::logs::{follow_journal, ControllerLogLayer, LogBroker};
use reachy_controller::sim::SimMode;
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
/// address is refused. Without LAN mode only loopback: anything else would
/// let the network drive the robot with no login.
fn bind_host(lan: bool, host: Option<String>) -> Result<String, String> {
    match host {
        Some(host) if !lan && is_loopback_host(&host) => Ok(host),
        Some(host) if !lan => Err(format!(
            "CONTROLLER_HOST={host} without CONTROLLER_LAN=1 would serve the dashboard with no login; \
             set CONTROLLER_LAN=1 for LAN access"
        )),
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
    let sim = SimMode::from_env(&daemon_url);
    let sim_url = sim.sim_url().to_string();
    let state = AppState::with_sim(daemon, logs.clone(), state_dir, Duration::from_millis(300), auth, sim);
    tokio::spawn(reachy_controller::sim::stop_leftover(state.clone()));
    tokio::spawn(reachy_controller::audio::end_leftover(state.clone()));
    tokio::spawn(reachy_controller::datasets::prefetch(state.clone()));
    tokio::spawn(follow_journal(logs, env_or("REACHY_DAEMON_UNIT", "reachy-bridge.service")));

    let host = bind_host(lan, std::env::var("CONTROLLER_HOST").ok().filter(|h| !h.is_empty()))
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
    let address = format!("{host}:{}", env_or("CONTROLLER_PORT", "7861"));
    let listener = tokio::net::TcpListener::bind(&address).await?;
    tracing::info!("controller listening on http://{address}");
    if lan {
        tracing::info!("LAN access on: devices other than this one must pair (reachy-bridge pair <code>)");
    } else if std::env::var("CONTROLLER_TRUST_TAILNET").is_ok_and(|v| !v.trim().is_empty()) {
        tracing::warn!("CONTROLLER_TRUST_TAILNET only applies with CONTROLLER_LAN=1; ignoring it");
    }
    // The app UI proxy (#158): loopback only, or every interface with the
    // dashboard in LAN mode, behind the same login. Apps themselves stay on
    // loopback (the runtime's app_bind), so it is the LAN's only way to them (#186).
    let app_host = if lan { host.as_str() } else { "127.0.0.1" };
    let app_address = format!("{app_host}:{}", reachy_controller::app_proxy::http_port());
    // A busy port costs the app UIs over HTTPS, not the dashboard.
    let app_listener = match tokio::net::TcpListener::bind(&app_address).await {
        Ok(app_listener) => {
            tracing::info!("app UI proxy listening on http://{app_address}");
            if let Ok(address) = app_listener.local_addr() {
                reachy_controller::app_proxy::set_listening(address.port());
            }
            Some(app_listener)
        }
        Err(error) => {
            tracing::error!(%error, "app UI proxy not started on {app_address}: app UIs won't open over HTTPS");
            None
        }
    };
    // Graceful shutdown waits for every connection, and the dashboard's SSE
    // and camera streams (and app UI streams) never end on their own: give
    // both listeners 3 s, then return.
    // Ports the app proxy never forwards to, whatever an app's URL says: the
    // controller's own and the daemon's.
    let dashboard_port = listener.local_addr()?.port();
    let mut own_ports = vec![dashboard_port];
    own_ports.extend(app_listener.as_ref().and_then(|l| l.local_addr().ok()).map(|a| a.port()));
    for url in [&daemon_url, &sim_url] {
        own_ports.extend(reqwest::Url::parse(url).ok().and_then(|url| url.port_or_known_default()));
    }
    // Nor the daemon's WebRTC signalling: it is on loopback so the LAN can't
    // reach it, and an app's manifest mustn't re-expose it through here (#186).
    own_ports.push(reachy_controller::app_proxy::SIGNALLING_PORT);
    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    let stopped = |mut rx: tokio::sync::watch::Receiver<bool>| async move {
        let _ = rx.wait_for(|stop| *stop).await;
    };
    let app = reachy_controller::app(state.clone()).into_make_service_with_connect_info::<std::net::SocketAddr>();
    let server = axum::serve(listener, app).with_graceful_shutdown(stopped(stop_rx.clone()));
    let app_server = app_listener.map(|app_listener| {
        let app_proxy = reachy_controller::app_proxy::router(state, dashboard_port, &own_ports)
            .into_make_service_with_connect_info::<std::net::SocketAddr>();
        axum::serve(app_listener, app_proxy).with_graceful_shutdown(stopped(stop_rx.clone()))
    });
    tokio::spawn(async move {
        shutdown_signal().await;
        tracing::info!("shutting down");
        let _ = stop_tx.send(true);
    });
    tokio::select! {
        result = async {
            match app_server {
                Some(app_server) => tokio::try_join!(server.into_future(), app_server.into_future()).map(|_| ()),
                None => server.await,
            }
        } => result,
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
        assert_eq!(bind_host(false, Some("localhost".into())).unwrap(), "localhost");
        assert!(bind_host(false, Some("0.0.0.0".into())).is_err(), "no login without LAN mode");
        assert!(bind_host(false, Some("192.168.1.10".into())).is_err());
        assert_eq!(bind_host(true, None).unwrap(), "0.0.0.0");
        assert_eq!(bind_host(true, Some("127.0.0.1".into())).unwrap(), "0.0.0.0");
        assert_eq!(bind_host(true, Some("::".into())).unwrap(), "[::]");
        assert!(bind_host(true, Some("192.168.1.10".into())).is_err());
    }
}
