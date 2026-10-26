"""
Check of AI code before it runs: finds actions that Undo AI cannot revert or that reach outside QGIS.

- deleting / moving / overwriting files and folders;
- system commands and other programs (subprocess, os.system …);
- hidden code (exec / eval / compile / __import__ of strings) that cannot be reviewed;
- writing straight into the data of an existing layer through its provider (SHP, GPKG, PostGIS … are changed on
  disk at once, without the edit buffer, so Undo AI cannot revert it). Memory layers created by the code itself are
  normal and not reported;
- removing every layer / clearing the project.
The check reads the code (it does not run it). Returns a list of human readable reasons; empty = nothing risky.
"""
import ast
import re

from .i18n import language

FILE_DELETE = {("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs"), ("shutil", "rmtree"),
               ("shutil", "move"), ("os", "rename"), ("os", "replace"), ("os", "truncate")}
SYSTEM = {("os", "system"), ("os", "popen"), ("os", "startfile"), ("os", "kill"), ("os", "execv"),
          ("os", "execl"), ("os", "spawnv"), ("os", "spawnl"), ("QProcess", "start"), ("QProcess", "execute"),
          ("QProcess", "startDetached")}
SYSTEM_MODULES = {"subprocess", "ctypes", "socket", "multiprocessing", "winreg", "pty"}
PROVIDER_EDITS = {"addFeatures", "deleteFeatures", "changeAttributeValues", "changeGeometryValues",
                  "changeFeatures", "addAttributes", "deleteAttributes", "renameAttributes", "truncate",
                  "createSpatialIndex"}
PATH_DELETE = {"unlink", "rmdir"}  # pathlib.Path methods
HIDDEN = {"exec", "eval", "compile", "__import__"}
PROJECT_WIPE = {"clear", "removeAllMapLayers"}


def _dotted(node):
    """'os.path.remove' style name of a call target, or ''."""
    parts = []
    while isinstance(node, (ast.Attribute, ast.Call)):
        if isinstance(node, ast.Call):  # QgsProject.instance().clear -> QgsProject.instance.clear
            node = node.func
            continue
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def check(code):
    reasons = []

    def add(text):
        if text not in reasons:
            reasons.append(text)

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # the executor reports syntax errors itself
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if name.split(".")[0] in SYSTEM_MODULES:
                    add(f"uses '{name.split('.')[0]}' (system commands / other programs)")
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        parts = name.split(".")
        pair = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", parts[-1] if parts else "")
        if pair in FILE_DELETE or name in ("shutil.rmtree", "shutil.move"):
            add(f"deletes or moves files ({name})")
        elif pair in SYSTEM or parts[0] in SYSTEM_MODULES:
            add(f"runs a system command or another program ({name})")
        elif parts and parts[-1] in PATH_DELETE and len(parts) > 1 and parts[0] not in ("os",):
            add(f"deletes files ({name})")
        elif name in HIDDEN:
            add(f"runs hidden code from text ({name})")
        elif parts and parts[-1] in PROJECT_WIPE and ("QgsProject" in name or "project" in name.lower()):
            add("removes all layers / clears the project")
        elif name == "open" and _write_mode(node):
            add("writes or overwrites a file with open()")
    if _provider_edits_existing(code):
        add("writes straight into the data of an existing layer (for file layers like SHP / GPKG this cannot be "
            "undone)")
    return reasons


def _write_mode(node):
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in "wax")


EXISTING_GETTERS = {"get_layer", "mapLayersByName", "mapLayer", "activeLayer", "selectedLayers", "mapLayers",
                    "layers", "checkedLayers"}


def _uses_existing(node):
    """True if the expression gets a layer that is already in the project (get_layer, mapLayersByName …)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            last = _dotted(sub.func).split(".")[-1]
            if last in EXISTING_GETTERS:
                return True
    return False


def _is_new_layer(node):
    """True for QgsVectorLayer(..., "memory"), layer.materialize(...): layers created by the code itself."""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted(node.func).split(".")[-1]
    if name == "materialize":
        return True
    if name == "QgsVectorLayer":
        return any(isinstance(a, ast.Constant) and a.value == "memory" for a in list(node.args) +
                   [k.value for k in node.keywords])
    return False


def _provider_edits_existing(code):
    """Provider edits (dataProvider().addFeatures …) on a layer that already existed in the project.
    Layers created by the code (memory layers, Processing results) are removed by Undo AI anyway."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    existing, new, risky_providers = set(), set(), set()
    nodes = sorted((n for n in ast.walk(tree) if hasattr(n, "lineno")), key=lambda n: (n.lineno, n.col_offset))
    for node in nodes:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
            if _is_new_layer(value):
                new.update(names)
                existing.difference_update(names)
            elif isinstance(value, ast.Call) and _dotted(value.func).endswith("dataProvider"):
                owner = value.func.value if isinstance(value.func, ast.Attribute) else None
                if owner is not None and _layer_is_existing(owner, existing, new):
                    risky_providers.update(names)
            elif _uses_existing(value) or (isinstance(value, ast.Name) and value.id in existing):
                existing.update(names)
                new.difference_update(names)
        elif isinstance(node, ast.For) and _uses_existing(node.iter):
            existing.update(t.id for t in ast.walk(node.target) if isinstance(t, ast.Name))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in PROVIDER_EDITS:
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in risky_providers:
                return True
            if isinstance(target, ast.Call) and _dotted(target.func).endswith("dataProvider"):
                owner = target.func.value if isinstance(target.func, ast.Attribute) else None
                if owner is not None and _layer_is_existing(owner, existing, new):
                    return True
    return False


def _layer_is_existing(owner, existing, new):
    if isinstance(owner, ast.Name):
        return owner.id in existing and owner.id not in new
    return _uses_existing(owner)


RU_REASONS = [
    (r"^uses '(.*)' \(system commands / other programs\)$", r"использует «\1» (системные команды / другие программы)"),
    (r"^deletes or moves files \((.*)\)$", r"удаляет или перемещает файлы (\1)"),
    (r"^runs a system command or another program \((.*)\)$", r"запускает системную команду или программу (\1)"),
    (r"^deletes files \((.*)\)$", r"удаляет файлы (\1)"),
    (r"^runs hidden code from text \((.*)\)$", r"выполняет скрытый код из текста (\1)"),
    (r"^removes all layers / clears the project$", r"удаляет все слои / очищает проект"),
    (r"^writes or overwrites a file with open\(\)$", r"записывает или перезаписывает файл через open()"),
    (r"^writes straight into the data of an existing layer .*$",
     r"пишет прямо в данные существующего слоя (для файловых слоёв SHP / GPKG это нельзя отменить)"),
]
_RU = [(re.compile(p), t) for p, t in RU_REASONS]


def describe(reasons):
    """Reasons in the interface language."""
    if language() != "ru":
        return reasons
    out = []
    for reason in reasons:
        for pattern, template in _RU:
            if pattern.match(reason):
                reason = pattern.sub(template, reason)
                break
        out.append(reason)
    return out
