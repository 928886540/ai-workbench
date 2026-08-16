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
    assert 'outDir: "dist"' in vite
    assert 'register("/sw.js?v=vue-9"' in main
    assert "leon-vue-v9" in service_worker


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
        'async sendMessage(sessionId: string, content: string)',
        'async getModelSettings(sessionId: string, refresh = false)',
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
        "api.clearSession();",
        "function logout()",
        "closeEvents();",
        "let lastAgentErrorAt = 0",
        'case "agent.error":',
        "lastAgentErrorAt = Date.now();",
        "if (Date.now() - lastAgentErrorAt > 1000)",
        "finishAssistant(error instanceof Error ? error.message : \"发送失败\", \"error\")",
    ):
        assert fragment in chat, fragment

    # Opening a new session must close the previous EventSource first;
    # transient failures stay on the browser's native reconnect path.
    assert "closeEvents();\n  setConnection(\"正在连接…\", \"neutral\");" in chat
    assert "eventSource = api.connectEvents(" in chat
    assert "eventSource?.close();" in chat
    assert "function handleEventError(source: EventSource): void" in chat
    assert "source.readyState === EventSource.CONNECTING" in chat
    assert "source.readyState === EventSource.CLOSED" in chat
    assert ".checkHealth()" in chat
    assert "expireLogin();" in chat
    assert "function handleOnline(): void" in chat
    assert 'window.addEventListener("online", handleOnline)' in chat
    assert 'window.removeEventListener("online", handleOnline)' in chat


def test_vue_message_bubble_copy_edit_retry_contract() -> None:
    bubble = _read("web/src/components/MessageBubble.vue")
    chat = _read("web/src/views/ChatView.vue")
    messages = _read("web/src/stores/messages.ts")

    for fragment in (
        "navigator.clipboard.writeText(text)",
        'document.execCommand("copy")',
        "function startEditing(): void",
        'emit("edit", props.message.id, editDraft.value)',
        "emit('retry', message.id)",
        'class="message-editor"',
        'class="message-edit-actions"',
        'class="message-toolbar"',
    ):
        assert fragment in bubble, fragment

    for fragment in (
        "function retryMessage(messageId: string): void",
        "const candidate = messages.value[cursor];",
        'if (candidate.role !== "user" || !candidate.text.trim()) continue;',
        "draft.value = candidate.text.trim();",
        "void sendMessage();",
        "function editMessage(messageId: string, text: string): void",
        "if (message) message.text = text;",
        "@retry=\"retryMessage\"",
        "@edit=\"editMessage\"",
    ):
        assert fragment in chat, fragment

    assert "export const messages = ref<ChatMessage[]>([]);" in messages
    assert "export function findMessage(id: string | null): ChatMessage | null" in messages


def test_vue_model_and_voice_settings_dom_contract() -> None:
    settings = _read("web/src/views/SettingsView.vue")
    voice = _read("web/src/components/VoiceSettings.vue")
    voice_store = _read("web/src/stores/voice.ts")

    for fragment in (
        'class="page-panel settings-panel"',
        'aria-label="模型 ID"',
        'role="listbox"',
        'class="model-option"',
        'aria-label="可用模型"',
        "@focus=\"listOpen = true\"",
        "@input=\"handleInput\"",
        "listOpen.value = false",
        "api.setModelSettings(props.sessionId, model)",
        "<VoiceSettings />",
    ):
        assert fragment in settings, fragment

    for fragment in (
        'class="settings-card voice-settings"',
        'class="settings-toggle"',
        'type="checkbox"',
        'class="voice-search-row"',
        'aria-label="搜索音色"',
        'class="voice-list"',
        'class="voice-option"',
        'class="voice-option__star"',
        'class="voice-option__demo"',
        'class="voice-disabled"',
        "selectVoice(id)",
        "toggleFavoriteVoice(voice.id)",
        "setAutoplayAll(($event.target as HTMLInputElement).checked)",
        "await loadVoiceCatalog(refresh)",
    ):
        assert fragment in voice, fragment

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
    assert "status.value = voiceEnabled.value" in voice
