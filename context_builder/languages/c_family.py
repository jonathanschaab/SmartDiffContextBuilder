"""C and C++ language profile."""

import re

from .base import LanguageProfile


class CFamilyProfile(LanguageProfile):
    """Capabilities shared by C and C++ source/header files."""

    name = "c-family"
    extensions = frozenset({
        ".c", ".cc", ".cpp", ".cxx",
        ".h", ".hpp", ".hxx",
    })
    supports_macro_expansion = True
    supports_compile_commands = True
    uses_c_style_definitions = True
    supports_cpp_raw_strings = True
    lsp_command = ("clangd", "--background-index")

    def _get_boundaries(self, func_name):
        """Return the C-family specific regex boundary patterns (lead_b, trail_b) for func_name."""
        if not func_name:
            return '', ''
        lead_b = (
            r'(?<![a-zA-Z0-9_])'
            if func_name[0].isalnum() or func_name[0] in ('_', '~')
            else ''
        )
        trail_b = (
            r'(?![a-zA-Z0-9_])'
            if func_name[-1].isalnum() or func_name[-1] == '_'
            else ''
        )
        return lead_b, trail_b

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            re.compile(
                r'\b(?:class|struct|union|enum)\s+'
                + lead_b + escaped + trail_b
            ),
            re.compile(
                r'\b(?<!\busing\s)(?<!\busing\s\s)(?<!\busing\s\s\s)namespace\s+'
                + lead_b + escaped + trail_b
            ),
            re.compile(
                r'\busing\s+' + lead_b + escaped + trail_b + r'\s*='
            ),
            re.compile(
                r'\btypedef\b[^;]+' + lead_b + escaped + trail_b
            ),
            re.compile(
                r'^\s*\}\s*' + lead_b + escaped + trail_b + r'\s*;'
            )
        ]


C_FAMILY = CFamilyProfile()
