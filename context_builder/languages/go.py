"""Go language profile."""

import re

from .base import LanguageProfile


class GoProfile(LanguageProfile):
    """Go syntax behavior."""

    name = "go"
    extensions = frozenset({".go"})
    multiline_string_delimiters = ("`",)

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        # func myFunc(...) or func (r *Receiver) myFunc(...)
        # type MyType struct/interface/...
        # myFunc := func(...) or myFunc = func(...)
        return [
            re.compile(r'\bfunc\s+(?:\([^)]*\)\s*)?' + lead_b + escaped + trail_b),
            re.compile(r'\btype\s+' + lead_b + escaped + trail_b),
            re.compile(lead_b + escaped + trail_b + r'\s*(?::)?=\s*func\b')
        ]


GO = GoProfile()
