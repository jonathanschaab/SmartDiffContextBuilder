"""Language profiles used by shared repository analysis."""

from .registry import get_language_profile
from .unknown_language import UNKNOWN_LANGUAGE

__all__ = ["UNKNOWN_LANGUAGE", "get_language_profile"]
