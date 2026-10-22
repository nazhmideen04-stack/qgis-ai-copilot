"""
Internet data — ONLY through links given by the user (the «Data link» field or a link in the request).
Without a user link every internet request is blocked (OpenStreetMap is a separate built-in feature).
The link's website is allowed automatically; other websites (e.g. a file on another server) — after asking.
Downloads go through the QGIS network (QGIS proxy and SSL settings).
"""
import os
import re
import urllib.parse

from qgis.core import (QgsFeature, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingUtils, QgsProject,
                       QgsRasterLayer, QgsVectorLayer)
from qgis.PyQt.QtCore import QUrl

from .compat import new_field
from .progress import report_status, run_processing

S = "ai_copilot"

RASTER_EXT = {".tif", ".tiff", ".asc", ".nc", ".img", ".vrt", ".jp2", ".dem", ".hgt"}
TABLE_EXT = {".csv", ".tsv", ".txt"}
LON_NAMES = ("lon", "lng", "long", "longitude", "x", "lon_dd")
LAT_NAMES = ("lat", "latitude", "y", "lat_dd")

_user_links = []


def set_user_links(urls):
    """Links given by the user (set by the panel before every request)."""
    _user_links[:] = [u.strip() for u in urls if u and u.strip()]


def user_link():
    return _user_links[0] if _user_links else None


_page_cache = {}


def cached_page(url):
    """Content of an attached page that was already read (see link_explorer.analyse_page), or None."""
    return _page_cache.get(url)


def cache_page(url, info):
    _page_cache[url] = info


def clear_page_cache():
    """A new task reads the attached page again (it may have changed)."""
    _page_cache.clear()


_user_query = [""]


def set_user_query(text):
    """The user's request: its words are used to find the relevant data on the linked website."""
    _user_query[0] = str(text or "")


def user_query():
    return _user_query[0]


# --------------------------------------------------------------------------- permissions

def _domain(url):
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _plain_host(url):
    host = _domain(url)
    return host[4:] if host.startswith("www.") else host


def link_hosts():
    """Websites of the links the user attached (www. ignored). Only these may be contacted."""
    return {_plain_host(u) for u in _user_links if _plain_host(u)}


def _check_url(url, purpose=""):
    """Every download of the plugin passes here. With an attached link ONLY the website of that link is contacted:
    any other website is refused at once - no dialog, no remembered permissions."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http(s) URLs are allowed, got: {url[:80]}")
    if not _domain(url):
        raise ValueError(f"Invalid URL: {url[:80]}")
    if not _user_links:
        raise PermissionError("Internet access is only allowed for links the user provides. "
                              "Ask the user to paste a link into the 'Data link' field.")
    if _plain_host(url) not in link_hosts():
        raise PermissionError(f"Only the attached link is used. {_domain(url)} is another website: it is not "
                              "contacted. Use the data of the attached page.")


# --------------------------------------------------------------------------- download

def _guess_suffix(url, default=".dat"):
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".geojson", ".json", ".csv", ".tsv", ".zip", ".gpkg", ".kml", ".kmz", ".gml", ".shp",
                ".tif", ".tiff", ".xlsx", ".xls", ".ods", ".xml", ".txt", ".nc", ".asc", ".fgb", ".parquet"):
        if path.endswith(ext):
            return ext
    return default


def _downloaded_path(result, requested):
    """Path of the downloaded file. Some QGIS versions add an extension to the requested name
    (page.html -> page.html.html), so the path reported by the algorithm is used."""
    for path in ((result or {}).get("OUTPUT"), requested):
        if path and os.path.exists(path):
            return path
    raise RuntimeError(f"The download did not produce a file ({requested}).")


def download_file(url, filename=None, purpose=""):
    """Downloads a file (with the user's permission) to a temporary folder. Returns the path."""
    _check_url(url, purpose)
    name = filename or ("ai_copilot_download" + _guess_suffix(url))
    target = QgsProcessingUtils.generateTempFilename(name)
    result = run_processing("native:filedownloader", {"URL": url, "OUTPUT": target},
                            stage=f"Downloading from {_domain(url)}…")
    target = _downloaded_path(result, target)
    size = os.path.getsize(target)
    print(f"Downloaded {size / 1024:.1f} KB from {_domain(url)} → {target}")
    return target


def fetch_text(url, purpose="", max_chars=None):
    path = download_file(url, "ai_copilot_page.txt", purpose)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return text[:max_chars] if max_chars else text


# --------------------------------------------------------------------------- file → layer

def _local_uri(path):
    return QUrl.fromLocalFile(path).toString()


def _csv_layer(path, name, delimiter=None):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        header = fh.readline()
    if delimiter is None:
        delimiter = max([",", ";", "\t", "|"], key=header.count)
    cols = [c.strip().strip('"').lower() for c in header.split(delimiter)]
    original = [c.strip().strip('"') for c in header.split(delimiter)]
    params = {"type": "csv", "delimiter": "\\t" if delimiter == "\t" else delimiter, "detectTypes": "yes",
              "geomType": "none"}
    lon = next((original[i] for i, c in enumerate(cols) if c in LON_NAMES), None)
    lat = next((original[i] for i, c in enumerate(cols) if c in LAT_NAMES), None)
    if lon and lat:
        params.update({"xField": lon, "yField": lat, "crs": "EPSG:4326"})
        params.pop("geomType")
    # not URL-encoded: the delimited text provider expects ";" and "EPSG:4326" as they are
    uri = _local_uri(path) + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return QgsVectorLayer(uri, name, "delimitedtext")


def load_file(path=None, name=None, add=True):
    """Loads a local file as a layer (vector, raster or CSV/XLSX table).
    path=None — a file dialog opens (e.g. a table the user downloaded manually)."""
    if path is None:
        from ..ui.dialogs import ask_open_file
        path = ask_open_file("Data files (*.csv *.xlsx *.xls *.ods *.geojson *.json *.gpkg *.shp *.kml *.zip "
                             "*.tif *.tiff);;All files (*.*)")
        if not path:
            print("No file selected.")
            return None
    name = name or os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    report_status("Loading data…", os.path.basename(path))

    if ext in RASTER_EXT:
        layer = QgsRasterLayer(path, name)
    elif ext in TABLE_EXT:
        layer = _csv_layer(path, name)
    else:
        source = f"/vsizip/{path}" if ext in (".zip", ".kmz") else path
        layer = QgsVectorLayer(source, name, "ogr")
        if not layer.isValid():
            raster = QgsRasterLayer(source, name)
            layer = raster if raster.isValid() else layer
    if not layer.isValid():
        raise ValueError(f"Could not open '{path}' as a layer (format not recognised or file damaged).")
    if add:
        QgsProject.instance().addMapLayer(layer)
    count = layer.featureCount() if isinstance(layer, QgsVectorLayer) else f"{layer.width()}x{layer.height()} px"
    print(f"Loaded layer '{layer.name()}': {count}")
    return layer


def load_from_url(url, name=None, purpose="", add=True):
    """Downloads a file by URL and adds it as a layer (GeoJSON, CSV, ZIP Shapefile, GPKG, KML, GeoTIFF...)."""
    path = download_file(url, "ai_copilot_layer" + _guess_suffix(url, ".geojson"), purpose)
    base = name or os.path.splitext(os.path.basename(urllib.parse.urlparse(url).path))[0] or "Web data"
    return load_file(path, base, add)


# --------------------------------------------------------------------------- records → layer

def _field_for(name, values):
    present = [v for v in values if v not in (None, "")]
    if present and all(isinstance(v, bool) for v in present):
        kind = "bool"
    elif present and all(_is_int(v) for v in present):
        kind = "int"
    elif present and all(_is_float(v) for v in present):
        kind = "double"
    else:
        kind = "string"
    return new_field(str(name)[:60], kind)


def _is_int(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    try:
        int(str(v).strip())
        return True
    except (TypeError, ValueError):
        return False


def _is_float(v):
    try:
        float(str(v))
        return True
    except (TypeError, ValueError):
        return False


def _convert(value, field):
    if value in (None, ""):
        return None
    type_name = field.typeName().lower()
    try:
        if "int" in type_name:
            return int(float(str(value)))
        if "double" in type_name or "real" in type_name:
            return float(str(value))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, bool) else str(value)


def layer_from_records(records, name="Table", lon="lon", lat="lat", wkt=None, crs="EPSG:4326", add=True):
    """Creates a layer from a list of dicts (e.g. an API response).
    Geometry: lon/lat fields, or a wkt field (e.g. 'Point(27.1 38.4)'), otherwise a table without geometry."""
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        raise ValueError("layer_from_records: no records.")
    keys = []
    for r in records:
        for k in r:
            if k not in keys:
                keys.append(k)
    has_xy = lon in keys and lat in keys
    has_wkt = wkt is not None and wkt in keys
    if has_wkt:
        geom_type = QgsGeometry.fromWkt(str(next((r[wkt] for r in records if r.get(wkt)), ""))).type()
        kind = {0: "Point", 1: "LineString", 2: "Polygon"}.get(int(getattr(geom_type, "value", geom_type)), "Point")
        uri = f"Multi{kind}?crs={crs}" if kind != "Point" else f"Point?crs={crs}"
    elif has_xy:
        uri = f"Point?crs={crs}"
    else:
        uri = "None"
    layer = QgsVectorLayer(uri, name, "memory")
    fields = QgsFields()
    for key in keys:
        if has_wkt and key == wkt:
            continue
        fields.append(_field_for(key, [r.get(key) for r in records]))
    provider = layer.dataProvider()
    provider.addAttributes(list(fields))
    layer.updateFields()

    features = []
    for r in records:
        feat = QgsFeature(layer.fields())
        feat.setAttributes([_convert(r.get(f.name()), f) for f in layer.fields()])
        if has_wkt and r.get(wkt):
            feat.setGeometry(QgsGeometry.fromWkt(str(r[wkt])))
        elif has_xy and _is_float(r.get(lon)) and _is_float(r.get(lat)):
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(r[lon]), float(r[lat]))))
        features.append(feat)
    provider.addFeatures(features)
    layer.updateExtents()
    if add:
        QgsProject.instance().addMapLayer(layer)
    print(f"Created layer '{name}' with {len(features)} features, fields: {[f.name() for f in layer.fields()]}")
    return layer


# --------------------------------------------------------------------------- web services

def list_wms_layers(url):
    """Prints the layers of a WMS service (name and title) to choose which one to add."""
    sep = "&" if "?" in url else "?"
    text = fetch_text(f"{url}{sep}SERVICE=WMS&REQUEST=GetCapabilities", purpose="WMS capabilities")
    found = re.findall(r"<Layer[^>]*>\s*<Name>([^<]+)</Name>\s*<Title>([^<]*)</Title>", text)
    for layer_name, title in found:
        print(f"{layer_name:40} {title}")
    if not found:
        print("No named layers found (check the URL).")
    return [n for n, _ in found]


def add_web_layer(url, name="Web layer", kind="auto", layer_name="", crs="EPSG:3857"):
    """Adds an online layer: kind = 'xyz' | 'wms' | 'wfs' | 'arcgis_feature' | 'arcgis_map' | 'file' | 'auto'."""
    _check_url(url.replace("{", "").replace("}", ""), purpose=f"web layer {name}")
    low = url.lower()
    if kind == "auto":
        if "{z}" in low or "{x}" in low:
            kind = "xyz"
        elif "featureserver" in low:
            kind = "arcgis_feature"
        elif "mapserver" in low:
            kind = "arcgis_map"
        elif "service=wfs" in low or "/wfs" in low:
            kind = "wfs"
        elif "service=wms" in low or "/wms" in low:
            kind = "wms"
        else:
            kind = "file"

    if kind == "file":
        return load_from_url(url, name)
    if kind == "xyz":
        layer = QgsRasterLayer(f"type=xyz&url={urllib.parse.quote(url, safe=':/{}')}&zmin=0&zmax=19", name, "wms")
    elif kind == "wms":
        base = url.split("?")[0]
        layer = QgsRasterLayer(f"crs={crs}&format=image/png&layers={layer_name}&styles=&url={base}", name, "wms")
    elif kind == "wfs":
        base = url.split("?")[0]
        layer = QgsVectorLayer(f"url='{base}' typename='{layer_name}' version='auto'", name, "WFS")
    elif kind == "arcgis_feature":
        layer = QgsVectorLayer(f"url='{url}'", name, "arcgisfeatureserver")
    elif kind == "arcgis_map":
        layer = QgsRasterLayer(f"url='{url}'", name, "arcgismapserver")
    else:
        raise ValueError(f"Unknown web layer kind: {kind}")
    if not layer.isValid():
        raise ValueError(f"Could not open the {kind} service at {url[:120]}"
                         + (" — check layer_name (load() without it lists the layers)."
                            if kind in ("wms", "wfs") else ""))
    QgsProject.instance().addMapLayer(layer)
    print(f"Added {kind} layer '{name}'.")
    return layer
