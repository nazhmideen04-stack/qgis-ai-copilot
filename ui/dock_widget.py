import contextlib
import html
import os
import re
import shlex

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import QThread, QTimer, QUrl, Qt, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QFont, QFontDatabase, QKeySequence, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPushButton, QSplitter, QTextBrowser, QTextEdit,
                                 QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)
try:
    from qgis.PyQt.QtGui import QShortcut  # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QShortcut

# Relative imports: the plugin works with any folder name
# (it used to be `qgis_ai_copilot.core` while the folder was `qgis-ai-copilot-main` → ImportError)
from ..core import ai_client
from ..core.context import SCOPE_CHOSEN, SCOPES, get_project_context, resolve_scope
from ..core import commands, field_hints, osm, progress, safety, web_data
from ..core.i18n import LANGUAGES, dynamic, set_language, tr, tr_lines, tr_undo, translate_tree
from ..core.history import RequestArchive
from ..core.log import log_warning
from ..core.scenarios import ScenarioStore
from ..core import user_commands
from ..core.user_commands import CommandStore
from ..core.undo import UNDO
from .dialogs import pick_layers, show_history
from .tool_dialogs import commands_dialog, field_values_dialog, scenarios_dialog, smart_join_dialog

FOLD_LINES = 10            # outputs longer than this are folded in the chat
FOLD_PREVIEW = 6           # lines of a folded output shown before «Show all»
from .status_panel import StatusPanel

S = "ai_copilot"
MAX_HISTORY = 8                  # messages the AI remembers
MAX_DETAILED_HISTORY = 3         # the most recent ones are sent in detail, older ones briefly
MAX_HISTORY_CODE_CHARS = 900     # code of a recent answer kept in the memory
MAX_HISTORY_TEXT_CHARS = 250     # explanation of a recent answer
MAX_HISTORY_RESULT_CHARS = 400   # output/error of a recent run
MAX_LAST_RESULT_CHARS = 6000     # output of the LAST run: the AI needs all of it for its next step
PAGE_TEXT_FOR_AI = 9000          # characters of the attached page's text sent with the question (~2300 tokens)
RESTORE_MEMORY_HOURS = 12        # after a QGIS restart only a recent conversation is restored
MAX_RESULT_CHARS = 2500          # full error sent with «Fix with AI»
OSM_WORDS = re.compile(r"\b(osm|openstreetmap|open\s*street\s*map|overpass|quickosm)\b", re.IGNORECASE)
MAX_AGENT_STEPS = 5       # maximum automatic steps per task
MAX_AUTO_FIXES = 2        # maximum automatic error fixes in a row
CONTINUE_MARKER = "[[CONTINUE]]"
THINKING_AFTER_MS = 1200  # if no characters arrive within this time — show «model is thinking»
UI_REFRESH_SEC = 0.05     # how often to repaint the window while code is running
PROMPT_FONT_PT = 12       # request box font size
PROMPT_FONTS = ("Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", "Ubuntu",
                "Cantarell", "Noto Sans", "Roboto", "Open Sans")
REQUEST_MAX_WIDTH = 980   # the request bar is centred at the bottom and not wider than this
DOCK_AREAS = {"left": Qt.DockWidgetArea.LeftDockWidgetArea, "right": Qt.DockWidgetArea.RightDockWidgetArea,
              "top": Qt.DockWidgetArea.TopDockWidgetArea, "bottom": Qt.DockWidgetArea.BottomDockWidgetArea}
PROMPT_STYLE = ("QTextEdit { border: 1px solid palette(mid); border-radius: 8px; padding: 6px 9px; }"
                "QTextEdit:focus { border: 1px solid palette(highlight); }")


def _prompt_font():
    """A clean modern UI font that exists on this computer (Segoe UI on Windows, Helvetica Neue on macOS,
    Ubuntu / Noto Sans on Linux), slightly smaller than before."""
    try:
        available = set(QFontDatabase.families())      # Qt6: static
    except TypeError:
        available = set(QFontDatabase().families())    # Qt5: instance method
    family = next((f for f in PROMPT_FONTS if f in available), QApplication.font().family())
    font = QFont(family)
    font.setPointSize(PROMPT_FONT_PT)
    return font


def _markdown_light(text):
    """The few Markdown marks the AI uses in explanations: **bold**, `code`, headings and list bullets."""
    lines = []
    for line in html.escape(text).split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = f"<b>{stripped.lstrip('#').strip()}</b>"
        elif stripped[:2] in ("- ", "* "):
            line = "&nbsp;&nbsp;• " + stripped[2:]
        lines.append(line)
    out = "<br>".join(lines)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def _needs_user(error):
    """Errors that code cannot fix — the agent must not auto-retry them."""
    return any(s in error for s in ("PermissionError", "Stopped by user", "Export cancelled",
                                    "User denied", "declined", "No data link", "Risky code was not run"))


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


class StreamWorker(QThread):
    page_signal = pyqtSignal(str)                  # status while the attached page is read
    chunk_signal = pyqtSignal(str)
    retry_signal = pyqtSignal(int, int, int, int)  # (attempt, attempts, delay in s, HTTP code)
    finished_signal = pyqtSignal(str, str)  # (full answer, error)

    def __init__(self, provider, model, api_key, prompt, context, net, fast=True, page_url=None, query=""):
        super().__init__()
        self.args = (provider, model, api_key, prompt, context)
        self.net = net
        self.fast = fast
        self.page_url, self.query = page_url, query
        self._cancel = False

    def _page_context(self):
        """Reads the attached page (once per task, then cached) and returns it as a text block for the AI."""
        from ..core import link_explorer
        info = web_data.cached_page(self.page_url)
        if info is None:
            self.page_signal.emit(self.page_url)
            raw = ai_client.fetch_page(self.page_url, ai_client.Net(), cancel=lambda: self._cancel)
            info = link_explorer.analyse_page(raw, self.page_url, self.query, text_limit=PAGE_TEXT_FOR_AI)
            web_data.cache_page(self.page_url, info)
        return f"\n\nATTACHED PAGE CONTENT ({self.page_url}):\n" + link_explorer.context_block(info)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        parts = []
        if self.page_url:
            try:
                page = self._page_context()
            except ai_client.Cancelled:
                self.finished_signal.emit("", "Stopped by user.")
                return
            except Exception as e:  # the AI can still use read_page() itself
                page = f"\n\nATTACHED PAGE ({self.page_url}) could not be read: {e}. Try read_page() or explore_link()."
            self.args = self.args[:4] + (self.args[4] + page,)
        try:
            for piece in ai_client.stream_completion(*self.args, net=self.net, cancel=lambda: self._cancel,
                                                     fast=self.fast, on_retry=self.retry_signal.emit):
                parts.append(piece)
                self.chunk_signal.emit(piece)
            self.finished_signal.emit("".join(parts), "")
        except ai_client.Cancelled:
            self.finished_signal.emit("".join(parts), "Stopped by user.")
        except ai_client.AIError as e:
            self.finished_signal.emit("".join(parts), str(e))
        except Exception as e:
            self.finished_signal.emit("".join(parts), f"Unexpected error: {type(e).__name__}: {e}")


class ModelsWorker(QThread):
    result_signal = pyqtSignal(str, list, str)  # (provider, models, error)

    def __init__(self, provider, api_key, net):
        super().__init__()
        self.provider, self.api_key, self.net = provider, api_key, net

    def run(self):
        try:
            models = ai_client.list_models(self.provider, self.api_key, self.net)
            self.result_signal.emit(self.provider, models, "")
        except ai_client.AIError as e:
            self.result_signal.emit(self.provider, [], str(e))
        except Exception as e:
            self.result_signal.emit(self.provider, [], f"{type(e).__name__}: {e}")


def _make_dockable(dock):
    """Docks can be attached to any side of the QGIS window, floated and closed."""
    dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
    dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                     | QDockWidget.DockWidgetFeature.DockWidgetFloatable
                     | QDockWidget.DockWidgetFeature.DockWidgetClosable)


class RequestDock(QDockWidget):
    """The request bar: docked at the bottom of the QGIS window, the input box centred, the Run button on its right
    and the live status above it. The settings and the chat are in the other panel, which can be placed anywhere."""

    def __init__(self, parent=None):
        super().__init__("AI Copilot — Request", parent)
        self.setObjectName("AICopilotRequestDock")
        _make_dockable(self)

    def build(self, prompt_input, command_hint, status, run_button):
        outer = QWidget()
        row = QHBoxLayout(outer)
        row.setContentsMargins(6, 4, 6, 6)
        row.addStretch(1)
        center = QWidget()
        center.setMaximumWidth(REQUEST_MAX_WIDTH)
        center.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        column = QVBoxLayout(center)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(status)
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        prompt_input.setMinimumHeight(56)
        prompt_input.setMaximumHeight(160)
        input_row.addWidget(prompt_input, 1)
        run_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        run_button.setMaximumHeight(160)
        input_row.addWidget(run_button)
        column.addLayout(input_row)
        column.addWidget(command_hint)
        row.addWidget(center, 20)
        row.addStretch(1)
        self.setWidget(outer)


class AICopilotDockWidget(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("AI Copilot", parent)
        self.setObjectName("AICopilotDock")
        self.settings = QgsSettings()
        _make_dockable(self)
        self.request_dock = RequestDock(parent)

        self.worker = None
        self.models_worker = None
        self.archive = RequestArchive()
        self.chat_history = self.archive.conversation_entries(MAX_HISTORY,
                                                              max_age_hours=RESTORE_MEMORY_HOURS)
        self.pending_code = ""
        self.last_prompt = ""
        self.pending_entry = None  # history entry that pending_code belongs to
        self.last_failed = None    # (code, error) — for the «Fix with AI» button
        self.pending_answer = ""   # full AI answer that pending_code belongs to
        self.agent_task = None     # the user's original task (agent mode)
        self.agent_steps = 0
        self.agent_fixes = 0
        self.agent_links = []
        self.lang_hint = ""        # answer-language instruction for the current task
        self.chars_received = 0
        self.received_text = ""
        self.executing = False
        self._last_ui_refresh = 0.0
        self.scenarios = ScenarioStore()
        self.task_prompt = ""      # the user's last task and the code that worked for it (for «Save scenario»)
        self.task_codes = []
        self.field_window = None
        self.user_commands = CommandStore()
        self.command_draft = None  # /newcommand: the AI is writing a command, not running code
        self.chat_items = []       # chat content (text and folded blocks) for re-rendering
        self._fold_seq = 0
        self._stream_start = 0

        self.code_format = QTextCharFormat()
        self.code_format.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        self.init_ui()

    # ------------------------------------------------------------------ UI
    def init_ui(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Settings that are changed rarely: folded away by default (gear button), the summary shows the model
        header = QHBoxLayout()
        self.settings_toggle = QToolButton()
        self.settings_toggle.setText("Settings")
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setAutoRaise(True)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_toggle.setToolTip("Show / hide the settings: provider, model, API key and options")
        header.addWidget(self.settings_toggle)
        self.settings_summary = dynamic(QLabel())
        self.settings_summary.setStyleSheet("color: gray; font-size: 11px;")
        header.addWidget(self.settings_summary, 1)
        layout.addLayout(header)
        self.settings_box = QWidget()
        sl = QVBoxLayout(self.settings_box)
        sl.setContentsMargins(4, 0, 0, 6)
        layout.addWidget(self.settings_box)

        sl.addWidget(QLabel("AI Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(ai_client.PROVIDERS)
        sl.addWidget(self.provider_combo)

        sl.addWidget(QLabel("Model:"))
        model_layout = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setToolTip("Pick from the list or type the model name manually")
        model_layout.addWidget(self.model_combo, 1)
        self.fetch_models_btn = dynamic(QPushButton("Refresh"), "text")
        self.fetch_models_btn.setMinimumWidth(70)
        self.fetch_models_btn.setToolTip("Load models (also checks the API key)")
        self.fetch_models_btn.clicked.connect(self.fetch_available_models)
        model_layout.addWidget(self.fetch_models_btn)
        sl.addLayout(model_layout)

        sl.addWidget(QLabel("API Key:"))
        key_layout = QHBoxLayout()
        self.api_key_input = dynamic(QLineEdit(), "placeholder")
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.editingFinished.connect(self.on_api_key_finished)
        key_layout.addWidget(self.api_key_input, 1)
        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setMinimumWidth(55)
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setToolTip("Show / hide key")
        self.show_key_btn.toggled.connect(lambda on: self.api_key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_layout.addWidget(self.show_key_btn)
        sl.addLayout(key_layout)

        self.plugins_cb = QCheckBox("Use installed plugins (send plugin list to the AI)")
        self.plugins_cb.setToolTip("Lets the AI use Processing algorithms, menus and objects of your other plugins")
        self.plugins_cb.setChecked(_as_bool(self.settings.value(f"{S}/use_plugins", True), True))
        self.plugins_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/use_plugins", v))
        sl.addWidget(self.plugins_cb)

        # Chat (top) and the controls with the request box (bottom) share a splitter: drag it to resize
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(QLabel("Chat / Output:"))
        self.log_display = QTextBrowser()
        # Links (websites and created files) are opened by us: otherwise file:// would load inside the chat
        self.log_display.setOpenLinks(False)
        self.log_display.anchorClicked.connect(self.on_chat_link)
        self.log_display.setMinimumHeight(80)
        top_layout.addWidget(self.log_display, 1)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        self.fast_cb = QCheckBox("Fast answers (less model thinking)")
        self.fast_cb.setToolTip("Limits the internal 'thinking' of models that support it (Gemini 2.5 / 3, OpenAI o3, "
                                "o4, gpt-5). Much faster answers; switch off for very complex tasks.")
        self.fast_cb.setChecked(_as_bool(self.settings.value(f"{S}/fast_answers", True), True))
        self.fast_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/fast_answers", v))
        sl.addWidget(self.fast_cb)

        self.agent_cb = QCheckBox("Agent mode: multi-step tasks and automatic error fixing")
        self.agent_cb.setToolTip(
            "The AI may first inspect algorithms/data, then continue, and automatically asks for a fix when code "
            f"fails (max {MAX_AGENT_STEPS} steps). With Auto-run off you still confirm every step with Run Code.")
        self.agent_cb.setChecked(_as_bool(self.settings.value(f"{S}/agent_mode", True), True))
        self.agent_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/agent_mode", v))
        sl.addWidget(self.agent_cb)

        options = QHBoxLayout()
        self.auto_run_cb = QCheckBox("Auto-run code")
        self.auto_run_cb.setToolTip("Execute AI code immediately. Off = review first, then press Run Code")
        self.auto_run_cb.setChecked(_as_bool(self.settings.value(f"{S}/auto_run", False)))
        self.auto_run_cb.toggled.connect(self.on_auto_run_toggled)
        options.addWidget(self.auto_run_cb)
        self.run_manual_btn = QPushButton("Run Code")
        self.run_manual_btn.setEnabled(False)
        self.run_manual_btn.clicked.connect(self.execute_pending_code)
        options.addWidget(self.run_manual_btn)
        self.fix_btn = QPushButton("Fix with AI")
        self.fix_btn.setToolTip("Send the error back to the AI and ask for corrected code")
        self.fix_btn.setEnabled(False)
        self.fix_btn.clicked.connect(self.fix_with_ai)
        options.addWidget(self.fix_btn)
        self.clear_mem_btn = QPushButton("Clear")
        self.clear_mem_btn.setToolTip("Clear chat and AI memory")
        self.clear_mem_btn.clicked.connect(self.clear_memory)
        options.addWidget(self.clear_mem_btn)
        bottom_layout.addLayout(options)

        tools = QHBoxLayout()
        self.undo_btn = dynamic(QPushButton("Undo AI"), "tip")
        self.undo_btn.setToolTip("Undo the last AI action (code, command, scenario or join)")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.undo_last)
        tools.addWidget(self.undo_btn)
        self.scenarios_btn = QPushButton("Scenarios…")
        self.scenarios_btn.setToolTip("Saved scenarios: save a task with its code and run it again without the AI")
        self.scenarios_btn.clicked.connect(self.open_scenarios)
        tools.addWidget(self.scenarios_btn)
        self.history_btn = QPushButton("History")
        self.history_btn.setToolTip("Archive of your requests: search, view and send again")
        self.history_btn.clicked.connect(self.open_history)
        tools.addWidget(self.history_btn)
        bottom_layout.addLayout(tools)

        extra = QHBoxLayout()
        self.hints_cb = QCheckBox("Field value hints for the AI")
        self.hints_cb.setToolTip("Sends the ranges and typical values of fields of the working layers to the AI, so it "
                                 "uses real values in filters. Switch off to send only field names.")
        self.hints_cb.setChecked(_as_bool(self.settings.value(f"{S}/field_hints", True), True))
        self.hints_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/field_hints", v))
        extra.addWidget(self.hints_cb)
        self.background_cb = QCheckBox("Heavy work in background")
        self.background_cb.setToolTip("Processing algorithms run in a background thread: QGIS does not freeze and "
                                      "Stop reacts at once. Switch off only if an algorithm misbehaves.")
        background = _as_bool(self.settings.value(f"{S}/background_processing", True), True)
        self.background_cb.setChecked(background)
        progress.set_background(background)
        self.background_cb.toggled.connect(self.on_background_toggled)
        extra.addWidget(self.background_cb)
        extra.addStretch(1)
        sl.addLayout(extra)
        self.safety_cb = QCheckBox("Ask before risky code")
        self.safety_cb.setToolTip("Even with Auto-run, asks before code that deletes or moves files, runs system "
                                  "commands, runs hidden code or writes straight into layer files (SHP, GPKG…) — "
                                  "changes that Undo AI cannot revert.")
        self.safety_cb.setChecked(_as_bool(self.settings.value(f"{S}/safety_check", True), True))
        self.safety_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/safety_check", v))
        sl.addWidget(self.safety_cb)
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("Interface language:"))
        self.lang_combo = QComboBox()
        self.lang_combo.setProperty("ai_translate_items", True)
        for key, title in LANGUAGES:
            self.lang_combo.addItem(title, key)
        saved_lang = str(self.settings.value(f"{S}/language", "auto") or "auto")
        if saved_lang not in [key for key, _title in LANGUAGES]:
            saved_lang = "auto"  # an old choice that is no longer offered
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(saved_lang)))
        self.lang_combo.currentIndexChanged.connect(self.on_language_changed)
        lang_row.addWidget(self.lang_combo, 1)
        sl.addLayout(lang_row)


        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Layers for AI:"))
        self.scope_combo = QComboBox()
        self.scope_combo.setProperty("ai_translate_items", True)
        for key, title in SCOPES:
            self.scope_combo.addItem(title, key)
        saved_scope = str(self.settings.value(f"{S}/layer_scope", "all") or "all")
        self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(saved_scope)))
        self.scope_combo.currentIndexChanged.connect(self.on_scope_changed)
        scope_row.addWidget(self.scope_combo, 1)
        self.choose_layers_btn = QPushButton("Choose…")
        self.choose_layers_btn.setToolTip("Choose the layers the AI should work with")
        self.choose_layers_btn.clicked.connect(self.choose_layers)
        scope_row.addWidget(self.choose_layers_btn)
        bottom_layout.addLayout(scope_row)
        self.scope_label = dynamic(QLabel())
        self.scope_label.setStyleSheet("color: gray; font-size: 11px;")
        bottom_layout.addWidget(self.scope_label)

        link_header = QHBoxLayout()
        self.link_toggle = QToolButton()
        self.link_toggle.setText("Data link")
        self.link_toggle.setCheckable(True)
        self.link_toggle.setAutoRaise(True)
        self.link_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.link_toggle.setToolTip("Show / hide the data link field")
        link_header.addWidget(self.link_toggle)
        self.link_summary = dynamic(QLabel())
        self.link_summary.setStyleSheet("color: gray; font-size: 11px;")
        link_header.addWidget(self.link_summary, 1)
        bottom_layout.addLayout(link_header)
        self.link_box = QWidget()
        link_box_layout = QVBoxLayout(self.link_box)
        link_box_layout.setContentsMargins(0, 0, 0, 0)
        link_box_layout.addWidget(QLabel("The AI reads this page and uses the internet only with your link:"))
        link_layout = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("Paste a link to a website, file or API with the data you need…")
        self.link_input.setClearButtonEnabled(True)
        link_layout.addWidget(self.link_input, 1)
        self.link_open_btn = QPushButton("Open")
        self.link_open_btn.setMinimumWidth(60)
        self.link_open_btn.setToolTip("Open the link in your browser")
        self.link_open_btn.clicked.connect(self.open_link)
        link_layout.addWidget(self.link_open_btn)
        link_box_layout.addLayout(link_layout)
        bottom_layout.addWidget(self.link_box)
        self.link_input.textChanged.connect(lambda _t: self.update_link_summary())
        self.link_toggle.toggled.connect(self.set_link_expanded)

        self.prompt_input = QTextEdit()
        self.prompt_input.setAcceptRichText(False)
        self.prompt_input.setFont(_prompt_font())
        self.prompt_input.setStyleSheet(PROMPT_STYLE)
        self.prompt_input.setMinimumHeight(64)
        self.prompt_input.setPlaceholderText("e.g. Create a 250 m buffer around selected features\n"
                                             "Type / for quick commands without AI · Ctrl+Enter to send")
        self.prompt_input.textChanged.connect(self.update_command_hint)
        self.command_hint = dynamic(QLabel())
        self.command_hint.setWordWrap(True)
        self.command_hint.setStyleSheet("color: gray; font-size: 11px;")
        self.command_hint.hide()
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(keys), self.prompt_input)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.send_from_keyboard)

        self.status = StatusPanel()
        bottom_layout.addStretch(1)

        # The settings scroll: the panel stays small enough to dock anywhere in QGIS
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(bottom)
        scroll.setMinimumHeight(60)
        self.splitter.addWidget(top)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        saved = self.settings.value(f"{S}/splitter")
        if saved:
            self.splitter.restoreState(saved)
        self.splitter.splitterMoved.connect(self._save_splitter)
        layout.addWidget(self.splitter, 1)
        self.set_link_expanded(_as_bool(self.settings.value(f"{S}/link_expanded", False)))

        self.run_button = dynamic(QPushButton("Run with AI"), "text")
        self.run_button.setMinimumHeight(34)
        self.run_button.setStyleSheet("QPushButton { font-weight: bold; padding: 4px 14px; }")
        self.run_button.clicked.connect(self.on_run_clicked)
        self.request_dock.build(self.prompt_input, self.command_hint, self.status, self.run_button)

        widget.setMinimumWidth(0)
        self.setWidget(widget)
        self.dockLocationChanged.connect(self._save_place)
        self.settings_toggle.toggled.connect(self.set_settings_expanded)
        set_language(saved_lang)
        self.retranslate()
        self.topLevelChanged.connect(lambda _f: self._save_place())

        last_provider = self.settings.value(f"{S}/last_provider", "Google Gemini")
        idx = self.provider_combo.findText(str(last_provider))
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)
        self.on_provider_changed(self.provider_combo.currentText())
        self._connect_layer_signals()
        self.update_scope_label()
        self._update_undo_btn()
        has_key = self.is_local() or bool(self.current_key())
        self.set_settings_expanded(_as_bool(self.settings.value(f"{S}/settings_expanded", not has_key), not has_key))
        self.model_combo.currentTextChanged.connect(lambda _t: self.update_settings_summary())
        self.update_settings_summary()
        if self.chat_history:
            self.append_text(f"Memory: {len(self.chat_history)} message(s) of your last conversation restored. "
                             "Press Clear to start a new conversation.", "gray")

    # ------------------------------------------------------------------ helpers
    def net(self):
        return ai_client.Net()  # QGIS network settings (proxy, SSL, authentication) apply automatically

    def current_key(self):
        return ai_client.clean_api_key(self.api_key_input.text())

    def is_local(self, provider=None):
        return (provider or self.provider_combo.currentText()) in ai_client.LOCAL_PROVIDERS

    def append_html(self, html_text):
        self.chat_items.append({"html": html_text})
        self.log_display.append(html_text)
        self._scroll_down()

    # ------------------------------------------------------------------ compact chat: folded code and long output
    def add_fold(self, kind, body, open_=False, color=None, tail=False):
        """Adds a folded block: kind "code" (hidden until clicked) or "output" / "error" (first / last lines shown,
        the rest behind «Show all»). Returns the block id."""
        self._fold_seq += 1
        item = {"fold": self._fold_seq, "kind": kind, "body": body, "open": open_, "color": color, "tail": tail}
        self.chat_items.append(item)
        self.log_display.append(self._fold_html(item))
        self._scroll_down()
        return self._fold_seq

    def _fold_html(self, item):
        link = f"aicopilot://fold/{item['fold']}"
        lines = item["body"].rstrip().splitlines()
        color = f" style='color:{item['color']}'" if item.get("color") else ""
        if item["kind"] == "code":
            title = html.escape(tr(f"Code — {len(lines)} line(s)"))
            if not item["open"]:
                return (f"<a href='{link}'>▸ {html.escape(tr('Show code'))}</a> "
                        f"<span style='color:gray'>{title}</span>")
            return (f"<a href='{link}'>▾ {html.escape(tr('Hide code'))}</a> <span style='color:gray'>{title}</span>"
                    f"<pre>{html.escape(item['body'])}</pre>")
        if len(lines) <= FOLD_LINES or item["open"]:
            hide = f"<a href='{link}'>▴ {html.escape(tr('Hide'))}</a>" if len(lines) > FOLD_LINES else ""
            return f"<pre{color}>{html.escape(item['body'])}</pre>{hide}"
        shown = lines[-FOLD_PREVIEW:] if item.get("tail") else lines[:FOLD_PREVIEW]
        more = html.escape(tr(f"{len(lines) - FOLD_PREVIEW} more line(s)"))
        text = html.escape("\n".join(shown))
        text = ("…\n" + text) if item.get("tail") else (text + "\n…")
        return f"<pre{color}>{text}</pre><a href='{link}'>▾ {html.escape(tr('Show all'))}</a> " \
               f"<span style='color:gray'>({more})</span>"

    def on_chat_link(self, url):
        if url.scheme() == "aicopilot":
            try:
                fold_id = int(url.path().strip("/"))
            except ValueError:
                return
            for item in self.chat_items:
                if item.get("fold") == fold_id:
                    item["open"] = not item["open"]
                    self._rerender_chat(keep_position=True)
                    return
            return
        QDesktopServices.openUrl(url)

    def _rerender_chat(self, keep_position=False):
        bar = self.log_display.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        old = bar.value()
        parts = [self._fold_html(i) if "fold" in i else i["html"] for i in self.chat_items]
        self.log_display.setHtml("".join(f"<div>{p}</div>" for p in parts))
        if keep_position and not at_bottom:
            bar.setValue(old)
        else:
            self._scroll_down()

    def add_answer(self, answer):
        """The AI answer in the chat: the explanation as text, every code block folded."""
        from ..core.executor import extract_code
        pieces = re.split(r"(```[^\n]*\n.*?(?:```|$))", answer, flags=re.DOTALL)
        for piece in pieces:
            if piece.startswith("```"):
                code, _w = extract_code(piece if piece.rstrip().endswith("```") and piece.count("```") >= 2
                                        else piece + "\n```")
                if code.strip():
                    self.add_fold("code", code)
                continue
            text = piece.replace(CONTINUE_MARKER, "").strip()
            if text:
                self.append_html(_markdown_light(text))

    @staticmethod
    def _visible_prose(text):
        """What is shown while the answer streams: the explanation without code blocks."""
        shown = re.sub(r"```[^\n]*\n.*?```", "\n", text, flags=re.DOTALL)
        writing = ""
        if "```" in shown:  # an unfinished code block is being written
            start = shown.index("```")
            lines = shown[start:].count("\n")
            shown = shown[:start]
            writing = "\n" + tr("Writing code…") + f" ({lines})"
        shown = re.sub(r"`{1,2}$", "", shown).replace(CONTINUE_MARKER, "")
        return re.sub(r"\n{3,}", "\n\n", shown).strip() + writing

    def append_text(self, text, color=None, bold=False):
        safe = html.escape(tr_lines(text)).replace("\n", "<br>")
        if bold:
            safe = f"<b>{safe}</b>"
        if color:
            safe = f"<span style='color:{color}'>{safe}</span>"
        self.append_html(safe)

    def _scroll_down(self):
        bar = self.log_display.verticalScrollBar()
        bar.setValue(bar.maximum())

    def save_settings(self):
        provider = self.provider_combo.currentText()
        self.settings.setValue(f"{S}/last_provider", provider)
        model = self.model_combo.currentText().strip()
        if model:
            self.settings.setValue(f"{S}/{provider}_last_model", model)
        if not self.is_local(provider):
            self.settings.setValue(f"{S}/{provider}_api_key", self.current_key())

    # ------------------------------------------------------------------ provider / key / models
    def on_provider_changed(self, provider):
        self.settings.setValue(f"{S}/last_provider", provider)
        saved_key = str(self.settings.value(f"{S}/{provider}_api_key", "") or "")
        self.api_key_input.blockSignals(True)
        self.api_key_input.setText(saved_key)
        self.api_key_input.blockSignals(False)

        local = self.is_local(provider)
        self.api_key_input.setEnabled(not local)
        self.show_key_btn.setEnabled(not local)
        self.api_key_input.setPlaceholderText(tr("Not needed for local AI") if local else tr("Paste API key here..."))

        self.model_combo.clear()
        last_model = str(self.settings.value(f"{S}/{provider}_last_model", "") or "")
        if last_model:
            self.model_combo.addItem(last_model)
            self.model_combo.setCurrentText(last_model)

        if local or saved_key:
            self.fetch_available_models()
        self.update_settings_summary()

    def on_api_key_finished(self):
        provider = self.provider_combo.currentText()
        key = self.current_key()
        if key != self.api_key_input.text():
            self.api_key_input.setText(key)  # show the cleaned key
        saved = str(self.settings.value(f"{S}/{provider}_api_key", "") or "")
        if key and key != saved:
            self.save_settings()
            self.fetch_available_models()
        self.update_settings_summary()

    def fetch_available_models(self):
        if self.models_worker is not None and self.models_worker.isRunning():
            return
        provider = self.provider_combo.currentText()
        key = self.current_key()
        if not self.is_local(provider) and not key:
            self.append_text(f"Enter the API key for {provider} first.", "orange")
            return
        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("…")
        if not self._busy():
            self.status.start("models", f"Connecting to {provider}…", "Checking the API key and loading the model list")
        self.models_worker = ModelsWorker(provider, key, self.net())
        self.models_worker.result_signal.connect(self.on_models_loaded)
        self.models_worker.start()

    def on_models_loaded(self, provider, models, error):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText(tr("Refresh"))
        if not self._busy():
            self.status.stop()
        if provider != self.provider_combo.currentText():
            return  # the user has already switched the provider
        if error:
            self.append_text(f"Error: {error}", "red")
            return
        if not models:
            self.append_text(f"{provider}: no models found. Type the model name manually.", "orange")
            return
        current = self.model_combo.currentText().strip()
        last = str(self.settings.value(f"{S}/{provider}_last_model", "") or "")
        self.model_combo.clear()
        self.model_combo.addItems(models)
        for candidate in (current, last):
            if candidate in models:
                self.model_combo.setCurrentText(candidate)
                break
        self.append_text(f"{provider}: connected, {len(models)} models loaded.", "green")

    # ------------------------------------------------------------------ chat
    def _busy(self):
        return self.executing or (self.worker is not None and self.worker.isRunning())

    def on_auto_run_toggled(self, checked):
        if checked:
            answer = QMessageBox.warning(
                self, tr("Enable auto-run?"),
                tr("AI-generated Python code will be executed immediately, without review.\n\n"
                   "This code runs inside QGIS with full access to your project and files "
                   "and can modify or delete data.\n\nEnable auto-run anyway?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.auto_run_cb.blockSignals(True)
                self.auto_run_cb.setChecked(False)
                self.auto_run_cb.blockSignals(False)
                checked = False
        self.settings.setValue(f"{S}/auto_run", checked)

    def open_history(self):
        prompt = show_history(self.archive, self)
        if prompt:
            self.prompt_input.setPlainText(prompt)
            self.prompt_input.setFocus()

    def clear_memory(self):
        self.chat_history.clear()
        self.archive.new_conversation()  # the archive keeps the old requests
        self.pending_code = ""
        self.pending_entry = None
        self.last_failed = None
        self.agent_task = None
        self.run_manual_btn.setEnabled(False)
        self.fix_btn.setEnabled(False)
        self.log_display.clear()
        self.chat_items.clear()
        self.append_text("Memory cleared.", "gray")

    def on_run_clicked(self):
        if self.executing:
            progress.request_cancel()
            self.agent_task = None
            self.run_button.setEnabled(False)
            self.run_button.setText(tr("Stopping…"))
            self.status.set_stage("stopping", "Stopping the running code…",
                                  "Waiting for the current step to react to the cancel request")
            return
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.agent_task = None  # Stop also interrupts the agent's chain of steps
            self.run_button.setEnabled(False)
            self.run_button.setText(tr("Stopping…"))
            self.status.set_stage("stopping", "Stopping…", "Waiting for the provider to close the stream")
            return
        self.run_ai_process()

    def fix_with_ai(self):
        if not self.last_failed or (self.worker is not None and self.worker.isRunning()):
            return
        code, error = self.last_failed
        task = f"Original task: {self.agent_task}\n" if self.agent_task else ""
        prompt = (f"{task}The previous code failed. Fix it and return the complete corrected script.\n"
                  f"Error:\n{error[-MAX_RESULT_CHARS:]}\n\nFailed code:\n```python\n{code}\n```")
        self.run_ai_process(prompt, shown_prompt="Fix the previous error", agent_continue=True)

    # ------------------------------------------------------------------ agent
    def _agent_next(self, success, answer):
        """After code runs, decides whether the agent needs another step."""
        if not self.agent_cb.isChecked() or not self.agent_task:
            return
        if self.agent_steps >= MAX_AGENT_STEPS:
            self.append_text(f"Agent stopped: step limit ({MAX_AGENT_STEPS}) reached. "
                             "Write a new message to continue.", "orange")
            self.agent_task = None
            return
        if not success:
            if self.agent_fixes >= MAX_AUTO_FIXES:
                self.append_text("Agent stopped after several failed fixes. "
                                 "Check the error or rephrase the task.", "orange")
                self.agent_task = None
                return
            self.agent_fixes += 1
            self.agent_steps += 1
            self.append_text(f"Auto-fix attempt {self.agent_fixes}/{MAX_AUTO_FIXES}…", "#6a1b9a")
            QTimer.singleShot(300, self.fix_with_ai)
            return
        self.agent_fixes = 0
        if CONTINUE_MARKER in answer:
            self.agent_steps += 1
            prompt = (f"Continue the task: {self.agent_task}\n"
                      "The output of your previous step is in the conversation history. Do the next step. "
                      f"Write {CONTINUE_MARKER} only if yet another step is required.")
            shown = f"Step {self.agent_steps + 1}: continue"
            QTimer.singleShot(300, lambda: self.run_ai_process(prompt, shown_prompt=shown, agent_continue=True))
        else:
            self.agent_task = None  # task completed

    def run_ai_process(self, prompt_override=None, shown_prompt=None, agent_continue=False):
        provider = self.provider_combo.currentText()
        model = self.model_combo.currentText().strip()
        key = self.current_key()
        prompt = (prompt_override or self.prompt_input.toPlainText()).strip()

        if not prompt:
            return
        if prompt_override is None and commands.is_command(prompt):
            self.run_command(prompt)  # quick command: no AI, no tokens
            return
        if not self.is_local(provider) and not key:
            self.append_text(f"Enter the API key for {provider}.", "red")
            return
        if not model:
            self.append_text("Choose a model (press Refresh) or type its name.", "red")
            return

        self.save_settings()
        if not agent_continue:  # a new task from the user
            self.agent_task = prompt
            self.agent_steps = 0
            self.agent_fixes = 0
            self.agent_links = self._user_links(prompt)
            self.lang_hint = ai_client.language_hint(prompt)  # kept for agent steps and "Fix with AI"
            web_data.set_user_query(prompt)  # the request's words rank the data found on the user's link
            web_data.clear_page_cache()
            osm.set_osm_request(OSM_WORDS.search(prompt) is not None)
            field_hints.clear_cache()  # values may have changed since the last task
            self.task_prompt, self.task_codes = prompt, []
        web_data.set_user_links(self.agent_links)
        self.last_prompt = shown_prompt or prompt
        self.pending_code = ""
        self.pending_entry = None
        self.run_manual_btn.setEnabled(False)
        self.fix_btn.setEnabled(False)
        self.append_html(f"<br><b>{tr('You:')}</b> {html.escape(self.last_prompt).replace(chr(10), '<br>')}")
        self.append_html(f"<b>{tr('AI:')}</b>")
        self.log_display.append("")  # separate paragraph for the streamed answer
        self._stream_start = self.log_display.document().characterCount() - 1
        if prompt_override is None:
            self.prompt_input.clear()

        step_info = f"Agent step {self.agent_steps + 1}/{MAX_AGENT_STEPS}" if agent_continue and self.agent_task else ""
        self.status.start("agent" if step_info else "context", "Collecting project context…",
                          step_info or "Layers, CRS, map extent" + (", plugins" if self.plugins_cb.isChecked() else ""))
        self.run_button.setText(tr("Stop"))
        QApplication.processEvents()

        try:
            working, scope_note = self.working_layers()
            self.scope_label.setText(tr(scope_note))
            self.status.set_detail(scope_note)
            project_ctx = get_project_context(working, hints=self.hints_cb.isChecked())
        except Exception as e:
            project_ctx = f"(project context unavailable: {e})"
        context = f"PROJECT CONTEXT:\n{project_ctx}"
        if self.agent_links:
            context += "\n\nUSER DATA LINKS (internet access allowed for these):\n" + "\n".join(self.agent_links)
        else:
            context += "\n\nUSER DATA LINKS: none (no internet except download_osm)."

        if self.plugins_cb.isChecked():
            try:
                from ..core.plugins import get_plugins_context
                context += "\n\n" + get_plugins_context(prompt)
            except Exception as e:
                log_warning(f"Could not collect plugin context: {e}")
                context += f"\n\n(plugin context unavailable: {e})"

        recent = self.chat_history[-MAX_HISTORY:]
        history = "".join(self._history_item(h, brief=i < len(recent) - MAX_DETAILED_HISTORY,
                                             last=i == len(recent) - 1)
                          for i, h in enumerate(recent))
        if history:
            context += f"\n\nPREVIOUS CONVERSATION:\n{history}"

        self.chars_received = 0
        self.received_text = ""
        self.status.set_stage("connect", f"Sending request to {provider}…",
                              f"Model: {model}" + (f" · {step_info}" if step_info else ""))

        hint = getattr(self, "lang_hint", "") or ai_client.language_hint(prompt)
        page_url = self.agent_links[0] if self.agent_links else None
        self.worker = StreamWorker(provider, model, key, f"{prompt}\n\n{hint}", context, self.net(),
                                   self.fast_cb.isChecked(), page_url, web_data.user_query())
        self.worker.page_signal.connect(lambda url: self.status.set_stage(
            "download", "Reading the attached page…", url[:100]))
        self.worker.chunk_signal.connect(self.on_ai_chunk)
        self.worker.retry_signal.connect(self.on_ai_retry)
        self.worker.finished_signal.connect(self.on_ai_finished)
        self.worker.start()
        QTimer.singleShot(THINKING_AFTER_MS, self._maybe_thinking)

    def _maybe_thinking(self):
        if self.worker is not None and self.worker.isRunning() and self.chars_received == 0:
            model = self.model_combo.currentText().strip()
            self.status.set_stage("thinking", f"{model} is thinking…",
                                  "Connected. Waiting for the first words — reasoning models may think "
                                  "for up to a minute before answering.")

    @staticmethod
    def _history_item(h, brief=False, last=False):
        """Conversation memory entry. Recent ones: explanation, code and result; older ones (brief): a short
        summary, so the AI remembers the whole dialog without large requests."""
        from ..core.executor import extract_code
        answer = h.get("answer", "")
        code, _warning = extract_code(answer)
        prose = re.sub(r"```.*?(```|$)", " ", answer, flags=re.DOTALL).replace("[[CONTINUE]]", "")
        prose = re.sub(r"\s+", " ", prose).strip()
        result = h.get("result", "")
        if brief:
            text = f"User: {h['prompt'][:150]}\nAI: {prose[:120]}" + (" (code omitted)" if code else "") + "\n"
            return text + (f"Result: {result[-100:]}\n" if result else "") + "\n"
        text = f"User: {h['prompt'][:500]}\n"
        if prose:
            text += f"AI answer: {prose[:MAX_HISTORY_TEXT_CHARS]}\n"
        if code:
            text += f"AI code:\n{code[:MAX_HISTORY_CODE_CHARS]}\n"
        if result:
            limit = MAX_LAST_RESULT_CHARS if last else MAX_HISTORY_RESULT_CHARS
            # the start of an output holds the most relevant findings (e.g. the ranked list of explore_link)
            text += f"Result: {result[:limit] if last else result[-limit:]}\n"
        return text + "\n"

    def on_ai_retry(self, attempt, attempts, delay, code):
        provider = self.provider_combo.currentText()
        reason = "is busy" if code in (503, 529) else "limits requests" if code == 429 else "had a server error"
        self.status.set_stage("thinking", f"{provider} {reason} (HTTP {code}) — retrying in {delay} s",
                              f"Attempt {attempt}/{attempts}. Press Stop to cancel. Busy periods are usually short.")
        self.append_text(f"{provider} {reason} (HTTP {code}). Retrying automatically in {delay} s "
                         f"(attempt {attempt}/{attempts})…", "orange")
        self.log_display.append("")
        self._stream_start = self.log_display.document().characterCount() - 1

    def on_ai_chunk(self, chunk):
        self.chars_received += len(chunk)
        self.received_text += chunk
        in_code = self.received_text.count("```") % 2 == 1
        seconds = max(self.status.elapsed(), 0.1)
        last_line = self.received_text.rstrip().splitlines()[-1][:70] if self.received_text.strip() else ""
        self.status.set_stage("writing", "Writing code…" if in_code else "AI is answering…",
                              f"{self.chars_received} characters · {self.chars_received / seconds:.0f}/s"
                              + (f"\n{last_line}" if last_line else ""))
        cursor = QTextCursor(self.log_display.document())
        cursor.setPosition(self._stream_start)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(self._visible_prose(self.received_text))
        self._scroll_down()

    def on_ai_finished(self, ai_response, error_msg):
        cursor = QTextCursor(self.log_display.document())  # the streamed preview is replaced by the final answer
        cursor.setPosition(self._stream_start)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        if ai_response.strip():
            self.add_answer(ai_response)
        took = self.status.elapsed()
        self.status.stop()
        self.run_button.setEnabled(True)
        self.run_button.setText(tr("Run with AI"))
        self.append_text(f"(took {took:.1f}s)", "gray")

        provider, model = self.provider_combo.currentText(), self.model_combo.currentText().strip()
        if error_msg:
            self.append_text(f"Error: {error_msg}", "red", bold=True)
            self.agent_task = None
            self.command_draft = None
            self.archive.add(self.last_prompt, ai_response, provider, model, in_memory=False,
                             result=f"Error: {error_msg}")
            return  # a cut-off/failed answer is never executed
        if not ai_response.strip():
            self.append_text("The AI returned an empty answer.", "orange")
            self.command_draft = None
            return

        if self.command_draft is not None:  # /newcommand: save the code as a command instead of running it
            self.archive.add(self.last_prompt, ai_response, provider, model, in_memory=False)
            self._finish_command_draft(ai_response)
            return
        entry = self.archive.add(self.last_prompt, ai_response, provider, model)
        self.chat_history.append(entry)
        del self.chat_history[:-MAX_HISTORY]

        try:
            from ..core import executor
            code, warning = executor.extract_code(ai_response)
        except Exception as e:
            self.append_text(f"Error: could not load the executor: {e}", "red")
            return

        if warning:
            self.append_text(f"Warning: {warning}", "orange")
        if not code or executor.is_comment_only(code):
            self.agent_task = None
            return  # a plain text answer — nothing to run

        self.pending_code = code
        self.pending_entry = entry
        self.pending_answer = ai_response
        if self.auto_run_cb.isChecked():
            self.execute_pending_code()
        else:
            self.run_manual_btn.setEnabled(True)
            self.run_manual_btn.setStyleSheet("background-color:#2e7d32; color:white; font-weight:bold;")
            self.append_text("Code is ready — press «Run Code» to execute it (or enable Auto-run to run code "
                             "automatically).", "#2e7d32", bold=True)

    def execute_pending_code(self):
        if not self.pending_code:
            return
        code = self.pending_code
        self.run_manual_btn.setEnabled(False)
        self.run_manual_btn.setStyleSheet("")
        success, msg, output = self._execute([code], self.last_prompt, entry=self.pending_entry)
        if not success and _needs_user(msg):
            self.agent_task = None  # code cannot fix this — stop instead of looping
            self.append_text("Agent stopped: this needs your action (permission, a data link or a cancelled "
                             "dialog). Nothing was retried.", "orange")
        if msg == "Stopped by user.":
            self.agent_task = None

        if success:
            self.last_failed = None
            self.fix_btn.setEnabled(False)
            self.task_codes.append(code)  # «Scenarios → Save last task with its code»
            result = f"Success.\n{output}" if output else "Success (no printed output)."
        else:
            self.last_failed = (code, msg)
            self.fix_btn.setEnabled(True)
            result = f"FAILED:\n{msg}" + (f"\nPrinted before failing:\n{output}" if output else "")

        # The result goes into the history — the AI will see it in the next request
        if self.pending_entry is not None:
            self.pending_entry["result"] = result
            if self.pending_entry.get("id"):
                self.archive.update(self.pending_entry["id"], result, success)
        answer = self.pending_answer
        self.pending_code = ""
        self.pending_entry = None
        self.pending_answer = ""
        self._agent_next(success, answer)

    def _confirm_risky(self, codes):
        """Asks before code that Undo AI cannot revert or that reaches outside QGIS. True = run it."""
        reasons = []
        for code in codes:
            reasons += [r for r in safety.check(code) if r not in reasons]
        if not reasons:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("Risky code"))
        lead = ("Этот код:" if tr("Run anyway") != "Run anyway" else "This code:")
        tail = ("«Отменить ИИ» не сможет вернуть такие изменения. Запустить?" if tr("Run anyway") != "Run anyway"
                else "Undo AI cannot revert such changes. Run it?")
        box.setText(lead + "\n• " + "\n• ".join(safety.describe(reasons)) + "\n\n" + tail)
        box.setDetailedText("\n\n".join(codes))
        run = box.addButton(tr("Run anyway"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("Don't run"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is run

    def _execute(self, codes, label, entry=None, check=True):
        """Runs one or more code blocks as ONE undoable action and shows the output in the chat.
        Returns (success, message, printed output) of the last block that ran."""
        from ..core import executor
        if check and self.safety_cb.isChecked() and not self._confirm_risky(codes):
            self.append_text("Risky code was not run.", "orange", bold=True)
            return False, "Risky code was not run.", ""
        self._set_executing(True)
        snapshot = UNDO.begin(label)
        if snapshot is not None:
            snapshot.entry = entry
        outputs = []
        success, msg = True, "Code executed successfully."
        try:
            for code in codes:
                success, msg, output = executor.run_pyqgis_code(code)
                if output:
                    outputs.append(output)
                if not success:
                    break
        except Exception as e:
            success, msg = False, f"{type(e).__name__}: {e}"
        finally:
            took = self.status.elapsed()
            summary, not_undoable = UNDO.end(snapshot)
            self._set_executing(False)
        output = "\n".join(outputs)
        self.append_text(f"(code ran {took:.1f}s)", "gray")
        self._show_files(progress.take_outputs())
        if output:
            self.append_html(f"<span style='color:gray'><b>{tr('Output:')}</b></span>")
            self.add_fold("output", output, color="#607d8b")
        if success:
            self.append_text(msg, "green")
        else:
            self.append_html(f"<span style='color:red'><b>{tr('Execution error:')}</b></span>")
            self.add_fold("error", tr(msg), color="red", tail=True)
        if summary:
            self.append_text(f"Undo AI can: {tr_undo(summary)}.", "gray")
        if not_undoable:
            self.append_text("Cannot be undone: " + tr_undo("; ".join(not_undoable[:4])), "orange")
        self._update_undo_btn()
        return success, msg, output

    # ------------------------------------------------------------------ undo, commands, tools
    def _update_undo_btn(self):
        can = UNDO.can_undo() and not self._busy()
        self.undo_btn.setEnabled(can)
        self.undo_btn.setToolTip(tr(f"Undo: {UNDO.last_label()[:80]}\nWill {tr_undo(UNDO.last_summary())}") if UNDO.can_undo()
                                 else tr("Undo the last AI action (code, command, scenario or join)"))

    def undo_last(self):
        if self._busy():
            return
        if not UNDO.can_undo():
            self.append_text("Nothing to undo.", "gray")
            return
        snapshot = UNDO.stack[-1]
        self.agent_task = None
        try:
            messages = UNDO.undo()
        except Exception as e:
            messages = [f"Undo failed: {type(e).__name__}: {e}"]
            log_warning(f"Undo failed: {e}")
        self.append_html(f"<br><b>{tr('Undo:')}</b> {html.escape(snapshot.label[:120])}")
        self.append_text("\n".join(messages), "#1565c0")
        entry = getattr(snapshot, "entry", None)
        if entry is not None:  # the AI must know that its changes are gone
            entry["result"] = (entry.get("result") or "") + "\n[The user pressed Undo: these changes were reverted.]"
            if entry.get("id"):
                self.archive.update(entry["id"], entry["result"], False)
        self.task_codes = []
        try:
            from qgis.utils import iface
            iface.mapCanvas().refresh()
        except (ImportError, AttributeError):
            pass
        self._update_undo_btn()

    def send_from_keyboard(self):
        if not self._busy():
            self.run_ai_process()

    def update_command_hint(self):
        text = self.prompt_input.toPlainText()
        if commands.is_command(text) and "\n" not in text.strip():
            hints = commands.completions(text.strip(), user=self.user_commands.items)
            self.command_hint.setText("\n".join(hints) if hints else tr("Unknown command — type /help"))
            self.command_hint.show()
        else:
            self.command_hint.hide()

    def run_command(self, text):
        """A quick command typed with "/": runs at once without the AI."""
        self.prompt_input.clear()
        self.append_html(f"<br><b>{tr('You:')}</b> <code>{html.escape(text)}</code>")
        name, rest = commands.split(text)
        mine = self.user_commands.get(name) if name not in commands.COMMANDS else None
        if mine is not None:
            try:
                code, check = user_commands.compile_command(mine, rest, self.user_commands)
            except commands.CommandError as e:
                self.append_text(str(e), "red")
                self.append_text(f"Usage: {mine['usage'] or '/' + mine['name']}", "gray")
                return
            self._execute([code], text, check=check)
            return
        try:
            code, local, rest = commands.build_call(text)
        except commands.CommandError as e:
            self.append_text(str(e), "red")
            return
        if local:
            self._local_command(local, rest)
            return
        self.append_html(f"<span style='color:gray'>{tr('Command (no AI):')}</span> <code>{html.escape(code)}</code>")
        self._execute([commands.HELPERS + "\n" + code], text, check=False)

    def _local_command(self, name, rest):
        if name == "help":
            self.add_fold("output", commands.help_text(user_commands.help_lines(self.user_commands)), open_=True)
        elif name == "commands":
            self.open_commands()
        elif name == "newcommand":
            self.new_command_with_ai(rest)
        elif name == "undo":
            self.undo_last()
        elif name == "fields":
            if rest:
                self._execute([f"field_info({rest.strip(chr(34) + chr(39))!r})"], f"/fields {rest}", check=False)
            else:
                self.open_fields()
        elif name == "join":
            try:
                names = shlex.split(rest)
            except ValueError:
                names = rest.split()
            if len(names) >= 2:
                self._execute([f"smart_join({names[0]!r}, {names[1]!r})"], f"/join {rest}", check=False)
            else:
                self.open_join(names[0] if names else None)
        elif name == "scenario":
            item = self.scenarios.get(rest.strip(chr(34) + chr(39))) if rest else None
            if item is None:
                self.append_text(f"No scenario named '{rest}'. Saved: {', '.join(self.scenarios.names()) or 'none'}.",
                                 "orange")
            else:
                self._use_scenario("run" if item["steps"] else "ai", item)
        elif name == "scenarios":
            self.open_scenarios()

    def open_join(self, table_name=None):
        if self._busy():
            return
        choice = smart_join_dialog(self, table_name)
        if choice is None:
            return
        if choice.get("load_file"):
            self.append_html("<br><b>Join:</b> add a table file")
            success, _msg, _out = self._execute(["load()"], "Add a table file", check=False)
            if success:
                QTimer.singleShot(100, self.open_join)
            return
        args = ", ".join(f"{k}={v!r}" for k, v in choice.items())
        self.append_html(f"<br><b>Join:</b> <code>{html.escape(f'smart_join({args})')}</code>")
        self._execute([f"smart_join({args})"], f"Join '{choice['table']}' to '{choice['layer']}'", check=False)

    def open_fields(self, layer_name=None):
        if self.field_window is not None:
            with contextlib.suppress(RuntimeError):  # the window was closed and deleted
                self.field_window.close()
        self.field_window = field_values_dialog(self.insert_into_prompt, self, layer_name)

    def insert_into_prompt(self, text):
        cursor = self.prompt_input.textCursor()
        before = self.prompt_input.toPlainText()[:cursor.position()]
        cursor.insertText(("" if not before or before.endswith((" ", "\n", "(")) else " ") + text + " ")
        self.prompt_input.setTextCursor(cursor)
        self.prompt_input.setFocus()

    def open_scenarios(self):
        if self._busy():
            return
        last = None
        if self.task_codes:
            last = (self.task_prompt, list(self.task_codes), self.provider_combo.currentText(),
                    self.model_combo.currentText().strip())
        choice = scenarios_dialog(self.scenarios, self, self.prompt_input.toPlainText(), last)
        if choice:
            self._use_scenario(*choice)

    def _use_scenario(self, action, item):
        if action == "command":
            slug = re.sub(r"[^a-z0-9_]+", "_", item["name"].lower()).strip("_")[:24]
            if not slug or not slug[0].isalpha():
                slug = "my_" + slug
            self.open_commands({"name": slug, "description": item["prompt"][:120], "kind": "python",
                                "body": "\n\n".join(item["steps"]), "usage": f"/{slug}"})
            return
        if action == "edit":
            self.prompt_input.setPlainText(item["prompt"])
            self.prompt_input.setFocus()
        elif action == "ai":
            self.prompt_input.setPlainText(item["prompt"])
            self.run_ai_process()
        elif action == "run":
            note = tr(f"saved code, {len(item['steps'])} step(s), no AI")
            self.append_html(f"<br><b>{tr('Scenario:')}</b> {html.escape(item['name'])} "
                             f"<span style='color:gray'>({html.escape(note)})</span>")
            if not self.auto_run_cb.isChecked():
                answer = QMessageBox.question(
                    self, tr("Run scenario?"), tr(f"Run the saved code of '{item['name']}' now?\n\n"
                                                  "It runs inside QGIS like AI code. You can undo it afterwards "
                                                  "with «Undo AI»."))
                if answer != QMessageBox.StandardButton.Yes:
                    self.append_text("Cancelled.", "gray")
                    return
            self._execute(item["steps"], f"Scenario '{item['name']}'")

    # ------------------------------------------------------------------ the user's own commands
    def open_commands(self, draft=None):
        if self._busy():
            return
        choice = commands_dialog(self.user_commands, self, draft)
        if choice and choice[0] == "ai":
            self.prompt_input.setPlainText("/newcommand ")
            self.prompt_input.setFocus()
            cursor = self.prompt_input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.prompt_input.setTextCursor(cursor)
            self.append_text("Write: /newcommand <name> <what it should do>, e.g. "
                             "/newcommand relief hillshade of a DEM layer, 50 % transparent", "gray")

    def new_command_with_ai(self, rest):
        """/newcommand name description: the AI writes the command; the user checks and saves it."""
        name, _sep, description = rest.strip().partition(" ")
        name = name.strip().lstrip("/").lower()
        if not name or not description.strip():
            self.append_text("Usage: /newcommand <name> <what it does>", "red")
            return
        problem = user_commands.validate(user_commands._clean({"name": name, "body": "pass", "kind": "python"}),
                                         None)
        if problem:
            self.append_text(problem, "red")
            return
        self.command_draft = {"name": name, "description": description.strip()}
        self.run_ai_process(user_commands.ai_prompt(name, description.strip()),
                            shown_prompt=f"/newcommand {name} {description.strip()}")

    def _finish_command_draft(self, answer):
        from ..core.executor import extract_code
        draft, self.command_draft = self.command_draft, None
        self.agent_task = None
        code, _warning = extract_code(answer)
        if not code.strip():
            self.append_text("The AI did not write a command. Describe it in other words.", "orange")
            return
        draft.update(kind="python", body=code, usage=user_commands.usage_from_answer(answer, draft["name"]))
        self.append_text("Command written. Check it in the window and press Save.", "#2e7d32", bold=True)
        self.open_commands(draft)

    def on_background_toggled(self, checked):
        progress.set_background(checked)
        self.settings.setValue(f"{S}/background_processing", checked)

    # ------------------------------------------------------------------ layers for the AI
    def _layer_signals(self):
        """(signal, slot) pairs that keep the «Layers for AI» hint up to date."""
        project = QgsProject.instance()
        pairs = [(project.layersAdded, self.update_scope_label),
                 (project.layersRemoved, self.update_scope_label),
                 (project.layerTreeRoot().visibilityChanged, self.update_scope_label),
                 (project.readProject, self.update_scope_label)]
        try:
            import qgis.utils
            view = qgis.utils.iface.layerTreeView() if qgis.utils.iface is not None else None
        except (ImportError, AttributeError):
            view = None
        if view is not None:
            pairs.append((view.currentLayerChanged, self.update_scope_label))
            selection = view.selectionModel()  # may be None before the layer tree model is set
            if selection is not None:
                pairs.append((selection.selectionChanged, self.update_scope_label))
        return pairs

    def _connect_layer_signals(self):
        for signal, slot in self._layer_signals():
            signal.connect(slot)

    def _disconnect_layer_signals(self):
        for signal, slot in self._layer_signals():
            with contextlib.suppress(TypeError, RuntimeError):  # already disconnected
                signal.disconnect(slot)

    def _chosen_ids(self):
        value, _ok = QgsProject.instance().readEntry("ai_copilot", "chosen_layers", "")
        return [i for i in str(value).split(";") if i]

    def _save_chosen_ids(self, ids):
        QgsProject.instance().writeEntry("ai_copilot", "chosen_layers", ";".join(ids))

    def current_scope(self):
        return self.scope_combo.currentData() or "all"

    def on_scope_changed(self, _index):
        self.settings.setValue(f"{S}/layer_scope", self.current_scope())
        if self.current_scope() == SCOPE_CHOSEN and not self._chosen_ids():
            self.choose_layers()
        self.update_scope_label()

    def choose_layers(self, *_args):
        layers = [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.isValid()]
        if not layers:
            self.append_text("The project has no layers yet.", "orange")
            return
        chosen = pick_layers(layers, self._chosen_ids(), self)
        if chosen is None:
            return
        self._save_chosen_ids(chosen)
        if self.current_scope() != SCOPE_CHOSEN:
            self.scope_combo.setCurrentIndex(self.scope_combo.findData(SCOPE_CHOSEN))
        self.update_scope_label()

    def working_layers(self):
        layers, note = resolve_scope(self.current_scope(), self._chosen_ids())
        return layers, note

    def update_scope_label(self, *_args):
        try:
            _layers, note = self.working_layers()
        except RuntimeError as e:  # the project is being closed
            note = str(e)
        self.scope_label.setText(tr(note))

    def _save_splitter(self, *_args):
        self.settings.setValue(f"{S}/splitter", self.splitter.saveState())

    def set_link_expanded(self, expanded):
        """Shows or hides the data link field (hidden = more room for the request box)."""
        self.link_toggle.blockSignals(True)
        self.link_toggle.setChecked(bool(expanded))
        self.link_toggle.blockSignals(False)
        self.link_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.link_box.setVisible(bool(expanded))
        self.settings.setValue(f"{S}/link_expanded", bool(expanded))
        self.update_link_summary()

    def update_link_summary(self):
        url = self.link_input.text().strip()
        if url:
            host = QUrl.fromUserInput(url).host() or url[:40]
            self.link_summary.setText(tr(f"attached: {host}"))
        else:
            self.link_summary.setText("" if self.link_toggle.isChecked() else tr("none — click to add a link"))

    def open_link(self):
        url = self.link_input.text().strip()
        if url:
            QDesktopServices.openUrl(QUrl.fromUserInput(url))

    def _user_links(self, prompt):
        """Links given by the user: the «Data link» field and addresses in the request text."""
        links = []
        field = self.link_input.text().strip()
        if field:
            links.append(field if "://" in field else "https://" + field)
        for url in re.findall(r"https?://[^\s<>\"']+", prompt):
            url = url.rstrip(".,;:!?)")
            if url not in links:
                links.append(url)
        return links

    def _show_files(self, files):
        """Links to created files: open the file or the folder."""
        rows = []
        for path in files:
            if not os.path.exists(path):
                continue
            file_url = QUrl.fromLocalFile(path).toString()
            folder_url = QUrl.fromLocalFile(os.path.dirname(path)).toString()
            rows.append(f"&nbsp;&nbsp;<a href='{html.escape(file_url)}'>{html.escape(os.path.basename(path))}</a>"
                        f" &nbsp;·&nbsp; <a href='{html.escape(folder_url)}'>{tr('open folder')}</a>")
        if rows:
            self.append_html(f"<b style='color:#2e7d32'>{tr('Created files:')}</b><br>" + "<br>".join(rows))

    def _set_executing(self, on):
        self.executing = on
        for w in (self.fix_btn, self.clear_mem_btn, self.history_btn, self.fetch_models_btn, self.provider_combo,
                  self.prompt_input, self.undo_btn, self.scenarios_btn,
                  self.link_input, self.scope_combo, self.choose_layers_btn):
            w.setEnabled(not on)
        if on:
            self.run_button.setText(tr("Stop"))
            self.run_button.setEnabled(True)
            self.status.start("running", "Running code…",
                              "QGIS may respond slowly while the code works. Press Stop to cancel.")
            progress.set_status_handler(self._on_code_status)
            QApplication.processEvents()
        else:
            progress.set_status_handler(None)
            self.status.stop()
            self.run_button.setText(tr("Run with AI"))
            self.run_button.setEnabled(True)
            self.fix_btn.setEnabled(self.last_failed is not None)
            self._update_undo_btn()

    def _on_code_status(self, stage, detail, percent):
        """Called from running code (main thread): update the panel and let the window repaint."""
        kind = "download" if ("download" in stage.lower() or "search" in stage.lower()) else "running"
        if not progress.is_cancel_requested():
            self.status.set_stage(kind, stage, detail or None)
            self.status.set_progress(percent if percent else None)
        now = self.status.elapsed()
        if now - self._last_ui_refresh >= UI_REFRESH_SEC:
            self._last_ui_refresh = now
            self.status.refresh()
            QApplication.processEvents()

    # ------------------------------------------------------------------ settings fold and language
    def set_settings_expanded(self, expanded):
        self.settings_toggle.blockSignals(True)
        self.settings_toggle.setChecked(bool(expanded))
        self.settings_toggle.blockSignals(False)
        self.settings_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.settings_box.setVisible(bool(expanded))
        self.settings.setValue(f"{S}/settings_expanded", bool(expanded))
        self.update_settings_summary()

    def update_settings_summary(self, *_args):
        if not hasattr(self, "settings_summary"):
            return
        provider = self.provider_combo.currentText()
        model = self.model_combo.currentText().strip()
        need_key = not self.is_local(provider) and not self.current_key()
        text = f"{provider} · {model}" if model else provider
        if need_key:
            text += " · " + ("нет ключа API" if tr("Settings") != "Settings" else "no API key")
        self.settings_summary.setText("" if self.settings_toggle.isChecked() else text)
        self.settings_summary.setStyleSheet(f"color: {'#c62828' if need_key else 'gray'}; font-size: 11px;")

    def on_language_changed(self, _index):
        value = self.lang_combo.currentData() or "auto"
        self.settings.setValue(f"{S}/language", value)
        set_language(value)
        self.retranslate()

    def retranslate(self):
        """Puts every text of both panels into the chosen language (also while the panel is open)."""
        translate_tree(self)
        translate_tree(self.request_dock)
        if not self._busy():
            self.run_button.setText(tr("Run with AI"))
        self.fetch_models_btn.setText(tr("Refresh"))
        local = self.is_local()
        self.api_key_input.setPlaceholderText(tr("Not needed for local AI") if local else tr("Paste API key here..."))
        self.update_scope_label()
        self.update_link_summary()
        self.update_settings_summary()
        self._update_undo_btn()
        if self.chat_items:
            self._rerender_chat(keep_position=True)

    # ------------------------------------------------------------------ placement
    def saved_area(self):
        """Dock area of the settings panel chosen by the user last time (right by default)."""
        return DOCK_AREAS.get(str(self.settings.value(f"{S}/dock_area", "right")), DOCK_AREAS["right"])

    def restore_floating(self):
        if _as_bool(self.settings.value(f"{S}/dock_floating", False)):
            self.setFloating(True)

    def _save_place(self, *_args):
        main = self.parent()
        if main is None or not hasattr(main, "dockWidgetArea"):
            return
        area = main.dockWidgetArea(self)
        name = next((k for k, v in DOCK_AREAS.items() if v == area), None)
        if name:
            self.settings.setValue(f"{S}/dock_area", name)
        self.settings.setValue(f"{S}/dock_floating", self.isFloating())

    def set_panels_visible(self, visible):
        for dock in (self, self.request_dock):
            dock.setVisible(visible)
            if visible:
                dock.raise_()

    def panels_visible(self):
        return self.isVisible() or self.request_dock.isVisible()

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        """Stops threads cleanly (called when the plugin is unloaded)."""
        self._disconnect_layer_signals()
        self.status.stop()
        progress.set_status_handler(None)
        if self.executing:
            progress.request_cancel()
        for w in (self.worker, self.models_worker):
            if w is not None and w.isRunning():
                if hasattr(w, "cancel"):
                    w.cancel()
                w.wait(3000)

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)
