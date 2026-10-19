import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt
from .ui.dock_widget import AICopilotDockWidget

class AICopilotPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None

    def initGui(self):
        # Путь к файлу icon.png в папке плагина
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        # Создаем действие с иконкой
        self.action = QAction(icon, "AI Copilot", self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        
        # Добавляем в панель инструментов и в меню
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&AI Copilot", self.action)

    def unload(self):
        if self.dock:
            self.iface.removeDockWidget(self.dock)
        self.iface.removePluginMenu("&AI Copilot", self.action)
        self.iface.removeToolBarIcon(self.action)

    def toggle_dock(self):
        if not self.dock:
            self.dock = AICopilotDockWidget(self.iface.mainWindow())
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.show()