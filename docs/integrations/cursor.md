# Using airlie's models in Cursor

Cursor can talk to any OpenAI-compatible endpoint through **Settings → Models → API Keys → "Override OpenAI Base
URL"**. One constraint shapes the setup: Cursor's chat and agent requests leave from Cursor's own backend, not from
your machine, so `127.0.0.1` is unreachable and the role server must be reachable from the internet through a
tunnel. That is why the server takes an API key.

Tab autocomplete and code indexing stay on Cursor's models regardless; the override covers chat, plan and agent.

## 1. Engines and the role server

```bash
task restart -- --topology studio          # vllm-mlx (base, generalist, reviewer…) + Kev
task serve -- --api-key <choose-a-secret>  # roles and catalog models on 127.0.0.1:8090, bearer auth on /v1
```

`task serve` exposes every role (`coder`, `architect`, `chief`, `qa`, …: prompt prepended) and every catalog
model (`base`, `generalist`, `small`, …: raw) under `/v1/models`.

## 2. Tunnel

```bash
task tunnel
```

A Cloudflare quick tunnel prints a `https://<random>.trycloudflare.com` URL; it changes every start. (`ngrok http
8090` works the same way.) Anyone with that URL and the secret can use your models; stop the tunnel when done.

## 3. Cursor

Settings → Models:

1. **API Keys → OpenAI API Key**: the secret from step 1.
2. **Override OpenAI Base URL**: `https://<random>.trycloudflare.com/v1` (no `/chat/completions`; Cursor appends it).
3. **Add custom model** for each name you want in the picker: `coder` (agent mode: tool calling, the coder prompt),
   `generalist` (Qwen3.8, tools + vision), `architect` (plans), `base` (raw coder model). Cursor routes every
   non-Claude, non-Gemini model name to the override URL.
4. Pick the model in chat; use `coder` for agent mode.

## Verify from the shell

```bash
curl -s https://<random>.trycloudflare.com/v1/models -H "Authorization: Bearer <secret>" | head -c 300
```

The request shows up in the `task serve` terminal as `POST /v1/chat/completions`, and in the engine log.
