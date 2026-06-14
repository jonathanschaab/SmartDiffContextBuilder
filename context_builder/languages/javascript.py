"""JavaScript and TypeScript language profiles."""

import re

from .base import LanguageProfile


class JavaScriptProfile(LanguageProfile):
    """JavaScript syntax behavior."""

    name = "javascript"
    extensions = frozenset({".js"})

    def get_definition_patterns(self, func_name):
        # Create a strict custom left boundary using a fixed-width negative lookbehind
        if func_name[0].isalnum() or func_name[0] in '$_':
            lead_b = r'(?<![$_a-zA-Z0-9])'
        else:
            lead_b = ''

        # Create a strict custom right boundary using a negative lookahead
        if func_name[-1].isalnum() or func_name[-1] in '$_':
            trail_b = r'(?![$_a-zA-Z0-9])'
        else:
            trail_b = ''

        escaped = re.escape(func_name)
        return [
            re.compile(r'\b(?:function|class)\s+' + lead_b + escaped + trail_b),
            re.compile(
                lead_b + escaped + trail_b +
                r'\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*=>'
            ),
            re.compile(
                r'^\s*(?:async\s+|\*\s*|get\s+|set\s+|public\s+|private\s+|'
                r'protected\s+|static\s+|readonly\s+)*' +
                lead_b + escaped + trail_b + r'\s*\([^)]*\)\s*(?::\s*[^{]+)?\{'
            ),
        ]

    def get_call_pattern(self, func_name):
        # Create a strict custom left boundary using a fixed-width negative lookbehind
        if func_name[0].isalnum() or func_name[0] in '$_':
            lead_b = r'(?<![$_a-zA-Z0-9])'
        else:
            lead_b = ''

        # Create a strict custom right boundary using a negative lookahead
        if func_name[-1].isalnum() or func_name[-1] in '$_':
            trail_b = r'(?![$_a-zA-Z0-9])'
        else:
            trail_b = ''

        escaped = re.escape(func_name)
        return re.compile(lead_b + escaped + trail_b)


class TypeScriptProfile(JavaScriptProfile):
    """TypeScript syntax and tooling behavior."""

    name = "typescript"
    extensions = frozenset({".ts"})
    lsp_command = ("typescript-language-server", "--stdio")


JAVASCRIPT = JavaScriptProfile()
TYPESCRIPT = TypeScriptProfile()
