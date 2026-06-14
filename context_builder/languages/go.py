"""Go language profile."""

import re

from .base import LanguageProfile


class GoProfile(LanguageProfile):
    """Go syntax behavior."""

    name = "go"
    extensions = frozenset({".go"})

    def get_definition_patterns(self, func_name):
        lead_b = r'\b' if func_name[0].isalnum() or func_name[0] == '_' else ''
        trail_b = r'\b' if func_name[-1].isalnum() or func_name[-1] == '_' else ''
        escaped = re.escape(func_name)
        # func myFunc(...) or func (r *Receiver) myFunc(...)
        return [
            re.compile(r'\bfunc\s+(?:\([^)]*\)\s*)?' + lead_b + escaped + trail_b)
        ]


GO = GoProfile()
