"""Windows batch language profile."""

import re

from .base import LanguageProfile


_REM_COMMENT_PATTERN = re.compile(r"\brem\b", re.IGNORECASE)
_DOUBLE_COLON_COMMENT_PATTERN = re.compile(r"(?:\A|&|\||\()\s*(@\s*::|::)")


class BatchProfile(LanguageProfile):
    """Windows batch comment behavior."""

    name = "batch"
    extensions = frozenset({".bat"})
    comment_prefix = "REM"
    line_comment = "REM"
    supports_block_comments = False
    keywords = frozenset({
        'echo', 'rem', 'set', 'if', 'else', 'for', 'in', 'do', 'goto', 'call',
        'exit', 'pause', 'choice', 'shift', 'setlocal', 'endlocal', 'errorlevel'
    })

    def strip_strings_and_comments(self, line):
        """Strip case-insensitive REM tokens and double-colon :: comments."""
        cleaned = self.strip_string_literals(line)

        # Collect start indices of any matches
        comment_starts = []
        double_colon_match = _DOUBLE_COLON_COMMENT_PATTERN.search(cleaned)
        if double_colon_match:
            comment_starts.append(double_colon_match.start(1))

        rem_match = _REM_COMMENT_PATTERN.search(cleaned)
        if rem_match:
            comment_starts.append(rem_match.start())

        if comment_starts:
            return cleaned[:min(comment_starts)]
        return cleaned


BATCH = BatchProfile()
