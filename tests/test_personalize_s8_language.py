"""The languages you dictate in: which the engine takes, and which one is next."""
import pytest

from services import dictation_language as dl
from services.settings import SettingsKey, settings_manager


@pytest.fixture(autouse=True)
def _forget_remote_host():
    dl.note_remote_runtime(None)
    yield
    dl.note_remote_runtime(None)


def _settings(engine="local_whisper", chosen=(), active="", **extra):
    return {
        SettingsKey.SELECTED_MODEL: engine,
        SettingsKey.DICTATION_LANGUAGES: list(chosen),
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: active,
        **extra,
    }


class TestChoices:
    @pytest.mark.parametrize("engine,expected", [
        ("local_whisper", ["en", "es", "de"]),
        ("api", ["en", "es", "de"]),
        ("parakeet", ["en", "es"]),
        ("moonshine", ["en"]),
        ("parakeet_mlx", []),
        ("remote", ["en", "es", "de"]),
    ])
    def test_only_languages_the_engine_takes(self, engine, expected):
        settings = _settings(engine, chosen=["en", "es", "de"])
        assert dl.language_choices(settings) == expected

    @pytest.mark.parametrize("model", ["small.en", "distil-large-v3", "tiny.en"])
    def test_english_only_whisper_models_take_english(self, model):
        settings = _settings(chosen=["es", "en"], **{SettingsKey.WHISPER_MODEL: model})
        assert dl.language_choices(settings) == ["en"]
        assert dl.single_language_reason(settings).startswith("This Whisper model understands English only")

    def test_saved_list_is_cleaned_up_without_losing_order(self):
        settings = _settings(chosen=["fr", "xx", "auto", "en", "fr", 3, None])
        assert dl.chosen_languages(settings) == ["fr", "en"]
        assert dl.chosen_languages({SettingsKey.DICTATION_LANGUAGES: "en"}) == []

    def test_remote_host_languages_once_known(self):
        settings = _settings("remote", chosen=["en", "de", "es"])
        dl.note_remote_runtime({
            "family": "parakeet", "languages": ["en", "ru", "es", "auto"],
            "selected": {"language": "es"},
        })
        assert dl.language_choices(settings) == ["en", "es"]
        assert dl.engine_language(settings) == "es"

        dl.note_remote_runtime({"family": "local_whisper", "languages": []})
        assert dl.language_choices(settings) == ["en", "de", "es"]

        dl.note_remote_runtime({"family": "moonshine", "languages": ["en"]})
        assert dl.language_choices(settings) == ["en"]
        assert dl.single_language_reason(settings) == "The other computer's engine uses one language."

        dl.note_remote_runtime(None)
        assert dl.language_choices(settings) == ["en", "de", "es"]

    @pytest.mark.parametrize("engine,reason", [
        ("moonshine", "Moonshine understands English only."),
        ("parakeet_mlx", "Parakeet MLX detects the language by itself."),
        ("parakeet", ""),
        ("local_whisper", ""),
    ])
    def test_single_language_engines_say_why(self, engine, reason):
        assert dl.single_language_reason(_settings(engine)) == reason

    def test_legacy_api_selection_counts_as_the_api(self):
        assert dl.engine({SettingsKey.SELECTED_MODEL: "api_whisper"}) == "api"
        assert dl.engine({SettingsKey.SELECTED_MODEL: "nonsense"}) == dl.engine({})


class TestJobLanguage:
    def test_active_language_is_used_only_when_the_engine_takes_it(self):
        assert dl.job_language(_settings(chosen=["en", "es"], active="es")) == "es"
        assert dl.job_language(_settings("parakeet", chosen=["en", "de"], active="de")) == ""
        assert dl.job_language(_settings(chosen=["en", "es"], active="")) == ""
        assert dl.job_language(_settings(chosen=[], active="es")) == ""

    def test_current_language_falls_back_to_the_engine(self):
        assert dl.current_language(_settings(chosen=["en", "es"], active="es")) == "es"
        assert dl.current_language(_settings(chosen=["en", "es"])) == "auto"
        parakeet = _settings("parakeet", chosen=["en", "es"], **{SettingsKey.LOCAL_ASR_LANGUAGE: "fr"})
        assert dl.current_language(parakeet) == "fr"
        assert dl.current_language(_settings("moonshine")) == "en"


class TestMutators:
    def test_cycle_wraps_through_the_choices(self):
        settings = _settings(chosen=["en", "es", "fr"], active="en")
        seen = []
        for _ in range(4):
            dl.cycle(settings)
            seen.append(settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE])
        assert seen == ["es", "fr", "en", "es"]

    def test_cycle_starts_after_the_engines_own_language(self):
        settings = _settings("parakeet", chosen=["es", "en", "fr"], **{SettingsKey.LOCAL_ASR_LANGUAGE: "en"})
        dl.cycle(settings)
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "fr"

        unknown = _settings(chosen=["es", "fr"])
        dl.cycle(unknown)
        assert unknown[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "es"

    @pytest.mark.parametrize("chosen", [[], ["en"], ["de", "en"]])
    def test_cycle_leaves_one_or_no_choice_alone(self, chosen):
        settings = _settings("moonshine", chosen=chosen, active="")
        before = dict(settings)
        dl.cycle(settings)
        assert settings == before

    def test_set_chosen_keeps_the_active_language_valid(self):
        settings = _settings(chosen=["en", "es"], active="es")
        dl.set_chosen(settings, ["fr", "es", "fr", "auto"])
        assert settings[SettingsKey.DICTATION_LANGUAGES] == ["fr", "es"]
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "es"

        dl.set_chosen(settings, ["de", "fr"])
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "de"

        dl.set_chosen(settings, [])
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == ""

    def test_set_active_ignores_languages_that_are_not_choices(self):
        settings = _settings(chosen=["en", "es"], active="en")
        dl.set_active(settings, "de")
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "en"
        dl.set_active(settings, "es")
        assert settings[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "es"

    def test_switching_never_touches_the_engine_language(self):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet",
            SettingsKey.LOCAL_ASR_LANGUAGE: "ru",
        })
        settings_manager.mutate_settings(lambda s: dl.set_chosen(s, ["en", "es"]))
        settings_manager.mutate_settings(dl.cycle)
        settings_manager.mutate_settings(lambda s: dl.set_active(s, "en"))
        saved = settings_manager.load_all_settings()
        assert saved[SettingsKey.LOCAL_ASR_LANGUAGE] == "ru"
        assert saved[SettingsKey.DICTATION_ACTIVE_LANGUAGE] == "en"
        assert dl.job_language(saved) == "en"


def test_labels():
    assert dl.label("es") == "Spanish"
    assert dl.label("zh") == "Chinese"
    assert dl.label("xx") == "xx"
    assert dl.short_label("es") == "ES"
    assert dl.short_label("auto") == "AUTO"
