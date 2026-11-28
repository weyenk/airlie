#!/bin/zsh
# Run ON the Mac Studio (once). Installs Rapid-MLX + ffmpeg, authorizes the Air
# MacBook SSH key if present, writes gen env, installs a LaunchAgent for image gen
# on :8001, starts it, and smoke-tests /v1/models.
set -euo pipefail

PIN="${RAPID_MLX_PIN:-1.1.0}"
PORT="${RAPID_MLX_PORT:-8001}"
IMAGE_MODEL="${RAPID_MLX_IMAGE_MODEL:-flux2-klein-4b}"
VIDEO_MODEL="${RAPID_MLX_VIDEO_MODEL:-wan2.2-ti2v-5b-q8}"
STATE_DIR="${HOME}/.local/state/rapid-mlx"
CONFIG_DIR="${HOME}/.config/rapid-mlx"
ENV_FILE="${CONFIG_DIR}/gen.env"
PLIST_LABEL="ai.rapidmlx.gen"
PLIST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PUBKEY_CANDIDATES=(
  "${SCRIPT_DIR}/air-macbook.pub"
  "${REPO_ROOT}/scripts/studio-gen/air-macbook.pub"
  "${HOME}/Library/Mobile Documents/com~apple~CloudDocs/Desktop/studio-gen/air-macbook.pub"
  "${HOME}/Desktop/studio-gen/air-macbook.pub"
)
# Optional local env (omlx key parity); never commit gen.env.local
for env_local in "${SCRIPT_DIR}/gen.env.local" "${REPO_ROOT}/scripts/studio-gen/gen.env.local"; do
  if [[ -f "${env_local}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_local}"
    set +a
    break
  fi
done

mkdir -p "${STATE_DIR}" "${CONFIG_DIR}" "${HOME}/.ssh" "${HOME}/Library/LaunchAgents"

echo "==> ffmpeg"
if ! command -v ffmpeg >/dev/null 2>&1; then
  brew install ffmpeg
else
  ffmpeg -version | head -1
fi

echo "==> rapid-mlx==${PIN}"
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:${PATH}"
if command -v uv >/dev/null 2>&1; then
  uv tool install --force "rapid-mlx==${PIN}"
else
  python3 -m pip install --user "rapid-mlx==${PIN}"
fi
RAPID_BIN="$(command -v rapid-mlx)"
[[ -n "${RAPID_BIN}" && -x "${RAPID_BIN}" ]] || {
  echo "rapid-mlx not on PATH after install" >&2
  exit 1
}
"${RAPID_BIN}" --version || true

echo "==> authorize Air MacBook SSH key (if present)"
AUTHORIZED="${HOME}/.ssh/authorized_keys"
touch "${AUTHORIZED}"
chmod 600 "${AUTHORIZED}"
for pub in "${PUBKEY_CANDIDATES[@]}"; do
  if ! key="$(cat "${pub}" 2>/dev/null | tr -d '\n')"; then
    [[ -e "${pub}" ]] && echo "    skip unreadable: ${pub}"
    continue
  fi
  [[ -n "${key}" ]] || continue
  if ! grep -qxF "${key}" "${AUTHORIZED}"; then
    print -r -- "${key}" >>"${AUTHORIZED}"
    echo "    added $(basename "${pub}")"
  else
    echo "    already present: $(basename "${pub}")"
  fi
done
chmod 700 "${HOME}/.ssh"

echo "==> gen.env (API key parity with omlx when GEN_API_KEY / RAPID_MLX_API_KEY set)"
# Prefer key already in env / gen.env.local; avoid sourcing a broken prior gen.env (zsh globs).
API_KEY="${RAPID_MLX_API_KEY:-${GEN_API_KEY:-}}"
if [[ -z "${API_KEY}" && -f "${ENV_FILE}" ]]; then
  API_KEY="$(noglob grep -E '^RAPID_MLX_API_KEY=' "${ENV_FILE}" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi
if [[ -z "${API_KEY}" ]]; then
  API_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
)"
  echo "    generated new RAPID_MLX_API_KEY (saved in ${ENV_FILE})"
fi
umask 077
cat >"${ENV_FILE}" <<EOF
# Managed by scripts/studio-gen/bootstrap.sh — do not commit.
RAPID_MLX_PIN=${PIN}
RAPID_MLX_PORT=${PORT}
RAPID_MLX_HOST=0.0.0.0
RAPID_MLX_API_KEY=${API_KEY}
RAPID_MLX_IMAGE_MODEL=${IMAGE_MODEL}
RAPID_MLX_VIDEO_MODEL=${VIDEO_MODEL}
# Hostnames clients send (Tailscale MagicDNS + loopback + LAN IP for homenet). Quoted for shell source.
RAPID_MLX_TRUSTED_HOSTS="localhost 127.0.0.1 mac-studio mac-studio.local mac-studio.taila5f450.ts.net 100.77.221.113 192.168.4.34"
RAPID_MLX_RUN=${SCRIPT_DIR}/run-serve.sh
EOF
chmod 600 "${ENV_FILE}"
chmod +x "${SCRIPT_DIR}/run-serve.sh" "${SCRIPT_DIR}/serve-image.sh" "${SCRIPT_DIR}/serve-video.sh"

echo "==> LaunchAgent ${PLIST_LABEL} (image model by default)"
# Stop previous
launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true

RUN_SERVE="${SCRIPT_DIR}/run-serve.sh"
cat >"${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Rapid-MLX image gen on :${PORT}. Video: scripts/studio-gen/serve-video.sh -->
<plist version="1.0">
<dict>
  <key>Label</key><string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${RUN_SERVE}</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${STATE_DIR}/gen.log</string>
  <key>StandardErrorPath</key><string>${STATE_DIR}/gen.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>${HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${HOME}/.local/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "${PLIST}"
launchctl enable "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || launchctl kickstart "gui/$(id -u)/${PLIST_LABEL}"

echo "==> waiting for http://127.0.0.1:${PORT}/v1/models"
for i in {1..90}; do
  if curl -fsS -m 2 -H "Authorization: Bearer ${API_KEY}" "http://127.0.0.1:${PORT}/v1/models" >/tmp/rapid-mlx-models.json 2>/dev/null; then
    echo "    ready after ${i}s"
    python3 - <<'PY'
import json
d=json.load(open("/tmp/rapid-mlx-models.json"))
ids=[m.get("id") for m in d.get("data", d if isinstance(d, list) else [])]
print("    models:", ids)
PY
    break
  fi
  sleep 2
  if [[ "${i}" -eq 90 ]]; then
    echo "    timed out; last log lines:"
    tail -40 "${STATE_DIR}/gen.log" || true
    exit 1
  fi
done

echo "==> image smoke (tiny)"
curl -fsS -m 600 -H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json" \
  "http://127.0.0.1:${PORT}/v1/images/generations" \
  -d "{\"model\":\"${IMAGE_MODEL}\",\"prompt\":\"a red apple on a white table, product photo\",\"size\":\"512x512\"}" \
  -o "${STATE_DIR}/smoke-image.json"
python3 - <<PY
import json
from pathlib import Path
p=Path("${STATE_DIR}/smoke-image.json")
d=json.loads(p.read_text())
data=d.get("data") or []
assert data, d
item=data[0]
ok=bool(item.get("b64_json") or item.get("url"))
print("    image ok:", ok, "keys:", sorted(item.keys()))
assert ok
PY

cat <<EOF

Bootstrap complete.
  base_url:  http://127.0.0.1:${PORT}/v1
  Tailscale: http://mac-studio:8001/v1  or  http://100.77.221.113:8001/v1
  image:     ${IMAGE_MODEL}
  video:     stop agent, then: rapid-mlx serve ${VIDEO_MODEL} --port ${PORT} --api-key \$RAPID_MLX_API_KEY ...
  env:       ${ENV_FILE}
  log:       ${STATE_DIR}/gen.log
  ssh:       from Air: ssh studio   (after this authorized_keys update)

Video swap (manual):
  launchctl bootout gui/\$(id -u)/${PLIST_LABEL}
  set -a; source ${ENV_FILE}; set +a
  rapid-mlx serve \${RAPID_MLX_VIDEO_MODEL} --host 0.0.0.0 --port \${RAPID_MLX_PORT} \\
    --api-key \${RAPID_MLX_API_KEY} --trusted-hosts \${=RAPID_MLX_TRUSTED_HOSTS}
EOF
