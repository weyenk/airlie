# Local image / video generation (Rapid-MLX)

Chat, vision, and audio stay on **omlx** (port **8000**). Image and video
**generation** run on a separate **Rapid-MLX** process (port **8001**). Clients
call OpenAI-shaped HTTP only. Airlie does not host or wrap diffusion tools.

```
clients ──chat/VLM/audio──► omlx :8000
clients ──images/videos───► rapid-mlx :8001
```

## Pin

| Item | Value |
| --- | --- |
| Package | `rapid-mlx==1.1.0` |
| Image (default LaunchAgent) | `flux2-klein-4b` |
| Video (swap manually) | `wan2.2-ti2v-5b-q8` |
| Port | `8001` |
| Env file on Studio | `~/.config/rapid-mlx/gen.env` |
| Log | `~/.local/state/rapid-mlx/gen.log` |
| LaunchAgent | `ai.rapidmlx.gen` |

Adult / fictional-adult NSFW is a product concern (checkpoint choice + your
app). Child-related content is forbidden; enforcement filters are a later pass.

## Install on the Mac Studio (once)

From a Terminal **on the Studio**, in this checkout (or after copying
`scripts/studio-gen/` over):

```bash
# Prefer the same API key as omlx (optional but handy for clients):
#   export GEN_API_KEY='…'   # omlx Bearer token
cd ~/git/airlie   # or your airlie path
chmod +x scripts/studio-gen/bootstrap.sh
./scripts/studio-gen/bootstrap.sh
```

That installs ffmpeg + Rapid-MLX, authorizes the Air MacBook SSH pubkey if
`scripts/studio-gen/air-macbook.pub` (or the iCloud Desktop copy) is present,
writes `gen.env`, loads the LaunchAgent, and smoke-tests image generation.

Interactive shell from the Air (survives sleep / IP changes):

```bash
studio   # alias: mosh --server=/opt/homebrew/bin/mosh-server studio
```

Non-interactive automation: `ssh studio '…'` (Host `studio` → `mac-studio` in
`~/.ssh/config`).

## Endpoints

Base URL (Studio loopback): `http://127.0.0.1:8001/v1`  
Over LAN (homenet): `http://192.168.4.34:8001/v1`  
Over Tailscale: `http://mac-studio:8001/v1` or `http://100.77.221.113:8001/v1`  
(omlx is the default MagicDNS target: `https://mac-studio.taila5f450.ts.net`
→ `127.0.0.1:8000`. Gen stays plain HTTP on **8001** so it does not fight
Tailscale Serve on 443. The Studio UI lives at `http://airlie.home.arpa` on
homenet, not on MagicDNS.)

`RAPID_MLX_TRUSTED_HOSTS` must include every hostname clients put in the HTTP
`Host` header (including `192.168.4.34` for the NAS Telegram bot). Otherwise
Rapid-MLX returns `400 Invalid host header`.

Send `Authorization: Bearer $RAPID_MLX_API_KEY` (from `gen.env`).

### Image

```bash
curl http://127.0.0.1:8001/v1/images/generations \
  -H "Authorization: Bearer $RAPID_MLX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"flux2-klein-4b","prompt":"…","size":"1024x1024"}'
```

### Video (async job)

Stop the image LaunchAgent first (one gen model resident at a time):

```bash
launchctl bootout "gui/$(id -u)/ai.rapidmlx.gen"
set -a; source ~/.config/rapid-mlx/gen.env; set +a
rapid-mlx serve "$RAPID_MLX_VIDEO_MODEL" --host 0.0.0.0 --port "$RAPID_MLX_PORT" \
  --api-key "$RAPID_MLX_API_KEY" --trusted-hosts ${=RAPID_MLX_TRUSTED_HOSTS}
```

Or: `./scripts/studio-gen/serve-video.sh`

```bash
curl http://127.0.0.1:8001/v1/videos \
  -H "Authorization: Bearer $RAPID_MLX_API_KEY" \
  -F model=wan2.2-ti2v-5b-q8 \
  -F 'prompt=…' -F seconds=1 -F size=832x512
# poll GET /v1/videos/{id} then GET /v1/videos/{id}/content
```

Restore image serve: `./scripts/studio-gen/serve-image.sh` (re-bootstraps the
LaunchAgent) or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.rapidmlx.gen.plist`.

## Memory vs omlx

With Flash Next + Gemma loaded (~88 GB), prefer **klein** only alongside chat.

**Video crashed this Mac Studio (2026-11-21):** serving `wan2.2-ti2v-5b-q8`
while omlx still held Flash Next (~79 GB resident) drove the machine into
reboot mid-job. Before `serve-video.sh`:

1. In omlx admin, **unload** Flash Next / Gemma (or stop omlx).
2. Confirm free memory (`memory_pressure` / Activity Monitor).
3. Then swap to Wan. Restore image LaunchAgent afterward with `serve-image.sh`.

PLE-on-SSD helps chat RAM; it does not shrink diffusion weights. Do not run
Wan A14B-class checkpoints next to pinned chat models on 128 GB.

## Airlie

No airlie engine spawn for gen. Optional later: HTTP client tools pointed at
`:8001` only. Existing `engines.image` / `engines.video` `weights_only` entries
are catalog leftovers, not this path.
