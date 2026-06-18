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
    supports_nested_block_comments = False
    uses_indentation_blocks = False
    supports_macro_expansion = False
    supports_compile_commands = False
    uses_c_style_definitions = False
    lsp_command = None
    test_query = None
    tests_can_share_source_file = False
    multiline_string_delimiters = ()
    supports_cpp_raw_strings = False
    supports_rust_raw_strings = False
    uses_rust_character_literals = False
    _cached_block_comment_pattern = None
    _cached_string_literal_pattern = None
    _cached_nested_pattern = None
    _cached_inner_block_comment_pattern = None
    _CPP_RAW_STRING_PATTERN = (
        r'\b(?:u8|u|U|L)?R"(?P<cpp_raw_delim>[^ ()\\\t\r\n\v\f]{0,16})'
        r'\((?:.*?)\)(?P=cpp_raw_delim)"'
    )
    _RUST_RAW_STRING_PATTERN = (
        r'\b(?:br|cr|r)(?P<rust_raw_hashes>#*)"(?:.*?)"'
        r'(?P=rust_raw_hashes)'
    )
    _RUST_CHARACTER_LITERAL_PATTERN = (
        r'(?:"(?:[^"\\\r\n]|\\.)*")|'
        r"(?:'(?:\\[xX][0-9a-fA-F]{2}|\\u\{[0-9a-fA-F]{1,6}\}|\\.|[^'\\\r\n])')"
    )
    _STANDARD_STRING_PATTERN = (
        r'(?:"(?:[^"\\\r\n]|\\.)*")|(?:\'(?:[^\'\\\r\n]|\\.)*\')'
    )

    def strip_string_literals(self, line):
        """Remove quoted strings before applying language comment rules."""
        pattern = self._cached_string_literal_pattern
        if pattern is None:
            parts = []
            if self.supports_cpp_raw_strings:
                parts.append(self._CPP_RAW_STRING_PATTERN)
            if self.supports_rust_raw_strings:
                parts.append(self._RUST_RAW_STRING_PATTERN)
            if self.multiline_string_delimiters:
                for delim in self.multiline_string_delimiters:
                    escaped_delim = re.escape(delim)
                    first_char_escaped = re.escape(delim[0])
                    pattern_str = (
                        rf'{escaped_delim}'
                        rf'(?:[^\\{first_char_escaped}]|\\.|'
                        rf'(?!{escaped_delim}){first_char_escaped})*?'
                        rf'{escaped_delim}'
                    )
                    parts.append(pattern_str)
                for delim in self.multiline_string_delimiters:
                    parts.append(re.escape(delim) + r'[^\r\n]*')
            if self.uses_rust_character_literals:
                parts.append(self._RUST_CHARACTER_LITERAL_PATTERN)
            else:
                parts.append(self._STANDARD_STRING_PATTERN)
            pattern = re.compile('|'.join(parts), re.DOTALL)
            self._cached_string_literal_pattern = pattern
        return pattern.sub(lambda m: "\n" * m.group(0).count("\n"), line)

    def strip_strings_and_comments(self, line):
        """Remove strings and same-line comments before regex analysis."""
        cleaned = self.strip_string_literals(line)
        if self.supports_block_comments:
            if self.supports_nested_block_comments:
                cleaned = self._strip_nested_block_comments_only(cleaned)
            else:
                cleaned = _BLOCK_COMMENT_PATTERN.sub("", cleaned)
        if self.line_comment and self.line_comment in cleaned:
            cleaned = cleaned.split(self.line_comment, 1)[0]
        return cleaned

    def _find_nested_block_comment_end(self, text, start_pos, pattern):
        """Scan forward to find the matching end of a nested block comment."""
        depth = 1
        sp = start_pos
        while depth > 0:
            m = pattern.search(text, sp)
            if not m:
                return len(text)
            t = m.group(0)
            if t == self.block_comment_start:
                depth += 1
            elif t == self.block_comment_end:
                depth -= 1
            sp = m.end()
        return sp

    def _strip_nested_block_comments_only(self, text):
        """Remove nested block comments from text where strings are already stripped."""
        if not (self.block_comment_start and self.block_comment_end):
            return text

        pattern = self._cached_nested_pattern
        if pattern is None:
            escaped_start = re.escape(self.block_comment_start)
            escaped_end = re.escape(self.block_comment_end)
            if self.line_comment:
                escaped_line = re.escape(self.line_comment)
                pattern = re.compile(f"{escaped_start}|{escaped_end}|{escaped_line}")
            else:
                pattern = re.compile(f"{escaped_start}|{escaped_end}")
            self._cached_nested_pattern = pattern

        p = 0
        result = []
        last_idx = 0
        while p < len(text):
            match = pattern.search(text, p)
            if not match:
                break

            token = match.group(0)
            if self.line_comment and token == self.line_comment:
                result.append(text[last_idx:match.start()])
                result.append(text[match.start():])
                last_idx = len(text)
                break

            if token == self.block_comment_start:
                result.append(text[last_idx:match.start()])
                sp = self._find_nested_block_comment_end(text, match.end(), pattern)
                last_idx = sp
                p = sp
            else:
                p = match.end()

        if last_idx < len(text):
            result.append(text[last_idx:])
        return "".join(result)

    def _strip_nested_block_comments(self, content, pattern):
        """Remove nested block comments from content, preserving line count."""
        if not (self.block_comment_start and self.block_comment_end):
            return content

        p = 0
        result = []
        last_idx = 0
        inner_pattern = self._cached_inner_block_comment_pattern
        if inner_pattern is None:
            escaped_start = re.escape(self.block_comment_start)
            escaped_end = re.escape(self.block_comment_end)
            inner_pattern = re.compile(f"{escaped_start}|{escaped_end}")
            self._cached_inner_block_comment_pattern = inner_pattern

        while p < len(content):
            match = pattern.search(content, p)
            if not match:
                break

            group_dict = match.groupdict()
            if group_dict.get("comment_start") is not None:
                result.append(content[last_idx:match.start()])
                sp = self._find_nested_block_comment_end(content, match.end(), inner_pattern)
                result.append("\n" * content.count("\n", match.start(), sp))
                last_idx = sp
                p = sp
            else:
                result.append(content[last_idx:match.start()])
                val_to_replace = None
                for key, val in group_dict.items():
                    if key.startswith("multiline_") and val is not None:
                        val_to_replace = val
                        break
                if val_to_replace is not None:
                    result.append("\n" * val_to_replace.count("\n"))
                else:
                    result.append(match.group(0))
                last_idx = match.end()
                p = match.end()

        if last_idx < len(content):
            result.append(content[last_idx:])
        return "".join(result)

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

    def _compile_block_comment_pattern(self):
        parts = []
        if (
            self.supports_block_comments
            and self.block_comment_start
            and self.block_comment_end
        ):
            escaped_start = re.escape(self.block_comment_start)
            escaped_end = re.escape(self.block_comment_end)
            if self.supports_nested_block_comments:
                parts.append(rf'(?P<comment_start>{escaped_start})')
            else:
                parts.append(rf'(?P<comment>{escaped_start}.*?{escaped_end})')

        if self.supports_cpp_raw_strings:
            parts.append(f'(?P<multiline_cpp_raw>{self._CPP_RAW_STRING_PATTERN})')

        if self.supports_rust_raw_strings:
            parts.append(f'(?P<multiline_rust_raw>{self._RUST_RAW_STRING_PATTERN})')

        for i, delim in enumerate(self.multiline_string_delimiters):
            escaped_delim = re.escape(delim)
            first_char_escaped = re.escape(delim[0])
            pattern_str = (
                rf'(?P<multiline_{i}>'
                rf'{escaped_delim}'
                rf'(?:[^\\{first_char_escaped}]|\\.|'
                rf'(?!{escaped_delim}){first_char_escaped})*?'
                rf'{escaped_delim})'
            )
            parts.append(pattern_str)

        # Named backreferences to avoid quote capturing group offset issues
        if self.uses_rust_character_literals:
            parts.append(f'(?P<string>{self._RUST_CHARACTER_LITERAL_PATTERN})')
        else:
            parts.append(f'(?P<string>{self._STANDARD_STRING_PATTERN})')

        if self.line_comment:
            escaped_line_comment = re.escape(self.line_comment)
            parts.append(rf'(?P<line_comment>{escaped_line_comment}[^\n]*)')

        return re.compile('|'.join(parts), re.DOTALL)

    def strip_block_comments(self, content):
        """Remove block comments and multiline string literals from content.

        Replaces them with newlines to preserve line count.
        """
        has_block_comments = (
            self.supports_block_comments
            and self.block_comment_start
            and self.block_comment_end
        )
        if (
            not has_block_comments
            and not self.multiline_string_delimiters
            and not self.supports_cpp_raw_strings
            and not self.supports_rust_raw_strings
        ):
            return content

        # Early return check if none of the multiline starts are present
        starts = []
        if self.supports_block_comments and self.block_comment_start:
            starts.append(self.block_comment_start)
        starts.extend(self.multiline_string_delimiters)
        if self.supports_cpp_raw_strings:
            starts.append('R"')
        if self.supports_rust_raw_strings:
            starts.extend(['r"', 'r#', 'br"', 'br#', 'cr"', 'cr#'])

        if not any(start in content for start in starts):
            return content

        pattern = self._cached_block_comment_pattern
        if pattern is None:
            pattern = self._compile_block_comment_pattern()
            self._cached_block_comment_pattern = pattern

        if self.supports_nested_block_comments:
            return self._strip_nested_block_comments(content, pattern)

        def replacer(match):
            group_dict = match.groupdict()
            if group_dict.get("comment") is not None:
                return "\n" * match.group("comment").count("\n")
            for key, val in group_dict.items():
                if key.startswith("multiline_") and val is not None:
                    return "\n" * val.count("\n")
            return match.group(0)

        return pattern.sub(replacer, content)

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
