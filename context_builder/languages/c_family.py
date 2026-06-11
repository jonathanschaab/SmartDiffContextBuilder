"""C and C++ language profile."""

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


C_FAMILY = CFamilyProfile()
