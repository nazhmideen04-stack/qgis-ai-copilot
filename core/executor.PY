import re
import sys
import os
import math
import json
import urllib.request
import traceback
import processing

import qgis.core
import qgis.gui
import qgis.analysis
import qgis.PyQt
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt import QtCore, QtGui, QtWidgets
from qgis.utils import iface

# Пытаемся подгрузить модули топологии и графов (если установлены)
try:
    import qgis.networkanalysis
except ImportError:
    pass

# Пытаемся подгрузить тяжелые растровые библиотеки (Они нужны для NDVI)
try:
    from osgeo import gdal, osr, ogr
except ImportError:
    pass

try:
    import numpy as np
except ImportError:
    pass

# Маскируем PyQt5 для QGIS 4 (Qt6)
sys.modules["PyQt5"] = qgis.PyQt
sys.modules["PyQt5.QtCore"] = QtCore
sys.modules["PyQt5.QtGui"] = QtGui
sys.modules["PyQt5.QtWidgets"] = QtWidgets

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
    # 1. Загружаем стандартные системные библиотеки Python
    scope = {
        "iface": iface,
        "processing": processing,
        "PyQt5": qgis.PyQt,
        "os": os,
        "sys": sys,
        "math": math,
        "json": json,
        "urllib": urllib
    }
    
    # 2. Добавляем GDAL, OSR и NumPy в память, если они найдены в системе
    if "osgeo.gdal" in sys.modules:
        scope["gdal"] = sys.modules["osgeo.gdal"]
        scope["osr"] = sys.modules["osgeo.osr"]
        scope["ogr"] = sys.modules["osgeo.ogr"]
    if "numpy" in sys.modules:
        scope["np"] = sys.modules["numpy"]
        scope["numpy"] = sys.modules["numpy"]
        
    # 3. АБСОЛЮТНЫЙ РЕЖИМ: Сканируем и загружаем ВСЕ классы из ВСЕХ модулей QGIS и Qt
    modules_to_load = [
        qgis.core,
        qgis.gui,
        qgis.analysis,
        QtCore,
        QtGui,
        QtWidgets
    ]
    
    if "qgis.networkanalysis" in sys.modules:
        modules_to_load.append(sys.modules["qgis.networkanalysis"])
        
    for mod in modules_to_load:
        for key, value in mod.__dict__.items():
            if not key.startswith("_"):
                scope[key] = value
                
    try:
        # Выполняем скрипт в этой всемогущей среде
        exec(code, scope)
        iface.mapCanvas().refresh()
        QApplication.processEvents()
        return True, "Успешно! Скрипт выполнен."
        
    except SyntaxError as e:
        return False, f"Ошибка синтаксиса (ИИ оборвал код).\nДетальнее: {e}\n👉 Нажмите 'Run with AI' еще раз."
        
    except Exception:
        return False, traceback.format_exc()
