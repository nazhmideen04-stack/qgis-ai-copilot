"""
Dialogs through which AI code asks the user.
The AI never takes external data silently: the user sees the source, the link and a data sample.
"""
import html
import os

from qgis.core import QgsRasterLayer, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                                 QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                                 QPushButton, QSplitter, QTextBrowser, QVBoxLayout)

from ..core.progress import report_status

S = "ai_copilot"


def _parent():
    try:
        import qgis.utils
        return qgis.utils.iface.mainWindow()
    except (ImportError, AttributeError):  # no iface (tests/console) — a window without a parent
        return None


def _esc(value):
    return html.escape(str(value if value is not None else ""))


def _sample_html(sample, max_rows=10):
    """Data sample: list of dicts → HTML table; string → <pre>."""
    if sample is None:
        return ""
    if isinstance(sample, (list, tuple)) and sample and isinstance(sample[0], dict):
        rows = list(sample)[:max_rows]
        cols = []
        for row in rows:
            for key in row:
                if key not in cols:
                    cols.append(key)
        cols = cols[:12]
        head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td>{_esc(r.get(c))[:80]}</td>" for c in cols) + "</tr>" for r in rows)
        more = f"<p><i>… {len(sample) - len(rows)} more rows</i></p>" if len(sample) > len(rows) else ""
        return (f"<table border='1' cellspacing='0' cellpadding='3' style='border-collapse:collapse'>"
                f"<tr>{head}</tr>{body}</table>{more}")
    return f"<pre>{_esc(str(sample)[:3000])}</pre>"


def _item_html(item):
    parts = [f"<h3>{_esc(item.get('title', 'Dataset'))}</h3>"]
    if item.get("source"):
        parts.append(f"<p><b>Source:</b> {_esc(item['source'])}</p>")
    if item.get("url"):
        parts.append(f"<p><b>Link:</b> <a href='{_esc(item['url'])}'>{_esc(item['url'])}</a></p>")
    for key, label in (("format", "Format"), ("coverage", "Coverage"), ("updated", "Updated"),
                       ("license", "License"), ("size", "Size")):
        if item.get(key):
            parts.append(f"<p><b>{label}:</b> {_esc(item[key])}</p>")
    if item.get("description"):
        parts.append(f"<p>{_esc(item['description'])[:1500]}</p>")
    if item.get("sample") is not None:
        parts.append("<p><b>Sample:</b></p>" + _sample_html(item["sample"]))
    return "".join(parts)


# --------------------------------------------------------------------------- choosing data

def choose_data(title, items, message="", multi=True):
    """Shows the found sources/datasets and lets the user choose.
    items: list of dicts {title, url, source, description, format, coverage, license, size, sample, ...}.
    Returns the list of chosen dicts (or a dict / None when multi=False). An empty list means refusal."""
    items = [dict(i) if isinstance(i, dict) else {"title": str(i)} for i in items]
    if not items:
        print("choose_data: nothing to choose from.")
        return [] if multi else None
    report_status("Waiting for your choice…", title)

    dialog = QDialog(_parent())
    dialog.setWindowTitle(f"AI Copilot — {title}")
    dialog.resize(820, 520)
    layout = QVBoxLayout(dialog)
    intro = QLabel(message or ("The AI found these data sources. Select what to use "
                               "(click an item to see details and the link):"))
    intro.setWordWrap(True)
    layout.addWidget(intro)

    splitter = QSplitter(Qt.Orientation.Horizontal)
    list_widget = QListWidget()
    details = QTextBrowser()
    details.setOpenExternalLinks(True)
    for item in items:
        label = item.get("title", "Dataset")
        if item.get("source"):
            label += f"  —  {item['source']}"
        entry = QListWidgetItem(label)
        if multi:
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Unchecked)
        list_widget.addItem(entry)
    if not multi:
        list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    list_widget.currentRowChanged.connect(lambda row: details.setHtml(_item_html(items[row])) if row >= 0 else None)
    list_widget.setCurrentRow(0)
    splitter.addWidget(list_widget)
    splitter.addWidget(details)
    splitter.setSizes([320, 500])
    layout.addWidget(splitter, 1)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use selected" if multi else "Use this")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        print(f"User cancelled the choice: {title}")
        return [] if multi else None
    if multi:
        chosen = [items[i] for i in range(list_widget.count())
                  if list_widget.item(i).checkState() == Qt.CheckState.Checked]
        print(f"User selected {len(chosen)} item(s): {[c.get('title') for c in chosen]}")
        return chosen
    row = list_widget.currentRow()
    chosen = items[row] if row >= 0 else None
    print(f"User selected: {chosen.get('title') if chosen else None}")
    return chosen


def confirm_data(title, description="", url="", sample=None, question="Use this data?"):
    """Shows the found data (description, link, sample) and asks whether to use it. → bool"""
    report_status("Waiting for your confirmation…", title)
    dialog = QDialog(_parent())
    dialog.setWindowTitle(f"AI Copilot — {title}")
    dialog.resize(760, 480)
    layout = QVBoxLayout(dialog)
    browser = QTextBrowser()
    browser.setOpenExternalLinks(True)
    browser.setHtml(_item_html({"title": title, "url": url, "description": description, "sample": sample}))
    layout.addWidget(browser, 1)
    label = QLabel(f"<b>{_esc(question)}</b>")
    layout.addWidget(label)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    ok = dialog.exec() == QDialog.DialogCode.Accepted
    print(f"User {'ACCEPTED' if ok else 'REJECTED'} the data: {title}")
    return ok


def ask_yes_no(title, text):
    """Simple Yes/No question. Returns True for Yes."""
    report_status("Waiting for your answer…", title)
    answer = QMessageBox.question(_parent(), f"AI Copilot — {title}", text,
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def ask_user(question, options=None, default=""):
    """Asks the user a question. With options — a choice list, without — free text. None = cancelled."""
    report_status("Waiting for your answer…", question)
    if options:
        options = [str(o) for o in options]
        current = options.index(default) if default in options else 0
        answer, ok = QInputDialog.getItem(_parent(), "AI Copilot", question, options, current, False)
    else:
        answer, ok = QInputDialog.getText(_parent(), "AI Copilot", question, text=str(default))
    result = answer if ok else None
    print(f"User answered: {result!r}")
    return result


# --------------------------------------------------------------------------- files

def _default_dir():
    saved = QgsSettings().value(f"{S}/export_dir", "")
    if saved and os.path.isdir(str(saved)):
        return str(saved)
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return docs if os.path.isdir(docs) else os.path.expanduser("~")


def remember_dir(path):
    QgsSettings().setValue(f"{S}/export_dir", os.path.dirname(os.path.abspath(path)))


def ask_save_path(default_name="output", file_filter="All files (*.*)", title="Save as"):
    """«Save as» dialog. Returns a path or None."""
    report_status("Waiting for you to choose where to save…", default_name)
    path, _ = QFileDialog.getSaveFileName(_parent(), f"AI Copilot — {title}",
                                          os.path.join(_default_dir(), default_name), file_filter)
    if path:
        remember_dir(path)
    return path or None


def ask_open_file(file_filter="All files (*.*)", title="Open file"):
    """File dialog (e.g. a table the user downloaded manually from a website). Path or None."""
    report_status("Waiting for you to choose a file…", title)
    path, _ = QFileDialog.getOpenFileName(_parent(), f"AI Copilot — {title}", _default_dir(), file_filter)
    if path:
        remember_dir(path)
    return path or None


def ask_folder(title="Choose a folder"):
    report_status("Waiting for you to choose a folder…", title)
    path = QFileDialog.getExistingDirectory(_parent(), f"AI Copilot — {title}", _default_dir())
    if path:
        QgsSettings().setValue(f"{S}/export_dir", path)
    return path or None


# --------------------------------------------------------------------------- choosing layers

def pick_layers(layers, checked_ids=(), parent=None):
    """Dialog with a checkable list of layers. Returns the list of chosen layer ids, or None if cancelled."""
    dialog = QDialog(parent or _parent())
    dialog.setWindowTitle("AI Copilot — layers for the AI")
    dialog.resize(420, 520)
    layout = QVBoxLayout(dialog)
    info = QLabel("Check the layers the AI should work with. It gets their fields and details; "
                  "other layers are sent by name only.")
    info.setWordWrap(True)
    layout.addWidget(info)

    search = QLineEdit()
    search.setPlaceholderText("Filter by name…")
    search.setClearButtonEnabled(True)
    layout.addWidget(search)

    list_widget = QListWidget()
    checked = set(checked_ids)
    for layer in layers:
        kind = "raster" if isinstance(layer, QgsRasterLayer) else "vector" if isinstance(layer, QgsVectorLayer) else ""
        entry = QListWidgetItem(f"{layer.name()}" + (f"  ({kind})" if kind else ""))
        entry.setData(Qt.ItemDataRole.UserRole, layer.id())
        entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        entry.setCheckState(Qt.CheckState.Checked if layer.id() in checked else Qt.CheckState.Unchecked)
        list_widget.addItem(entry)
    layout.addWidget(list_widget, 1)

    def apply_filter(text):
        text = text.lower()
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            item.setHidden(text not in item.text().lower())

    def set_all(state):
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    search.textChanged.connect(apply_filter)
    row = QHBoxLayout()
    all_btn = QPushButton("All")
    all_btn.clicked.connect(lambda: set_all(Qt.CheckState.Checked))
    none_btn = QPushButton("None")
    none_btn.clicked.connect(lambda: set_all(Qt.CheckState.Unchecked))
    row.addWidget(all_btn)
    row.addWidget(none_btn)
    row.addStretch(1)
    layout.addLayout(row)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return [list_widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(list_widget.count())
            if list_widget.item(i).checkState() == Qt.CheckState.Checked]


# --------------------------------------------------------------------------- request archive

def _status_text(entry):
    if entry.get("ok") is True:
        return "OK"
    if entry.get("ok") is False:
        return "error"
    return "answer" if entry.get("answer") else "-"


def show_history(archive, parent=None):
    """Request archive: search, details, send again, delete. Returns the prompt to reuse, or None."""
    dialog = QDialog(parent or _parent())
    dialog.setWindowTitle("AI Copilot — request archive")
    dialog.resize(900, 560)
    layout = QVBoxLayout(dialog)
    search = QLineEdit()
    search.setPlaceholderText("Search in requests and answers…")
    search.setClearButtonEnabled(True)
    layout.addWidget(search)

    splitter = QSplitter(Qt.Orientation.Horizontal)
    list_widget = QListWidget()
    details = QTextBrowser()
    details.setOpenExternalLinks(True)
    splitter.addWidget(list_widget)
    splitter.addWidget(details)
    splitter.setSizes([380, 520])
    layout.addWidget(splitter, 1)
    chosen = {"prompt": None}

    def fill():
        text = search.text().lower()
        list_widget.clear()
        for entry in archive.entries():
            if text and text not in (entry["prompt"] + " " + entry.get("answer", "")).lower():
                continue
            when = entry["time"].replace("T", " ")[:16]
            item = QListWidgetItem(f"{when}  [{_status_text(entry)}]  {entry['prompt'][:70]}")
            item.setData(Qt.ItemDataRole.UserRole, entry["id"])
            list_widget.addItem(item)
        list_widget.setCurrentRow(0)
        if list_widget.count() == 0:
            details.setHtml("<i>No requests yet.</i>" if not text else "<i>Nothing found.</i>")

    def current():
        item = list_widget.currentItem()
        entry_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((e for e in archive.entries() if e["id"] == entry_id), None)

    def show_details(_row):
        entry = current()
        if entry is None:
            return
        details.setHtml(
            f"<p><b>{_esc(entry['time'].replace('T', ' '))}</b> — {_esc(entry.get('provider'))} "
            f"{_esc(entry.get('model'))} — <b>{_status_text(entry)}</b></p>"
            f"<p><b>Request:</b><br>{_esc(entry['prompt']).replace(chr(10), '<br>')}</p>"
            f"<p><b>Answer:</b></p><pre>{_esc(entry.get('answer', ''))[:6000]}</pre>"
            + (f"<p><b>Result:</b></p><pre>{_esc(entry.get('result', ''))[:3000]}</pre>"
               if entry.get("result") else ""))

    def use_again():
        entry = current()
        if entry is not None:
            chosen["prompt"] = entry["prompt"]
            dialog.accept()

    def delete():
        entry = current()
        if entry is not None:
            archive.delete(entry["id"])
            fill()

    def clear_all():
        if QMessageBox.question(dialog, "AI Copilot", "Delete the whole request archive?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            archive.clear()
            fill()

    search.textChanged.connect(lambda _t: fill())
    list_widget.currentRowChanged.connect(show_details)
    list_widget.itemDoubleClicked.connect(lambda _i: use_again())
    row = QHBoxLayout()
    for title, slot in (("Use again", use_again), ("Delete", delete), ("Clear archive", clear_all)):
        button = QPushButton(title)
        button.clicked.connect(slot)
        row.addWidget(button)
    row.addStretch(1)
    close = QPushButton("Close")
    close.clicked.connect(dialog.reject)
    row.addWidget(close)
    layout.addLayout(row)
    fill()
    dialog.exec()
    return chosen["prompt"]
