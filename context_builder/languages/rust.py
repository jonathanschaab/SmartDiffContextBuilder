"""Rust language profile."""

import re

from .base import LanguageProfile


class RustProfile(LanguageProfile):
    """Rust syntax and tooling behavior."""

    name = "rust"
    extensions = frozenset({".rs"})
    lsp_command = ("rust-analyzer",)
    test_query = (
        '(attribute_item (attribute (identifier) @attr (#eq? @attr "test")))'
    )
    tests_can_share_source_file = True
    supports_rust_raw_strings = True
    uses_rust_character_literals = True
    supports_nested_block_comments = True
    keywords = frozenset({
        'as', 'async', 'await', 'break', 'const', 'continue', 'crate', 'dyn',
        'else', 'enum', 'extern', 'false', 'fn', 'for', 'if', 'impl', 'in',
        'let', 'loop', 'match', 'mod', 'move', 'mut', 'pub', 'ref', 'return',
        'self', 'Self', 'static', 'struct', 'super', 'trait', 'true', 'type',
        'union', 'unsafe', 'use', 'where', 'while', 'yield', 'macro_rules'
    })
    flow_keywords = frozenset({
        'return', 'if', 'else', 'for', 'while', 'loop', 'match', 'break',
        'continue', 'await', 'yield', 'use', 'mod',
    })
    declaration_query = "[(let_declaration) @decl (assignment_expression) @assign]"

    def get_definition_patterns(self, func_name):
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return [
            re.compile(
                r'\b(?:fn|macro_rules!|struct|enum|union|type|trait|mod|const|static)\s+'
                + lead_b + escaped + trail_b
            )
        ]


RUST = RustProfile()
