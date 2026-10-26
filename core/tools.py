"""
Single command set for AI code (25 commands).

Processing:  run_alg, find_alg, get_layer, new_field, style
Map:         add_basemap, zoom_to, add_points
Interface:   find_action, run_action, plugin
Data:        download_osm, load, load_from_link, explore_link, read_page, page_table, page_text, fetch,
             layer_from_records
Dialogs:     ask, choose, confirm
Output:      create_layout, export
Tables:      field_info, smart_join, join_keys
Heavy work:  run_in_background

Internet access (except OpenStreetMap) is available only after the user pastes a link
into the «Data link» field or into the request text.
"""
import json
import os
import re
import urllib.parse
import zipfile

import qgis.utils
from qgis.core import QgsApplication, QgsMapLayer, QgsPrintLayout, QgsProject

from . import layout_export as lx
from . import link_explorer as explorer
from . import plugins as pl
from . import qgis_tools as qt
from . import web_data as wd
from .compat import new_field
from .maps import add_basemap, add_points, zoom_to
from .osm import download_osm
from .field_hints import field_info
from .progress import report_status, run_in_background
from .smart_join import smart_join, suggest_join, describe_suggestions


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
    """Every QGIS and plugin button/menu item. One action can appear under several names (menu path, toolbar path,
    short text), so the exact path the AI got from the context always matches."""
    seen, out = set(), []

    def add(name, action):
        key = (name.lower(), id(action))
        if name and key not in seen:
            seen.add(key)
            out.append((name, action.objectName(), action))

    for path, action in pl._plugin_actions() + pl._toolbar_actions():
        add(path, action)
    for text, _obj_name, action in qt._qgis_actions():
        add(text, action)
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
        candidates, ids = [], set()
        for t, o, a in actions:
            if (query in t.lower() or query in o.lower()) and id(a) not in ids:  # same action, other name
                ids.add(id(a))
                candidates.append((t, a))
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


def _find_plugin(name):
    """Plugin entry by package or title; tolerant to case, spaces and partial names."""
    catalog = pl.plugin_catalog(include_core=True)
    wanted = pl._norm(name)
    exact = [p for p in catalog if wanted in (pl._norm(p["package"]), pl._norm(p["name"]))]
    if exact:
        return exact[0], catalog
    partial = [p for p in catalog if wanted and (wanted in pl._norm(p["package"]) or wanted in pl._norm(p["name"]))]
    if len(partial) == 1:
        return partial[0], catalog
    if len(partial) > 1:
        raise ValueError(f"Several plugins match '{name}': {[p['name'] for p in partial]}. Use the exact name.")
    return None, catalog


def _print_plugin_info(entry, obj):
    print(f"{entry['name']} [{entry['package']}] {entry.get('version', '')} — {entry.get('description', '')}")
    if entry["algorithms"]:
        print(f"Processing algorithms ({len(entry['algorithms'])}) — use run_alg, see parameters with find_alg(id):")
        for alg_id, title in entry["algorithms"][:40]:
            print(f"  {alg_id:45} {title}")
    if entry["actions"]:
        print(f"Menu / toolbar actions ({len(entry['actions'])}) — use run_action('<path>'):")
        for path, action in entry["actions"][:40]:
            print(f"  {path}{'' if action.isEnabled() else '  (disabled now)'}")
    if obj is not None:
        members = [m for m in dir(obj) if not m.startswith("_")]
        methods = [m for m in members if callable(getattr(obj, m, None))]
        attrs = [m for m in members if m not in methods]
        print(f"Python object: {type(obj).__module__}.{type(obj).__name__}")
        if methods:
            print("  methods: " + ", ".join(methods[:40]))
        if attrs:
            print("  attributes: " + ", ".join(attrs[:40]))
    if not entry["algorithms"] and not entry["actions"]:
        print("No Processing algorithms or menu items found for this plugin.")


def plugin(name=None, info=True):
    """plugin() — list plugins with what can be used from each.
    plugin("QuickOSM") — prints the plugin's algorithms, menu/toolbar actions and Python API, returns the plugin
    object. A disabled plugin is enabled after the user confirms; a missing one opens the plugin manager."""
    if not name:
        for p in pl.plugin_catalog():
            state = "active" if p["active"] else "disabled"
            print(f"{p['name']} [{p['package']}] {state}: {len(p['algorithms'])} algorithm(s), "
                  f"{len(p['actions'])} action(s)")
        return None
    entry, _catalog = _find_plugin(name)
    if entry is None:
        print(f"Plugin '{name}' is not installed. Opening the plugin manager so the user can install it.")
        pl.open_plugin_manager()
        return None
    package = entry["package"]
    if package not in qgis.utils.active_plugins:
        if not pl.enable_plugin(package):
            return None
        entry, _catalog = _find_plugin(package)  # refresh: algorithms and menus appear after start
    obj = qgis.utils.plugins.get(package)
    if info:
        _print_plugin_info(entry, obj)
    return obj


# --------------------------------------------------------------------------- data

def load(source=None, name=None, kind="auto", layer_name=""):
    """Adds a layer from any source:
    - None → local file dialog;
    - a file path (CSV, XLSX, GeoJSON, SHP, GPKG, KML, ZIP, GeoTIFF …);
    - a URL of a file or web service (XYZ, WMS, WFS, ArcGIS REST). WMS/WFS without layer_name — choose a layer."""
    if source is None or (isinstance(source, str) and os.path.exists(source)):
        return wd.load_file(source, name)
    url = str(source)
    if wd.user_link():
        url = _allowed_link(url)  # with an attached link: only that page and the files it links to
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
    if wd.user_link():
        url = _allowed_link(url)  # with an attached link: only that page and the files it links to
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


def _sniff_suffix(path, url):
    """File type of a download: from the URL, otherwise from the file content (download links often have none)."""
    suffix = wd._guess_suffix(url, "")
    if suffix:
        return suffix
    with open(path, "rb") as fh:
        head = fh.read(4096)
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            return ".zip"
        if any(n.startswith("xl/") for n in names):
            return ".xlsx"
        return ".ods" if "content.xml" in names and "mimetype" in names else ".zip"
    if head.startswith(b"SQLite format 3"):
        return ".gpkg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    text = head.lstrip()
    if text[:1] in (b"{", b"["):
        return ".json"
    low = text.lower()
    if b"<kml" in low:
        return ".kml"
    if b"<gml" in low or b"wfs:featurecollection" in low:
        return ".gml"
    first_lines = head.decode("utf-8", "replace").splitlines()[:5]
    if len(first_lines) >= 2 and any(sep in first_lines[0] for sep in (",", ";", "\t", "|")):
        return ".csv"
    return ".dat"


def _load_one(url, name=None, _hops=0):
    """Downloads one resource, shows a sample, asks for confirmation and loads it. If the link opens a page that
    only shows the file (code hosting, data portals), the page's Raw / Download link is followed once."""
    name = name or os.path.splitext(os.path.basename(urllib.parse.urlparse(url).path))[0] or "Data from link"
    path = wd.download_file(url, "ai_copilot_link.dat", purpose="load data")
    with open(path, "rb") as fh:
        head = fh.read(4096)
    if b"<html" in head.lower() or head.lstrip().lower().startswith(b"<!doctype html"):
        with open(path, "rb") as fh:
            page = explorer._decode(fh.read())
        target = explorer.raw_file_link(page, url) if _hops == 0 else None
        if target:
            print(f"The link opens a page; following its file link: {target}")
            return _load_one(target, name, _hops + 1)
        print(f"Skipped {url}: it is a web page, not a data file.")
        return None
    suffix = _sniff_suffix(path, url)
    if suffix != ".dat" and not path.endswith(suffix):
        os.replace(path, path + suffix)
        path += suffix
    if suffix in (".json", ".geojson"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("type") in ("FeatureCollection", "Feature"):
            if not confirm(name or "GeoJSON data", url=url, sample=_sample_from_file(path)):
                return None
            if not path.endswith(".geojson"):
                os.replace(path, path + ".geojson")
                path += ".geojson"
            return wd.load_file(path, name)
        files = _package_files(data, url)
        if files and _hops < 2:  # a dataset description: load the data files it lists
            print(f"The JSON describes a dataset with {len(files)} data file(s).")
            chosen = files if len(files) == 1 else choose("Files of the dataset", files)
            layers = [_load_one(f["url"], f["title"], _hops + 1) for f in chosen]
            layers = [lyr for lyr in layers if lyr is not None]
            return layers or None
        records = _records_in_json(data)
        if not records:
            print("The JSON has no table-like records. Structure:\n" + json.dumps(data, ensure_ascii=False)[:1500])
            return None
        if not confirm(name or f"Table with {len(records)} rows", url=url, sample=records[:10]):
            return None
        return layer_from_records(records, name or "Data from link")
    if not confirm(name or os.path.basename(urllib.parse.urlparse(url).path) or "Data file",
                   url=url, sample=_sample_from_file(path)):
        return None
    return wd.load_file(path, name)


def _package_files(data, base_url):
    """Data files listed by a dataset description: Frictionless datapackage.json ("resources" with "path"/"url"),
    schema.org / DCAT ("distribution" with "contentUrl"/"downloadURL"/"accessURL")."""
    if not isinstance(data, dict):
        return []
    entries = data.get("resources") or data.get("distribution") or []
    files = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        link = (entry.get("path") or entry.get("url") or entry.get("contentUrl") or entry.get("downloadURL")
                or entry.get("accessURL"))
        if isinstance(link, list):
            link = link[0] if link else None
        if isinstance(link, str) and link:
            absolute = urllib.parse.urljoin(base_url, link)
            files.append({"kind": "file", "title": entry.get("title") or entry.get("name") or os.path.basename(link),
                          "url": absolute, "format": (entry.get("format") or entry.get("encodingFormat") or "").upper(),
                          "description": str(entry.get("description") or "")[:240], "source": base_url})
    return [f for f in files if explorer.is_data_link(f["url"]) or f["format"]]


NOT_FOUND_ON_PAGE = (
    "No table, embedded data or data file matching the request was found on the attached page {url}. "
    "Use the text of the page (page_text()) to take the data from it. If the data is not on this page, tell the "
    "user what the page contains - only this page is searched, other pages of the website are not opened.")


def _norm(url):
    parsed = urllib.parse.urlparse(str(url).strip())
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}" + \
        (f"?{parsed.query}" if parsed.query else "")


def _allowed_link(url):
    """Only the attached page itself, and the data files / map services that this page links to directly."""
    link = wd.user_link()
    if not link:
        raise PermissionError("No data link. Ask the user to paste a link into the 'Data link' field.")
    if url is None or _norm(url) == _norm(link):
        return link
    info = wd.cached_page(link) or {}
    found = explorer.last_found(link, wd.user_query()) or {}
    linked = {_norm(x["url"]) for x in info.get("links", []) if x.get("kind") in ("data file", "map service")}
    linked |= {_norm(c["url"]) for c in found.get("candidates", []) if c.get("kind") in ("file", "service")}
    if _norm(url) in linked:
        return url
    raise PermissionError(f"Only the attached page {link} is searched. {url} is another page; it is not opened. "
                          "Use the text, tables and data files of the attached page.")


def _attached_page(url):
    url = _allowed_link(url)
    info = wd.cached_page(url)
    if info is None:
        info = explorer.read_page(url, wd.user_query())
        wd.cache_page(url, info)
    return url, info


def page_text(url=None):
    """The whole text of the attached page, in its order, as one string (lines = paragraphs, list items, cells).
    Use it to extract data written in the text, e.g. with re.findall, then layer_from_records(...)."""
    url, info = _attached_page(url)
    if info.get("is_file"):
        raise ValueError(f"{url} is a data file, not a web page: use load_from_link().")
    text = info.get("full_text", "")
    print(f"Page text: {len(text)} characters, {text.count(chr(10)) + 1} lines")
    return text


def page_table(n=1, url=None):
    """All rows of table n of the attached page (the context shows each table with its first rows).
    Example: layer_from_records(page_table(1), "Population")."""
    url, info = _attached_page(url)
    tables = info.get("table_rows", [])
    if not tables:
        raise ValueError(f"The page {url} has no tables. Use its text, or read_page() to follow its links.")
    if not 1 <= int(n) <= len(tables):
        raise ValueError(f"The page has {len(tables)} table(s): use page_table(1) ... page_table({len(tables)}).")
    rows = tables[int(n) - 1]["records"]
    print(f"Table {n} '{tables[int(n) - 1]['title']}': {len(rows)} rows, columns {list(rows[0])}")
    return rows


def read_page(url=None, query=None):
    """Reads a page like a person: title, headings, main text (paragraphs with the request's words first), tables
    and numbered links (most relevant first). Use it to decide where the data is and which link to follow."""
    url = _allowed_link(url)
    if _norm(url) != _norm(wd.user_link()):
        raise PermissionError(f"{url} is a data file of the page: load it with load_from_link(url=...).")
    info = wd.cached_page(url) or explorer.read_page(url, wd.user_query() if query is None else query)
    wd.cache_page(url, info)
    print(explorer.describe_page(info))
    return info.get("links", [])


def explore_link(url=None, query=None):
    """Looks at the user's link without loading anything: searches the page and the most promising pages of the same
    website and prints the numbered tables, embedded data, files and map services found, most relevant first.
    Then load the matching ones with load_from_link(pick=[numbers])."""
    url = _allowed_link(url)
    found = explorer.explore(url, wd.user_query() if query is None else query)
    print(explorer.describe(found))
    if not found["candidates"] and not found["direct"]:
        print(NOT_FOUND_ON_PAGE.format(url=url))
    return found["candidates"]


def _as_list(result):
    """A loader may return one layer, several layers (a dataset with several files) or None."""
    if result is None:
        return []
    return [r for r in result if r is not None] if isinstance(result, list) else [result]


def _load_candidate(item, ask_sample):
    if item["kind"] == "table":
        if ask_sample and not confirm(item["title"], item.get("description", ""), item["url"], item.get("sample")):
            return None
        return layer_from_records(item["records"], name=item["title"].split(" (")[0][:60])
    if item["kind"] == "service":
        return load(item["url"])
    if item["kind"] == "document":
        print(f"Skipped {item['url']}: documents (PDF, Word) cannot be loaded as data.")
        return None
    return _load_one(item["url"], item.get("title"))


def load_from_link(url=None, query=None, pick=None):
    """Main command for the user's link. Searches the linked page AND the most promising pages of the same website
    for data: tables on the pages, data embedded in the pages, data files (also download links without an extension
    and file pages with a Raw / Download button), map services and open-data portal datasets.
    pick=None: the user chooses in a dialog. pick=[1, 3]: loads those numbers of the last explore_link() result
    (the user still confirms a sample). Returns the list of added layers."""
    url = _allowed_link(url)
    if any(s in url.lower() for s in explorer.SERVICE_PATTERNS):
        return [load(url)]
    if _norm(url) != _norm(wd.user_link()):  # a data file that the attached page links to
        return _as_list(_load_one(url))
    query = wd.user_query() if query is None else query
    found = (explorer.last_found(url, query) if pick else None) or explorer.explore(url, query)
    if found["direct"]:  # the link itself is a file or an API response
        return _as_list(_load_one(url))
    candidates = found["candidates"]
    if not candidates:
        print(explorer.describe(found))
        print(NOT_FOUND_ON_PAGE.format(url=url))
        return []
    if pick:
        numbers = [pick] if isinstance(pick, int) else list(pick)
        chosen = [candidates[n - 1] for n in numbers if 1 <= int(n) <= len(candidates)]
        if not chosen:
            raise ValueError(f"pick must be numbers between 1 and {len(candidates)} from explore_link().")
        ask_sample = True
    else:
        print(explorer.describe(found))
        chosen = choose("Data found at the link", [c for c in candidates if c["kind"] != "document"],
                        message="The AI searched the link and related pages of the website. Select the data to use "
                                "(click an item to see details and a sample):")
        ask_sample = False  # the user has already seen the samples in the choice dialog
    layers = [layer for item in chosen for layer in _as_list(_load_candidate(item, ask_sample))]
    print(f"Loaded {len(layers)} layer(s): {[lyr.name() for lyr in layers]}")
    return layers


def join_keys(table, layer):
    """Prints which fields of a table and a map layer can be used to join them (best first) and returns the list."""
    suggestions = suggest_join(table, layer)
    print(describe_suggestions(suggestions))
    for s in suggestions[:1]:
        if s["unmatched"]:
            print(f"Unmatched table keys of the best pair (examples): {s['unmatched']}")
    return suggestions


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
    "download_osm": download_osm, "load": load, "load_from_link": load_from_link, "explore_link": explore_link,
    "read_page": read_page, "page_table": page_table,
    "page_text": page_text,
    "fetch": fetch,
    "layer_from_records": layer_from_records,
    "ask": ask, "choose": choose, "confirm": confirm,
    "create_layout": create_layout, "export": export,
    "field_info": field_info, "smart_join": smart_join, "join_keys": join_keys,
    "run_in_background": run_in_background,
}
