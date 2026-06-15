"""Python language profile."""

import re

from .base import LanguageProfile


class PythonProfile(LanguageProfile):
    """Python-specific syntax and tooling behavior."""

    name = "python"
    extensions = frozenset({".py"})
    comment_prefix = "#"
    line_comment = "#"
    multiline_string_delimiters = ('"""', "'''")
    supports_block_comments = False
    uses_indentation_blocks = True
    lsp_command = ("pylsp",)
    test_query = (
        "(function_definition name: (identifier) @name "
        '(#match? @name "^test_"))'
    )

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            re.compile(r'\b(?:def|class)\s+' + lead_b + escaped + trail_b),
            re.compile(lead_b + escaped + trail_b + r'\s*=\s*lambda\b')
        ]


PYTHON = PythonProfile()
