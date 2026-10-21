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
