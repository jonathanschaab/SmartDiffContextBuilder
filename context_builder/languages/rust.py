"""Rust language profile."""

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


RUST = RustProfile()
