#!/usr/bin/env bash
# Remove reachy-bridge from this device (#142). The same as
# `reachy-bridge uninstall`; see that command for what it removes.
#
#   deploy/uninstall.sh [--keep-data] [--dry-run] [--yes]
#
#   --keep-data  keep installed apps, settings and paired devices for a reinstall
#   --dry-run    print what would be removed and change nothing
#   --yes        don't ask for confirmation
set -euo pipefail
exec "$(dirname "${BASH_SOURCE[0]}")/reachy-bridge" uninstall "$@"
