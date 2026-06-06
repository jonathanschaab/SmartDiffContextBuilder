import os
import unittest
import tempfile
from context_builder.cache import LRUFileCache
from context_builder.ast_engine import (
    strip_strings_and_comments,
    extract_function_bounds_regex,
    split_massive_block_ast,
    trace_lexical_dependencies_regex
)

class TestAstEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = LRUFileCache(capacity=5)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_strip_strings_and_comments(self):
        self.assertEqual(strip_strings_and_comments("int a = 5; // comment"), "int a = 5; ")
        self.assertEqual(strip_strings_and_comments("def foo(): # python comment", is_python=True), "def foo(): ")
        self.assertEqual(strip_strings_and_comments('std::string s = "hello // world";'), 'std::string s = ;')

    def test_extract_function_bounds_regex_python(self):
        code = (
            "def outer():\n"
            "    print('hello')\n"
            "    def inner():\n"
            "        pass\n"
            "    return 5\n"
        )
        file_path = os.path.join(self.temp_dir.name, "test.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Tracing outer function bounds starting inside
        start, end = extract_function_bounds_regex(file_path, 2, file_cache=self.cache)
        self.assertEqual(start, 0)
        self.assertEqual(end, 5)

    def test_extract_function_bounds_regex_cpp(self):
        code = (
            "void my_func() {\n"
            "    int a = 5;\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "test.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        start, end = extract_function_bounds_regex(file_path, 2, file_cache=self.cache)
        self.assertEqual(start, 0)
        self.assertEqual(end, 3)

    def test_split_massive_block_ast_fallback(self):
        # Test fallback behavior (AST not supported/fallback to line count)
        source = "line1\nline2\nline3\nline4\n"
        result = split_massive_block_ast(source, "file.txt", max_lines=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["suffix"], " (Truncated)")
        self.assertIn("line1\nline2", result[0]["text"])

    def test_trace_lexical_dependencies_regex(self):
        code = (
            "void target_func();\n"
            "void caller() {\n"
            "    target_func();\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "caller.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        # Seed cache
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("target_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        self.assertEqual(len(callers[file_path]), 2)
