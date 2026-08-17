"""Static contracts for the canonical Vue Web client.

The repository does not currently install Playwright (or a browser test runner) in
the uv environment.  These checks therefore exercise the source-level contract
that a fake Gateway can satisfy, without contacting an LLM, Volink, or an image
backend.  A browser suite can be layered on top once its runtime is made a
declared project dependency.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
WEB_ROOT = PROJECT_ROOT / "web"


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_vue_entry_is_the_only_web_client() -> None:
    config = _read("src/leon_agent/config.py")
    gateway = _read("src/leon_agent/gateway/app.py")
    env_example = (PROJECT_ROOT.parents[1] / ".env.example").read_text(encoding="utf-8")
    entry = _read("web/index.html")
    vite = _read("web/vite.config.ts")
    main = _read("web/src/main.ts")
    service_worker = _read("web/public/sw.js")

    assert "LEON_WEB_CLIENT" not in config
    assert "LEON_WEB_CLIENT" not in env_example
    assert "_LEGACY_WEB_DIR" not in gateway
    assert "Vue client build required at" in gateway
    assert "_WEB_DIR: Path = _resolve_web_dir()" in gateway
    assert 'app.mount("/", StaticFiles(directory=_WEB_DIR, html=True)' in gateway
    assert '<div id="app"></div>' in entry
    assert '<script type="module" src="/src/main.ts"></script>' in entry
    assert '<link rel="icon" href="/icon.svg" type="image/svg+xml" />' in entry
    assert '<link rel="apple-touch-icon" href="/icon-512.png" />' in entry
    assert 'outDir: "dist"' in vite
    assert 'register("/sw.js?v=vue-27"' in main
    assert "leon-vue-v27" in service_worker


def test_vue_api_contract_is_fake_gateway_friendly() -> None:
    client = _read("web/src/api/client.ts")

    # Session/login traffic is routed through one request helper, which makes a
    # fake fetch implementation sufficient for local browser tests later.
    for fragment in (
        'const TOKEN_KEY = "leon_token"',
        'const SESSION_KEY = "leon_session"',
        'async checkHealth(',
        'async getSession(sessionId: string)',
        'async createSession()',
        'async sendMessage(',
        'async cancelMessage(sessionId: string)',
        'async getModelSettings(sessionId: string, refresh = false)',
        'private readonly modelSettingsCache = new Map<string, ModelSettingsResponse>()',
        'getCachedModelSettings(sessionId: string): ModelSettingsResponse | null',
        'if (cached && !refresh) return cached;',
        'async setModelSettings(',
        'async getVoiceCatalog(refresh = false)',
        'private async request<T>(',
        'if (response.status === 401)',
        'source.onerror = () => onError(source)',
    ):
        assert fragment in client, fragment

    assert '"/api/health"' in client
    assert '"/api/agent/sessions"' in client
    assert "`/api/voice/catalog${suffix}`" in client
    assert 'Authorization: `Bearer ${token}`' in client
    # Browser code must never receive the upstream Volink credential.
    assert "VOLINK_API_KEY" not in client


def test_vue_chat_login_session_restore_and_error_dedup_contract() -> None:
    chat = _read("web/src/views/ChatView.vue")

    for fragment in (
        "async function openSession()",
        "await api.getSession(api.sessionId)",
        "const created = await api.createSession()",
        "api.setSession(created.session_id)",
        "api.clearSession()",
        "async function login()",
        "await api.checkHealth(undefined, token)",
        'placeholder="输入访问口令"',
        "api.clearSession();",
        "function logout()",
        "closeEvents();",
        "function focusLatestBubbleAfterHydration(): void",
        'panel.querySelectorAll<HTMLElement>(".message-row")',
        'latest?.scrollIntoView({ block: "end", inline: "nearest" });',
        "authenticated.value = true;\n  focusLatestBubbleAfterHydration();",
        'let lastAgentErrorFingerprint = ""',
        "const ERROR_DEDUPE_WINDOW_MS = 10_000",
        'case "agent.error":',
        "function finishAgentErrorOnce(content: string): void",
        'latest?.status === "error"',
        "finishAgentErrorOnce(error instanceof Error ? error.message : \"发送失败\")",
        'case "assistant.cancelled":',
        "const cancelledAssistantTraceIds = new Set<string>();",
        "function suppressActiveAssistantTrace(): void",
        "function shouldSuppressAssistantEvent(data: Record<string, unknown>): boolean",
        "if (shouldSuppressAssistantEvent(data)) break;",
        "const partialContent = asString(data.content);",
        "if (partialContent && message) message.text = partialContent;",
        "async function stopSending(): Promise<void>",
        "suppressActiveAssistantTrace();",
        "api.cancelMessage(sessionId)",
        "activeSendController?.abort()",
    ):
        assert fragment in chat, fragment
    assert "输入密钥，回到对话" not in chat
    assert "正在验证连接" not in chat

    # Opening a new session must close the previous EventSource first;
    # transient failures stay on the browser's native reconnect path.
    assert "closeEvents();\n  setConnection(\"正在连接…\", \"neutral\");" in chat
    assert "const source = api.connectEvents(" in chat
    assert "eventSource = source;" in chat
    assert "eventSource?.close();" in chat
    assert "function handleEventError(source: EventSource): void" in chat
    assert "source.readyState === EventSource.CONNECTING" in chat
    assert "source.readyState === EventSource.CLOSED" in chat
    assert ".checkHealth()" in chat
    assert "expireLogin();" in chat
    assert "function handleOnline(): void" in chat
    assert 'window.addEventListener("online", handleOnline)' in chat
    assert 'window.removeEventListener("online", handleOnline)' in chat
    assert "async function reconcileAfterResume(): Promise<void>" in chat
    assert "const refreshed = await api.getSession(sessionId);" in chat
    assert "appendHistory(refreshed.messages, refreshed.voice_clips || []);" in chat
    assert "await loadImageState(true);" in chat
    assert "if (shouldRecheckActiveTurn) void reconcileActiveTurn(sessionId);" in chat
    assert "activeSendController?.abort();" in chat
    assert "const requestId = ++imageStateRequestId;" in chat
    assert "requestId !== imageStateRequestId" in chat
    assert "if (source !== eventSource || api.sessionId !== sessionId) return;" in chat
    assert 'document.addEventListener("visibilitychange", handleVisibilityChange)' in chat
    assert 'document.removeEventListener("visibilitychange", handleVisibilityChange)' in chat
    assert 'window.addEventListener("pageshow", handlePageShow)' in chat
    assert 'window.removeEventListener("pageshow", handlePageShow)' in chat
    assert 'lastEventId = "";' in chat
    assert 'eventCursorSessionId = "";' in chat

    client = _read("web/src/api/client.ts")
    assert 'if (lastEventId) params.set("last_event_id", lastEventId);' in client
    assert "onEvent(JSON.parse(message.data) as LeonEvent, message.lastEventId);" in client

    images = _read("web/src/stores/images.ts")
    assert "const TERMINAL_IMAGE_STATUSES" in images
    assert "keepTerminalState" in images


def test_vue_session_history_switch_contract() -> None:
    client = _read("web/src/api/client.ts")
    chat = _read("web/src/views/ChatView.vue")
    history = _read("web/src/components/SessionHistoryPanel.vue")

    for fragment in (
        "export interface SessionSummary",
        "async listSessions(",
        "async setSessionPinned(",
        "`/api/agent/sessions/${sessionId}/pin`",
    ):
        assert fragment in client, fragment

    for fragment in (
        "async function switchSession(sessionId: string)",
        "api.setSession(session.session_id)",
        "appendHistory(session.messages, session.voice_clips || [])",
        "clearImageState();",
        "void loadImageState(true);",
        "connectEvents();",
        "activeView.value = \"chat\";",
        "async function createNewSession()",
        "async function toggleSessionPinned(sessionId: string, pinned: boolean)",
        "<SessionHistoryPanel",
        ':active-session-id="api.sessionId"',
    ):
        assert fragment in chat, fragment

    switch_body = chat.split("async function switchSession(sessionId: string)", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "ensureVoiceCatalog" not in switch_body
    assert "getModelSettings" not in switch_body

    for fragment in (
        'id="session-history-panel"',
        'aria-label="历史会话"',
        'aria-label="新建会话"',
        "sessions",
        "session.pinned",
        'select: [sessionId: string]',
        '"toggle-pin": [sessionId: string, pinned: boolean]',
        'emit("select", sessionId)',
        'emit("toggle-pin", sessionId, pinned)',
    ):
        assert fragment in history, fragment


def test_vue_message_bubble_copy_edit_retry_contract() -> None:
    bubble = _read("web/src/components/MessageBubble.vue")
    editor = _read("web/src/components/MessageEditDialog.vue")
    viewer = _read("web/src/components/ImageViewer.vue")
    chat = _read("web/src/views/ChatView.vue")
    messages = _read("web/src/stores/messages.ts")
    markdown = _read("web/src/utils/markdown.ts")

    for fragment in (
        "navigator.clipboard.writeText(text)",
        'document.execCommand("copy")',
        "edit: [messageId: string];",
        "emit('edit', message.id)",
        "emit('retry', message.id)",
        "displayedMessage.value.role === \"user\"",
        'class="message-revision"',
        "emit('revision', message.id)",
        'class="message-toolbar"',
        'class="message-tools"',
        'class="message-tool"',
        "preview: [urls: string[], index: number];",
        'querySelectorAll<HTMLAnchorElement>("a.markdown-image-link")',
        'emit("preview", urls.length ? urls : [link.href], index);',
    ):
        assert fragment in bubble, fragment

    for fragment in (
        "function retryMessage(messageId: string): void",
        'if (selected.role === "user")',
        'else if (selected.role === "agent")',
        "beginMessageRevision(target);",
        "void sendTurn(content, { appendUser: false, retry: retryLatest });",
        "const turnAssistantId = pendingAssistantId.value;",
        "if (!current && turnAssistantId) current = findMessage(turnAssistantId);",
        "function cycleRevision(messageId: string): void",
        "function openMessageEditor(messageId: string): void",
        "function saveMessageEditor(): void",
        "if (message) message.text = editDraft.value;",
        "const previewItems = ref<ViewerImage[]>([]);",
        "function openPreview(urls: string[] | string, index = 0): void",
        'v-model:index="previewIndex"',
        ':items="previewItems"',
        "@retry=\"retryMessage\"",
        "@edit=\"openMessageEditor\"",
        "@revision=\"cycleRevision\"",
        'case "tool.started":',
        "startTool(data);",
        'case "tool.finished":',
        "finishTool(data);",
        "function toolOutputImageUrls(output: Record<string, unknown>): string[]",
        "if (!message.images.includes(url)) message.images.push(url);",
        'if (message.images.length) message.kind = "image-result";',
        'message.kind === "image-result"',
        "message.images.length > 0",
    ):
        assert fragment in chat, fragment

    for fragment in (
        '<Teleport to="body">',
        'class="message-edit-overlay"',
        'class="message-edit-dialog"',
        'class="message-edit-dialog__textarea"',
        'aria-label="消息内容"',
        "emit('save')",
        "emit('cancel')",
    ):
        assert fragment in editor, fragment

    for fragment in (
        "export interface ViewerImage",
        'class="image-viewer"',
        'aria-label="上一张"',
        'aria-label="下一张"',
        "function handlePointerDown(event: PointerEvent): void",
        "function handlePointerMove(event: PointerEvent): void",
        "function handlePointerUp(event: PointerEvent): void",
        "const activePointers = new Map<number, PointerPosition>();",
        "function beginPinch(): void",
        "function constrainedTranslation(",
        "const MAX_SCALE = 5;",
        "Math.abs(deltaX) < 48",
        "move(deltaX < 0 ? 1 : -1);",
        '@pointerdown="handlePointerDown"',
        '@pointermove="handlePointerMove"',
        '@pointerup="handlePointerUp"',
        '@wheel.prevent="handleWheel"',
        ':data-zoomed="scale > 1"',
        'aria-label="重置缩放"',
        "{{ activeIndex + 1 }} / {{ items.length }}",
    ):
        assert fragment in viewer, fragment

    assert "export const messages = ref<ChatMessage[]>([]);" in messages
    assert "tools: MessageToolCall[];" in messages
    assert "revisions: MessageRevision[];" in messages
    assert "export function beginMessageRevision(message: ChatMessage): void" in messages
    assert 'kind: "message",' in messages
    assert "export function findMessage(id: string | null): ChatMessage | null" in messages
    assert "return `${formatTokenCount(total)} tokens`;" in bubble
    assert "↑${formatTokenCount" not in bubble
    assert "↓${formatTokenCount" not in bubble
    for fragment in (
        "export function extractImageHrefs(raw: string): string[]",
        "export function stripImageLinks(raw: string): string",
        'const prose = text.replace(markdownLinkPattern(), "");',
        "function isImageLabel(value: string): boolean",
        'renderExplicitImages(imageUrls, "")',
        "renderMarkdown(stripImageLinks(message.text))",
    ):
        assert fragment in f"{markdown}\n{bubble}", fragment


def test_vue_task_thumbnail_and_contain_viewer_contract() -> None:
    tasks = _read("web/src/views/TasksView.vue")
    chat = _read("web/src/views/ChatView.vue")
    gallery = _read("web/src/views/GalleryView.vue")
    styles = _read("web/src/styles.css")

    for fragment in (
        "preview: [url: string];",
        "Boolean(task.imageUrl)",
        'class="task-card__thumbnail"',
        "emit('preview', task.imageUrl)",
        ':src="task.imageUrl"',
    ):
        assert fragment in tasks, fragment
    assert "任务详情" not in tasks
    assert '@preview="openPreview"' in chat
    assert "ImageViewer" in gallery
    assert ':items="viewerItems"' in gallery
    assert ".task-card__thumbnail" in styles
    assert ".markdown-image" in styles
    assert "height: auto;" in styles
    assert "max-height: none;" in styles
    assert "object-fit: contain;" in styles
    assert "max-width: 100vw;" in styles
    assert "max-height: 100dvh;" in styles
    assert "object-fit: contain;" in styles
    assert "grid-auto-rows: 1fr;" in styles
    assert (
        ".image-viewer__close,\n.image-viewer__nav,\n.image-viewer__zoom-controls {\n"
        "  position: fixed;"
    ) in styles
    assert "background-color: rgb(22 37 55 / 94%);" in styles
    assert "overscroll-behavior: none;" in styles
    assert "touch-action: none;" in styles


def test_vue_model_and_voice_settings_dom_contract() -> None:
    settings = _read("web/src/views/SettingsView.vue")
    confirm = _read("web/src/components/ConfirmDialog.vue")
    voice = _read("web/src/components/VoiceSettings.vue")
    voice_store = _read("web/src/stores/voice.ts")
    styles = _read("web/src/styles.css")

    for fragment in (
        'class="page-panel settings-panel"',
        'aria-label="模型 ID"',
        'role="listbox"',
        'class="model-option"',
        'class="model-picker"',
        'aria-label="可用模型"',
        "@focus=\"listOpen = true\"",
        "@input=\"handleInput\"",
        "listOpen.value = false",
        "api.setModelSettings(props.sessionId, model)",
        "api.getCachedModelSettings(props.sessionId)",
        ":placeholder=\"activeModel || defaultModel || '默认模型'\"",
        "<VoiceSettings />",
        "<ConfirmDialog",
        ':open="logoutConfirmOpen"',
        'title="确认退出登录？"',
        'confirm-label="确认退出"',
        '@click="logoutConfirmOpen = true"',
    ):
        assert fragment in settings, fragment
    assert 'status.value = "正在加载…"' not in settings
    assert 'status.value = "正在保存…"' not in settings
    assert "当前：" not in settings
    for fragment in (
        'class="confirm-overlay"',
        'class="confirm-dialog"',
        'class="confirm-dialog__actions"',
        "emit('cancel')",
        "emit('confirm')",
    ):
        assert fragment in confirm, fragment

    for fragment in (
        'class="settings-card voice-settings"',
        "AudioLines",
        'aria-label="选择音色"',
        'active?.name || "选择音色"',
        'class="settings-toggle"',
        'class="settings-switch"',
        'type="checkbox"',
        'class="voice-search-row"',
        'class="voice-catalog"',
        'aria-label="搜索音色"',
        'class="voice-list"',
        'class="voice-catalog-panel__header"',
        'class="voice-catalog-backdrop"',
        'class="voice-catalog-panel__close"',
        'class="voice-pager"',
        'class="voice-option"',
        'class="voice-option__star"',
        'class="voice-option__demo"',
        'class="voice-disabled"',
        "selectVoice(id)",
        "toggleFavoriteVoice(voice.id)",
        "setAutoplayAll(($event.target as HTMLInputElement).checked)",
        "await loadVoiceCatalog(refresh)",
        'document.addEventListener("keydown", handleDocumentKeydown)',
        'if (event.key === "Escape" && catalogOpen.value) closeCatalog();',
    ):
        assert fragment in voice, fragment
    assert 'status.value = refresh ? "正在刷新…" : "正在加载…"' not in voice
    assert "当前：" not in voice
    assert "touch-action: manipulation;" in styles
    assert "background: linear-gradient(145deg, #f06f78, #d94f5c) !important;" in styles
    assert ".confirm-overlay" in styles

    for fragment in (
        'const PREFS_KEY = "leon_voice_prefs"',
        "export function loadVoiceCatalog(refresh = false)",
        "if (catalogRequest) return catalogRequest;",
        ".getVoiceCatalog(refresh)",
        "export function selectVoice(id: string)",
        "export function toggleFavoriteVoice(id: string)",
        "export function setAutoplayAll(value: boolean)",
    ):
        assert fragment in voice_store, fragment

    # Regression guard: the shared loader returns a boolean and updates the
    # reactive store; VoiceSettings must not reference a nonexistent `payload`
    # variable after awaiting it.
    assert "status.value = payload.enabled" not in voice
    # Voice catalog is tabbed (all/favorites) and paginated instead of
    # silently collapsing to favorites-only.
    assert 'const voiceTab = ref<"all" | "favorites">("all")' in voice
    assert "const PAGE_SIZE = 20" in voice
    assert "grid-template-rows: auto minmax(0, 1fr) auto;" in styles
    assert ".voice-catalog-panel" in styles and "overflow: hidden;" in styles
    assert ".voice-list" in styles and "overflow-y: auto;" in styles
    assert "align-content: start;" in styles
    assert "grid-auto-rows: minmax(48px, auto);" in styles
