"""
Single command set for AI code (21 commands).

Processing:  run_alg, find_alg, get_layer, new_field, style
Map:         add_basemap, zoom_to, add_points
Interface:   find_action, run_action, plugin
Data:        download_osm, load, load_from_link, fetch, layer_from_records
Dialogs:     ask, choose, confirm
Output:      create_layout, export

Internet access (except OpenStreetMap) is available only after the user pastes a link
into the «Data link» field or into the request text.
"""
import json
import os
import re
import urllib.parse

import qgis.utils
from qgis.core import QgsApplication, QgsMapLayer, QgsPrintLayout, QgsProject

from . import layout_export as lx
from . import plugins as pl
from . import qgis_tools as qt
from . import web_data as wd
from .compat import new_field
from .maps import add_basemap, add_points, zoom_to
from .osm import download_osm
from .progress import report_status

DATA_EXT = (".csv", ".tsv", ".xlsx", ".xls", ".ods", ".json", ".geojson", ".zip", ".gpkg", ".kml", ".kmz",
            ".gml", ".shp", ".tif", ".tiff", ".fgb", ".parquet", ".xml", ".txt")
MAX_LINK_CANDIDATES = 40


# --------------------------------------------------------------------------- processing

def find_alg(text):
    """Exact algorithm id → prints its parameters; otherwise searches algorithms by keyword."""
    if QgsApplication.processingRegistry().algorithmById(text) is not None:
        pl.algorithm_help(text)
    else:
        pl.find_algorithms(text)


def style(layer, field=None, mode="auto", classes=5, method="jenks", ramp=None, labels=None, label_size=9):
    """Styles a layer with one command.
    mode: auto (number → graduated, text → categorized) | graduated | categorized.
    labels: field or expression for labels."""
    layer = qt.get_layer(layer)
    if field:
        if mode == "auto":
            idx = layer.fields().indexOf(field)
            if idx < 0:
                raise ValueError(f"Field '{field}' not found. Fields: {[f.name() for f in layer.fields()]}")
            mode = "graduated" if layer.fields().at(idx).isNumeric() else "categorized"
        if mode == "graduated":
            qt.style_graduated(layer, field, classes, method, ramp or "Viridis")
        else:
            qt.style_categorized(layer, field, ramp or "Spectral")
    if labels:
        is_expr = labels not in [f.name() for f in layer.fields()]
        qt.add_labels(layer, labels, label_size, is_expression=is_expr)
    return layer


# --------------------------------------------------------------------------- buttons, menus, plugins

def _all_actions():
    seen, out = set(), []
    for text, obj_name, action in qt._qgis_actions():
        out.append((text, obj_name, action))
        seen.add(id(action))
    for path, action in pl._plugin_actions():
        if id(action) not in seen:
            out.append((path, action.objectName(), action))
    return out


def find_action(keyword=""):
    """Prints QGIS and plugin buttons/menu items containing the keyword."""
    kw = keyword.lower()
    found = [(t, o, a) for t, o, a in _all_actions() if kw in t.lower() or kw in o.lower()]
    for text, obj_name, action in found[:60]:
        print(f"{text:55} {obj_name}{'' if action.isEnabled() else '  (disabled now)'}")
    if not found:
        print(f"No actions match '{keyword}'.")
    elif len(found) > 60:
        print(f"... {len(found) - 60} more, refine the keyword")


def run_action(name):
    """Triggers a button/menu item by objectName, text or menu path ('Plugins > QuickOSM > QuickOSM')."""
    query = name.strip().lower()
    actions = _all_actions()
    match = next((a for t, o, a in actions if o.lower() == query or t.lower() == query), None)
    if match is None:
        candidates = [(t, a) for t, o, a in actions if query in t.lower() or query in o.lower()]
        if len(candidates) > 1:
            raise ValueError("Several actions match, be more specific:\n" + "\n".join(t for t, _ in candidates[:15]))
        match = candidates[0][1] if candidates else None
    if match is None:
        raise ValueError(f"No action matches '{name}'. Use find_action('keyword').")
    if not match.isEnabled():
        raise RuntimeError(f"Action '{name}' is disabled right now (select a layer or feature first?).")
    report_status("Waiting for the opened window…", "If a dialog opened, finish or close it to continue")
    match.trigger()
    print(f"Triggered: {name}")
    return True


def plugin(name=None):
    """plugin() — list plugins. plugin("QuickOSM") — the plugin object; a disabled plugin is enabled
    (after confirmation), a missing one opens the plugin manager."""
    if not name:
        pl.list_plugins()
        return None
    wanted = name.strip().lower()
    package = next((p["package"] for p in pl.get_plugins_info(include_core=True)
                    if wanted in (p["package"].lower(), p["name"].lower())), None)
    if package is None:
        print(f"Plugin '{name}' is not installed. Opening the plugin manager so the user can install it.")
        pl.open_plugin_manager()
        return None
    if package not in qgis.utils.active_plugins and not pl.enable_plugin(package):
        return None
    return qgis.utils.plugins.get(package)


# --------------------------------------------------------------------------- data

def load(source=None, name=None, kind="auto", layer_name=""):
    """Adds a layer from any source:
    - None → local file dialog;
    - a file path (CSV, XLSX, GeoJSON, SHP, GPKG, KML, ZIP, GeoTIFF …);
    - a URL of a file or web service (XYZ, WMS, WFS, ArcGIS REST). WMS/WFS without layer_name — choose a layer."""
    if source is None or (isinstance(source, str) and os.path.exists(source)):
        return wd.load_file(source, name)
    url = str(source)
    low = url.lower()
    service = kind != "auto" or any(s in low for s in ("{z}", "featureserver", "mapserver", "service=wms",
                                                       "service=wfs", "/wms", "/wfs"))
    if not service:
        return wd.load_from_url(url, name)
    if (kind in ("wms", "auto") and ("wms" in low) and not layer_name):
        layers = wd.list_wms_layers(url)
        if not layers:
            raise ValueError("The WMS service has no named layers.")
        layer_name = layers[0] if len(layers) == 1 else ask("Which WMS layer to add?", layers)
        if not layer_name:
            return None
    return wd.add_web_layer(url, name or layer_name or "Web layer", kind, layer_name)


def fetch(url):
    """Downloads a website/API response: JSON → Python object, otherwise text. Prints the beginning."""
    text = wd.fetch_text(url, purpose="read data")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"--- {url} (text, {len(text)} chars) ---\n{text[:1500]}")
        return text
    preview = json.dumps(data, ensure_ascii=False)[:1500]
    print(f"--- {url} (JSON) ---\n{preview}")
    return data


def layer_from_records(records, name="Table", lon=None, lat=None, wkt=None):
    """List of dicts → layer. Coordinate and WKT fields are detected automatically if not given."""
    keys = {k.lower(): k for r in records[:50] if isinstance(r, dict) for k in r}
    lon = lon or next((keys[k] for k in wd.LON_NAMES if k in keys), None)
    lat = lat or next((keys[k] for k in wd.LAT_NAMES if k in keys), None)
    if wkt is None:
        wkt = next((keys[k] for k in ("wkt", "geometry", "geom", "coord", "coordinates", "location") if k in keys
                    and isinstance(records[0].get(keys[k]), str)
                    and re.match(r"^\s*(MULTI)?(POINT|LINESTRING|POLYGON)", records[0][keys[k]], re.I)), None)
    return wd.layer_from_records(records, name, lon or "lon", lat or "lat", wkt)


def _sample_from_file(path, rows=8):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".tsv", ".txt", ".json", ".geojson", ".xml", ".kml", ".gml"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readline() for _ in range(rows))
    return f"{os.path.basename(path)} — {os.path.getsize(path) / 1024:.1f} KB ({ext or 'binary'})"


def _records_in_json(data):
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        for value in data.values():
            found = _records_in_json(value) if isinstance(value, dict) else None
            if found:
                return found
    return None


def _ckan_resources(url):
    """CKAN dataset page (…/dataset/<id>) → dataset files through the CKAN API."""
    parsed = urllib.parse.urlparse(url)
    match = re.search(r"/dataset/([^/?#]+)", parsed.path)
    if not match:
        return None
    api = f"{parsed.scheme}://{parsed.netloc}/api/3/action/package_show?id={match.group(1)}"
    try:
        data = json.loads(wd.fetch_text(api, purpose="read dataset files"))
    except (ValueError, OSError, RuntimeError):
        return None
    result = data.get("result") or {}
    return [{"title": r.get("name") or "file", "url": r.get("url"), "format": (r.get("format") or "").upper(),
             "description": (r.get("description") or "")[:300], "source": result.get("title", "")}
            for r in result.get("resources", []) if r.get("url")]


def _html_data_links(html_text, base_url):
    title = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    page_title = re.sub(r"\s+", " ", title.group(1)).strip() if title else base_url
    items, seen = [], set()
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html_text, re.I | re.S):
        absolute = urllib.parse.urljoin(base_url, href.strip())
        path = urllib.parse.urlparse(absolute).path.lower()
        text = re.sub(r"<[^>]+>|\s+", " ", label).strip()
        is_data = path.endswith(DATA_EXT) or re.search(r"download|export|/api/|format=(csv|json|xlsx|geojson)",
                                                       absolute, re.I)
        if is_data and absolute not in seen and absolute.startswith("http"):
            seen.add(absolute)
            ext = os.path.splitext(path)[1].upper().lstrip(".")
            items.append({"title": text or os.path.basename(path) or absolute, "url": absolute,
                          "format": ext, "source": page_title})
    return items[:MAX_LINK_CANDIDATES]


def _load_one(url, name=None):
    """Downloads one resource, shows a sample, asks for confirmation and loads it."""
    path = wd.download_file(url, "ai_copilot_link" + wd._guess_suffix(url, ".dat"), purpose="load data")
    with open(path, "rb") as fh:
        head = fh.read(2048).lstrip()
    if head[:1] in (b"{", b"["):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("type") in ("FeatureCollection", "Feature"):
            if not confirm("GeoJSON data", url=url, sample=_sample_from_file(path)):
                return None
            os.replace(path, path + ".geojson")
            return wd.load_file(path + ".geojson", name)
        records = _records_in_json(data)
        if not records:
            print("The JSON has no table-like records. Structure:\n" + json.dumps(data, ensure_ascii=False)[:1500])
            return None
        if not confirm(f"Table with {len(records)} rows", url=url, sample=records[:10]):
            return None
        return layer_from_records(records, name or "Data from link")
    if head[:1] == b"<" and b"<html" in head.lower():
        return "html"
    if not confirm(name or os.path.basename(urllib.parse.urlparse(url).path) or "Data file",
                   url=url, sample=_sample_from_file(path)):
        return None
    suffix = wd._guess_suffix(url, "")
    if suffix and not path.endswith(suffix):
        os.replace(path, path + suffix)
        path += suffix
    return wd.load_file(path, name)


def load_from_link(url=None):
    """Main command for the user's link: opens the link, finds data on it (files, tables, APIs,
    CKAN datasets), lets the user choose, shows a sample, asks for confirmation and loads it.
    Returns the list of added layers."""
    url = url or wd.user_link()
    if not url:
        raise PermissionError("No data link. Ask the user to paste a link into the 'Data link' field.")
    low = url.lower()
    if any(s in low for s in ("{z}", "featureserver", "mapserver", "service=wms", "service=wfs")):
        return [load(url)]

    report_status("Reading the link…", url)
    candidates = _ckan_resources(url)
    if candidates is None:
        result = _load_one(url)
        if result != "html":
            return [result] if result is not None else []
        candidates = _html_data_links(wd.fetch_text(url, purpose="read page"), url)
    if not candidates:
        print("No downloadable data found on this page. Ask the user for a direct link to the file or API.")
        return []
    print(f"Found {len(candidates)} data link(s) on the page.")
    chosen = choose("Data found at the link", candidates)
    layers = []
    for item in chosen:
        layer = _load_one(item["url"], item.get("title"))
        if layer not in (None, "html"):
            layers.append(layer)
    print(f"Loaded {len(layers)} layer(s): {[lyr.name() for lyr in layers]}")
    return layers


# --------------------------------------------------------------------------- dialogs

def ask(question, options=None, default=""):
    from ..ui.dialogs import ask_user
    return ask_user(question, options, default)


def choose(title, items, message="", multi=True):
    from ..ui.dialogs import choose_data
    return choose_data(title, items, message, multi)


def confirm(title, description="", url="", sample=None, question="Use this data?"):
    from ..ui.dialogs import confirm_data
    return confirm_data(title, description, url, sample, question)


# --------------------------------------------------------------------------- output

def create_layout(title, layers=None, extent=None, subtitle="", sources="", page="A4", orientation="landscape",
                  legend=True, scalebar=True, north_arrow=True):
    return lx.create_layout(title, layers, extent, subtitle, sources, page, orientation,
                            legend, scalebar, north_arrow)


def export(what, path=None, **options):
    """Exports anything; format by the path extension, no path — «Save as» dialog.
    what: layout (object or name) → .pdf .png .jpg .tif .svg; layer (object or name) → .gpkg .shp .geojson .kml
    .csv .xlsx .ods .dxf .gml .fgb .gpx .parquet / raster .tif .png .asc; "map" → map image;
    "project" → .qgz; "all" → every layer to a folder (options: fmt="gpkg")."""
    if isinstance(what, QgsPrintLayout):
        return lx.export_layout(what, path, options.get("dpi", 300))
    if isinstance(what, QgsMapLayer):
        return lx.export_layer(what, path, options.get("crs"))
    key = str(what).strip()
    if key.lower() == "map":
        return lx.export_map_image(path, options.get("width"), options.get("height"))
    if key.lower() == "project":
        return lx.export_project(path)
    if key.lower() in ("all", "all_layers"):
        return lx.export_all_layers(path, options.get("fmt", "gpkg"))
    if QgsProject.instance().layoutManager().layoutByName(key) is not None:
        return lx.export_layout(key, path, options.get("dpi", 300))
    return lx.export_layer(qt.get_layer(key), path, options.get("crs"))


COMMANDS = {
    "run_alg": qt.run_alg, "find_alg": find_alg, "get_layer": qt.get_layer, "new_field": new_field, "style": style,
    "find_action": find_action, "run_action": run_action, "plugin": plugin,
    "add_basemap": add_basemap, "zoom_to": zoom_to, "add_points": add_points,
    "download_osm": download_osm, "load": load, "load_from_link": load_from_link, "fetch": fetch,
    "layer_from_records": layer_from_records,
    "ask": ask, "choose": choose, "confirm": confirm,
    "create_layout": create_layout, "export": export,
}
