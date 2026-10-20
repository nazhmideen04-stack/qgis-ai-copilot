"""Сбор контекста проекта для промпта. Совместимо с QGIS 4 (без устаревших enum-ов)."""
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsWkbTypes

from .log import log_warning

MAX_FIELDS = 15
MAX_LAYERS = 40


def _iface():
    try:
        from qgis.utils import iface
        return iface
    except ImportError as e:
        log_warning(f"qgis.utils.iface is unavailable: {e}")
        return None


def get_project_context() -> str:
    project = QgsProject.instance()
    iface = _iface()
    lines = [f"Project CRS: {project.crs().authid() or 'not set'}"]

    if iface is not None:
        try:
            canvas = iface.mapCanvas()
            ext = canvas.extent()
            lines.append(
                f"Map canvas CRS: {canvas.mapSettings().destinationCrs().authid()}, "
                f"extent: {ext.xMinimum():.4f}, {ext.yMinimum():.4f}, {ext.xMaximum():.4f}, {ext.yMaximum():.4f}"
            )
            active = iface.activeLayer()
            if active is not None:
                lines.append(f"Active layer: '{active.name()}'")
        except Exception as e:  # контекст карты необязателен, но причину пишем в журнал
            log_warning(f"Could not read map canvas context: {e}")

    layers = [l for l in project.mapLayers().values() if l.isValid()]
    if not layers:
        lines.append("No layers currently loaded in the project.")
        return "\n".join(lines)

    lines.append("Open layers:")
    for layer in layers[:MAX_LAYERS]:
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
        info += f", CRS: {layer.crs().authid()}"
        lines.append(info)

    if len(layers) > MAX_LAYERS:
        lines.append(f"... and {len(layers) - MAX_LAYERS} more layers")
    return "\n".join(lines)
