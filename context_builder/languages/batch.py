"""Windows batch language profile."""

import re

from .base import LanguageProfile


_REM_COMMENT_PATTERN = re.compile(r"\brem\b", re.IGNORECASE)


class BatchProfile(LanguageProfile):
    """Windows batch comment behavior."""

    name = "batch"
    extensions = frozenset({".bat"})
    comment_prefix = "REM"
    line_comment = "REM"
    supports_block_comments = False

    def strip_strings_and_comments(self, line):
        """Strip case-insensitive REM tokens without matching longer words."""
        cleaned = self.strip_string_literals(line)
        comment = _REM_COMMENT_PATTERN.search(cleaned)
        if comment:
            return cleaned[:comment.start()]
        return cleaned


BATCH = BatchProfile()
