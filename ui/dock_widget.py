import contextlib
import html
import os
import re

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import QThread, QTimer, QUrl, Qt, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QFontDatabase, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPushButton, QSplitter, QTextBrowser, QTextEdit,
                                 QToolButton, QVBoxLayout, QWidget)

# Relative imports: the plugin works with any folder name
# (it used to be `qgis_ai_copilot.core` while the folder was `qgis-ai-copilot-main` → ImportError)
from ..core import ai_client
from ..core.context import SCOPE_CHOSEN, SCOPES, get_project_context, resolve_scope
from ..core import osm, progress, web_data
from ..core.history import RequestArchive
from ..core.log import log_warning
from .dialogs import pick_layers, show_history
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


def _needs_user(error):
    """Errors that code cannot fix — the agent must not auto-retry them."""
    return any(s in error for s in ("PermissionError", "Stopped by user", "Export cancelled",
                                    "User denied", "declined", "No data link"))


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


class AICopilotDockWidget(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("AI Copilot", parent)
        self.setObjectName("AICopilotDock")
        self.settings = QgsSettings()

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

        self.code_format = QTextCharFormat()
        self.code_format.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        self.init_ui()

    # ------------------------------------------------------------------ UI
    def init_ui(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        layout.addWidget(QLabel("AI Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(ai_client.PROVIDERS)
        layout.addWidget(self.provider_combo)

        layout.addWidget(QLabel("Model:"))
        model_layout = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setToolTip("Pick from the list or type the model name manually")
        model_layout.addWidget(self.model_combo, 1)
        self.fetch_models_btn = QPushButton("Refresh")
        self.fetch_models_btn.setFixedWidth(70)
        self.fetch_models_btn.setToolTip("Load models (also checks the API key)")
        self.fetch_models_btn.clicked.connect(self.fetch_available_models)
        model_layout.addWidget(self.fetch_models_btn)
        layout.addLayout(model_layout)

        layout.addWidget(QLabel("API Key:"))
        key_layout = QHBoxLayout()
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.editingFinished.connect(self.on_api_key_finished)
        key_layout.addWidget(self.api_key_input, 1)
        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setFixedWidth(55)
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setToolTip("Show / hide key")
        self.show_key_btn.toggled.connect(lambda on: self.api_key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_layout.addWidget(self.show_key_btn)
        layout.addLayout(key_layout)

        self.plugins_cb = QCheckBox("Use installed plugins (send plugin list to the AI)")
        self.plugins_cb.setToolTip("Lets the AI use Processing algorithms, menus and objects of your other plugins")
        self.plugins_cb.setChecked(_as_bool(self.settings.value(f"{S}/use_plugins", True), True))
        self.plugins_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/use_plugins", v))
        layout.addWidget(self.plugins_cb)

        # Chat (top) and the controls with the request box (bottom) share a splitter: drag it to resize
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(QLabel("Chat / Output:"))
        self.log_display = QTextBrowser()
        # Links (websites and created files) are opened by us: otherwise file:// would load inside the chat
        self.log_display.setOpenLinks(False)
        self.log_display.anchorClicked.connect(QDesktopServices.openUrl)
        top_layout.addWidget(self.log_display, 1)
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        self.fast_cb = QCheckBox("Fast answers (less model thinking)")
        self.fast_cb.setToolTip("Limits the internal 'thinking' of models that support it (Gemini 2.5 / 3, OpenAI o3, "
                                "o4, gpt-5). Much faster answers; switch off for very complex tasks.")
        self.fast_cb.setChecked(_as_bool(self.settings.value(f"{S}/fast_answers", True), True))
        self.fast_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/fast_answers", v))
        bottom_layout.addWidget(self.fast_cb)

        self.agent_cb = QCheckBox("Agent mode: multi-step tasks and automatic error fixing")
        self.agent_cb.setToolTip(
            "The AI may first inspect algorithms/data, then continue, and automatically asks for a fix when code "
            f"fails (max {MAX_AGENT_STEPS} steps). With Auto-run off you still confirm every step with Run Code.")
        self.agent_cb.setChecked(_as_bool(self.settings.value(f"{S}/agent_mode", True), True))
        self.agent_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/agent_mode", v))
        bottom_layout.addWidget(self.agent_cb)

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

        self._request_label_index = bottom_layout.count()

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Layers for AI:"))
        self.scope_combo = QComboBox()
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
        self.scope_label = QLabel()
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
        self.link_summary = QLabel()
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
        self.link_open_btn.setFixedWidth(60)
        self.link_open_btn.setToolTip("Open the link in your browser")
        self.link_open_btn.clicked.connect(self.open_link)
        link_layout.addWidget(self.link_open_btn)
        link_box_layout.addLayout(link_layout)
        bottom_layout.addWidget(self.link_box)
        self.link_input.textChanged.connect(lambda _t: self.update_link_summary())
        self.link_toggle.toggled.connect(self.set_link_expanded)

        request_row = QHBoxLayout()
        request_row.addWidget(QLabel("Your Request:"))
        request_row.addStretch(1)
        self.history_btn = QPushButton("History")
        self.history_btn.setToolTip("Archive of your requests: search, view and send again")
        self.history_btn.clicked.connect(self.open_history)
        request_row.addWidget(self.history_btn)
        bottom_layout.addLayout(request_row)
        self.prompt_input = QTextEdit()
        self.prompt_input.setMinimumHeight(70)
        self.prompt_input.setPlaceholderText("e.g. Create a 250 m buffer around selected features")
        bottom_layout.addWidget(self.prompt_input, 1)  # the request box takes all free space of the bottom pane

        self.status = StatusPanel()
        bottom_layout.insertWidget(self._request_label_index, self.status)  # above the request box, next to the chat

        self.splitter.addWidget(top)
        self.splitter.addWidget(bottom)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        saved = self.settings.value(f"{S}/splitter")
        if saved:
            self.splitter.restoreState(saved)
        self.splitter.splitterMoved.connect(self._save_splitter)
        layout.addWidget(self.splitter, 1)
        self.set_link_expanded(_as_bool(self.settings.value(f"{S}/link_expanded", False)))

        self.run_button = QPushButton("Run with AI")
        self.run_button.clicked.connect(self.on_run_clicked)
        layout.addWidget(self.run_button)

        self.setWidget(widget)

        last_provider = self.settings.value(f"{S}/last_provider", "Google Gemini")
        idx = self.provider_combo.findText(str(last_provider))
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)
        self.on_provider_changed(self.provider_combo.currentText())
        self._connect_layer_signals()
        self.update_scope_label()
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
        self.log_display.append(html_text)
        self._scroll_down()

    def append_text(self, text, color=None, bold=False):
        safe = html.escape(text).replace("\n", "<br>")
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
        self.api_key_input.setPlaceholderText("Not needed for local AI" if local else "Paste API key here...")

        self.model_combo.clear()
        last_model = str(self.settings.value(f"{S}/{provider}_last_model", "") or "")
        if last_model:
            self.model_combo.addItem(last_model)
            self.model_combo.setCurrentText(last_model)

        if local or saved_key:
            self.fetch_available_models()

    def on_api_key_finished(self):
        provider = self.provider_combo.currentText()
        key = self.current_key()
        if key != self.api_key_input.text():
            self.api_key_input.setText(key)  # show the cleaned key
        saved = str(self.settings.value(f"{S}/{provider}_api_key", "") or "")
        if key and key != saved:
            self.save_settings()
            self.fetch_available_models()

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
        self.fetch_models_btn.setText("Refresh")
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
                self, "Enable auto-run?",
                "AI-generated Python code will be executed immediately, without review.\n\n"
                "This code runs inside QGIS with full access to your project and files "
                "and can modify or delete data.\n\nEnable auto-run anyway?",
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
        self.append_text("Memory cleared.", "gray")

    def on_run_clicked(self):
        if self.executing:
            progress.request_cancel()
            self.agent_task = None
            self.run_button.setEnabled(False)
            self.run_button.setText("Stopping…")
            self.status.set_stage("stopping", "Stopping the running code…",
                                  "Waiting for the current step to react to the cancel request")
            return
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.agent_task = None  # Stop also interrupts the agent's chain of steps
            self.run_button.setEnabled(False)
            self.run_button.setText("Stopping…")
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
        web_data.set_user_links(self.agent_links)
        self.last_prompt = shown_prompt or prompt
        self.pending_code = ""
        self.pending_entry = None
        self.run_manual_btn.setEnabled(False)
        self.fix_btn.setEnabled(False)
        self.append_html(f"<br><b>You:</b> {html.escape(self.last_prompt).replace(chr(10), '<br>')}")
        self.append_html("<b>AI:</b>")
        self.log_display.append("")  # separate paragraph for the streamed answer
        if prompt_override is None:
            self.prompt_input.clear()

        step_info = f"Agent step {self.agent_steps + 1}/{MAX_AGENT_STEPS}" if agent_continue and self.agent_task else ""
        self.status.start("agent" if step_info else "context", "Collecting project context…",
                          step_info or "Layers, CRS, map extent" + (", plugins" if self.plugins_cb.isChecked() else ""))
        self.run_button.setText("Stop")
        QApplication.processEvents()

        try:
            working, scope_note = self.working_layers()
            self.scope_label.setText(scope_note)
            self.status.set_detail(scope_note)
            project_ctx = get_project_context(working)
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

    def on_ai_chunk(self, chunk):
        self.chars_received += len(chunk)
        self.received_text += chunk
        in_code = self.received_text.count("```") % 2 == 1
        seconds = max(self.status.elapsed(), 0.1)
        last_line = self.received_text.rstrip().splitlines()[-1][:70] if self.received_text.strip() else ""
        self.status.set_stage("writing", "Writing code…" if in_code else "AI is answering…",
                              f"{self.chars_received} characters · {self.chars_received / seconds:.0f}/s"
                              + (f"\n{last_line}" if last_line else ""))
        cursor = self.log_display.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk, self.code_format)
        self.log_display.setTextCursor(cursor)
        self._scroll_down()

    def on_ai_finished(self, ai_response, error_msg):
        took = self.status.elapsed()
        self.status.stop()
        self.run_button.setEnabled(True)
        self.run_button.setText("Run with AI")
        self.append_text(f"(took {took:.1f}s)", "gray")

        provider, model = self.provider_combo.currentText(), self.model_combo.currentText().strip()
        if error_msg:
            self.append_text(f"Error: {error_msg}", "red", bold=True)
            self.agent_task = None
            self.archive.add(self.last_prompt, ai_response, provider, model, in_memory=False,
                             result=f"Error: {error_msg}")
            return  # a cut-off/failed answer is never executed
        if not ai_response.strip():
            self.append_text("The AI returned an empty answer.", "orange")
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
            self.append_text("Code is ready — press «Run Code» to execute it "
                             "(or enable Auto-run to run code automatically).", "#2e7d32", bold=True)

    def execute_pending_code(self):
        if not self.pending_code:
            return
        code = self.pending_code
        self.run_manual_btn.setEnabled(False)
        self.run_manual_btn.setStyleSheet("")
        self._set_executing(True)
        try:
            from ..core import executor
            success, msg, output = executor.run_pyqgis_code(code)
        except Exception as e:
            success, msg, output = False, f"{type(e).__name__}: {e}", ""
        finally:
            took = self.status.elapsed()
            self._set_executing(False)
        self.append_text(f"(code ran {took:.1f}s)", "gray")
        if not success and _needs_user(msg):
            self.agent_task = None  # code cannot fix this — stop instead of looping
            self.append_text("Agent stopped: this needs your action (permission, a data link or a cancelled "
                             "dialog). Nothing was retried.", "orange")
        self._show_files(progress.take_outputs())
        if msg == "Stopped by user.":
            self.agent_task = None

        if output:
            self.append_html("<span style='color:gray'><b>Output:</b></span>"
                             f"<pre style='color:#555'>{html.escape(output)}</pre>")
        if success:
            self.append_text(msg, "green")
            self.last_failed = None
            self.fix_btn.setEnabled(False)
            result = f"Success.\n{output}" if output else "Success (no printed output)."
        else:
            self.append_html(
                "<span style='color:red'><b>Execution error:</b></span>"
                f"<pre style='color:red'>{html.escape(msg)}</pre>"
            )
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
        self.scope_label.setText(note)

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
            self.link_summary.setText(f"attached: {host}")
        else:
            self.link_summary.setText("" if self.link_toggle.isChecked() else "none — click to add a link")

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
                        f" &nbsp;·&nbsp; <a href='{html.escape(folder_url)}'>open folder</a>")
        if rows:
            self.append_html("<b style='color:#2e7d32'>Created files:</b><br>" + "<br>".join(rows))

    def _set_executing(self, on):
        self.executing = on
        for w in (self.fix_btn, self.clear_mem_btn, self.history_btn, self.fetch_models_btn, self.provider_combo,
                  self.prompt_input,
                  self.link_input, self.scope_combo, self.choose_layers_btn):
            w.setEnabled(not on)
        if on:
            self.run_button.setText("Stop")
            self.run_button.setEnabled(True)
            self.status.start("running", "Running code…",
                              "QGIS may respond slowly while the code works. Press Stop to cancel.")
            progress.set_status_handler(self._on_code_status)
            QApplication.processEvents()
        else:
            progress.set_status_handler(None)
            self.status.stop()
            self.run_button.setText("Run with AI")
            self.run_button.setEnabled(True)

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
