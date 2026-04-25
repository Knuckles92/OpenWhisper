"""
Style factory for creating waveform visualization styles.
"""
from typing import Dict, Any, Optional
from .base_style import BaseWaveformStyle
from .particle_style import ParticleStyle


# Registry of available styles
_style_registry: Dict[str, type] = {
    'particle': ParticleStyle,
}


def get_available_styles() -> Dict[str, type]:
    """Get dictionary of all available styles.

    Returns:
        Dictionary mapping style names to style classes
    """
    return _style_registry.copy()


def create_style(style_name: str, width: int, height: int, config: Optional[Dict[str, Any]] = None) -> BaseWaveformStyle:
    """Create a waveform style instance.

    Args:
        style_name: Name of the style to create
        width: Canvas width
        height: Canvas height
        config: Style configuration dictionary

    Returns:
        Instance of the requested style

    Raises:
        ValueError: If style_name is not registered
    """
    if style_name not in _style_registry:
        raise ValueError(f"Unknown style '{style_name}'. Available styles: {list(_style_registry.keys())}")

    style_class = _style_registry[style_name]

    # Get default config if none provided
    if config is None:
        from config import config as app_config
        config = app_config.WAVEFORM_STYLE_CONFIGS.get(style_name, {})

    return style_class(width, height, config)


def register_style(name: str, style_class: type):
    """Register a new style.

    Args:
        name: Name to register the style under
        style_class: Style class (must inherit from BaseWaveformStyle)

    Raises:
        TypeError: If style_class doesn't inherit from BaseWaveformStyle
    """
    if not issubclass(style_class, BaseWaveformStyle):
        raise TypeError(f"Style class must inherit from BaseWaveformStyle")

    _style_registry[name] = style_class
