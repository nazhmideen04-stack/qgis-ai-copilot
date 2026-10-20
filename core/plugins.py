"""
Interaction with other installed QGIS plugins.

Three ways to work with plugins:
1. Processing algorithms registered by plugins (QuickOSM, SAGA, ORS Tools...)
   — called through run_alg(). The most reliable way.
2. Plugin menu items / buttons — open a plugin's dialog.
3. Direct access to the plugin object through qgis.utils.plugins (advanced).

The public commands for AI code are in core/tools.py.
"""
import re
import time

import qgis.utils
from qgis.core import Qgis, QgsApplication, QgsProcessingParameterDefinition, QgsSettings
from qgis.PyQt.QtWidgets import QMessageBox

from .log import log_info, log_warning

# Built-in Processing providers (everything else comes from plugins)
BUILTIN_PROVIDERS = {"native", "qgis", "gdal", "3d", "pdal", "model", "script", "project", "grass", "grass7"}
# System Python plugins present in every QGIS installation
CORE_PLUGINS = {"processing", "grassprovider", "db_manager", "MetaSearch", "otbprovider", "sagaprovider"}

MAX_PLUGIN_ALGS_IN_CONTEXT = 25
MAX_RELEVANT_ALGS = 5
MAX_DETAILED_ALGS = 2
STATIC_CONTEXT_TTL = 60.0  # seconds; the plugin list rarely changes, no need to rebuild it every request

_static_cache = {"time": 0.0, "text": ""}

_STOPWORDS = {"the", "and", "for", "with", "from", "into", "that", "this", "all", "layer", "layers",
              "create", "make", "use", "using", "map", "please", "data"}


def _own_package():
    return __name__.split(".")[0]


def _meta(name, field):
    try:
        value = qgis.utils.pluginMetadata(name, field)
    except Exception as e:
        log_warning(f"Cannot read metadata '{field}' of plugin {name}: {e}")
        return ""
    return "" if value in (None, "__error__") else str(value)


def _strip_amp(text):
    return text.replace("&", "").replace("…", "").replace("...", "").strip()


# --------------------------------------------------------------------------- discovery

def get_plugins_info(include_core=False):
    """Installed Python plugins: package name, title, version, active or not."""
    own = _own_package()
    active = set(qgis.utils.active_plugins)
    result = []
    for name in sorted(set(qgis.utils.available_plugins) | active):
        if name == own or (not include_core and name in CORE_PLUGINS):
            continue
        result.append({
            "package": name,
            "name": _meta(name, "name") or name,
            "version": _meta(name, "version"),
            "description": _meta(name, "description")[:160],
            "active": name in active,
        })
    return result


def _providers():
    registry = QgsApplication.processingRegistry()
    return list(registry.providers()) if registry else []


def _all_algorithms():
    registry = QgsApplication.processingRegistry()
    return list(registry.algorithms()) if registry else []


def get_plugin_providers():
    """Processing providers added by third-party plugins."""
    out = []
    for p in _providers():
        if p.id() in BUILTIN_PROVIDERS:
            continue
        out.append({"id": p.id(), "name": p.name(), "active": p.isActive(),
                    "algorithms": [(a.id(), a.displayName()) for a in p.algorithms()]})
    return out


def _collect_menu(menu, path, out):
    for action in menu.actions():
        if action.isSeparator():
            continue
        text = _strip_amp(action.text())
        if not text:
            continue
        sub = action.menu()
        if sub is not None:
            _collect_menu(sub, path + [text], out)
        else:
            out.append((" > ".join(path + [text]), action))


def _plugin_actions():
    """[(menu path, QAction)] for menu items added by plugins."""
    iface = qgis.utils.iface
    if iface is None:
        return []
    out = []
    for getter, title in (("pluginMenu", "Plugins"), ("webMenu", "Web"), ("databaseMenu", "Database")):
        menu = getattr(iface, getter, lambda: None)()
        if menu is not None:
            _collect_menu(menu, [title], out)

    # In the Vector/Raster menus plugins create a submenu named after themselves — take only those
    plugin_titles = {p["name"].lower() for p in get_plugins_info()}
    for getter, title in (("vectorMenu", "Vector"), ("rasterMenu", "Raster")):
        menu = getattr(iface, getter, lambda: None)()
        if menu is None:
            continue
        for action in menu.actions():
            sub = action.menu()
            if sub is not None and _strip_amp(action.text()).lower() in plugin_titles:
                _collect_menu(sub, [title, _strip_amp(action.text())], out)

    own_menu = "Plugins > AI Copilot"
    return [(p, a) for p, a in out if not p.startswith(own_menu) and not p.startswith("Plugins > Manage")]


def _tokens(text):
    words = re.findall(r"[^\W\d_][\w]{2,}", text.lower())  # any alphabet (localized QGIS UI too)
    return {w for w in words if w not in _STOPWORDS}


def find_relevant_algorithms(prompt, limit=MAX_RELEVANT_ALGS):
    """Simple algorithm search by the keywords of the request."""
    tokens = _tokens(prompt)
    if not tokens:
        return []
    scored = []
    for alg in _all_algorithms():
        haystack = " ".join([alg.id(), alg.displayName(), alg.group(), " ".join(alg.tags())]).lower()
        score = sum(2 if t in alg.displayName().lower() else 1 for t in tokens if t in haystack)
        if alg.provider() is not None and alg.provider().id() not in BUILTIN_PROVIDERS:
            score += 0.5 if score else 0  # slightly prefer plugin algorithms
        if score:
            scored.append((score, alg))
    scored.sort(key=lambda x: -x[0])
    return [alg for _, alg in scored[:limit]]


def _is_optional(param):
    # QGIS 4: Qgis.ProcessingParameterFlag.Optional; QGIS 3: QgsProcessingParameterDefinition.FlagOptional
    flag_enum = getattr(Qgis, "ProcessingParameterFlag", None)
    flag = getattr(flag_enum, "Optional", None) or getattr(QgsProcessingParameterDefinition, "FlagOptional", None)
    if flag is None:
        return False
    try:
        return bool(param.flags() & flag)
    except TypeError:
        return False


def describe_algorithm(alg):
    """Algorithm parameter description (so the AI calls the algorithm correctly)."""
    lines = [f"{alg.id()} — {alg.displayName()}"]
    for p in alg.parameterDefinitions():
        optional = _is_optional(p)
        default = p.defaultValue()
        default_txt = f", default={default!r}" if default not in (None, "") else ""
        opt_txt = ", optional" if optional else ""
        extra = ""
        options = getattr(p, "options", None)
        if callable(options):
            try:
                opts = list(options())
                if opts:
                    extra = f", options={list(enumerate(opts))[:12]}"
            except Exception as e:
                log_warning(f"Cannot read options of {alg.id()}:{p.name()}: {e}")
        lines.append(f"    {p.name()} ({p.type()}{opt_txt}{default_txt}{extra}): {p.description()}")
    outs = [f"{o.name()} ({o.type()})" for o in alg.outputDefinitions()]
    if outs:
        lines.append(f"    outputs: {', '.join(outs)}")
    return "\n".join(lines)


def _static_plugins_context():
    """Installed plugins and their Processing algorithms (cached for STATIC_CONTEXT_TTL seconds)."""
    now = time.monotonic()
    if _static_cache["text"] and now - _static_cache["time"] < STATIC_CONTEXT_TTL:
        return _static_cache["text"]
    parts = []
    plugins = get_plugins_info()
    if plugins:
        act = [f"{p['name']} [{p['package']}]" for p in plugins if p["active"]]
        inact = [f"{p['name']} [{p['package']}]" for p in plugins if not p["active"]]
        parts.append("INSTALLED PLUGINS: " + ("; ".join(act) or "none"))
        if inact:
            parts.append("DISABLED PLUGINS: " + "; ".join(inact))

    providers = get_plugin_providers()
    if providers:
        lines = ["PLUGIN ALGORITHMS (run_alg):"]
        count = 0
        for prov in providers:
            if not prov["active"] or count >= MAX_PLUGIN_ALGS_IN_CONTEXT:
                continue
            ids = [alg_id for alg_id, _name in prov["algorithms"]][:MAX_PLUGIN_ALGS_IN_CONTEXT - count]
            count += len(ids)
            lines.append(f"  {prov['name']}: {', '.join(ids)}")
        if count >= MAX_PLUGIN_ALGS_IN_CONTEXT:
            lines.append("  ... more: find_alg('keyword')")
        parts.append("\n".join(lines))
    _static_cache.update(time=now, text="\n".join(parts))
    return _static_cache["text"]


def get_plugins_context(prompt=""):
    """Plugin context block for the AI prompt."""
    parts = [_static_plugins_context()]
    relevant = find_relevant_algorithms(prompt) if prompt else []
    if relevant:
        lines = ["POSSIBLY RELEVANT ALGORITHMS:"]
        for i, alg in enumerate(relevant):
            lines.append(describe_algorithm(alg) if i < MAX_DETAILED_ALGS else f"{alg.id()} — {alg.displayName()}")
        parts.append("\n".join(lines))

    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- internal helpers used by core/tools.py

def list_plugins(include_core=False):
    """Prints the installed plugins."""
    for p in get_plugins_info(include_core):
        state = "active" if p["active"] else "disabled"
        print(f"{p['package']:30} {state:9} {p['name']} {p['version']} — {p['description']}")


def get_plugin(package_name):
    """Returns the running plugin object (qgis.utils.plugins) or None."""
    plugin = qgis.utils.plugins.get(package_name)
    if plugin is None:
        print(f"Plugin '{package_name}' is not running. Installed: {sorted(qgis.utils.plugins)}")
    return plugin


def find_algorithms(keyword, provider=None):
    """Prints Processing algorithms containing the keyword (in id, name or tags)."""
    kw = keyword.lower()
    found = 0
    for alg in _all_algorithms():
        prov = alg.provider().id() if alg.provider() else ""
        if provider and prov != provider:
            continue
        hay = " ".join([alg.id(), alg.displayName(), " ".join(alg.tags())]).lower()
        if kw in hay:
            print(f"{alg.id():45} {alg.displayName()}")
            found += 1
    if not found:
        print(f"No algorithms found for '{keyword}'.")


def algorithm_help(algorithm_id):
    """Prints the parameters of a Processing algorithm."""
    alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if alg is None:
        print(f"Algorithm '{algorithm_id}' not found. Use find_alg('keyword').")
        return
    print(describe_algorithm(alg))
    helptext = alg.shortHelpString()
    if helptext:
        print("\n" + re.sub(r"<[^>]+>", "", helptext)[:1500])


def list_plugin_actions(keyword=""):
    """Prints plugin menu items (optionally filtered)."""
    for path, _ in _plugin_actions():
        if keyword.lower() in path.lower():
            print(path)


def trigger_plugin_action(path_or_text):
    """Triggers a plugin menu item: exact path 'Plugins > X > Y' or part of the name."""
    actions = _plugin_actions()
    query = path_or_text.lower().strip()
    match = next((a for p, a in actions if p.lower() == query), None)
    if match is None:
        candidates = [(p, a) for p, a in actions if query in p.lower()]
        if len(candidates) > 1:
            print("Several actions match, specify the full path:\n" + "\n".join(p for p, _ in candidates))
            return False
        match = candidates[0][1] if candidates else None
    if match is None:
        print(f"No plugin action matches '{path_or_text}'. Use find_action('keyword').")
        return False
    if not match.isEnabled():
        print(f"Action '{path_or_text}' is disabled right now (maybe a layer must be selected).")
        return False
    log_info(f"Triggering plugin action: {path_or_text}")
    match.trigger()
    return True


def enable_plugin(package_name):
    """Enables an installed but disabled plugin (after user confirmation)."""
    if package_name in qgis.utils.active_plugins:
        return True
    if package_name not in qgis.utils.available_plugins:
        print(f"Plugin '{package_name}' is not installed. Ask the user to install it "
              "(plugin(name) opens the plugin manager).")
        return False
    answer = QMessageBox.question(
        qgis.utils.iface.mainWindow() if qgis.utils.iface else None,
        "AI Copilot", f"The AI wants to enable the plugin '{_meta(package_name, 'name') or package_name}'.\n"
                      "Enable it?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        print("User declined enabling the plugin.")
        return False
    ok = qgis.utils.loadPlugin(package_name) and qgis.utils.startPlugin(package_name)
    if ok:
        _static_cache["time"] = 0.0
        QgsSettings().setValue(f"PythonPlugins/{package_name}", True)
        log_info(f"Plugin enabled: {package_name}")
    else:
        print(f"Failed to start plugin '{package_name}', see the Python error dialog / message log.")
    return bool(ok)


def open_plugin_manager():
    """Opens «Manage and Install Plugins» so the user can install the required plugin."""
    iface = qgis.utils.iface
    action = getattr(iface, "actionManagePlugins", lambda: None)() if iface else None
    if action is not None:
        action.trigger()
        return True
    print("Open Plugins > Manage and Install Plugins… manually.")
    return False
