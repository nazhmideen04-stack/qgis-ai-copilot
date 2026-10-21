"""
AI Copilot - reference demo "Terrain of Izmir in one minute" (TIN).

Runs the whole demo scenario WITHOUT the AI and prints OK / FAIL checks with real numbers. Use it to see what the
AI should produce and to verify that everything works on your QGIS version (3.28+ or 4).

How to run: open an EMPTY project, then Plugins > Python Console > Show Editor > Open Script (this file) > Run Script.
Requirements: the AI Copilot plugin installed and enabled. Internet is needed only to display the satellite basemap.
The heights are SYNTHETIC (a modelled hill around Kadifekale), not real measurements.
"""
import datetime
import importlib
import math
import os

import processing
import qgis.utils
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsFillSymbol, QgsGeometry, QgsGradientColorRamp, QgsGradientStop, QgsLayoutItemLegend,
                       QgsMarkerSymbol, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle,
                       QgsSingleBandPseudoColorRenderer, QgsVectorLayer)
from qgis.PyQt.QtGui import QColor, QPainter

CENTER_LAT, CENTER_LON = 38.4133, 27.1440   # Kadifekale hill, Izmir (WGS84)
HALF_SIZE = 1500                            # metres -> 3 x 3 km work area
N_POINTS = 150
CONTOUR_STEP = 10                           # metres
PIXEL_SIZE = 10                             # metres
WORK_CRS = "EPSG:32635"                     # UTM zone 35N, metres
OUT_DIR = os.path.join(os.path.expanduser("~"), "AI_Copilot_demo_tin",
                       datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S"))

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'OK  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def plugin_module(name):
    """A module of the installed AI Copilot plugin (works with any plugin folder name)."""
    for package in qgis.utils.active_plugins:
        if qgis.utils.pluginMetadata(package, "name") == "AI Copilot":
            return importlib.import_module(f"{package}.core.{name}")
    raise RuntimeError("Install and enable the AI Copilot plugin first.")


def first_algorithm(*ids):
    """Algorithm ids differ between QGIS versions: return the first one that exists."""
    registry = QgsApplication.processingRegistry()
    for algorithm_id in ids:
        if registry.algorithmById(algorithm_id) is not None:
            return algorithm_id
    raise RuntimeError(f"None of these algorithms is available: {ids}")


def halton(index, base):
    """Quasi-random number in [0, 1): evenly spread and identical on every run."""
    result, fraction = 0.0, 1.0
    while index > 0:
        fraction /= base
        result += fraction * (index % base)
        index //= base
    return result


def synthetic_height(x, y, cx, cy):
    """Metres. A main hill at Kadifekale, a smaller one to the south-east, a rise away from the bay and a little
    deterministic noise."""
    main = 165 * math.exp(-(math.hypot(x - cx, y - cy) / 650) ** 2)
    second = 55 * math.exp(-(math.hypot(x - (cx + 800), y - (cy - 700)) / 400) ** 2)
    rise = 0.008 * ((x - cx) - (y - cy))           # the bay lies to the north-west
    noise = 2.0 * math.sin(x / 37.0) * math.cos(y / 53.0)
    return round(max(0.0, 12 + main + second + rise + noise), 1)


# --------------------------------------------------------------------------- 1. map, place, work area
compat = plugin_module("compat")
maps = plugin_module("maps")
layout_export = plugin_module("layout_export")
qgis_tools = plugin_module("qgis_tools")
os.makedirs(OUT_DIR, exist_ok=True)

project = QgsProject.instance()
work_crs = QgsCoordinateReferenceSystem(WORK_CRS)
project.setCrs(work_crs)
to_utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), work_crs, project)
center = to_utm.transform(QgsPointXY(CENTER_LON, CENTER_LAT))
cx, cy = center.x(), center.y()
area_rect = QgsRectangle(cx - HALF_SIZE, cy - HALF_SIZE, cx + HALF_SIZE, cy + HALF_SIZE)

basemap = maps.add_basemap("Esri World Imagery")
check("satellite basemap added", basemap is not None and basemap.isValid(), basemap.name())

area_layer = QgsVectorLayer(f"Polygon?crs={WORK_CRS}", "Demo work area", "memory")
feature = QgsFeature()
feature.setGeometry(QgsGeometry.fromRect(area_rect))
area_layer.dataProvider().addFeatures([feature])
area_layer.updateExtents()
area_layer.renderer().setSymbol(QgsFillSymbol.createSimple(
    {"color": "0,0,0,0", "outline_color": "255,235,59,255", "outline_width": "0.6"}))
check("work area 3 x 3 km", abs(area_rect.width() - 2 * HALF_SIZE) < 0.01,
      f"{area_rect.width() / 1000:.1f} x {area_rect.height() / 1000:.1f} km, {WORK_CRS}")

# --------------------------------------------------------------------------- 2. points with heights
points_mem = QgsVectorLayer(f"Point?crs={WORK_CRS}", "Demo points", "memory")
points_mem.dataProvider().addAttributes([compat.new_field("id", "int"), compat.new_field("z", "double")])
points_mem.updateFields()
features = []
for i in range(1, N_POINTS + 1):
    x = area_rect.xMinimum() + halton(i, 2) * area_rect.width()
    y = area_rect.yMinimum() + halton(i, 3) * area_rect.height()
    f = QgsFeature(points_mem.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    f.setAttributes([i, synthetic_height(x, y, cx, cy)])
    features.append(f)
points_mem.dataProvider().addFeatures(features)
points_path = os.path.join(OUT_DIR, "points.gpkg")
processing.run("native:savefeatures", {"INPUT": points_mem, "OUTPUT": points_path})
points = QgsVectorLayer(points_path, "Demo points", "ogr")

heights = [f["z"] for f in points.getFeatures()]
inside = all(area_rect.contains(f.geometry().asPoint()) for f in points.getFeatures())
check(f"{N_POINTS} points inside the work area", points.featureCount() == N_POINTS and inside,
      f"{points.featureCount()} points")
check("every point has a height", all(h is not None for h in heights) and 0 <= min(heights) and max(heights) < 300,
      f"z {min(heights):.1f} - {max(heights):.1f} m")

# --------------------------------------------------------------------------- 3. TIN triangles and surface
tin_path = os.path.join(OUT_DIR, "tin_triangles.gpkg")
processing.run(first_algorithm("native:delaunaytriangulation", "qgis:delaunaytriangulation"),
               {"INPUT": points, "OUTPUT": tin_path})
triangles = QgsVectorLayer(tin_path, "Demo TIN triangles", "ogr")
hull = QgsGeometry.unaryUnion([f.geometry() for f in points.getFeatures()]).convexHull()
hull_vertices = len(hull.asPolygon()[0]) - 1
expected = 2 * N_POINTS - 2 - hull_vertices         # Delaunay: triangles = 2n - 2 - (points on the hull)
check("TIN triangles (2n - 2 - h rule)", triangles.featureCount() == expected,
      f"{triangles.featureCount()} triangles, expected {expected}")

z_index = points.fields().indexOf("z")
surface_path = os.path.join(OUT_DIR, "tin_surface.tif")
extent = f"{area_rect.xMinimum()},{area_rect.xMaximum()},{area_rect.yMinimum()},{area_rect.yMaximum()} [{WORK_CRS}]"
processing.run("qgis:tininterpolation", {
    "INTERPOLATION_DATA": f"{points_path}::~::0::~::{z_index}::~::0", "METHOD": 0,
    "EXTENT": extent, "PIXEL_SIZE": PIXEL_SIZE, "OUTPUT": surface_path})
surface = QgsRasterLayer(surface_path, "Demo terrain (TIN)")
provider = surface.dataProvider()
diffs = []
for f in points.getFeatures():
    value, ok = provider.sample(f.geometry().asPoint(), 1)
    if ok:
        diffs.append(abs(value - f["z"]))
# points on the outer edge of the TIN may fall into a pixel whose centre is already outside it (no value there)
check("TIN surface passes through the points",
      surface.isValid() and len(diffs) >= N_POINTS - hull_vertices and max(diffs) < 6,
      f"{surface.width()} x {surface.height()} px, {len(diffs)} points sampled, "
      f"max difference {max(diffs) if diffs else 0:.2f} m")
hull_box = hull.boundingBox()
samples, valid = 0, 0
for i in range(1, 401):
    p = QgsPointXY(hull_box.xMinimum() + halton(i, 5) * hull_box.width(),
                   hull_box.yMinimum() + halton(i, 7) * hull_box.height())
    if hull.contains(QgsGeometry.fromPointXY(p)):
        samples += 1
        valid += provider.sample(p, 1)[1]
check("no empty cells inside the TIN", samples > 0 and valid == samples, f"{valid}/{samples} samples")

# --------------------------------------------------------------------------- 4. contours and hillshade
contours_path = os.path.join(OUT_DIR, "contours.gpkg")
processing.run("gdal:contour", {"INPUT": surface_path, "BAND": 1, "INTERVAL": CONTOUR_STEP,
                                "FIELD_NAME": "ELEV", "OUTPUT": contours_path})
contours = QgsVectorLayer(contours_path, f"Demo contours ({CONTOUR_STEP} m)", "ogr")
levels = sorted({round(f["ELEV"], 3) for f in contours.getFeatures()})
check(f"contours every {CONTOUR_STEP} m", contours.featureCount() > 0 and all(v % CONTOUR_STEP == 0 for v in levels),
      f"{len(levels)} levels, {levels[0]:.0f} - {levels[-1]:.0f} m" if levels else "none")

hillshade_path = os.path.join(OUT_DIR, "hillshade.tif")
hillshade_id = first_algorithm("native:hillshade", "gdal:hillshade")
hillshade_params = {"INPUT": surface_path, "Z_FACTOR": 2, "AZIMUTH": 315, "OUTPUT": hillshade_path}
hillshade_params["V_ANGLE" if hillshade_id.startswith("native") else "ALTITUDE"] = 45
processing.run(hillshade_id, hillshade_params)
hillshade = QgsRasterLayer(hillshade_path, "Demo hillshade")
check("hillshade created", hillshade.isValid(), hillshade_id)

# --------------------------------------------------------------------------- 5. styling and map
stats = provider.bandStatistics(1)
ramp = QgsGradientColorRamp(QColor(56, 142, 60), QColor(121, 85, 72), False,
                            [QgsGradientStop(0.5, QColor(253, 216, 53))])  # green - yellow - brown
renderer = QgsSingleBandPseudoColorRenderer(provider, 1)
renderer.setClassificationMin(stats.minimumValue)
renderer.setClassificationMax(stats.maximumValue)
renderer.createShader(ramp)
surface.setRenderer(renderer)
surface.setOpacity(0.65)
hillshade.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)
hillshade.setOpacity(0.6)
triangles.renderer().setSymbol(QgsFillSymbol.createSimple(
    {"color": "0,0,0,0", "outline_color": "255,255,255,110", "outline_width": "0.12"}))
contours.renderer().symbol().setColor(QColor(62, 39, 35))
qgis_tools.add_labels(contours, "ELEV", size=7)
points.renderer().setSymbol(QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,87,34", "size": "1.4"}))
for layer in (surface, hillshade, contours, triangles, points, area_layer):
    project.addMapLayer(layer)

if qgis.utils.iface is not None:
    canvas = qgis.utils.iface.mapCanvas()
    canvas.setExtent(area_rect)
    canvas.refresh()

# --------------------------------------------------------------------------- 6. layout and PDF
layout = layout_export.create_layout(
    "Terrain of Izmir (demo)", layers=[points, triangles, contours, hillshade, surface, area_layer, basemap],
    extent=area_rect, subtitle=f"TIN from {N_POINTS} points around Kadifekale - synthetic heights",
    sources="Synthetic heights (AI Copilot demo)", open_designer=qgis.utils.iface is not None)
for item in layout.items():                         # the hillshade is a visual effect, not a legend entry
    if isinstance(item, QgsLayoutItemLegend):
        node = item.model().rootGroup().findLayer(hillshade.id())
        if node is not None:
            node.parent().removeChildNode(node)
pdf_path = layout_export.export_layout(layout, os.path.join(OUT_DIR, "terrain_demo.pdf"))
check("PDF map exported", os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 10000,
      f"{os.path.getsize(pdf_path) / 1024:.0f} KB")

print(f"\nRESULT: {sum(results)} of {len(results)} checks passed. Files: {OUT_DIR}")
