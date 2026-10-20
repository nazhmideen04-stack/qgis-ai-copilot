"""Writes to the QGIS log (View → Panels → Log Messages → «AI Copilot» tab)."""
from qgis.core import Qgis, QgsMessageLog

TAG = "AI Copilot"


def log_warning(message: str):
    QgsMessageLog.logMessage(str(message), TAG, Qgis.MessageLevel.Warning)


def log_info(message: str):
    QgsMessageLog.logMessage(str(message), TAG, Qgis.MessageLevel.Info)
