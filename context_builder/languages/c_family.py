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
        lead_b = r'\b' if func_name[0].isalnum() or func_name[0] == '_' else ''
        trail_b = r'\b' if func_name[-1].isalnum() or func_name[-1] == '_' else ''
        escaped = re.escape(func_name)
        return [
            re.compile(r'\b(?:class|struct)\s+' + lead_b + escaped + trail_b)
        ]


C_FAMILY = CFamilyProfile()
