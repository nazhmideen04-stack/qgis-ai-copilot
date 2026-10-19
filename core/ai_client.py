import json
import time
import urllib.request
import urllib.error

SYSTEM_PROMPT = """You are a helpful PyQGIS expert and GIS assistant for modern QGIS 4 (Qt6).

Communication Rules:
1. You CAN and SHOULD talk to the user! Answer their GIS questions, explain your approach, or give advice in plain Russian language.
2. If the user asks to execute an action on the map, write the text explanation, and then MUST wrap the Python code strictly inside a single ```python ... ``` block.
3. NEVER import PyQt5! This is QGIS 4 running Qt6. Use `from qgis.PyQt import QtCore, QtGui, QtWidgets`.
4. NEVER import classes from `qgis.core` or `qgis.utils`! They are already pre-loaded globally (e.g., QgsProject, QgsRasterLayer, QgsVectorLayer, QgsGeometry, QgsField, etc.). Just use them directly!
5. To add a basemap:
   layer = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png", "OpenStreetMap", "wms")
   if layer.isValid(): QgsProject.instance().addMapLayer(layer)
6. Always add newly created layers to QgsProject.instance().
"""

def request_ai_code(provider: str, model_name: str, api_key: str, prompt: str, project_context: str) -> str:
    start_time = time.time()
    user_payload = f"Project Context:\n{project_context}\n\nTask to execute:\n{prompt}"
    clean_model = model_name.replace("models/", "").strip()

    # --- 1. GOOGLE GEMINI ---
    if provider == "Google Gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [
                {"parts": [{"text": f"{SYSTEM_PROMPT}\n\n{user_payload}"}]}
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 8192
            }
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                elapsed = round(time.time() - start_time, 2)
                candidates = data.get("candidates", [])
                if not candidates:
                    raise RuntimeError("Empty response received from Google Gemini API.")
                parts = candidates[0].get("content", {}).get("parts", [])
                code_text = "".join([p.get("text", "") for p in parts if "text" in p and not p.get("thought", False)])
                if not code_text and parts:
                    code_text = parts[0].get("text", "")
                return f"# Generated in {elapsed}s via {clean_model}\n" + code_text
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            try:
                err_json = json.loads(error_body)
                msg = err_json.get("error", {}).get("message", error_body)
            except Exception:
                msg = error_body
            raise RuntimeError(f"Google API Error ({e.code}): {msg}")

    # --- 2. ANTHROPIC CLAUDE ---
    elif provider == "Claude (Anthropic)":
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
        payload = {
            "model": clean_model,
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_payload}],
            "temperature": 0.0
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                elapsed = round(time.time() - start_time, 2)
                return f"# Generated in {elapsed}s via {clean_model}\n" + data["content"][0]["text"]
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            raise RuntimeError(f"Claude API Error ({e.code}): {err_msg}")

    # --- 3. OPENAI / DEEPSEEK ---
    else:
        url = "https://api.deepseek.com/chat/completions" if provider == "DeepSeek" else "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": clean_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_payload}
            ],
            "temperature": 0.0,
            "max_tokens": 2048
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                elapsed = round(time.time() - start_time, 2)
                return f"# Generated in {elapsed}s via {clean_model}\n" + data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            raise RuntimeError(f"{provider} API Error ({e.code}): {err_msg}")
