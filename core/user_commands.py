"""
The user's own quick commands. Two kinds:

macro   - lines of existing commands, run one after another as ONE action (one Undo):
              /buffer $1 $2
              /style $last area_km2
          $1 … $9 = the arguments typed after the command, $* = all of them, $last = the layer the previous line
          created (as a whole argument), ${2:100} = argument 2 or 100 when it is not given.
python  - Python code (the same environment as AI code: qgis.core, iface, run_alg, get_layer, style …).
          args = the typed arguments (numbers are numbers), kwargs = key=value options, arg(n, default) = argument n.

Stored in the QGIS profile (ai_copilot_commands.json); export / import to share them.
"""
import datetime
import json
import os
import re
import uuid

from qgis.core import QgsApplication

from . import commands as builtin
from .log import log_warning

FILE_NAME = "ai_copilot_commands.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
MAX_DEPTH = 4                     # a macro may call other user commands, this deep
LAST = "\x00LAST\x00"             # placeholder of $last while a macro line is built
_ARG_RE = re.compile(r"\$\{(\d)(?::([^}]*))?\}|\$(\d|\*|last)")


def default_path():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), FILE_NAME)


EXAMPLES = [
    {"name": "relief", "usage": "/relief <dem layer> [z factor]", "kind": "python",
     "description": "hillshade of a DEM, half transparent (example: Python)",
     "body": 'dem = get_layer(arg(1))\n'
             'result = run_alg("native:hillshade", {"INPUT": dem, "Z_FACTOR": arg(2, 1)}, name=f"{dem.name()} relief")\n'
             'relief = result["LAYERS"][0]\n'
             'relief.renderer().setOpacity(0.5)\n'
             'relief.triggerRepaint()\n'
             'print(f"Relief of {dem.name()} added, 50 % transparent")'},
    {"name": "density", "usage": "/density <polygon layer> <number field>", "kind": "macro",
     "description": "area in km², density per km² and colours (example: list of commands)",
     "body": '/measure $1\n/calc $1 density = round("$2" / "area_km2", 1)\n/style $1 density'},
    {"name": "quickmap", "usage": "/quickmap <place> [title]", "kind": "macro",
     "description": "OpenStreetMap basemap, go to a place, PDF map",
     "body": "/basemap OpenStreetMap\n/zoom $1\n/pdf ${2:Map}"},
    {"name": "osmhex", "usage": "/osmhex <amenity> <place>", "kind": "macro",
     "description": "OpenStreetMap places (cafe, school…) of a city as a hexagon map",
     "body": "/osm amenity $1 place=$2\n/hexbin $last"},
]


class CommandStore:
    def __init__(self, path=None):
        self.path = path or default_path()
        first_start = not os.path.exists(self.path)
        self.items = self._load(self.path)
        if first_start:  # a few examples to start from (they can be edited or deleted)
            self.items = [_clean(dict(e)) for e in EXAMPLES]
            self.save()

    @staticmethod
    def _load(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log_warning(f"User commands could not be read from {path}: {e}")
            return []
        items = data.get("commands", []) if isinstance(data, dict) else data
        return [_clean(i) for i in items if isinstance(i, dict) and NAME_RE.match(str(i.get("name", "")).lower())]

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "commands": self.items}, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log_warning(f"User commands could not be saved: {e}")

    def names(self):
        return sorted(i["name"] for i in self.items)

    def get(self, name):
        name = str(name).strip().lstrip("/").lower()
        return next((i for i in self.items if i["name"] == name), None)

    def put(self, item, old_name=None):
        """Adds or replaces a command (validated). Returns the stored item."""
        item = _clean(item)
        problem = validate(item, self, old_name)
        if problem:
            raise ValueError(problem)
        self.items = [i for i in self.items if i["name"] not in (item["name"], old_name)]
        self.items.append(item)
        self.save()
        return item

    def delete(self, name):
        self.items = [i for i in self.items if i["name"] != name]
        self.save()

    def export(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "commands": self.items}, fh, ensure_ascii=False, indent=1)
        return len(self.items)

    def import_file(self, path):
        """Adds the commands of a file; a name that exists gets the suffix _2, _3 … Returns the count."""
        added = 0
        for item in self._load(path):
            base, n = item["name"], 2
            while self.get(item["name"]) or item["name"] in builtin.COMMANDS:
                item["name"] = f"{base[:27]}_{n}"
                n += 1
            item["id"] = uuid.uuid4().hex
            self.items.append(item)
            added += 1
        self.save()
        return added


def _clean(item):
    body = str(item.get("body", "")).strip("\n")
    kind = item.get("kind") or guess_kind(body)
    return {"id": str(item.get("id") or uuid.uuid4().hex), "name": str(item.get("name", "")).strip().lstrip("/").lower(),
            "usage": str(item.get("usage", "")).strip(), "description": str(item.get("description", "")).strip(),
            "kind": "python" if kind == "python" else "macro", "body": body,
            "updated": str(item.get("updated") or datetime.datetime.now().isoformat(timespec="seconds"))}


def guess_kind(body):
    """Macro when every non-empty line is a /command (comments with # allowed)."""
    lines = [ln.strip() for ln in str(body).splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return "macro" if lines and all(ln.startswith("/") for ln in lines) else "python"


def validate(item, store=None, old_name=None):
    """Error text, or "" when the command is fine."""
    name = item["name"]
    if not NAME_RE.match(name):
        return "Name: 2-31 characters, Latin letters, digits and _, starting with a letter (e.g. relief, buf100)."
    if name in builtin.COMMANDS or name in ("newcommand", "commands"):
        return f"/{name} is a built-in command. Choose another name."
    if store is not None and name != old_name and store.get(name):
        return f"A command /{name} already exists."
    if not item["body"].strip():
        return "The command is empty."
    if item["kind"] == "macro":
        for line in _macro_lines(item["body"]):
            if not line.startswith("/"):
                return f"In a command list every line starts with /: {line[:60]}"
            first = line[1:].split(" ", 1)[0].lower()
            if first == name:
                return "A command cannot call itself."
            if first in builtin.COMMANDS and builtin.COMMANDS[first][1] == "local":
                return f"/{first} cannot be used inside a command list (it opens a window or works on the panel)."
    else:
        try:
            compile(item["body"], f"/{name}", "exec")
        except SyntaxError as e:
            return f"Python syntax error in line {e.lineno}: {e.msg}"
    return ""


def _macro_lines(body):
    return [ln.strip() for ln in str(body).splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _split_args(rest):
    tokens = builtin._tokens(rest) if rest.strip() else []
    positional, options = [], {}
    for token in tokens:
        match = builtin._KEY_VALUE.match(token)
        if match:
            options[match.group(1)] = builtin._value(match.group(2))
        else:
            positional.append(token)
    return tokens, positional, options


def _quote(text):
    text = str(text)
    if text in (LAST, "$last"):
        return LAST
    return text if text and re.match(r"^[^\s\"']+$", text) else '"' + text.replace('"', '\\"') + '"'


def _substitute(line, positional, rest):
    def repl(match):
        if match.group(1):  # ${n:default}
            n, default = int(match.group(1)), match.group(2)
            if n <= len(positional):
                return _quote(positional[n - 1])
            if default is None:
                raise builtin.CommandError(f"Argument {n} is missing.")
            return _quote(default)
        key = match.group(3)
        if key == "last":
            return LAST
        if key == "*":
            return rest
        n = int(key)
        if n > len(positional):
            raise builtin.CommandError(f"Argument {n} is missing.")
        return _quote(positional[n - 1])
    return _ARG_RE.sub(repl, line)


MACRO_PRELUDE = '''
_ai_seen = set(QgsProject.instance().mapLayers())
_last = None
def _ai_newest():
    global _last
    new = [lyr for lid, lyr in QgsProject.instance().mapLayers().items() if lid not in _ai_seen]
    _ai_seen.update(QgsProject.instance().mapLayers())
    if new:
        _last = new[-1].name()
    return _last
'''


def compile_command(item, rest, store, depth=0):
    """The Python code of a user command called with the argument text `rest`. Returns (code, needs_check):
    needs_check is True for Python commands (they may contain risky code), False for lists of built-in commands."""
    if depth > MAX_DEPTH:
        raise builtin.CommandError("Commands call each other too deep (loop?).")
    _tokens, positional, options = _split_args(rest)
    if item["kind"] == "python":
        values = [builtin._value(p) for p in positional]
        listed = repr(values).replace("'$last'", "_last")
        prelude = (f"args = {listed}\nkwargs = {options!r}\n"
                   "def arg(n, default=None):\n    return args[n - 1] if 0 < n <= len(args) else default\n")
        return builtin.HELPERS + "\n" + prelude + item["body"], True
    parts = [builtin.HELPERS, MACRO_PRELUDE]
    check = False
    for line in _macro_lines(item["body"]):
        expanded = _substitute(line, positional, rest)
        name, sub_rest = builtin.split(expanded)
        nested = store.get(name) if store is not None else None
        if nested is not None:
            code, nested_check = compile_command(nested, sub_rest.replace(LAST, "$last"), store, depth + 1)
            check = check or nested_check
            parts.append(code.replace(builtin.HELPERS, "").replace(MACRO_PRELUDE, ""))
        else:
            code, local, _r = builtin.build_call(expanded.replace(LAST, "LASTLAYERPLACEHOLDER"))
            if local:
                raise builtin.CommandError(f"/{local} cannot be used inside a command list.")
            code = code.replace("'LASTLAYERPLACEHOLDER'", "_last").replace('"LASTLAYERPLACEHOLDER"', "_last")
            if "LASTLAYERPLACEHOLDER" in code:
                raise builtin.CommandError("$last works only as a whole argument, e.g. /style $last area.")
            parts.append(f"print({('▸ ' + expanded.replace(LAST, '$last'))!r})\n{code}")
        parts.append("_ai_newest()")
    return "\n".join(parts), check


def help_lines(store):
    """Lines of /help for the user's commands."""
    if not store.items:
        return []
    width = max(len(i["usage"] or "/" + i["name"]) for i in store.items)
    return [f"  {(i['usage'] or '/' + i['name']):<{width}}  {i['description']}" for i in
            sorted(store.items, key=lambda x: x["name"])]


def ai_prompt(name, description):
    """Request to the AI: write the body of a new Python command."""
    return (f"Create a reusable quick command /{name} for this QGIS plugin. What it must do: {description}\n"
            "Answer with exactly two parts:\n"
            f"1. One line: Usage: /{name} <arguments>   (name each argument, e.g. <dem layer> [interval])\n"
            "2. ONE ```python block with the BODY of the command: top-level code, no def wrapper, no example call.\n"
            "The typed arguments are in `args` (list; numbers already converted, layer names are strings) and "
            "`kwargs` (key=value options); arg(n, default) gives argument n (1-based). Use the built-in commands "
            "(run_alg, get_layer, style, export, zoom_to …) where possible; layers by name via get_layer(arg(1)). "
            "Raise ValueError with a clear message for missing or wrong arguments. print() a short result. "
            "Do not run it now and do not write [[CONTINUE]].")


def usage_from_answer(answer, name):
    match = re.search(r"Usage:\s*(/" + re.escape(name) + r"[^\n`]*)", answer)
    return match.group(1).strip() if match else f"/{name}"
