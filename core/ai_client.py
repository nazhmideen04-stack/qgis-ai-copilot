"""
Network client for LLM providers.
Python standard library only — no external dependencies.
Errors are raised as AIError with a user-friendly message.
"""
import json
import time
import unicodedata
import urllib.parse
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
- ALWAYS reply in the language of the user's request (Russian, Turkish, English, any language). These instructions
  and the context are in English only for technical reasons: never switch to English because of them.
- Questions -> plain text, no code.
- Tasks -> one or two sentences + ALL code in ONE ```python block (never nested or split).

DO ONLY WHAT WAS ASKED
- Write the minimal code for exactly the requested task. No extra actions the user did not ask for:
  no downloading data, no styling, no layouts, no exports, no zooming, no new layers "for convenience".
- Use the existing project layers whenever they can do the job.
- download_osm ONLY when the user explicitly asks to download/get OpenStreetMap data or map features
  that are not in the project. Never download data as a side step of another task.
- Prefer one step. Use [[CONTINUE]] only when you truly cannot write correct code without inspecting first.
- Simple requests need simple code: "open a satellite map" -> add_basemap("Esri World Imagery");
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
  add_basemap(name)         online basemap: "OpenStreetMap", "Esri World Imagery" (satellite), "Esri World Street Map",
                            "Esri World Topo", "CartoDB Positron", "CartoDB Dark Matter", "OpenTopoMap".
                            Google basemaps are not available (Google terms of use): offer the Esri equivalent.
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
- Basemaps are NOT external data: use add_basemap() for any "OSM/satellite/street map" request (no link needed,
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


# Unicode script names (as in unicodedata.name) -> how the request is written
_SCRIPTS = [("CYRILLIC", "Cyrillic script"), ("ARABIC", "Arabic script"), ("GREEK", "Greek script"),
            ("HEBREW", "Hebrew script"), ("CJK", "Chinese characters"), ("HIRAGANA", "Japanese script"),
            ("KATAKANA", "Japanese script"), ("HANGUL", "Korean script"), ("DEVANAGARI", "Devanagari script"),
            ("THAI", "Thai script"), ("GEORGIAN", "Georgian script"), ("ARMENIAN", "Armenian script")]


def language_hint(text):
    """Instruction appended to every request so the model answers in the user's language,
    even though the system prompt and the project context are in English.
    Non-Latin scripts are named; for Latin letters the language is left to the model, because the letters alone
    are misleading (e.g. Russian in transliteration typed on a Turkish keyboard contains Turkish letters)."""
    counts = {}
    for char in str(text or ""):
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        script = next((label for key, label in _SCRIPTS if key in name), None)
        if script:
            counts[script] = counts.get(script, 0) + 1
    detected = max(counts, key=counts.get) if counts else None
    note = f" The request uses {detected}." if detected else (
        " The request is written in Latin letters. Determine its language from the WORDS, not from single letters: "
        "it may be English, Turkish, German, or another language typed in Latin letters, for example Russian in "
        "transliteration (possibly on a Turkish keyboard). Answer in that language, using its normal alphabet "
        "(transliterated Russian -> answer in Russian with Cyrillic letters).")
    return ("[Answer language] Write your whole answer - explanation, plan, questions and the text of print() "
            "reports - in the same language as the user's request above." + note +
            " Keep code, function names and layer/field names unchanged.")


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


class _HttpError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}")
        self.code, self.body = code, body


class _NetworkError(Exception):
    def __init__(self, message, ssl_error=False):
        super().__init__(message)
        self.ssl_error = ssl_error


class _QgisResponse:
    """HTTP request through the QGIS network manager (QGIS proxy, SSL and authentication settings).
    Used as a context manager; iterating yields the response body line by line as it arrives (streaming)."""

    POLL_MS = 200  # how often a waiting request checks for Stop

    def __init__(self, url, payload, headers, timeout, local, cancel):
        from qgis.core import QgsNetworkAccessManager
        from qgis.PyQt.QtCore import QByteArray, QEventLoop, QTimer, QUrl
        from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest

        if local:  # Ollama on this computer: never through a proxy
            self._manager = QNetworkAccessManager()
            self._manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        else:
            self._manager = QgsNetworkAccessManager.instance()
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        all_headers = {"User-Agent": "QGIS-AI-Copilot"}
        all_headers.update(headers or {})
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        for name, value in all_headers.items():
            request.setRawHeader(name.encode("ascii"), str(value).encode("ascii"))
        if payload is None:
            self._reply = self._manager.get(request)
        else:
            self._reply = self._manager.post(request, QByteArray(json.dumps(payload).encode("utf-8")))

        self._timeout = float(timeout)
        self._cancel = cancel or (lambda: False)
        self._loop = QEventLoop()
        self._tick = QTimer()
        self._tick.setInterval(self.POLL_MS)
        self._tick.timeout.connect(self._loop.quit)
        for signal in (self._reply.readyRead, self._reply.finished, self._reply.metaDataChanged):
            signal.connect(self._loop.quit)
        self._last_activity = time.monotonic()

    # ---------------------------------------------------------------- waiting
    def _wait(self):
        """Runs the event loop until data, completion, Stop or timeout."""
        self._tick.start()
        self._loop.exec()
        self._tick.stop()
        if self._cancel():
            self._reply.abort()
            raise Cancelled()
        if self._reply.bytesAvailable() or self._reply.isFinished():
            self._last_activity = time.monotonic()
        elif time.monotonic() - self._last_activity > self._timeout:
            self._reply.abort()
            raise TimeoutError()

    def _status(self):
        from qgis.PyQt.QtNetwork import QNetworkRequest
        value = self._reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        return int(value) if value is not None else None

    def _raise_network_error(self):
        from qgis.PyQt.QtNetwork import QNetworkReply
        error = self._reply.error()
        ssl_error = error == QNetworkReply.NetworkError.SslHandshakeFailedError
        raise _NetworkError(self._reply.errorString(), ssl_error)

    def _read_all(self):
        while not self._reply.isFinished():
            self._wait()
        return bytes(self._reply.readAll()).decode("utf-8", "replace")

    # ---------------------------------------------------------------- context manager / iteration
    def __enter__(self):
        while self._status() is None and not self._reply.isFinished():
            self._wait()
        status = self._status()
        if status is None:  # finished without an HTTP answer: DNS, connection, SSL...
            self._raise_network_error()
        if status >= 400:
            raise _HttpError(status, self._read_all())
        return self

    def __iter__(self):
        from qgis.PyQt.QtNetwork import QNetworkReply
        buffer = b""
        while True:
            data = bytes(self._reply.readAll())
            if data:
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield line
            elif self._reply.isFinished():
                break
            else:
                self._wait()
        if self._reply.error() not in (QNetworkReply.NetworkError.NoError,
                                       QNetworkReply.NetworkError.OperationCanceledError):
            self._raise_network_error()
        if buffer:
            yield buffer

    def read(self):
        return self._read_all().encode("utf-8")

    def __exit__(self, *exc):
        if not self._reply.isFinished():
            self._reply.abort()
        self._reply.deleteLater()
        return False


class Net:
    """Network access through QGIS (Settings → Options → Network / Authentication apply automatically)."""

    def __init__(self):
        self.cancel = None  # set by stream_completion: makes Stop work while waiting for the provider

    def open(self, url, payload=None, headers=None, timeout=TIMEOUT_CLOUD, local=False):
        return _QgisResponse(url, payload, headers, timeout, local, self.cancel)

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


def _http_error_message(provider, code, body) -> str:
    detail = (body or "").strip()
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
        detail = (body or "").strip()
    hint = _HTTP_HINTS.get(code, "")
    return f"{provider}: HTTP {code}. {hint}\n{detail[:600]}".strip()


def _network_error_message(provider, error) -> str:
    text = str(error)
    if getattr(error, "ssl_error", False):
        return (f"{provider}: SSL certificate check failed ({text}). This is usually caused by an antivirus or a "
                "corporate proxy. Check the certificates in QGIS: Settings → Options → Authentication.")
    if provider in LOCAL_PROVIDERS:
        return f"Cannot reach Ollama at {OLLAMA_URL} ({text}). Is Ollama running? Try `ollama serve`."
    return f"{provider}: network error ({text}). Check the internet connection and Settings → Options → Network."


@contextmanager
def _translate_errors(provider):
    try:
        yield
    except (AIError, Cancelled):
        raise
    except _HttpError as e:
        raise AIError(_http_error_message(provider, e.code, e.body)) from None
    except _NetworkError as e:
        raise AIError(_network_error_message(provider, e)) from None
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
    net.cancel = cancel
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
