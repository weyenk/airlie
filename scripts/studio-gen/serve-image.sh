#!/bin/zsh
# Re-enable the image LaunchAgent (flux2-klein-4b on :8001).
set -euo pipefail
LABEL=ai.rapidmlx.gen
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
[[ -f "${PLIST}" ]] || { echo "missing ${PLIST}; run bootstrap.sh first" >&2; exit 1; }
# Stop a foreground video serve if any
pkill -f "rapid-mlx serve wan" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true
echo "image LaunchAgent started; log: ~/.local/state/rapid-mlx/gen.log"
