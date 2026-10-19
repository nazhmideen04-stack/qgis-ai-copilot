from qgis.core import QgsProject, QgsWkbTypes

def get_project_context() -> str:
    project = QgsProject.instance()
    layers_info = []

    for _, layer in project.mapLayers().items():
        if not layer.isValid():
            continue

        info = f"- Layer: '{layer.name()}'"
        if layer.type() == layer.VectorLayer:
            geom = QgsWkbTypes.displayString(layer.wkbType())
            fields = [f.name() for f in layer.fields()][:10]
            info += f" [Vector: {geom}], Fields: {fields}, Count: {layer.featureCount()}"
        elif layer.type() == layer.RasterLayer:
            info += f" [Raster, Bands: {layer.bandCount()}]"
        
        info += f", CRS: {layer.crs().authid()}"
        layers_info.append(info)

    if not layers_info:
        return "No layers currently loaded in the project."

    return f"Project CRS: {project.crs().authid()}\nOpen Layers:\n" + "\n".join(layers_info)
