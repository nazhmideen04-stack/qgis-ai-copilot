"""
Basemaps and coordinates.

add_basemap("Google Satellite")       — online basemap (no data link needed: tiles are only displayed)
zoom_to("38.4192, 27.1287")           — zoom to coordinates, a layer, a place name or a bounding box
add_points([(38.4192, 27.1287, "A")]) — point layer from coordinates

Coordinates are read the way people write them: "lat, lon" in WGS84 (EPSG:4326), decimal or
degrees-minutes-seconds with N/S/E/W. Swapped values (lon, lat) are detected when unambiguous.
Everything is transformed to the map CRS automatically.
"""
import re
import urllib.parse

import qgis.utils
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry,
                       QgsMapLayer, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from .progress import report_status

WGS84 = "EPSG:4326"

BASEMAPS = {
    "OpenStreetMap": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19),
    "Google Maps": ("https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}", 20),
    "Google Satellite": ("https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}", 20),
    "Google Hybrid": ("https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", 20),
    "Google Terrain": ("https://mt1.google.com/vt/lyrs=p&x={x}&y={y}&z={z}", 20),
    "Esri World Imagery": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/"
                           "{z}/{y}/{x}", 19),
    "Esri World Street Map": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/"
                              "{z}/{y}/{x}", 19),
    "Esri World Topo": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/"
                        "{z}/{y}/{x}", 19),
    "CartoDB Positron": ("https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png", 20),
    "CartoDB Dark Matter": ("https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", 20),
    "OpenTopoMap": ("https://a.tile.opentopomap.org/{z}/{x}/{y}.png", 17),
}
ALIASES = {
    "google": "Google Maps", "google map": "Google Maps", "google maps": "Google Maps", "google road": "Google Maps",
    "google satellite": "Google Satellite", "google sat": "Google Satellite", "google hybrid": "Google Hybrid",
    "google terrain": "Google Terrain", "osm": "OpenStreetMap", "openstreetmap": "OpenStreetMap",
    "satellite": "Esri World Imagery", "imagery": "Esri World Imagery", "esri": "Esri World Imagery",
    "esri satellite": "Esri World Imagery", "street": "Esri World Street Map", "topo": "OpenTopoMap",
    "topographic": "OpenTopoMap", "terrain": "Google Terrain", "hybrid": "Google Hybrid",
    "light": "CartoDB Positron", "positron": "CartoDB Positron", "carto": "CartoDB Positron",
    "dark": "CartoDB Dark Matter", "dark matter": "CartoDB Dark Matter",
}


# --------------------------------------------------------------------------- basemaps

def _resolve_basemap(name):
    key = re.sub(r"\s+", " ", str(name or "OpenStreetMap").strip().lower())
    for title in BASEMAPS:
        if key == title.lower():
            return title
    if key in ALIASES:
        return ALIASES[key]
    words = key.split()
    for title in BASEMAPS:
        if all(w in title.lower() for w in words):
            return title
    for alias in sorted(ALIASES, key=len, reverse=True):
        if alias in key:
            return ALIASES[alias]
    raise ValueError(f"Unknown basemap '{name}'. Available: {list(BASEMAPS)}")


def add_basemap(name="OpenStreetMap"):
    """Adds an online basemap at the bottom of the layer list. Names: OpenStreetMap, Google Maps,
    Google Satellite, Google Hybrid, Google Terrain, Esri World Imagery, Esri World Street Map, Esri World Topo,
    CartoDB Positron, CartoDB Dark Matter, OpenTopoMap (aliases like "google", "satellite", "dark" work too).
    Respect the tile provider's terms of use."""
    title = _resolve_basemap(name)
    url, zmax = BASEMAPS[title]
    project = QgsProject.instance()
    existing = project.mapLayersByName(title)
    if existing:
        print(f"Basemap '{title}' is already in the project.")
        return existing[0]
    uri = f"type=xyz&url={urllib.parse.quote(url, safe=':/{}')}&zmin=0&zmax={zmax}"
    layer = QgsRasterLayer(uri, title, "wms")
    if not layer.isValid():
        raise RuntimeError(f"Could not create the basemap '{title}'. Check the internet connection.")
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    root.insertLayer(len(root.children()), layer)  # bottom, so data layers stay on top
    print(f"Basemap added: {title}")
    return layer


# --------------------------------------------------------------------------- coordinates

_DMS = re.compile(r"(-?\d+(?:\.\d+)?)\s*[°º:\s]\s*(?:(\d+(?:\.\d+)?)\s*['′:\s]\s*)?(?:(\d+(?:\.\d+)?)\s*[\"″]?)?"
                  r"\s*([NSEWnsew])")


def _dms_to_decimal(deg, minutes, seconds, hemi):
    value = abs(float(deg)) + float(minutes or 0) / 60 + float(seconds or 0) / 3600
    return -value if hemi.upper() in "SW" or str(deg).startswith("-") else value


def parse_latlon(value):
    """Returns (lat, lon) from "38.42, 27.13", "38.42 27.13", (lat, lon), {"lat":..,"lon":..}
    or DMS like 38°25'09"N 27°07'43"E. A pair given as (lon, lat) is swapped when obvious."""
    if isinstance(value, QgsPointXY):
        return value.y(), value.x()
    if isinstance(value, dict):
        keys = {k.lower(): v for k, v in value.items()}
        lat = next((keys[k] for k in ("lat", "latitude", "y") if k in keys), None)
        lon = next((keys[k] for k in ("lon", "lng", "long", "longitude", "x") if k in keys), None)
        if lat is None or lon is None:
            raise ValueError(f"No lat/lon keys in {value}")
        return float(lat), float(lon)
    if isinstance(value, str):
        dms = _DMS.findall(value)
        if len(dms) >= 2:
            lat = lon = None
            for deg, minutes, seconds, hemi in dms[:2]:
                dec = _dms_to_decimal(deg, minutes, seconds, hemi)
                if hemi.upper() in "NS":
                    lat = dec
                else:
                    lon = dec
            if lat is not None and lon is not None:
                return lat, lon
        text = value if "." in value else re.sub(r"(\d),(\d)", r"\1.\2", value)  # "38,42 27,13" → decimals
        numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
        if len(numbers) < 2:
            raise ValueError(f"Could not read coordinates from '{value}'. Use 'lat, lon', e.g. '38.4192, 27.1287'.")
        a, b = float(numbers[0]), float(numbers[1])
    else:
        a, b = float(value[0]), float(value[1])
    if abs(a) > 90 >= abs(b):  # given as (lon, lat)
        a, b = b, a
    if abs(a) > 90 or abs(b) > 180:
        raise ValueError(f"({a}, {b}) are not valid WGS84 coordinates (lat ±90, lon ±180). "
                         "If they are projected (metres), pass crs='EPSG:xxxx' and x, y.")
    return a, b


def _canvas():
    return qgis.utils.iface.mapCanvas()


def _to_canvas(point_or_rect, src_crs=WGS84):
    canvas_crs = _canvas().mapSettings().destinationCrs()
    transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(src_crs), canvas_crs, QgsProject.instance())
    if isinstance(point_or_rect, QgsRectangle):
        return transform.transformBoundingBox(point_or_rect)
    return transform.transform(point_or_rect)


def zoom_to(target, scale=None):
    """Zooms the map to: coordinates ("lat, lon", (lat, lon), DMS), a layer or layer name,
    a bounding box (west, south, east, north) in WGS84, or a place name (looked up in OpenStreetMap).
    scale — map scale denominator for points (default 1:10 000)."""
    canvas = _canvas()
    if isinstance(target, QgsMapLayer) or (isinstance(target, str) and QgsProject.instance().mapLayersByName(target)):
        layer = target if isinstance(target, QgsMapLayer) else QgsProject.instance().mapLayersByName(target)[0]
        rect = _to_canvas(layer.extent(), layer.crs().authid())
        rect.scale(1.05)
        canvas.setExtent(rect)
        canvas.refresh()
        print(f"Zoomed to layer '{layer.name()}'.")
        return
    if isinstance(target, (list, tuple)) and len(target) == 4:
        west, south, east, north = (float(v) for v in target)
        canvas.setExtent(_to_canvas(QgsRectangle(west, south, east, north)))
        canvas.refresh()
        print(f"Zoomed to bbox W{west} S{south} E{east} N{north}.")
        return
    try:
        lat, lon = parse_latlon(target)
    except (ValueError, TypeError):
        if not isinstance(target, str):
            raise
        from .osm import geocode_place
        report_status("Looking up the place…", target)
        bbox, _area, full_name = geocode_place(target)
        canvas.setExtent(_to_canvas(bbox))
        canvas.refresh()
        print(f"Zoomed to {full_name}.")
        return
    canvas.setCenter(_to_canvas(QgsPointXY(lon, lat)))
    canvas.zoomScale(float(scale or 10000))
    canvas.refresh()
    print(f"Zoomed to lat {lat:.6f}, lon {lon:.6f} at 1:{int(canvas.scale())}.")


def add_points(points, name="Points", zoom=True):
    """Creates a point layer (WGS84) from coordinates: one item or a list of "lat, lon" strings,
    (lat, lon) / (lat, lon, "label") tuples or dicts with lat/lon (and optional label/name)."""
    if isinstance(points, (str, dict, QgsPointXY)) or (isinstance(points, (list, tuple)) and points
                                                       and isinstance(points[0], (int, float))):
        points = [points]
    layer = QgsVectorLayer(f"Point?crs={WGS84}", name, "memory")
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("label", QMetaType.Type.QString), QgsField("lat", QMetaType.Type.Double),
                            QgsField("lon", QMetaType.Type.Double)])
    layer.updateFields()
    features = []
    for i, item in enumerate(points, 1):
        label = f"Point {i}"
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("name") or label)
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            label, item = str(item[2]), item[:2]
        lat, lon = parse_latlon(item)
        feature = QgsFeature(layer.fields())
        feature.setAttributes([label, lat, lon])
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    print(f"Layer '{name}' with {len(features)} point(s): "
          + "; ".join(f"{f['label']} ({f['lat']:.6f}, {f['lon']:.6f})" for f in features[:10]))
    if zoom and features:
        if len(features) == 1:
            zoom_to((features[0]["lat"], features[0]["lon"]))
        else:
            zoom_to(layer)
    return layer


def describe_view():
    """Current map view in WGS84 (for the AI context): center, bounds and scale."""
    canvas = _canvas()
    crs = canvas.mapSettings().destinationCrs()
    transform = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(WGS84), QgsProject.instance())
    rect = transform.transformBoundingBox(canvas.extent())
    center = rect.center()
    return (f"Map view: center lat {center.y():.5f}, lon {center.x():.5f}; bounds lat {rect.yMinimum():.5f}"
            f"..{rect.yMaximum():.5f}, lon {rect.xMinimum():.5f}..{rect.xMaximum():.5f} (WGS84); "
            f"scale 1:{int(canvas.scale())}; map CRS {crs.authid()}")
