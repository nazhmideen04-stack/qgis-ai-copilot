"""
Field value hints: what is really stored in the fields of a layer (ranges of numbers and dates, the values of text
fields). They are
- added to the project context, so the AI writes filters with real values ("type" = 'residential', not 'Residential');
- printed by field_info(layer) in AI code and by the /fields command;
- shown in the «Fields» window, where a double click puts a field or a value into the request.
Scans are limited (large layers, expression fields are skipped) and cached until the next request.
"""
from qgis.core import QgsVectorLayer

from .compat import is_expression_field
from .log import log_warning

MAX_SCAN_FEATURES = 200000   # larger layers: no value scan (it could take long on file layers)
MAX_FIELDS = 15              # fields with hints per layer in the AI context
MAX_VALUES = 6               # text values shown per field in the AI context
MAX_VALUE_CHARS = 30
MAX_LAYER_HINT_CHARS = 700   # one layer's hint line in the AI context
UNIQUE_SCAN = 41             # distinct values read to decide "few values" vs "many"

_cache = {}


def clear_cache():
    _cache.clear()


def _short(value):
    text = str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1] + "…"


def _is_null(value):
    if value is None:
        return True
    try:
        return value.isNull()  # QVariant NULL
    except AttributeError:
        return False


def _fmt_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _short(value)
    return f"{number:g}" if abs(number) < 1e15 else f"{number:.4g}"


def field_summary(layer, index, unique_limit=UNIQUE_SCAN):
    """One field: {'name', 'type', 'kind': number|text|date|bool|other, 'min', 'max', 'values', 'many'}."""
    field = layer.fields().at(index)
    info = {"name": field.name(), "type": field.typeName(), "kind": "other", "min": None, "max": None,
            "values": [], "many": False}
    if field.isNumeric():
        info["kind"] = "number"
    elif "date" in field.typeName().lower() or "time" in field.typeName().lower():
        info["kind"] = "date"
    elif field.typeName().lower() in ("bool", "boolean"):
        info["kind"] = "bool"
    elif "string" in field.typeName().lower() or "char" in field.typeName().lower() or "text" in field.typeName().lower():
        info["kind"] = "text"
    if is_expression_field(layer, index):
        info["kind"] = "expression"
        return info
    values = [v for v in layer.uniqueValues(index, unique_limit) if not _is_null(v)]
    info["many"] = len(values) >= unique_limit
    if info["kind"] in ("number", "date"):
        low, high = layer.minimumValue(index), layer.maximumValue(index)
        info["min"] = None if _is_null(low) else low
        info["max"] = None if _is_null(high) else high
    info["values"] = sorted(values, key=lambda v: str(v))
    return info


def layer_summaries(layer, max_fields=None):
    """Summaries of the fields of a layer (cached). Empty list for non-vector or too large layers."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return []
    count = layer.featureCount()
    names = tuple(f.name() for f in layer.fields())
    key = (layer.id(), count, names, layer.subsetString())
    if key not in _cache:
        if count > MAX_SCAN_FEATURES or count <= 0:
            _cache[key] = []
        else:
            out = []
            for index in range(len(names)):
                try:
                    out.append(field_summary(layer, index))
                except Exception as e:  # a single odd field must not break the context
                    log_warning(f"Field hints: '{layer.name()}.{names[index]}' skipped: {e}")
            _cache[key] = out
    summaries = _cache[key]
    return summaries[:max_fields] if max_fields else summaries


def describe_field(info, max_values=MAX_VALUES):
    """Short hint text for one field, e.g. "type: 'farm'|'forest'|'urban'" or "pop: 12..53400"."""
    name = info["name"]
    if info["kind"] == "expression":
        return ""
    values = info["values"]
    if info["kind"] in ("number", "date") and info["min"] is not None:
        span = (f"{_fmt_number(info['min'])}..{_fmt_number(info['max'])}" if info["kind"] == "number"
                else f"{_short(info['min'])}..{_short(info['max'])}")
        if not info["many"] and len(values) <= 8 and info["kind"] == "number":
            return f"{name}: " + "|".join(_fmt_number(v) for v in values)
        return f"{name}: {span}"
    if not values:
        return f"{name}: (empty)"
    shown = "|".join(f"'{_short(v)}'" for v in values[:max_values])
    if info["many"]:
        return f"{name}: many values, e.g. {shown}"
    more = f"|… ({len(values)} values)" if len(values) > max_values else ""
    return f"{name}: {shown}{more}"


def context_hint(layer):
    """One line for the AI context: real values of the layer's fields."""
    if isinstance(layer, QgsVectorLayer) and layer.featureCount() > MAX_SCAN_FEATURES:
        return "  field values: (large layer, not scanned — use field_info() if needed)"
    parts = [describe_field(info) for info in layer_summaries(layer, MAX_FIELDS)]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    line = "  field values: " + "; ".join(parts)
    if len(line) > MAX_LAYER_HINT_CHARS:
        line = line[:MAX_LAYER_HINT_CHARS - 1] + "…"
    return line


def field_info(layer, field=None, values=30):
    """Prints the real values of a layer's fields (ranges, distinct values). field=None: all fields."""
    from .qgis_tools import get_layer
    layer = get_layer(layer)
    if not isinstance(layer, QgsVectorLayer):
        raise ValueError(f"'{layer.name()}' is not a vector layer.")
    fields = layer.fields()
    indexes = [fields.indexFromName(field)] if field else list(range(fields.count()))
    if field and indexes[0] < 0:
        raise ValueError(f"Field '{field}' not found. Fields: {[f.name() for f in fields]}")
    print(f"Layer '{layer.name()}': {layer.featureCount()} features, {fields.count()} fields")
    for index in indexes:
        info = field_summary(layer, index, unique_limit=max(values + 1, UNIQUE_SCAN))
        print("  " + (describe_field(info, max_values=values) or f"{info['name']}: (expression field)")
              + f"   [{info['type']}]")
    return layer
