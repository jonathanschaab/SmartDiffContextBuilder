"""Shared language profile primitives."""

import re


_STRING_LITERAL_PATTERN = re.compile(r'(["\'])(?:(?=(\\?))\2.)*?\1')
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/")
_DECLARATION_PATTERN = re.compile(
    r"\b(?:fn|def|function|sub|func|class|macro)\s+([A-Za-z0-9_]+)"
)
_CALL_STYLE_PATTERN = re.compile(r"(~?\b[A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IGNORED_CALL_NAMES = {
    "if", "for", "while", "switch", "catch", "return", "sizeof",
    "sizeof_array", "__attribute__", "__declspec", "__pragma", "alignas",
    "alignof", "decltype", "noexcept", "static_assert", "typeof",
    "__typeof__", "throw", "typeid",
}


class LanguageProfile:
    """Describe language-specific behavior used by shared analysis engines."""

    name = "unknown"
    extensions = frozenset()
    comment_prefix = "//"
    line_comment = "//"
    block_comment_start = "/*"
    block_comment_end = "*/"
    supports_block_comments = True
    uses_indentation_blocks = False
    supports_macro_expansion = False
    supports_compile_commands = False
    uses_c_style_definitions = False
    lsp_command = None
    test_query = None
    tests_can_share_source_file = False

    @staticmethod
    def strip_string_literals(line):
        """Remove quoted strings before applying language comment rules."""
        return _STRING_LITERAL_PATTERN.sub("", line)

    def strip_strings_and_comments(self, line):
        """Remove strings and same-line comments before regex analysis."""
        cleaned = self.strip_string_literals(line)
        if self.supports_block_comments:
            cleaned = _BLOCK_COMMENT_PATTERN.sub("", cleaned)
        if self.line_comment and self.line_comment in cleaned:
            cleaned = cleaned.split(self.line_comment, 1)[0]
        return cleaned

    def format_omission_comment(self, message):
        """Format generated truncation text using valid language comments."""
        if (
            self.supports_block_comments
            and self.block_comment_start
            and self.block_comment_end
        ):
            return (
                f"{self.block_comment_start} ... [{message}] ... "
                f"{self.block_comment_end}"
            )
        comment_marker = self.line_comment or self.comment_prefix
        if not comment_marker:
            return f"... [{message}] ..."
        return f"{comment_marker} ... [{message}] ..."

    def extract_function_name(self, cleaned_chunk, start, end):
        """Extract a declaration name, with a conservative call-style fallback."""
        declaration = _DECLARATION_PATTERN.search(cleaned_chunk)
        if declaration:
            return declaration.group(1)

        for match in _CALL_STYLE_PATTERN.finditer(cleaned_chunk):
            name = match.group(1)
            if name not in _IGNORED_CALL_NAMES:
                return name

        return f"block_lines_{start}_{end}"

    def _get_boundaries(self, func_name):
        """Return the regex boundary patterns (lead_b, trail_b) for func_name."""
        if not func_name:
            return '', ''
        lead_b = r'\b' if func_name[0].isalnum() or func_name[0] == '_' else ''
        trail_b = r'\b' if func_name[-1].isalnum() or func_name[-1] == '_' else ''
        return lead_b, trail_b

    def get_definition_patterns(self, func_name):
        """Return a list of compiled regex patterns to identify a definition of func_name."""
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        # Default fallback/generic definition keywords
        pattern = re.compile(
            r'\b(?:fn|def|function|sub|func|class|macro)\s+' + lead_b + escaped + trail_b
        )
        return [pattern]

    def get_call_pattern(self, func_name):
        """Return a compiled regex pattern to identify a call to func_name."""
        lead_b, trail_b = self._get_boundaries(func_name)
        escaped = re.escape(func_name)
        return re.compile(lead_b + escaped + trail_b)
