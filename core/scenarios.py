"""
Saved scenarios: a request and (optionally) the code that solved it, kept under a name.
A scenario can be run again without the AI (its saved code: instant, free, the same result every time),
sent to the AI again, or put into the request box. Stored in the QGIS profile (ai_copilot_scenarios.json);
scenarios can be exported to / imported from a file to share them.
"""
import datetime
import json
import os
import uuid

from qgis.core import QgsApplication

from .log import log_warning

FILE_NAME = "ai_copilot_scenarios.json"


def default_path():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), FILE_NAME)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class ScenarioStore:
    def __init__(self, path=None):
        self.path = path or default_path()
        self.items = self._load(self.path)

    @staticmethod
    def _load(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log_warning(f"Scenarios could not be read from {path}: {e}")
            return []
        items = data.get("scenarios", []) if isinstance(data, dict) else data
        return [_clean(i) for i in items if isinstance(i, dict) and i.get("name")]

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "scenarios": self.items}, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log_warning(f"Scenarios could not be saved: {e}")

    def names(self):
        return [i["name"] for i in self.items]

    def get(self, name):
        wanted = str(name).strip().lower()
        return next((i for i in self.items if i["name"].lower() == wanted), None)

    def add(self, name, prompt, steps=None, provider="", model=""):
        """Adds a scenario or replaces the one with the same name."""
        existing = self.get(name)
        item = _clean({"id": existing["id"] if existing else uuid.uuid4().hex, "name": name, "prompt": prompt,
                       "steps": list(steps or []), "provider": provider, "model": model,
                       "created": existing["created"] if existing else _now(), "updated": _now()})
        if existing:
            self.items[self.items.index(existing)] = item
        else:
            self.items.append(item)
        self.save()
        return item

    def rename(self, scenario_id, new_name):
        for item in self.items:
            if item["id"] == scenario_id:
                item["name"], item["updated"] = new_name, _now()
        self.save()

    def delete(self, scenario_id):
        self.items = [i for i in self.items if i["id"] != scenario_id]
        self.save()

    def export(self, path, ids=None):
        chosen = [i for i in self.items if ids is None or i["id"] in ids]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "scenarios": chosen}, fh, ensure_ascii=False, indent=1)
        return len(chosen)

    def import_file(self, path):
        """Adds the scenarios of a file; same names get the suffix " (imported)". Returns the count."""
        added = 0
        for item in self._load(path):
            name = item["name"]
            while self.get(name):
                name += " (imported)"
            item.update(id=uuid.uuid4().hex, name=name)
            self.items.append(item)
            added += 1
        self.save()
        return added


def _clean(item):
    return {"id": str(item.get("id") or uuid.uuid4().hex), "name": str(item.get("name", "")).strip()[:120],
            "prompt": str(item.get("prompt", "")), "steps": [str(s) for s in item.get("steps", []) if str(s).strip()],
            "provider": str(item.get("provider", "")), "model": str(item.get("model", "")),
            "created": str(item.get("created", "")), "updated": str(item.get("updated", ""))}
