#!/usr/bin/env bash
#
# Build the GStreamer `webrtcsink` element (gst-plugins-rs) for an arm64
# Ubuntu 24.04 device (JetPack 7) or for a release.
#
# The Reachy Mini daemon's media server needs `webrtcsink` with
# `run-signalling-server`, which gst-plugins-rs added after 0.13. It is not
# packaged for Ubuntu 24.04 (GStreamer 1.24.2), so it is built from source.
# Releases attach the plugin built by this script on ubuntu-24.04-arm (#132),
# so devices only download it; building on a Jetson takes about 7 minutes.
#
# Prerequisites (once, with sudo):
#   sudo apt-get install -y libgstreamer-plugins-bad1.0-dev libssl-dev pkg-config gstreamer1.0-tools
# and Rust (rustup).
#
# Usage:
#   bash tools/build_webrtcsink_jetson.sh             build and install into ~/.local/share/gstreamer-1.0/plugins
#   bash tools/build_webrtcsink_jetson.sh --out DIR   build and copy the plugin to DIR only (release builds)
#
# GST_RS_VERSION picks the gst-plugins-rs tag (default below); GST_RS_SRC the
# checkout directory.
#
set -euo pipefail

VERSION="${GST_RS_VERSION:-0.14.5}"
SRC_DIR="${GST_RS_SRC:-${TMPDIR:-/tmp}/gst-plugins-rs-$VERSION}"
PLUGIN_DIR="$HOME/.local/share/gstreamer-1.0/plugins"
SO_NAME="libgstrswebrtc.so"
OUT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
    *) echo "usage: $0 [--out DIR]" >&2; exit 2 ;;
  esac
done

echo "==> Checking prerequisites"
command -v cargo >/dev/null || { echo "ERROR: cargo/rust not found"; exit 1; }
command -v gst-inspect-1.0 >/dev/null || { echo "ERROR: gst-inspect-1.0 not found (gstreamer1.0-tools)"; exit 1; }
command -v strip >/dev/null || { echo "ERROR: strip not found (binutils)"; exit 1; }
for module in gstreamer-webrtc-1.0 openssl; do
  if ! pkg-config --exists "$module"; then
    echo "ERROR: $module dev headers missing."
    echo "       Run: sudo apt-get install -y libgstreamer-plugins-bad1.0-dev libssl-dev"
    exit 1
  fi
done

echo "==> Fetching gst-plugins-rs $VERSION into $SRC_DIR"
if [ ! -d "$SRC_DIR/.git" ]; then
  git clone --depth 1 --branch "$VERSION" \
    https://github.com/GStreamer/gst-plugins-rs.git "$SRC_DIR"
fi
checked_out="$(git -C "$SRC_DIR" describe --tags --exact-match 2>/dev/null || true)"
[ "$checked_out" = "$VERSION" ] \
  || { echo "ERROR: $SRC_DIR is at '${checked_out:-an untagged commit}', not $VERSION; remove it or set GST_RS_SRC"; exit 1; }

echo "==> Building gst-plugin-webrtc (release)"
(cd "$SRC_DIR" && cargo build --release --locked -p gst-plugin-webrtc)

BUILT_SO="$SRC_DIR/target/release/$SO_NAME"
[ -f "$BUILT_SO" ] || { echo "ERROR: build did not produce $BUILT_SO"; exit 1; }

echo "==> Verifying the plugin registers webrtcsink with run-signalling-server"
check_dir="$(mktemp -d)"
trap 'rm -rf "$check_dir"' EXIT
# The unstripped plugin is about 140 MB, mostly debug info; strip a copy.
cp "$BUILT_SO" "$check_dir/"
BUILT_SO="$check_dir/$SO_NAME"
strip --strip-unneeded "$BUILT_SO"
inspect="$(GST_REGISTRY="$check_dir/registry.bin" GST_PLUGIN_PATH="$check_dir" gst-inspect-1.0 webrtcsink || true)"
case "$inspect" in
  *run-signalling-server*) ;;
  *) echo "ERROR: built plugin has no webrtcsink with run-signalling-server"; exit 1 ;;
esac

if [ -n "$OUT_DIR" ]; then
  mkdir -p "$OUT_DIR"
  cp -f "$BUILT_SO" "$OUT_DIR/"
  echo "SUCCESS: $OUT_DIR/$SO_NAME (gst-plugins-rs $VERSION)"
  exit 0
fi

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
