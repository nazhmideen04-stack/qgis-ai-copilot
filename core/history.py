"""
Request archive and conversation memory, saved in the QGIS user profile (ai_copilot_history.json).

- Archive: every request with the time, provider, model, the AI answer and the result of the code (OK / error).
  The user can browse it, search it and send an old request again.
- Memory: the entries of the current conversation are what the AI remembers. The memory survives a QGIS restart;
  "Clear" starts a new conversation (the archive keeps the old entries).
API keys are never stored here.
"""
import datetime
import json
import os
import uuid

from qgis.core import QgsApplication

from .log import log_warning

FILE_NAME = "ai_copilot_history.json"
MAX_ENTRIES = 300          # oldest entries are removed beyond this
MAX_ANSWER_CHARS = 8000    # stored part of an AI answer


def default_path():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), FILE_NAME)


class RequestArchive:
    def __init__(self, path=None):
        self.path = path or default_path()
        self.data = self._load()

    # ------------------------------------------------------------------ storage
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                data.setdefault("conversation", uuid.uuid4().hex)
                return data
        except FileNotFoundError:
            pass  # first start: no archive yet
        except (OSError, ValueError) as e:
            log_warning(f"Request archive could not be read, a new one is started: {e}")
        return {"conversation": uuid.uuid4().hex, "entries": []}

    def save(self):
        self.data["entries"] = self.data["entries"][-MAX_ENTRIES:]
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)  # atomic: the archive is never half-written
        except OSError as e:
            log_warning(f"Request archive could not be saved: {e}")

    # ------------------------------------------------------------------ entries
    def add(self, prompt, answer="", provider="", model="", in_memory=True, result=""):
        entry = {
            "id": uuid.uuid4().hex,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "conversation": self.data["conversation"],
            "prompt": str(prompt),
            "answer": str(answer)[:MAX_ANSWER_CHARS],
            "provider": provider,
            "model": model,
            "result": result,
            "ok": None if not result else False,
            "in_memory": bool(in_memory),
        }
        self.data["entries"].append(entry)
        self.save()
        return entry

    def update(self, entry_id, result, ok):
        for entry in self.data["entries"]:
            if entry["id"] == entry_id:
                entry["result"], entry["ok"] = str(result)[:MAX_ANSWER_CHARS], bool(ok)
                self.save()
                return entry
        return None

    def entries(self):
        """All entries, newest first."""
        return list(reversed(self.data["entries"]))

    def delete(self, entry_id):
        self.data["entries"] = [e for e in self.data["entries"] if e["id"] != entry_id]
        self.save()

    def clear(self):
        self.data["entries"] = []
        self.save()

    # ------------------------------------------------------------------ conversation memory
    def new_conversation(self):
        self.data["conversation"] = uuid.uuid4().hex
        self.save()

    def conversation_entries(self, limit, max_age_hours=None):
        """The last `limit` remembered entries of the current conversation, oldest first.
        With max_age_hours: if the conversation's last message is older, a new conversation is started instead
        (an old, finished dialog would only make the requests longer and slower)."""
        current = [e for e in self.data["entries"]
                   if e.get("conversation") == self.data["conversation"] and e.get("in_memory")]
        if current and max_age_hours is not None:
            try:
                last = datetime.datetime.fromisoformat(current[-1]["time"])
            except (KeyError, ValueError):
                last = None
            if last is None or datetime.datetime.now() - last > datetime.timedelta(hours=max_age_hours):
                self.new_conversation()
                return []
        return current[-limit:]
