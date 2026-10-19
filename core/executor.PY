import re
import sys
import traceback
import processing
import qgis.core
import qgis.PyQt
from qgis.PyQt.QtWidgets import QApplication
from qgis.utils import iface

# Маскируем PyQt5 в системных модулях для совместимости со старыми скриптами
sys.modules["PyQt5"] = qgis.PyQt
sys.modules["PyQt5.QtCore"] = qgis.PyQt.QtCore
sys.modules["PyQt5.QtGui"] = qgis.PyQt.QtGui
sys.modules["PyQt5.QtWidgets"] = qgis.PyQt.QtWidgets

def extract_code(text: str) -> str:
    match = re.search(r"```[a-zA-Z]*\n(.*?)```", text, re.IGNORECASE | re.DOTALL)
    raw = match.group(1) if match else text
    
    raw = re.sub(r"```[a-zA-Z]*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```", "", raw)
    raw = raw.strip()

    cleaned = re.sub(r"\bfrom\s+PyQt5\b", "from qgis.PyQt", raw)
    cleaned = re.sub(r"\bimport\s+PyQt5\b", "import qgis.PyQt as PyQt5", cleaned)
    return cleaned

def run_pyqgis_code(code: str) -> tuple[bool, str]:
    # 1. Базовые глобальные переменные
    scope = {
        "iface": iface,
        "processing": processing,
        "PyQt5": qgis.PyQt,
    }
    
    # 2. МАГИЯ: Автоматически забрасываем ВСЕ тысячи классов QGIS (включая QgsColorRampGradient) в память!
    for key, value in qgis.core.__dict__.items():
        if not key.startswith("_"):
            scope[key] = value
            
    try:
        exec(code, scope)
        iface.mapCanvas().refresh()
        QApplication.processEvents()
        return True, "Успешно! Карта обновлена."
        
    except SyntaxError as e:
        return False, f"Нейросеть оборвала текст или ошиблась в синтаксисе.\nДетальнее: {e}\n\n👉 Решение: Нажмите 'Run with AI' еще раз."
        
    except Exception:
        return False, traceback.format_exc()
