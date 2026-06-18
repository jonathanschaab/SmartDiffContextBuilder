# pylint: disable=too-many-public-methods

"""Tests for language profile resolution and fallback behavior."""

import unittest

from context_builder.languages import UNKNOWN_LANGUAGE, get_language_profile
from context_builder.languages.base import LanguageProfile


class TestLanguageProfiles(unittest.TestCase):
    """Verify language-specific policy stays behind the registry boundary."""

    def test_python_profile(self):
        """Python uses indentation, hash comments, and pylsp."""
        profile = get_language_profile("src/example.py")

        self.assertEqual(profile.name, "python")
        self.assertTrue(profile.uses_indentation_blocks)
        self.assertEqual(profile.comment_prefix, "#")
        self.assertEqual(profile.lsp_command, ("pylsp",))
        self.assertEqual(
            profile.strip_strings_and_comments("def foo(): # comment"),
            "def foo(): ",
        )
        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "# ... [Body Omitted] ...",
        )

    def test_python_multiline_string_stripping(self):
        """Python profile correctly strips single-line and multiline triple-quoted strings."""
        profile = get_language_profile("src/example.py")

        # Single-line triple-quoted string
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table"""'),
            "query = ",
        )
        # Single-line triple-quoted string with single quotes
        self.assertEqual(
            profile.strip_strings_and_comments("query = '''SELECT * FROM table'''"),
            "query = ",
        )
        # Triple-quoted string starting line (unclosed fallback)
        self.assertEqual(
            profile.strip_strings_and_comments('query = """'),
            "query = ",
        )
        # Triple-quoted string containing standard quotes
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM "table" """'),
            "query = ",
        )
        # Triple-quoted string with an inline comment
        self.assertEqual(
            profile.strip_strings_and_comments('query = """SELECT * FROM table""" # comment'),
            "query =  ",
        )
        # Preserve newlines in multiline comments / strings
        content = 'query = """\nSELECT *\nFROM "table"\n"""'
        self.assertEqual(
            profile.strip_block_comments(content),
            'query = \n\n\n',
        )

    def test_c_family_profile(self):
        """C-family files expose preprocessing and compile database support."""
        for file_path in (
            "main.c",
            "main.cc",
            "main.cpp",
            "main.cxx",
            "main.h",
            "main.hpp",
            "main.hxx",
        ):
            profile = get_language_profile(file_path)
            self.assertEqual(profile.name, "c-family")
            self.assertTrue(profile.supports_macro_expansion)
            self.assertTrue(profile.supports_compile_commands)
            self.assertTrue(profile.uses_c_style_definitions)
            self.assertEqual(
                profile.lsp_command,
                ("clangd", "--background-index"),
            )
            self.assertEqual(
                profile.format_omission_comment("Body Omitted"),
                "/* ... [Body Omitted] ... */",
            )

    def test_known_non_c_profiles(self):
        """Other registered languages retain their comment and LSP policy."""
        self.assertEqual(
            get_language_profile("lib.rs").lsp_command,
            ("rust-analyzer",),
        )
        self.assertTrue(
            get_language_profile("lib.rs").tests_can_share_source_file
        )
        self.assertIsNotNone(get_language_profile("tests.py").test_query)
        self.assertEqual(
            get_language_profile("app.ts").lsp_command,
            ("typescript-language-server", "--stdio"),
        )
        self.assertEqual(get_language_profile("main.go").name, "go")
        self.assertEqual(get_language_profile("script.sh").comment_prefix, "#")
        self.assertEqual(get_language_profile("Makefile").comment_prefix, "#")
        self.assertEqual(
            get_language_profile("makefile-client").comment_prefix,
            "#",
        )
        self.assertEqual(get_language_profile("build.bat").comment_prefix, "REM")

    def test_batch_rem_comments_are_case_insensitive_distinct_tokens(self):
        """Batch REM comments ignore casing without matching longer words."""
        profile = get_language_profile("build.bat")

        for marker in ("REM", "rem", "Rem"):
            self.assertEqual(
                profile.strip_strings_and_comments(f"echo before & {marker} after"),
                "echo before & ",
            )
        for command in ("PREMIUM", "REMARK", "REMEDY"):
            self.assertEqual(
                profile.strip_strings_and_comments(f"echo {command}"),
                f"echo {command}",
            )
        self.assertEqual(
            profile.strip_strings_and_comments('echo "REM is text"'),
            "echo ",
        )

    def test_unknown_language_fallback(self):
        """Unknown extensions use the explicit conservative fallback profile."""
        profile = get_language_profile("source.custom")

        self.assertIs(profile, UNKNOWN_LANGUAGE)
        self.assertFalse(profile.supports_macro_expansion)
        self.assertFalse(profile.supports_compile_commands)
        self.assertIsNone(profile.lsp_command)
        self.assertEqual(profile.comment_prefix, "//")
        self.assertEqual(
            profile.strip_strings_and_comments('value("text"); // comment'),
            "value(); ",
        )
        self.assertEqual(
            profile.extract_function_name("widget()", 4, 8),
            "widget",
        )

    def test_custom_block_comment_delimiters_are_used(self):
        """Profiles can override block-comment delimiters for omission text."""
        class HtmlLikeProfile(LanguageProfile):
            """Minimal block-comment override used for omission formatting tests."""
            supports_block_comments = True
            block_comment_start = "<!--"
            block_comment_end = "-->"

        profile = HtmlLikeProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "<!-- ... [Body Omitted] ... -->",
        )

    def test_missing_comment_markers_fall_back_to_plain_text(self):
        """Profiles without comment markers omit text without rendering 'None'."""
        class MarkerlessProfile(LanguageProfile):
            """Profile with no usable comment markers for defensive formatting."""
            supports_block_comments = False
            line_comment = None
            comment_prefix = None

        profile = MarkerlessProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "... [Body Omitted] ...",
        )

    def test_missing_block_delimiters_fall_back_to_line_comments(self):
        """Block-comment profiles without delimiters reuse line comments safely."""
        class IncompleteBlockProfile(LanguageProfile):
            """Profile missing block delimiters but still declaring line comments."""
            supports_block_comments = True
            block_comment_start = None
            block_comment_end = ""
            line_comment = "#"
            comment_prefix = "#"

        profile = IncompleteBlockProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "# ... [Body Omitted] ...",
        )

    def test_missing_block_delimiters_and_line_comments_fall_back_to_plain_text(self):
        """Profiles missing all comment markers degrade to plain omission text."""
        class MarkerlessBlockProfile(LanguageProfile):
            """Profile with no valid block or line comment markers."""
            supports_block_comments = True
            block_comment_start = None
            block_comment_end = None
            line_comment = None
            comment_prefix = None

        profile = MarkerlessBlockProfile()

        self.assertEqual(
            profile.format_omission_comment("Body Omitted"),
            "... [Body Omitted] ...",
        )

    def test_extension_lookup_is_case_insensitive(self):
        """Registry lookups normalize extension casing."""
        self.assertEqual(get_language_profile(".PY").name, "python")
        self.assertEqual(get_language_profile("HEADER.HPP").name, "c-family")

    def test_hidden_files_with_multiple_dots_use_final_extension(self):
        """Hidden filenames are resolved as paths rather than pure extensions."""
        self.assertEqual(get_language_profile(".test.py").name, "python")
        self.assertEqual(get_language_profile(".config.js").name, "javascript")

    def test_windows_paths_resolve_on_any_platform(self):
        """Backslash paths use the same language resolution on every OS."""
        self.assertEqual(
            get_language_profile(r"src\package\module.py").name,
            "python",
        )
        self.assertEqual(
            get_language_profile(r"src\build\Makefile-client").comment_prefix,
            "#",
        )
        self.assertEqual(
            get_language_profile(r"src\.config.js").name,
            "javascript",
        )

    def test_cpp_raw_string_stripping(self):
        """C++ profile correctly strips single-line and multiline raw string literals."""
        profile = get_language_profile("main.cpp")
        self.assertEqual(
            profile.strip_strings_and_comments('R"delimiter(my_func();)delimiter"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('R"(my_func();)"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('R"foo(my_func("hello");)foo"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('u8R"foo(bar)foo"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('LR"--(STUV)--"'),
            '',
        )

        # Raw string prefixes that are part of larger identifiers should not
        # be matched as raw strings
        self.assertEqual(
            profile.strip_strings_and_comments('fooR"(bar)"'),
            'fooR',
        )

        # Multiline raw string in content should be stripped by
        # strip_block_comments preserving newlines
        content = 'R"foo(\nmy_func("hello");\n)foo"'
        self.assertEqual(profile.strip_block_comments(content), '\n\n')

    def test_rust_raw_string_stripping(self):
        """Rust profile correctly strips single-line and multiline raw string literals."""
        profile = get_language_profile("lib.rs")
        self.assertEqual(
            profile.strip_strings_and_comments('r#"// my_func()"#'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('r"// my_func()"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('br#"// my_func()"#'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('r##"my_func("hello")"##'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('cr"foo"'),
            '',
        )
        self.assertEqual(
            profile.strip_strings_and_comments('cr#"foo"#'),
            '',
        )

        # Raw string prefixes that are part of larger identifiers should not
        # be matched as raw strings
        self.assertEqual(
            profile.strip_strings_and_comments('bar"hello"'),
            'bar',
        )

        # Multiline raw string in content should be stripped by
        # strip_block_comments preserving newlines
        content = 'r#"\nline 1\nline 2"#'
        self.assertEqual(profile.strip_block_comments(content), '\n\n')

    def test_rust_lifetimes_and_character_literals(self):
        """Rust lifetimes are preserved and valid char literals are stripped."""
        profile = get_language_profile("lib.rs")

        # Test character literals are correctly stripped
        self.assertEqual(profile.strip_strings_and_comments("'a'"), "")
        self.assertEqual(profile.strip_strings_and_comments("'\\n'"), "")
        self.assertEqual(profile.strip_strings_and_comments("'\\u{1f600}'"), "")

        # Test lifetimes are preserved
        self.assertEqual(
            profile.strip_strings_and_comments("fn foo<'a, 'b>()"),
            "fn foo<'a, 'b>()",
        )

        # Test block comments on the same line as multiple lifetimes are stripped
        content = "fn foo<'a /* comment */, 'b>()"
        self.assertEqual(
            profile.strip_block_comments(content),
            "fn foo<'a , 'b>()",
        )

    def test_unclosed_string_literals_do_not_span_lines(self):
        """Unclosed standard strings do not match across newlines in strip_block_comments."""
        profile = get_language_profile("main.cpp")
        content = '"unclosed\n/* comment containing my_func(); */\n"closed"'
        # The block comment should be stripped, but standard string shouldn't cross lines
        self.assertEqual(
            profile.strip_block_comments(content),
            '"unclosed\n\n"closed"'
        )


if __name__ == "__main__":
    unittest.main()
