"""Java language profile."""

import re

from .base import LanguageProfile


class JavaProfile(LanguageProfile):
    """Java-specific syntax and tooling behavior."""

    name = "java"
    extensions = frozenset({".java"})
    comment_prefix = "//"
    line_comment = "//"
    block_comment_start = "/*"
    block_comment_end = "*/"
    supports_block_comments = True
    supports_nested_block_comments = False
    uses_indentation_blocks = False
    lsp_command = ("jdtls",)

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            # Class, Interface, Enum, Record definitions
            re.compile(
                r'\b(?:class|interface|enum|record)\s+'
                + lead_b + escaped + trail_b
            ),
            # Annotation type definition (@interface)
            re.compile(r'@interface\s+' + lead_b + escaped + trail_b),
            # Method and Constructor definitions
            # Matches modifiers, optional generic params (<T>), optional return type,
            # method name, and parameter list start
            re.compile(
                r'^\s*(?:(?:public|protected|private|static|final|abstract|'
                r'synchronized|native|strictfp|default|transient|volatile)\s+)*'
                r'(?:<[^>]+>\s*)?'
                r'(?:(?:[A-Za-z0-9_<>\[\],.@]+\s+)+)?'
                + lead_b + escaped + trail_b + r'\s*\('
            )
        ]


JAVA = JavaProfile()
