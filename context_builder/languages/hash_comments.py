"""Profiles for languages whose line comments begin with a hash."""

from .base import LanguageProfile


class HashCommentProfile(LanguageProfile):
    """Shared behavior for shell, Perl, Make, and CMake files."""

    name = "hash-comment"
    extensions = frozenset({".sh", ".pl", ".mk", ".cmake"})
    comment_prefix = "#"
    line_comment = "#"
    supports_block_comments = False


HASH_COMMENTS = HashCommentProfile()
