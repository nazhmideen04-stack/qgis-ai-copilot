"""
«Undo last AI action»: takes a snapshot of the project before AI code (or a command / scenario / smart join) runs
and restores it on request.

What is undone:
- layers added by the action are removed, layers it removed are added back (memory layers with their data);
- style, labels, filter (subset), name, visibility and table joins of existing layers are restored;
- data edits saved through the edit buffer (startEditing → commitChanges) are reverted: added features are deleted,
  deleted features re-added, changed attributes and geometries restored, added / deleted fields reverted;
- memory (temporary) layers up to MEMORY_SNAPSHOT_LIMIT features are restored completely, whatever the code did;
- new print layouts are removed, the project CRS and the map view are restored.
Not undone: files written to disk (exports) and changes written straight to a file provider without the edit
buffer — those are reported as "cannot be undone".
"""
import contextlib
import functools
import hashlib

from qgis.core import (QgsFeature, QgsFeatureRequest, QgsField, QgsFields, QgsGeometry, QgsMapLayerStyle, QgsProject,
                       QgsVectorLayer, QgsVectorLayerJoinInfo)

from .log import log_warning

MAX_UNDO = 10                      # actions kept in the undo stack
MEMORY_SNAPSHOT_LIMIT = 50000      # features of one memory layer copied before an action
MEMORY_SNAPSHOT_TOTAL = 200000     # features of all memory layers copied before an action
DELETED_FIELD_VALUES_LIMIT = 100000


def _iface():
    try:
        from qgis.utils import iface
        return iface
    except ImportError:
        return None


def _style_xml(layer):
    try:
        style = QgsMapLayerStyle()
        style.readFromLayer(layer)
        return style.xmlData()
    except Exception as e:  # a style that cannot be read is simply not restored
        log_warning(f"Undo: could not read the style of '{layer.name()}': {e}")
        return None


def _apply_style(layer, xml):
    style = QgsMapLayerStyle(xml)
    style.writeToLayer(layer)
    layer.triggerRepaint()


def _visible(layer_id):
    node = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
    return node.itemVisibilityChecked() if node is not None else None


def _joins(layer):
    if not isinstance(layer, QgsVectorLayer):
        return []
    return [QgsVectorLayerJoinInfo(j) for j in layer.vectorJoins()]


def _join_key(join):
    return (join.joinLayerId(), join.joinFieldName(), join.targetFieldName(), join.prefix() or "")


def _copy_data(layer):
    """Fields and features of a memory layer, with a hash to detect changes cheaply."""
    digest = hashlib.sha1()
    features = []
    for feature in layer.getFeatures():
        attrs = list(feature.attributes())
        geom = QgsGeometry(feature.geometry())
        features.append((attrs, geom))
        digest.update(repr(attrs).encode("utf-8", "replace"))
        digest.update(bytes(geom.asWkb()) if not geom.isNull() else b"-")
    names = [f.name() for f in layer.fields()]
    digest.update(repr(names).encode())
    return {"fields": QgsFields(layer.fields()), "features": features, "hash": digest.hexdigest()}


def _data_hash(layer):
    digest = hashlib.sha1()
    for feature in layer.getFeatures():
        geom = feature.geometry()
        digest.update(repr(list(feature.attributes())).encode("utf-8", "replace"))
        digest.update(bytes(geom.asWkb()) if not geom.isNull() else b"-")
    digest.update(repr([f.name() for f in layer.fields()]).encode())
    return digest.hexdigest()


class _LayerState:
    def __init__(self, layer):
        self.name = layer.name()
        self.style = _style_xml(layer)
        self.subset = layer.subsetString() if isinstance(layer, QgsVectorLayer) else None
        self.visible = _visible(layer.id())
        self.joins = _joins(layer)
        self.count = layer.featureCount() if isinstance(layer, QgsVectorLayer) else None
        self.field_names = [f.name() for f in layer.fields()] if isinstance(layer, QgsVectorLayer) else None
        self.editable = layer.isEditable() if isinstance(layer, QgsVectorLayer) else False


class Snapshot:
    """State of the project before one action. finish() is called after the action and works out what changed."""

    def __init__(self, label):
        self.label = label
        project = QgsProject.instance()
        self.layers = dict(project.mapLayers())
        self.states = {lid: _LayerState(layer) for lid, layer in self.layers.items()}
        self.memory_data = {}
        total = 0
        for lid, layer in self.layers.items():
            if isinstance(layer, QgsVectorLayer) and layer.providerType() == "memory":
                count = layer.featureCount()
                if count <= MEMORY_SNAPSHOT_LIMIT and total + count <= MEMORY_SNAPSHOT_TOTAL:
                    self.memory_data[lid] = _copy_data(layer)
                    total += count
        self.removed = {}       # layer id -> (restorable layer object, tree index)
        self.edits = {}         # layer id -> list of committed edit records
        self.layouts = {lay.name() for lay in project.layoutManager().layouts()}
        self.crs = project.crs()
        iface = _iface()
        canvas = iface.mapCanvas() if iface is not None else None
        self.extent = canvas.extent() if canvas is not None else None
        self.changes = []       # human readable list of what will be undone
        self.not_undoable = []
        self._connections = []
        self._connect()

    # ------------------------------------------------------------------ recording while the action runs
    def _connect(self):
        project = QgsProject.instance()
        self._add_connection(project.layersWillBeRemoved, self._on_layers_removed)
        for lid, layer in self.layers.items():
            if isinstance(layer, QgsVectorLayer) and lid not in self.memory_data:
                self._add_connection(layer.beforeCommitChanges, functools.partial(self._before_commit, layer))
                self._add_connection(layer.committedFeaturesAdded, functools.partial(self._features_added, lid))

    def _add_connection(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _disconnect(self):
        for signal, slot in self._connections:
            with contextlib.suppress(TypeError, RuntimeError):
                signal.disconnect(slot)
        self._connections = []

    def _on_layers_removed(self, items):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        for item in items:
            lid = item if isinstance(item, str) else item.id()
            layer = self.layers.get(lid)
            if layer is None or lid in self.removed:
                continue
            try:
                if lid in self.memory_data:
                    copy = self._memory_copy(layer, self.memory_data[lid])
                elif isinstance(layer, QgsVectorLayer) and layer.providerType() == "memory":
                    copy = layer.materialize(QgsFeatureRequest())
                else:
                    copy = layer.clone()
                node = root.findLayer(lid)
                index = node.parent().children().index(node) if node is not None and node.parent() else 0
                self.removed[lid] = (copy, index)
            except Exception as e:
                self.not_undoable.append(f"removed layer '{layer.name()}' ({e})")

    @staticmethod
    def _memory_copy(layer, data):
        copy = layer.materialize(QgsFeatureRequest().setFilterFids([]))
        provider = copy.dataProvider()
        provider.deleteAttributes(list(range(len(copy.fields()))))
        provider.addAttributes([QgsField(f) for f in data["fields"]])
        copy.updateFields()
        _add_rows(copy, data["fields"], data["features"])
        return copy

    def _before_commit(self, layer, *_args):
        """Called just before the edit buffer is written: the provider still has the original data."""
        buffer = layer.editBuffer()
        if buffer is None:
            return
        try:
            provider = layer.dataProvider()
            pfields = provider.fields()
            layer_fields = layer.fields()
            record = {"added_ids": [], "deleted": [], "attrs": {}, "geoms": {},
                      "added_fields": [f.name() for f in buffer.addedAttributes()], "deleted_fields": []}
            deleted = [f for f in buffer.deletedFeatureIds() if f >= 0]
            changed_attrs = {fid: v for fid, v in buffer.changedAttributeValues().items() if fid >= 0}
            changed_geoms = {fid: g for fid, g in buffer.changedGeometries().items() if fid >= 0}
            wanted = set(deleted) | set(changed_attrs) | set(changed_geoms)
            originals = {}
            if wanted:
                request = QgsFeatureRequest().setFilterFids(list(wanted))
                originals = {f.id(): f for f in provider.getFeatures(request)}
            for fid in deleted:
                if fid in originals:
                    f = originals[fid]
                    record["deleted"].append(({pfields.at(i).name(): v for i, v in enumerate(f.attributes())},
                                              QgsGeometry(f.geometry())))
            for fid, values in changed_attrs.items():
                original = originals.get(fid)
                if original is None:
                    continue
                old = {}
                for index in values:
                    if 0 <= index < layer_fields.count():
                        name = layer_fields.at(index).name()
                        pindex = pfields.indexFromName(name)
                        if pindex >= 0:
                            old[name] = original.attributes()[pindex]
                record["attrs"][fid] = old
            for fid in changed_geoms:
                if fid in originals:
                    record["geoms"][fid] = QgsGeometry(originals[fid].geometry())
            for index in buffer.deletedAttributeIds():
                if 0 <= index < pfields.count():
                    field = QgsField(pfields.at(index))
                    if layer.featureCount() > DELETED_FIELD_VALUES_LIMIT:
                        self.not_undoable.append(f"deleted field '{field.name()}' of '{layer.name()}' (too large)")
                        continue
                    values = {f.id(): f.attributes()[index] for f in provider.getFeatures()}
                    record["deleted_fields"].append((field, values))
            self.edits.setdefault(layer.id(), []).append(record)
        except Exception as e:
            self.not_undoable.append(f"edits of '{layer.name()}' ({e})")
            log_warning(f"Undo: could not record edits of '{layer.name()}': {e}")

    def _features_added(self, lid, _layer_id, features):
        records = self.edits.get(lid)
        if records:
            records[-1]["added_ids"].extend(f.id() for f in features)

    # ------------------------------------------------------------------ after the action
    def finish(self):
        """Stops recording and lists the changes. Returns True if there is anything to undo."""
        self._disconnect()
        project = QgsProject.instance()
        now = project.mapLayers()
        added = [layer.name() for lid, layer in now.items() if lid not in self.layers]
        if added:
            self.changes.append(f"remove {len(added)} new layer(s): {', '.join(added[:5])}"
                                + (" …" if len(added) > 5 else ""))
        if self.removed:
            names = [self.states[lid].name for lid in self.removed]
            self.changes.append(f"bring back {len(names)} removed layer(s): {', '.join(names[:5])}")
        for lid, layer in now.items():
            state = self.states.get(lid)
            if state is None:
                continue
            what = []
            if state.style is not None and _style_xml(layer) != state.style:
                what.append("style")
            if isinstance(layer, QgsVectorLayer):
                if layer.subsetString() != state.subset:
                    what.append("filter")
                if [_join_key(j) for j in _joins(layer)] != [_join_key(j) for j in state.joins]:
                    what.append("joins")
            if layer.name() != state.name:
                what.append("name")
            if _visible(lid) != state.visible:
                what.append("visibility")
            if lid in self.memory_data and isinstance(layer, QgsVectorLayer):
                if _data_hash(layer) != self.memory_data[lid]["hash"]:
                    what.append("data")
            elif lid in self.edits:
                what.append("data")
            elif isinstance(layer, QgsVectorLayer) and not layer.isEditable() and (
                    layer.featureCount() != state.count or [f.name() for f in layer.fields()] != state.field_names):
                self.not_undoable.append(f"data of '{layer.name()}' changed directly in the file")
            if isinstance(layer, QgsVectorLayer) and layer.isEditable() and not state.editable:
                what.append("unsaved edits")
            if what:
                self.changes.append(f"restore {', '.join(what)} of '{state.name}'")
        new_layouts = [lay.name() for lay in project.layoutManager().layouts() if lay.name() not in self.layouts]
        if new_layouts:
            self.changes.append(f"remove layout(s): {', '.join(new_layouts)}")
        if project.crs() != self.crs:
            self.changes.append("restore the project CRS")
        iface = _iface()
        canvas = iface.mapCanvas() if iface is not None else None
        if canvas is not None and self.extent is not None and canvas.extent() != self.extent:
            self.changes.append("restore the map view")
        return bool(self.changes)

    def summary(self):
        return "; ".join(self.changes)

    # ------------------------------------------------------------------ undo
    def restore(self):
        """Puts the project back to the snapshot. Returns a list of messages."""
        project = QgsProject.instance()
        messages = []
        now = project.mapLayers()
        new_ids = [lid for lid in now if lid not in self.layers]
        if new_ids:
            project.removeMapLayers(new_ids)
            messages.append(f"Removed {len(new_ids)} layer(s) created by the AI.")
        root = project.layerTreeRoot()
        for lid, (copy, index) in self.removed.items():
            if project.mapLayer(lid) is not None:
                continue
            state = self.states[lid]
            project.addMapLayer(copy, False)
            root.insertLayer(min(index, len(root.children())), copy)
            copy.setName(state.name)
            if state.style:
                _apply_style(copy, state.style)
            messages.append(f"Layer '{state.name}' is back.")
        for lid, layer in project.mapLayers().items():
            state = self.states.get(lid)
            if state is None or lid in self.removed:
                continue
            try:
                messages.extend(self._restore_layer(layer, state))
            except Exception as e:
                messages.append(f"Could not fully restore '{state.name}': {e}")
                log_warning(f"Undo of '{state.name}' failed: {e}")
        manager = project.layoutManager()
        for layout in list(manager.layouts()):
            if layout.name() not in self.layouts:
                manager.removeLayout(layout)
                messages.append(f"Removed layout '{layout.name()}'.")
        if project.crs() != self.crs:
            project.setCrs(self.crs)
            messages.append("Project CRS restored.")
        iface = _iface()
        canvas = iface.mapCanvas() if iface is not None else None
        if canvas is not None and self.extent is not None and canvas.extent() != self.extent:
            canvas.setExtent(self.extent)
            canvas.refresh()
        return messages

    def _restore_layer(self, layer, state):
        messages = []
        lid = layer.id()
        is_vector = isinstance(layer, QgsVectorLayer)
        if is_vector and layer.isEditable() and not state.editable:
            layer.rollBack()
        if lid in self.memory_data and is_vector:
            data = self.memory_data[lid]
            if _data_hash(layer) != data["hash"]:
                _replace_memory_data(layer, data)
                messages.append(f"Data of '{state.name}' restored.")
        elif lid in self.edits and is_vector:
            for record in reversed(self.edits[lid]):
                _revert_edits(layer, record)
            messages.append(f"Edits of '{state.name}' reverted.")
        if is_vector:
            current = [_join_key(j) for j in _joins(layer)]
            wanted = [_join_key(j) for j in state.joins]
            if current != wanted:
                for join in _joins(layer):
                    layer.removeJoin(join.joinLayerId())
                for join in state.joins:
                    layer.addJoin(QgsVectorLayerJoinInfo(join))
                messages.append(f"Joins of '{state.name}' restored.")
            if layer.subsetString() != state.subset:
                layer.setSubsetString(state.subset or "")
        if state.style is not None and _style_xml(layer) != state.style:
            _apply_style(layer, state.style)
            messages.append(f"Style of '{state.name}' restored.")
        if layer.name() != state.name:
            layer.setName(state.name)
        node = QgsProject.instance().layerTreeRoot().findLayer(lid)
        if node is not None and state.visible is not None and node.itemVisibilityChecked() != state.visible:
            node.setItemVisibilityChecked(state.visible)
        layer.triggerRepaint()
        return messages


def _add_rows(layer, fields, rows):
    target = layer.fields()
    mapping = [target.indexFromName(f.name()) for f in fields]
    features = []
    for attrs, geom in rows:
        feature = QgsFeature(target)
        values = [None] * target.count()
        for source_index, target_index in enumerate(mapping):
            if target_index >= 0 and source_index < len(attrs):
                values[target_index] = attrs[source_index]
        feature.setAttributes(values)
        feature.setGeometry(QgsGeometry(geom))
        features.append(feature)
    if features:
        layer.dataProvider().addFeatures(features)


def _replace_memory_data(layer, data):
    provider = layer.dataProvider()
    provider.deleteFeatures([f.id() for f in layer.getFeatures(QgsFeatureRequest().setNoAttributes())])
    wanted = [f.name() for f in data["fields"]]
    extra = [i for i, f in enumerate(provider.fields()) if f.name() not in wanted]
    if extra:
        provider.deleteAttributes(extra)
    have = [f.name() for f in provider.fields()]
    missing = [QgsField(f) for f in data["fields"] if f.name() not in have]
    if missing:
        provider.addAttributes(missing)
    layer.updateFields()
    _add_rows(layer, data["fields"], data["features"])
    layer.updateExtents()


def _revert_edits(layer, record):
    provider = layer.dataProvider()
    if record["added_ids"]:
        provider.deleteFeatures(record["added_ids"])
    added = [provider.fields().indexFromName(n) for n in record["added_fields"]]
    added = [i for i in added if i >= 0]
    if added:
        provider.deleteAttributes(added)
        layer.updateFields()
    for field, values in record["deleted_fields"]:
        if provider.fields().indexFromName(field.name()) < 0:
            provider.addAttributes([field])
            layer.updateFields()
        index = provider.fields().indexFromName(field.name())
        if index >= 0 and values:
            provider.changeAttributeValues({fid: {index: v} for fid, v in values.items()})
    if record["attrs"]:
        pfields = provider.fields()
        changes = {}
        for fid, old in record["attrs"].items():
            mapped = {pfields.indexFromName(n): v for n, v in old.items() if pfields.indexFromName(n) >= 0}
            if mapped:
                changes[fid] = mapped
        if changes:
            provider.changeAttributeValues(changes)
    if record["geoms"]:
        provider.changeGeometryValues(record["geoms"])
    if record["deleted"]:
        pfields = provider.fields()
        features = []
        for attrs, geom in record["deleted"]:
            feature = QgsFeature(pfields)
            feature.setAttributes([attrs.get(f.name()) for f in pfields])
            feature.setGeometry(geom)
            features.append(feature)
        provider.addFeatures(features)
    layer.updateFields()
    layer.updateExtents()
    layer.reload()


class UndoManager:
    """Stack of recent AI actions that can be undone, newest last."""

    def __init__(self):
        self.stack = []
        self._active = None

    def begin(self, label):
        """Takes a snapshot before an action. Returns it (pass it to end())."""
        try:
            self._active = Snapshot(label)
        except Exception as e:  # undo must never block the action itself
            log_warning(f"Undo snapshot failed: {e}")
            self._active = None
        return self._active

    def end(self, snapshot):
        """After the action: keeps the snapshot if something changed. Returns (summary, not_undoable list)."""
        if snapshot is None:
            return "", []
        self._active = None
        try:
            changed = snapshot.finish()
        except Exception as e:
            log_warning(f"Undo: could not compare the project: {e}")
            return "", []
        if changed:
            self.stack.append(snapshot)
            del self.stack[:-MAX_UNDO]
        return (snapshot.summary() if changed else ""), snapshot.not_undoable

    def can_undo(self):
        return bool(self.stack)

    def last_label(self):
        return self.stack[-1].label if self.stack else ""

    def last_summary(self):
        return self.stack[-1].summary() if self.stack else ""

    def undo(self):
        """Undoes the newest action. Returns a list of messages."""
        if not self.stack:
            return ["Nothing to undo."]
        snapshot = self.stack.pop()
        return snapshot.restore() or ["Nothing had to be changed."]

    def clear(self):
        self.stack.clear()


UNDO = UndoManager()
