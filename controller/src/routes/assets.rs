//! The dashboard, embedded from `web/dist` at build time.

use axum::http::{StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use rust_embed::RustEmbed;

#[derive(RustEmbed)]
#[folder = "../web/dist"]
struct Dist;

/// Serve an embedded file; `/` is the dashboard. Vite's hashed `/assets/*`
/// are immutable; HTML is revalidated so a new build shows up on reload.
pub async fn serve(uri: Uri) -> Response {
    let path = uri.path().trim_start_matches('/');
    let path = if path.is_empty() { "index.html" } else { path };
    let Some(file) = Dist::get(path) else {
        if path == "index.html" {
            return (StatusCode::NOT_FOUND, "Dashboard not built: run `bun run build` in web/, then rebuild.")
                .into_response();
        }
        return StatusCode::NOT_FOUND.into_response();
    };
    let cache = if path.starts_with("assets/") { "public, max-age=31536000, immutable" } else { "no-cache" };
    (
        [
            (header::CONTENT_TYPE, mime_guess::from_path(path).first_or_octet_stream().to_string()),
            (header::CACHE_CONTROL, cache.to_string()),
        ],
        file.data,
    )
        .into_response()
}
