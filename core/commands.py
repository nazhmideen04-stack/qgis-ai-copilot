"""
Commands typed in the request box: they start with "/" and run at once, WITHOUT the AI and without writing code
(no tokens, no waiting). Example: /buffer Roads 100, /zoom Izmir, /alg native:centroids INPUT=Parcels.
Each command is turned into one call of a plugin command (shown in the chat) and runs like AI code:
with progress, Stop and Undo.
"""
import ast
import re
import shlex

# name: (function, how arguments are read, usage, description)
#   "text"   - everything after the command is one text argument
#   "args"   - positional arguments and key=value options
#   "params" - first argument + KEY=VALUE pairs collected into a parameters dict (Processing)
#   "local"  - handled by the panel itself (undo, dialogs)
COMMANDS = {
    "help": (None, "local", "/help", "list of commands"),
    "undo": (None, "local", "/undo", "undo the last AI action"),
    "join": (None, "local", "/join [table] [layer]", "smart join of a table to a map layer (window if no names)"),
    "fields": (None, "local", "/fields [layer]", "real values of the fields (window if no layer)"),
    "scenario": (None, "local", "/scenario <name>", "run a saved scenario without the AI"),
    "scenarios": (None, "local", "/scenarios", "open saved scenarios"),
    "commands": (None, "local", "/commands", "your own commands: create, edit, share"),
    "newcommand": (None, "local", "/newcommand <name> <what it does>", "the AI writes a new command for you"),
    "alg": ("run_alg", "params", "/alg <algorithm id> KEY=VALUE …", "run any Processing algorithm"),
    "find": ("find_alg", "text", "/find <word or algorithm id>", "search algorithms / show parameters"),
    "buffer": ("_buffer", "args", "/buffer <layer> <distance> [segments=8] [dissolve=True]", "buffer a layer"),
    "clip": ("_clip", "args", "/clip <layer> <mask layer>", "clip a layer by a mask"),
    "centroids": ("_centroids", "args", "/centroids <layer>", "centroids of the features"),
    "dissolve": ("_dissolve", "args", "/dissolve <layer> [field]", "dissolve features (by a field)"),
    "reproject": ("_reproject", "args", "/reproject <layer> <EPSG:xxxx>", "reproject a layer"),
    "filter": ("_filter", "args", "/filter <layer> <expression>", "show only features matching an expression"),
    "select": ("_select", "args", "/select <layer> <expression>", "select features by an expression"),
    "info": ("_info", "text", "/info <layer>", "overview: features, geometry, CRS, extent, number fields"),
    "stats": ("_stats", "args", "/stats <layer> <field>", "statistics of a field (numbers or top values of text)"),
    "count": ("_count", "args", "/count <layer> [expression]", "how many features (match an expression)"),
    "top": ("_top", "args", "/top <layer> <field> [n=10] [asc=False]", "select and list the N largest values"),
    "calc": ("_calc", "args", "/calc <layer> <new field> = <expression>", "calculate a field (field calculator)"),
    "measure": ("_measure", "text", "/measure <layer>", "add area (km²) or length (km) of every feature"),
    "heatmap": ("_heatmap", "args", "/heatmap <point layer> [radius]", "heatmap (kernel density) of points"),
    "hexbin": ("_hexbin", "args", "/hexbin <point layer> [cell size]", "hexagon grid with the number of points"),
    "nearest": ("_nearest", "args", "/nearest <layer> <other layer>", "distance to and attributes of the nearest"),
    "intersect": ("_intersect", "args", "/intersect <layer> <other layer>", "intersection of two layers"),
    "merge": ("_merge", "args", "/merge <layer> <layer> […]", "merge several layers into one"),
    "fix": ("_fix", "text", "/fix <layer>", "repair invalid geometries"),
    "csv": ("_csv", "args", "/csv <layer> [path]", "save the attribute table as CSV / Excel"),
    "pdf": ("_pdf", "args", "/pdf [title] [path]", "print layout of the current map exported to PDF"),
    "screenshot": ("_screenshot", "args", "/screenshot [path]", "save the current map view as an image"),
    "style": ("style", "args", "/style <layer> <field> [labels=field]", "colour a layer by a field"),
    "labels": ("_labels", "args", "/labels <layer> <field or expression>", "label features"),
    "zoom": ("zoom_to", "text", "/zoom <place | lat, lon | layer>", "go to a place, coordinates or a layer"),
    "basemap": ("add_basemap", "text", "/basemap <name>", "add a basemap (OpenStreetMap, Esri World Imagery…)"),
    "osm": ("download_osm", "args", "/osm <key> [value] [place=…]", "download OpenStreetMap data"),
    "load": ("load", "args", "/load [path or URL]", "add a layer from a file or service"),
    "layout": ("create_layout", "text", "/layout <title>", "print layout of the current map"),
    "export": ("export", "args", "/export <layout | layer | map | project | all> [path]", "export to a file"),
    "action": ("run_action", "text", "/action <menu item>", "press any QGIS / plugin button or menu item"),
    "actions": ("find_action", "text", "/actions <word>", "find QGIS / plugin buttons and menu items"),
    "plugin": ("plugin", "text", "/plugin [name]", "list plugins / show what a plugin offers"),
}

# Short commands built from Processing algorithms (layers by name, results added to the map)
HELPERS = '''
def _buffer(layer, distance, segments=8, dissolve=False):
    return run_alg("native:buffer", {"INPUT": layer, "DISTANCE": distance, "SEGMENTS": segments,
                                     "DISSOLVE": dissolve}, name=f"{get_layer(layer).name()} buffer {distance}")
def _clip(layer, mask):
    return run_alg("native:clip", {"INPUT": layer, "OVERLAY": mask}, name=f"{get_layer(layer).name()} clipped")
def _centroids(layer):
    return run_alg("native:centroids", {"INPUT": layer}, name=f"{get_layer(layer).name()} centroids")
def _dissolve(layer, field=None):
    params = {"INPUT": layer}
    if field:
        params["FIELD"] = [field]
    return run_alg("native:dissolve", params, name=f"{get_layer(layer).name()} dissolved")
def _reproject(layer, crs):
    return run_alg("native:reprojectlayer", {"INPUT": layer, "TARGET_CRS": QgsCoordinateReferenceSystem(str(crs))},
                   name=f"{get_layer(layer).name()} {crs}")
def _filter(layer, expression=""):
    lyr = get_layer(layer)
    if not lyr.setSubsetString(expression):
        raise ValueError(f"Invalid filter for '{lyr.name()}': {expression}")
    print(f"Filter on '{lyr.name()}': {expression or '(none)'} -> {lyr.featureCount()} features")
def _select(layer, expression):
    lyr = get_layer(layer)
    lyr.selectByExpression(expression)
    print(f"Selected {lyr.selectedFeatureCount()} of {lyr.featureCount()} features in '{lyr.name()}'")
def _labels(layer, field):
    style(layer, labels=field)
def _numbers(lyr, field):
    idx = lyr.fields().indexFromName(field)
    if idx < 0:
        raise ValueError(f"Field '{field}' not found. Fields: {[f.name() for f in lyr.fields()]}")
    req = QgsFeatureRequest().setSubsetOfAttributes([idx])
    return idx, [f.attributes()[idx] for f in lyr.getFeatures(req)]
def _empty(v):
    return v is None or (hasattr(v, "isNull") and v.isNull()) or v == ""
def _info(layer):
    lyr = get_layer(layer)
    print(f"Layer '{lyr.name()}' — {lyr.crs().authid()} ({lyr.crs().description()})")
    if not isinstance(lyr, QgsVectorLayer):
        print(f"Raster: {lyr.width()} x {lyr.height()} px, {lyr.bandCount()} band(s), extent {lyr.extent().toString(2)}")
        return
    print(f"Features: {lyr.featureCount()} ({QgsWkbTypes.displayString(lyr.wkbType())}), "
          f"selected: {lyr.selectedFeatureCount()}, filter: {lyr.subsetString() or 'none'}")
    print(f"Extent: {lyr.extent().toString(2)}")
    print(f"Source: {lyr.source()[:200]}")
    field_info(lyr, values=8)
def _stats(layer, field):
    lyr = get_layer(layer)
    idx, values = _numbers(lyr, field)
    filled = [v for v in values if not _empty(v)]
    print(f"'{lyr.name()}'.{field}: {len(values)} features, {len(values) - len(filled)} empty")
    if lyr.fields().at(idx).isNumeric():
        nums = sorted(float(v) for v in filled)
        if not nums:
            return
        mean = sum(nums) / len(nums)
        mid = len(nums) // 2
        median = nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2
        std = (sum((x - mean) ** 2 for x in nums) / len(nums)) ** 0.5
        print(f"  sum {sum(nums):,.4g} · mean {mean:,.4g} · median {median:,.4g} · min {nums[0]:,.4g} · "
              f"max {nums[-1]:,.4g} · std {std:,.4g}")
    else:
        counts = {}
        for v in filled:
            counts[str(v)] = counts.get(str(v), 0) + 1
        print(f"  {len(counts)} distinct values; most common:")
        for value, n in sorted(counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:>7}  {value}")
def _count(layer, expression=""):
    lyr = get_layer(layer)
    if not expression:
        print(f"'{lyr.name()}': {lyr.featureCount()} features")
        return lyr.featureCount()
    expr = QgsExpression(expression)
    if expr.hasParserError():
        raise ValueError(f"Invalid expression: {expr.parserErrorString()}")
    n = sum(1 for _f in lyr.getFeatures(QgsFeatureRequest(expr)))
    print(f"'{lyr.name()}': {n} of {lyr.featureCount()} features match {expression}")
    return n
def _top(layer, field, n=10, asc=False):
    lyr = get_layer(layer)
    idx, _v = _numbers(lyr, field)
    feats = [f for f in lyr.getFeatures() if not _empty(f.attributes()[idx])]
    feats.sort(key=lambda f: f.attributes()[idx], reverse=not asc)
    chosen = feats[:int(n)]
    lyr.selectByIds([f.id() for f in chosen])
    label = next((fl.name() for fl in lyr.fields() if fl.name().lower() in ("name", "ad", "adi", "title", "название")),
                 None)
    print(f"{'Smallest' if asc else 'Largest'} {len(chosen)} by {field} (selected on the map):")
    for i, f in enumerate(chosen, 1):
        print(f"  {i:>3}. {f.attributes()[idx]}" + (f"  {f[label]}" if label else f"  (id {f.id()})"))
def _calc(layer, text):
    if "=" not in text:
        raise ValueError("Usage: /calc <layer> <new field> = <expression>")
    field, expression = (p.strip() for p in text.split("=", 1))
    field = field.strip('"')
    lyr = get_layer(layer)
    expr = QgsExpression(expression)
    if expr.hasParserError():
        raise ValueError(f"Invalid expression: {expr.parserErrorString()}")
    ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    da = QgsDistanceArea()
    da.setSourceCrs(lyr.crs(), QgsProject.instance().transformContext())
    da.setEllipsoid(QgsProject.instance().ellipsoid() or "EPSG:7030")
    expr.setGeomCalculator(da)
    expr.setDistanceUnits(QgsProject.instance().distanceUnits())
    expr.setAreaUnits(QgsProject.instance().areaUnits())
    expr.prepare(ctx)
    results = {}
    for f in lyr.getFeatures():
        ctx.setFeature(f)
        results[f.id()] = expr.evaluate(ctx)
        if expr.hasEvalError():
            raise ValueError(f"Expression error: {expr.evalErrorString()}")
    sample = next((v for v in results.values() if not _empty(v)), None)
    kind = "double" if isinstance(sample, (int, float)) and not isinstance(sample, bool) else "string"
    if isinstance(sample, int) and not isinstance(sample, bool) and all(
            isinstance(v, int) or _empty(v) for v in results.values()):
        kind = "int"
    was_editing = lyr.isEditable()
    if not was_editing and not lyr.startEditing():
        raise RuntimeError(f"'{lyr.name()}' cannot be edited.")
    idx = lyr.fields().indexFromName(field)
    if idx < 0:
        lyr.addAttribute(new_field(field, kind))
        lyr.updateFields()
        idx = lyr.fields().indexFromName(field)
    for fid, value in results.items():
        lyr.changeAttributeValue(fid, idx, value)
    if not was_editing and not lyr.commitChanges():
        errors = lyr.commitErrors()
        lyr.rollBack()
        raise RuntimeError(f"Could not save: {errors}")
    print(f"'{lyr.name()}'.{field} = {expression}  → {len(results)} features ({kind})")
def _measure(layer):
    lyr = get_layer(layer)
    gtype = QgsWkbTypes.geometryDisplayString(lyr.geometryType())
    if gtype == "Polygon":
        _calc(lyr, "area_km2 = round($area / 1000000, 4)")
    elif gtype == "Line":
        _calc(lyr, "length_km = round($length / 1000, 4)")
    else:
        raise ValueError("/measure works with polygon or line layers.")
def _extent_text(lyr):
    e = lyr.extent()
    return f"{e.xMinimum()},{e.xMaximum()},{e.yMinimum()},{e.yMaximum()} [{lyr.crs().authid()}]"
def _heatmap(layer, radius=None):
    lyr = get_layer(layer)
    e = lyr.extent()
    radius = float(radius) if radius else max(e.width(), e.height()) / 25 or 1
    return run_alg("qgis:heatmapkerneldensityestimation", {"INPUT": lyr, "RADIUS": radius,
                   "PIXEL_SIZE": radius / 10}, name=f"{lyr.name()} heatmap")
def _hexbin(layer, size=None):
    lyr = get_layer(layer)
    e = lyr.extent()
    size = float(size) if size else max(e.width(), e.height()) / 30 or 1
    grid = run_alg("native:creategrid", {"TYPE": 4, "EXTENT": _extent_text(lyr), "HSPACING": size,
                   "VSPACING": size, "CRS": lyr.crs()}, load=False)["OUTPUT"]
    counted = run_alg("native:countpointsinpolygon", {"POLYGONS": grid, "POINTS": lyr, "FIELD": "NUMPOINTS"},
                      load=False)["OUTPUT"]
    result = run_alg("native:extractbyexpression", {"INPUT": counted, "EXPRESSION": '"NUMPOINTS" > 0'},
                     name=f"{lyr.name()} hexbins")
    style(result["LAYERS"][0], "NUMPOINTS", mode="graduated")
    return result
def _nearest(layer, other):
    return run_alg("native:joinbynearest", {"INPUT": layer, "INPUT_2": other, "NEIGHBORS": 1},
                   name=f"{get_layer(layer).name()} + nearest {get_layer(other).name()}")
def _intersect(layer, other):
    return run_alg("native:intersection", {"INPUT": layer, "OVERLAY": other},
                   name=f"{get_layer(layer).name()} ∩ {get_layer(other).name()}")
def _merge(*layers):
    if len(layers) < 2:
        raise ValueError("Usage: /merge <layer> <layer> […]")
    return run_alg("native:mergevectorlayers", {"LAYERS": [get_layer(l) for l in layers]}, name="Merged")
def _fix(layer):
    return run_alg("native:fixgeometries", {"INPUT": layer}, name=f"{get_layer(layer).name()} fixed")
def _csv(layer, path=None):
    lyr = get_layer(layer)
    if not path:
        path, _f = QFileDialog.getSaveFileName(iface.mainWindow(), "AI Copilot", f"{lyr.name()}.csv",
                                               "CSV (*.csv);;Excel (*.xlsx);;LibreOffice (*.ods)")
        if not path:
            print("Cancelled.")
            return None
    return export(lyr, path)
def _pdf(title=None, path=None):
    layout = create_layout(title or QgsProject.instance().baseName() or "Map")
    return export(layout, path)
def _screenshot(path=None):
    return export("map", path)
'''

_KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


class CommandError(ValueError):
    pass


def is_command(text):
    return str(text).lstrip().startswith("/")


def _value(text):
    """Python literal if possible (100, 2.5, True, [1, 2], 'x'), otherwise the text itself."""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def split(text):
    """'/buffer "Main roads" 100' -> ('buffer', 'rest of the line')."""
    text = str(text).strip()[1:]
    name, _sep, rest = text.partition(" ")
    return name.strip().lower(), rest.strip()


def _tokens(rest):
    try:
        return shlex.split(rest, posix=True)
    except ValueError as e:
        raise CommandError(f"Check the quotes: {e}")


def _first_token(rest):
    """'"Main roads" "type" = 1' -> ('Main roads', '"type" = 1'); 'Roads x > 1' -> ('Roads', 'x > 1')."""
    rest = rest.strip()
    if rest[:1] in ("'", '"'):
        end = rest.find(rest[0], 1)
        if end > 0:
            return rest[1:end], rest[end + 1:].strip()
    first, _sep, remainder = rest.partition(" ")
    return first, remainder.strip()


def build_call(text):
    """Turns a typed command into Python code. Returns (code, local_name, argument text).
    For local commands code is "" and the panel handles local_name."""
    name, rest = split(text)
    if name not in COMMANDS:
        close = [n for n in COMMANDS if n.startswith(name[:2])] if name else []
        raise CommandError(f"Unknown command /{name}." + (f" Did you mean: {', '.join('/' + c for c in close)}?"
                                                          if close else " Type /help for the list."))
    func, style, usage, _desc = COMMANDS[name]
    if style == "local":
        return "", name, rest
    if style == "text":
        if not rest and name not in ("plugin", "actions"):
            raise CommandError(f"Usage: {usage}")
        args = [repr(rest.strip("\"'"))] if rest else []
        return f"{func}({', '.join(args)})", None, rest
    if name in ("filter", "select", "calc", "count"):  # the expression is taken as typed (quotes are kept)
        layer, expression = _first_token(rest)
        if not layer or (name in ("select", "calc") and not expression):
            raise CommandError(f"Usage: {usage}")
        return f"{func}({layer!r}, {expression!r})", None, rest
    tokens = _tokens(rest)
    positional, options = [], {}
    for token in tokens:
        match = _KEY_VALUE.match(token)
        if match:
            options[match.group(1)] = _value(match.group(2))
        else:
            positional.append(_value(token))
    if style == "params":
        if not positional:
            raise CommandError(f"Usage: {usage}")
        alg_id = str(positional[0])
        extra = {k: options.pop(k) for k in ("load", "name") if k in options}
        call = f"run_alg({alg_id!r}, {options!r}" + "".join(f", {k}={v!r}" for k, v in extra.items()) + ")"
        return call, None, rest
    if name in ("buffer", "clip", "reproject", "style", "labels") and not positional:
        raise CommandError(f"Usage: {usage}")
    args = [repr(p) for p in positional] + [f"{k}={v!r}" for k, v in options.items()]
    return f"{func}({', '.join(args)})", None, rest


GROUPS = [
    ("Panel", "Панель", ["help", "undo", "scenario", "scenarios", "commands", "newcommand", "fields", "join"]),
    ("Look at the data", "Изучить данные", ["info", "stats", "count", "top"]),
    ("Calculate", "Вычислить", ["calc", "measure", "filter", "select"]),
    ("Geoprocessing", "Геообработка", ["buffer", "clip", "intersect", "merge", "dissolve", "centroids", "nearest",
                                       "reproject", "fix", "alg", "find"]),
    ("Maps and analysis", "Карты и анализ", ["heatmap", "hexbin", "style", "labels", "zoom", "basemap"]),
    ("Data and export", "Данные и экспорт", ["load", "osm", "csv", "pdf", "screenshot", "layout", "export"]),
    ("QGIS and plugins", "QGIS и модули", ["action", "actions", "plugin"]),
]

RU_DESC = {
    "help": "список команд", "undo": "отменить последнее действие ИИ",
    "join": "умное соединение таблицы со слоем карты (окно, если без названий)",
    "fields": "реальные значения полей (окно, если без слоя)", "scenario": "запустить сохранённый сценарий без ИИ",
    "scenarios": "открыть сохранённые сценарии",
    "commands": "свои команды: создать, изменить, поделиться", "newcommand": "ИИ напишет для вас новую команду",
    "alg": "запустить любой алгоритм Processing",
    "find": "найти алгоритм / показать его параметры", "buffer": "буфер вокруг объектов слоя",
    "clip": "обрезать слой по маске", "centroids": "центроиды объектов", "dissolve": "объединить объекты (по полю)",
    "reproject": "перепроецировать слой", "filter": "показать только объекты по выражению",
    "select": "выделить объекты по выражению",
    "info": "обзор: объекты, геометрия, СК, охват, значения полей",
    "stats": "статистика поля (числа или частые значения текста)",
    "count": "сколько объектов (подходят под выражение)", "top": "выделить и показать N наибольших значений",
    "calc": "вычислить поле (калькулятор полей)", "measure": "добавить площадь (км²) или длину (км) объектов",
    "heatmap": "тепловая карта (плотность) точек", "hexbin": "сетка шестиугольников с числом точек",
    "nearest": "расстояние до ближайшего объекта и его атрибуты", "intersect": "пересечение двух слоёв",
    "merge": "объединить несколько слоёв в один", "fix": "исправить ошибки геометрии",
    "csv": "сохранить таблицу атрибутов в CSV / Excel", "pdf": "макет текущей карты и экспорт в PDF",
    "screenshot": "сохранить текущий вид карты картинкой", "style": "раскрасить слой по полю",
    "labels": "подписать объекты", "zoom": "перейти к месту, координатам или слою",
    "basemap": "добавить подложку (OpenStreetMap, Esri World Imagery…)", "osm": "загрузить данные OpenStreetMap",
    "load": "добавить слой из файла или сервиса", "layout": "макет печати текущей карты",
    "export": "экспорт в файл", "action": "нажать любую кнопку / пункт меню QGIS или модуля",
    "actions": "найти кнопки и пункты меню QGIS и модулей", "plugin": "список модулей / что умеет модуль",
}


def _desc(name):
    from .i18n import language
    return RU_DESC.get(name, COMMANDS[name][3]) if language() == "ru" else COMMANDS[name][3]


def help_text(user_lines=None):
    from .i18n import language
    ru = language() == "ru"
    width = max(len(v[2]) for v in COMMANDS.values())
    lines = ["Команды выполняются сразу, без ИИ (названия слоёв с пробелами — в кавычках):" if ru else
             "Commands run at once, without the AI (layer names with spaces in quotes):"]
    for en_title, ru_title, names in GROUPS:
        lines.append("")
        lines.append(ru_title if ru else en_title)
        lines += [f"  {COMMANDS[n][2]:<{width}}  {_desc(n)}" for n in names if n in COMMANDS]
    lines.append("")
    lines.append("Мои команды" if ru else "My commands")
    lines += user_lines or ["  " + ("пока нет — /newcommand или /commands" if ru else
                                    "none yet — /newcommand or /commands")]
    lines.append("")
    lines.append(("Примеры: " if ru else "Examples: ") + "/buffer Roads 250 · /stats Parcels area · "
                 "/calc Parcels density = \"pop\" / \"area_km2\" · /hexbin Cafes 500 · /top Districts pop 5 · "
                 "/filter Parcels \"area\" > 5000")
    return "\n".join(lines)


def completions(text, limit=4, user=()):
    """Hints shown under the request box while a command is typed. user: the user's command dicts."""
    name, _rest = split(text)
    mine = {u["name"]: u for u in user}
    if " " in str(text).strip():
        if name in mine:
            return [f"{mine[name]['usage'] or '/' + name} — {mine[name]['description']}"]
        entry = COMMANDS.get(name)
        return [f"{entry[2]} — {_desc(name)}"] if entry else []
    found = [f"{v[2]} — {_desc(n)}" for n, v in COMMANDS.items() if n.startswith(name)]
    found += [f"{u['usage'] or '/' + n} — {u['description']}" for n, u in sorted(mine.items()) if n.startswith(name)]
    return found[:limit]
