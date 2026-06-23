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
    keywords = frozenset({
        'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
        'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
        'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
        'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
        'while', 'with', 'yield', 'self'
    })
    flow_keywords = frozenset({
        'return', 'if', 'elif', 'else', 'for', 'while', 'with', 'try',
        'except', 'finally', 'raise', 'yield', 'await', 'break', 'continue',
        'pass', 'assert', 'del', 'lambda', 'in', 'is', 'not', 'and', 'or',
        'print',
    })
    declaration_query = "[(assignment) @assign]"

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            re.compile(r'\b(?:def|class)\s+' + lead_b + escaped + trail_b),
            re.compile(lead_b + escaped + trail_b + r'\s*=\s*lambda\b')
        ]


PYTHON = PythonProfile()
