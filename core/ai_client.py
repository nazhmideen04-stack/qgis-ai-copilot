import json
import urllib.request
import urllib.error
import socket

# Hard limit for all network connections (prevents infinite hanging)
socket.setdefaulttimeout(60)

SYSTEM_PROMPT = """You are an expert PyQGIS developer for QGIS 4 (Qt6).

CRITICAL EXECUTION RULES:
1. OUTPUT ONLY PURE PYTHON CODE. NO EXPLANATIONS OUTSIDE OF CODE. NO ```python TAGS.
2. ALWAYS include standard imports if needed (import os, random, math).
3. NEVER import PyQt5! Use `from qgis.PyQt import QtCore, QtGui, QtWidgets`.
4. NEVER import classes from `qgis.core`, `qgis.gui`, `qgis.analysis`, `osgeo` or `numpy`! They are already pre-loaded globally.

EXCEPTION RULE:
If the request is too complex, impossible, or requires manual GUI steps (like the QuickOSM plugin), DO NOT write broken code. 
Instead, reply ONLY with Python comments (every line starting with #) explaining the issue in English.
"""

def request_ai_code(provider, model, api_key, prompt, context=""):
    full_prompt = f"{context}\n\nUser Request:\n{prompt}"
    
    try:
        # --- GOOGLE GEMINI ---
        if provider == "Google Gemini":
            url = f"[https://generativelanguage.googleapis.com/v1beta/models/](https://generativelanguage.googleapis.com/v1beta/models/){model}:generateContent?key={api_key}"
            data = {
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": full_prompt}]}]
            }
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data["candidates"][0]["content"]["parts"][0]["text"]
                
        # --- OLLAMA (Local) ---
        elif provider == "Ollama (Local)":
            url = "http://localhost:11434/api/generate"
            data = {
                "model": model,
                "system": SYSTEM_PROMPT,
                "prompt": full_prompt,
                "stream": False
            }
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp: # Local AI gets more time for "cold start"
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data["response"]
                
        # --- OPENAI ---
        elif provider == "OpenAI":
            url = "[https://api.openai.com/v1/chat/completions](https://api.openai.com/v1/chat/completions)"
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": full_prompt}
                ]
            }
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data["choices"][0]["message"]["content"]
                
        # --- DEEPSEEK ---
        elif provider == "DeepSeek":
            url = "[https://api.deepseek.com/chat/completions](https://api.deepseek.com/chat/completions)"
            data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": full_prompt}
                ]
            }
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data["choices"][0]["message"]["content"]
                
        # --- CLAUDE (Anthropic) ---
        elif provider == "Claude (Anthropic)":
            url = "[https://api.anthropic.com/v1/messages](https://api.anthropic.com/v1/messages)"
            data = {
                "model": model,
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": full_prompt}]
            }
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data["content"][0]["text"]
                
        else:
            raise Exception(f"Provider {provider} is not supported.")

    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        raise Exception(f"Server Error ({e.code}): {err_msg}")
    except urllib.error.URLError as e:
        raise Exception(f"Connection failed (check internet or timeout): {e.reason}")
    except socket.timeout:
        raise Exception("Request timed out. The server took too long to respond.")
    except Exception as e:
        raise Exception(f"API Internal Error: {str(e)}")
