"""JSON-compatible values for desktop service events and domain responses."""
import dataclasses
from collections import deque
from datetime import date, datetime
from enum import Enum
from pathlib import Path


def serializable(value):
    if dataclasses.is_dataclass(value):
        return serializable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [serializable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return serializable(value.value)
    raise TypeError(f"Unsupported desktop response value: {type(value).__name__}")
