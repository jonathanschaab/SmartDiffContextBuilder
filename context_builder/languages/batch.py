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
        """Strip case-insensitive REM tokens and double-colon :: comments."""
        cleaned = self.strip_string_literals(line)
        
        # Check for double-colon comment
        double_colon_idx = cleaned.find("::")
        
        # Check for REM comment
        rem_match = _REM_COMMENT_PATTERN.search(cleaned)
        
        # Find the earliest comment marker
        comment_start = None
        if double_colon_idx != -1:
            comment_start = double_colon_idx
        if rem_match:
            if comment_start is None or rem_match.start() < comment_start:
                comment_start = rem_match.start()
                
        if comment_start is not None:
            return cleaned[:comment_start]
        return cleaned


BATCH = BatchProfile()
