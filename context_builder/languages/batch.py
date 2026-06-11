"""Windows batch language profile."""

from .base import LanguageProfile


class BatchProfile(LanguageProfile):
    """Windows batch comment behavior."""

    name = "batch"
    extensions = frozenset({".bat"})
    comment_prefix = "REM"
    line_comment = "REM"
    supports_block_comments = False


BATCH = BatchProfile()
