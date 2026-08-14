# Leon Agent AI Collaboration State

This is the shared source of truth for Codex, Notion AI, and any other coding agent working on
`projects/02-leon-agent`.

Read this file and the project `README.md` before changing code. Update this file when an API,
runtime boundary, canonical path, or known limitation changes.

## Current Branch And Runtime

- Active integration branch: `feat/leon-model-switch`
- Production command: `uv run leon-server --host 127.0.0.1 --port 8233`
- Public Web URL: `https://leon.928886540.xyz`
- Cloudflare Tunnel config: `D:\cloudflared\config.yml`
- CLI command: `leon` (editable uv tool install pointing at this repository)
- LLM config source: current `~/.codex/config.toml` written by CC Switch

Never commit `.env`, API tokens, SQLite databases, generated images, caches, or local IDE files.

## Canonical Files

- CLI: `src/leon_agent/cli.py`
- Gateway: `src/leon_agent/gateway/app.py`
- Web client: `src/leon_agent/web/index.html`
- Service Worker: `src/leon_agent/web/sw.js`
- LLM transport: `packages/workbench_core/src/workbench_core/llm.py`
- Session persistence: `src/leon_agent/session.py`
- Model helpers: `src/leon_agent/models.py`
- Tests: `tests/` and `packages/workbench_core/tests/`

The read-only global latest-image backend lives in the separate ComfyUI repository at
`D:\apiWorkSpace\ComfyUI-aki\ComfyUI-aki-v3\ComfyUI`:

- Query implementation: `app/ios/backend/gallery.py`
- Route registration: `app/ios/backend/routes.py`
- HTTP API: `GET /ios/image_gallery/recent?limit=<count>`
- Compatibility API: `GET /ios/image_gallery/latest`

There must be only one Web client source. Do not recreate `projects/02-leon-agent/web/`; the
FastAPI server only serves `src/leon_agent/web/`.

## Model Selection Contract

Model names are provider-defined identifiers and are case-sensitive.

- Never lowercase, uppercase, translate, alias, or otherwise normalize a manually entered model ID.
- CLI `/model` and the Web settings page fetch the catalog from the active provider's `/models`.
- The active provider is resolved from the current `config.toml` `base_url` and token.
- Numeric CLI shortcuts map only to the most recently fetched provider catalog.
- Manual model IDs remain allowed when `/models` is unavailable or incomplete.
- A session override is bound to `profile + base_url`. Changing CC Switch provider invalidates the
  previous provider's session override and restores the new provider's default model.

For the Web client, the complete LLM connection is pinned when its Leon session is created:
`profile/provider`, `base_url`, API key, and default model. Changing CC Switch or editing TOML must
not affect an already logged-in Web session. Logging out clears the browser session id; the next
login creates a new Leon session and captures the then-current TOML provider. Model-catalog refresh
only retries `/models` against the pinned provider.

## Web Event Contract

The Gateway publishes these SSE events:

- `session.connected`
- `user.message`
- `assistant.started`
- `assistant.completed`
- `assistant.notice` when background image rendering finishes
- `tool.started` with `input`
- `tool.finished` with `output`
- `image.task.created`
- `image.task.updated`
- `image.completed`
- `agent.error`

SSE currently transports lifecycle events and final answers. It does not yet provide true LLM token
streaming. The Web client progressively renders a completed answer for smoother presentation; do not
describe that as backend token streaming.

`/nsfw` is a direct image command in both CLI and Web; it is not the name of a fixed workflow. It
bypasses the LLM completely. Syntax is `/nsfw [--model <name-or-id>] <source text>`. The default is
`玛莉卡 -> k2_queen_marika`; aliases such as `tifa-plus`, `蒂法增强`, and `玛莉卡` resolve against
the currently installed mode catalog. `/nsfw` or `/nsfw --models` lists Chinese names plus exact
mode IDs. The Web composer fetches `GET /api/image-modes` and shows selectable mode suggestions
while the user types `--model`.

Normal LLM-routed generation and `/nsfw` share the same completion tracker. Each finished image
emits `image.completed`; the tracker then persists and emits an `assistant.notice` whose Markdown
contains the completed image URL. That notice must render as a new image-bearing assistant bubble,
so the user never needs to ask the LLM whether rendering finished.

Web refresh state is not reconstructed from SSE memory. The client loads
`GET /api/agent/sessions/{session_id}/image-state`, which merges the current Leon task sync and
gallery sync results. Completed task `image_url` values are used as a fallback when the gallery sync
lags behind the task endpoint.

`get_latest_images(limit)` is intentionally global and read-only. The LLM must extract the requested
count from the user's wording and pass it as `limit`; examples: latest one -> `1`, latest five ->
`5`, latest twelve -> `12`. `limit` is required; there is no fixed or default count in the Agent
tool. The backend clamps the count to `1..100` and returns completed images with a
non-empty `final_image_url` across the whole Leon image database, without a `chat_id` filter. Do not
expand it into global search/delete APIs. Normalize every `final_image_url` into an absolute public
`image_url` before returning it to the LLM.

## UX State

Merged from `feat/leon-ux-polish`:

- Thinking timer while the Agent request is running
- Progressive answer rendering and safe local Markdown rendering
- Collapsible tool input/output cards
- Image generation skeleton replaced by the completed image
- Markdown image syntax and plain ComfyUI image URLs render clickable images inside the assistant
  bubble; clicking opens the current-page full-screen viewer
- Chat history, image tasks, and gallery are restored after a page refresh
- The chat message area remains touch-scrollable and stops auto-following when the user scrolls up
- Mobile keyboard layout relies on `interactive-widget=resizes-content` and `100dvh`; do not add a
  second `visualViewport.height` resize or force-scroll on textarea focus, because that creates a
  blank bottom area and pushes the composer too far upward
- Chat and gallery images open in the current-page full-screen viewer, not a new tab/download page
- Typing `/nsfw --model` opens a selectable mode menu above the composer
- Error card with retry-to-composer behavior
- CLI image-generation progress indicator

Deliberately excluded:

- QR-code dependency and terminal QR output
- Unsanitized `marked.parse()` output
- Fake `assistant_delta` handling presented as true streaming
- Static model catalogs copied from one provider
- Integer-valued JSON Schema `enum` fields, because Gemini tool declarations only accept string
  enum values; use numeric bounds and descriptions instead

## Required Validation

Run in this order:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
uv run leon --help
```

For Web changes also verify a mobile viewport with a real browser:

- login succeeds
- composer accepts text and remains inside the viewport
- settings fetch models from the current `base_url`
- a case-sensitive custom model ID is saved unchanged
- SSE connects without duplicate error cards
- generated-image skeleton is replaced by the real image

## Handoff Format

Every AI handoff must include:

```text
Branch:
Commit(s):
Goal:
Changed files:
Behavior/API changes:
Validation run:
Known limitations:
Next action:
```

Do not report a feature as complete only because UI code exists. Verify that the backend emits the
event or data the UI consumes, and verify the running server is serving the edited canonical file.
