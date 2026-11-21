#!/bin/zsh
# Called by LaunchAgent / serve-*.sh. Reads ~/.config/rapid-mlx/gen.env.
set -euo pipefail
setopt noglob
ENV_FILE="${HOME}/.config/rapid-mlx/gen.env"
[[ -f "${ENV_FILE}" ]] || { echo "missing ${ENV_FILE}" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
MODEL="${1:-${RAPID_MLX_IMAGE_MODEL}}"
HOST="${RAPID_MLX_HOST:-0.0.0.0}"
PORT="${RAPID_MLX_PORT:-8001}"
# Word-split trusted hosts without globbing [::1]
typeset -a TRUSTED
TRUSTED=(${=RAPID_MLX_TRUSTED_HOSTS})
exec rapid-mlx serve "${MODEL}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --api-key "${RAPID_MLX_API_KEY}" \
  --trusted-hosts "${TRUSTED[@]}"
