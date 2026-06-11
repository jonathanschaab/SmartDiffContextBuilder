"""JavaScript and TypeScript language profiles."""

from .base import LanguageProfile


class JavaScriptProfile(LanguageProfile):
    """JavaScript syntax behavior."""

    name = "javascript"
    extensions = frozenset({".js"})


class TypeScriptProfile(JavaScriptProfile):
    """TypeScript syntax and tooling behavior."""

    name = "typescript"
    extensions = frozenset({".ts"})
    lsp_command = ("typescript-language-server", "--stdio")


JAVASCRIPT = JavaScriptProfile()
TYPESCRIPT = TypeScriptProfile()
