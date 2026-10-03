"""Common language presets and the pinned runtimes' language contracts."""
from __future__ import annotations

LANGUAGE_LABELS = {
    "en": "English", "ru": "Russian", "es": "Spanish", "fr": "French",
    "pt": "Portuguese", "zh": "Mandarin", "auto": "Auto",
}

# Parakeet v3 / Orukeet cover 25 European languages. Qwen covers 30;
# Nemotron includes zh-CN in its broader coverage tier. Moonshine is English-only.
_BACKEND_LANGUAGES = {
    "parakeet": ("en", "ru", "es", "fr", "pt", "auto"),
    "parakeet_mlx": ("auto",),
    "qwen_asr": tuple(LANGUAGE_LABELS),
    "nemotron": tuple(LANGUAGE_LABELS),
    "moonshine": ("en",),
}


def language_choices(backend: str) -> tuple[str, ...]:
    return _BACKEND_LANGUAGES.get(backend, ())


def selected_language(backend: str, stored: str | None) -> str:
    choices = language_choices(backend)
    return stored if stored in choices else "en" if backend == "moonshine" else "auto"


_QWEN_LANGUAGES = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ar": "Arabic",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish",
    "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "tl": "Filipino", "fil": "Filipino", "fa": "Persian", "el": "Greek",
    "ro": "Romanian", "hu": "Hungarian", "mk": "Macedonian",
}


def qwen_language_name(language: str | None) -> str | None:
    if not language or language == "auto":
        return None
    if language in _QWEN_LANGUAGES.values():
        return language
    code = language.replace("_", "-").split("-", 1)[0].lower()
    if code not in _QWEN_LANGUAGES:
        raise ValueError(f"Qwen does not support language {language!r}. Choose Auto or a supported language.")
    return _QWEN_LANGUAGES[code]


def native_language_code(backend: str, language: str | None) -> str | None:
    # The pinned Nemotron GGUF has zh-CN / zh-TW prompts, but no bare zh.
    return "zh-CN" if backend == "nemotron" and language == "zh" else language
