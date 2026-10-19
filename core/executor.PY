import re
import traceback
import processing
from qgis.core import QgsProject
from qgis.utils import iface

def extract_code(text: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()

def run_pyqgis_code(code: str) -> tuple[bool, str]:
    scope = {
        "iface": iface,
        "QgsProject": QgsProject,
        "processing": processing,
    }
    try:
        exec(code, scope)
        iface.mapCanvas().refresh()
        return True, "Executed successfully. Map canvas updated."
    except Exception:
        return False, traceback.format_exc()