import html
from urllib.parse import quote

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QThread, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QFontDatabase, QTextCharFormat, QTextCursor
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QHBoxLayout, QLabel, QLineEdit,
                                 QProgressBar, QPushButton, QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

# Относительные импорты: плагин работает при любом имени папки
# (раньше было `qgis_ai_copilot.core`, а папка называлась `qgis-ai-copilot-main` → ImportError)
from ..core import ai_client
from ..core.context import get_project_context

S = "ai_copilot"
MAX_HISTORY = 4
MAX_HISTORY_CHARS = 3000


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def qgis_proxy_url():
    """Прокси из Настройки → Параметры → Сеть QGIS (если включён)."""
    s = QgsSettings()
    if not _as_bool(s.value("proxy/proxyEnabled", False)):
        return None
    host = str(s.value("proxy/proxyHost", "") or "").strip()
    if not host:
        return None
    port = str(s.value("proxy/proxyPort", "") or "").strip()
    user = str(s.value("proxy/proxyUser", "") or "")
    pwd = str(s.value("proxy/proxyPassword", "") or "")
    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""
    host = host.split("://", 1)[-1]
    return f"http://{auth}{host}:{port}" if port else f"http://{auth}{host}"


class StreamWorker(QThread):
    chunk_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(str, str)  # (полный ответ, ошибка)

    def __init__(self, provider, model, api_key, prompt, context, net):
        super().__init__()
        self.args = (provider, model, api_key, prompt, context)
        self.net = net
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        parts = []
        try:
            for piece in ai_client.stream_completion(*self.args, net=self.net, cancel=lambda: self._cancel):
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
    result_signal = pyqtSignal(str, list, str)  # (провайдер, модели, ошибка)

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
        self.chat_history = []
        self.pending_code = ""
        self.last_prompt = ""
        self.elapsed_time = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_timer)

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
        self.fetch_models_btn = QPushButton("🗘")
        self.fetch_models_btn.setFixedWidth(40)
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
        self.show_key_btn = QPushButton("👁")
        self.show_key_btn.setFixedWidth(40)
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setToolTip("Show / hide key")
        self.show_key_btn.toggled.connect(lambda on: self.api_key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_layout.addWidget(self.show_key_btn)
        layout.addLayout(key_layout)

        self.ssl_cb = QCheckBox("Skip SSL check (only if you get certificate errors)")
        self.ssl_cb.setChecked(_as_bool(self.settings.value(f"{S}/skip_ssl", False)))
        self.ssl_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/skip_ssl", v))
        layout.addWidget(self.ssl_cb)

        layout.addWidget(QLabel("Chat / Output:"))
        self.log_display = QTextBrowser()
        self.log_display.setOpenExternalLinks(True)
        layout.addWidget(self.log_display, 1)

        options = QHBoxLayout()
        self.auto_run_cb = QCheckBox("Auto-run code")
        self.auto_run_cb.setToolTip("Execute AI code immediately. Off = review first, then press ▶ Run Code")
        self.auto_run_cb.setChecked(_as_bool(self.settings.value(f"{S}/auto_run", False)))
        self.auto_run_cb.toggled.connect(lambda v: self.settings.setValue(f"{S}/auto_run", v))
        options.addWidget(self.auto_run_cb)
        self.run_manual_btn = QPushButton("▶ Run Code")
        self.run_manual_btn.setEnabled(False)
        self.run_manual_btn.clicked.connect(self.execute_pending_code)
        options.addWidget(self.run_manual_btn)
        self.clear_mem_btn = QPushButton("🗑 Clear")
        self.clear_mem_btn.setToolTip("Clear chat and AI memory")
        self.clear_mem_btn.clicked.connect(self.clear_memory)
        options.addWidget(self.clear_mem_btn)
        layout.addLayout(options)

        layout.addWidget(QLabel("Your Request:"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setMaximumHeight(80)
        self.prompt_input.setPlaceholderText("e.g. Create a 250 m buffer around selected features")
        layout.addWidget(self.prompt_input)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

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

    # ------------------------------------------------------------------ helpers
    def net(self):
        return ai_client.Net(verify_ssl=not self.ssl_cb.isChecked(), proxy_url=qgis_proxy_url())

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
            self.api_key_input.setText(key)  # показываем очищенный ключ
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
        self.models_worker = ModelsWorker(provider, key, self.net())
        self.models_worker.result_signal.connect(self.on_models_loaded)
        self.models_worker.start()

    def on_models_loaded(self, provider, models, error):
        self.fetch_models_btn.setEnabled(True)
        self.fetch_models_btn.setText("🗘")
        if provider != self.provider_combo.currentText():
            return  # пользователь уже переключил провайдера
        if error:
            self.append_text(f"✗ {error}", "red")
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
        self.append_text(f"✓ {provider}: connected, {len(models)} models loaded.", "green")

    # ------------------------------------------------------------------ chat
    def update_timer(self):
        self.elapsed_time += 0.1
        if self.worker is not None and self.worker.isRunning():
            self.run_button.setText(f"■ Stop  ({self.elapsed_time:.1f}s)")

    def clear_memory(self):
        self.chat_history.clear()
        self.pending_code = ""
        self.run_manual_btn.setEnabled(False)
        self.log_display.clear()
        self.append_text("🗑 Memory cleared.", "gray")

    def on_run_clicked(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.run_button.setEnabled(False)
            self.run_button.setText("Stopping…")
            return
        self.run_ai_process()

    def run_ai_process(self):
        provider = self.provider_combo.currentText()
        model = self.model_combo.currentText().strip()
        key = self.current_key()
        prompt = self.prompt_input.toPlainText().strip()

        if not prompt:
            return
        if not self.is_local(provider) and not key:
            self.append_text(f"Enter the API key for {provider}.", "red")
            return
        if not model:
            self.append_text("Choose a model (click 🗘) or type its name.", "red")
            return

        self.save_settings()
        self.last_prompt = prompt
        self.pending_code = ""
        self.run_manual_btn.setEnabled(False)
        self.append_html(f"<br><b>You:</b> {html.escape(prompt).replace(chr(10), '<br>')}")
        self.append_html("<b>AI:</b>")
        self.log_display.append("")  # отдельный абзац для потока ответа
        self.prompt_input.clear()

        try:
            project_ctx = get_project_context()
        except Exception as e:
            project_ctx = f"(project context unavailable: {e})"
        history = "".join(
            f"User: {h['prompt']}\nAI answer:\n{h['answer'][:MAX_HISTORY_CHARS]}\n\n" for h in self.chat_history
        )
        context = f"PROJECT CONTEXT:\n{project_ctx}"
        if history:
            context += f"\n\nPREVIOUS CONVERSATION:\n{history}"

        self.progress_bar.show()
        self.elapsed_time = 0.0
        self.timer.start(100)
        self.run_button.setText("■ Stop")

        self.worker = StreamWorker(provider, model, key, prompt, context, self.net())
        self.worker.chunk_signal.connect(self.on_ai_chunk)
        self.worker.finished_signal.connect(self.on_ai_finished)
        self.worker.start()

    def on_ai_chunk(self, chunk):
        cursor = self.log_display.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk, self.code_format)
        self.log_display.setTextCursor(cursor)
        self._scroll_down()

    def on_ai_finished(self, ai_response, error_msg):
        self.timer.stop()
        self.progress_bar.hide()
        self.run_button.setEnabled(True)
        self.run_button.setText("Run with AI")
        self.append_text(f"(took {self.elapsed_time:.1f}s)", "gray")

        if error_msg:
            self.append_text(f"✗ {error_msg}", "red", bold=True)
            return  # оборванный/ошибочный ответ никогда не выполняем
        if not ai_response.strip():
            self.append_text("The AI returned an empty answer.", "orange")
            return

        self.chat_history.append({"prompt": self.last_prompt, "answer": ai_response})
        del self.chat_history[:-MAX_HISTORY]

        try:
            from ..core import executor
            code, warning = executor.extract_code(ai_response)
        except Exception as e:
            self.append_text(f"✗ Could not load the executor: {e}", "red")
            return

        if warning:
            self.append_text(f"⚠ {warning}", "orange")
        if not code or executor.is_comment_only(code):
            return  # обычный текстовый ответ — выполнять нечего

        self.pending_code = code
        if self.auto_run_cb.isChecked():
            self.execute_pending_code()
        else:
            self.run_manual_btn.setEnabled(True)
            self.append_text("Code is ready. Review it and press ▶ Run Code.", "#1e6fd9")

    def execute_pending_code(self):
        if not self.pending_code:
            return
        self.run_manual_btn.setEnabled(False)
        try:
            from ..core import executor
            success, msg = executor.run_pyqgis_code(self.pending_code)
        except Exception as e:
            success, msg = False, f"{type(e).__name__}: {e}"
        if success:
            self.append_text(f"✓ {msg}", "green")
        else:
            self.append_html(
                "<span style='color:red'><b>✗ Execution error:</b></span>"
                f"<pre style='color:red'>{html.escape(msg)}</pre>"
            )
        self.pending_code = ""

    # ------------------------------------------------------------------ shutdown
    def shutdown(self):
        """Корректно останавливает потоки (вызывается при выгрузке плагина)."""
        self.timer.stop()
        for w in (self.worker, self.models_worker):
            if w is not None and w.isRunning():
                if hasattr(w, "cancel"):
                    w.cancel()
                w.wait(3000)

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)
