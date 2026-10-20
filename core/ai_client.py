"""
Сетевой клиент для LLM-провайдеров.
Только стандартная библиотека Python — никаких внешних зависимостей.
Ошибки поднимаются как AIError с понятным пользователю текстом.
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

OPENAI_COMPATIBLE = {
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com",
}

SYSTEM_PROMPT = """You are a helpful PyQGIS expert and GIS assistant for QGIS 4 (Qt6).

COMMUNICATION RULES:
1. Reply in the SAME LANGUAGE the user writes in.
2. If the user only asks a question, answer in plain text and do NOT write code.
3. If the user asks to do something on the map, give a short explanation, then put ALL
   executable code in ONE single ```python ... ``` block. Never split it into several blocks.

CODE RULES:
1. NEVER import PyQt5 or PyQt6. Use `from qgis.PyQt import QtCore, QtGui, QtWidgets` if needed.
2. All classes from qgis.core, qgis.gui, qgis.analysis, QtCore, QtGui, QtWidgets are already
   available globally, as well as: iface, processing, os, sys, math, json, gdal/ogr/osr and np (if installed).
3. Qt6 uses scoped enums, e.g. Qt.GlobalColor.red, QMessageBox.StandardButton.Ok.
4. Always add new layers with QgsProject.instance().addMapLayer(layer) and check layer.isValid().
5. Use the exact layer names and field names from the project context. Respect the layer CRS;
   reproject or use a metric CRS before measuring distances/areas when the CRS is geographic.
6. Prefer processing.run("native:...") algorithms; use "memory:" or "TEMPORARY_OUTPUT" for outputs.
7. If the task is impossible or requires manual GUI steps, do NOT write code: explain why in text.
"""


class AIError(Exception):
    """Ошибка с понятным пользователю сообщением."""


class Cancelled(Exception):
    """Пользователь остановил генерацию."""


# --------------------------------------------------------------------------- helpers

def clean_api_key(key) -> str:
    """Убирает пробелы, кавычки, переносы строк и невидимые символы, попавшие при копировании."""
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
        import certifi  # есть во многих сборках QGIS
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class Net:
    """Настройки сети: проверка SSL и прокси (берётся из настроек QGIS)."""

    def __init__(self, verify_ssl: bool = True, proxy_url: str = None):
        self.verify_ssl = verify_ssl
        self.proxy_url = proxy_url

    def open(self, url, payload=None, headers=None, timeout=TIMEOUT_CLOUD, local=False):
        proxies = {}
        if self.proxy_url and not local:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        # Явный ProxyHandler: не тратим время на авто-поиск системного прокси
        # и не трогаем глобальный urllib (раньше он патчился для всего QGIS).
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
    except Exception:
        pass
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
    """Разбирает поток Server-Sent Events и отдаёт JSON-объекты."""
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
    }
    # Ключ передаётся в заголовке, а не в URL (не попадёт в логи/сообщения об ошибках)
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


def _openai_compatible(net, base_url, model, key, text):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": True,
    }
    with net.open(f"{base_url}/chat/completions", payload, {"Authorization": f"Bearer {key}"}) as resp:
        for chunk in _sse(resp):
            _stream_error(chunk)
            for choice in chunk.get("choices", []):
                content = (choice.get("delta") or {}).get("content")
                if content:
                    yield content


def _claude_max_tokens(model: str) -> int:
    # Старые модели Claude 3 (haiku/sonnet/opus) принимают максимум 4096
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
    payload = {"model": model, "system": SYSTEM_PROMPT, "prompt": text, "stream": True}
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
    """Генератор кусочков текста ответа. Бросает AIError или Cancelled."""
    net = net or Net()
    key = clean_api_key(api_key)
    _check_key(provider, key)
    model = (model or "").strip()
    if not model:
        raise AIError("No model selected. Click 🗘 to load the list or type the model name.")
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
    """Возвращает список доступных моделей. Заодно проверяет, что ключ рабочий."""
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
