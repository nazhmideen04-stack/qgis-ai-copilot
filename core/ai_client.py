"""
Network client for LLM providers.
Python standard library only — no external dependencies.
Errors are raised as AIError with a user-friendly message.
"""
import json
import re
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

SYSTEM_PROMPT = """You are an expert QGIS assistant (QGIS 3.28+ and 4). You can do anything QGIS can do.

ANSWER
- ALWAYS reply in the language of the user's request; never switch to English because these rules are English.
- Questions -> plain text, no code. Tasks -> 1-2 sentences + ALL code in ONE ```python block (never split/nested).

DO ONLY WHAT WAS ASKED
- Strictly answer the user's request. Perform only the actions explicitly asked for. Do not add information and do not call tools that the user did not request.
- Minimal code for exactly the request: no unrequested downloads, styling, layouts, exports, zooming or extra layers.
- Use existing project layers when they can do the job. download_osm ONLY if the user asks for OpenStreetMap data
  or features not in the project.

CODE
- Never import PyQt5/PyQt6 (use qgis.PyQt). Available: qgis.core/gui/analysis, QtCore/QtGui/QtWidgets, iface,
  processing, os, sys, math, json, gdal/ogr/osr, np. Fully qualified enums (Qt.GlobalColor.red) work on Qt5 and Qt6.
- Fields ONLY via new_field(name, kind) (string|int|double|bool|date|datetime). Exact layer/field names from context.
- WORKING LAYERS in the context are the ones to use; others are listed by name (print their fields if needed).
- Metric CRS for distances/areas. Coordinates from users are "lat, lon" WGS84; QgsPointXY is (lon, lat); transform
  to the map CRS (zoom_to/add_points do it). The context gives the map view in WGS84.
- No try/except that only prints: let errors raise. print() useful results (you will see them).

COMMANDS (built in, no import) - PREFER THEM to raw PyQGIS: a task one command can do = ONE line of code.
They validate input, accept layer names, report progress and can be undone.
- run_alg(id, {params}, name=None): any Processing algorithm, layers by name, results added to the map.
  find_alg(text|id) prints matches / parameters. get_layer(name). new_field(name, kind).
- style(layer, field, labels=None); zoom_to(place | "lat, lon" | layer); add_points(points); add_basemap(name).
- field_info(layer, field=None): real values of fields. smart_join(table, layer, table_field=None,
  layer_field=None, fields=None, prefix=None): joins a table (CSV/Excel) to a map layer, finds the key fields and
  fixes key spelling / number formats itself; join_keys(table, layer) only lists the key candidates.
- download_osm(key, value=None, place=None); load(path|url); create_layout(title, layers); export(what, path).
- find_action(word) / run_action(name); plugin(name); ask(question, options) / choose / confirm dialogs.
- run_in_background(func, *args): runs a slow pure-Python computation (no layer / GUI access inside) without
  freezing QGIS. Processing algorithms already run in the background.
- Use raw PyQGIS only when no command fits. FIELD VALUES in the context are real: use them exactly in filters.

PLUGINS
- The context lists installed plugins with "run_alg: <ids>" and/or "run_action: '<path>'". Use exactly those.
  If a plugin says "inspect with plugin()", print plugin("name") first and end with [[CONTINUE]].
- Never invent plugin algorithm ids, menu paths or methods.

INTERNET
- Anything else needs a user link
  (USER DATA LINKS in the context) and uses only that page. Never suggest or name websites or data sources yourself.
- No link but external data needed: ask the user to paste a link in "Data link" or load a file with load().
- With a link: search ONLY that page. ATTACHED PAGE CONTENT in the context is the user's page, already read for
  you: its TEXT in order, headings, tables (first rows) and links. ANALYSE THE TEXT and take the data from it - also
  data written in paragraphs, lists and lines ("Konak: 336.545"), not only tables. Answer or do the task directly,
  usually in ONE step. In code: page_table(n) = all rows of a table; page_text() = the whole text (parse it, e.g.
  with re.findall, and build a layer with layer_from_records(rows, "name")); data files and map services that the
  page links to: load_from_link(url=<that file>). Numbers like 336.545 / 1.234,5 use dots or commas as thousand
  separators in many languages.
- With a link, ONLY that link is used - this is mandatory. Never open, name, suggest or use any other page, website
  or data source (also not from your own knowledge), no download_osm and no place search unless the user asked for
  OpenStreetMap. The plugin refuses other websites anyway. If the data is not on the attached page, say exactly
  what the page contains and that another link is needed. Never invent statistics.

OSM TAGS: amenity=cafe|restaurant|fast_food|bar|hospital|clinic|pharmacy|school|university|bank|atm|parking|fuel;
  shop; highway; railway; building; landuse; natural; leisure=park; natural=water; waterway; tourism

MULTI-STEP AND MEMORY
- To inspect first (parameters, fields, link contents): print it and end with a separate line [[CONTINUE]]
  (never on the last step). If a run failed, fix it from the error instead of repeating the same code.
- PREVIOUS CONVERSATION in the context is your memory of this dialog: use earlier layers, results and decisions.
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
    note = f" ({detected})" if detected else (
        " (Latin letters: judge by the words, not single letters; transliterated Russian -> Russian in Cyrillic)")
    return ("[Answer language] Write the explanation, plan, questions and print() texts in the language of the "
            "request above" + note + ". Keep code and layer/field names unchanged.")


RETRY_HTTP_CODES = {429, 500, 502, 503, 504, 529}   # temporary provider problems: worth trying again
RETRY_DELAYS = (3, 8, 15)                          # seconds before the 2nd, 3rd and 4th attempt


class AIError(Exception):
    """Error with a user-friendly message. code: the HTTP status of the provider's answer, if any."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


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
            # always the current version of a page or file, never a stale copy from the QGIS disk cache
            request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                                 QNetworkRequest.CacheLoadControl.AlwaysNetwork)
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
        raise AIError(_http_error_message(provider, e.code, e.body), code=e.code) from None
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

def gemini_thinking(model, fast):
    """Limits the model's internal "thinking" (the main cause of slow answers). None = provider default.
    Gemini 2.5: thinkingBudget (Flash / Flash-Lite can switch it off, Pro only limit it).
    Gemini 3.x: thinkingLevel. Older Gemini models have no thinking."""
    if not fast:
        return None
    m = model.lower()
    if "gemini-2.5" in m:
        return {"thinkingBudget": 1024 if "pro" in m else 0}
    if re.search(r"gemini-[3-9]", m):
        return {"thinkingLevel": "low"}
    return None


def openai_reasoning_effort(model, fast):
    """OpenAI reasoning models (o3, o4, gpt-5...) accept reasoning_effort; o1 and other models do not."""
    m = model.lower()
    if fast and (m.startswith(("o3", "o4")) or m.startswith("gpt-5")):
        return "low"
    return None


def _gemini(net, model, key, text, fast=True):
    model = model.replace("models/", "", 1)
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{urllib.parse.quote(model)}:streamGenerateContent?alt=sse")
    config = {"temperature": TEMPERATURE}
    thinking = gemini_thinking(model, fast)
    if thinking:
        config["thinkingConfig"] = thinking
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": config,
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


def _openai_compatible(net, base_url, model, key, text, fast=True):
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
    effort = openai_reasoning_effort(model, fast) if "openai" in base_url else None
    if effort:
        payload["reasoning_effort"] = effort
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

PAGE_TIMEOUT = 25  # seconds for reading an attached web page


def fetch_page(url, net=None, cancel=lambda: False):
    """Downloads an attached web page through the QGIS network (proxy, SSL, authentication). Works in a
    background thread, so QGIS does not freeze while the page is read."""
    net = net or Net()
    net.cancel = cancel
    with _translate_errors("Website"):
        with net.open(url, headers={"Accept": "text/html,application/xhtml+xml,*/*"}, timeout=PAGE_TIMEOUT) as resp:
            return resp.read()


def _wait_before_retry(seconds, cancel):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel():
            raise Cancelled()
        time.sleep(0.2)


def stream_completion(provider, model, api_key, prompt, context="", net=None, cancel=lambda: False, fast=True,
                      on_retry=None):
    """Generator of response text chunks. Raises AIError or Cancelled."""
    net = net or Net()
    net.cancel = cancel
    key = clean_api_key(api_key)
    _check_key(provider, key)
    model = (model or "").strip()
    if not model:
        raise AIError("No model selected. Press 'Refresh' to load the list or type the model name.")
    text = f"{context}\n\nUser Request:\n{prompt}" if context else prompt

    def open_stream():
        if provider == "Google Gemini":
            return _gemini(net, model, key, text, fast)
        if provider in OPENAI_COMPATIBLE:
            return _openai_compatible(net, OPENAI_COMPATIBLE[provider], model, key, text, fast)
        if provider == "Claude (Anthropic)":
            return _claude(net, model, key, text)
        if provider == PROVIDER_OLLAMA:
            return _ollama(net, model, text)
        raise AIError(f"Provider '{provider}' is not supported.")

    # Temporary provider errors (overloaded, rate limit, server error) are retried automatically,
    # but only while nothing has been received yet - a started answer is never repeated.
    for attempt in range(len(RETRY_DELAYS) + 1):
        started = False
        gen = open_stream()
        try:
            with _translate_errors(provider):
                for piece in gen:
                    if cancel():
                        gen.close()
                        raise Cancelled()
                    started = True
                    yield piece
            return
        except AIError as e:
            if started or e.code not in RETRY_HTTP_CODES or attempt >= len(RETRY_DELAYS):
                if e.code in RETRY_HTTP_CODES and attempt:
                    raise AIError(f"{e}\n(Tried {attempt + 1} times. Try again later or choose another model.)",
                                  code=e.code) from None
                raise
            delay = RETRY_DELAYS[attempt]
            if on_retry is not None:
                on_retry(attempt + 2, len(RETRY_DELAYS) + 1, delay, e.code)
            _wait_before_retry(delay, cancel)


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
