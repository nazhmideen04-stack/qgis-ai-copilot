"""Extracts code from the AI answer and runs it in a prepared QGIS environment."""
import contextlib
import importlib.util
import io
import textwrap
import json
import math
import os
import re
import sys
import traceback
import urllib

import qgis.analysis
import qgis.core
import qgis.gui
import qgis.PyQt
from qgis.PyQt import QtCore, QtGui, QtWidgets
from qgis.PyQt.QtWidgets import QApplication
from qgis.utils import iface

from .log import log_info, log_warning
from .progress import CancelledByUser, ProcessingProxy, is_cancel_requested, reset_cancel
from .tools import COMMANDS

MAX_OUTPUT_CHARS = 6000

_SCOPE_CACHE = None

_FENCE_LINE_RE = re.compile(r"^\s*```+\s*([A-Za-z0-9_+.-]*)\s*$")
_PY_LANGS = {"", "python", "py", "python3", "pyqgis"}


def _alias_pyqt5():
    """If PyQt5 is missing (QGIS 4 / Qt6), alias it to qgis.PyQt so older AI code does not fail.
    If a real PyQt5 is installed, leave it alone so other plugins are not broken."""
    if "PyQt5" in sys.modules or importlib.util.find_spec("PyQt5") is not None:
        return
    sys.modules["PyQt5"] = qgis.PyQt
    sys.modules["PyQt5.QtCore"] = QtCore
    sys.modules["PyQt5.QtGui"] = QtGui
    sys.modules["PyQt5.QtWidgets"] = QtWidgets


def _base_scope() -> dict:
    """Scope for exec. Built once (thousands of classes — too expensive to rebuild every time)."""
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
        "iface": iface, "processing": ProcessingProxy(), "os": os, "sys": sys,
        "math": math, "json": json, "urllib": urllib,
    })
    scope.update(COMMANDS)  # the plugin's commands (see core/tools.py)
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
    """Returns (code, warning).
    code == "" — the answer has no code (a plain text answer, nothing to run).
    Handles nested/double fences (```` ``` ```python ... ``` ``` ````),
    indented blocks (inside lists) and ignores blocks in other languages (bash, json…)."""
    if not text:
        return "", ""
    blocks = []
    current, lang = None, ""
    for line in text.splitlines():
        fence = _FENCE_LINE_RE.match(line)
        if fence:
            marker_lang = fence.group(1).lower()
            if current is None:
                current, lang = [], marker_lang
            elif not any(x.strip() for x in current):
                current, lang = [], marker_lang or lang  # empty block: a fence inside a fence
            else:
                blocks.append((lang, current))
                current, lang = None, ""
            continue
        if current is not None:
            current.append(line)

    code_blocks = [textwrap.dedent("\n".join(body)).strip() for lg, body in blocks if lg in _PY_LANGS]
    code = "\n\n".join(b for b in code_blocks if b)

    warning = ""
    if current is not None and any(x.strip() for x in current) and lang in _PY_LANGS:
        warning = ("The code block in the answer is incomplete (the response was cut off). "
                   "Not executed — try again or ask for a shorter script.")
        return "", warning
    if not code and "```" in text:
        warning = "The answer contains a code block, but it is empty or not Python — nothing to run."
    return _fix_imports(code), warning


def is_comment_only(code: str) -> bool:
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    return all(line.startswith("#") for line in lines)


def run_pyqgis_code(code: str) -> tuple:
    """Runs the code in the main thread. Returns (success, message, printed_output).
    printed_output — everything the code printed: shown in the chat and
    sent to the AI with the next request (so the AI can explore an unfamiliar plugin)."""
    scope = dict(_base_scope())
    scope["__name__"] = "__main__"
    try:
        compiled = compile(code, "<ai_generated_code>", "exec")
    except SyntaxError as e:
        return False, (f"Syntax error in the AI code (line {e.lineno}): {e.msg}\n"
                       f"{(e.text or '').rstrip()}"), ""

    log_info("Executing AI-generated code:\n" + code)
    reset_cancel()
    buffer = io.StringIO()
    try:
        # Running code is the core feature of the plugin (like the QGIS Python console).
        # Code runs ONLY after an explicit user action: the «Run Code» button
        # or Auto-run enabled by the user with a separate confirmation. The code is visible in the chat and the log.
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            exec(compiled, scope)  # nosec B102
    except CancelledByUser:
        return False, "Stopped by user.", _trim(buffer.getvalue())
    except Exception:
        if is_cancel_requested():
            return False, "Stopped by user.", _trim(buffer.getvalue())
        error = traceback.format_exc(limit=6)
        log_warning("AI-generated code failed:\n" + error)
        return False, error, _trim(buffer.getvalue())
    finally:
        _refresh_canvas()
    return True, "Code executed successfully.", _trim(buffer.getvalue())


def _trim(text):
    text = text.strip()
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n... (output truncated, {len(text)} chars total)"
    return text


def _refresh_canvas():
    try:
        iface.mapCanvas().refresh()
        QApplication.processEvents()
    except Exception as e:  # a canvas refresh is not critical, but log the reason
        log_warning(f"Could not refresh the map canvas: {e}")
