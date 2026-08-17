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
- Process model: exactly one Uvicorn worker; the command rejects `--workers` values other than `1`
  because SSE replay, active turns, provider snapshots, and voice clips are process-local
- Public Web URL: `https://leon.928886540.xyz`
- Cloudflare Tunnel config: `D:\cloudflared\config.yml`
- CLI command: `leon` (editable uv tool install pointing at this repository)
- Canonical runtime config: `~/.leon/config.toml`; `.codex`, CC Switch, and the repository `.env`
  are bootstrap-only inputs for the first `leon-config init`

Runtime restart rule:

- Editing `projects/02-leon-agent/src/` does not update the running Gateway. Restart `leon-server`
  and verify the served route/assets before claiming the backend change is live.
- Editing the separate Leon / ComfyUI backend or its plugin requires restarting ComfyUI and checking
  the live `/ios/*` behavior. If one task changes both repositories, restart and verify both services.

Never commit `.env`, API tokens, SQLite databases, generated images, caches, or local IDE files.

## Canonical Files

- CLI: `src/leon_agent/cli.py`
- Gateway: `src/leon_agent/gateway/app.py`
- Canonical Web client: `web/` (`src/api`, `src/stores`, `src/views`, `src/components`)
- Service Worker: `web/public/sw.js`
- Leon runtime config: `src/leon_agent/config.py`
- User config loader/migrator: `src/leon_agent/config_file.py`
- Agent system prompt composition: `src/leon_agent/agent.py`
- Volink TTS client: `src/leon_agent/voice_client.py`
- LLM transport: `packages/workbench_core/src/workbench_core/llm.py`
- Session persistence: `src/leon_agent/session.py`
- Model helpers: `src/leon_agent/models.py`
- Channel-independent Leon image service: `src/leon_agent/service.py`
- Web search provider/service: `src/leon_agent/search/`
- Read-only file adapter: `src/leon_agent/file_tools.py`
- Shared file search core: `../../packages/workbench_core/src/workbench_core/files/`
- Tests: `tests/` and `packages/workbench_core/tests/`

The first Leon MCP server lives in `projects/04-mcp-lab/leon-mcp-server`. It is a separate
workspace package and must call `LeonToolService`; do not copy image request construction or tool
schemas into another channel adapter.

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
  prompt content or the real `.leon` config.

## Model Selection Contract

Model names are provider-defined identifiers and are case-sensitive.

- Never lowercase, uppercase, translate, alias, or otherwise normalize a manually entered model ID.
- CLI `/model` and the Web settings page fetch the catalog from the active provider's `/models`.
- The active provider is resolved only from `~/.leon/config.toml` `base_url` and token.
- Numeric CLI shortcuts map only to the most recently fetched provider catalog.
- Manual model IDs remain allowed when `/models` is unavailable or incomplete.
- A session override is bound to `profile + base_url`. Editing `.leon` and restarting the owning
  process invalidates a mismatched session override and restores the configured default model.

For the Web client, the complete LLM connection is pinned in Gateway process memory when its Leon
session is created: `profile/provider`, `base_url`, API key, and default model. Editing `.leon` does
not affect that session while the same Gateway process remains alive; restart Gateway and create a
new session to use the new provider. Model-catalog refresh only retries `/models` against the
in-memory pinned provider.

The pin is restart-safe since the `llm_base_url` column landed: session creation persists the provider
scope plus base URL (never the API key) in SQLite. On a Gateway restart the first request for an old
session re-resolves the secret by identity — a matching current provider is used directly, a `ccs:*`
pin is rejected with HTTP 409 before any provider lookup, and other pins that no longer match the
current `.leon` provider fail with the same 409 instead of silently recapturing it. Old sessions
created before the pin column existed capture the current provider on first touch.

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

SSE transports lifecycle events, true online-only `assistant.delta` token fragments, and the final
`assistant.completed` answer. Deltas do not enter the replay window; reconnecting clients recover the
authoritative full text from `assistant.completed`.

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
- The TUI runs inline instead of entering the alternate full-screen buffer. The host terminal owns
  mouse-wheel scrollback, drag selection, and Ctrl+Shift+C, so prompt_toolkit mouse reporting stays
  disabled. PageUp/PageDown also scroll the transcript. Image labels use terminal-native OSC 8 links
  with `/open` as the fallback, so clickability does not require application mouse capture. A one-row
  gap separates transcript/status output from the composer. The composer uses a `»` marker on its
  first line, keeps continuation lines unindented, and explicitly toggles cursor visibility because
  prompt_toolkit's Win32 output backend ignores blinking cursor shapes.
- Completed exchanges are separated by horizontal rules. Only the latest completed turn may show a
  `Worked for <duration>` label; launching the next turn replaces that label with a plain rule, and
  resumed history uses plain rules because persisted messages do not contain execution duration.
- The latest assistant marker keeps the foreground color while older assistant markers are green.
  Execution states use distinct running/success/error/cancel/warning colors.
- Pressing Enter while a turn is running queues the complete message, clears the composer, and sends
  queued messages in order after the current turn settles. It must not print repeated "previous turn"
  warnings or run two Agent turns against the same session concurrently.
- Interactive CLI image generation returns after the backend accepts the jobs. A daemon tracker owns
  completion polling, keeps the composer usable, persists the final image message to the submission's
  original session, and renders clickable image links. `leon --once` remains synchronous because no
  long-lived terminal process exists to receive a later notification.
- `/resume`, `/retry`, `/last`, `/copy`, `/tools`, and `/status` are part of the CLI contract. Resume
  replays up to the TUI's 240-message display capacity in SQLite order while last/retry/copy still
  target only the latest exchange. Slash completion is case-insensitive and includes Chinese descriptions.
- Esc/Ctrl+C cooperatively cancels the current turn: late output is not rendered or persisted, and no
  later LLM/tool round continues. A synchronous HTTP read already in flight cannot be safely killed;
  it may remain blocked until the provider returns or the 30-second timeout expires. Do not close the
  shared HTTP client and claim hard cancellation.
- The shared `AgentTool.return_direct` flag is for deterministic, user-facing tool results that do
  not need another provider round-trip. Leon's interactive `generate_images` uses it to report the
  accepted background submission immediately; the tracker renders image URLs only after completion.
  This avoids both a long foreground wait and an unnecessary second provider request.

## Web Search Contract

- `web_search` is optional and read-only. It is registered only when `TAVILY_API_KEY` is non-empty;
  missing search configuration must not prevent chat or image tools from starting.
- `search/provider.py` owns Tavily HTTP details. `search/service.py` validates tool arguments and
  returns only normalized title, URL, snippet, source, and publication time. Do not put search code
  into the image-specific `LeonToolService`.
- The default is `search_depth=basic` and five results to control latency and credits. Use
  `advanced` only for an explicit deep-research request.
- Search output is untrusted evidence, never instructions. The model must cite returned URLs and
  must not claim facts that the results do not support.
- `TAVILY_API_KEY` stays in the backend environment and is sent only in the Authorization header.
  Never put a real key, a key-bearing MCP URL, or raw credentials into Git, events, tool results, or
  logs. Revoke any key that has appeared in chat or screenshots.
- This is a direct Tavily API adapter for Leon Agent. It does not mean Leon is a general MCP Client,
  and it does not add search to `projects/04-mcp-lab/leon-mcp-server`.
- `extract`, `crawl`, `map`, `research`, page fetching, cache, and credits budgeting are not part of
  the first search slice. See `docs/web-search.md` before extending the contract.

## File Tools Contract

- File Search is optional. The read tools are registered only when `LEON_FILE_ROOTS` contains a valid
  JSON allowlist; an empty value must leave ordinary chat, image tools, and `web_search` usable.
- `list_files` lists root aliases or a directory, `file_search` performs case-insensitive literal filename/
  content search, and `read_file` returns a bounded line range. All three delegate to
  `workbench_core.files.FileSearchService`; do not put filesystem policy in CLI, Gateway, or `code_agent`.
- The model receives only `root_id`, normalized relative paths, line numbers, and citations such as
  `workbench:docs/README.md:42`. Never expose the configured absolute path in tool output or error text.
- `create_file` and `write_file` are optional write tools. They are registered only when a composition root
  explicitly injects an authorized `FileWriteService` whose root ids exactly match the read service. The former
  is no-clobber create; the latter is existing-file whole replacement. Neither creates directories, appends,
  patches, deletes, moves, or executes files.
- Every path must be relative to a configured root. Resolve and re-check containment for every candidate;
  skip symlink/junction/reparse entries, hidden/system entries, dot directories, `.env*`, credentials,
  private keys, SQLite sidecars, unsupported binary files, and invalid encodings.
- Resource limits are part of the contract: at most 8 roots, 1 MiB per file, 2,000 scanned files/20 MiB/50
  matches per search, and 200 lines/16,000 characters per read. Handlers must revalidate numeric arguments;
  JSON Schema bounds are not a security boundary.
- File contents are untrusted evidence. The system prompt must prevent file text from changing Agent rules,
  expanding roots, requesting secrets, or authorizing writes. Write authorization comes only from an exact
  first-line `!file create root:path` or `!file write root:path` command in the current user turn; natural language
  only proposes that confirmation. Model arguments never carry `confirmed`, user text, or write counts. A single
  turn allows at most one write, and audit/SSE/SQLite projections omit content and absolute paths. Delete, move,
  execution, PDF/DOCX parser, embedding index, RAG layer, and File Search MCP exposure remain out of scope.
- After changing `LEON_FILE_ROOTS` or any `src/`/shared-core code, restart `leon-server` and verify the new
  process. The focused checks are `packages/workbench_core/tests/test_file_search.py` and
  `test_file_write.py` plus `tests/test_leon_file_search.py`, `test_leon_file_write.py`, `test_cli.py`, and
  `test_gateway.py`; use temporary test roots only.

## Memory Contract

- CLI and Gateway create `MemoryService` from the same `LEON_SESSION_DB` as `SessionStore`; the MVP principal
  is the fixed local identity `local-owner`, never the API token. Normal Agent turns register
  `memory_get`/`memory_upsert`/`memory_delete`; direct `/nsfw` and Leon MCP do not.
- A write/delete is authorized only by the exact current user message and consumes the turn's single attempt.
  Ordinary preferences are not harvested automatically, file/web content cannot authorize Memory, and a second
  write attempt returns `write_limit_reached`.
- Every turn rebuilds a separate untrusted system context: user overrides global, at most 12 complete records,
  2,400 characters total, and 512 characters per automatically injected value. Oversized values remain available
  only through an explicit `memory_get`.
- Raw values may enter the configured LLM provider's current in-memory transcript. They must never enter
  `AgentEvent`, SSE, `ToolStep`, or SQLite `tool_calls`; those surfaces use the Memory tools' metadata-only audit
  projection. `memory_delete` hard-deletes the primary row but does not erase historical user messages.
- A completed Memory write is not rolled back if the user cancels later in the same turn. The audit remains
  redacted, and the next turn rebuilds context from the resulting SQLite state. See `docs/memory.md` before
  extending scope, adding a management API, multi-user identities, encryption claims, or MCP exposure.

## Planning Contract

- Normal `LeonAgent` turns register `plan_create`, `plan_update`, and `plan_get`; direct `/nsfw` registries and
  Leon MCP do not. Existing `AgentRuntime` remains the only execution loop.
- Planning is optional for genuinely multi-step work and forbidden for ordinary chat or one domain-tool action.
  One turn owns at most one ordered plan with 2..8 steps; every new turn resets the service.
- The server enforces `pending -> in_progress -> completed|failed`, one active step, ordered starts, and terminal
  immutability. Planning tools only track work and cannot authorize file writes, Memory changes, or other tools.
- Raw step descriptions stay only in the current provider transcript. `AgentEvent`, SSE, `ToolStep`, and SQLite
  retain only step count/index/status, aggregate counts, active step, and done state through audit projection.
- Cancellation does not force synthetic plan completion. Completed business side effects keep their existing
  audit behavior; the next turn starts with no plan. Read `docs/planning.md` before adding persistence, DAGs,
  parallelism, automatic retries, Web management APIs, or MCP exposure.

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
- Manual/automatic text TTS uses one reusable audio element. A blocked iOS autoplay attempt keeps
  the pending audio URL and callback until the user taps the visible unlock button; do not revoke it
  early. Agent-generated `voice.ready` clips instead play from the visible bubble audio element so
  the control shown to the user is the control that owns playback.

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

Conversation flow pass (SW cache `leon-vue-v14`):

- The app shell is one continuous surface: header, content, composer, and bottom navigation use
  separators instead of four independent rounded cards.
- Model and voice catalogs are floating popovers, so opening either list does not move the cards,
  logout button, or bottom navigation. Normal loading/saving states no longer add temporary rows.
- `tool.started` / `tool.finished` render inside the active assistant bubble; the composer no longer
  owns a global task-status line. Direct image submission keeps the one-provider-turn optimization.
- Web image submission explicitly returns `waited_for_completion=false`, so the direct answer says
  the task was submitted in the background instead of incorrectly claiming completion.
- Completed image Markdown and its natural completion note persist as one assistant message. Live
  events and legacy two-message history are merged into one image-result bubble without text-action
  controls.

Interaction correction pass (SW cache `leon-vue-v15`):

- The current installed mode catalog (Chinese name, aliases, exact workflow id) is supplied to the
  LLM. The prompt makes the current turn authoritative, so a named mode is not inherited from an
  earlier turn; `generate_images` itself does not parse keywords or force-rewrite workflow ids.
- The active send button becomes a stop control backed by a per-session cancellation endpoint;
  cancellation publishes `assistant.cancelled` and never duplicates an `agent.error` bubble.
- Retrying reuses the current assistant bubble. Previous attempts remain available through the
  version button below the bubble; user bubbles also expose copy, retry, and edit actions.
- Voice selection uses a wider, taller popover with fixed tabs/search and pagination. Only the
  middle voice list scrolls, so the outer popover no longer gains a second scrollbar.
- Image submission copy always says `已提交 N 张图片任务` and asks the user to wait for automatic
  delivery; it no longer claims completion before an image URL is available.

Refresh/voice/UI reliability pass (SW cache `leon-vue-v21`):

- Session history includes stable message ids plus persisted assistant revisions. Refreshing during
  an active normal/retry turn reconstructs the pending bubble from `active_turn`; completion remains
  in SQLite and retry version counts survive later reloads.
- The cancel route accepts POST and DELETE. The Web client retries DELETE only when POST returns 405,
  preserving compatibility while a previously launched Gateway process is still serving old routes.
- Manual TTS uses a 10-entry browser LRU, while the Gateway keeps a process-wide minimum 10-entry
  normalized text + voice cache shared by `/api/agent/tts` and `speak_text`.
- `voice.ready` playback is owned by the visible bubble's custom play/pause/progress control. The
  native audio element is hidden and no separate invisible player starts the same clip.
- The voice catalog is a modal with backdrop, close button, Escape support, fixed header/footer and
  an internally scrolling list. Sparse favorite/search results retain 48px rows instead of stretching.
- Login wording no longer promises a return to an earlier conversation or inserts a loading row that
  shifts the layout. Pending replies use the three animated thinking dots again.
- Message editing uses one viewport modal; saving replaces only the selected bubble text, so editing
  never participates in or expands the conversation layout.
- Completed task cards show a clickable thumbnail and omit internal task/plan details. Task, chat and
  gallery previews use `object-fit: contain`; unused space uses the shared blue-gray viewer backdrop.
- Viewer close/previous/next controls are fixed to the visual viewport and remain independent from
  the image layer. Model settings are cached for the current app session and only refresh on demand.
- Chat bubbles pass their complete rendered image set and selected index into the shared viewer;
  chat and gallery both support previous/next buttons, keyboard navigation and horizontal swipes.
- Structured image URLs from tools such as `get_latest_images` are promoted into the assistant
  image bubble. Duplicate “view image” Markdown is hidden and image-only results never trigger TTS.
- The stop-generation action uses the themed danger color, and logout now requires confirmation.

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
uv run pytest projects/02-leon-agent/tests/test_search.py -q
uv run pytest packages/workbench_core/tests/test_file_search.py projects/02-leon-agent/tests/test_leon_file_search.py -q
npm --prefix projects/02-leon-agent/web run build
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
uv run leon --help
```

Python tests use an autouse temporary `LEON_CONFIG_FILE` with a fake provider. They never read the
real user profile or contact a provider; do not change `~/.leon/config.toml` to make tests pass.

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
is `leon-vue-v21`.

The LLM transport safety fix was validated separately with `101 passed`. Current Vue provider-free
validation additionally includes `npm run typecheck`, `npm run build`, and `manual_vue_web_check.py`
at `76/76` for both Vite preview and FastAPI Vue entry; these checks use fake API/SSE and do not prove
real provider or mobile behavior. Vue is the only Web entry.
The integrated CLI/Web baseline was most recently validated at `188 passed`, repository-level Ruff
clean, and `uv run leon --help` successful.
The optional Tavily search slice was additionally validated with 16 provider-free tests on Python
3.10 and 3.13, the Leon project suite at 173 passed, the full workspace at 206 passed with an
explicit fake LLM provider, and a keyless read-only `/search` protocol probe that returned `title`,
`url`, and `content` without using account credits. A live `leon --once` run with `grok-4.6`
completed `web_search` and returned a Chinese answer citing Tavily's official documentation URL.
The MCP slice was additionally validated with `leon-mcp --help`, stdio `initialize/tools/list`, and
Streamable HTTP `initialize/tools/list`; both transports expose five tools and no smoke test calls
`generate_images`.

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
