"""
Smart join of a table (CSV, Excel, any attribute table) to a map layer.

suggest_join(table, layer) compares every field of the table with every field of the layer and finds the key pair
whose values match best. Values are compared after normalisation, so these still match:
  "01" = "1" = 1 = 1.0,  "İzmir" = "izmir" = "IZMIR" = " Izmir ",  "Bayraklı" = "Bayrakli",  "Konak-Merkez" = "konak merkez".
smart_join(...) then
- adds a live QGIS join (Layer Properties → Joins) when the values match exactly, or
- creates a new layer "<layer> + <table>" with the table columns when the keys match only after normalisation
  (a live join cannot fix spelling and number formats),
and prints how many features matched and examples of rows that did not match.
"""
import re
import unicodedata

from qgis.core import (QgsFeatureRequest, QgsField, QgsMapLayerStyle, QgsProject, QgsVectorLayer,
                       QgsVectorLayerJoinInfo)

from .compat import attribute_request
from .qgis_tools import get_layer

MAX_SCAN = 100000          # rows read from each side
MAX_FIELDS = 40            # fields compared per side
KEY_WORDS = ("id", "code", "kod", "kodu", "key", "fid", "gid", "name", "ad", "adi", "isim", "name_en", "il", "ilce",
             "district", "region", "province", "city", "iso", "nuts", "oktmo", "okato", "kato", "zip", "postcode",
             "mahalle", "neighbourhood", "neighborhood", "county", "state", "country", "admin", "район", "код",
             "название", "город", "область")

_TRANSLIT = str.maketrans({"ı": "i", "ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe", "ё": "е"})
_PUNCT = re.compile(r"[\s\-_./,;:'\"()\[\]]+")
_NUMBER = re.compile(r"^[+-]?\d+([.,]\d+)?$")


def _is_null(value):
    if value is None:
        return True
    try:
        return value.isNull()
    except AttributeError:
        return False


def raw_key(value):
    """The value as QGIS compares it in a live join (integers without .0)."""
    if _is_null(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def norm_key(value):
    """Normalised key: numbers without leading zeros / decimals, text without case, accents and punctuation."""
    if _is_null(value):
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    text = str(value).strip()
    if not text:
        return None
    if _NUMBER.match(text):
        number = float(text.replace(",", "."))
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    text = text.casefold().translate(_TRANSLIT)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _PUNCT.sub(" ", text).strip() or None


def _name_key(name):
    return norm_key(name) or ""


def _scan(layer, limit=MAX_SCAN):
    """Values of the first MAX_FIELDS fields: {field name: list of values}."""
    names = [f.name() for f in layer.fields()][:MAX_FIELDS]
    columns = {n: [] for n in names}
    request = attribute_request(layer, names)
    request.setLimit(limit)
    for feature in layer.getFeatures(request):
        for name in names:
            columns[name].append(feature[name])
    return columns


def _name_bonus(table_field, layer_field):
    a, b = _name_key(table_field), _name_key(layer_field)
    if a and a == b:
        return 0.15
    words_a, words_b = set(a.split()), set(b.split())
    if words_a & words_b & set(KEY_WORDS):
        return 0.08
    if (words_a & set(KEY_WORDS)) and (words_b & set(KEY_WORDS)):
        return 0.04
    return 0.0


def suggest_join(table, layer, top=5):
    """Ranks the possible key pairs. Returns a list of dicts, best first:
    {table_field, layer_field, match, exact, matched, total, unmatched, duplicates, score}.
    match = share of table keys found in the layer after normalisation, exact = without normalisation."""
    table, layer = get_layer(table), get_layer(layer)
    t_cols, l_cols = _scan(table), _scan(layer)
    l_sets = {}
    for name, values in l_cols.items():
        norm = {norm_key(v) for v in values} - {None}
        raw = {raw_key(v) for v in values} - {None}
        if len(norm) >= 2 or (len(values) == 1 and norm):
            l_sets[name] = (norm, raw)
    suggestions = []
    for t_name, t_values in t_cols.items():
        t_norm_list = [norm_key(v) for v in t_values]
        t_norm = set(t_norm_list) - {None}
        if not t_norm or (len(t_norm) < 2 and len(t_values) > 1):
            continue
        t_raw = {raw_key(v) for v in t_values} - {None}
        nonnull = [k for k in t_norm_list if k is not None]
        duplicates = len(nonnull) - len(set(nonnull))
        for l_name, (l_norm, l_raw) in l_sets.items():
            common = t_norm & l_norm
            if not common:
                continue
            match = len(common) / len(t_norm)
            exact = len(t_raw & l_raw) / len(t_raw) if t_raw else 0.0
            score = match + _name_bonus(t_name, l_name) - (0.2 if duplicates > len(nonnull) * 0.5 else 0.0)
            unmatched = sorted(t_norm - l_norm)[:5]
            originals = {}
            for v in t_values:
                k = norm_key(v)
                if k in unmatched and k not in originals:
                    originals[k] = str(v)
            suggestions.append({"table_field": t_name, "layer_field": l_name, "match": match, "exact": exact,
                                "matched": len(common), "total": len(t_norm),
                                "unmatched": [originals.get(k, k) for k in unmatched],
                                "duplicates": duplicates, "score": score})
    suggestions.sort(key=lambda s: (-s["score"], -s["matched"]))
    return suggestions[:top]


def describe_suggestions(suggestions):
    lines = []
    for i, s in enumerate(suggestions, 1):
        note = "exact" if s["exact"] >= s["match"] - 1e-9 else f"exact only {s['exact']:.0%}, fixed by normalising"
        lines.append(f"{i}. table.{s['table_field']} ↔ layer.{s['layer_field']}: {s['match']:.0%} of table keys "
                     f"found ({s['matched']}/{s['total']}, {note})"
                     + (f", {s['duplicates']} duplicate key(s)" if s["duplicates"] else ""))
    return "\n".join(lines) or "No matching key fields found."


def smart_join(table, layer, table_field=None, layer_field=None, fields=None, prefix=None, mode="auto", name=None):
    """Joins a table to a map layer.
    table / layer: layer objects or names. Key fields are found automatically when not given.
    fields: table fields to add (default: all except the key). prefix: added to the new field names
    (default "<table name>_"; "" for none). mode: auto | live (QGIS join) | copy (new layer with normalised matching).
    Returns the layer that has the joined fields."""
    table, layer = get_layer(table), get_layer(layer)
    if not isinstance(table, QgsVectorLayer) or not isinstance(layer, QgsVectorLayer):
        raise ValueError("Both the table and the map layer must be vector layers / tables.")
    best = None
    if not table_field or not layer_field:
        suggestions = suggest_join(table, layer)
        if table_field:
            suggestions = [s for s in suggestions if s["table_field"] == table_field]
        if layer_field:
            suggestions = [s for s in suggestions if s["layer_field"] == layer_field]
        if not suggestions:
            raise ValueError(f"No key fields with matching values found between '{table.name()}' and "
                             f"'{layer.name()}'. Table fields: {[f.name() for f in table.fields()]}; "
                             f"layer fields: {[f.name() for f in layer.fields()]}")
        best = suggestions[0]
        table_field, layer_field = best["table_field"], best["layer_field"]
        print("Key candidates:\n" + describe_suggestions(suggestions))
    for lyr, fld in ((table, table_field), (layer, layer_field)):
        if lyr.fields().indexFromName(fld) < 0:
            raise ValueError(f"Field '{fld}' not found in '{lyr.name()}'. Fields: {[f.name() for f in lyr.fields()]}")
    if best is None:
        found = [s for s in suggest_join(table, layer, top=50)
                 if s["table_field"] == table_field and s["layer_field"] == layer_field]
        best = found[0] if found else {"match": 0.0, "exact": 0.0, "matched": 0, "total": 0, "unmatched": [],
                                       "duplicates": 0}
    if fields is None:
        fields = [f.name() for f in table.fields() if f.name() != table_field]
    else:
        fields = [fields] if isinstance(fields, str) else list(fields)
        bad = [f for f in fields if table.fields().indexFromName(f) < 0]
        if bad:
            raise ValueError(f"Table fields {bad} not found. Fields: {[f.name() for f in table.fields()]}")
    prefix = f"{table.name()}_" if prefix is None else str(prefix)
    if mode == "auto":
        mode = "live" if best["exact"] >= best["match"] - 1e-9 else "copy"

    if mode == "live":
        result = _live_join(table, layer, table_field, layer_field, fields, prefix)
    elif mode == "copy":
        result = _copy_join(table, layer, table_field, layer_field, fields, prefix, name)
    else:
        raise ValueError("mode must be 'auto', 'live' or 'copy'.")
    _report(result, layer, table, table_field, layer_field, fields, prefix, mode, best)
    return result


def _live_join(table, layer, table_field, layer_field, fields, prefix):
    info = QgsVectorLayerJoinInfo()
    info.setJoinLayer(table)
    info.setJoinFieldName(table_field)
    info.setTargetFieldName(layer_field)
    info.setUsingMemoryCache(True)
    info.setPrefix(prefix)
    info.setJoinFieldNamesSubset(fields)
    layer.removeJoin(table.id())  # one join per table: replace an older one
    if not layer.addJoin(info):
        raise RuntimeError(f"QGIS could not add the join of '{table.name()}' to '{layer.name()}'.")
    layer.triggerRepaint()
    return layer


def _copy_join(table, layer, table_field, layer_field, fields, prefix, name):
    lookup = {}
    request = attribute_request(table, [table_field] + fields)
    for feature in table.getFeatures(request):
        key = norm_key(feature[table_field])
        if key is not None and key not in lookup:
            lookup[key] = [feature[f] for f in fields]
    result = layer.materialize(QgsFeatureRequest())
    result.setName(name or f"{layer.name()} + {table.name()}")
    provider = result.dataProvider()
    existing = {f.name() for f in result.fields()}
    new_fields = []
    for f in fields:
        field = QgsField(table.fields().field(f))
        target = prefix + f
        while target in existing:
            target += "_1"
        existing.add(target)
        field.setName(target)
        new_fields.append(field)
    provider.addAttributes(new_fields)
    result.updateFields()
    indexes = [result.fields().indexFromName(f.name()) for f in new_fields]
    key_index = result.fields().indexFromName(layer_field)
    changes = {}
    for feature in result.getFeatures(attribute_request(result, [layer_field])):
        values = lookup.get(norm_key(feature.attributes()[key_index]))
        if values is not None:
            changes[feature.id()] = dict(zip(indexes, values))
    if changes:
        provider.changeAttributeValues(changes)
    style = QgsMapLayerStyle()
    style.readFromLayer(layer)
    style.writeToLayer(result)
    QgsProject.instance().addMapLayer(result)
    return result


def _report(result, layer, table, table_field, layer_field, fields, prefix, mode, best):
    key_index = layer.fields().indexFromName(layer_field)
    table_keys = {norm_key(f[table_field]) for f in table.getFeatures(attribute_request(table, [table_field]))}
    table_keys.discard(None)
    total = matched = 0
    for feature in layer.getFeatures(attribute_request(layer, [layer_field])):
        total += 1
        if norm_key(feature.attributes()[key_index]) in table_keys:
            matched += 1
    how = ("live QGIS join (Layer Properties → Joins)" if mode == "live"
           else f"new layer '{result.name()}' (keys matched after normalising spelling / number format)")
    print(f"Joined '{table.name()}'.{table_field} → '{layer.name()}'.{layer_field} as {how}.")
    print(f"Map features with data: {matched} of {total} ({(matched / total if total else 0):.0%}). "
          f"Added fields: {[prefix + f for f in fields][:12]}{' …' if len(fields) > 12 else ''}")
    if best.get("unmatched"):
        print(f"Table keys without a map feature (examples): {best['unmatched']}")
    if best.get("duplicates"):
        print(f"Warning: the table has {best['duplicates']} duplicate key(s); the first row is used.")
