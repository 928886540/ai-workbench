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
- Leon runtime config: `src/leon_agent/config.py`
- Agent system prompt composition: `src/leon_agent/agent.py`
- Volink TTS client: `src/leon_agent/voice_client.py`
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

## Additional System Prompt Contract

- `LEON_SYSTEM_PROMPT_FILE` optionally points to a UTF-8 text file whose content is appended to the
  built-in Leon system prompt for both CLI and Web requests.
- Relative paths resolve from the repository root.
- Missing, non-file, non-UTF-8, and empty values fail clearly; do not silently ignore them.
- Keep local prompt files under the gitignored `data/system-prompts/` directory. Never commit the
  prompt content or the real `.env` path selection.

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
- `voice.ready`
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

Normal LLM-routed generation and `/nsfw` share the same completion tracker. When the task endpoint
already has the final image URL, it emits `image.completed` immediately; the gallery endpoint is only
a fallback. The tracker persists the image Markdown separately, then asks the session-pinned LLM for
a short natural completion note and emits that text through `assistant.notice`. The tool card is the
only running indicator: there is no image skeleton bubble. A completed image is appended as a new
bottom bubble and auto-scrolled into view without waiting for the LLM note.

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

## Voice Contract

- Secrets stay server-side through `VOLINK_API_KEY`; the browser never calls Volink directly.
- `GET /api/voice/catalog` returns four TTS models and the paginated voice catalog. The upstream
  request must include `lang=zh-CN`, otherwise Chinese names such as `风韵少妇` become unsearchable
  English labels.
- `POST /api/agent/tts` accepts `text` plus optional `voice_id` and returns `audio/mpeg` directly.
- Browser and gateway both normalize speakable text: remove Markdown list markers, emoji, URLs,
  workflow/mode IDs, task IDs, and plan IDs; join line breaks with a natural Chinese pause. Keep this
  behavior aligned between `speakableText()` and `prepare_speech_text()`.
- Agent-initiated `speak_text` stores a short-lived clip, publishes `voice.ready`, and serves it from
  `GET /api/voice/clips/{clip_id}`.
- Voice IDs are 24-character IDs, not display names, and each voice is bound to its catalog model.
- A TTS `502` is an observed intermittent Volink upstream failure, not evidence of a local text-length
  limit. Log the voice ID, raw/prepared character counts, and upstream error. Do not add retries unless
  retry semantics are explicitly designed.
- The Web client uses one reusable audio element. A blocked iOS autoplay attempt keeps the pending
  audio URL and callback until the user taps the visible unlock button; do not revoke it early.

## UX State

Merged from `feat/leon-ux-polish`:

- Thinking timer while the Agent request is running
- Progressive answer rendering and safe local Markdown rendering
- Collapsible tool input/output cards
- Image progress stays in the tool card; completed images append as new bottom bubbles
- Markdown image syntax and plain ComfyUI image URLs render clickable images inside the assistant
  bubble; clicking opens the current-page full-screen viewer
- Full-screen viewer supports album navigation, pinch/double-click focal zoom, drag-to-pan, and mouse
  wheel; the redundant visible zoom controls were removed
- Chat history, image tasks, and gallery are restored after a page refresh
- Image tasks use the human mode name and Chinese status as primary content; internal job/plan IDs are
  hidden inside a collapsed details section
- The chat message area remains touch-scrollable and stops auto-following when the user scrolls up
- Mobile keyboard layout relies on `interactive-widget=resizes-content` and `100dvh`; do not add a
  second `visualViewport.height` resize or force-scroll on textarea focus, because that creates a
  blank bottom area and pushes the composer too far upward
- Chat and gallery images open in the current-page full-screen viewer, not a new tab/download page
- Typing `/nsfw --model` opens a selectable mode menu above the composer
- Assistant bubbles support copy, real retry, local edit, and TTS playback; edited text is used by TTS
- TTS strips Markdown/emoji/internal identifiers, shows loading then animated level bars, restores the
  normal playback icon on completion, and surfaces the gateway error detail on failure
- Voice search takes the available mobile row width and both search/refresh controls meet a 44px touch
  target
- Model candidates open on focus/input and collapse after a successful save
- Error cards retry the matching previous user message directly
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
- completed images append at the bottom without a skeleton bubble
- bubble copy/retry/edit/TTS actions work after rerendering
- TTS shows loading/playing/idle states and preserves blocked iOS audio until unlock

Current baseline on 2026-08-15: `96 passed`, Ruff clean, and
`tests/manual_web_check.py` `51/51` with system Chrome at `390x844` touch viewport.

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
