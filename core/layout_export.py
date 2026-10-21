"""
Print layouts and export to any format.

create_layout("Izmir cafes", layers=["OSM cafes (points)"])   — ready layout: map, title, legend,
                                                                scale bar, north arrow, footer (CRS, date, sources)
export_layout(layout, "map.pdf")        — PDF / PNG / JPG / TIFF / BMP / WEBP / SVG
export_layer("Roads", "roads.gpkg")     — GPKG, SHP, GeoJSON, KML, CSV, XLSX, ODS, DXF, GML, FlatGeobuf, GPX, Parquet…
                                          rasters: GeoTIFF, PNG, JPG, ASC, VRT…
export_all_layers(folder, "gpkg")       — all project layers at once
export_map_image("map.png")             — the current map as an image
export_project("project.qgz")           — the QGIS project
If no path is given, a «Save as» dialog opens.
"""
import datetime
import os

import qgis.utils
from qgis.core import (Qgis, QgsApplication, QgsCoordinateTransform, QgsLayoutExporter, QgsLayoutItemLabel,
                       QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemPage, QgsLayoutItemPicture,
                       QgsLayoutItemScaleBar, QgsLayoutPoint, QgsLayoutSize, QgsMapLayer, QgsMapRendererParallelJob,
                       QgsPrintLayout, QgsProject, QgsRasterLayer, QgsRectangle, QgsTextFormat, QgsVectorLayer)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QFont

from .compat import distance_km, distance_m, enum_value, layout_mm, metres_per_map_unit, set_label_text, set_rect_null
from .log import log_info
from .progress import register_output, report_status, run_processing
from .qgis_tools import get_layer

PAGE_SIZES = {"A0", "A1", "A2", "A3", "A4", "A5", "Letter", "Legal", "Tabloid"}

LAYOUT_FILTER = ("PDF (*.pdf);;PNG image (*.png);;JPEG image (*.jpg);;TIFF image (*.tif);;SVG (*.svg);;"
                 "All files (*.*)")
VECTOR_FILTER = ("GeoPackage (*.gpkg);;Shapefile (*.shp);;GeoJSON (*.geojson);;KML (*.kml);;CSV (*.csv);;"
                 "Excel (*.xlsx);;OpenDocument (*.ods);;DXF (*.dxf);;GML (*.gml);;FlatGeobuf (*.fgb);;GPX (*.gpx);;"
                 "GeoParquet (*.parquet);;All files (*.*)")
RASTER_FILTER = "GeoTIFF (*.tif);;PNG (*.png);;JPEG (*.jpg);;ASCII Grid (*.asc);;VRT (*.vrt);;All files (*.*)"
VECTOR_FORMATS = {"gpkg", "shp", "geojson", "json", "kml", "csv", "xlsx", "ods", "dxf", "gml", "fgb", "gpx",
                  "parquet", "sqlite", "tab", "mif"}


def _mm():
    return layout_mm()


def _point(x, y):
    return QgsLayoutPoint(x, y, _mm())


def _size(w, h):
    return QgsLayoutSize(w, h, _mm())


def _ensure_path(path, default_name, file_filter):
    if path:
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        return os.path.abspath(path)
    from ..ui.dialogs import ask_save_path
    chosen = ask_save_path(default_name, file_filter)
    if not chosen:
        raise RuntimeError("Export cancelled: no file chosen.")
    return chosen


def _north_arrow_svg():
    for base in QgsApplication.svgPaths():
        folder = os.path.join(base, "arrows")
        if not os.path.isdir(folder):
            continue
        candidates = sorted(f for f in os.listdir(folder) if f.lower().startswith("northarrow") and f.endswith(".svg"))
        if candidates:
            preferred = [f for f in candidates if f.startswith("NorthArrow_02")]
            return os.path.join(folder, (preferred or candidates)[0])
    return ""


def _text_format(size, bold=False):
    fmt = QgsTextFormat()
    font = QFont()
    font.setBold(bold)
    fmt.setFont(font)
    fmt.setSize(size)
    return fmt


def _add_label(layout, text, x, y, w, h, size, bold=False):
    label = QgsLayoutItemLabel(layout)
    label.setText(text)
    font = QFont()
    font.setBold(bold)
    font.setPointSizeF(size)
    set_label_text(label, _text_format(size, bold), font)
    layout.addLayoutItem(label)
    label.attemptMove(_point(x, y))
    label.attemptResize(_size(w, h))
    return label


def _set_legend_sync(legend_item, manual):
    # QGIS 4: setSyncMode; earlier builds: the deprecated setAutoUpdateModel
    sync = getattr(Qgis, "LegendSyncMode", None)
    if sync is not None and hasattr(legend_item, "setSyncMode"):
        legend_item.setSyncMode(sync.Manual if manual else sync.VisibleLayers)
    else:
        legend_item.setAutoUpdateModel(not manual)


def _resolve_layers(layers):
    if not layers:
        return []
    return [get_layer(item) if not isinstance(item, QgsMapLayer) else item for item in layers]


def _map_crs(layers):
    """Project CRS, or (when the project has none) the CRS of the first layer."""
    project = QgsProject.instance()
    if project.crs().isValid():
        return project.crs()
    candidates = list(layers or []) + [lyr for lyr in project.mapLayers().values() if lyr.isValid()]
    return next((lyr.crs() for lyr in candidates if lyr.crs().isValid()), project.crs())


def _extent_for(extent, layers, crs=None):
    project = QgsProject.instance()
    crs = crs if crs is not None else _map_crs(layers)
    if isinstance(extent, QgsRectangle):
        return QgsRectangle(extent)
    sources = []
    if isinstance(extent, (str, QgsMapLayer)):
        sources = [get_layer(extent)]
    elif extent is None and layers:
        sources = layers
    if sources:
        rect = QgsRectangle()
        set_rect_null(rect)
        for layer in sources:
            transform = QgsCoordinateTransform(layer.crs(), crs, project)
            rect.combineExtentWith(transform.transformBoundingBox(layer.extent()))
        rect.scale(1.05)
        return rect
    if qgis.utils.iface is not None:
        return QgsRectangle(qgis.utils.iface.mapCanvas().extent())
    all_layers = [lyr for lyr in project.mapLayers().values() if lyr.isValid()]  # no map window
    return _extent_for(None, all_layers, crs) if all_layers else QgsRectangle()


def create_layout(title, layers=None, extent=None, subtitle="", sources="", page="A4", orientation="landscape",
                  legend=True, scalebar=True, north_arrow=True, open_designer=True, name=None):
    """Creates a ready-to-print layout and (by default) opens it in the layout designer.

    layers   — list of layers or layer names for the map and legend (None = all visible layers).
    extent   — QgsRectangle, a layer/layer name, or None (extent of the given layers or the current map).
    sources  — data sources credit («© OpenStreetMap contributors, geoBoundaries»).
    page     — A0…A5, Letter, Legal, Tabloid;  orientation — 'landscape' | 'portrait'.
    """
    project = QgsProject.instance()
    report_status("Creating layout…", title)
    name = name or title
    manager = project.layoutManager()
    existing = manager.layoutByName(name)
    if existing is not None:
        manager.removeLayout(existing)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(name)
    portrait = enum_value(QgsLayoutItemPage, "Orientation", "Portrait")
    landscape = enum_value(QgsLayoutItemPage, "Orientation", "Landscape")
    orient = portrait if str(orientation).lower().startswith("p") else landscape
    page_name = page if page in PAGE_SIZES else "A4"
    page_item = layout.pageCollection().page(0)
    page_item.setPageSize(page_name, orient)
    page_w = page_item.pageSize().width()
    page_h = page_item.pageSize().height()

    margin = 10.0
    header_h = 22.0 if subtitle else 16.0
    footer_h = 8.0
    side_w = 55.0 if legend else 0.0
    map_x, map_y = margin, margin + header_h
    map_w = page_w - 2 * margin - (side_w + 5 if legend else 0)
    map_h = page_h - map_y - margin - footer_h

    layer_list = _resolve_layers(layers)

    # --- map
    map_item = QgsLayoutItemMap(layout)
    map_item.setRect(0, 0, 10, 10)
    layout.addLayoutItem(map_item)
    map_item.attemptMove(_point(map_x, map_y))
    map_item.attemptResize(_size(map_w, map_h))
    map_crs = _map_crs(layer_list)
    map_item.setCrs(map_crs)
    if layer_list:
        canvas = qgis.utils.iface.mapCanvas() if qgis.utils.iface is not None else None
        canvas_order = [lyr for lyr in canvas.layers() if lyr in layer_list] if canvas is not None else []
        ordered = canvas_order + [lyr for lyr in layer_list if lyr not in canvas_order]
        map_item.setLayers(ordered)
        map_item.setKeepLayerSet(True)
    map_item.zoomToExtent(_extent_for(extent, layer_list, map_crs))
    map_item.setFrameEnabled(True)

    # --- title and subtitle
    _add_label(layout, title, margin, margin, page_w - 2 * margin, 10, 20, bold=True)
    if subtitle:
        _add_label(layout, subtitle, margin, margin + 11, page_w - 2 * margin, 7, 11)

    # --- legend
    if legend:
        legend_item = QgsLayoutItemLegend(layout)
        legend_item.setTitle("Legend")
        legend_item.setLinkedMap(map_item)
        _set_legend_sync(legend_item, manual=bool(layer_list))
        if layer_list:
            root = legend_item.model().rootGroup()
            root.removeAllChildren()
            for lyr in layer_list:
                root.addLayer(lyr)
        legend_item.setResizeToContents(True)
        layout.addLayoutItem(legend_item)
        legend_item.attemptMove(_point(page_w - margin - side_w, map_y))

    # --- north arrow
    if north_arrow:
        svg = _north_arrow_svg()
        if svg:
            arrow = QgsLayoutItemPicture(layout)
            arrow.setPicturePath(svg)
            arrow.setLinkedMap(map_item)
            layout.addLayoutItem(arrow)
            arrow.attemptResize(_size(14, 14))
            arrow.attemptMove(_point(map_x + map_w - 18, map_y + 4))

    # --- scale bar
    if scalebar:
        bar = QgsLayoutItemScaleBar(layout)
        bar.setStyle("Single Box")
        bar.setLinkedMap(map_item)
        width_m = map_item.extent().width() * metres_per_map_unit(map_crs)
        use_km = width_m >= 5000  # smaller maps read better in metres
        bar.applyDefaultSize(distance_km() if use_km else distance_m())
        bar.setUnitLabel("km" if use_km else "m")
        layout.addLayoutItem(bar)
        bar.attemptMove(_point(map_x + 4, map_y + map_h - 16))

    # --- footer
    footer = f"CRS: {map_crs.authid() or map_crs.description()}  ·  Created: {datetime.date.today():%Y-%m-%d}"
    if sources:
        footer += f"  ·  Data: {sources}"
    _add_label(layout, footer, margin, page_h - margin - footer_h + 2, page_w - 2 * margin, 6, 8)

    manager.addLayout(layout)
    log_info(f"Layout created: {name}")
    orient_name = "portrait" if orient == portrait else "landscape"
    print(f"Layout '{name}' created ({page_name} {orient_name}, {len(layer_list) or 'all visible'} layer(s)).")
    if open_designer and qgis.utils.iface is not None:
        qgis.utils.iface.openLayoutDesigner(layout)
    return layout


def get_layout(name):
    layout = QgsProject.instance().layoutManager().layoutByName(name)
    if layout is None:
        names = [lay.name() for lay in QgsProject.instance().layoutManager().printLayouts()]
        raise ValueError(f"Layout '{name}' not found. Existing layouts: {names}")
    return layout


def export_layout(layout, path=None, dpi=300):
    """Exports a layout; format by extension (.pdf .png .jpg .jpeg .tif .tiff .bmp .webp .svg)."""
    layout = get_layout(layout) if isinstance(layout, str) else layout
    path = _ensure_path(path, f"{layout.name()}.pdf", LAYOUT_FILTER)
    ext = os.path.splitext(path)[1].lower()
    report_status("Exporting layout…", os.path.basename(path))
    exporter = QgsLayoutExporter(layout)
    if ext == ".pdf":
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.dpi = dpi
        result = exporter.exportToPdf(path, settings)
    elif ext == ".svg":
        settings = QgsLayoutExporter.SvgExportSettings()
        settings.dpi = dpi
        result = exporter.exportToSvg(path, settings)
    else:
        settings = QgsLayoutExporter.ImageExportSettings()
        settings.dpi = dpi
        result = exporter.exportToImage(path, settings)
    if result != enum_value(QgsLayoutExporter, "ExportResult", "Success"):
        raise RuntimeError(f"Layout export failed ({result}): {exporter.errorMessage() or path}")
    register_output(path)
    print(f"Layout exported → {path}")
    return path


def export_layer(layer, path=None, crs=None, options=None):
    """Exports a layer to a file; format by extension. Vector: .gpkg .shp .geojson .kml .csv .xlsx .ods .dxf .gml
    .fgb .gpx .parquet …; raster: .tif .png .jpg .asc .vrt …  crs — e.g. 'EPSG:4326' to reproject."""
    layer = get_layer(layer) if not isinstance(layer, QgsMapLayer) else layer
    is_raster = isinstance(layer, QgsRasterLayer)
    default = f"{layer.name()}.{'tif' if is_raster else 'gpkg'}"
    path = _ensure_path(path, default, RASTER_FILTER if is_raster else VECTOR_FILTER)
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    stage = f"Exporting {layer.name()} → {os.path.basename(path)}"

    if is_raster:
        params = {"INPUT": layer, "OUTPUT": path}
        if crs:
            params = {"INPUT": layer, "TARGET_CRS": crs, "OUTPUT": path}
            run_processing("gdal:warpreproject", params, stage=stage)
        else:
            run_processing("gdal:translate", params, stage=stage)
    else:
        if ext not in VECTOR_FORMATS:
            print(f"Note: '.{ext}' is not a common vector format; GDAL will try to guess the driver.")
        source = layer
        if crs:
            source = run_processing("native:reprojectlayer", {"INPUT": layer, "TARGET_CRS": crs,
                                                              "OUTPUT": "TEMPORARY_OUTPUT"},
                                    stage=f"Reprojecting {layer.name()} to {crs}")["OUTPUT"]
        params = {"INPUT": source, "OUTPUT": path}
        if ext == "csv" and isinstance(layer, QgsVectorLayer) and layer.isSpatial():
            params["LAYER_OPTIONS"] = options or "GEOMETRY=AS_WKT"
        elif options:
            params["LAYER_OPTIONS"] = options
        run_processing("native:savefeatures", params, stage=stage)
    register_output(path)
    print(f"Layer '{layer.name()}' exported → {path}")
    return path


def export_all_layers(folder=None, fmt="gpkg", raster_fmt="tif"):
    """Exports all project layers to a folder (vector — fmt, raster — raster_fmt). Online layers are skipped."""
    if folder is None:
        from ..ui.dialogs import ask_folder
        folder = ask_folder("Folder for exported layers")
        if not folder:
            raise RuntimeError("Export cancelled: no folder chosen.")
    os.makedirs(folder, exist_ok=True)
    done = []
    for layer in QgsProject.instance().mapLayers().values():
        provider = layer.providerType() if hasattr(layer, "providerType") else ""
        if provider in ("wms", "arcgismapserver", "wcs", "xyzvectortiles"):
            print(f"Skipped online layer: {layer.name()}")
            continue
        safe = "".join(c if c.isalnum() or c in " _-." else "_" for c in layer.name()).strip() or "layer"
        ext = raster_fmt if isinstance(layer, QgsRasterLayer) else fmt
        done.append(export_layer(layer, os.path.join(folder, f"{safe}.{ext}")))
    print(f"Exported {len(done)} layer(s) to {folder}")
    return done


def export_map_image(path=None, width=None, height=None, dpi=None):
    """Saves the current map as an image (PNG/JPG/TIF…). Default size — 2× the map canvas size."""
    canvas = qgis.utils.iface.mapCanvas()
    path = _ensure_path(path, "map.png", "PNG (*.png);;JPEG (*.jpg);;TIFF (*.tif);;All files (*.*)")
    settings = canvas.mapSettings()
    w = int(width or canvas.width() * 2)
    h = int(height or canvas.height() * 2)
    settings.setOutputSize(QSize(w, h))
    if dpi:
        settings.setOutputDpi(dpi)
    report_status("Rendering map image…", f"{w}×{h} px")
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    if not job.renderedImage().save(path):
        raise RuntimeError(f"Could not save the image to {path}")
    register_output(path)
    print(f"Map image ({w}×{h}) saved → {path}")
    return path


def export_project(path=None):
    """Saves the QGIS project (.qgz/.qgs)."""
    path = _ensure_path(path, "project.qgz", "QGIS project (*.qgz *.qgs)")
    if not QgsProject.instance().write(path):
        raise RuntimeError(f"Could not save the project: {QgsProject.instance().error()}")
    register_output(path)
    print(f"Project saved → {path}")
    return path
