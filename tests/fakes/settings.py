"""An in-memory stand-in for ``services.settings.settings_manager``."""
from services.settings import SettingsKey


class InMemorySettings:
    """Keeps every setting in ``values``, which a test may read and edit directly.

    A dict passed in is kept, not copied, so the caller sees every save.
    """

    def __init__(self, values=None):
        self.values = {} if values is None else values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def save_setting(self, key, value):
        self.values[key] = value

    def load_all_settings(self):
        return dict(self.values)

    def save_all_settings(self, settings):
        self.values.clear()
        self.values.update(settings)

    def update_settings(self, updates, *, remove=()):
        self.values.update(updates)
        for key in remove:
            self.values.pop(key, None)
        return dict(self.values)

    def mutate_settings(self, mutator):
        return mutator(self.values)

    def load_model_selection(self):
        return self.values.get(SettingsKey.SELECTED_MODEL, "local_whisper")
