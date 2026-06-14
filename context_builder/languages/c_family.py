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
    lsp_command = ("clangd", "--background-index")

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            re.compile(
                r'\b(?:class|struct|union|enum|namespace)\s+'
                + lead_b + escaped + trail_b
            )
        ]


C_FAMILY = CFamilyProfile()
