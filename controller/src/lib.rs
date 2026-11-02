pub mod app_proxy;
pub mod apps;
pub mod audio;
pub mod auth;
pub mod control;
pub mod daemon;
pub mod datasets;
pub mod error;
pub mod expressions;
pub mod host;
pub mod logs;
pub mod routes;
pub mod setup;
pub mod sim;
pub mod state;
pub mod streams;

use axum::Router;
use axum::extract::Request;
use axum::middleware::{self, Next};
use axum::response::Response;

use crate::state::Shared;

/// Log every request that can act on the robot, with its outcome.
async fn log_actions(request: Request, next: Next) -> Response {
    let method = request.method().clone();
    let path = request.uri().path().to_string();
    let response = next.run(request).await;
    let status = response.status();
    if status.is_server_error() {
        tracing::warn!("{method} {path} -> {}", status.as_u16());
    } else if method != axum::http::Method::GET {
        tracing::info!("{method} {path} -> {}", status.as_u16());
    }
    response
}

pub fn app(state: Shared) -> Router {
    Router::new()
        .merge(routes::power::router())
        .merge(routes::apps::router())
        .merge(audio::router())
        .merge(routes::controls::router())
        .merge(routes::expressions::router())
        .merge(routes::telemetry::router())
        .merge(routes::camera::router())
        .merge(routes::logs::router())
        .merge(routes::drive::router())
        .merge(routes::store::router())
        .merge(auth::router())
        .merge(control::router())
        .merge(sim::router())
        .merge(routes::sounds::router())
        .merge(setup::router())
        .fallback(routes::assets::serve)
        .layer(middleware::from_fn_with_state(state.clone(), sim::guard))
        // After auth::guard (layers run outermost first): needs its AuthContext.
        .layer(middleware::from_fn_with_state(state.clone(), control::guard))
        .layer(middleware::from_fn_with_state(state.clone(), auth::guard))
        .layer(middleware::from_fn(log_actions))
        .with_state(state)
}
