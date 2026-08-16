# Leon Agent AI Collaboration State

This is the shared source of truth for Codex, Notion AI, and any other coding agent working on
`projects/02-leon-agent`.

Read this file and the project `README.md` before changing code. Update this file when an API,
runtime boundary, canonical path, or known limitation changes.

## Current Branch And Runtime

- Active integration branch: `feat/leon-model-switch`
- Integrated Web/Gateway SSE replay baseline: `df7e92e`
- Integrated daily CLI TUI baseline: `d520df9`
- Production command: `uv run leon-server --host 127.0.0.1 --port 8233`
- Public Web URL: `https://leon.928886540.xyz`
- Cloudflare Tunnel config: `D:\cloudflared\config.yml`
- CLI command: `leon` (editable uv tool install pointing at this repository)
- LLM config source: current `~/.codex/config.toml` written by CC Switch

Never commit `.env`, API tokens, SQLite databases, generated images, caches, or local IDE files.

## Canonical Files

- CLI: `src/leon_agent/cli.py`
- Gateway: `src/leon_agent/gateway/app.py`
- Canonical Web client: `web/` (`src/api`, `src/stores`, `src/views`, `src/components`)
- Service Worker: `web/public/sw.js`
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

There is exactly one Web client tree: Vue under `web/`. The removed `src/leon_agent/web/` tree and
`LEON_WEB_CLIENT` switch must not be reintroduced. FastAPI always serves `web/dist/`; run the Vite
build before starting or testing the Gateway.

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

For the Web client, the complete LLM connection is pinned in Gateway process memory when its Leon
session is created: `profile/provider`, `base_url`, API key, and default model. Changing CC Switch or
editing TOML does not affect that session while the same Gateway process remains alive. Logging out
clears the browser session id; the next login creates a new Leon session and captures the then-current
provider. Model-catalog refresh only retries `/models` against the in-memory pinned provider.

The pin is restart-safe since the `llm_base_url` column landed: session creation persists the provider
scope plus base URL (never the API key) in SQLite. On a Gateway restart the first request for an old
session re-resolves the secret by identity — a matching current provider is used directly, a `ccs:*`
pin is re-resolved by provider name from the CC Switch DB (and fails with HTTP 409 when the provider
is gone or its base URL changed), and `toml:`/`env` pins that no longer match fail explicitly with the
same 409 instead of silently recapturing the active provider. Old sessions created before the pin
column existed still capture the current provider on first touch (no persisted identity to honor).

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

Each replayable SSE event carries a process-local monotonically increasing `id:` field. The Gateway
retains the latest 100 events per session in memory and honors the browser's `Last-Event-ID` (plus
legacy query aliases) on reconnect. `session.connected` is a data-only connection marker and does not
advance the replay cursor. This is best-effort replay: a process restart drops the in-memory window.
The Vue client leaves transient reconnects to native `EventSource`, probes `/api/health` for stale
credentials, returns to login on 401, and wakes a CLOSED stream on the browser `online` event.

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

## CLI Interaction Contract

- `leon` uses `prompt-toolkit` when stdin/stdout are interactive: the upper pane is scrollable
  output and the bottom pane is a dynamic 1-to-6-line composer. Enter sends; Shift+Enter inserts a
  newline, with Ctrl+Enter and Esc+Enter as terminal compatibility fallbacks. Non-TTY environments
  keep the Rich prompt fallback without interruption tracebacks.
- The startup panel shows the active model, provider/profile, LLM base URL, configuration source,
  image backend, and session id. All command and answer output goes through the same renderer so
  answers remain visually separated from input.
- Input history is loaded from the current SQLite session. Up/down recalls it, switching or creating
  a session refreshes it, and a draft typed while a turn is running remains in the composer.
- `/resume`, `/retry`, `/last`, `/copy`, `/tools`, and `/status` are part of the CLI contract. Slash
  completion is case-insensitive and includes Chinese descriptions.
- Esc/Ctrl+C cooperatively cancels the current turn: late output is not rendered or persisted, and no
  later LLM/tool round continues. A synchronous HTTP read already in flight cannot be safely killed;
  it may remain blocked until the provider returns or the 30-second timeout expires. Do not close the
  shared HTTP client and claim hard cancellation.
- The shared `AgentTool.return_direct` flag is for deterministic, user-facing tool results that do
  not need another provider round-trip. Leon's `generate_images` uses it and renders image URLs or
  task status directly after the image tool finishes. This avoids an unnecessary second LLM request
  after a long image wait, which is important for low-RPM providers.

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

Mobile-compact pass (`feat/leon-web-mobile-compact`, SW cache `leon-vue-v10`):

- Single-line chat header and page headings; English eyebrow labels (`IMAGE JOBS` etc.) removed
- Page/composer/bubble paddings tightened for 390px-class viewports; composer placeholder is one
  friendly Chinese sentence
- Message bubbles are plain rounded rectangles without tails; per-message actions are
  `@lucide/vue` icon buttons (copy, edit, retry, TTS) instead of text links
- Voice catalog defaults to collapsed; the JOK voice is filtered out of the catalog and never
  selectable; the preview button cycles loading/playing/stop states
- Full-screen viewer spans the viewport (`100vw`/`100dvh`, cover) with a circular close button
  seated inside the top safe area, shared by chat and gallery
- Icon system unified on `@lucide/vue` (no hand-drawn SVG buttons)

W4/W5 completion pass (SW cache `leon-vue-v11`):

- `assistant.completed` now carries authoritative `model`, `elapsed_ms`, and
  `usage: {input_tokens, output_tokens}` captured from the OpenAI-compatible provider response and
  accumulated across tool turns; the agent bubble toolbar renders elapsed, `↑in/↓out` tokens, and the
  served model name, each omitted when the value is missing (no fake `0 tokens`)
- ASR input: `POST /api/agent/asr` (multipart audio, OpenAI-compatible `/audio/transcriptions`
  upstream) plus `GET /api/agent/asr/status`; the composer mic records via `MediaRecorder`, uploads,
  and fills the textarea without auto-sending. Requires `LEON_ASR_BASE_URL`/`LEON_ASR_TOKEN`; the mic
  stays hidden when unconfigured
- Message history restore (`GET /sessions/:id`) includes `created_at`; gaps over 10 minutes render a
  centered time divider (`load_messages(include_created_at=True)` keeps LLM history payload clean)
- Theme base: core palette extracted to CSS variables in `:root` (`--bg/--surface/--text/--primary/
  --line/--danger` …) to prepare for dark mode or custom wallpapers

De-clutter pass (SW cache `leon-vue-v12`):

- Blue `Leon` brand + connection status render side by side. Logout is a full-width danger button at
  the bottom of Settings; the header no longer exposes logout
- Settings cards dropped one-time hints (base URL, provider id, "留空保存…", "N 个可用模型…",
  "★ 可收藏"); status lines appear only while loading or on error
- Model/voice refresh controls are subtle in-row icon buttons inside their cards
- Voice autoplay is an iOS-style switch (native checkbox stays for a11y, visually hidden)
- Voice catalog: 全部/收藏 tabs + 20-per-page pagination; the old favorites-only default (which hid
  the catalog behind a single favorite) is gone
- Agent bubble meta shows elapsed + `↑in/↓out` tokens only (model name removed) in a spaced,
  right-aligned muted group

Fixed shell redesign (SW cache `leon-vue-v13`):

- Chat/Tasks/Gallery/Settings share one fixed branded header; secondary views no longer render their
  own page-title rows. Tasks/Gallery expose refresh as a contextual action in the shared header.
- The app shell uses explicit header/content/navigation grid areas, preventing the bottom navigation
  from falling into the stretch row when switching away from Chat.
- Model refresh sits in the model-card top-right corner; voice autoplay uses an explicit switch track
  and thumb; the voice tab label stays `收藏` without appending the favorite count.

True streaming:

- `LLMClient.chat_turn(on_delta=...)` streams with `stream=True` +
  `stream_options={"include_usage": True}`; content fragments fire `on_delta`, tool-call argument
  fragments are stitched back per index, and the final usage chunk feeds the W4 meta pipeline
- The agent loop forwards deltas as `assistant_delta` events; the Gateway republishes them as SSE
  `assistant.delta`, which the Vue client has always handled (appends to the streaming bubble)
- `assistant.delta` bypasses the 100-event replay window so deltas cannot evict state events;
  reconnecting clients recover full text from `assistant.completed`
- The CLI status line switches from「模型思考中」to「正在生成」on the first delta
- Clients whose `chat_turn` lacks `on_delta` (older fakes/tests) are detected by signature and keep
  the non-streaming path

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
npm --prefix projects/02-leon-agent/web run build
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
uv run leon --help
```

If the machine's real TOML currently has no active `model_provider`, make the full suite explicitly
provider-free instead of editing the real config:

```powershell
$env:LLM_SOURCE="env"
$env:LLM_API_KEY="test-key"
$env:LLM_BASE_URL="http://127.0.0.1:9/v1"
$env:LLM_MODEL="test-model"
.\.venv\Scripts\python.exe -m pytest -q
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

Historical legacy browser coverage was retired when the single-file client was deleted. The
canonical provider-free browser suite is `tests/manual_vue_web_check.py`; the Service Worker cache
is `leon-vue-v9`.

The LLM transport safety fix was validated separately with `101 passed`. Current Vue provider-free
validation additionally includes `npm run typecheck`, `npm run build`, and `manual_vue_web_check.py`
at `19/19` for both Vite preview and FastAPI Vue entry; these checks use fake API/SSE and do not prove
real provider or mobile behavior. Vue is the only Web entry.
The integrated CLI/Web baseline was most recently validated with explicit fake LLM environment values
at `166 passed`, repository-level Ruff clean, and `uv run leon --help` successful. The lower count
reflects removal of legacy-only HTML and selector tests, not lost Vue behavior coverage.

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
