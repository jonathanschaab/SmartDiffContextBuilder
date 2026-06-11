"""Tests for language profile resolution and fallback behavior."""

import unittest

from context_builder.languages import UNKNOWN_LANGUAGE, get_language_profile


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

    def test_extension_lookup_is_case_insensitive(self):
        """Registry lookups normalize extension casing."""
        self.assertEqual(get_language_profile(".PY").name, "python")
        self.assertEqual(get_language_profile("HEADER.HPP").name, "c-family")

    def test_hidden_files_with_multiple_dots_use_final_extension(self):
        """Hidden filenames are resolved as paths rather than pure extensions."""
        self.assertEqual(get_language_profile(".test.py").name, "python")
        self.assertEqual(get_language_profile(".config.js").name, "javascript")


if __name__ == "__main__":
    unittest.main()
