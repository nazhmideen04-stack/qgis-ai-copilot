from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QTextEdit, 
    QLineEdit, QPushButton, QComboBox, QLabel, QMessageBox
)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsSettings
from ..core.context import get_project_context
from ..core.ai_client import request_ai_code
from ..core.executor import extract_code, run_pyqgis_code

class AICopilotDockWidget(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("AI Copilot", parent)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.settings = QgsSettings()

        container = QWidget()
        layout = QVBoxLayout(container)

        # Provider selector
        layout.addWidget(QLabel("Select AI Provider:"))
        self.provider_box = QComboBox()
        self.provider_box.addItems(["Google Gemini", "OpenAI (GPT-4o)", "DeepSeek", "Claude (Anthropic)"])
        self.provider_box.currentTextChanged.connect(self.load_saved_key)
        layout.addWidget(self.provider_box)

        # API Key field
        layout.addWidget(QLabel("API Key:"))
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("Enter your API Key...")
        self.key_input.textChanged.connect(self.save_current_key)
        layout.addWidget(self.key_input)

        # Log & Code view
        layout.addWidget(QLabel("Generated Code & Execution Log:"))
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)

        # Prompt input
        layout.addWidget(QLabel("What would you like to do?"))
        self.prompt_input = QTextEdit()
        self.prompt_input.setPlaceholderText("e.g., Create a 500m buffer around the active layer...")
        self.prompt_input.setMaximumHeight(70)
        layout.addWidget(self.prompt_input)

        # Submit button
        self.run_btn = QPushButton("Run with AI")
        self.run_btn.clicked.connect(self.on_submit)
        layout.addWidget(self.run_btn)

        self.setWidget(container)
        self.load_saved_key()

    def get_settings_key_name(self):
        provider = self.provider_box.currentText().split()[0].lower()
        return f"ai_copilot/api_key_{provider}"

    def load_saved_key(self):
        saved = self.settings.value(self.get_settings_key_name(), "", type=str)
        self.key_input.setText(saved)

    def save_current_key(self):
        self.settings.setValue(self.get_settings_key_name(), self.key_input.text().strip())

    def on_submit(self):
        prompt = self.prompt_input.toPlainText().strip()
        api_key = self.key_input.text().strip()
        provider = self.provider_box.currentText()

        if not prompt or not api_key:
            QMessageBox.warning(self, "Warning", "Please provide both an API Key and a prompt!")
            return

        self.log_area.append(f"<b>Request ({provider}):</b> {prompt}")
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Processing...")

        try:
            ctx = get_project_context()
            raw_answer = request_ai_code(provider, api_key, prompt, ctx)
            code = extract_code(raw_answer)
            self.log_area.append(f"<pre style='color:#007acc;'>{code}</pre>")

            ok, msg = run_pyqgis_code(code)
            if ok:
                self.log_area.append("<b style='color:green;'>Success! Map updated.</b><br>")
            else:
                self.log_area.append(f"<b style='color:red;'>Execution Error:</b><br>{msg}<br>")
        except Exception as err:
            self.log_area.append(f"<b style='color:red;'>API Error:</b> {str(err)}<br>")
        finally:
            self.run_btn.setEnabled(True)
            self.run_btn.setText("Run with AI")