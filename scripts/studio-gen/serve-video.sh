#!/bin/zsh
# Stop the image LaunchAgent and serve Wan TI2V on :8001 (foreground).
# Ctrl-C stops video; then run serve-image.sh to restore.
#
# WARNING: Wan + omlx Flash Next OOM-rebooted the Studio (2026-11-21).
# Unload heavy omlx models (or stop omlx) before running this.
set -euo pipefail
setopt noglob
ENV_FILE="${HOME}/.config/rapid-mlx/gen.env"
[[ -f "${ENV_FILE}" ]] || { echo "missing ${ENV_FILE}; run bootstrap.sh first" >&2; exit 1; }
if [[ "${I_UNLOADED_OMLX:-}" != "1" ]]; then
  echo "Refusing to start video serve while I_UNLOADED_OMLX is unset." >&2
  echo "Unload Flash/Gemma in omlx (or stop omlx), then:" >&2
  echo "  I_UNLOADED_OMLX=1 $0" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
LABEL=ai.rapidmlx.gen
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
echo "serving ${RAPID_MLX_VIDEO_MODEL} on :${RAPID_MLX_PORT} (Ctrl-C to stop)"
exec "${RAPID_MLX_RUN:-$(dirname "$0")/run-serve.sh}" "${RAPID_MLX_VIDEO_MODEL}"
