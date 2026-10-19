from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QTextEdit, 
    QLineEdit, QPushButton, QComboBox, QLabel, QMessageBox
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.core import QgsSettings
from ..core.context import get_project_context
from ..core.ai_client import request_ai_code
from ..core.executor import extract_code, run_pyqgis_code

PROVIDER_CONFIG = {
    "Google Gemini": ["gemini-3.8-flash", "gemini-2.0-flash"],
    "OpenAI": ["gpt-4o-mini", "gpt-4o", "o3-mini"],
    "DeepSeek": ["deepseek-chat", "deepseek-reasoner"],
    "Claude (Anthropic)": ["claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"]
}

class AIWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, provider: str, model_name: str, api_key: str, prompt: str, context: str):
        super().__init__()
        self.provider = provider
        self.model_name = model_name
        self.api_key = api_key
        self.prompt = prompt
        self.context = context

    def run(self):
        try:
            raw_answer = request_ai_code(self.provider, self.model_name, self.api_key, self.prompt, self.context)
            self.finished.emit(raw_answer)
        except Exception as e:
            self.error.emit(str(e))

class AICopilotDockWidget(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("AI Copilot", parent)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.settings = QgsSettings()
        self.worker = None

        container = QWidget()
        layout = QVBoxLayout(container)

        # Provider Selector
        layout.addWidget(QLabel("AI Provider:"))
        self.provider_box = QComboBox()
        self.provider_box.addItems(list(PROVIDER_CONFIG.keys()))
        self.provider_box.currentTextChanged.connect(self.on_provider_changed)
        layout.addWidget(self.provider_box)

        # Model Selector
        layout.addWidget(QLabel("Model Name (select or type):"))
        self.model_box = QComboBox()
        self.model_box.setEditable(True)
        self.model_box.currentTextChanged.connect(self.save_current_model)
        layout.addWidget(self.model_box)

        # API Key Field
        layout.addWidget(QLabel("API Key:"))
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("Paste provider API Key here...")
        self.key_input.textChanged.connect(self.save_current_key)
        layout.addWidget(self.key_input)

        # Output / Code Area
        layout.addWidget(QLabel("Chat & Code Log:"))
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)

        # Prompt Input
        layout.addWidget(QLabel("Instruction / Task:"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText("Например: как работает буфер? Или: открой OSM карту...")
        self.prompt_input.setMaximumHeight(80)
        layout.addWidget(self.prompt_input)

        # Action Button
        self.run_btn = QPushButton("Run with AI")
        self.run_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.run_btn.clicked.connect(self.on_submit)
        layout.addWidget(self.run_btn)

        self.setWidget(container)
        self.on_provider_changed(self.provider_box.currentText())

    def get_provider_key_prefix(self) -> str:
        return self.provider_box.currentText().split()[0].lower()

    def on_provider_changed(self, provider: str):
        self.model_box.blockSignals(True)
        self.model_box.clear()
        models = PROVIDER_CONFIG.get(provider, [])
        self.model_box.addItems(models)
        self.model_box.blockSignals(False)

        pref = self.get_provider_key_prefix()
        saved_key = self.settings.value(f"ai_copilot/key_{pref}", "", type=str)
        saved_model = self.settings.value(f"ai_copilot/model_{pref}", "", type=str)

        self.key_input.setText(saved_key)
        if saved_model:
            self.model_box.setEditText(saved_model)
        elif models:
            self.model_box.setCurrentIndex(0)

    def save_current_key(self):
        pref = self.get_provider_key_prefix()
        self.settings.setValue(f"ai_copilot/key_{pref}", self.key_input.text().strip())

    def save_current_model(self, model_name: str):
        pref = self.get_provider_key_prefix()
        self.settings.setValue(f"ai_copilot/model_{pref}", model_name.strip())

    def on_submit(self):
        prompt = self.prompt_input.toPlainText().strip()
        api_key = self.key_input.text().strip()
        provider = self.provider_box.currentText()
        model_name = self.model_box.currentText().strip()

        if not prompt or not api_key or not model_name:
            QMessageBox.warning(self, "Missing Parameters", "Please provide an API Key, Model Name, and prompt task.")
            return

        self.log_area.append(f"<b>Вы:</b> {prompt}")
        self.run_btn.setEnabled(False)
        self.run_btn.setText("AI думает...")

        ctx = get_project_context()

        self.worker = AIWorker(provider, model_name, api_key, prompt, ctx)
        self.worker.finished.connect(self.on_generation_finished)
        self.worker.error.connect(self.on_generation_error)
        self.worker.start()

    def on_generation_finished(self, raw_answer: str):
        # 1. Форматируем ответ ИИ: текст + стилизованный блок кода
        escaped_text = raw_answer.replace('<', '&lt;').replace('>', '&gt;')
        html_text = escaped_text.replace('```python', '<br><pre style="background-color:#f4f4f4; color:#333; padding:10px; border: 1px solid #ddd; border-radius:4px; font-family: monospace;">').replace('```', '</pre><br>').replace('\n', '<br>')
        
        # Выводим текстовый ответ ИИ в красивом голубом облачке
        self.log_area.append(f"<div style='background-color:#e6f7ff; padding:10px; border-left: 4px solid #007acc; margin-bottom: 5px; font-size: 13px;'><b>🤖 AI Copilot:</b><br>{html_text}</div>")

        # 2. Вытаскиваем сам код для QGIS
        code = extract_code(raw_answer)
        
        # 3. Выполняем скрипт, ТОЛЬКО если ИИ реально прислал блок кода
        if "```" in raw_answer:
            ok, msg = run_pyqgis_code(code)
            if ok:
                self.log_area.append("<b style='color:green;'>✅ Скрипт успешно применён к карте!</b><br><br>")
            else:
                self.log_area.append(f"<b style='color:red;'>❌ Ошибка выполнения скрипта:</b><br>{msg}<br><br>")
        else:
            # Если кода нет, значит мы просто пообщались с ИИ
            self.log_area.append("<i style='color:gray;'>Скрипт не обнаружен (только текстовый ответ).</i><br><br>")

        # Разблокируем кнопку
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run with AI")

    def on_generation_error(self, err_msg: str):
        self.log_area.append(f"<b style='color:red;'>API Error:</b> {err_msg}<br>")
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run with AI")
