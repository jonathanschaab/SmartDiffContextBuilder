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
    multiline_string_delimiters = ('"""',)

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
            # Method and Constructor definitions (with modifiers or return type)
            re.compile(
                r'^\s*(?:<[^>]+>\s*)?'
                r'(?:(?:(?!return\b|throw\b|new\b|else\b|case\b|if\b|while\b|'
                r'for\b|switch\b|assert\b)[A-Za-z0-9_<>\[\],.@]+\s+)+)'
                + lead_b + escaped + trail_b + r'\s*\('
            ),
            # Package-private constructor definition (no modifiers/return type, no semicolon)
            re.compile(
                r'^\s*(?!.*;)' + lead_b + escaped + trail_b + r'\s*\('
            )
        ]


JAVA = JavaProfile()
