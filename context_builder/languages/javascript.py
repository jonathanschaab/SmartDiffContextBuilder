"""JavaScript and TypeScript language profiles."""

import re

from .base import LanguageProfile


class JavaScriptProfile(LanguageProfile):
    """JavaScript syntax behavior."""

    name = "javascript"
    extensions = frozenset({".js"})

    def _get_boundaries(self, func_name):
        """Return the JS/TS specific regex boundary patterns (lead_b, trail_b) for func_name."""
        if not func_name:
            return '', ''
        lead_b = r'(?<![$_a-zA-Z0-9])' if func_name[0].isalnum() or func_name[0] in '$_' else ''
        trail_b = r'(?![$_a-zA-Z0-9])' if func_name[-1].isalnum() or func_name[-1] in '$_' else ''
        return lead_b, trail_b

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            re.compile(
                r'\b(?:function(?:\s*\*\s*|\s+)|class\s+|interface\s+|type\s+|'
                r'const\s+|let\s+|var\s+)'
                + lead_b + escaped + trail_b
            ),
            re.compile(
                lead_b + escaped + trail_b +
                r'\s*[=:]\s*(?:async\s*)?(?:<[^>]+>)?\s*(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*=>'
            ),
            re.compile(
                r'^\s*(?:async\s+|\*\s*|get\s+|set\s+|public\s+|private\s+|'
                r'protected\s+|static\s+|readonly\s+)*' +
                lead_b + escaped + trail_b +
                r'\s*(?:<[^>]+>)?\s*\([^)]*\)\s*(?::\s*(?:[^{;]|\{[^}]*\})*)?\{'
            ),
            re.compile(
                r'^\s*(?:async\s+|\*\s*|get\s+|set\s+|public\s+|private\s+|'
                r'protected\s+|static\s+|readonly\s+)+' +
                lead_b + escaped + trail_b + r'\s*(?:<[^>]+>)?\s*\('
            ),
        ]


class TypeScriptProfile(JavaScriptProfile):
    """TypeScript syntax and tooling behavior."""

    name = "typescript"
    extensions = frozenset({".ts"})
    lsp_command = ("typescript-language-server", "--stdio")


JAVASCRIPT = JavaScriptProfile()
TYPESCRIPT = TypeScriptProfile()
