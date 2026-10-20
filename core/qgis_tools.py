"""
Tools that make ALL QGIS functions reliably available:

run_alg(id, params)        — any Processing algorithm (1000+ in QGIS: native, gdal, grass, plugins).
                             Validates parameters against the real algorithm instead of the AI's memory,
                             accepts layers by name and adds the results to the map.
get_layer(name)            — layer by name (case-insensitive) with a clear error.
QGIS buttons and menus — internal functions for find_action / run_action (core/tools.py).
"""
import qgis.utils
from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsClassificationEqualInterval,
                       QgsClassificationJenks, QgsClassificationPrettyBreaks, QgsClassificationQuantile,
                       QgsGraduatedSymbolRenderer, QgsMapLayer, QgsPalLayerSettings, QgsProject, QgsRasterLayer,
                       QgsStyle, QgsSymbol, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)

from .log import log_info
from .progress import run_processing
from .plugins import _is_optional, _strip_amp, describe_algorithm

try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

LAYER_PARAM_TYPES = {"source", "vector", "raster", "layer", "mesh", "pointcloud", "vectortilelayer", "annotation"}
MULTI_LAYER_PARAM_TYPES = {"multilayer"}
LAYER_OUTPUT_TYPES = {"outputVector", "outputRaster", "outputLayer", "outputPointCloud", "outputVectorTile"}


# --------------------------------------------------------------------------- layers

def _find_layer(name_or_id):
    if isinstance(name_or_id, QgsMapLayer):
        return name_or_id
    project = QgsProject.instance()
    layer = project.mapLayer(str(name_or_id))
    if layer is not None:
        return layer
    matches = project.mapLayersByName(str(name_or_id))
    if not matches:
        wanted = str(name_or_id).strip().lower()
        matches = [lyr for lyr in project.mapLayers().values() if lyr.name().strip().lower() == wanted]
    return matches[0] if matches else None


def get_layer(name_or_id):
    """Layer by name or id. If not found — an error listing the existing layers."""
    layer = _find_layer(name_or_id)
    if layer is None:
        names = [lyr.name() for lyr in QgsProject.instance().mapLayers().values()]
        raise ValueError(f"Layer '{name_or_id}' not found. Existing layers: {names}")
    return layer


# --------------------------------------------------------------------------- algorithms

def _suggest_algorithms(alg_id, limit=8):
    part = alg_id.split(":")[-1].lower()
    found = []
    for alg in QgsApplication.processingRegistry().algorithms():
        if part and (part in alg.id().lower() or part in alg.displayName().lower()):
            found.append(alg.id())
    return found[:limit]


def _resolve_layer_values(alg, params):
    defs = {p.name(): p for p in alg.parameterDefinitions()}
    for name, value in list(params.items()):
        ptype = defs[name].type()
        if ptype in LAYER_PARAM_TYPES and isinstance(value, str):
            layer = _find_layer(value)
            if layer is not None:
                params[name] = layer
        elif ptype in MULTI_LAYER_PARAM_TYPES and isinstance(value, (list, tuple)):
            params[name] = [(_find_layer(v) or v) if isinstance(v, str) else v for v in value]


def _check_params(alg, params):
    defs = {p.name(): p for p in alg.parameterDefinitions()}
    unknown = [k for k in params if k not in defs]
    if unknown:
        raise ValueError(f"Unknown parameter(s) {unknown} for {alg.id()}.\nValid parameters:\n"
                         + describe_algorithm(alg))
    dest_names = {p.name() for p in alg.destinationParameterDefinitions()}
    missing = []
    for p in alg.parameterDefinitions():
        if p.name() in params or p.name() in dest_names or _is_optional(p):
            continue
        if p.defaultValue() is None:
            missing.append(p.name())
    if missing:
        raise ValueError(f"Missing required parameter(s) {missing} for {alg.id()}.\n" + describe_algorithm(alg))
    for p in alg.destinationParameterDefinitions():
        if p.name() not in params and not _is_optional(p):
            params[p.name()] = "TEMPORARY_OUTPUT"


def _load_output(value, out_def, title):
    project = QgsProject.instance()
    layer = None
    if isinstance(value, QgsMapLayer):
        layer = value
    elif isinstance(value, str) and value:
        existing = project.mapLayer(value)
        if existing is not None:
            return existing
        if out_def.type() == "outputRaster":
            layer = QgsRasterLayer(value, title)
        else:
            layer = QgsVectorLayer(value, title, "ogr")
    if layer is None or not layer.isValid():
        return None
    if project.mapLayer(layer.id()) is None:
        layer.setName(title)
        project.addMapLayer(layer)
    return layer


def run_alg(algorithm_id, params=None, load=True, name=None):
    """Runs ANY Processing algorithm with parameter validation and (by default) adds
    the output layers to the map. Layers can be passed by name: {"INPUT": "Roads"}.
    Returns the processing.run results dict (+ key "LAYERS" with the list of added layers)."""
    alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if alg is None:
        hint = _suggest_algorithms(algorithm_id)
        raise ValueError(f"Algorithm '{algorithm_id}' does not exist."
                         + (f" Similar: {hint}" if hint else " Use find_alg('keyword')."))
    params = dict(params or {})
    _check_params(alg, params)
    _resolve_layer_values(alg, params)

    log_info(f"run_alg {algorithm_id} {params}")
    result = run_processing(algorithm_id, params, stage=f"Running: {alg.displayName()}")

    added = []
    if load:
        layer_outputs = [o for o in alg.outputDefinitions() if o.type() in LAYER_OUTPUT_TYPES]
        for out in layer_outputs:
            title = name or alg.displayName()
            if len(layer_outputs) > 1:
                title = f"{title} ({out.description()})"
            layer = _load_output(result.get(out.name()), out, title)
            if layer is not None:
                added.append(layer)

    shown = {k: (v.name() if isinstance(v, QgsMapLayer) else v) for k, v in result.items()}
    print(f"Done: {alg.displayName()} [{algorithm_id}] → {shown}")
    for layer in added:
        count = layer.featureCount() if isinstance(layer, QgsVectorLayer) else ""
        print(f"  added layer: {layer.name()} {count}")
    result = dict(result)
    result["LAYERS"] = added
    return result


# --------------------------------------------------------------------------- QGIS buttons and menus

def _qgis_actions():
    iface = qgis.utils.iface
    if iface is None:
        return []
    seen, out = set(), []
    for action in iface.mainWindow().findChildren(QAction):
        if action.isSeparator() or action.menu() is not None:
            continue
        text = _strip_amp(action.text())
        if not text:
            continue
        key = (text, action.objectName())
        if key in seen:
            continue
        seen.add(key)
        out.append((text, action.objectName(), action))
    return out


def list_qgis_actions(keyword=""):
    """Prints QGIS buttons/menu items (text and objectName) containing the keyword."""
    kw = keyword.lower()
    count = 0
    for text, obj_name, action in _qgis_actions():
        if kw in text.lower() or kw in obj_name.lower():
            state = "" if action.isEnabled() else "  (disabled now)"
            print(f"{text:50} {obj_name}{state}")
            count += 1
    if not count:
        print(f"No QGIS actions match '{keyword}'.")


def trigger_qgis_action(text_or_object_name):
    """Triggers a QGIS button/menu item by objectName (mActionOpenTable) or by text."""
    query = text_or_object_name.strip().lower()
    actions = _qgis_actions()
    match = next((a for t, o, a in actions if o.lower() == query), None)
    match = match or next((a for t, o, a in actions if t.lower() == query), None)
    if match is None:
        candidates = [(t, o, a) for t, o, a in actions if query in t.lower() or query in o.lower()]
        if len(candidates) > 1:
            print("Several actions match, use the objectName:\n"
                  + "\n".join(f"{t:50} {o}" for t, o, _ in candidates[:20]))
            return False
        match = candidates[0][2] if candidates else None
    if match is None:
        print(f"No QGIS action matches '{text_or_object_name}'. Use find_action('keyword').")
        return False
    if not match.isEnabled():
        print(f"Action '{text_or_object_name}' is disabled right now (select a layer/feature first?).")
        return False
    log_info(f"Triggering QGIS action: {text_or_object_name}")
    match.trigger()
    return True


# --------------------------------------------------------------------------- styling

CLASS_METHODS = {
    "jenks": QgsClassificationJenks,
    "quantile": QgsClassificationQuantile,
    "equal": QgsClassificationEqualInterval,
    "pretty": QgsClassificationPrettyBreaks,
}


def _ramp(name):
    style = QgsStyle.defaultStyle()
    ramp = style.colorRamp(name)
    if ramp is None:
        available = style.colorRampNames()
        raise ValueError(f"Colour ramp '{name}' not found. Available: {available[:40]}")
    return ramp


def style_graduated(layer, field, classes=5, method="jenks", ramp="Viridis", invert=False):
    """Colours by a numeric field (population, density…). method: jenks | quantile | equal | pretty."""
    layer = get_layer(layer)
    if layer.fields().indexOf(field) < 0:
        raise ValueError(f"Field '{field}' not found in '{layer.name()}'. Fields: {[f.name() for f in layer.fields()]}")
    renderer = QgsGraduatedSymbolRenderer(field)
    renderer.setSourceSymbol(QgsSymbol.defaultSymbol(layer.geometryType()))
    renderer.setClassificationMethod(CLASS_METHODS.get(method.lower(), QgsClassificationJenks)())
    renderer.updateClasses(layer, int(classes))
    color_ramp = _ramp(ramp)
    if invert:
        color_ramp.invert()
    renderer.updateColorRamp(color_ramp)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    ranges = [f"{r.lowerValue():g}–{r.upperValue():g}" for r in renderer.ranges()]
    print(f"Styled '{layer.name()}' by {field} ({method}, {len(ranges)} classes): {ranges}")
    return renderer


def style_categorized(layer, field, ramp="Spectral"):
    """Colours by category (feature type, district, class…)."""
    layer = get_layer(layer)
    idx = layer.fields().indexOf(field)
    if idx < 0:
        raise ValueError(f"Field '{field}' not found in '{layer.name()}'. Fields: {[f.name() for f in layer.fields()]}")
    values = sorted(layer.uniqueValues(idx), key=lambda v: str(v))
    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    categories = QgsCategorizedSymbolRenderer.createCategories(values, symbol, layer, field)
    renderer = QgsCategorizedSymbolRenderer(field, categories)
    renderer.updateColorRamp(_ramp(ramp))
    layer.setRenderer(renderer)
    layer.triggerRepaint()
    print(f"Styled '{layer.name()}' by categories of {field}: {len(values)} categories")
    return renderer


def add_labels(layer, field_or_expression, size=9, is_expression=False):
    """Feature labels from a field or an expression."""
    layer = get_layer(layer)
    settings = QgsPalLayerSettings()
    settings.fieldName = field_or_expression
    settings.isExpression = bool(is_expression)
    fmt = QgsTextFormat()
    fmt.setSize(size)
    buffer = fmt.buffer()
    buffer.setEnabled(True)
    fmt.setBuffer(buffer)
    settings.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()
    print(f"Labels enabled on '{layer.name()}': {field_or_expression}")
