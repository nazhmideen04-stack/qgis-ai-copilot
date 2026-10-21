"""Writes to the QGIS log (View → Panels → Log Messages → «AI Copilot» tab)."""
from qgis.core import QgsMessageLog

from .compat import message_level

TAG = "AI Copilot"


def log_warning(message: str):
    QgsMessageLog.logMessage(str(message), TAG, message_level("Warning"))


def log_info(message: str):
    QgsMessageLog.logMessage(str(message), TAG, message_level("Info"))
