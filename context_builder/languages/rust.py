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

    def get_definition_patterns(self, func_name):
        lead_b = r'\b' if func_name[0].isalnum() or func_name[0] == '_' else ''
        trail_b = r'\b' if func_name[-1].isalnum() or func_name[-1] == '_' else ''
        escaped = re.escape(func_name)
        return [
            re.compile(r'\b(?:fn|macro_rules!)\s+' + lead_b + escaped + trail_b)
        ]


RUST = RustProfile()
