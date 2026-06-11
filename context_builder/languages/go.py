"""Go language profile."""

from .base import LanguageProfile


class GoProfile(LanguageProfile):
    """Go syntax behavior."""

    name = "go"
    extensions = frozenset({".go"})


GO = GoProfile()
