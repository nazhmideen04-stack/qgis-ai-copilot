def classFactory(iface):
    from .plugin import AICopilotPlugin
    return AICopilotPlugin(iface)