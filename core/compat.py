"""
Compatibility layer: the same code runs on QGIS 3.28+ (Qt5 / PyQt5) and QGIS 4 (Qt6 / PyQt6).

Newer QGIS versions moved many enums into the Qgis namespace (Qgis.LayoutUnit, Qgis.DistanceUnit...)
and switched field types from QVariant to QMetaType. These helpers pick whatever the running version has.
"""
from qgis.core import Qgis, QgsField, QgsUnitTypes


def qgis_version_int():
    """E.g. 33410 for QGIS 3.34.10, 40000 for QGIS 4.0."""
    return Qgis.versionInt() if hasattr(Qgis, "versionInt") else Qgis.QGIS_VERSION_INT


def enum_value(owner, enum_name, member):
    """owner.enum_name.member (scoped, newer QGIS) or owner.member (older QGIS)."""
    scoped = getattr(owner, enum_name, None)
    if scoped is not None and hasattr(scoped, member):
        return getattr(scoped, member)
    return getattr(owner, member)


def message_level(name):
    """Qgis.MessageLevel.Info / Warning / Critical."""
    return enum_value(Qgis, "MessageLevel", name)


def layout_mm():
    """Millimetres for layout positions and sizes."""
    if hasattr(Qgis, "LayoutUnit"):
        return Qgis.LayoutUnit.Millimeters
    return enum_value(QgsUnitTypes, "LayoutUnit", "LayoutMillimeters")  # QGIS < 3.30


def distance_km():
    """Kilometres for scale bars."""
    if hasattr(Qgis, "DistanceUnit"):
        return Qgis.DistanceUnit.Kilometers
    return enum_value(QgsUnitTypes, "DistanceUnit", "DistanceKilometers")  # QGIS < 3.30


def distance_m():
    """Metres for scale bars."""
    if hasattr(Qgis, "DistanceUnit"):
        return Qgis.DistanceUnit.Meters
    return enum_value(QgsUnitTypes, "DistanceUnit", "DistanceMeters")  # QGIS < 3.30


def metres_per_map_unit(crs):
    """How many metres one map unit of the CRS is (degrees: ~111 km at the equator)."""
    if crs.isGeographic():
        return 111320.0
    return QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), distance_m())


_FIELD_KINDS = {
    "string": ("QString", "String"),
    "int": ("LongLong", "LongLong"),
    "double": ("Double", "Double"),
    "bool": ("Bool", "Bool"),
    "date": ("QDate", "Date"),
    "datetime": ("QDateTime", "DateTime"),
}


def new_field(name, kind="string"):
    """QgsField that works on every QGIS version. kind: string | int | double | bool | date | datetime.
    QGIS 3.38+ and Qt6 builds expect QMetaType types, older Qt5 versions QVariant types."""
    meta_name, variant_name = _FIELD_KINDS[str(kind).lower()]
    from qgis.PyQt.QtCore import QMetaType, QVariant
    # QVariant types: QGIS < 3.38 on Qt5. QMetaType types: QGIS 3.38+ and every Qt6 build (no QVariant.Double there).
    if qgis_version_int() < 33800 and hasattr(QVariant, variant_name):
        return QgsField(str(name), getattr(QVariant, variant_name))
    return QgsField(str(name), getattr(QMetaType.Type, meta_name))


def set_rect_null(rect):
    """Empty rectangle ready for combineExtentWith (setNull in 3.34+, setMinimal before)."""
    if hasattr(rect, "setNull"):
        rect.setNull()
    else:
        rect.setMinimal()
    return rect


def set_label_text(label, text_format, font):
    """Layout label style: text format in 3.24+, plain font in older versions."""
    if hasattr(label, "setTextFormat"):
        label.setTextFormat(text_format)
    else:
        label.setFont(font)


def no_geometry_flag():
    """QgsFeatureRequest flag: do not fetch geometries (Qgis.FeatureRequestFlag in 3.36+, older names before)."""
    from qgis.core import QgsFeatureRequest
    scoped = getattr(Qgis, "FeatureRequestFlag", None)
    if scoped is not None and hasattr(scoped, "NoGeometry"):
        return scoped.NoGeometry
    return enum_value(QgsFeatureRequest, "Flag", "NoGeometry")


def attribute_request(layer, field_names=None, geometry=False):
    """Feature request for a fast scan: only the given attributes, no geometry unless asked."""
    from qgis.core import QgsFeatureRequest
    request = QgsFeatureRequest()
    if not geometry:
        request.setFlags(no_geometry_flag())
    if field_names is not None:
        request.setSubsetOfAttributes(list(field_names), layer.fields())
    return request


def algorithm_runs_in_main_thread(alg):
    """True if a Processing algorithm must not run in a background thread (NoThreading flag)."""
    from qgis.core import QgsProcessingAlgorithm
    scoped = getattr(Qgis, "ProcessingAlgorithmFlag", None)
    flag = getattr(scoped, "NoThreading", None) if scoped is not None else None
    if flag is None:
        flag = getattr(QgsProcessingAlgorithm, "FlagNoThreading", None)
    if flag is None:
        return False
    try:
        return bool(alg.flags() & flag)
    except TypeError:
        return False


def is_expression_field(layer, index):
    """Virtual (expression) fields are computed for every feature: too slow for value scans."""
    from qgis.core import QgsFields
    origin = layer.fields().fieldOrigin(index)
    expression = getattr(Qgis, "FieldOrigin", None)
    if expression is not None and hasattr(expression, "Expression"):
        return origin == expression.Expression
    return origin == enum_value(QgsFields, "FieldOrigin", "OriginExpression")
