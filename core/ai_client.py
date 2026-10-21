"""
Network client for LLM providers.
Python standard library only — no external dependencies.
Errors are raised as AIError with a user-friendly message.
"""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

OLLAMA_URL = "http://localhost:11434"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_CLOUD = 90
TIMEOUT_LOCAL = 300
TIMEOUT_MODELS = 15

PROVIDER_OLLAMA = "Ollama (Local)"
PROVIDERS = [PROVIDER_OLLAMA, "OpenAI", "DeepSeek", "Google Gemini", "Claude (Anthropic)"]
LOCAL_PROVIDERS = {PROVIDER_OLLAMA}

TEMPERATURE = 0.2  # low randomness: precise code, fewer unrequested actions

OPENAI_COMPATIBLE = {
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com",
}

SYSTEM_PROMPT = """You are an expert QGIS assistant (QGIS 3.28+ and QGIS 4). You can do anything QGIS can do.

ANSWER FORMAT
- Reply in the user's language. Questions -> plain text, no code.
- Tasks -> one or two sentences + ALL code in ONE ```python block (never nested or split).

DO ONLY WHAT WAS ASKED
- Write the minimal code for exactly the requested task. No extra actions the user did not ask for:
  no downloading data, no styling, no layouts, no exports, no zooming, no new layers "for convenience".
- Use the existing project layers whenever they can do the job.
- download_osm ONLY when the user explicitly asks to download/get OpenStreetMap data or map features
  that are not in the project. Never download data as a side step of another task.
- Prefer one step. Use [[CONTINUE]] only when you truly cannot write correct code without inspecting first.
- Simple requests need simple code: "open Google satellite" -> add_basemap("Google Satellite");
  "go to 38.42, 27.14" -> zoom_to("38.42, 27.14"); "put a point at ..." -> add_points(...).

CODE RULES
- Never import PyQt5/PyQt6 (use qgis.PyQt). Already available: all qgis.core/gui/analysis classes,
  QtCore/QtGui/QtWidgets, iface, processing, os, sys, math, json, gdal/ogr/osr, np.
- The exact QGIS version is in the context. Write code that works on it: fully qualified enums
  (Qt.GlobalColor.red, QMessageBox.StandardButton.Yes) work on both Qt5 and Qt6 builds.
- Create attribute fields ONLY with new_field(name, kind), kind = string|int|double|bool|date|datetime
  (QgsField types differ between QGIS versions). Use exact layer/field names from the context.
- If the context lists WORKING LAYERS, work with them. Other layers are given by name only; if you need
  their fields, print them first (get_layer(name).fields()) and continue with [[CONTINUE]].
- Metric CRS for distances/areas when data is in EPSG:4326.

COORDINATES
- Users write coordinates as "lat, lon" in WGS84 (EPSG:4326) unless they say otherwise.
- QgsPointXY is (x=lon, y=lat). The map CRS may differ from EPSG:4326: always transform
  (QgsCoordinateTransform) — or simply use zoom_to / add_points, which parse and transform for you.
- The context gives the current map view in WGS84 (center, bounds, scale).
- No try/except that only prints: let errors raise. print() useful results (you will see them).

COMMANDS (already defined, prefer them)
  run_alg(id, params, load=True, name=None)  any Processing algorithm; validates params, accepts layer names,
                            adds outputs to the map. Ex: run_alg("native:buffer", {"INPUT": "Roads", "DISTANCE": 100})
  find_alg(text)            exact id -> prints parameters; otherwise searches algorithms
  get_layer(name)           layer by name
  new_field(name, kind)     QgsField for any QGIS version (string|int|double|bool|date|datetime)
  style(layer, field=None, mode="auto", classes=5, method="jenks", ramp=None, labels=None)
                            graduated/categorized colours by field, labels by field or expression
  add_basemap(name)         online basemap: "Google Maps", "Google Satellite", "Google Hybrid", "Google Terrain",
                            "OpenStreetMap", "Esri World Imagery", "Esri World Topo", "CartoDB Positron",
                            "CartoDB Dark Matter", "OpenTopoMap" (aliases: "google", "satellite", "dark"...)
  zoom_to(target, scale=None)   "lat, lon" / (lat, lon) / DMS / layer / (west, south, east, north) / place name
  add_points(points, name="Points")   point layer from "lat, lon" strings, (lat, lon, "label") tuples or dicts
  find_action(keyword) / run_action(name)    any QGIS or plugin button/menu item (dialogs wait for the user)
  plugin(name=None)         plugin() lists plugins; plugin("name") prints its algorithms, menu/toolbar actions and
                            Python API, returns the plugin object (enables it or opens the plugin manager if needed)
  download_osm(key="", value="", tags=None, extent=None, place=None, layer_name=None)
      OpenStreetMap data: download_osm("amenity","cafe|bar"), download_osm(tags=["building","highway"]),
      download_osm() = all objects (small area), place="City" = inside its boundary
  load(source=None, name=None, kind="auto", layer_name="")   local file (None -> file dialog), file URL,
                            or web service (XYZ/WMS/WFS/ArcGIS REST)
  load_from_link(url=None)  reads the user's link, finds data on it (files, tables, APIs, CKAN datasets),
                            lets the user choose, shows a sample, asks to confirm, loads it. Returns layers.
  fetch(url)                raw API/page response (JSON -> dict/list, else text); layer_from_records(rows, name)
  ask(question, options=None) / choose(title, items) / confirm(title, description, url, sample)   dialogs
  create_layout(title, layers=None, extent=None, subtitle="", sources="", page="A4", orientation="landscape")
                            map + title + legend + scale bar + north arrow + footer, opens the designer
  export(what, path=None, **opts)   layout -> pdf/png/jpg/tif/svg; layer -> gpkg/shp/geojson/kml/csv/xlsx/dxf/...;
                            "map" -> image; "project" -> qgz; "all" -> every layer to a folder. No path -> Save dialog.

USING INSTALLED PLUGINS
- The context lists every installed plugin with what can be used: "run_alg: <ids>" and/or "run_action: '<path>'".
- When the user names a plugin or a task fits one: use its algorithms with run_alg (check parameters with
  find_alg(id) first if unsure), or open its window with run_action using the exact path from the context.
- If the plugin line says "inspect with plugin()" or you need more than the context shows, print plugin("name")
  first and end with [[CONTINUE]]. Call plugin methods only if plugin() printed them.
- Never invent plugin algorithm ids, menu paths or method names.

INTERNET DATA
- Basemaps are NOT external data: use add_basemap() for any "Google/OSM/satellite map" request (no link needed,
  no plugins needed).
- Never suggest, name or link websites or data sources yourself, and never access the internet except
  add_basemap, zoom_to(place) and download_osm, unless the user gave a link (see USER DATA LINKS).
- If the task needs external data and there is no link, say briefly that the user should paste a link to the
  data source into the "Data link" field, or load a local file with load().
- With a link: call load_from_link(), then join/style/analyse the result. Never invent statistics.

OSM TAGS: food amenity=cafe|restaurant|fast_food|bar ; shop ; health amenity=hospital|clinic|pharmacy ;
  education amenity=school|university|kindergarten ; roads highway ; railway ; building ; landuse ; natural ;
  leisure=park ; water natural=water, waterway ; tourism ; amenity=bank|atm|parking|fuel|place_of_worship

MULTI-STEP
- If you must inspect something first (algorithm parameters, field values, link contents), print it and end the
  message with a separate line [[CONTINUE]]; you will get the output and continue. Never use it on the last step.
- If the previous run failed, fix it using the error message instead of repeating the same code.
"""


class AIError(Exception):
    """Error with a user-friendly message."""


class Cancelled(Exception):
    """The user stopped the generation."""


# --------------------------------------------------------------------------- helpers

def clean_api_key(key) -> str:
    """Removes spaces, quotes, line breaks and invisible characters picked up when copying."""
    key = str(key or "")
    for ch in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
        key = key.replace(ch, "")
    key = "".join(key.split())
    return key.strip("\"'")


def _check_key(provider: str, key: str):
    if provider in LOCAL_PROVIDERS:
        return
    if not key:
        raise AIError(f"API key for {provider} is empty.")
    if not key.isascii():
        raise AIError(
            "The API key contains non-Latin characters (usually a copy-paste or keyboard-layout issue). "
            "Copy the key again from the provider's website."
        )


def _ssl_context(verify: bool) -> ssl.SSLContext:
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi  # available in many QGIS builds
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class Net:
    """Network settings: SSL verification and proxy (taken from the QGIS settings)."""

    def __init__(self, verify_ssl: bool = True, proxy_url: str = None):
        self.verify_ssl = verify_ssl
        self.proxy_url = proxy_url

    def open(self, url, payload=None, headers=None, timeout=TIMEOUT_CLOUD, local=False):
        proxies = {}
        if self.proxy_url and not local:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        # Explicit ProxyHandler: no time wasted on system proxy auto-detection
        # and the global urllib is left untouched (it used to be patched for all of QGIS).
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies),
            urllib.request.HTTPSHandler(context=_ssl_context(self.verify_ssl)),
        )
        hdrs = {"User-Agent": "QGIS-AI-Copilot/0.2"}
        hdrs.update(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
        return opener.open(req, timeout=timeout)

    def get_json(self, url, headers=None, local=False):
        with self.open(url, headers=headers, timeout=TIMEOUT_MODELS, local=local) as resp:
            return json.loads(resp.read().decode("utf-8"))


_HTTP_HINTS = {
    400: "Bad request — check the model name (and the key for Gemini).",
    401: "Invalid API key.",
    402: "Insufficient balance on the provider account.",
    403: "Access denied — the key has no permission, or the service is unavailable in your region.",
    404: "Model or endpoint not found — choose another model.",
    429: "Rate limit or quota exceeded — wait or check billing.",
    500: "Provider server error — try again later.",
    503: "Provider is temporarily unavailable — try again later.",
    529: "Provider is overloaded — try again later.",
}


def _http_error_message(provider, e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    detail = body.strip()
    try:
        data = json.loads(body)
        if isinstance(data, list) and data:
            data = data[0]
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            detail = err.get("message") or json.dumps(err, ensure_ascii=False)
        elif err:
            detail = str(err)
    except (ValueError, AttributeError, TypeError, KeyError, IndexError):
        # The response body is not JSON — show it as is
        detail = body.strip()
    hint = _HTTP_HINTS.get(e.code, "")
    return f"{provider}: HTTP {e.code}. {hint}\n{detail[:600]}".strip()


def _url_error_message(provider, e: urllib.error.URLError) -> str:
    reason = getattr(e, "reason", e)
    text = str(reason)
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return (f"{provider}: SSL certificate check failed ({text}). "
                "Usually caused by an antivirus/corporate proxy. Enable 'Skip SSL check' in the plugin "
                "only if you trust your network.")
    if provider in LOCAL_PROVIDERS:
        return f"Cannot reach Ollama at {OLLAMA_URL} ({text}). Is Ollama running? Try `ollama serve`."
    return f"{provider}: network error ({text}). Check your internet connection / proxy settings in QGIS."


@contextmanager
def _translate_errors(provider):
    try:
        yield
    except (AIError, Cancelled):
        raise
    except urllib.error.HTTPError as e:
        raise AIError(_http_error_message(provider, e)) from None
    except urllib.error.URLError as e:
        raise AIError(_url_error_message(provider, e)) from None
    except TimeoutError:
        raise AIError(f"{provider}: the request timed out. Try again or pick a faster model.") from None
    except UnicodeEncodeError:
        raise AIError("The API key or model name contains invalid (non-Latin) characters.") from None
    except ConnectionError as e:
        raise AIError(f"{provider}: connection lost ({e}).") from None


def _sse(resp):
    """Parses a Server-Sent Events stream and yields JSON objects."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _stream_error(chunk):
    err = chunk.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise AIError(msg or "The provider returned an error during streaming.")


# --------------------------------------------------------------------------- providers

def _gemini(net, model, key, text):
    model = model.replace("models/", "", 1)
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{urllib.parse.quote(model)}:streamGenerateContent?alt=sse")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": TEMPERATURE},
    }
    # The key is sent in a header, not in the URL (keeps it out of logs and error messages)
    with net.open(url, payload, {"x-goog-api-key": key}) as resp:
        for chunk in _sse(resp):
            _stream_error(chunk)
            block = (chunk.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise AIError(f"Gemini blocked the request: {block}")
            for cand in chunk.get("candidates", []):
                for part in (cand.get("content") or {}).get("parts", []):
                    if part.get("text") and not part.get("thought"):
                        yield part["text"]


def _supports_temperature(model):
    """OpenAI reasoning models (o1/o3/o4, gpt-5…) reject a custom temperature."""
    m = model.lower()
    return not (m.startswith(("o1", "o3", "o4", "gpt-5")) or "reasoner" in m)


def _openai_compatible(net, base_url, model, key, text):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": True,
    }
    if _supports_temperature(model):
        payload["temperature"] = TEMPERATURE
    with net.open(f"{base_url}/chat/completions", payload, {"Authorization": f"Bearer {key}"}) as resp:
        for chunk in _sse(resp):
            _stream_error(chunk)
            for choice in chunk.get("choices", []):
                content = (choice.get("delta") or {}).get("content")
                if content:
                    yield content


def _claude_max_tokens(model: str) -> int:
    # Older Claude 3 models (haiku/sonnet/opus) accept at most 4096
    m = model.lower()
    if m.startswith("claude-3-") and not m.startswith(("claude-3-5", "claude-3-7")):
        return 4096
    return 8192


def _claude(net, model, key, text):
    payload = {
        "model": model,
        "max_tokens": _claude_max_tokens(model),
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text}],
        "stream": True,
        "temperature": TEMPERATURE,
    }
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    with net.open("https://api.anthropic.com/v1/messages", payload, headers) as resp:
        for event in _sse(resp):
            etype = event.get("type")
            if etype == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield delta["text"]
            elif etype == "error":
                _stream_error(event)


def _ollama(net, model, text):
    payload = {"model": model, "system": SYSTEM_PROMPT, "prompt": text, "stream": True,
               "options": {"temperature": TEMPERATURE}}
    with net.open(f"{OLLAMA_URL}/api/generate", payload, timeout=TIMEOUT_LOCAL, local=True) as resp:
        for raw in resp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                chunk = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise AIError(f"Ollama: {chunk['error']}")
            if chunk.get("response"):
                yield chunk["response"]
            if chunk.get("done"):
                break


# --------------------------------------------------------------------------- public API

def stream_completion(provider, model, api_key, prompt, context="", net=None, cancel=lambda: False):
    """Generator of response text chunks. Raises AIError or Cancelled."""
    net = net or Net()
    key = clean_api_key(api_key)
    _check_key(provider, key)
    model = (model or "").strip()
    if not model:
        raise AIError("No model selected. Press 'Refresh' to load the list or type the model name.")
    text = f"{context}\n\nUser Request:\n{prompt}" if context else prompt

    if provider == "Google Gemini":
        gen = _gemini(net, model, key, text)
    elif provider in OPENAI_COMPATIBLE:
        gen = _openai_compatible(net, OPENAI_COMPATIBLE[provider], model, key, text)
    elif provider == "Claude (Anthropic)":
        gen = _claude(net, model, key, text)
    elif provider == PROVIDER_OLLAMA:
        gen = _ollama(net, model, text)
    else:
        raise AIError(f"Provider '{provider}' is not supported.")

    with _translate_errors(provider):
        for piece in gen:
            if cancel():
                gen.close()
                raise Cancelled()
            yield piece


_OPENAI_EXCLUDE = ("audio", "realtime", "tts", "transcribe", "image", "embedding",
                   "search", "moderation", "dall-e", "whisper", "instruct")


def _is_openai_chat_model(model_id: str) -> bool:
    mid = model_id.lower()
    if any(x in mid for x in _OPENAI_EXCLUDE):
        return False
    return mid.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))


def list_models(provider, api_key, net=None) -> list:
    """Returns the list of available models. Also verifies that the key works."""
    net = net or Net()
    key = clean_api_key(api_key)
    _check_key(provider, key)

    with _translate_errors(provider):
        if provider == PROVIDER_OLLAMA:
            data = net.get_json(f"{OLLAMA_URL}/api/tags", local=True)
            return sorted(m["name"] for m in data.get("models", []))

        if provider == "OpenAI":
            data = net.get_json("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
            return sorted((m["id"] for m in data.get("data", []) if _is_openai_chat_model(m["id"])), reverse=True)

        if provider == "DeepSeek":
            data = net.get_json("https://api.deepseek.com/models", {"Authorization": f"Bearer {key}"})
            return [m["id"] for m in data.get("data", [])]

        if provider == "Google Gemini":
            data = net.get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                                {"x-goog-api-key": key})
            models = []
            for m in data.get("models", []):
                name = m.get("name", "").replace("models/", "", 1)
                if "gemini" in name and "generateContent" in m.get("supportedGenerationMethods", []):
                    models.append(name)
            return models

        if provider == "Claude (Anthropic)":
            data = net.get_json("https://api.anthropic.com/v1/models?limit=1000",
                                {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
            return [m["id"] for m in data.get("data", [])]

    raise AIError(f"Provider '{provider}' is not supported.")
