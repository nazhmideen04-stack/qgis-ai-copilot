import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction  # Qt6: QAction lives in QtGui
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from .ui.dock_widget import AICopilotDockWidget


class AICopilotPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, "AI Copilot", self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addVectorToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu("&AI Copilot", self.action)  # category=Vector in metadata.txt

    def unload(self):
        if self.dock is not None:
            self.dock.shutdown()
            for dock in (self.dock.request_dock, self.dock):
                self.iface.removeDockWidget(dock)
                dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginVectorMenu("&AI Copilot", self.action)
            self.iface.removeVectorToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None

    def toggle_dock(self):
        if self.dock is None:
            # Settings + chat: docked inside QGIS (right side by default, or where the user put it last time).
            # Request bar: docked at the bottom, the input box centred.
            self.dock = AICopilotDockWidget(self.iface.mainWindow())
            self.iface.addDockWidget(self.dock.saved_area(), self.dock)
            self.iface.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock.request_dock)
            self.dock.restore_floating()
            self.dock.set_panels_visible(True)
            self.dock.prompt_input.setFocus()
            return
        if self.dock.panels_visible():
            self.dock.set_panels_visible(False)
            return
        self.dock.set_panels_visible(True)
        self.dock.prompt_input.setFocus()
