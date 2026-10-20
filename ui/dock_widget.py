import json
import urllib.request
import urllib.error
from qgis.PyQt.QtWidgets import (QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, 
                                 QComboBox, QLineEdit, QTextEdit, QPushButton, QLabel, 
                                 QTextBrowser, QApplication, QProgressBar)
from qgis.PyQt.QtCore import QSettings, QTimer, QThread, pyqtSignal
from qgis.core import QgsSettings, QgsProject

PROVIDERS = ["Ollama (Local)", "OpenAI", "DeepSeek", "Google Gemini", "Claude (Anthropic)"]

# Фоновый рабочий процесс (чтобы QGIS не зависал)
class AIWorker(QThread):
    finished_signal = pyqtSignal(str, str) # Отправляет (результат, текст_ошибки)

    def __init__(self, provider, model, api_key, prompt, context):
        super().__init__()
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.prompt = prompt
        self.context = context

    def run(self):
        from qgis_ai_copilot.core import ai_client
        try:
            ai_response = ai_client.request_ai_code(self.provider, self.model, self.api_key, self.prompt, self.context)
            self.finished_signal.emit(ai_response, "")
        except Exception as e:
            self.finished_signal.emit("", str(e))


class AICopilotDockWidget(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("AI Copilot", parent)
        self.settings = QgsSettings()
        
        # Таймер для отсчета времени
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_timer)
        self.elapsed_time = 0.0
        
        self.init_ui()

    def init_ui(self):
        widget = QWidget()
        layout = QVBoxLayout()

        layout.addWidget(QLabel("AI Provider:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(PROVIDERS)
        self.provider_combo.currentTextChanged.connect(self.update_models)
        layout.addWidget(self.provider_combo)

        layout.addWidget(QLabel("Model Name (Auto-fetched):"))
        model_layout = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        model_layout.addWidget(self.model_combo)

        self.fetch_models_btn = QPushButton("🗘")
        self.fetch_models_btn.setToolTip("Refresh list manually")
        self.fetch_models_btn.setFixedWidth(40)
        self.fetch_models_btn.clicked.connect(self.fetch_available_models)
        model_layout.addWidget(self.fetch_models_btn)
        
        layout.addLayout(model_layout)

        layout.addWidget(QLabel("API Key (leave empty for Ollama):"))
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.editingFinished.connect(self.on_api_key_finished)
        layout.addWidget(self.api_key_input)

        layout.addWidget(QLabel("Chat / Output:"))
        self.log_display = QTextBrowser()
        self.log_display.setOpenExternalLinks(True)
        layout.addWidget(self.log_display)

        layout.addWidget(QLabel("Your Request:"))
        self.prompt_input = QTextEdit()
        layout.addWidget(self.prompt_input)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.run_button = QPushButton("Run with AI")
        self.run_button.clicked.connect(self.run_ai_process)
        layout.addWidget(self.run_button)

        widget.setLayout(layout)
        self.setWidget(widget)

        self.load_settings()
        self.update_models(self.provider_combo.currentText())

    def update_timer(self):
        self.elapsed_time += 0.1
        self.run_button.setText(f"🗘 AI is thinking... {self.elapsed_time:.1f}s")

    def on_api_key_finished(self):
        provider = self.provider_combo.currentText()
        saved_key = self.settings.value(f"ai_copilot/{provider}_api_key", "")
        current_key = self.api_key_input.text().strip()
        
        if current_key and current_key != saved_key:
            self.save_settings()
            self.fetch_available_models()

    def update_models(self, provider):
        self.model_combo.clear()
        saved_key = self.settings.value(f"ai_copilot/{provider}_api_key", "")
        
        self.api_key_input.blockSignals(True)
        self.api_key_input.setText(saved_key)
        self.api_key_input.blockSignals(False)
        
        if provider == "Ollama (Local)":
            self.api_key_input.setPlaceholderText("API Key not needed for local AI")
            self.api_key_input.setEnabled(False)
            self.fetch_available_models()
        elif provider == "Claude (Anthropic)":
            self.api_key_input.setPlaceholderText("Enter API Key here...")
            self.api_key_input.setEnabled(True)
            self.model_combo.addItems(["claude-3-5-sonnet-latest", "claude-3-haiku-20240307"])
        else:
            self.api_key_input.setPlaceholderText("Enter API Key here...")
            self.api_key_input.setEnabled(True)
            if saved_key:
                self.fetch_available_models()
            else:
                self.model_combo.addItem("Waiting for API Key...")

    def fetch_available_models(self):
        provider = self.provider_combo.currentText()
        api_key = self.api_key_input.text().strip()
        models = []

        if provider == "Claude (Anthropic)":
            return

        try:
            self.fetch_models_btn.setEnabled(False)
            QApplication.processEvents()

            if provider == "Ollama (Local)":
                req = urllib.request.Request("http://localhost:11434/api/tags")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m["name"] for m in data.get("models", [])]

            elif provider == "OpenAI":
                if not api_key: return
                req = urllib.request.Request("https://api.openai.com/v1/models")
                req.add_header("Authorization", f"Bearer {api_key}")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m["id"] for m in data.get("data", []) if "gpt" in m["id"] or "o1" in m["id"]]
                    models.sort(reverse=True)

            elif provider == "DeepSeek":
                if not api_key: return
                req = urllib.request.Request("https://api.deepseek.com/models")
                req.add_header("Authorization", f"Bearer {api_key}")
                req.add_header("Accept", "application/json")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m["id"] for m in data.get("data", [])]

            elif provider == "Google Gemini":
                if not api_key: return
                req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m["name"].replace("models/", "") for m in data.get("models", []) if "gemini" in m["name"]]

            if models:
                self.model_combo.clear()
                self.model_combo.addItems(models)
                last_model = self.settings.value(f"ai_copilot/{provider}_last_model", "")
                if last_model in models:
                    self.model_combo.setCurrentText(last_model)
                self.append_log(f"<font color='green'>✓ {provider} models auto-fetched.</font>", True)
                
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.model_combo.clear()
                self.model_combo.addItem("Invalid API Key")
                self.append_log("<font color='red'>✗ Error: Invalid API Key.</font>", True)
        except Exception:
            self.model_combo.clear()
            self.model_combo.addItem("Connection Error")
        finally:
            self.fetch_models_btn.setEnabled(True)

    def save_settings(self):
        provider = self.provider_combo.currentText()
        model = self.model_combo.currentText()
        api_key = self.api_key_input.text()
        
        self.settings.setValue("ai_copilot/last_provider", provider)
        self.settings.setValue(f"ai_copilot/{provider}_last_model", model)
        if provider != "Ollama (Local)":
            self.settings.setValue(f"ai_copilot/{provider}_api_key", api_key)

    def load_settings(self):
        last_provider = self.settings.value("ai_copilot/last_provider", "Google Gemini")
        index = self.provider_combo.findText(last_provider)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
            
        last_model = self.settings.value(f"ai_copilot/{last_provider}_last_model", "")
        if last_model:
            self.model_combo.setCurrentText(last_model)

    def append_log(self, text: str, is_html=False):
        if is_html:
            self.log_display.append(text)
        else:
            self.log_display.append(text.replace("\n", "<br>"))
        scrollbar = self.log_display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def run_ai_process(self):
        provider = self.provider_combo.currentText()
        model = self.model_combo.currentText()
        api_key = self.api_key_input.text()
        prompt = self.prompt_input.toPlainText()
        
        if not prompt.strip():
            return
            
        self.append_log(f"<b>You:</b> {prompt}", is_html=True)
        self.prompt_input.clear()
        
        # Подготовка интерфейса к ожиданию
        self.run_button.setEnabled(False)
        self.progress_bar.show()
        self.elapsed_time = 0.0
        self.timer.start(100) # Обновляем каждые 100 мс (0.1 сек)
        
        # Запускаем ИИ в фоне, чтобы QGIS не "зависал"
        context = f"Project CRS: {QgsProject.instance().crs().authid()}"
        self.worker = AIWorker(provider, model, api_key, prompt, context)
        self.worker.finished_signal.connect(self.on_ai_finished)
        self.worker.start()

    def on_ai_finished(self, ai_response, error_msg):
        # Останавливаем таймер и восстанавливаем интерфейс
        self.timer.stop()
        self.progress_bar.hide()
        self.run_button.setEnabled(True)
        self.run_button.setText("Run with AI")
        self.save_settings()

        from qgis_ai_copilot.core import executor

        if error_msg:
            self.append_log(f"<font color='red'>✗ Connection error: {error_msg}</font>", is_html=True)
            return

        self.append_log(f"<b>AI (took {self.elapsed_time:.1f}s):</b><br>{ai_response}", is_html=True)
        
        try:
            code = executor.extract_code(ai_response)
            if code:
                success, msg = executor.run_pyqgis_code(code)
                if not success:
                    self.append_log(f"<font color='red'>✗ Execution error: {msg}</font>", is_html=True)
                else:
                    self.append_log(f"<font color='green'>✓ Code executed successfully.</font>", is_html=True)
        except Exception as e:
            self.append_log(f"<font color='red'>✗ Critical error: {e}</font>", is_html=True)
