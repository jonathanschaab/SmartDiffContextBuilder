"""Fallback behavior for unrecognized languages."""

from .base import LanguageProfile


class UnknownLanguageProfile(LanguageProfile):
    """Conservative C-like fallback matching the legacy analyzer behavior."""


UNKNOWN_LANGUAGE = UnknownLanguageProfile()
