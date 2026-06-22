"""Unit tests for language-specific keywords and declaration query behavior."""

import unittest
import os
import tempfile

from context_builder.ast_engine import (
    AST_ENGINE,
    extract_identifiers_with_positions_regex,
    get_class_members,
    resolve_variable_definition_regex_fallback,
)
from context_builder.languages.c_family import C_FAMILY
from context_builder.languages.go import GO
from context_builder.cache import LRUFileCache


class TestLanguageKeywords(unittest.TestCase):
    """Test keyword and query customization in LanguageProfiles."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = LRUFileCache(capacity=10)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_python_extract_variables_ignores_self_and_def(self):
        """Python variable extraction should ignore 'self', 'def', and other keywords."""
        code = "def my_func(self, value):\n    x = value\n"
        file_path = os.path.join(self.temp_dir.name, "script.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Tracing on line 1 and 2
        results = extract_identifiers_with_positions_regex(file_path, [1, 2], file_cache=self.cache)
        words = [r[0] for r in results]

        # 'def' and 'self' should be ignored
        self.assertNotIn("def", words)
        self.assertNotIn("self", words)
        self.assertIn("value", words)
        self.assertIn("x", words)

    def test_cpp_class_members_ignores_reserved_keywords(self):
        """C++ class members should exclude C++ keywords like class, const, and public."""
        code = (
            "class MyClass {\n"
            "public:\n"
            "    const int my_constant = 42;\n"
            "    int my_val = 10;\n"
            "};\n"
        )
        file_path = os.path.join(self.temp_dir.name, "class.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        members = get_class_members(file_path, "MyClass", C_FAMILY, file_cache=self.cache)
        member_names = [m[0] for m in members]

        # 'const', 'public', 'int' should not be recognized as class members
        self.assertNotIn("const", member_names)
        self.assertNotIn("public", member_names)
        self.assertNotIn("int", member_names)
        self.assertIn("my_constant", member_names)
        self.assertIn("my_val", member_names)

    def test_go_short_var_declaration_query(self):
        """Go-specific declaration query should locate short var declarations (:=)."""
        code = (
            "package main\n"
            "func main() {\n"
            "    target := 42\n"
            "    _ = target\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "main.go")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        if AST_ENGINE.is_supported(".go"):
            res_file, res_line = resolve_variable_definition_regex_fallback(
                file_path, "target", 4, self.cache, GO
            )
            self.assertEqual(res_file, file_path)
            self.assertEqual(res_line, 3)


if __name__ == "__main__":
    unittest.main()
