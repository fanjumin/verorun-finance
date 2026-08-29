"""Plugin i18n bridge — allows module-level t() access."""
_current_plugin = None


def set_plugin(plugin):
    global _current_plugin
    _current_plugin = plugin


def t(text, locale=None):
    if _current_plugin:
        return _current_plugin.t(text, locale)
    return text
