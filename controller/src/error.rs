//! API errors, answered as `{"detail": "..."}` like the FastAPI controller.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::daemon::DaemonError;

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub detail: String,
}

pub type ApiResult<T> = Result<T, ApiError>;

impl ApiError {
    pub fn new(status: u16, detail: impl Into<String>) -> Self {
        Self {
            status: StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
            detail: detail.into(),
        }
    }

    pub fn unavailable() -> Self {
        Self::new(503, "Reachy daemon unavailable")
    }

    pub fn bad_body() -> Self {
        Self::new(400, "Invalid request body")
    }
}

impl From<DaemonError> for ApiError {
    /// Pass the daemon's own status and detail through; no answer is a 503.
    fn from(error: DaemonError) -> Self {
        match error {
            DaemonError::Unavailable => Self::unavailable(),
            DaemonError::Refused { status, detail } => {
                Self::new(status, detail.unwrap_or_else(|| "Daemon refused".to_string()))
            }
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({"detail": self.detail}))).into_response()
    }
}
