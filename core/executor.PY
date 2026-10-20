"""Извлечение кода из ответа ИИ и его выполнение в подготовленном окружении QGIS."""
import importlib.util
import json
import math
import os
import re
import sys
import traceback
import urllib

import processing
import qgis.analysis
import qgis.core
import qgis.gui
import qgis.PyQt
from qgis.PyQt import QtCore, QtGui, QtWidgets
from qgis.PyQt.QtWidgets import QApplication
from qgis.utils import iface

from .log import log_info, log_warning

_SCOPE_CACHE = None

_FENCE_RE = re.compile(r"```[ \t]*(?:python|py|python3)?[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
_OPEN_FENCE_RE = re.compile(r"```[ \t]*(?:python|py|python3)[ \t]*\r?\n", re.IGNORECASE)


def _alias_pyqt5():
    """Если PyQt5 нет (QGIS 4 / Qt6), подменяем его на qgis.PyQt, чтобы старый код ИИ не падал.
    Если настоящий PyQt5 установлен — ничего не трогаем, чтобы не сломать другие плагины."""
    if "PyQt5" in sys.modules or importlib.util.find_spec("PyQt5") is not None:
        return
    sys.modules["PyQt5"] = qgis.PyQt
    sys.modules["PyQt5.QtCore"] = QtCore
    sys.modules["PyQt5.QtGui"] = QtGui
    sys.modules["PyQt5.QtWidgets"] = QtWidgets


def _base_scope() -> dict:
    """Окружение для exec. Строится один раз (тысячи классов — дорого делать каждый раз)."""
    global _SCOPE_CACHE
    if _SCOPE_CACHE is not None:
        return _SCOPE_CACHE

    _alias_pyqt5()
    modules = [qgis.core, qgis.gui, qgis.analysis, QtCore, QtGui, QtWidgets]
    scope = {}
    for mod in modules:
        for key, value in vars(mod).items():
            if not key.startswith("_"):
                scope[key] = value

    scope.update({
        "iface": iface, "processing": processing, "os": os, "sys": sys,
        "math": math, "json": json, "urllib": urllib,
    })
    try:
        from osgeo import gdal, ogr, osr
        scope.update({"gdal": gdal, "ogr": ogr, "osr": osr})
    except ImportError:
        pass
    try:
        import numpy as np
        scope.update({"np": np, "numpy": np})
    except ImportError:
        pass

    _SCOPE_CACHE = scope
    return scope


def _fix_imports(code: str) -> str:
    code = re.sub(r"\bfrom\s+PyQt[56](\.|\s)", r"from qgis.PyQt\1", code)
    code = re.sub(r"\bimport\s+PyQt[56]\b(?!\.)", "import qgis.PyQt as PyQt5", code)
    return code


def extract_code(text: str) -> tuple:
    """Возвращает (code, warning).
    code == "" — в ответе нет кода (обычный текстовый ответ, выполнять нечего)."""
    if not text:
        return "", ""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        code = "\n\n".join(b.strip() for b in blocks if b.strip())
        return _fix_imports(code), ""

    # Блок открылся, но не закрылся — ответ оборван (лимит токенов / обрыв сети)
    opened = _OPEN_FENCE_RE.search(text)
    if opened:
        partial = text[opened.end():].strip()
        return "", ("The code block in the answer is incomplete (the response was cut off). "
                    "Not executed — try again or ask for a shorter script." if partial else "")
    return "", ""


def is_comment_only(code: str) -> bool:
    lines = [l.strip() for l in code.splitlines() if l.strip()]
    return all(l.startswith("#") for l in lines)


def run_pyqgis_code(code: str) -> tuple:
    """Выполняет код в главном потоке. Возвращает (success, message)."""
    scope = dict(_base_scope())
    scope["__name__"] = "__main__"
    try:
        compiled = compile(code, "<ai_generated_code>", "exec")
    except SyntaxError as e:
        return False, (f"Syntax error in the AI code (line {e.lineno}): {e.msg}\n"
                       f"{(e.text or '').rstrip()}\nTry 'Run with AI' again.")
    log_info("Executing AI-generated code:\n" + code)
    try:
        # Выполнение кода — основная функция плагина (как Python-консоль QGIS).
        # Код выполняется ТОЛЬКО после явного действия пользователя: кнопка «▶ Run Code»
        # или включённый им «Auto-run» с отдельным подтверждением. Код виден в чате и журнале.
        exec(compiled, scope)  # nosec B102
    except Exception:
        error = traceback.format_exc(limit=6)
        log_warning("AI-generated code failed:\n" + error)
        return False, error
    finally:
        _refresh_canvas()
    return True, "Code executed successfully."


def _refresh_canvas():
    try:
        iface.mapCanvas().refresh()
        QApplication.processEvents()
    except Exception as e:  # обновление карты не критично, но пишем причину в журнал
        log_warning(f"Could not refresh the map canvas: {e}")
