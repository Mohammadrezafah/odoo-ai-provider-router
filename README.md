# AI Provider Router

Route Odoo 20 Enterprise **Ask AI** completions to any OpenAI-compatible Chat
Completions API — OpenRouter, DeepSeek, OpenCode Go, or your own endpoint —
without touching the core `ai` module, the Discuss UI, or `/mail/message/post`.

## How it works

- Extends `ai.session`: `_save_and_submit_request` (async) and
  `_get_completions` (sync) route to the configured provider when enabled,
  and delegate unchanged to Odoo's native AI service when disabled.
- Maps Odoo's internal message parts (text, tool calls, tool results,
  structured output) to OpenAI chat-completions format and back, preserving
  native tools, confirmations, and the signed completion callback.
- Async responses are delivered through the existing HMAC-signed
  `/ai/completion_result_ready` callback — the agent loop, permissions, and
  company context stay entirely native.
- When enabled and the provider fails, the request fails explicitly —
  there is no silent fallback to Odoo IAP.

## Requirements

- Odoo **20.0** Enterprise with the `ai` module installed
- `requests` (already an Odoo dependency) — no extra packages

## Install

1. Clone this repo into your Odoo addons path.
2. Install module `ai_provider_router` (Apps → Update Apps List → install,
   or `odoo-bin module install ai_provider_router -d <db>`).
3. **Settings → General Settings → AI Provider Routing** (admin only):

   | Field | Example |
   |---|---|
   | Enable AI Provider Routing | on |
   | Provider Base URL | `https://openrouter.ai/api/v1` (default) |
   | Provider Model | e.g. `openai/gpt-5-mini` |
   | Provider API Key | your key (write-only, never shown again) |

4. Chat as usual in **Discuss → AI → + Chat**.

## Provider notes

- Base URL must be HTTPS (HTTP allowed only for `127.0.0.1`/`localhost`).
- The endpoint used is `<base URL>/chat/completions`.
- **OpenCode Go**: use `https://opencode.ai/zen/go/v1` and a model served via
  chat/completions (`glm-5.3-flash`, `kimi-k2.6`, `deepseek-v4.1-flash`, …).
  The addon automatically sends the required `x-opencode-session` header and
  a client User-Agent for `opencode.ai` hosts. Models on the `/responses`
  API (`grok-4.6`, `grok-4.7`, `gpt-*-luna`) are **not** supported.
- **DeepSeek**: base URL `https://api.deepseek.com`.

## Limitations

- Only chat completions are rerouted. Embeddings/RAG, image generation,
  web grounding, and transcription keep using Odoo's native services.
- Unsupported request options (`boost_reasoning`, `web_grounding`,
  `image_generation`, `inline_data` parts) fail with a clear error instead
  of being silently dropped.
- Non-streaming requests only (`stream=false`).
- No automatic model discovery — type the model name in Settings.

## Security

- API key is stored in `ir.config_parameter`, write-only in Settings
  (UI shows only "configured" status), admin-only, rejected if it contains
  line breaks.
- No cookies, session tokens, or env proxies reach the provider
  (`trust_env=False`, no redirects).
- Errors surfaced to users and logs are sanitized — never include keys,
  request bodies, or provider error payloads.

## License

LGPL-3
