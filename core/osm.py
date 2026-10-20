"""
Download ANY OpenStreetMap data.

Examples:
    download_osm("amenity", "cafe")                      # cafes in the current extent
    download_osm("shop")                                 # all shops of any kind
    download_osm("amenity", "cafe|restaurant|bar")       # several values
    download_osm(tags=["building", "highway", "landuse=park"])  # several tags at once
    download_osm()                                       # ALL objects in the extent (small area only)
    download_osm("amenity", "hospital", place="Izmir")   # by city/district name (boundary from OSM)

Source:
- one tag in the current extent + QuickOSM installed → QuickOSM (verified parameters);
- anything else, or if QuickOSM fails → Overpass API directly through the QGIS network (respects the proxy).
"""
import json
import urllib.parse

import qgis.utils
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsProcessingUtils, QgsProject, QgsRectangle, QgsReferencedRectangle,
                       QgsVectorLayer)

from .log import log_info, log_warning
from .progress import check_cancel, report_status, run_processing

QUICKOSM_ALG = "quickosm:downloadosmdataextentquery"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
QUICKOSM_OUTPUTS = {
    "OUTPUT_POINTS": "points",
    "OUTPUT_LINES": "lines",
    "OUTPUT_MULTILINESTRINGS": "multilinestrings",
    "OUTPUT_MULTIPOLYGONS": "multipolygons",
}
OGR_OSM_LAYERS = ("points", "lines", "multilinestrings", "multipolygons", "other_relations")

# Area limits (in square degrees) — protection against Overpass and QGIS freezing
MAX_AREA_TAGGED = 6.0      # ~ a large region (e.g. a whole province) for specific tags
MAX_AREA_ALL = 0.004       # ~ 6×6 km when downloading ALL objects

# OSM guard: downloads happen only when the user asked for OSM data, or after the user confirms
_osm_requested = False
_osm_declined = False


def set_osm_request(requested):
    """Called by the panel for every new user task: did the request mention OSM / OpenStreetMap?"""
    global _osm_requested, _osm_declined
    _osm_requested = bool(requested)
    _osm_declined = False


def _check_osm_allowed(label, where):
    global _osm_requested, _osm_declined
    if _osm_requested:
        return
    if _osm_declined:
        raise PermissionError("The user declined downloading OpenStreetMap data for this task. "
                              "Do NOT call download_osm again; use the existing layers or ask the user.")
    from ..ui.dialogs import ask_yes_no
    if ask_yes_no("Download OpenStreetMap data?",
                  f"The AI wants to download OpenStreetMap data [{label}] in {where}.\n\n"
                  "Your request did not ask for OpenStreetMap data. Download it?"):
        _osm_requested = True
        return
    _osm_declined = True
    raise PermissionError("The user declined downloading OpenStreetMap data. "
                          "Do NOT call download_osm again; use the existing layers or ask the user.")


# --------------------------------------------------------------------------- tags

def _parse_tag(item):
    """'amenity' / 'amenity=cafe' / 'amenity=cafe|bar' / ('amenity', 'cafe') → (key, [values])."""
    if isinstance(item, (tuple, list)):
        key, value = (list(item) + [""])[:2]
    else:
        key, _, value = str(item).partition("=")
    key = key.strip()
    value = (value or "").strip()
    values = [] if value in ("", "*") else [v.strip() for v in value.split("|") if v.strip()]
    if not key:
        raise ValueError(f"Invalid OSM tag: {item!r}")
    return key, values


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _overpass_filter(key, values):
    if not values:
        return f'["{_escape(key)}"]'
    if len(values) == 1:
        return f'["{_escape(key)}"="{_escape(values[0])}"]'
    pattern = "|".join(_escape(v).replace(".", "\\\\.") for v in values)
    return f'["{_escape(key)}"~"^({pattern})$"]'


def _tags_label(tags):
    parts = []
    for key, values in tags:
        parts.append(f"{key}={'|'.join(values)}" if values else key)
    return ", ".join(parts) if parts else "all objects"


# --------------------------------------------------------------------------- area

def _current_extent():
    canvas = qgis.utils.iface.mapCanvas()
    return QgsReferencedRectangle(canvas.extent(), canvas.mapSettings().destinationCrs())


def _to_wgs84(extent):
    crs = extent.crs() if hasattr(extent, "crs") else QgsProject.instance().crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs.isValid() and crs != wgs84:
        transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
        return transform.transformBoundingBox(QgsRectangle(extent))
    return QgsRectangle(extent)


def _download(url, suffix, stage="Downloading…"):
    target = QgsProcessingUtils.generateTempFilename(f"ai_copilot_{suffix}")
    run_processing("native:filedownloader", {"URL": url, "OUTPUT": target}, stage=stage)
    return target


def geocode_place(place):
    """Finds a place in OSM (Nominatim). Returns (WGS84 bbox, overpass area id or None, full name)."""
    query = urllib.parse.urlencode({"q": place, "format": "jsonv2", "limit": 1, "polygon": 0})
    report_status(f"Searching place “{place}”…", "OpenStreetMap Nominatim")
    path = _download(f"{NOMINATIM_URL}?{query}", "place.json", stage=f"Searching place “{place}”…")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not data:
        raise ValueError(f"Place '{place}' not found in OpenStreetMap. Try a more precise name, e.g. 'Konak, Izmir'.")
    hit = data[0]
    south, north, west, east = (float(v) for v in hit["boundingbox"])
    bbox = QgsRectangle(west, south, east, north)
    area_id = None
    osm_id = int(hit.get("osm_id", 0))
    if hit.get("osm_type") == "relation":
        area_id = 3600000000 + osm_id
    elif hit.get("osm_type") == "way":
        area_id = 2400000000 + osm_id
    return bbox, area_id, hit.get("display_name", place)


# --------------------------------------------------------------------------- download

def _layer_name(base, kind):
    return f"{base} ({kind})"


def _add_non_empty(layers):
    added = []
    for layer in layers:
        check_cancel()
        if layer is not None and layer.isValid() and layer.featureCount() > 0:
            QgsProject.instance().addMapLayer(layer)
            added.append(layer)
    return added


def _via_quickosm(key, value, extent, timeout, base_name):
    params = {"KEY": key, "VALUE": value, "EXTENT": extent, "TIMEOUT": int(timeout)}
    result = run_processing(QUICKOSM_ALG, params, stage=f"Downloading OSM via QuickOSM: {base_name}")
    layers = []
    for out_name, kind in QUICKOSM_OUTPUTS.items():
        layer = result.get(out_name)
        if isinstance(layer, str):
            layer = QgsVectorLayer(layer, out_name, "ogr")
        if isinstance(layer, QgsVectorLayer):
            layer.setName(_layer_name(base_name, kind))
            layers.append(layer)
    return _add_non_empty(layers)


def build_overpass_query(tags, bbox=None, area_id=None, timeout=60):
    """Builds an Overpass QL query (print() it to check)."""
    if area_id:
        header, loc = f"area(id:{area_id})->.searchArea;", "(area.searchArea)"
    else:
        loc = f"({bbox.yMinimum():.7f},{bbox.xMinimum():.7f},{bbox.yMaximum():.7f},{bbox.xMaximum():.7f})"
        header = ""
    body = "".join(f"nwr{_overpass_filter(k, v)}{loc};" for k, v in tags) if tags else f"nwr{loc};"
    return f"[out:xml][timeout:{int(timeout)}][maxsize:536870912];{header}({body});(._;>;);out body;"


def _via_overpass(tags, bbox, area_id, timeout, base_name):
    query = build_overpass_query(tags, bbox, area_id, timeout)
    log_info(f"Overpass query: {query}")
    target = _download(f"{OVERPASS_URL}?data={urllib.parse.quote(query)}", "osm.osm",
                       stage=f"Downloading OSM from Overpass: {base_name}")
    check_cancel()
    report_status("Reading downloaded OSM data…", target)
    layers = [QgsVectorLayer(f"{target}|layername={kind}", _layer_name(base_name, kind), "ogr")
              for kind in OGR_OSM_LAYERS]
    if not any(layer.isValid() for layer in layers):
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(500)
        raise RuntimeError(f"Overpass returned no valid OSM data. Server response: {head}")
    return _add_non_empty(layers)


def download_osm(key="", value="", tags=None, extent=None, place=None, timeout=None,
                 layer_name=None, use_quickosm=True, max_area=None):
    """Downloads OSM data and adds the non-empty layers (points, lines, polygons) to the project.

    key/value  — one tag: download_osm("shop"), download_osm("amenity", "cafe|bar").
    tags       — several tags: ["building", "highway=primary|secondary", ("amenity", "school")].
    no tags    — ALL objects (small area only).
    extent     — QgsReferencedRectangle/QgsRectangle (project CRS) or a layer; default: the map extent.
    place      — place name ("Izmir", "Konak, Izmir"): download inside its boundary instead of the extent.
    Returns the list of added layers.
    """
    parsed = [_parse_tag(t) for t in (tags or [])]
    if key:
        parsed.insert(0, _parse_tag((key, value)))
    _check_osm_allowed(_tags_label(parsed), place or "the current map extent")
    download_all = not parsed
    timeout = timeout or (180 if download_all or place else 90)

    area_id = None
    where = "current map extent"
    if place:
        bbox, area_id, full_name = geocode_place(place)
        where = full_name
        print(f"Place found: {full_name}")
    else:
        if extent is None:
            extent = _current_extent()
        else:
            where = "given extent"
        if hasattr(extent, "extent") and hasattr(extent, "crs") and callable(extent.extent):
            extent = QgsReferencedRectangle(extent.extent(), extent.crs())  # a layer was passed
        bbox = _to_wgs84(extent)

    limit = max_area or (MAX_AREA_ALL if download_all else MAX_AREA_TAGGED)
    size = bbox.width() * bbox.height()
    if size > limit:
        what = "ALL objects" if download_all else "these tags"
        raise ValueError(
            f"The area is too large to download {what} (about {size:.4f} sq. degrees, limit {limit}). "
            "Zoom in, choose a smaller place, or pass specific tags (or max_area=... at your own risk).")

    base_name = layer_name or f"OSM {_tags_label(parsed)}"
    single_simple = len(parsed) == 1 and len(parsed[0][1]) <= 1 and not place
    has_quickosm = QgsApplication.processingRegistry().algorithmById(QUICKOSM_ALG) is not None

    source = "Overpass API"
    if use_quickosm and has_quickosm and single_simple:
        k, vals = parsed[0]
        try:
            added = _via_quickosm(k, vals[0] if vals else "", extent, timeout, base_name)
            source = "QuickOSM"
        except Exception as e:
            log_warning(f"QuickOSM failed, falling back to Overpass API: {e}")
            print(f"QuickOSM failed ({e}), trying Overpass API directly…")
            added = _via_overpass(parsed, bbox, None, timeout, base_name)
    else:
        added = _via_overpass(parsed, bbox, area_id, timeout, base_name)

    label = _tags_label(parsed)
    if added:
        summary = ", ".join(f"{layer.name()}: {layer.featureCount()}" for layer in added)
        print(f"Downloaded OSM [{label}] in {where} via {source}: {summary}")
        log_info(f"download_osm [{label}] in {where} via {source}: {summary}")
    else:
        print(f"No OSM objects [{label}] found in {where} (via {source}).")
    return added
