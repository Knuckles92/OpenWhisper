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
    "parakeet_unified": ("en",),
}


def language_choices(backend: str) -> tuple[str, ...]:
    return _BACKEND_LANGUAGES.get(backend, ())


def model_language_choices(model_name: str | None, backend: str = "") -> tuple[str, ...]:
    """Language codes supported by a specific model, falling back to its backend."""
    if model_name:
        from services.local_asr.catalog import MODELS

        model = MODELS.get(model_name)
        if model is not None:
            if model.language_codes:
                return model.language_codes
            backend = backend or model.backend
    return language_choices(backend)


def selected_language(backend: str, stored: str | None, *, model_name: str | None = None) -> str:
    choices = model_language_choices(model_name, backend) if model_name else language_choices(backend)
    return stored if stored in choices else "en" if choices == ("en",) else "auto"


_QWEN_LANGUAGES = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ar": "Arabic",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish",
    "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish",
    "da": "Danish", "fi": "Finnish", "pl": "Polish", "cs": "Czech",
    "fil": "Filipino", "fa": "Persian", "el": "Greek",
    "ro": "Romanian", "hu": "Hungarian", "mk": "Macedonian",
}


def qwen_language_code(language: str | None) -> str | None:
    if not language or language == "auto":
        return None
    for code, name in _QWEN_LANGUAGES.items():
        if language == name:
            return code
    code = language.replace("_", "-").split("-", 1)[0].lower()
    if code == "tl":
        code = "fil"
    if code not in _QWEN_LANGUAGES:
        raise ValueError(f"Qwen does not support language {language!r}. Choose Auto or a supported language.")
    return code


def qwen_language_name(language: str | None) -> str | None:
    return _QWEN_LANGUAGES.get(qwen_language_code(language))


def native_language_code(backend: str, language: str | None) -> str | None:
    # The pinned Nemotron GGUF has zh-CN / zh-TW prompts, but no bare zh.
    return "zh-CN" if backend == "nemotron" and language == "zh" else language
