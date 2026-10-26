"""Status panel: animated indicator, stage, time, details and progress."""
import time

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout

from ..core.i18n import dynamic, tr, tr_lines

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Border and text colour for each stage
STAGE_COLORS = {
    "context": "#546e7a",
    "connect": "#1565c0",
    "thinking": "#6a1b9a",
    "writing": "#2e7d32",
    "running": "#e65100",
    "download": "#00838f",
    "agent": "#4527a0",
    "models": "#1565c0",
    "stopping": "#c62828",
}


class StatusPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("AICopilotStatus")
        dynamic(self)

        self.spinner_label = QLabel()
        self.spinner_label.setFixedWidth(16)
        self.stage_label = QLabel()
        self.time_label = QLabel()
        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: gray; font-size: 11px;")

        for label in (self.spinner_label, self.stage_label, self.time_label, self.detail_label):
            dynamic(label)
        self.bar = QProgressBar()
        self.bar.setMaximumHeight(8)
        self.bar.setTextVisible(False)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.spinner_label)
        top.addWidget(self.stage_label, 1)
        top.addWidget(self.time_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail_label)

        self._frame = 0
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    # ------------------------------------------------------------------ API
    def start(self, kind, text, detail=""):
        """Shows the panel and starts the timer (new operation)."""
        self._started = time.monotonic()
        self.set_stage(kind, text, detail)
        self.set_progress(None)
        self.show()
        if not self._timer.isActive():
            self._timer.start(100)
        self._tick()

    def set_stage(self, kind, text, detail=None):
        color = STAGE_COLORS.get(kind, "#455a64")
        self.stage_label.setText(f"<b style='color:{color}'>{tr(text)}</b>")
        self.setStyleSheet(f"#AICopilotStatus {{ border: 1px solid {color}; border-radius: 6px; }}")
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")
        if detail is not None:
            self.set_detail(detail)

    def set_detail(self, text):
        self.detail_label.setText(tr_lines(text) if text else text)
        self.detail_label.setVisible(bool(text))

    def set_progress(self, percent):
        """percent=None — endless animation; 0..100 — real percent."""
        if percent is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(max(0, min(100, percent))))

    def elapsed(self):
        return time.monotonic() - self._started if self._started else 0.0

    def stop(self):
        self._timer.stop()
        self.hide()

    def refresh(self):
        """Refresh the animation manually (when the main thread is busy running code)."""
        self._tick()

    # ------------------------------------------------------------------ internal
    def _tick(self):
        self._frame = (self._frame + 1) % len(SPINNER)
        self.spinner_label.setText(SPINNER[self._frame])
        seconds = self.elapsed()
        self.time_label.setText(f"{int(seconds // 60)}:{int(seconds % 60):02d}" if seconds >= 60 else f"{seconds:.1f}s")
