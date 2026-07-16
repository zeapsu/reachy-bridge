#!/usr/bin/env bash
#
# Build the GStreamer `webrtcsink` element (gst-plugins-rs) for this Jetson.
#
# The Reachy Mini daemon's media server requires the `webrtcsink` element from
# gst-plugins-rs (the GStreamer "Rust" plugins). It is NOT packaged for L4T /
# Ubuntu 22.04 aarch64, so we build it from source. GStreamer here is 1.20.3,
# so we use the gst-plugins-rs `0.10` branch (newer branches need GStreamer
# >= 1.22). We build only the webrtc plugin and install it into the per-user
# GStreamer plugin dir, so no root is needed for the build/install step.
#
# Prerequisite (run once, needs sudo -- the ONLY privileged step):
#   sudo apt-get update
#   sudo apt-get install -y libgstreamer-plugins-bad1.0-dev libssl-dev
#
# Then run this script (no sudo):
#   bash tools/build_webrtcsink_jetson.sh
#
set -euo pipefail

SRC_DIR="${GST_RS_SRC:-/tmp/gst-plugins-rs}"
BRANCH="0.10"
PLUGIN_DIR="$HOME/.local/share/gstreamer-1.0/plugins"
SO_NAME="libgstrswebrtc.so"

echo "==> Checking prerequisites"
command -v cargo >/dev/null || { echo "ERROR: cargo/rust not found"; exit 1; }
if ! pkg-config --exists gstreamer-webrtc-1.0; then
  echo "ERROR: gstreamer-webrtc-1.0 dev headers missing."
  echo "       Run: sudo apt-get install -y libgstreamer-plugins-bad1.0-dev libssl-dev"
  exit 1
fi
if ! pkg-config --exists openssl; then
  echo "ERROR: openssl dev headers missing."
  echo "       Run: sudo apt-get install -y libssl-dev"
  exit 1
fi

echo "==> Fetching gst-plugins-rs ($BRANCH) into $SRC_DIR"
if [ ! -d "$SRC_DIR/.git" ]; then
  git clone --depth 1 --branch "$BRANCH" \
    https://github.com/GStreamer/gst-plugins-rs.git "$SRC_DIR"
fi

echo "==> Building gst-plugin-webrtc (release) -- this can take 10-30 min"
cd "$SRC_DIR"
cargo build --release -p gst-plugin-webrtc

BUILT_SO="$SRC_DIR/target/release/$SO_NAME"
[ -f "$BUILT_SO" ] || { echo "ERROR: build did not produce $BUILT_SO"; exit 1; }

echo "==> Verifying the built plugin exposes webrtcsink"
gst-inspect-1.0 "$BUILT_SO" | grep -q webrtcsink \
  || { echo "ERROR: built plugin does not register webrtcsink"; exit 1; }

echo "==> Installing to user plugin dir: $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp -f "$BUILT_SO" "$PLUGIN_DIR/"

echo "==> Verifying GStreamer can load webrtcsink from the registry"
if gst-inspect-1.0 webrtcsink >/dev/null 2>&1; then
  echo "SUCCESS: webrtcsink is available."
else
  echo "webrtcsink not yet found on the default search path."
  echo "Export GST_PLUGIN_PATH to include it, e.g. add to your shell rc:"
  echo "  export GST_PLUGIN_PATH=\"$PLUGIN_DIR:\$GST_PLUGIN_PATH\""
fi
