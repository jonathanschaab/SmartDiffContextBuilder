"""Python language profile."""

import re

from .base import LanguageProfile


class PythonProfile(LanguageProfile):
    """Python-specific syntax and tooling behavior."""

    name = "python"
    extensions = frozenset({".py"})
    comment_prefix = "#"
    line_comment = "#"
    supports_block_comments = False
    uses_indentation_blocks = True
    lsp_command = ("pylsp",)
    test_query = (
        "(function_definition name: (identifier) @name "
        '(#match? @name "^test_"))'
    )

    def get_definition_patterns(self, func_name):
        lead_b = r'\b' if func_name[0].isalnum() or func_name[0] == '_' else ''
        trail_b = r'\b' if func_name[-1].isalnum() or func_name[-1] == '_' else ''
        escaped = re.escape(func_name)
        return [
            re.compile(r'\b(?:def|class)\s+' + lead_b + escaped + trail_b),
            re.compile(r'\b' + lead_b + escaped + trail_b + r'\s*=\s*lambda\b')
        ]


PYTHON = PythonProfile()
