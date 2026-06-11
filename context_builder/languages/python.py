"""Python language profile."""

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


PYTHON = PythonProfile()
