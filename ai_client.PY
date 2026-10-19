import json
import urllib.request
import urllib.error

SYSTEM_PROMPT = """You are an expert PyQGIS developer and GIS analyst.
Your task is to write working Python code using the PyQGIS API to satisfy the user's request.

Rules:
1. Return ONLY pure Python code wrapped in ```python ... ``` markdown tags.
2. Do not include any explanations, greetings, or text outside the code block.
3. Available globals in scope: `iface` (QgisInterface), `QgsProject`, and `processing`.
4. If creating a temporary layer, add it to the project: QgsProject.instance().addMapLayer(layer)
5. Ensure CRS compatibility and verify layer validity.
"""

def request_ai_code(provider: str, api_key: str, prompt: str, project_context: str) -> str:
    user_content = f"CURRENT QGIS PROJECT CONTEXT:\n{project_context}\n\nUSER REQUEST:\n{prompt}"
    
    # --- 1. GOOGLE GEMINI ---
    if provider == "Google Gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\n{user_content}"}]}],
            "generationConfig": {"temperature": 0.1}
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            raise RuntimeError(f"Google API Error ({e.code}): {err_msg}")

    # --- 2. ANTHROPIC CLAUDE ---
    elif provider == "Claude (Anthropic)":
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
        payload = {
            "model": "claude-3-5-sonnet-latest",
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": 0.1
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["content"][0]["text"]
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            raise RuntimeError(f"Claude API Error ({e.code}): {err_msg}")

    # --- 3. OPENAI / DEEPSEEK ---
    else:
        model_name = "deepseek-chat" if provider == "DeepSeek" else "gpt-4o"
        url = "https://api.deepseek.com/chat/completions" if provider == "DeepSeek" else "https://api.openai.com/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.1
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            raise RuntimeError(f"{provider} API Error ({e.code}): {err_msg}")