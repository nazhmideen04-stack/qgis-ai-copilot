"""
Finds data on the website the user linked, instead of expecting a ready file.

explore(url, query) searches ONLY the page the user linked (no other pages of the website) and collects:
- tables shown on the pages (HTML <table>), with numbers recognised;
- data embedded in the pages (JSON in <script> blocks, schema.org Dataset descriptions);
- links to data files (CSV, Excel, GeoJSON, ZIP, GeoPackage, KML, GeoTIFF...), also download links without an
  extension and file links found inside scripts;
- links to map services (WMS, WFS, ArcGIS REST, XYZ tiles) and files of open-data portal datasets (CKAN).
Relevance: whole words of the user's request (any language) and data words; navigation pages are skipped.
Data files and map services that the page links to directly belong to the page and can be loaded.
"""
import heapq
import json
import os
import re
import urllib.parse
import unicodedata
import urllib.robotparser
from html.parser import HTMLParser

from . import web_data as wd
from .progress import check_cancel, report_status

MAX_PAGES = 1               # only the page the user linked is searched (no other pages of the website)
MAX_DEPTH = 1
MAX_CANDIDATES = 40
MIN_TABLE_ROWS = 2
MAX_SCRIPT_CHARS = 3_000_000
DATA_EXT = (".csv", ".tsv", ".xlsx", ".xls", ".ods", ".json", ".geojson", ".zip", ".gpkg", ".kml", ".kmz",
            ".gml", ".shp", ".tif", ".tiff", ".fgb", ".parquet")
DOC_EXT = (".pdf", ".doc", ".docx")
PAGE_EXT = (".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".cfm")
SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".ppt", ".pptx", ".mp4",
            ".mp3", ".woff", ".woff2", ".ttf", ".xml", ".rss", ".atom")
SERVICE_PATTERNS = ("featureserver", "mapserver", "service=wms", "service=wfs", "/wms", "/wfs", "{z}")
DATA_WORDS = {"data", "dataset", "datasets", "download", "downloads", "export", "statistics", "statistic", "stats",
              "table", "tables", "csv", "xlsx", "xls", "excel", "json", "geojson", "api", "shapefile", "gis",
              "layer", "layers", "catalog", "catalogue", "file", "files", "indicator", "indicators", "opendata",
              "raw", "portal"}
NAVIGATION_WORDS = {"login", "logout", "signin", "signup", "register", "account", "cart", "privacy", "cookie",
                    "cookies", "terms", "contact", "careers", "jobs", "press", "blog", "news", "help", "support",
                    "issues", "pulls", "pull", "actions", "projects", "security", "pulse", "activity", "forks",
                    "stargazers", "watchers", "settings", "notifications", "sponsors", "marketplace", "pricing",
                    "features", "enterprise", "commits", "branches", "tags", "releases", "wiki", "discussions",
                    "custom", "properties", "report", "abuse", "feedback", "share", "print", "facebook", "twitter",
                    "linkedin", "instagram", "youtube", "navigation", "menu", "header", "footer", "sidebar"}
SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:")


# --------------------------------------------------------------------------- words and scores

_PLAIN = {0x0131: "i", 0x00df: "ss", 0x0153: "oe", 0x00e6: "ae", 0x00f8: "o", 0x0142: "l", 0x0111: "d"}


def _plain(text):
    """Lower case without accents, so that differently written forms match (accented / plain letters)."""
    text = unicodedata.normalize("NFKD", str(text or "").lower().translate(_PLAIN))
    return "".join(c for c in text if not unicodedata.combining(c))


def _words(text):
    """Whole words (letters of any alphabet, 3+ characters) and years, lower case, without accents."""
    plain = _plain(text)
    return set(re.findall(r"[^\W\d_]{3,}", plain)) | set(re.findall(r"\b(?:19|20)\d\d\b", plain))


def _same_word(a, b):
    """Equal, or forms of one word: a shared beginning of 5+ letters covering most of the shorter word."""
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter < 5 or a.isdigit() or b.isdigit():
        return False
    common = len(os.path.commonprefix([a, b]))
    return common >= 5 and common >= 0.7 * shorter


def _overlap(words, targets):
    return sum(1 for w in targets if any(_same_word(w, x) for x in words))


def _link_words(url, label, base_path):
    """Words of a link: its text plus the part of its path that differs from the start page.
    The query string is ignored (e.g. login?return_to=... repeats the current page)."""
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    if base_path and path.startswith(base_path):
        path = path[len(base_path):]
    return _words(label) | _words(path)


def _is_navigation(url, label):
    path_words = _words(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    return bool((path_words | _words(label)) & NAVIGATION_WORDS)


def _score(words, query_words):
    return 3 * _overlap(words, query_words) + _overlap(words, DATA_WORDS)


# --------------------------------------------------------------------------- HTML parsing

class _PageParser(HTMLParser):
    """Title, links, tables, script blocks and GET forms of an HTML page (standard library only)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.links, self.tables, self.scripts, self.forms = "", [], [], [], []
        self.headings, self.blocks, self._block = [], [], []
        self._skip_text = 0
        self._in_title = False
        self._link = None
        self._heading, self._last_heading = None, ""
        self._table_stack = []
        self._script = None
        self._form = None

    BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "td", "tr", "br", "section", "article", "dd", "dt"}

    def _flush_block(self):
        text = _clean("".join(self._block))
        if text:
            self.blocks.append(text)
        self._block = []

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        if tag in self.BLOCK_TAGS:
            self._flush_block()
        if tag in ("style", "noscript", "svg"):
            self._skip_text += 1
        if tag == "title":
            self._in_title = True
        elif tag == "a" and attrs.get("href"):
            self._link = [attrs["href"], attrs.get("title", "") + " " + attrs.get("aria-label", "")]
        elif tag in ("h1", "h2", "h3", "h4", "caption"):
            self._heading = ""
        elif tag == "script":
            self._script = {"type": attrs.get("type", "").lower(), "id": attrs.get("id", ""), "text": []}
        elif tag == "form" and attrs.get("method", "get").lower() == "get":
            self._form = {"action": attrs.get("action", ""), "inputs": [], "hidden": {}}
        elif tag == "input" and self._form is not None and attrs.get("name"):
            kind = attrs.get("type", "text").lower()
            if kind in ("text", "search", ""):
                self._form["inputs"].append(attrs["name"])
            elif kind == "hidden":
                self._form["hidden"][attrs["name"]] = attrs.get("value", "")
        elif tag == "table":
            self._table_stack.append({"title": self._last_heading, "rows": [], "row": None, "cell": None})
        elif self._table_stack:
            table = self._table_stack[-1]
            if tag == "tr":
                table["row"] = []
            elif tag in ("td", "th") and table["row"] is not None:
                table["cell"] = ""

    def close(self):
        super().close()
        self._flush_block()

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self._flush_block()
        if tag in ("style", "noscript", "svg") and self._skip_text:
            self._skip_text -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link is not None:
            self.links.append((self._link[0], _clean(self._link[1])))
            self._link = None
        elif tag in ("h1", "h2", "h3", "h4", "caption") and self._heading is not None:
            self._last_heading = _clean(self._heading) or self._last_heading
            if tag != "caption" and _clean(self._heading):
                self.headings.append(_clean(self._heading))
            if tag == "caption" and self._table_stack:
                self._table_stack[-1]["title"] = self._last_heading
            self._heading = None
        elif tag == "script" and self._script is not None:
            self.scripts.append({"type": self._script["type"], "id": self._script["id"],
                                 "text": "".join(self._script["text"])[:MAX_SCRIPT_CHARS]})
            self._script = None
        elif tag == "form" and self._form is not None:
            if self._form["inputs"]:
                self.forms.append(self._form)
            self._form = None
        elif self._table_stack:
            table = self._table_stack[-1]
            if tag in ("td", "th") and table["cell"] is not None and table["row"] is not None:
                table["row"].append(_clean(table["cell"]))
                table["cell"] = None
            elif tag == "tr" and table["row"] is not None:
                if any(table["row"]):
                    table["rows"].append(table["row"])
                table["row"] = None
            elif tag == "table":
                done = self._table_stack.pop()
                self.tables.append({"title": done["title"], "rows": done["rows"]})

    def handle_data(self, data):
        if self._script is not None:
            self._script["text"].append(data)
            return
        if not self._skip_text and not self._in_title:
            self._block.append(data)
        if self._in_title:
            self.title += data
        if self._link is not None:
            self._link[1] += data
        if self._heading is not None:
            self._heading += data
        if self._table_stack and self._table_stack[-1]["cell"] is not None:
            self._table_stack[-1]["cell"] += data


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _decode(raw):
    """Page text in its own encoding (meta charset), e.g. windows-1254 on Turkish websites."""
    match = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", raw[:4096], re.I)
    for encoding in ([match.group(1).decode("ascii", "ignore")] if match else []) + ["utf-8"]:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- tables and embedded data

_THOUSANDS = re.compile(r"^-?\d{1,3}([.,\s\u00a0\u202f]\d{3})+([.,]\d+)?$")


def _number(value):
    """'336.545', '336,545', '336 545', '12,5', '1.234,5' -> number; otherwise None."""
    text = str(value).strip().replace("\u00a0", " ").replace("\u202f", " ").rstrip("%").strip()
    if not text:
        return None
    if _THOUSANDS.match(text):
        last_sep = max(text.rfind(","), text.rfind("."))
        decimals = len(text) - last_sep - 1
        grouped = re.sub(r"[.,\s]", "", text)
        if decimals == 3 or last_sep < 0:  # the last separator groups thousands
            return int(grouped)
        return float(grouped[:-decimals] + "." + grouped[-decimals:])
    if re.match(r"^-?\d+([.,]\d+)?$", text):
        return float(text.replace(",", ".")) if re.search(r"[.,]", text) else int(text)
    return None


def table_records(rows):
    """Table rows (first row = header) -> list of dicts; numeric columns become numbers."""
    if len(rows) < MIN_TABLE_ROWS + 1:
        return []
    width = max(len(r) for r in rows)
    header = [h or f"col{i + 1}" for i, h in enumerate(rows[0] + [""] * (width - len(rows[0])))]
    seen = {}
    for i, h in enumerate(header):  # unique, non-empty field names
        seen[h] = seen.get(h, 0) + 1
        if seen[h] > 1:
            header[i] = f"{h}_{seen[h]}"
    body = [r + [""] * (width - len(r)) for r in rows[1:] if len(r) > 1]
    numeric = [all(_number(r[i]) is not None or not r[i] for r in body) and any(r[i] for r in body)
               for i in range(width)]
    return [{header[i]: (_number(r[i]) if numeric[i] and r[i] else r[i] or None) for i in range(width)}
            for r in body]


def _record_lists(data, path="", found=None, depth=0):
    """Lists of flat dicts inside a JSON structure (>= 3 rows, >= 2 mostly scalar keys)."""
    found = [] if found is None else found
    if depth > 12 or len(found) > 20:
        return found
    if isinstance(data, list):
        dicts = [d for d in data[:200] if isinstance(d, dict)]
        if len(dicts) >= 3 and len(dicts) >= 0.8 * min(len(data), 200):
            keys = set().union(*(d.keys() for d in dicts[:20]))
            scalar = [k for k in keys if all(not isinstance(d.get(k), (dict, list)) for d in dicts[:20])]
            if len(scalar) >= 2:
                rows = [{k: d.get(k) for k in scalar} for d in data if isinstance(d, dict)]
                found.append((path or "data", rows))
                return found
        for i, item in enumerate(data[:50]):
            _record_lists(item, f"{path}[{i}]", found, depth + 1)
    elif isinstance(data, dict):
        for key, value in data.items():
            _record_lists(value, f"{path}.{key}" if path else str(key), found, depth + 1)
    return found


UI_KEYS = {"icon", "label", "href", "anchor", "commandid", "enabled", "subscribed", "popovertarget", "htmltext",
           "level", "classname", "class", "style", "target", "tooltip", "selected", "active", "visible", "disabled",
           "hassimplifiedpath", "contenttype", "testid", "aria", "slot", "variant", "size", "color", "order"}


def _is_ui_data(path, rows):
    """Lists that describe the website's interface (menus, buttons, icons), not data."""
    keys = {str(k).lower().replace("_", "").replace("-", "") for k in rows[0]}
    ui = len(keys & UI_KEYS)
    has_numbers = any(isinstance(v, (int, float)) and not isinstance(v, bool) for r in rows[:20] for v in r.values())
    return ui >= max(2, len(keys) // 2) or (ui >= 1 and not has_numbers and len(rows) < 10) or \
        bool(_words(path) & {"navigation", "menu", "toc", "breadcrumbs", "sidebar", "header", "footer"})


def _json_strings(data, out=None, depth=0):
    out = [] if out is None else out
    if depth > 14 or len(out) > 2000:
        return out
    if isinstance(data, str):
        out.append(data)
    elif isinstance(data, list):
        for item in data[:500]:
            _json_strings(item, out, depth + 1)
    elif isinstance(data, dict):
        for value in data.values():
            _json_strings(value, out, depth + 1)
    return out


def _script_json(script):
    """JSON of a <script> block: application/json, ld+json, __NEXT_DATA__ or `window.X = {...};`."""
    text = script["text"].strip()
    if not text:
        return None
    candidates = [text] if script["type"] in ("application/json", "application/ld+json") or \
        script["id"] == "__NEXT_DATA__" else []
    match = re.search(r"=\s*(\{.*\}|\[.*\])\s*;?\s*$", text, re.S)
    if match and len(match.group(1)) > 50:
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


_FILE_IN_TEXT = re.compile(r"(?:https?://|/)[^\s\"'<>()\\]+?\.(?:csv|xlsx|xls|geojson|json|zip|gpkg|kml|kmz|"
                           r"tif|tiff|ods|parquet)(?:\?[^\s\"'<>()\\]*)?(?=[\s\"'<>()\\]|$)", re.I)


# --------------------------------------------------------------------------- links

def is_data_link(url):
    """A data file (not a web page): a data extension, a format=... parameter, or a download/export endpoint
    with parameters, e.g. /download?id=7 or /export/15."""
    parsed = urllib.parse.urlparse(url)
    path, query = parsed.path.lower(), parsed.query.lower()
    if path.endswith(DATA_EXT) or re.search(r"(^|&)(format|f|type)=(csv|json|geojson|xlsx|xls|kml|zip|gpkg)", query):
        return True
    if path.endswith(PAGE_EXT):
        return False
    return bool(re.search(r"/(download|export|get_?file)\b", path) and (query or re.search(r"/\d+$", path)))


def _host(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_site(url, base):
    """Only the linked host is crawled (www. is ignored); other sub-domains (support., blog.) are not."""
    return _host(url) == _host(base)


def raw_file_link(page_html, page_url):
    """On a page that only shows a file (code hosting, data portals), the link of the file itself:
    a 'Raw' / 'Download' button or a link with the same file name and a data extension."""
    parser = _PageParser()
    parser.feed(page_html)
    name = os.path.basename(urllib.parse.urlparse(page_url).path).lower()
    best = None
    for href, label in parser.links:
        absolute = urllib.parse.urljoin(page_url, href.strip())
        words = _words(label)
        target = os.path.basename(urllib.parse.urlparse(absolute).path).lower()
        if words & {"raw", "download"} or (name and target == name and absolute != page_url):
            if (absolute != page_url and not absolute.lower().startswith(SKIP_SCHEMES)
                    and _same_site(absolute, page_url)):
                best = absolute if words & {"raw"} or best is None else best
    return best


# --------------------------------------------------------------------------- robots.txt

class _Robots:
    def __init__(self, start_url):
        parsed = urllib.parse.urlparse(start_url)
        self.parser = urllib.robotparser.RobotFileParser()
        self.loaded = False
        try:
            text = wd.fetch_text(f"{parsed.scheme}://{parsed.netloc}/robots.txt", purpose="read robots.txt")
            self.parser.parse(text.splitlines())
            self.loaded = True
        except Exception:  # no robots.txt (or unreachable): everything is allowed, as for browsers
            self.loaded = False

    def allowed(self, url):
        return not self.loaded or self.parser.can_fetch("*", url)


# --------------------------------------------------------------------------- exploring

def _read(url):
    path = wd.download_file(url, "ai_copilot_page.html", purpose="read page")
    with open(path, "rb") as fh:
        return fh.read()


def _looks_like_html(raw):
    head = raw[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head or b"<body" in head


def _ckan_resources(url):
    parsed = urllib.parse.urlparse(url)
    match = re.search(r"/dataset/([^/?#]+)", parsed.path)
    if not match:
        return None
    api = f"{parsed.scheme}://{parsed.netloc}/api/3/action/package_show?id={match.group(1)}"
    try:
        data = json.loads(wd.fetch_text(api, purpose="read dataset files"))
    except Exception:  # not a CKAN portal after all
        return None
    result = data.get("result") or {}
    return [{"kind": "file", "title": r.get("name") or "file", "url": r.get("url"),
             "format": (r.get("format") or "").upper(), "description": (r.get("description") or "")[:300],
             "source": result.get("title", ""), "score": 2} for r in result.get("resources", [])
            if r.get("url") and _same_site(r["url"], url)]  # files hosted on other websites are not used


class _Search:
    def __init__(self, url, query, max_pages=MAX_PAGES):
        self.max_pages = max_pages
        self.start = url
        self.base_path = urllib.parse.urlparse(url).path.rstrip("/")
        self.query_words = _words(query)
        self.query = query
        self.candidates, self.pages, self.seen = [], [], {url}
        self.queue = []  # (-score, order, depth, url)
        self.order = 0
        self.ignored_external = 0
        self.robots = _Robots(url) if max_pages > 1 else None  # one page given by the user: no crawl, no robots

    # ---- queue
    def push(self, url, score, depth):
        url = url.split("#")[0]
        if (self.robots is None or url in self.seen or depth > MAX_DEPTH or score < 0
                or not self.robots.allowed(url)):
            return
        self.seen.add(url)
        self.order += 1
        heapq.heappush(self.queue, (-score, self.order, depth, url))

    def add(self, candidate, words):
        if any(c["url"] == candidate["url"] and c["kind"] == candidate["kind"] and c["title"] == candidate["title"]
               for c in self.candidates):
            return
        candidate["score"] = _score(words, self.query_words) + candidate.get("score", 0)
        self.candidates.append(candidate)

    # ---- one page
    def read_page(self, page_url, raw, depth):
        self.pages.append(page_url)
        report_status("Searching the website for data…", f"page {len(self.pages)}/{self.max_pages}: {page_url[:80]}")
        parser = _PageParser()
        parser.feed(_decode(raw))
        page_title = _clean(parser.title) or page_url
        page_words = _words(page_title)

        for n, table in enumerate(parser.tables, 1):
            records = table_records(table["rows"])
            if len(records) >= MIN_TABLE_ROWS and len(records[0]) >= 2:
                title = table["title"] or f"Table {n}"
                self.add({"kind": "table", "title": f"{title} ({len(records)} rows)", "url": page_url,
                          "source": page_title, "format": "HTML table", "records": records,
                          "sample": records[:10], "description": "Columns: " + ", ".join(records[0]), "score": 2},
                         _words(title) | _words(" ".join(records[0])) | page_words)

        for script in parser.scripts:
            data = _script_json(script)
            if data is not None:
                for path, rows in _record_lists(data):
                    if len(rows) >= MIN_TABLE_ROWS and not _is_ui_data(path, rows):
                        self.add({"kind": "table", "title": f"Embedded data '{path[-60:]}' ({len(rows)} rows)",
                                  "url": page_url, "source": page_title, "format": "JSON in page", "records": rows,
                                  "sample": rows[:10], "description": "Fields: " + ", ".join(rows[0]), "score": 0},
                                 _words(path) | _words(" ".join(rows[0])) | page_words)
                texts = [s for s in _json_strings(data) if len(s) < 600]
            else:
                texts = [script["text"]]
            for text in texts:
                for match in _FILE_IN_TEXT.findall(text):
                    absolute = urllib.parse.urljoin(page_url, match)
                    if (absolute.startswith("http") and _same_site(absolute, self.start)
                            and not _is_navigation(absolute, "")):
                        name = os.path.basename(urllib.parse.urlparse(absolute).path)
                        self.add({"kind": "file", "title": name or absolute, "url": absolute, "source": page_title,
                                  "format": os.path.splitext(name)[1].upper().lstrip("."), "score": 1},
                                 _words(absolute) | page_words)

        for href, label in parser.links:
            absolute = urllib.parse.urljoin(page_url, href.strip()).split("#")[0]
            low = absolute.lower()
            if not low.startswith("http") or low.startswith(SKIP_SCHEMES):
                continue
            if not _same_site(absolute, self.start):
                self.ignored_external += 1  # other websites are never used
                continue
            if _is_navigation(absolute, label):
                continue  # login, settings, issues, social networks...
            words = _link_words(absolute, label, self.base_path)
            path_low = urllib.parse.urlparse(absolute).path.lower()
            if any(p in low for p in SERVICE_PATTERNS):
                self.add({"kind": "service", "title": label or absolute, "url": absolute, "source": page_title,
                          "format": "map service", "score": 1}, words)
            elif is_data_link(absolute):
                ext = os.path.splitext(path_low)[1].upper().lstrip(".")
                self.add({"kind": "file", "title": label or os.path.basename(path_low) or absolute, "url": absolute,
                          "source": page_title, "format": ext, "score": 2}, words | page_words)
            elif path_low.endswith(DOC_EXT):
                self.add({"kind": "document", "title": label or os.path.basename(path_low), "url": absolute,
                          "source": page_title, "format": "document (not loadable)", "score": -2}, words)
            elif _same_site(absolute, self.start) and not path_low.endswith(SKIP_EXT):
                self.push(absolute, _score(words, self.query_words), depth + 1)

        if page_url == self.start and self.query and self.max_pages > 1:
            for form in parser.forms[:2]:  # the website's own search, with the words of the request
                params = dict(form["hidden"], **{form["inputs"][0]: self.query[:100]})
                action = urllib.parse.urljoin(page_url, form["action"] or page_url)
                if _same_site(action, self.start):
                    self.push(action.split("?")[0] + "?" + urllib.parse.urlencode(params), 4, 1)

    # ---- crawl
    def run(self, first_raw):
        self.read_page(self.start, first_raw, 0)
        while len(self.pages) < self.max_pages:
            check_cancel()
            if not self.queue:
                break
            _neg, _order, depth, url = heapq.heappop(self.queue)
            try:
                raw = _read(url)
            except Exception as e:  # one unreachable page must not stop the search (incl. QgsProcessingException)
                print(f"Skipped {url}: {(str(e).splitlines() or [''])[0][:80]}")
                continue
            if _looks_like_html(raw):
                self.read_page(url, raw, depth)
            else:  # a "page" link that turned out to be a file
                self.add({"kind": "file", "title": os.path.basename(urllib.parse.urlparse(url).path) or url,
                          "url": url, "source": url, "format": "", "score": 2}, _words(url))
            strong = [c for c in self.candidates if c["score"] >= 5 and c["kind"] != "document"]
            if len(strong) >= 8:
                break
        self.candidates.sort(key=lambda c: -c["score"])
        useful = [c for c in self.candidates if c["score"] >= 0 and c["kind"] != "document"]
        documents = [c for c in self.candidates if c["kind"] == "document"]
        return (useful or self.candidates)[:MAX_CANDIDATES] + documents[:5]


_last = {"key": None, "found": None}


def explore(url, query="", max_pages=None):
    """Explores the user's link. Returns {"pages": [...], "candidates": [...], "direct": bool}.
    A candidate: {kind: table|file|service|document, title, url, source, format, description, sample, records,
    score}. The last result is remembered, so load_from_link(pick=[...]) can load candidates by number."""
    ckan = _ckan_resources(url)
    if ckan is not None:
        for c in ckan:
            c["score"] += _score(_words(f"{c['title']} {c['format']} {c['description']}"), _words(query))
        found = {"pages": [url], "candidates": sorted(ckan, key=lambda c: -c["score"]), "direct": False}
    else:
        report_status("Reading the link…", url)
        raw = _read(url)
        if not _looks_like_html(raw):
            found = {"pages": [url], "candidates": [], "direct": True}  # the link itself is a file / API answer
        else:
            search = _Search(url, query, max_pages or MAX_PAGES)
            found = {"pages": search.pages, "candidates": search.run(raw), "direct": False,
                     "ignored_external": search.ignored_external}
    _last.update(key=(url, query), found=found)
    return found


def last_found(url, query):
    return _last["found"] if _last["key"] == (url, query) else None


def _embedded_texts(parser):
    """Human-readable texts stored in the page's embedded data (pages whose text is built by JavaScript)."""
    texts = []
    for script in parser.scripts:
        data = _script_json(script)
        if data is None:
            continue
        for value in _json_strings(data):
            value = _clean(re.sub(r"<[^>]+>", " ", value))
            letters = sum(c.isalpha() for c in value)
            if (len(value) >= 20 and " " in value and letters >= 0.5 * len(value)
                    and not value.lower().startswith(("http", "/", "{"))):
                texts.append(value)
    return list(dict.fromkeys(texts))[:400]


def _fit_text(blocks, query_words, limit):
    """The page text in its own order. If it is longer than the limit: the beginning of the page, then the
    paragraphs of the rest that contain words of the request (marked with '...')."""
    if sum(len(b) + 1 for b in blocks) <= limit:
        return blocks
    out, total, i = [], 0, 0
    while i < len(blocks) and total + len(blocks[i]) <= 0.6 * limit:
        out.append(blocks[i])
        total += len(blocks[i]) + 1
        i += 1
    for block in blocks[i:]:
        if total >= limit:
            break
        if _overlap(_words(block), query_words):
            piece = block[:max(0, int(limit - total))]
            out.append("... " + piece)
            total += len(piece) + 5
    return out


def read_page(url, query=""):
    """Reads one page the way a person sees it: title, headings, the main text (paragraphs with the words of the
    request first), tables and the numbered links with their texts (most relevant first)."""
    return analyse_page(_read(url), url, query)


def analyse_page(raw, url, query="", text_limit=2500):
    """The content of a downloaded page. Pure Python (no QGIS calls), so it can run in a background thread."""
    if not _looks_like_html(raw):
        return {"url": url, "is_file": True}
    parser = _PageParser()
    parser.feed(_decode(raw))
    parser.close()
    query_words = _words(query)
    base_path = urllib.parse.urlparse(url).path.rstrip("/")
    links, seen, ignored_external = [], set(), 0
    for href, label in parser.links:
        absolute = urllib.parse.urljoin(url, href.strip()).split("#")[0]
        low = absolute.lower()
        if not low.startswith("http") or absolute in seen or absolute == url or _is_navigation(absolute, label):
            continue
        if not _same_site(absolute, url):  # links to other websites are never shown to the AI or used
            ignored_external += 1
            seen.add(absolute)
            continue
        path_low = urllib.parse.urlparse(absolute).path.lower()
        if path_low.endswith(SKIP_EXT):
            continue
        seen.add(absolute)
        if any(p in low for p in SERVICE_PATTERNS):
            kind = "map service"
        elif is_data_link(absolute):
            kind = "data file"
        elif path_low.endswith(DOC_EXT):
            kind = "document"
        else:
            kind = "page"
        words = _link_words(absolute, label, base_path)
        links.append({"text": label or os.path.basename(path_low) or absolute, "url": absolute, "kind": kind,
                      "score": _score(words, query_words) + (2 if kind in ("data file", "map service") else 0)})
    links.sort(key=lambda link: -link["score"])
    blocks = list(dict.fromkeys(b for b in parser.blocks if len(b) >= 2))  # all text, without repeated lines
    if sum(len(b) for b in blocks) < 500:  # little visible text: the page keeps its text in embedded data
        blocks += [t for t in _embedded_texts(parser) if t not in blocks]
    full_text = "\n".join(blocks)
    text = _fit_text(blocks, query_words, text_limit)
    tables, table_rows = [], []
    for n, table in enumerate(parser.tables, 1):
        records = table_records(table["rows"])
        if records:
            title = table["title"] or f"Table {len(table_rows) + 1}"
            table_rows.append({"title": title, "records": records})
            tables.append(f"{title}: {len(records)} rows, columns {list(records[0])[:8]}")
    return {"url": url, "is_file": False, "title": _clean(parser.title), "headings": parser.headings[:25],
            "text": text, "full_text": full_text, "text_cut": len(full_text) > sum(len(t) for t in text) + 20,
            "tables": tables, "table_rows": table_rows, "links": links[:60], "ignored_external": ignored_external,
            "forms": [f["inputs"][0] for f in parser.forms][:3], "scripts_with_data": sum(
                1 for s in parser.scripts if _script_json(s) is not None)}


MAX_PAGE_TEXT = 12000      # characters of page text given to the AI (~3000 tokens)
_text_cache = {}           # url -> page text, so the same link is not downloaded again in one conversation


def page_text(url, max_chars=MAX_PAGE_TEXT):
    """The readable text of the user's link, in reading order: title, headings, paragraphs, lists and tables (as
    text rows), plus the main links. For a data file, its first lines. Used to analyse the page directly."""
    if url in _text_cache:
        return _text_cache[url]
    report_status("Reading the linked page…", url)
    raw = _read(url)
    if not _looks_like_html(raw):
        head = raw[:4000].decode("utf-8", "replace")
        text = f"[The link is a data file, not a web page. First lines:]\n{head}"
    else:
        parser = _PageParser()
        parser.feed(_decode(raw))
        parser.close()
        lines = [f"TITLE: {_clean(parser.title)}"]
        seen = set()
        for block in parser.blocks:                       # reading order; menus repeated on the page only once
            if len(block) < 3 or block in seen:
                continue
            seen.add(block)
            lines.append(block)
        for n, table in enumerate(parser.tables, 1):
            if len(table["rows"]) >= 2:
                lines.append(f"[TABLE {n}: {table['title'] or ''}]")
                lines.extend(" | ".join(row) for row in table["rows"][:200])
        links = [(label, urllib.parse.urljoin(url, href)) for href, label in parser.links
                 if label and not _is_navigation(urllib.parse.urljoin(url, href), label)]
        if links:
            lines.append("[MAIN LINKS]")
            lines.extend(f"{label[:80]} -> {link[:150]}" for label, link in links[:25])
        text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[... the page is longer: {len(text)} characters; read_page(url) shows more]"
    _text_cache[url] = text
    return text


def describe_page(info):
    if info.get("is_file"):
        return f"{info['url']} is a data file (not a web page): load it with load_from_link(url=...)."
    lines = [f"PAGE: {info['title']}  ({info['url']})"]
    if info["headings"]:
        lines.append("Headings: " + " | ".join(info["headings"][:15]))
    if info["tables"]:
        lines.append("Tables on the page: " + "; ".join(info["tables"][:8]))
    if info["scripts_with_data"]:
        lines.append(f"Data embedded in the page: {info['scripts_with_data']} JSON block(s) (explore_link finds them)")
    if info["forms"]:
        lines.append("Search box on the page: yes")
    lines.append("Text of the page (in its order):")
    lines.extend(f"  {t}" for t in info["text"])
    lines.append("Links (most relevant first):")
    for i, link in enumerate(info["links"], 1):
        lines.append(f"  {i}. [{link['kind']}] {link['text'][:70]} -> {link['url'][:120]}")
    return "\n".join(lines)


def context_block(info, max_rows=6):
    """The attached page as a compact text block for the AI: title, headings, text, tables (with the first rows)
    and the most relevant links. The full tables are available to the AI code with page_table(n)."""
    if info.get("is_file"):
        return "The link is a data file (not a web page): load it with load_from_link()."
    lines = [f"Title: {info['title']}"]
    if info["headings"]:
        lines.append("Headings: " + " | ".join(info["headings"][:15]))
    lines.append("Text of the page (in its order):")
    lines.extend(f"  {t}" for t in info["text"])
    if info.get("text_cut"):
        lines.append(f"  [the page text is longer ({len(info['full_text'])} characters): page_text() returns all of "
                     "it in code]")
    tables = info.get("table_rows", [])
    for n, table in enumerate(tables[:8], 1):
        records = table["records"]
        cols = list(records[0])[:10]
        lines.append(f"Table {n} '{table['title']}' ({len(records)} rows) - full rows in code: page_table({n})")
        lines.append("  " + " | ".join(str(c) for c in cols))
        lines.extend("  " + " | ".join(str(r.get(c, ""))[:30] for c in cols) for r in records[:max_rows])
        if len(records) > max_rows:
            lines.append(f"  ... {len(records) - max_rows} more rows")
    if len(tables) > 8:
        lines.append(f"... {len(tables) - 8} more tables (page_table(9) and so on)")
    if info.get("ignored_external"):
        lines.append(f"({info['ignored_external']} links to other websites are ignored: only this website is used)")
    if info["links"]:
        lines.append("Links on the page (most relevant first):")
        lines.extend(f"  - [{link['kind']}] {link['text'][:60]} -> {link['url'][:110]}" for link in info["links"][:20])
    return "\n".join(lines)


def describe(found):
    """Readable summary of what explore() found (printed for the AI and the user)."""
    lines = [f"Visited {len(found['pages'])} page(s):"] + [f"  {p}" for p in found["pages"]]
    if found["direct"]:
        lines.append("The link itself is a data file or an API response.")
    if found.get("ignored_external"):
        lines.append(f"{found['ignored_external']} links to other websites were ignored (only the link's website).")
    lines.append(f"Found {len(found['candidates'])} data source(s), most relevant first:")
    for i, c in enumerate(found["candidates"], 1):
        extra = f" — {c['description'][:140]}" if c.get("description") else ""
        lines.append(f"  {i}. [{c['kind']}] {c['title'][:80]} ({c.get('format') or '?'}) {c['url'][:110]}{extra}")
    return "\n".join(lines)
