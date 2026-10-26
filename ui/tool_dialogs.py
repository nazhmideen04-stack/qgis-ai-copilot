"""Windows of the panel tools: smart join, field values, saved scenarios."""
import html

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QGuiApplication
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                                 QTextBrowser, QVBoxLayout, QWidget)

from ..core import field_hints
from ..core.i18n import tr, translate_tree
from ..core.smart_join import suggest_join


def _vector_layers():
    return [lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid()]


class _WaitCursor:
    def __enter__(self):
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def __exit__(self, *exc):
        QGuiApplication.restoreOverrideCursor()


# --------------------------------------------------------------------------- smart join

def smart_join_dialog(parent=None, table_name=None, layer_name=None):
    """Lets the user pick the table, the map layer and the key fields (suggested automatically).
    Returns a dict of smart_join() arguments, {"load_file": True} to add a table file first, or None."""
    layers = _vector_layers()
    maps = [lyr for lyr in layers if lyr.isSpatial()]
    if not maps:
        QMessageBox.information(parent, "Smart join", "Add a map layer (with geometry) to the project first.")
        return None
    dialog = QDialog(parent)
    dialog.setWindowTitle("AI Copilot — join a table to the map")
    dialog.resize(720, 520)
    layout = QVBoxLayout(dialog)
    intro = QLabel("Choose the table and the map layer. The key fields are found automatically by comparing the "
                   "values; spelling, case, accents and number formats (\"01\" = 1) are matched too.")
    intro.setWordWrap(True)
    layout.addWidget(intro)

    form = QFormLayout()
    table_row = QHBoxLayout()
    table_combo = QComboBox()
    tables = sorted(layers, key=lambda lyr: (lyr.isSpatial(), lyr.name().lower()))  # plain tables first
    for lyr in tables:
        table_combo.addItem(lyr.name() + ("" if lyr.isSpatial() else "  (table)"), lyr.id())
    table_row.addWidget(table_combo, 1)
    open_btn = QPushButton("Add table file…")
    open_btn.setToolTip("Add a CSV / Excel / other table file to the project and join it")
    table_row.addWidget(open_btn)
    form.addRow("Table with data:", table_row)
    layer_combo = QComboBox()
    for lyr in maps:
        layer_combo.addItem(lyr.name(), lyr.id())
    form.addRow("Map layer:", layer_combo)
    layout.addLayout(form)

    for combo, wanted in ((table_combo, table_name), (layer_combo, layer_name)):
        if wanted:
            for i in range(combo.count()):
                if combo.itemText(i).split("  (")[0].lower() == str(wanted).lower():
                    combo.setCurrentIndex(i)
    if layer_name is None and layer_combo.count() > 1 and table_combo.currentData() == layer_combo.currentData():
        layer_combo.setCurrentIndex(1)

    layout.addWidget(QLabel("Key field pairs (best first) — select one:"))
    grid = QTableWidget(0, 4)
    grid.setHorizontalHeaderLabels(["Table field", "Map layer field", "Keys found", "Note"])
    grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    grid.verticalHeader().setVisible(False)
    grid.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
    layout.addWidget(grid, 1)
    details = QLabel()
    details.setWordWrap(True)
    details.setStyleSheet("color: gray;")
    layout.addWidget(details)

    options = QFormLayout()
    fields_list = QListWidget()
    fields_list.setMaximumHeight(110)
    options.addRow("Table fields to add:", fields_list)
    prefix_edit = QLineEdit()
    options.addRow("Prefix of new fields:", prefix_edit)
    mode_combo = QComboBox()
    mode_combo.addItem("Automatic (live join if keys match exactly, otherwise a new layer)", "auto")
    mode_combo.addItem("Live QGIS join (keeps the original layer, updates with the table)", "live")
    mode_combo.addItem("New layer with the table columns (fixes spelling / formats)", "copy")
    options.addRow("How:", mode_combo)
    layout.addLayout(options)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Join"))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    state = {"suggestions": [], "load_file": False}

    def fill_fields():
        table = QgsProject.instance().mapLayer(table_combo.currentData())
        fields_list.clear()
        if table is None:
            return
        key = state["suggestions"][grid.currentRow()]["table_field"] if state["suggestions"] and \
            grid.currentRow() >= 0 else None
        for f in table.fields():
            item = QListWidgetItem(f.name())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if f.name() == key else Qt.CheckState.Checked)
            fields_list.addItem(item)
        prefix_edit.setText(f"{table.name()}_")

    def show_details():
        row = grid.currentRow()
        if not state["suggestions"] or row < 0:
            details.setText("")
            return
        s = state["suggestions"][row]
        text = f"{s['matched']} of {s['total']} distinct table keys have a map feature."
        if s["unmatched"]:
            text += f" Not found, e.g.: {', '.join(map(str, s['unmatched']))}."
        if s["duplicates"]:
            text += f" The table has {s['duplicates']} repeated key(s): the first row is used."
        details.setText(text)
        fill_fields()

    def refresh():
        table = QgsProject.instance().mapLayer(table_combo.currentData())
        layer = QgsProject.instance().mapLayer(layer_combo.currentData())
        grid.setRowCount(0)
        state["suggestions"] = []
        if table is None or layer is None or table.id() == layer.id():
            details.setText(tr("Choose two different layers."))
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        with _WaitCursor():
            try:
                state["suggestions"] = suggest_join(table, layer, top=8)
            except Exception as e:
                details.setText(f"Could not compare the layers: {e}")
        for s in state["suggestions"]:
            row = grid.rowCount()
            grid.insertRow(row)
            note = tr("exact match") if s["exact"] >= s["match"] - 1e-9 else tr("matched after normalising")
            for col, text in enumerate((s["table_field"], s["layer_field"], f"{s['match']:.0%}", note)):
                grid.setItem(row, col, QTableWidgetItem(text))
        grid.resizeColumnsToContents()
        ok = bool(state["suggestions"])
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)
        if ok:
            grid.selectRow(0)
        else:
            details.setText(tr("No fields with matching values were found. Check that the table and the layer "
                               "describe the same places."))
            fill_fields()

    def load_file():
        state["load_file"] = True
        dialog.reject()

    table_combo.currentIndexChanged.connect(lambda _i: refresh())
    layer_combo.currentIndexChanged.connect(lambda _i: refresh())
    grid.itemSelectionChanged.connect(show_details)
    open_btn.clicked.connect(load_file)
    refresh()

    translate_tree(dialog)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return {"load_file": True} if state["load_file"] else None
    row = grid.currentRow()
    if row < 0 or not state["suggestions"]:
        return None
    s = state["suggestions"][row]
    fields = [fields_list.item(i).text() for i in range(fields_list.count())
              if fields_list.item(i).checkState() == Qt.CheckState.Checked]
    return {"table": table_combo.currentText().split("  (")[0], "layer": layer_combo.currentText(),
            "table_field": s["table_field"], "layer_field": s["layer_field"], "fields": fields,
            "prefix": prefix_edit.text(), "mode": mode_combo.currentData()}


# --------------------------------------------------------------------------- field values

def field_values_dialog(insert, parent=None, layer_name=None):
    """Browser of layers → fields → values. Double click (or «Insert») puts the field name or the value into the
    request box through insert(text). Non-modal: stays open while the user writes."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("AI Copilot — field values")
    dialog.resize(760, 460)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    layout = QVBoxLayout(dialog)
    hint = QLabel("Real values of your layers. Double-click a field or a value to put it into your request — "
                  "the AI then uses exactly these names and values.")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    layer_list, field_list, value_list = QListWidget(), QListWidget(), QListWidget()
    value_filter = QLineEdit()
    value_filter.setPlaceholderText("Filter values…")
    value_filter.setClearButtonEnabled(True)
    value_box = QWidget()
    value_layout = QVBoxLayout(value_box)
    value_layout.setContentsMargins(0, 0, 0, 0)
    value_layout.addWidget(value_filter)
    value_layout.addWidget(value_list, 1)
    for title, widget in (("Layers", layer_list), ("Fields", field_list), ("Values", value_box)):
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.addWidget(QLabel(f"<b>{title}</b>"))
        box_layout.addWidget(widget, 1)
        splitter.addWidget(box)
    splitter.setSizes([200, 260, 300])
    layout.addWidget(splitter, 1)
    info = QLabel()
    info.setStyleSheet("color: gray;")
    layout.addWidget(info)
    row = QHBoxLayout()
    insert_btn = QPushButton("Insert into request")
    close_btn = QPushButton("Close")
    row.addStretch(1)
    row.addWidget(insert_btn)
    row.addWidget(close_btn)
    layout.addLayout(row)

    for lyr in _vector_layers():
        item = QListWidgetItem(lyr.name())
        item.setData(Qt.ItemDataRole.UserRole, lyr.id())
        item.setToolTip(f"{lyr.featureCount()} features")
        layer_list.addItem(item)
        if layer_name and lyr.name().lower() == str(layer_name).lower():
            layer_list.setCurrentItem(item)

    def current_layer():
        item = layer_list.currentItem()
        return QgsProject.instance().mapLayer(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def on_layer():
        field_list.clear()
        value_list.clear()
        layer = current_layer()
        if layer is None:
            return
        too_big = layer.featureCount() > field_hints.MAX_SCAN_FEATURES
        with _WaitCursor():
            summaries = [] if too_big else field_hints.layer_summaries(layer)
        by_name = {s["name"]: s for s in summaries}
        for f in layer.fields():
            s = by_name.get(f.name())
            hint_text = field_hints.describe_field(s) if s else ""
            item = QListWidgetItem(f.name())
            item.setToolTip(f"{f.typeName()}\n{hint_text}" if hint_text else f.typeName())
            item.setData(Qt.ItemDataRole.UserRole, f.name())
            short = hint_text.split(": ", 1)[1] if ": " in hint_text else ""
            if short:
                item.setText(f"{f.name()}   —   {short[:60]}")
            field_list.addItem(item)
        info.setText(f"'{layer.name()}': {layer.featureCount()} features"
                     + (" — large layer: values are read on demand" if too_big else ""))

    def on_field():
        value_list.clear()
        layer, item = current_layer(), field_list.currentItem()
        if layer is None or item is None:
            return
        index = layer.fields().indexFromName(item.data(Qt.ItemDataRole.UserRole))
        with _WaitCursor():
            values = sorted((v for v in layer.uniqueValues(index, 500) if not field_hints._is_null(v)),
                            key=lambda v: (not isinstance(v, (int, float)), v if isinstance(v, (int, float)) else str(v)))
        for v in values:
            value_item = QListWidgetItem(str(v))
            value_item.setData(Qt.ItemDataRole.UserRole, v)
            value_list.addItem(value_item)
        more = " (first 500)" if len(values) >= 500 else ""
        info.setText(f"{item.data(Qt.ItemDataRole.UserRole)}: {len(values)} distinct value(s){more}")
        apply_filter(value_filter.text())

    def apply_filter(text):
        text = text.lower()
        for i in range(value_list.count()):
            value_list.item(i).setHidden(text not in value_list.item(i).text().lower())

    def insert_field(item=None):
        item = item or field_list.currentItem()
        if item is not None:
            insert(f'"{item.data(Qt.ItemDataRole.UserRole)}"')

    def insert_value(item=None):
        item = item or value_list.currentItem()
        if item is None:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        insert(str(value) if isinstance(value, (int, float)) else "'" + str(value).replace("'", "''") + "'")

    def insert_current():
        if value_list.hasFocus() or value_list.currentItem() is not None:
            insert_value()
        else:
            insert_field()

    layer_list.currentItemChanged.connect(lambda *_a: on_layer())
    field_list.currentItemChanged.connect(lambda *_a: on_field())
    field_list.itemDoubleClicked.connect(insert_field)
    value_list.itemDoubleClicked.connect(insert_value)
    value_filter.textChanged.connect(apply_filter)
    insert_btn.clicked.connect(insert_current)
    close_btn.clicked.connect(dialog.close)
    if layer_list.currentItem() is None and layer_list.count():
        layer_list.setCurrentRow(0)
    else:
        on_layer()
    translate_tree(dialog)
    dialog.show()
    return dialog


# --------------------------------------------------------------------------- scenarios

def scenarios_dialog(store, parent=None, current_prompt="", last_task=None):
    """Saved scenarios. last_task: (prompt, [code steps]) of the last successful AI task, or None.
    Returns (action, scenario) with action "run" (saved code, no AI) | "ai" (send to the AI) | "edit"
    (put into the request box), or None."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("AI Copilot — saved scenarios")
    dialog.resize(860, 520)
    layout = QVBoxLayout(dialog)
    save_row = QHBoxLayout()
    save_task_btn = QPushButton("Save last task with its code…")
    save_task_btn.setToolTip("The request and the code that worked: runs again later without the AI")
    save_task_btn.setEnabled(bool(last_task and last_task[1]))
    save_prompt_btn = QPushButton("Save current request…")
    save_prompt_btn.setToolTip("Only the text of the request in the box (it is sent to the AI when run)")
    save_prompt_btn.setEnabled(bool(current_prompt.strip()))
    save_row.addWidget(save_task_btn)
    save_row.addWidget(save_prompt_btn)
    save_row.addStretch(1)
    import_btn = QPushButton("Import…")
    export_btn = QPushButton("Export…")
    save_row.addWidget(import_btn)
    save_row.addWidget(export_btn)
    layout.addLayout(save_row)

    search = QLineEdit()
    search.setPlaceholderText("Search scenarios…")
    search.setClearButtonEnabled(True)
    layout.addWidget(search)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    names = QListWidget()
    view = QTextBrowser()
    splitter.addWidget(names)
    splitter.addWidget(view)
    splitter.setSizes([260, 600])
    layout.addWidget(splitter, 1)

    actions = QHBoxLayout()
    run_btn = QPushButton("Run saved code (no AI)")
    run_btn.setStyleSheet("font-weight: bold;")
    ai_btn = QPushButton("Send to AI")
    edit_btn = QPushButton("Put in request box")
    command_btn = QPushButton("Make a command…")
    command_btn.setToolTip("Turn the saved code into your own /command")
    rename_btn = QPushButton("Rename")
    delete_btn = QPushButton("Delete")
    close_btn = QPushButton("Close")
    for b in (run_btn, ai_btn, edit_btn, command_btn, rename_btn, delete_btn):
        actions.addWidget(b)
    actions.addStretch(1)
    actions.addWidget(close_btn)
    layout.addLayout(actions)
    result = {"value": None}

    def fill(select_id=None):
        names.clear()
        for item in sorted(store.items, key=lambda i: i["name"].lower()):
            entry = QListWidgetItem(item["name"] + ("" if item["steps"] else "   (request only)"))
            entry.setData(Qt.ItemDataRole.UserRole, item["id"])
            names.addItem(entry)
            if item["id"] == select_id:
                names.setCurrentItem(entry)
        if names.currentItem() is None and names.count():
            names.setCurrentRow(0)
        apply_search(search.text())
        show()

    def current():
        entry = names.currentItem()
        if entry is None:
            return None
        return next((i for i in store.items if i["id"] == entry.data(Qt.ItemDataRole.UserRole)), None)

    def show():
        item = current()
        for b in (ai_btn, edit_btn, rename_btn, delete_btn):
            b.setEnabled(item is not None)
        run_btn.setEnabled(bool(item and item["steps"]))
        command_btn.setEnabled(bool(item and item["steps"]))
        if item is None:
            view.setHtml("<p style='color:gray'>No scenarios yet. Run a task with the AI, then press "
                         "<b>Save last task with its code</b>.</p>")
            return
        parts = [f"<h3>{html.escape(item['name'])}</h3>",
                 f"<p><b>Request:</b><br>{html.escape(item['prompt']).replace(chr(10), '<br>')}</p>"]
        if item["steps"]:
            for n, code in enumerate(item["steps"], 1):
                parts.append(f"<p><b>Code, step {n}:</b></p><pre>{html.escape(code)}</pre>")
        else:
            parts.append("<p style='color:gray'>No saved code: the request is sent to the AI.</p>")
        meta = ", ".join(x for x in (item.get("provider"), item.get("model"), item.get("updated")) if x)
        if meta:
            parts.append(f"<p style='color:gray'>{html.escape(meta)}</p>")
        view.setHtml("".join(parts))

    def apply_search(text):
        text = text.lower()
        for i in range(names.count()):
            entry = names.item(i)
            item = next((x for x in store.items if x["id"] == entry.data(Qt.ItemDataRole.UserRole)), None)
            hay = (item["name"] + " " + item["prompt"]).lower() if item else ""
            entry.setHidden(text not in hay)

    def ask_name(default):
        name, ok = QInputDialog.getText(dialog, tr("Scenario name"), tr("Name of the scenario:"), text=default[:60])
        name = name.strip() if ok else ""
        if name and store.get(name):
            answer = QMessageBox.question(dialog, "Replace?", f"A scenario '{name}' exists. Replace it?")
            if answer != QMessageBox.StandardButton.Yes:
                return ""
        return name

    def save_task():
        prompt, steps = last_task
        name = ask_name(prompt.splitlines()[0] if prompt else "Scenario")
        if name:
            fill(store.add(name, prompt, steps, *(last_task[2:4] if len(last_task) >= 4 else ("", "")))["id"])

    def save_prompt():
        name = ask_name(current_prompt.strip().splitlines()[0])
        if name:
            fill(store.add(name, current_prompt.strip(), [])["id"])

    def finish(action):
        item = current()
        if item is not None:
            result["value"] = (action, item)
            dialog.accept()

    def rename():
        item = current()
        if item is None:
            return
        name = ask_name(item["name"])
        if name:
            store.rename(item["id"], name)
            fill(item["id"])

    def delete():
        item = current()
        if item and QMessageBox.question(dialog, "Delete", f"Delete scenario '{item['name']}'?") == \
                QMessageBox.StandardButton.Yes:
            store.delete(item["id"])
            fill()

    def do_export():
        path, _f = QFileDialog.getSaveFileName(dialog, "Export scenarios", "ai_copilot_scenarios.json",
                                               "Scenarios (*.json)")
        if path:
            count = store.export(path)
            QMessageBox.information(dialog, "Export", f"{count} scenario(s) saved to\n{path}")

    def do_import():
        path, _f = QFileDialog.getOpenFileName(dialog, "Import scenarios", "", "Scenarios (*.json)")
        if path:
            count = store.import_file(path)
            QMessageBox.information(dialog, "Import", f"{count} scenario(s) imported.")
            fill()

    save_task_btn.clicked.connect(save_task)
    save_prompt_btn.clicked.connect(save_prompt)
    import_btn.clicked.connect(do_import)
    export_btn.clicked.connect(do_export)
    names.currentItemChanged.connect(lambda *_a: show())
    names.itemDoubleClicked.connect(lambda *_a: finish("run" if (current() or {}).get("steps") else "ai"))
    search.textChanged.connect(apply_search)
    run_btn.clicked.connect(lambda: finish("run"))
    ai_btn.clicked.connect(lambda: finish("ai"))
    edit_btn.clicked.connect(lambda: finish("edit"))
    command_btn.clicked.connect(lambda: finish("command"))
    rename_btn.clicked.connect(rename)
    delete_btn.clicked.connect(delete)
    close_btn.clicked.connect(dialog.reject)
    fill()
    translate_tree(dialog)
    dialog.exec()
    return result["value"]


# --------------------------------------------------------------------------- the user's own commands

COMMANDS_HELP = (
    "<b>List of commands</b> — one command per line, run as one action (one Undo):<br>"
    "<code>/buffer $1 ${2:100}</code><br><code>/style $last area_km2</code><br>"
    "<code>$1</code> … <code>$9</code> — your arguments, <code>${2:100}</code> — argument 2 or 100, "
    "<code>$*</code> — all arguments, <code>$last</code> — the layer the previous line created.<br>"
    "<b>Python</b> — code like AI code; <code>args</code>, <code>kwargs</code>, <code>arg(1, default)</code> hold "
    "the arguments.")


def commands_dialog(store, parent=None, draft=None):
    """Create, edit, delete, import and export the user's commands. draft: a dict to start a new command from
    (written by the AI or made from a scenario). Returns ("ai", "") to ask the AI for a new command, or None."""
    from qgis.PyQt.QtGui import QFontDatabase
    from qgis.PyQt.QtWidgets import QPlainTextEdit
    from ..core.user_commands import _clean, guess_kind
    dialog = QDialog(parent)
    dialog.setWindowTitle("AI Copilot — my commands")
    dialog.resize(900, 560)
    layout = QVBoxLayout(dialog)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    names = QListWidget()
    editor = QWidget()
    form = QFormLayout(editor)
    name_edit = QLineEdit()
    name_edit.setPlaceholderText("relief")
    usage_edit = QLineEdit()
    usage_edit.setPlaceholderText("/relief <dem layer> [opacity]")
    desc_edit = QLineEdit()
    desc_edit.setPlaceholderText("Hillshade of a DEM, half transparent")
    kind_combo = QComboBox()
    kind_combo.setProperty("ai_translate_items", True)
    kind_combo.addItem("List of commands", "macro")
    kind_combo.addItem("Python", "python")
    body_edit = QPlainTextEdit()
    body_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    body_edit.setPlaceholderText("/buffer $1 ${2:100}\n/style $last area_km2")
    help_label = QLabel(COMMANDS_HELP)
    help_label.setWordWrap(True)
    help_label.setStyleSheet("color: gray;")
    form.addRow("Name:", name_edit)
    form.addRow("Usage:", usage_edit)
    form.addRow("Description:", desc_edit)
    form.addRow("Type:", kind_combo)
    form.addRow(body_edit)
    form.addRow(help_label)
    splitter.addWidget(names)
    splitter.addWidget(editor)
    splitter.setSizes([220, 680])
    layout.addWidget(splitter, 1)
    status = QLabel()
    status.setWordWrap(True)
    layout.addWidget(status)
    row = QHBoxLayout()
    new_btn, save_btn, delete_btn = QPushButton("New"), QPushButton("Save"), QPushButton("Delete")
    ai_btn = QPushButton("Ask AI to write one…")
    ai_btn.setToolTip("Describe what the command should do; the AI writes it, you check and save it")
    import_btn, export_btn, close_btn = QPushButton("Import…"), QPushButton("Export…"), QPushButton("Close")
    save_btn.setStyleSheet("font-weight: bold;")
    for b in (new_btn, save_btn, delete_btn, ai_btn):
        row.addWidget(b)
    row.addStretch(1)
    for b in (import_btn, export_btn, close_btn):
        row.addWidget(b)
    layout.addLayout(row)
    state = {"old": None, "result": None}

    def fill(select=None):
        names.blockSignals(True)
        names.clear()
        for item in sorted(store.items, key=lambda i: i["name"]):
            entry = QListWidgetItem(f"/{item['name']}")
            entry.setData(Qt.ItemDataRole.UserRole, item["name"])
            entry.setToolTip(item["description"])
            names.addItem(entry)
            if item["name"] == select:
                names.setCurrentItem(entry)
        names.blockSignals(False)

    def show(item):
        state["old"] = item["name"] if item and store.get(item["name"]) else None
        item = item or {}
        name_edit.setText(item.get("name", ""))
        usage_edit.setText(item.get("usage", ""))
        desc_edit.setText(item.get("description", ""))
        body_edit.setPlainText(item.get("body", ""))
        kind = item.get("kind") or "macro"
        kind_combo.setCurrentIndex(max(0, kind_combo.findData(kind)))
        delete_btn.setEnabled(state["old"] is not None)
        status.setText("")

    def on_select():
        entry = names.currentItem()
        if entry is not None:
            show(store.get(entry.data(Qt.ItemDataRole.UserRole)))

    def on_body_changed():
        body = body_edit.toPlainText()
        if body.strip() and not store.get(name_edit.text().strip().lower()):
            kind_combo.setCurrentIndex(kind_combo.findData(guess_kind(body)))

    def save():
        item = _clean({"name": name_edit.text(), "usage": usage_edit.text(), "description": desc_edit.text(),
                       "kind": kind_combo.currentData(), "body": body_edit.toPlainText()})
        if not item["usage"]:
            item["usage"] = f"/{item['name']}"
        try:
            stored = store.put(item, state["old"])
        except ValueError as e:
            status.setText(f"<span style='color:#c62828'>{html.escape(tr(str(e)))}</span>")
            return
        fill(stored["name"])
        show(stored)
        status.setText(f"<span style='color:#2e7d32'>{html.escape(tr('Saved. Type'))} "
                       f"<b>{html.escape(stored['usage'])}</b></span>")

    def delete():
        if state["old"] and QMessageBox.question(dialog, "AI Copilot", tr(f"Delete /{state['old']}?")) == \
                QMessageBox.StandardButton.Yes:
            store.delete(state["old"])
            fill()
            show(None)

    def do_export():
        path, _f = QFileDialog.getSaveFileName(dialog, "AI Copilot", "ai_copilot_commands.json", "Commands (*.json)")
        if path:
            status.setText(tr(f"{store.export(path)} command(s) exported."))

    def do_import():
        path, _f = QFileDialog.getOpenFileName(dialog, "AI Copilot", "", "Commands (*.json)")
        if path:
            status.setText(tr(f"{store.import_file(path)} command(s) imported."))
            fill()

    def ask_ai():
        state["result"] = ("ai", "")
        dialog.accept()

    names.currentItemChanged.connect(lambda *_a: on_select())
    body_edit.textChanged.connect(on_body_changed)
    new_btn.clicked.connect(lambda: (names.clearSelection(), show(None), name_edit.setFocus()))
    save_btn.clicked.connect(save)
    delete_btn.clicked.connect(delete)
    ai_btn.clicked.connect(ask_ai)
    import_btn.clicked.connect(do_import)
    export_btn.clicked.connect(do_export)
    close_btn.clicked.connect(dialog.reject)
    fill()
    if draft:
        show(draft)
        status.setText(tr("Check the command and press Save."))
    elif store.items:
        names.setCurrentRow(0)
        on_select()
    else:
        show(None)
    translate_tree(dialog)
    dialog.exec()
    return state["result"]
