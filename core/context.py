"""Collects the project context for the prompt. QGIS 4 compatible (no deprecated enums)."""
from qgis.core import Qgis, QgsProject, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes

from .log import log_warning

MAX_FIELDS = 15
MAX_LAYERS = 40
MAX_OTHER_NAMES = 30

# Which layers the AI gets in detail
SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"   # selected in the Layers panel
SCOPE_VISIBLE = "visible"     # checked (visible) in the Layers panel
SCOPE_CHOSEN = "chosen"       # a list chosen by the user in the plugin
SCOPES = [(SCOPE_ALL, "All layers"), (SCOPE_SELECTED, "Selected in Layers panel"),
          (SCOPE_VISIBLE, "Visible layers"), (SCOPE_CHOSEN, "Chosen list")]


def _iface():
    try:
        from qgis.utils import iface
        return iface
    except ImportError as e:
        log_warning(f"qgis.utils.iface is unavailable: {e}")
        return None


def _valid_layers():
    return [layer for layer in QgsProject.instance().mapLayers().values() if layer.isValid()]


def resolve_scope(scope, chosen_ids=()):
    """Returns (working layers or None for all, short note for the user)."""
    all_layers = _valid_layers()
    total = len(all_layers)
    if scope == SCOPE_ALL or total == 0:
        return None, f"AI sees all {total} layer(s) in detail"
    if scope == SCOPE_SELECTED:
        iface = _iface()
        view = iface.layerTreeView() if iface is not None else None
        layers = [lyr for lyr in (view.selectedLayers() if view is not None else []) if lyr.isValid()]
        empty_note = "nothing selected in the Layers panel"
    elif scope == SCOPE_VISIBLE:
        layers = [lyr for lyr in QgsProject.instance().layerTreeRoot().checkedLayers() if lyr.isValid()]
        empty_note = "no visible layers"
    else:
        by_id = {lyr.id(): lyr for lyr in all_layers}
        layers = [by_id[i] for i in chosen_ids if i in by_id]
        empty_note = "no layers chosen yet"
    if not layers:
        return None, f"{empty_note} — AI sees all {total} layer(s)"
    return layers, f"AI sees {len(layers)} of {total} layer(s) in detail, the rest by name only"


def _describe(layer):
    info = f"- '{layer.name()}'"
    if isinstance(layer, QgsVectorLayer):
        geom = QgsWkbTypes.displayString(layer.wkbType())
        fields = [f"{f.name()} ({f.typeName()})" for f in layer.fields()]
        more = f" … +{len(fields) - MAX_FIELDS}" if len(fields) > MAX_FIELDS else ""
        info += (f" [Vector: {geom}] features: {layer.featureCount()}, "
                 f"selected: {layer.selectedFeatureCount()}, fields: {fields[:MAX_FIELDS]}{more}")
    elif isinstance(layer, QgsRasterLayer):
        info += f" [Raster, bands: {layer.bandCount()}, size: {layer.width()}x{layer.height()}]"
    else:
        info += f" [{type(layer).__name__}]"
    return info + f", CRS: {layer.crs().authid()}"


def get_project_context(working_layers=None) -> str:
    """Project context. working_layers=None — all layers in detail; otherwise only the working layers
    in detail and the remaining layers by name only (saves tokens in large projects)."""
    project = QgsProject.instance()
    iface = _iface()
    lines = [f"QGIS version: {Qgis.version()}",
             f"Project CRS: {project.crs().authid() or 'not set'}"]

    if iface is not None:
        try:
            from .maps import describe_view
            lines.append(describe_view())
            active = iface.activeLayer()
            if active is not None:
                lines.append(f"Active layer: '{active.name()}'")
        except Exception as e:  # map context is optional, but log the reason
            log_warning(f"Could not read map canvas context: {e}")

    layers = _valid_layers()
    if not layers:
        lines.append("No layers currently loaded in the project.")
        return "\n".join(lines)

    if working_layers is None:
        lines.append("Open layers:")
        lines.extend(_describe(layer) for layer in layers[:MAX_LAYERS])
        if len(layers) > MAX_LAYERS:
            lines.append(f"... and {len(layers) - MAX_LAYERS} more layers")
        return "\n".join(lines)

    working_ids = {layer.id() for layer in working_layers}
    lines.append("WORKING LAYERS (chosen by the user; work with these unless the user names another layer):")
    lines.extend(_describe(layer) for layer in working_layers[:MAX_LAYERS])
    others = [layer.name() for layer in layers if layer.id() not in working_ids]
    if others:
        shown = ", ".join(f"'{name}'" for name in others[:MAX_OTHER_NAMES])
        more = f" (+{len(others) - MAX_OTHER_NAMES} more)" if len(others) > MAX_OTHER_NAMES else ""
        lines.append(f"Other layers in the project (names only): {shown}{more}")
    return "\n".join(lines)
