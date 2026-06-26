# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,protected-access
# pylint: disable=redefined-outer-name,reimported,too-many-lines,too-many-public-methods
# pylint: disable=consider-using-with,line-too-long,consider-using-from-import
# pylint: disable=too-few-public-methods

import os
import unittest
from collections import OrderedDict
from unittest.mock import ANY, patch, MagicMock
import tempfile
from types import SimpleNamespace

from context_builder.cache import LRUFileCache
from context_builder.ast_engine import (
    strip_strings_and_comments,
    extract_function_bounds_regex,
    extract_function_bounds,
    split_massive_block_ast,
    trace_lexical_dependencies_regex,
    extract_callees,
    find_callee_definition,
    extract_callees_regex
)

class TestAstEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = LRUFileCache(capacity=5)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tree_sitter_query_cache_reuses_compiled_queries(self):
        from context_builder import ast_engine

        orig_initialized = ast_engine.AST_ENGINE._initialized
        orig_languages = ast_engine.AST_ENGINE.languages.copy()
        orig_queries = ast_engine.AST_ENGINE.queries.copy()
        try:
            ast_engine.AST_ENGINE._initialized = True
            ast_engine.AST_ENGINE.languages = {".py": MagicMock()}
            ast_engine.AST_ENGINE.queries = OrderedDict()

            compiled_query = MagicMock()
            with patch(
                "context_builder.ast_engine.tree_sitter.Query",
                return_value=compiled_query,
            ) as mock_query:
                first = ast_engine.AST_ENGINE.get_query(".PY", "(identifier) @name")
                second = ast_engine.AST_ENGINE.get_query(".py", "(identifier) @name")

            self.assertIs(first, compiled_query)
            self.assertIs(second, compiled_query)
            mock_query.assert_called_once()
        finally:
            ast_engine.AST_ENGINE._initialized = orig_initialized
            ast_engine.AST_ENGINE.languages = orig_languages
            ast_engine.AST_ENGINE.queries = orig_queries

    def test_strip_strings_and_comments(self):
        self.assertEqual(
            strip_strings_and_comments("int a = 5; // comment", ".cpp"),
            "int a = 5; ",
        )
        self.assertEqual(
            strip_strings_and_comments(
                "def foo(): # python comment",
                "module.py",
            ),
            "def foo(): ",
        )
        self.assertEqual(
            strip_strings_and_comments(
                'std::string s = "hello // world";',
                "module.cpp",
            ),
            "std::string s = ;",
        )

    @patch("context_builder.ast_engine.get_language_profile")
    def test_strip_strings_and_comments_uses_language_registry(self, mock_profile):
        profile = mock_profile.return_value
        profile.strip_strings_and_comments.return_value = "cleaned"

        result = strip_strings_and_comments("source", "custom.language")

        mock_profile.assert_called_once_with("custom.language")
        profile.strip_strings_and_comments.assert_called_once_with("source")
        self.assertEqual(result, "cleaned")

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

    def test_extract_function_bounds_regex_java(self):
        code = (
            "public class MyClass {\n"
            "    public void myMethod() {\n"
            "        System.out.println(\"hello\");\n"
            "    }\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "test.java")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        start, end = extract_function_bounds_regex(file_path, 3, file_cache=self.cache)
        self.assertEqual(start, 1)
        self.assertEqual(end, 4)

    def test_split_massive_block_ast_fallback(self):
        # Test fallback behavior (AST not supported/fallback to line count)
        source = "line1\nline2\nline3\nline4\n"
        result = split_massive_block_ast(source, "file.txt", max_lines=2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["suffix"], " (Truncated)")
        self.assertIn("line1\nline2", result[0]["text"])

    def test_split_massive_block_ast_python_fallback(self):
        source = "line1\nline2\nline3\nline4\n"
        result = split_massive_block_ast(source, "file.py", max_lines=2)
        self.assertEqual(result[0]["suffix"], " (Truncated)")
        self.assertIn("# ... [Lines Omitted due to size] ...", result[0]["text"])

    def test_split_massive_block_ast_cpp_fallback(self):
        source = "line1\nline2\nline3\nline4\n"
        result = split_massive_block_ast(source, "file.cpp", max_lines=2)
        self.assertEqual(result[0]["suffix"], " (Truncated)")
        self.assertIn("/* ... [Lines Omitted due to size] ... */", result[0]["text"])

    def test_split_massive_block_ast_java_fallback(self):
        source = "line1\nline2\nline3\nline4\n"
        result = split_massive_block_ast(source, "file.java", max_lines=2)
        self.assertEqual(result[0]["suffix"], " (Truncated)")
        self.assertIn("/* ... [Lines Omitted due to size] ... */", result[0]["text"])

    def test_split_massive_block_ast_hash_comment_fallbacks(self):
        source = "line1\nline2\nline3\nline4\n"

        for file_path in ("script.sh", "Makefile", "makefile-client"):
            with self.subTest(file_path=file_path):
                result = split_massive_block_ast(source, file_path, max_lines=2)
                self.assertIn(
                    "# ... [Lines Omitted due to size] ...",
                    result[0]["text"],
                )
                self.assertNotIn("/*", result[0]["text"])

    def test_split_massive_block_ast_batch_comment_fallback(self):
        source = "line1\nline2\nline3\nline4\n"

        result = split_massive_block_ast(source, "build.bat", max_lines=2)

        self.assertIn(
            "REM ... [Lines Omitted due to size] ...",
            result[0]["text"],
        )
        self.assertNotIn("/*", result[0]["text"])

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_uppercase_extension_uses_ast(self, mock_ast_engine):
        source = (
            "def my_func():\n"
            "    # body line 1\n"
            "    # body line 2\n"
            "    pass\n"
        )

        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()
        mock_tree.root_node.children = [mock_child]
        mock_child.type = "function_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (3, 0)
        mock_parser.parse.return_value = mock_tree

        mock_ast_engine.is_supported.side_effect = lambda ext: ext == ".py"
        mock_ast_engine.parsers = {".py": mock_parser}

        result = split_massive_block_ast(source, "test.PY", max_lines=3)

        self.assertEqual(result[0]["suffix"], " (AST Semantically Pruned)")
        self.assertIn(
            "# ... [Inner Body Omitted for Context Preservation] ...",
            result[0]["text"],
        )


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

    def test_trace_lexical_dependencies_regex_supports_makefile_variants(self):
        code = (
            "# target_func() in a comment should be ignored\n"
            "build:\n"
            "\ttarget_func()\n"
        )
        file_path = os.path.join(self.temp_dir.name, "makefile-client")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex(
            "target_func",
            [file_path],
            file_cache=cache,
        )

        self.assertIn(file_path, callers)
        self.assertEqual([match["line"] for match in callers[file_path]], [3])

    def test_trace_lexical_dependencies_regex_js_generators(self):
        code = (
            "function* my_func() {}\n"
            "function * another_func() {\n"
            "  my_func();\n"
            "}\n"
            "function*my_func() {}\n"
            "function *my_func() {}\n"
            "function * my_func() {}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "generators.js")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        self.assertEqual([match["line"] for match in callers[file_path]], [3])

    def test_trace_lexical_dependencies_regex_python_lambdas(self):
        code = (
            "my_lambda = lambda: 42\n"
            "def other_func():\n"
            "    my_lambda()\n"
        )
        file_path = os.path.join(self.temp_dir.name, "lambdas.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_lambda", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        self.assertEqual([match["line"] for match in callers[file_path]], [3])

    def test_trace_lexical_dependencies_regex_python_multiline_strings(self):
        code = (
            "# Triple quotes commented out by #\n"
            "# \"\"\"\n"
            "# inside comment\n"
            "# \"\"\"\n"
            "my_func() # 5: Should match!\n"
            "\"\"\"\n"
            "This is a multiline docstring\n"
            "my_func() # 8: Inside docstring, should be ignored!\n"
            "# inside docstring\n"
            "\"\"\"\n"
            "my_func() # 11: Should match!\n"
        )
        file_path = os.path.join(self.temp_dir.name, "docstrings.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        # Only lines 5 and 11 are actual callers.
        # Line 8 is inside a docstring and should be ignored.
        self.assertEqual([match["line"] for match in callers[file_path]], [5, 11])

    def test_trace_lexical_dependencies_regex_js_inline_object_returns(self):
        code = (
            "class MyClass {\n"
            "  my_func(): { foo: string } {\n"
            "    return { foo: 'bar' };\n"
            "  }\n"
            "  other_func(): Promise<{ foo: string }> {\n"
            "    my_func();\n"
            "    return Promise.resolve({ foo: 'baz' });\n"
            "  }\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "inline_objects.ts")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        self.assertEqual([match["line"] for match in callers[file_path]], [6])

    def test_trace_lexical_dependencies_regex_js_redos_prevention(self):
        # Verify that a long string without a trailing '{' does not cause catastrophic backtracking
        from context_builder.languages.javascript import JAVASCRIPT
        import time

        patterns = JAVASCRIPT.get_definition_patterns("my_func")
        pattern = patterns[2]

        malicious_input = "my_func(): " + "A" * 500

        start_time = time.perf_counter()
        match = pattern.search(malicious_input)
        duration = time.perf_counter() - start_time

        self.assertFalse(match)
        # Should finish extremely fast (under 100 milliseconds)
        self.assertLess(duration, 0.1)

    def test_trace_lexical_dependencies_regex_cpp_redos_prevention(self):
        # Verify that long sequences of spaces do not cause catastrophic backtracking (ReDoS)
        import re
        import time

        func_name = "my_func"
        lead_b = r'\b'
        escaped_name = re.escape(func_name)
        pattern = re.compile(
            r'^\s*(?:(?:[A-Za-z0-9_<>,]+(?:::[A-Za-z0-9_<>,]+)*)'
            r'(?:\s+|[*&]+))*[\s*&]*'
            r'(?:(?:[A-Za-z0-9_<>,]+(?:::[A-Za-z0-9_<>,]+)*)::)?'
            + lead_b + escaped_name + r'\s*\('
        )

        # 1. Test long sequence of trailing spaces
        malicious_input_1 = "void " + " " * 500
        start_time = time.perf_counter()
        match_1 = pattern.search(malicious_input_1)
        duration_1 = time.perf_counter() - start_time

        self.assertFalse(match_1)
        self.assertLess(duration_1, 0.1)

        # 2. Test repeated spaces with nested quantifiers (overlapping match space)
        malicious_input_2 = "void  " * 25 + "b"
        start_time = time.perf_counter()
        match_2 = pattern.search(malicious_input_2)
        duration_2 = time.perf_counter() - start_time

        self.assertFalse(match_2)
        self.assertLess(duration_2, 0.1)

    def test_trace_lexical_dependencies_regex_cpp_pointer_reference_definitions(self):
        code = (
            "void* my_func() {\n"
            "}\n"
            "void * my_func() {\n"
            "}\n"
            "void *& my_func() {\n"
            "}\n"
            "void caller() {\n"
            "    my_func();\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "ptrs.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        # Lines 1, 3, 5 are C++ method definitions (pointer/reference qualifiers)
        # and should be ignored.
        # Only line 8 is the actual caller.
        self.assertEqual([match["line"] for match in callers[file_path]], [8])

    def test_trace_lexical_dependencies_regex_cpp_outofline_definitions(self):
        code = (
            "void MyClass::my_func() {\n"
            "}\n"
            "MyClass::my_func() {\n"
            "}\n"
            "void* MyClass::Nested::my_func() {\n"
            "}\n"
            "const void MyClass::my_func() {\n"
            "}\n"
            "unsigned int MyClass::my_func() {\n"
            "}\n"
            "static inline int MyClass::my_func() {\n"
            "}\n"
            "const std::vector<int>& MyClass::my_func() {\n"
            "}\n"
            "void caller() {\n"
            "    my_func();\n"
            "    MyClass::my_func();\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "outofline.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        # Definitions (lines 1, 3, 5, 7, 9, 11, 13) should be ignored.
        # Lines 16 and 17 are the actual callers.
        self.assertEqual([match["line"] for match in callers[file_path]], [16, 17])

    def test_trace_lexical_dependencies_regex_multiline_block_comments(self):
        code = (
            "/*\n"
            "  This is a multiline comment block\n"
            "  my_func(); // Inside comment, should be ignored\n"
            "*/\n"
            "my_func(); // This is the actual call\n"
        )
        file_path = os.path.join(self.temp_dir.name, "comments.js")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        # Only line 5 is the actual caller. The reference on line 3 is inside the comment.
        self.assertEqual([match["line"] for match in callers[file_path]], [5])

    def test_trace_lexical_dependencies_regex_block_comments_in_strings(self):
        code = (
            "const commentStart = \"/*\";\n"
            "my_func(); // 2: Should be matched, not stripped!\n"
            "const commentEnd = \"*/\";\n"
            "// This is a comment /*\n"
            "my_func(); // 5: Should be matched, not stripped by the line comment above!\n"
            "// */\n"
            "/* block */ my_func(); // 7: Mid-line block comment with trailing line comment, should match!\n"
            "// This is a \"string\" my_func(); // 8: Apparent string in line comment, should be ignored!\n"
            "/* block // line */ my_func(); // 9: Line comment prefix inside block comment, should match!\n"
            "/*\n"
            "  const myStr = \"hello\";\n"
            "  my_func(); // 12: Inside block comment, should be ignored\n"
            "*/\n"
            "my_func(); // 14: Should be matched\n"
        )
        file_path = os.path.join(self.temp_dir.name, "comments_in_strings.js")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        # Lines 2, 5, 7, 9, and 14 are actual callers.
        # Lines 8 and 12 are inside comments and should be ignored.
        self.assertEqual([match["line"] for match in callers[file_path]], [2, 5, 7, 9, 14])

    def test_trace_lexical_dependencies_regex_string_escapes(self):
        code = (
            "const s1 = \"escaped backslash \\\\\";\n"
            "my_func(); // 2: Should match!\n"
            "const s2 = \"\\\\\\\"\"; // 3: Escaped backslash and escaped quote (ends with \\\")\n"
            "/*\n"
            "  my_func(); // 5: Inside block comment, should be ignored\n"
            "*/\n"
            "my_func(); // 7: Should match!\n"
            "const s3 = \"\\\"\"; // 8: Escaped quote (\"\\\")\n"
            "my_func(); // 9: Should match!\n"
        )
        file_path = os.path.join(self.temp_dir.name, "escapes.js")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=cache)
        self.assertIn(file_path, callers)
        # Lines 2, 7, and 9 are actual callers.
        # Line 5 is inside block comment.
        self.assertEqual([match["line"] for match in callers[file_path]], [2, 7, 9])

    def test_extract_function_bounds_defensive(self):
        start, end = extract_function_bounds("some_file.py", 0, file_cache=self.cache)
        self.assertIsNone(start)
        self.assertIsNone(end)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_function_bounds_ast_finds_enclosing_function(self, mock_engine):
        from context_builder.ast_engine import extract_function_bounds_ast

        class FakeNode:
            def __init__(
                self, node_type, start, end, children=None, parent=None
            ):
                self.type = node_type
                self.start_point = (start, 0)
                self.end_point = (end, 0)
                self.children = children or []
                self.parent = parent
                for child in self.children:
                    child.parent = self

        statement = FakeNode("expression_statement", 2, 2)
        function = FakeNode("function_definition", 0, 3, [statement])
        root = FakeNode("module", 0, 3, [function])
        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        cache = MagicMock()
        cache.get_bytes.return_value = b"def target():\n    value = 1\n    call()\n"

        bounds = extract_function_bounds_ast(
            "source.py", 3, ".py", file_cache=cache
        )

        self.assertEqual(bounds, (0, 4))

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_function_bounds_ast_returns_none_without_target_node(
        self, mock_engine
    ):
        from context_builder.ast_engine import extract_function_bounds_ast

        root = SimpleNamespace(children=[])
        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        cache = MagicMock()
        cache.get_bytes.return_value = b""

        self.assertEqual(
            extract_function_bounds_ast("empty.py", 1, ".py", cache),
            (None, None),
        )

    @patch(
        "context_builder.ast_engine.extract_function_bounds_regex",
        return_value=(4, 8),
    )
    @patch(
        "context_builder.ast_engine.extract_function_bounds_ast",
        return_value=(None, None),
    )
    @patch("context_builder.ast_engine.AST_ENGINE.is_supported", return_value=True)
    def test_extract_function_bounds_uses_regex_when_ast_has_no_block(
        self, _mock_supported, _mock_ast, mock_regex
    ):
        bounds = extract_function_bounds("source.py", 6, file_cache=self.cache)

        self.assertEqual(bounds, (4, 8))
        mock_regex.assert_called_once_with(
            "source.py", 6, file_cache=self.cache
        )

    def test_extract_function_bounds_regex_handles_empty_and_out_of_range_files(self):
        cache = MagicMock()
        cache.get_lines.side_effect = [[], ["only line\n"]]

        self.assertEqual(
            extract_function_bounds_regex("empty.py", 1, cache),
            (None, None),
        )
        self.assertEqual(
            extract_function_bounds_regex("short.py", 3, cache),
            (None, None),
        )
        self.assertEqual(
            extract_function_bounds_regex("some_file.py", 0, cache),
            (None, None),
        )
        self.assertEqual(
            extract_function_bounds_regex("some_file.py", -5, cache),
            (None, None),
        )

        start, end = extract_function_bounds("some_file.py", -10, file_cache=self.cache)
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_extract_callees_and_find_definition(self):
        # Create a python file calling another function
        code_py = (
            "def foo():\n"
            "    bar()\n"
            "    baz()\n"
        )
        file_path = os.path.join(self.temp_dir.name, "test.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code_py)

        # Seed cache
        self.cache.get_content(file_path)

        # Extract callees between lines 0 and 3 (lines: def foo():, bar(), baz())
        callees = extract_callees(file_path, 0, 3, file_cache=self.cache)
        self.assertIn("bar", callees)
        self.assertIn("baz", callees)

        # Create another file defining bar
        def_py = (
            "def bar():\n"
            "    print('hello')\n"
        )
        def_path = os.path.join(self.temp_dir.name, "def.py")
        with open(def_path, "w", encoding="utf-8") as f:
            f.write(def_py)

        # Seed cache
        self.cache.get_content(def_path)

        # Try to find definition of bar
        path, line = find_callee_definition("bar", [file_path, def_path], file_cache=self.cache)
        self.assertEqual(path, def_path)
        self.assertEqual(line, 1)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_callees_node_text_missing(self, mock_ast_engine):
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_node = MagicMock()

        # Delete text attribute from mock node to simulate older py-tree-sitter versions
        del mock_node.text

        mock_tree.root_node.children = [mock_node]
        mock_node.start_point = (1, 0)
        mock_node.end_point = (2, 0)

        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        mock_lang = MagicMock()
        mock_query = MagicMock()
        mock_query.captures.return_value = [(mock_node, "id")]
        mock_ast_engine.languages = {".py": mock_lang}
        mock_ast_engine.get_query.return_value = mock_query

        from context_builder.ast_engine import extract_callees_ast

        mock_cache = MagicMock()
        mock_cache.get_bytes.return_value = b"def foo():\n    bar()\n"

        with self.assertRaises(AttributeError) as ctx:
            extract_callees_ast("dummy.py", 1, 3, ".py", mock_cache)

        self.assertIn("Node object lacks '.text' attribute", str(ctx.exception))

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_multiline_signature(self, mock_ast_engine):
        source = (
            "@decorator\n"
            "def my_func(\n"
            "    x,\n"
            "    y\n"
            "):\n"
            "    # body starts here\n"
            "    # line 1\n"
            "    # line 2\n"
            "    # line 3\n"
            "    # line 4\n"
            "    # line 5\n"
            "    pass\n"
        )

        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()

        mock_tree.root_node.children = [mock_child]
        mock_child.type = "function_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (11, 0)

        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        # We truncate with max_lines=7. The body is larger (12 lines), so it should be semantically truncated.
        res = split_massive_block_ast(source, "test.py", max_lines=7)

        self.assertEqual(len(res), 1)
        truncated_text = res[0]["text"]
        self.assertIn("@decorator", truncated_text)
        self.assertIn("def my_func(", truncated_text)
        self.assertIn("):", truncated_text)
        self.assertIn("# ... [Inner Body Omitted for Context Preservation] ...", truncated_text)
        self.assertIn("pass", truncated_text)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_python_type_hints(self, mock_ast_engine):
        source = (
            "def my_func(\n"
            "    x: int,\n"
            "    y: str = 'hello'\n"
            ") -> bool:\n"
            "    # body starts here\n"
            "    # line 1\n"
            "    # line 2\n"
            "    # line 3\n"
            "    # line 4\n"
            "    # line 5\n"
            "    pass\n"
        )

        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()

        mock_tree.root_node.children = [mock_child]
        mock_child.type = "function_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (11, 0)

        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source, "test.py", max_lines=6)

        self.assertEqual(len(res), 1)
        truncated_text = res[0]["text"]
        # It should contain the full signature:
        self.assertIn("def my_func(", truncated_text)
        self.assertIn("x: int", truncated_text)
        self.assertIn("y: str = 'hello'", truncated_text)
        self.assertIn(") -> bool:", truncated_text)
        self.assertIn("# ... [Inner Body Omitted for Context Preservation] ...", truncated_text)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_js_method_definition(self, mock_ast_engine):
        source = (
            "    myMethod(x) {\n"
            "        console.log(x);\n"
            "        console.log(x);\n"
            "        console.log(x);\n"
            "    }\n"
        )

        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()

        mock_tree.root_node.children = [mock_child]
        mock_child.type = "method_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (4, 0)

        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".js": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source, "test.js", max_lines=3)
        self.assertEqual(len(res), 1)
        truncated_text = res[0]["text"]
        self.assertIn("myMethod(x) {", truncated_text)
        self.assertIn("/* ... [Inner Body Omitted for Context Preservation] ... */", truncated_text)

    @patch("context_builder.ast_engine.extract_callees_ast")
    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_callees_empty_ast_no_regex_fallback(self, mock_ast_engine, mock_ast_fn):
        """If the AST parser succeeds but finds zero callees (e.g. a function
        body with only assignments and no calls), extract_callees must return
        that empty result rather than falling back to the regex extractor.

        Previously, `if callees:` treated an empty set as falsy and triggered
        the regex fallback, introducing potential false-positives."""
        mock_ast_engine.is_supported.return_value = True
        # Simulate AST parse succeeding with zero callees
        mock_ast_fn.return_value = set()

        code = (
            "def side_effect_only():\n"
            "    x = 1 + 2\n"
            "    return x\n"
        )
        file_path = os.path.join(self.temp_dir.name, "no_calls.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)
        self.cache.get_content(file_path)

        result = extract_callees(file_path, 0, 3, file_cache=self.cache)

        # Must return the (empty) AST result, not fall through to regex
        self.assertEqual(result, [])
        # extract_callees_ast should have been called exactly once
        mock_ast_fn.assert_called_once()

    def test_find_callee_definition_in_header(self):
        # Create a header file (.h) with a function definition
        code_h = (
            "void my_header_func() {\n"
            "    int y = 10;\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "my_header.h")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code_h)

        # Seed cache
        self.cache.get_content(file_path)

        # Find the definition of my_header_func
        path, line = find_callee_definition("my_header_func", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 1)

    def test_find_callee_definition_supports_makefile_variants(self):
        code = (
            "# function deploy in comments should be ignored\n"
            "function deploy\n"
        )
        file_path = os.path.join(self.temp_dir.name, "makefile-client")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        path, line = find_callee_definition("deploy", [file_path], file_cache=self.cache)

        self.assertEqual(path, file_path)
        self.assertEqual(line, 2)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_declaration_no_brace(self, mock_ast_engine):
        # A function declaration in C++ does not have a body or a brace.
        # It should not have a dangling brace appended when truncated.
        source = "void my_func_decl(int x);\n"

        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()

        mock_tree.root_node.children = [mock_child]
        mock_child.type = "function_declaration"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (0, 0)

        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".cpp": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source, "test.cpp", max_lines=1)
        self.assertEqual(len(res), 1)
        truncated_text = res[0]["text"]
        self.assertEqual(truncated_text.strip(), "void my_func_decl(int x);")

    def test_find_callee_definition_cpp_optional_prefix(self):
        # Checks C++ constructors, destructors (~), and multiline return type definitions
        code = (
            "MyClass::MyClass() {\n"
            "}\n"
            "MyClass::~MyClass() {\n"
            "}\n"
            "void\n"
            "my_multiline_func()\n"
            "{\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "methods.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        # Test constructor
        path, line = find_callee_definition("MyClass", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 1) # first match

        # Test destructor
        path, line = find_callee_definition("~MyClass", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 3)

        # Test multiline
        path, line = find_callee_definition("my_multiline_func", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 6)

    def test_find_callee_definition_cpp_template_return_types(self):
        # Verify that we can find C++ definitions that return multi-argument templates,
        # and support template class qualifiers with commas.
        code = (
            "std::map<int, std::string> get_map() {\n"
            "}\n"
            "std::pair<int, int> MyClass<T, U>::get_pair() {\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "templates.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        # 1. Test standard function returning multi-argument template
        path, line = find_callee_definition("get_map", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 1)

        # 2. Test class member function returning multi-argument template in template class
        path, line = find_callee_definition("get_pair", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 3)

    def test_trace_lexical_dependencies_regex_excludes_c_def(self):
        # Verify that C-style definitions matched by def_cpp_pattern are excluded,
        # but calls (even on lines by themselves) are counted.
        code = (
            "void my_func() {\n" # Definition
            "    my_func();\n"    # Caller (has semicolon)
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "regex_exclude.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex("my_func", [file_path], file_cache=self.cache)
        self.assertIn(file_path, callers)
        # Should only find 1 caller (line 2), not line 1 (the definition)
        self.assertEqual(len(callers[file_path]), 1)
        self.assertEqual(callers[file_path][0]["line"], 2)

    def test_split_massive_block_ast_negative_max_lines(self):
        source = "def foo():\n    pass\n"
        # Passing negative max_lines (e.g. -50)
        res = split_massive_block_ast(source, "test.py", max_lines=-50)
        self.assertEqual(len(res), 1)
        self.assertTrue("Omitted" in res[0]["text"] or "Truncated" in res[0]["suffix"])

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_trace_lexical_dependencies_ast_parent_none(self, mock_ast_engine):
        from context_builder.ast_engine import trace_lexical_dependencies_ast
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_capture_node = MagicMock()
        mock_capture_node.parent = None
        mock_capture_node.start_point = (0, 0)

        mock_query = MagicMock()
        mock_query.captures.return_value = [(mock_capture_node, "id")]

        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.languages = {".py": MagicMock()}
        mock_ast_engine.languages[".py"].query.return_value = mock_query
        mock_ast_engine.is_supported.return_value = True

        cache = LRUFileCache(capacity=5)
        cache.get_content = MagicMock(return_value="my_func()")
        cache.get_bytes = MagicMock(return_value=b"my_func()")
        cache.get_lines = MagicMock(return_value=["my_func()"])

        # It should run without raising AttributeError due to capture_node.parent being None
        res = trace_lexical_dependencies_ast("my_func", ["test.py"], file_cache=cache)
        self.assertEqual(res, {})

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_empty_sig_lines(self, mock_ast_engine):
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()
        mock_child.type = "function_definition"
        mock_child.start_point = (10, 0)
        mock_child.end_point = (5, 0) # start > end, making sig_lines empty

        mock_tree.root_node.children = [mock_child]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        source = "def foo():\n    pass\n"
        # It should run successfully without raising an IndexError.
        res = split_massive_block_ast(source, "test.py", max_lines=1)
        self.assertEqual(len(res), 1)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_callees_runtime_error_fallback(self, mock_ast_engine):
        """Verify that when AST callee extraction raises an unexpected Exception,
        it propagates as a RuntimeError, which is caught by extract_callees
        to trigger the regex-based fallback extraction."""
        mock_ast_engine.is_supported.return_value = True

        mock_lang = MagicMock()
        mock_ast_engine.languages = {".py": mock_lang}
        # Raise an exception (e.g. tree-sitter QuerySyntaxError or similar) when compiling query
        mock_ast_engine.get_query.side_effect = RuntimeError("Query syntax error")

        mock_parser = MagicMock()
        mock_parser.parse.return_value = MagicMock()
        mock_ast_engine.parsers = {".py": mock_parser}

        # Code calling some functions
        code = (
            "def foo():\n"
            "    bar()\n"
            "    baz()\n"
        )
        file_path = os.path.join(self.temp_dir.name, "fallback.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)
        self.cache.get_content(file_path)

        # Call extract_callees. It should catch the RuntimeError and fall back to regex
        callees = extract_callees(file_path, 0, 3, file_cache=self.cache)

        # Verify it successfully extracted the callees via regex fallback
        self.assertIn("bar", callees)
        self.assertIn("baz", callees)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_trace_lexical_dependencies_ast_operator_escape(self, mock_ast_engine):
        """Verify that when func_name contains regex metacharacters (e.g., C++ operator+),
        the query string is escaped and double-escaped correctly for the tree-sitter query engine."""
        from context_builder.ast_engine import trace_lexical_dependencies_ast

        mock_ast_engine.is_supported.return_value = True
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".cpp": mock_parser}

        mock_lang = MagicMock()
        mock_query = MagicMock()
        mock_query.captures.return_value = []
        mock_ast_engine.languages = {".cpp": mock_lang}
        mock_ast_engine.get_query.return_value = mock_query

        mock_cache = MagicMock()
        mock_cache.get_bytes.return_value = b"void operator+();"

        with patch(
            "context_builder.ast_engine.ripgrep_filter",
            return_value=["file.cpp"],
        ):
            trace_lexical_dependencies_ast("operator+", ["file.cpp"], file_cache=mock_cache)

        # Verify that the cached query lookup used double-escaped operator\\+
        mock_ast_engine.get_query.assert_called_once()
        query_str = mock_ast_engine.get_query.call_args[0][1]
        self.assertIn("operator\\\\+", query_str)

    def test_trace_lexical_dependencies_regex_operators(self):
        """Verify that trace_lexical_dependencies_regex correctly matches operator names
        and functions starting/ending with non-word characters by applying dynamic boundaries."""
        code = (
            "void test() {\n"
            "    obj1 + obj2;\n"       # not matching call directly
            "    obj1.operator+(obj2);\n" # should match caller
            "    operator+(obj1, obj2);\n"# should match caller
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "operators.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        # We search for "operator+"
        callers = trace_lexical_dependencies_regex("operator+", [file_path], file_cache=self.cache)
        self.assertIn(file_path, callers)
        lines = [c["line"] for c in callers[file_path]]
        self.assertIn(3, lines)
        self.assertIn(4, lines)
        self.assertNotIn(2, lines)

    def test_func_decl_pattern_backtracking_prevention(self):
        """Verify that func_decl_pattern is immune to catastrophic backtracking when evaluated
        against a long line of words/spaces without an opening parenthesis (."""
        import time
        from context_builder.ast_engine import extract_function_bounds_regex

        # A long line of words and spaces that does NOT end with '('.
        # Under the old regex, this would cause catastrophic backtracking and hang the process.
        long_line = "a " * 50 + "b"

        file_path = os.path.join(self.temp_dir.name, "backtrack.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(long_line + "\n")

        self.cache.get_content(file_path)

        start_time = time.time()
        # Call extract_function_bounds_regex, which runs func_decl_pattern
        extract_function_bounds_regex(file_path, 1, file_cache=self.cache)
        elapsed = time.time() - start_time

        # Verify it completed almost instantly (e.g. well under 0.1 seconds)
        self.assertLess(elapsed, 0.1)

    def test_lazy_initialization(self):
        """Verify that the AstEngine is initialized lazily upon checking support."""
        from context_builder.ast_engine import AstEngine, CONFIG, HAS_TREESITTER
        engine = AstEngine()
        self.assertFalse(engine._initialized)

        # Override bindings in CONFIG to test it's read
        orig_bindings = CONFIG['bindings'].copy()
        try:
            CONFIG['bindings'] = {'.dummy': ('dummy_module', 'dummy_func')}
            engine.is_supported('.dummy')
            self.assertTrue(engine._initialized)
            if HAS_TREESITTER:
                self.assertIn('.dummy', engine.missing_bindings)
        finally:
            CONFIG['bindings'] = orig_bindings

    @patch("context_builder.ast_engine.warn_once")
    def test_initialize_without_tree_sitter_warns_once(self, mock_warn):
        import context_builder.ast_engine as ast_engine

        engine = ast_engine.AstEngine()
        with patch.object(ast_engine, "HAS_TREESITTER", False):
            engine.initialize()
            engine.initialize()

        self.assertTrue(engine._initialized)
        mock_warn.assert_called_once_with(
            "tree-sitter",
            "For perfect AST scoping, install tree-sitter bindings.",
        )

    def test_initialize_accepts_language_object_from_older_binding(self):
        import context_builder.ast_engine as ast_engine

        binding_object = object()
        mock_module = SimpleNamespace(language=lambda: binding_object)
        mock_tree_sitter = MagicMock()
        mock_tree_sitter.Language.side_effect = TypeError("already a language")
        parser = mock_tree_sitter.Parser.return_value
        engine = ast_engine.AstEngine()

        with patch.dict(
            ast_engine.CONFIG,
            {"bindings": {".dummy": ("dummy_module", "language")}},
            clear=True,
        ), patch.object(
            ast_engine.importlib,
            "import_module",
            return_value=mock_module,
        ), patch.object(
            ast_engine,
            "HAS_TREESITTER",
            True,
        ), patch.object(
            ast_engine,
            "tree_sitter",
            mock_tree_sitter,
            create=True,
        ):
            engine.initialize()

        parser.set_language.assert_called_once_with(binding_object)
        self.assertIs(engine.languages[".dummy"], binding_object)
        self.assertIs(engine.parsers[".dummy"], parser)

    def test_custom_regex_and_query_overrides(self):
        """Verify that custom func_decl_pattern and callee_pattern configurations are respected."""
        from context_builder.ast_engine import _get_func_decl_pattern, _get_callee_pattern, CONFIG
        orig_decl = CONFIG['func_decl_pattern']
        orig_callee = CONFIG['callee_pattern']
        try:
            CONFIG['func_decl_pattern'] = r'\bMY_SPECIAL_DECL\b'
            CONFIG['callee_pattern'] = r'\bMY_SPECIAL_CALLEE\b'

            decl_re = _get_func_decl_pattern()
            callee_re = _get_callee_pattern()

            self.assertIsNotNone(decl_re.search("MY_SPECIAL_DECL"))
            self.assertIsNone(decl_re.search("def foo()"))

            self.assertIsNotNone(callee_re.search("MY_SPECIAL_CALLEE"))
            self.assertIsNone(callee_re.search("foo()"))
        finally:
            CONFIG['func_decl_pattern'] = orig_decl
            CONFIG['callee_pattern'] = orig_callee

    def test_invalid_bindings_warning(self):
        """Verify that warn_once is called if bindings are configured incorrectly."""
        from context_builder.ast_engine import AstEngine, CONFIG
        engine = AstEngine()

        orig_bindings = CONFIG['bindings'].copy()
        try:
            CONFIG['bindings'] = {'.dummy': 'invalid_string_instead_of_tuple'}
            with patch("context_builder.ast_engine.warn_once") as mock_warn:
                engine.initialize()
                from context_builder.ast_engine import HAS_TREESITTER
                if HAS_TREESITTER:
                    mock_warn.assert_any_call("invalid_binding_.dummy", ANY)
        finally:
            CONFIG['bindings'] = orig_bindings

    def test_failed_parser_setup_does_not_register_language(self):
        """A language is registered only after parser configuration succeeds."""
        import context_builder.ast_engine as ast_engine

        mock_module = MagicMock()
        mock_module.language.return_value = object()
        mock_tree_sitter = MagicMock()
        mock_tree_sitter.Language.return_value = MagicMock()
        mock_tree_sitter.Parser.return_value.set_language.side_effect = RuntimeError("bad parser")

        engine = ast_engine.AstEngine()
        orig_bindings = ast_engine.CONFIG["bindings"].copy()
        try:
            ast_engine.CONFIG["bindings"] = {".dummy": ("pkg.sub", "language")}
            with patch.object(
                ast_engine.importlib,
                "import_module",
                return_value=mock_module,
            ), patch.object(
                ast_engine,
                "HAS_TREESITTER",
                True,
            ), patch.object(
                ast_engine,
                "tree_sitter",
                mock_tree_sitter,
                create=True,
            ):
                engine.initialize()
        finally:
            ast_engine.CONFIG["bindings"] = orig_bindings

        self.assertNotIn(".dummy", engine.languages)
        self.assertNotIn(".dummy", engine.parsers)
        self.assertEqual(engine.missing_bindings[".dummy"], "pkg.sub")

    def test_quantifier_curly_braces_in_templates(self):
        """Verify that curly brace quantifiers (e.g. {1,3}) in templates do not crash definitions or dependency tracing."""
        from context_builder.ast_engine import trace_lexical_dependencies_ast, find_callee_definition, CONFIG
        from context_builder.config import reset_config
        reset_config()

        CONFIG['def_pattern_template'] = r'{lead_b}{escaped_callee}[a-z]{1,5}'
        CONFIG['cpp_def_pattern_template'] = r'{lead_b}{escaped_callee}[a-z]{1,5}'
        CONFIG['dependency_query_strings'] = {
            '.cpp': '(call_expression function: [(identifier) @id] (#match? @id ".*({escaped_func_name}|[a-z]{1,3}).*"))'
        }

        file_path = os.path.join(self.temp_dir.name, "quantifier.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("void fooabc() {}\n")

        self.cache.get_content(file_path)

        res_file, _res_line = find_callee_definition(
            "foo", [file_path], file_cache=self.cache
        )
        self.assertEqual(res_file, file_path)

        mock_lang = MagicMock()
        mock_query = MagicMock()
        mock_query.captures.return_value = []
        mock_lang.query.return_value = mock_query

        mock_parser = MagicMock()

        from context_builder.ast_engine import AST_ENGINE
        with patch("tree_sitter.Query", return_value=mock_query) as mock_query_class, \
             patch.dict(AST_ENGINE.languages, {".cpp": mock_lang}), \
             patch.dict(AST_ENGINE.parsers, {".cpp": mock_parser}), \
             patch.object(AST_ENGINE, "is_supported", return_value=True):
            trace_lexical_dependencies_ast("foo", [file_path], file_cache=self.cache)
            mock_query_class.assert_called_once()
            query_arg = mock_query_class.call_args[0][1]
            self.assertIn("foo", query_arg)
            self.assertIn("[a-z]{1,3}", query_arg)

    def test_trace_lexical_dependencies_regex_strips_comments_and_strings(self):
        """Verify that trace_lexical_dependencies_regex ignores matches inside comments and strings."""
        file_path = os.path.join(self.temp_dir.name, "regex_strip.py")
        content = (
            "def test_func():\n"
            "    # This is a comment calling target_func()\n"
            "    x = 'target_func() in a string'\n"
            "    y = target_func()  # Actual call\n"
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(file_path)

        callers = trace_lexical_dependencies_regex(
            "target_func", [file_path], file_cache=self.cache
        )
        self.assertIn(file_path, callers)
        # It should match only the actual call on line 4, not lines 2 and 3
        occurrences = callers[file_path]
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 4)
        self.assertEqual(occurrences[0]["code"], "y = target_func()  # Actual call")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_two_pass_prioritizes_signatures(self, mock_ast_engine):
        source = (
            "def method_one():\n"
            "    # body line 1\n"
            "    # body line 2\n"
            "    # body line 3\n"
            "    # body line 4\n"
            "    # body line 5\n"
            "    # body line 6\n"
            "    # body line 7\n"
            "    # body line 8\n"
            "    # body line 9\n"
            "    pass\n"
            "def method_two():\n"
            "    # body 1\n"
            "    # body 2\n"
            "    # body 3\n"
            "    pass\n"
            "def method_three():\n"
            "    # unique body 1\n"
            "    # unique body 2\n"
            "    # unique body 3\n"
            "    pass\n"
        )

        mock_parser = MagicMock()
        mock_tree = MagicMock()

        mock_child1 = MagicMock()
        mock_child1.type = "function_definition"
        mock_child1.start_point = (0, 0)
        mock_child1.end_point = (10, 0)

        mock_child2 = MagicMock()
        mock_child2.type = "function_definition"
        mock_child2.start_point = (11, 0)
        mock_child2.end_point = (15, 0)

        mock_child3 = MagicMock()
        mock_child3.type = "function_definition"
        mock_child3.start_point = (16, 0)
        mock_child3.end_point = (20, 0)

        mock_tree.root_node.children = [mock_child1, mock_child2, mock_child3]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        # Call split_massive_block_ast with max_lines=11.
        # total_min_lines is 3 + 3 + 3 = 9. remaining_budget = 2.
        # method_one upgrade cost is 8 -> does not upgrade.
        # method_two upgrade cost is 2 -> upgrades.
        # method_three upgrade cost is 2 -> does not upgrade.
        res = split_massive_block_ast(source, "test.py", max_lines=11)

        self.assertEqual(len(res), 1)
        text = res[0]["text"]

        # Verify method_one is truncated to its signature
        self.assertIn("def method_one():", text)
        self.assertIn("# ... [Inner Body Omitted for Context Preservation] ...", text)
        self.assertNotIn("# body line 1", text)

        # Verify method_two has its full body printed
        self.assertIn("def method_two():", text)
        self.assertIn("# body 1", text)
        self.assertIn("# body 2", text)
        self.assertIn("# body 3", text)

        # Verify method_three is truncated to its signature
        self.assertIn("def method_three():", text)
        self.assertNotIn("# unique body 1", text)

        # Verify both methods' signatures exist (so they are not omitted completely)
        self.assertTrue(text.count("def method_two():") == 1)
        self.assertTrue(text.count("def method_three():") == 1)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_min_lines_not_larger_than_full_lines(self, mock_ast_engine):
        source = (
            "def foo():\n"
            "    pass\n"
            "def bar():\n"
            "    pass\n"
        )
        mock_parser = MagicMock()
        mock_tree = MagicMock()

        mock_child1 = MagicMock()
        mock_child1.type = "function_definition"
        mock_child1.start_point = (0, 0)
        mock_child1.end_point = (1, 0)

        mock_child2 = MagicMock()
        mock_child2.type = "function_definition"
        mock_child2.start_point = (2, 0)
        mock_child2.end_point = (3, 0)

        mock_tree.root_node.children = [mock_child1, mock_child2]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source, "test.py", max_lines=3)
        self.assertEqual(len(res), 1)
        text = res[0]["text"]

        # Verify that the output was sliced to exactly 3 lines
        self.assertEqual(len(text.splitlines()), 3)
        # Verify no inner body omission comments exist (since min_lines was optimized to the full body)
        self.assertNotIn("Inner Body Omitted", text)
        self.assertEqual(text, "def foo():\n    pass\n    # ... [Remaining Methods Omitted] ...")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_new_definition_types(self, mock_ast_engine):
        # Verify decorated_definition, class_declaration, and export_statement are recognized as definitions.
        source_py = (
            "@my_decorator\n"
            "def my_func():\n"
            "    # line 1\n"
            "    # line 2\n"
            "    # line 3\n"
            "    # line 4\n"
            "    # line 5\n"
            "    pass\n"
        )
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()
        mock_child.type = "decorated_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (7, 0)

        mock_tree.root_node.children = [mock_child]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        # Using a budget of 5 lines. Since it is recognized as a definition,
        # it should get semantically truncated (signature + omission comment).
        res = split_massive_block_ast(source_py, "test.py", max_lines=5)
        self.assertEqual(len(res), 1)
        text = res[0]["text"]
        self.assertIn("@my_decorator", text)
        self.assertIn("Inner Body Omitted", text)

        # Verify class_declaration
        source_js = (
            "class MyClass {\n"
            "    constructor() {\n"
            "        // line 1\n"
            "        // line 2\n"
            "        // line 3\n"
            "        // line 4\n"
            "        // line 5\n"
            "    }\n"
            "}\n"
        )
        mock_child_js = MagicMock()
        mock_child_js.type = "class_declaration"
        mock_child_js.start_point = (0, 0)
        mock_child_js.end_point = (8, 0)

        mock_tree.root_node.children = [mock_child_js]
        mock_ast_engine.parsers = {".js": mock_parser}

        res = split_massive_block_ast(source_js, "test.js", max_lines=4)
        self.assertEqual(len(res), 1)
        text_js = res[0]["text"]
        self.assertIn("class MyClass {", text_js)
        self.assertIn("Inner Body Omitted", text_js)

        # Verify export_statement
        source_js2 = (
            "export const myFunc = () => {\n"
            "    // line 1\n"
            "    // line 2\n"
            "    // line 3\n"
            "    // line 4\n"
            "    // line 5\n"
            "};\n"
        )
        mock_child_js2 = MagicMock()
        mock_child_js2.type = "export_statement"
        mock_child_js2.start_point = (0, 0)
        mock_child_js2.end_point = (6, 0)

        mock_tree.root_node.children = [mock_child_js2]

        res = split_massive_block_ast(source_js2, "test.js", max_lines=4)
        self.assertEqual(len(res), 1)
        text_js2 = res[0]["text"]
        self.assertIn("export const myFunc = () => {", text_js2)
        self.assertIn("Inner Body Omitted", text_js2)

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_empty_children_fallback(self, mock_ast_engine):
        # Whitespace-only file with no AST children parsed
        source = "   \n  \n\t\n"
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_tree.root_node.children = []
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        # It should fall back to plain truncation instead of returning empty string
        res = split_massive_block_ast(source, "test.py", max_lines=2)
        self.assertEqual(len(res), 1)
        self.assertIn("Omitted", res[0]["text"])
        self.assertIn("Truncated", res[0]["suffix"])

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_remaining_omitted_indicator(self, mock_ast_engine):
        # Total minimum lines is 3 + 3 = 6. max_lines = 4.
        # So child2 is skipped entirely. Verify the omission comment is appended.
        source = (
            "def foo():\n"
            "    # long body\n"
            "    # long body\n"
            "    # long body\n"
            "    pass\n"
            "def bar():\n"
            "    # long body\n"
            "    # long body\n"
            "    # long body\n"
            "    pass\n"
        )
        mock_parser = MagicMock()
        mock_tree = MagicMock()

        mock_child1 = MagicMock()
        mock_child1.type = "function_definition"
        mock_child1.start_point = (0, 0)
        mock_child1.end_point = (4, 0)

        mock_child2 = MagicMock()
        mock_child2.type = "function_definition"
        mock_child2.start_point = (5, 0)
        mock_child2.end_point = (9, 0)

        mock_tree.root_node.children = [mock_child1, mock_child2]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source, "test.py", max_lines=4)
        self.assertEqual(len(res), 1)
        text = res[0]["text"]

        # Verify the omission comment replaced the last line to respect budget of 4
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[-1], "    # ... [Remaining Methods Omitted] ...")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_omission_preserves_last_line_and_indentation(self, mock_ast_engine):
        source = (
            "def foo():\n"
            "    # line 1\n"
            "    # line 2\n"
            "    # line 3\n"
            "    pass\n"
            "def bar():\n"
            "    pass\n"
        )
        mock_parser = MagicMock()
        mock_tree = MagicMock()

        mock_child1 = MagicMock()
        mock_child1.type = "function_definition"
        mock_child1.start_point = (0, 0)
        mock_child1.end_point = (4, 0)

        mock_child2 = MagicMock()
        mock_child2.type = "function_definition"
        mock_child2.start_point = (5, 0)
        mock_child2.end_point = (6, 0)

        mock_tree.root_node.children = [mock_child1, mock_child2]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source, "test.py", max_lines=4)
        self.assertEqual(len(res), 1)
        text = res[0]["text"]
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[2], "    pass")
        self.assertEqual(lines[3], "    # ... [Remaining Methods Omitted] ...")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_sibling_no_duplicate_lines(self, mock_ast_engine):
        # Sibling nodes sharing same line range (e.g. statement + trailing comment or multiple inline statements)
        source = (
            "import os; import sys  # inline imports\n"
            "def foo():\n"
            "    pass\n"
        )
        mock_parser = MagicMock()
        mock_tree = MagicMock()

        # Sibling 1: import os (line 0)
        mock_child1 = MagicMock()
        mock_child1.type = "import_statement"
        mock_child1.start_point = (0, 0)
        mock_child1.end_point = (0, 9)

        # Sibling 2: import sys (line 0)
        mock_child2 = MagicMock()
        mock_child2.type = "import_statement"
        mock_child2.start_point = (0, 11)
        mock_child2.end_point = (0, 21)

        # Sibling 3: trailing comment (line 0)
        mock_child3 = MagicMock()
        mock_child3.type = "comment"
        mock_child3.start_point = (0, 23)
        mock_child3.end_point = (0, 39)

        # Sibling 4: function definition (lines 1-2)
        mock_child4 = MagicMock()
        mock_child4.type = "function_definition"
        mock_child4.start_point = (1, 0)
        mock_child4.end_point = (2, 8)

        mock_tree.root_node.children = [mock_child1, mock_child2, mock_child3, mock_child4]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        # Truncating with budget of 3. They should fit without duplication or omission comments.
        res = split_massive_block_ast(source, "test.py", max_lines=3)
        self.assertEqual(len(res), 1)
        text = res[0]["text"]
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "import os; import sys  # inline imports")
        self.assertEqual(lines[1], "def foo():")
        self.assertEqual(lines[2], "    pass")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_split_massive_block_ast_data_structure_omission_preserves_indentation(self, mock_ast_engine):
        source_dict = (
            "    my_dict = {\n"
            "        'a': 1,\n"
            "        'b': 2,\n"
            "        'c': 3,\n"
            "        'd': 4,\n"
            "        'e': 5,\n"
            "        'f': 6,\n"
            "    }\n"
        )
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child_dict = MagicMock()
        mock_child_dict.type = "assignment"
        mock_child_dict.start_point = (0, 0)
        mock_child_dict.end_point = (7, 5)

        mock_tree.root_node.children = [mock_child_dict]
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True

        res = split_massive_block_ast(source_dict, "test.py", max_lines=6)
        self.assertEqual(len(res), 1)
        text = res[0]["text"]
        lines = text.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1], "        # ... [Data Structure Omitted] ...")

    def test_trace_lexical_dependencies_regex_js_definitions(self):
        """Verify JS/TS definitions (arrow functions, shorthands, ES5 function properties) are not treated as callers."""
        js_file = os.path.join(self.temp_dir.name, "app.js")
        content = (
            "const myFunc = () => {\n"
            "  console.log('arrow');\n"
            "};\n"
            "const myFunc = (x: number): number => { return x; };\n"
            "const myFunc = (x: number): Promise<{ a: number }> => { return x; };\n"
            "class Controller {\n"
            "  async myFunc(arg) {\n"
            "    console.log(arg);\n"
            "  }\n"
            "}\n"
            "function myFunc() {\n"
            "  return 42;\n"
            "}\n"
            "namespace myFunc {\n"
            "  export enum myFunc {\n"
            "    A, B\n"
            "  }\n"
            "}\n"
            "const obj = {\n"
            "  myFunc: function(param1) {},\n"
            "  myFunc: async function(param1) {},\n"
            "  myFunc: function*(param1) {},\n"
            "  myFunc: async function * (param1) {},\n"
            "  myFunc: function <T>(param: T): void {}\n"
            "};\n"
            "const callback = useDefault ? myFunc : function() {}; // Caller in ternary\n"
            "myFunc(); // This is the actual caller\n"
        )
        with open(js_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(js_file)

        callers = trace_lexical_dependencies_regex(
            "myFunc", [js_file], file_cache=self.cache
        )
        self.assertIn(js_file, callers)
        occurrences = callers[js_file]
        # Actual calls on lines 26 and 27 should be matched
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(occurrences[0]["line"], 26)
        self.assertEqual(occurrences[0]["code"], "const callback = useDefault ? myFunc : function() {}; // Caller in ternary")
        self.assertEqual(occurrences[1]["line"], 27)
        self.assertEqual(occurrences[1]["code"], "myFunc(); // This is the actual caller")

    def test_trace_lexical_dependencies_regex_no_cross_language_keyword_collisions(self):
        """Verify keyword definition checks are scoped to language profiles."""
        py_file = os.path.join(self.temp_dir.name, "script.py")
        cpp_file = os.path.join(self.temp_dir.name, "source.cpp")

        # In Python, def is a definition
        with open(py_file, "w", encoding="utf-8") as f:
            f.write("def my_func():\n    pass\n")
        self.cache.get_content(py_file)

        # In C++, def is not a definition, so "def my_func()" is a call
        with open(cpp_file, "w", encoding="utf-8") as f:
            f.write("void test() {\n    def my_func();\n}\n")
        self.cache.get_content(cpp_file)

        # Tracing my_func should find it in cpp_file but NOT in py_file
        callers = trace_lexical_dependencies_regex(
            "my_func", [py_file, cpp_file], file_cache=self.cache
        )
        self.assertNotIn(py_file, callers)
        self.assertIn(cpp_file, callers)
        self.assertEqual(len(callers[cpp_file]), 1)
        self.assertEqual(callers[cpp_file][0]["line"], 2)

    def test_trace_lexical_dependencies_regex_js_dollar_identifiers(self):
        """Verify that JS/TS identifiers with $ are matched correctly without false positive substring matches."""
        js_file = os.path.join(self.temp_dir.name, "dollar.js")
        content = (
            "const $init = () => {\n"
            "  console.log('init');\n"
            "};\n"
            "class MyClass {\n"
            "  $init() {\n"
            "    console.log('class init');\n"
            "  }\n"
            "}\n"
            "function test() {\n"
            "  $init(); // Actual call\n"
            "  legacy_$init(); // Substring, should NOT match\n"
            "  $init_legacy(); // Substring, should NOT match\n"
            "}\n"
        )
        with open(js_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(js_file)

        callers = trace_lexical_dependencies_regex(
            "$init", [js_file], file_cache=self.cache
        )
        self.assertIn(js_file, callers)
        occurrences = callers[js_file]
        # Only the actual call on line 10 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 10)
        self.assertEqual(occurrences[0]["code"], "$init(); // Actual call")

    def test_trace_lexical_dependencies_regex_empty_func_name(self):
        """Verify that trace_lexical_dependencies_regex handles empty function names gracefully without IndexError."""
        py_file = os.path.join(self.temp_dir.name, "empty_func.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write("def foo():\n    pass\n")
        self.cache.get_content(py_file)

        # Call with empty string
        callers = trace_lexical_dependencies_regex(
            "", [py_file], file_cache=self.cache
        )
        self.assertEqual(callers, {})

    def test_trace_lexical_dependencies_regex_js_multiline_and_ts_definitions(self):
        """Verify that multiline signatures, var/const declarations, and TS types/interfaces are not treated as calls."""
        ts_file = os.path.join(self.temp_dir.name, "app.ts")
        content = (
            "interface myFunc {\n"
            "  name: string;\n"
            "}\n"
            "type myFunc = () => void;\n"
            "const myFunc =\n"
            "  (x) => x;\n"
            "class MyClass {\n"
            "  public static async myFunc(\n"
            "    a: number,\n"
            "    b: number\n"
            "  ): Promise<void> {\n"
            "  }\n"
            "}\n"
            "myFunc(); // This is the actual call\n"
        )
        with open(ts_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(ts_file)

        callers = trace_lexical_dependencies_regex(
            "myFunc", [ts_file], file_cache=self.cache
        )
        self.assertIn(ts_file, callers)
        occurrences = callers[ts_file]
        # Only the actual call on line 14 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 14)
        self.assertEqual(occurrences[0]["code"], "myFunc(); // This is the actual call")

    def test_trace_lexical_dependencies_regex_cpp_definition_keywords(self):
        """Verify that class, struct, union, enum, namespace, and using are treated as definitions in C++."""
        cpp_file = os.path.join(self.temp_dir.name, "defs.cpp")
        content = (
            "namespace myFunc {\n"
            "  union myFunc {\n"
            "    int val;\n"
            "  };\n"
            "  enum myFunc {\n"
            "    A, B\n"
            "  };\n"
            "  struct myFunc {\n"
            "    int a;\n"
            "  };\n"
            "  class myFunc {\n"
            "  };\n"
            "  using myFunc = int;\n"
            "}\n"
            "void test() {\n"
            "  using namespace myFunc; // This is a reference, should be matched!\n"
            "  myFunc(); // Actual call\n"
            "}\n"
        )
        with open(cpp_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(cpp_file)

        callers = trace_lexical_dependencies_regex(
            "myFunc", [cpp_file], file_cache=self.cache
        )
        self.assertIn(cpp_file, callers)
        occurrences = callers[cpp_file]
        # Lines 16 (using namespace myFunc;) and 17 (myFunc();) should be matched
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(occurrences[0]["line"], 16)
        self.assertEqual(occurrences[0]["code"], "using namespace myFunc; // This is a reference, should be matched!")
        self.assertEqual(occurrences[1]["line"], 17)
        self.assertEqual(occurrences[1]["code"], "myFunc(); // Actual call")

    def test_trace_lexical_dependencies_regex_go_type_definitions(self):
        """Verify that Go type definitions (structs, interfaces) are ignored during caller tracing."""
        go_file = os.path.join(self.temp_dir.name, "types.go")
        content = (
            "package main\n"
            "type myFunc struct {}\n"
            "type myFunc interface {}\n"
            "type myFunc int\n"
            "func test() {\n"
            "  myFunc(); // Actual call\n"
            "}\n"
        )
        with open(go_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(go_file)

        callers = trace_lexical_dependencies_regex(
            "myFunc", [go_file], file_cache=self.cache
        )
        self.assertIn(go_file, callers)
        occurrences = callers[go_file]
        # Only the actual call on line 6 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 6)
        self.assertEqual(occurrences[0]["code"], "myFunc(); // Actual call")

    def test_trace_lexical_dependencies_regex_rust_definition_keywords(self):
        """Verify that Rust definition keywords are ignored during caller tracing."""
        rs_file = os.path.join(self.temp_dir.name, "defs.rs")
        content = (
            "mod myFunc {\n"
            "  struct myFunc {}\n"
            "  enum myFunc {}\n"
            "  union myFunc {}\n"
            "  type myFunc = i32;\n"
            "  trait myFunc {}\n"
            "  const myFunc: i32 = 42;\n"
            "  static myFunc: i32 = 42;\n"
            "}\n"
            "fn test() {\n"
            "  myFunc(); // Actual call\n"
            "}\n"
        )
        with open(rs_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(rs_file)

        callers = trace_lexical_dependencies_regex(
            "myFunc", [rs_file], file_cache=self.cache
        )
        self.assertIn(rs_file, callers)
        occurrences = callers[rs_file]
        # Only the actual call on line 11 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 11)
        self.assertEqual(occurrences[0]["code"], "myFunc(); // Actual call")

    def test_trace_lexical_dependencies_regex_ts_generics(self):
        """Verify that TS definitions with generic type parameters are ignored during caller tracing."""
        ts_file = os.path.join(self.temp_dir.name, "generics.ts")
        content = (
            "const myFunc = <T>(arg: T): T => arg;\n"
            "class MyClass {\n"
            "  myFunc<T>(arg: T): T {\n"
            "    return arg;\n"
            "  }\n"
            "  async anotherGeneric<T>(\n"
            "    x: T\n"
            "  ) {}\n"
            "}\n"
            "myFunc(); // Actual call\n"
        )
        with open(ts_file, "w", encoding="utf-8") as f:
            f.write(content)
        self.cache.get_content(ts_file)

        # Trace myFunc
        callers = trace_lexical_dependencies_regex(
            "myFunc", [ts_file], file_cache=self.cache
        )
        self.assertIn(ts_file, callers)
        occurrences = callers[ts_file]
        # Only the actual call on line 10 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 10)
        self.assertEqual(occurrences[0]["code"], "myFunc(); // Actual call")

        # Trace anotherGeneric (should have 0 callers because line 6 is a definition)
        callers = trace_lexical_dependencies_regex(
            "anotherGeneric", [ts_file], file_cache=self.cache
        )
        self.assertEqual(callers, {})

    def test_trace_lexical_dependencies_regex_advanced_definition_variants(self):
        """Verify that JS object properties, TS type arrows, Go anonymous function assignments, and C++ using/typedefs are ignored as definitions."""
        js_file = os.path.join(self.temp_dir.name, "object_prop.js")
        go_file = os.path.join(self.temp_dir.name, "assign.go")
        cpp_file = os.path.join(self.temp_dir.name, "types_cpp.cpp")

        # 1. JS/TS Object property & Type arrows
        js_content = (
            "const obj = {\n"
            "  myFunc: (a, b) => {\n"
            "    console.log(a);\n"
            "  }\n"
            "};\n"
            "type Handler = {\n"
            "  myFunc: (arg: number) => void;\n"
            "}\n"
            "myFunc(); // Actual call\n"
        )
        with open(js_file, "w", encoding="utf-8") as f:
            f.write(js_content)
        self.cache.get_content(js_file)

        # 2. Go Anonymous function assignment
        go_content = (
            "package main\n"
            "var myFunc = func() {}\n"
            "func main() {\n"
            "  myFunc := func(x int) {}\n"
            "  myFunc() // Actual call\n"
            "}\n"
        )
        with open(go_file, "w", encoding="utf-8") as f:
            f.write(go_content)
        self.cache.get_content(go_file)

        # 3. C/C++ typedef and using
        cpp_content = (
            "using myFunc = void(*)(int);\n"
            "typedef void (*myFunc)(int);\n"
            "typedef struct {\n"
            "  int x;\n"
            "} myFunc;\n"
            "void test() {\n"
            "  myFunc(); // Actual call\n"
            "}\n"
        )
        with open(cpp_file, "w", encoding="utf-8") as f:
            f.write(cpp_content)
        self.cache.get_content(cpp_file)

        # Trace JS
        js_callers = trace_lexical_dependencies_regex("myFunc", [js_file], file_cache=self.cache)
        self.assertIn(js_file, js_callers)
        self.assertEqual(len(js_callers[js_file]), 1)
        self.assertEqual(js_callers[js_file][0]["line"], 9)

        # Trace Go
        go_callers = trace_lexical_dependencies_regex("myFunc", [go_file], file_cache=self.cache)
        self.assertIn(go_file, go_callers)
        self.assertEqual(len(go_callers[go_file]), 1)
        self.assertEqual(go_callers[go_file][0]["line"], 5)

        # Trace CPP
        cpp_callers = trace_lexical_dependencies_regex("myFunc", [cpp_file], file_cache=self.cache)
        self.assertIn(cpp_file, cpp_callers)
        self.assertEqual(len(cpp_callers[cpp_file]), 1)
        self.assertEqual(cpp_callers[cpp_file][0]["line"], 7)

    def test_find_callee_definition_macro_heuristics(self):
        # Checks that find_callee_definition successfully identifies macro-generated
        # and macro-prefixed definitions in C++.
        code = (
            "TEST_F(MyClass, myTarget) {\n"
            "    // body\n"
            "}\n"
            "UFUNCTION(BlueprintCallable) void myTargetSameLine() {\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "macros.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        # Test finding definition in Google Test macro-argument format
        path, line = find_callee_definition("myTarget", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 1)

        # Test finding definition in Unreal same-line macro prefixed format
        path, line = find_callee_definition("myTargetSameLine", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 4)

    def test_find_callee_definition_ignores_block_comments(self):
        # Verify find_callee_definition ignores definitions that are commented out in block comments.
        code = (
            "/*\n"
            "void my_commented_func() {\n"
            "}\n"
            "*/\n"
            "void my_commented_func() {\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "block_comments.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        path, line = find_callee_definition("my_commented_func", [file_path], file_cache=self.cache)
        self.assertEqual(path, file_path)
        self.assertEqual(line, 5)  # Should point to line 5, not line 2

    def test_extract_callees_regex_ignores_block_comments(self):
        # Verify extract_callees_regex ignores call matches inside block comments.
        code = (
            "void my_caller() {\n"
            "    active_call();\n"
            "    /*\n"
            "    commented_call();\n"
            "    */\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "callees_block.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        callees = extract_callees_regex(file_path, 0, 6, self.cache)
        self.assertIn("active_call", callees)
        self.assertNotIn("commented_call", callees)

    def test_extract_function_bounds_regex_ignores_unbalanced_braces_in_block_comments(self):
        # Verify bounds extraction isn't corrupted by unbalanced braces inside block comments.
        code = (
            "void my_func() {\n"
            "    /* \n"
            "    } \n"
            "    */\n"
            "    int x = 0;\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "bounds_block.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        start, end = extract_function_bounds_regex(file_path, 1, self.cache)
        self.assertEqual(start, 0)
        self.assertEqual(end, 6)

    def test_extract_identifiers_regex(self):
        from context_builder.ast_engine import extract_identifiers_regex

        code = (
            "int x = 42;\n"
            "auto y = 100;\n"
            "if (x > y) {\n"
            "    my_func(x, y);\n"
            "    std::cout << x;\n"
            "    while (true) { return x; }\n"
            "}\n"
        )
        file_path = os.path.join(self.temp_dir.name, "regex_vars.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        ids_line_1 = extract_identifiers_regex(file_path, [1], file_cache=self.cache)
        self.assertEqual(ids_line_1, {"x"})

        ids_line_2 = extract_identifiers_regex(file_path, [2], file_cache=self.cache)
        self.assertEqual(ids_line_2, {"y"})

        ids_line_3 = extract_identifiers_regex(file_path, [3], file_cache=self.cache)
        self.assertEqual(ids_line_3, {"x", "y"})

        ids_line_4 = extract_identifiers_regex(file_path, [4], file_cache=self.cache)
        self.assertEqual(ids_line_4, {"x", "y"})

        ids_line_5 = extract_identifiers_regex(file_path, [5], file_cache=self.cache)
        self.assertEqual(ids_line_5, {"cout", "x"})

        ids_line_6 = extract_identifiers_regex(file_path, [6], file_cache=self.cache)
        self.assertEqual(ids_line_6, {"true", "x"})

        ids_multi = extract_identifiers_regex(file_path, [1, 2], file_cache=self.cache)
        self.assertEqual(ids_multi, {"x", "y"})

    def test_extract_identifiers_regex_ignores_member_properties(self):
        from context_builder.ast_engine import extract_identifiers_regex

        code = (
            "obj.x = local;\n"
            "ptr->x = other;\n"
            "ns::x(value);\n"
            "x = obj.y + ptr->z;\n"
            "spaced . prop = value;\n"
        )
        file_path = os.path.join(self.temp_dir.name, "regex_member_props.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        self.cache.get_content(file_path)

        ids = extract_identifiers_regex(file_path, [1, 2, 3, 4, 5], file_cache=self.cache)
        self.assertIn("obj", ids)
        self.assertIn("ptr", ids)
        self.assertIn("spaced", ids)
        self.assertIn("local", ids)
        self.assertIn("other", ids)
        self.assertIn("value", ids)
        self.assertIn("x", ids)
        self.assertNotIn("y", ids)
        self.assertNotIn("z", ids)
        self.assertNotIn("prop", ids)

    def test_is_line_definition_of_var_ignores_member_assignments(self):
        from context_builder.ast_engine import is_line_definition_of_var
        from context_builder.languages.python import PYTHON
        from context_builder.languages.c_family import C_FAMILY

        self.assertFalse(is_line_definition_of_var("self.x = 1", "x", PYTHON))
        self.assertFalse(is_line_definition_of_var("obj.x = 1", "x", PYTHON))
        self.assertFalse(is_line_definition_of_var("ptr->x = 1", "x", C_FAMILY))
        self.assertFalse(is_line_definition_of_var("ns::x = 1", "x", C_FAMILY))
        self.assertTrue(is_line_definition_of_var("x = 1", "x", PYTHON))

    def test_is_line_definition_of_var_ignores_language_declarations(self):
        from context_builder.ast_engine import is_line_definition_of_var
        from context_builder.languages.c_family import C_FAMILY
        from context_builder.languages.go import GO
        from context_builder.languages.java import JAVA
        from context_builder.languages.javascript import JAVASCRIPT, TYPESCRIPT
        from context_builder.languages.python import PYTHON
        from context_builder.languages.rust import RUST

        cases = [
            (C_FAMILY, "namespace app {", "app"),
            (C_FAMILY, "using Name = other::Name;", "Name"),
            (C_FAMILY, "template <typename T>", "T"),
            (C_FAMILY, "typename T value;", "T"),
            (GO, "package main", "main"),
            (GO, 'import fmt "fmt"', "fmt"),
            (JAVA, "package app;", "app"),
            (JAVA, "import app.Widget;", "app"),
            (JAVASCRIPT, "import thing from './thing';", "thing"),
            (JAVASCRIPT, "export value;", "value"),
            (TYPESCRIPT, "export type User = {}", "type"),
            (PYTHON, "import os", "os"),
            (PYTHON, "from pkg import item", "pkg"),
            (RUST, "use crate::thing;", "crate"),
            (RUST, "mod app;", "app"),
        ]

        for profile, line, var_name in cases:
            with self.subTest(profile=profile.name, line=line, var_name=var_name):
                self.assertFalse(is_line_definition_of_var(line, var_name, profile))

    def test_is_line_definition_of_var_matches_common_augmented_assignments(self):
        from context_builder.ast_engine import is_line_definition_of_var
        from context_builder.languages.c_family import C_FAMILY
        from context_builder.languages.go import GO
        from context_builder.languages.python import PYTHON

        cases = [
            (PYTHON, "x //= y", "x"),
            (PYTHON, "x **= y", "x"),
            (PYTHON, "x @= y", "x"),
            (C_FAMILY, "x %= y;", "x"),
            (C_FAMILY, "x <<= y;", "x"),
            (C_FAMILY, "x >>= y;", "x"),
            (C_FAMILY, "x &= y;", "x"),
            (C_FAMILY, "x |= y;", "x"),
            (C_FAMILY, "x ^= y;", "x"),
            (GO, "x &^= y", "x"),
        ]

        for profile, line, var_name in cases:
            with self.subTest(profile=profile.name, line=line):
                self.assertTrue(is_line_definition_of_var(line, var_name, profile))

    def test_is_line_definition_of_var_allows_short_decl_in_flow_statement(self):
        from context_builder.ast_engine import is_line_definition_of_var
        from context_builder.languages.go import GO
        from context_builder.languages.python import PYTHON

        self.assertTrue(is_line_definition_of_var("if x := 1; x < 2 {", "x", GO))
        self.assertTrue(is_line_definition_of_var("for i := 0; i < 10; i++ {", "i", GO))
        self.assertTrue(is_line_definition_of_var("if (x := value):", "x", PYTHON))

    def test_is_line_definition_of_var_checks_statements_after_flow_control(self):
        from context_builder.ast_engine import is_line_definition_of_var
        from context_builder.languages.c_family import C_FAMILY

        self.assertTrue(is_line_definition_of_var("if (ready); x = 1;", "x", C_FAMILY))
        self.assertTrue(is_line_definition_of_var("while (ready); x += 1;", "x", C_FAMILY))

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_identifiers_ast(self, mock_engine):
        from context_builder.ast_engine import extract_identifiers_ast

        class FakeNode:
            def __init__(self, node_type, start_line, text=None, children=None, parent=None, fields=None):
                self.type = node_type
                self.start_point = (start_line - 1, 0)
                self.text = text.encode('utf-8') if isinstance(text, str) else text
                self.children = children or []
                self.parent = parent
                self.fields = fields or {}
                for child in self.children:
                    child.parent = self

            def child_by_field_name(self, name):
                return self.fields.get(name)

        var_node = FakeNode("identifier", 2, "my_var")

        # func_call(x)
        call_id = FakeNode("identifier", 3, "func_call")
        arg_id = FakeNode("identifier", 3, "x")
        call_expr = FakeNode(
            "call_expression", 3,
            children=[call_id, arg_id],
            fields={"function": call_id}
        )

        # obj.foo
        obj_id = FakeNode("identifier", 4, "obj")
        foo_id = FakeNode("identifier", 4, "foo")
        member_expr = FakeNode(
            "member_expression", 4,
            children=[obj_id, foo_id],
            fields={"object": obj_id, "property": foo_id}
        )

        # void my_func()
        decl_id = FakeNode("identifier", 5, "my_func")
        func_decl = FakeNode(
            "function_declarator", 5,
            children=[decl_id],
            fields={"declarator": decl_id}
        )

        root = FakeNode("module", 1, children=[var_node, call_expr, member_expr, func_decl])

        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        mock_engine.is_supported.return_value = True

        cache = MagicMock()
        cache.get_bytes.return_value = b"some code"

        res = extract_identifiers_ast("file.py", [2, 3, 4, 5], file_cache=cache)
        self.assertEqual(res, {"my_var", "x", "obj"})

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_identifiers_ast_normalizes_uppercase_extension(self, mock_engine):
        from context_builder.ast_engine import extract_identifiers_ast

        class FakeNode:
            type = "module"
            children = []
            root_node = None

        root = FakeNode()
        root.root_node = root
        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        mock_engine.is_supported.return_value = True

        cache = MagicMock()
        cache.get_bytes.return_value = b"some code"
        cache.get_lines.return_value = []

        extract_identifiers_ast("file.PY", [1], file_cache=cache)

        mock_engine.is_supported.assert_called_once_with(".py")
        parser.parse.assert_called_once_with(b"some code")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_identifiers_ast_skips_sparse_nonintersecting_subtrees(self, mock_engine):
        from context_builder.ast_engine import extract_identifiers_ast

        class FakeNode:
            def __init__(
                    self, node_type, start_line, end_line=None, text=None,
                    children=None, fail_on_children=False
            ):
                self.type = node_type
                self.start_point = (start_line - 1, 0)
                self.end_point = ((end_line or start_line) - 1, 0)
                self.text = text.encode('utf-8') if isinstance(text, str) else text
                self._children = children or []
                self.fail_on_children = fail_on_children
                self.parent = None
                for child in self._children:
                    child.parent = self

            @property
            def children(self):
                if self.fail_on_children:
                    raise AssertionError("non-intersecting subtree was traversed")
                return self._children

            def child_by_field_name(self, _name):
                return None

        target_a = FakeNode("identifier", 10, text="line_ten")
        ignored_subtree = FakeNode(
            "block", 20, 30, children=[FakeNode("identifier", 25, text="ignored")],
            fail_on_children=True
        )
        target_b = FakeNode("identifier", 100, text="line_hundred")
        root = FakeNode("module", 1, 120, children=[target_a, ignored_subtree, target_b])

        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        mock_engine.is_supported.return_value = True

        cache = MagicMock()
        cache.get_bytes.return_value = b"some code"
        cache.get_lines.return_value = [""] * 120

        res = extract_identifiers_ast("file.py", [10, 100], file_cache=cache)

        self.assertEqual(res, {"line_ten", "line_hundred"})

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_identifiers_unified(self, mock_engine):
        from context_builder.ast_engine import extract_identifiers

        mock_engine.is_supported.return_value = True

        with patch("context_builder.ast_engine.extract_identifiers_ast") as mock_ast:
            mock_ast.return_value = {"ast_var"}
            res = extract_identifiers("file.py", [1], file_cache=self.cache)
            self.assertEqual(res, {"ast_var"})
            mock_ast.assert_called_once()

        with patch("context_builder.ast_engine.extract_identifiers_ast") as mock_ast, \
             patch("context_builder.ast_engine.extract_identifiers_regex") as mock_regex:
            mock_ast.side_effect = RuntimeError("AST parsing error")
            mock_regex.return_value = {"regex_var"}

            res = extract_identifiers("file.py", [1], file_cache=self.cache)
            self.assertEqual(res, {"regex_var"})
            mock_regex.assert_called_once()

        mock_engine.is_supported.return_value = False
        with patch("context_builder.ast_engine.extract_identifiers_regex") as mock_regex:
            mock_regex.return_value = {"regex_var_only"}
            res = extract_identifiers("file.py", [1], file_cache=self.cache)
            self.assertEqual(res, {"regex_var_only"})
            mock_regex.assert_called_once()

    def test_get_lhs_identifiers(self):
        from context_builder.ast_engine import get_lhs_identifiers

        class FakeNode:
            def __init__(self, node_type, children=None, parent=None, text=None):
                self.type = node_type
                self.children = children or []
                self.parent = parent
                self.text = text.encode('utf-8') if isinstance(text, str) else text
                for child in self.children:
                    child.parent = self
            def child_by_field_name(self, name):
                if name == "right" and len(self.children) > 2:
                    return self.children[2]
                return None

        x_node = FakeNode("identifier", text="x")
        op_node = FakeNode("=", text="=")
        y_node = FakeNode("identifier", text="y")
        assign_node = FakeNode("assignment_expression", children=[x_node, op_node, y_node])

        lhs_ids = get_lhs_identifiers(assign_node)
        self.assertEqual(lhs_ids, ["x"])

    def test_get_lhs_identifiers_handles_nodes_without_field_lookup(self):
        from context_builder.ast_engine import get_lhs_identifiers

        class MinimalNode:
            def __init__(self, node_type, children=None, text=None):
                self.type = node_type
                self.children = children or []
                self.text = text.encode('utf-8') if isinstance(text, str) else text

        x_node = MinimalNode("identifier", text="x")
        op_node = MinimalNode("=", text="=")
        y_node = MinimalNode("identifier", text="y")
        assign_node = MinimalNode("assignment_expression", children=[x_node, op_node, y_node])

        lhs_ids = get_lhs_identifiers(assign_node)
        self.assertEqual(lhs_ids, ["x"])

    def test_get_lhs_identifiers_stops_at_common_augmented_assignment(self):
        from context_builder.ast_engine import get_lhs_identifiers

        class MinimalNode:
            def __init__(self, node_type, children=None, text=None):
                self.type = node_type
                self.children = children or []
                self.text = text.encode('utf-8') if isinstance(text, str) else text

        x_node = MinimalNode("identifier", text="x")
        op_node = MinimalNode("**=", text="**=")
        y_node = MinimalNode("identifier", text="y")
        assign_node = MinimalNode("assignment_expression", children=[x_node, op_node, y_node])

        lhs_ids = get_lhs_identifiers(assign_node)
        self.assertEqual(lhs_ids, ["x"])

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_resolve_local_variable_ast(self, mock_engine):
        from context_builder.ast_engine import resolve_local_variable_ast

        class FakeNode:
            def __init__(self, node_type, start_line, text=None, children=None, parent=None):
                self.type = node_type
                self.start_point = (start_line - 1, 0)
                self.text = text.encode('utf-8') if isinstance(text, str) else text
                self.children = children or []
                self.parent = parent
                for child in self.children:
                    child.parent = self
            def child_by_field_name(self, _name):
                return None

        x_node = FakeNode("identifier", 2, "my_var")
        op_node = FakeNode("=", 2, "=")
        val_node = FakeNode("number", 2, "42")
        assign_node = FakeNode("assignment_expression", 2, children=[x_node, op_node, val_node])

        root = FakeNode("module", 1, children=[assign_node])
        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        mock_engine.is_supported.return_value = True
        mock_engine.languages = {}
        mock_engine.get_query.side_effect = RuntimeError("fall back to manual traversal")

        cache = MagicMock()
        cache.get_bytes.return_value = b"some code"
        cache.get_lines.return_value = ["line 1", "my_var = 42", "line 3"]

        with patch("context_builder.ast_engine.extract_function_bounds") as mock_bounds:
            mock_bounds.return_value = (0, 3)

            line, code = resolve_local_variable_ast("file.py", "my_var", 3, file_cache=cache)
            self.assertEqual(line, 2)
            self.assertEqual(code, "my_var = 42")

    @patch("context_builder.ast_engine.resolve_local_variable_ast")
    @patch("context_builder.lsp_client.get_lsp_definition")
    @patch("context_builder.lsp_client.get_lsp_type_definition")
    def test_resolve_variable_definition(self, mock_type_def, mock_def, mock_local):
        from context_builder.ast_engine import resolve_variable_definition

        mock_local.return_value = (2, "my_var = 42")
        res = resolve_variable_definition("file.py", "my_var", 3, 10, file_cache=self.cache)
        self.assertEqual(res["resolved_type"], "local")
        self.assertEqual(res["definitions"][0]["line"], 2)

        mock_local.return_value = (None, None)

        mock_def.return_value = [{
            "uri": "file:///c:/path/to/global.py",
            "range": {
                "start": {"line": 4, "character": 5},
                "end": {"line": 4, "character": 15}
            }
        }]
        mock_type_def.return_value = [{
            "uri": "file:///c:/path/to/type.py",
            "range": {
                "start": {"line": 9, "character": 2},
                "end": {"line": 9, "character": 12}
            }
        }]

        with patch("os.path.exists") as mock_exists:
            mock_exists.return_value = True

            cache = MagicMock()
            cache.get_lines.side_effect = lambda path: ["code 1", "code 2", "code 3", "code 4", "global_var = 10", "code 6", "code 7", "code 8", "code 9", "class User {}"]

            res = resolve_variable_definition("file.py", "my_var", 3, 10, file_cache=cache)
            self.assertEqual(res["resolved_type"], "global_and_type")
            self.assertEqual(len(res["definitions"]), 2)
            self.assertEqual(res["definitions"][0]["code"], "global_var = 10")
            self.assertEqual(res["definitions"][1]["code"], "class User {}")

    def test_regex_scope_building(self):
        from context_builder.ast_engine import build_scopes
        from context_builder.languages.python import PYTHON
        from context_builder.languages.c_family import C_FAMILY
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        # Test Python indentation scopes
        py_code = (
            "def foo():\n"
            "    x = 1\n"
            "    if True:\n"
            "        y = 2\n"
            "    return x\n"
        )
        py_file = os.path.join(self.temp_dir.name, "scopes_test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(py_code)

        _, all_scopes = build_scopes(py_file, PYTHON, cache)
        # Global scope + def scope + if scope
        self.assertEqual(len(all_scopes), 3)
        self.assertEqual(all_scopes[1].start_line, 1)  # def foo():
        self.assertEqual(all_scopes[2].start_line, 3)  # if True:
        self.assertEqual(all_scopes[2].end_line, 4)

        # Test C++ brace scopes
        cpp_code = (
            "void foo() {\n"
            "    int x = 1;\n"
            "    if (true) {\n"
            "        int y = 2;\n"
            "    }\n"
            "}\n"
        )
        cpp_file = os.path.join(self.temp_dir.name, "scopes_test.cpp")
        with open(cpp_file, "w", encoding="utf-8") as f:
            f.write(cpp_code)

        _, all_scopes_cpp = build_scopes(cpp_file, C_FAMILY, cache)
        self.assertEqual(len(all_scopes_cpp), 3)
        self.assertEqual(all_scopes_cpp[1].start_line, 1)  # void foo() {
        self.assertEqual(all_scopes_cpp[2].start_line, 3)  # if (true) {
        self.assertEqual(all_scopes_cpp[2].end_line, 5)

    def test_regex_fallback_local_resolution(self):
        from context_builder.ast_engine import resolve_variable_definition
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        py_code = (
            "x = 100\n"  # line 1 (global)
            "def foo():\n"  # line 2
            "    x = 1\n"  # line 3
            "    if True:\n"  # line 4
            "        y = 2\n"  # line 5
            "        print(x)\n"  # line 6 (ref to x)
        )
        py_file = os.path.join(self.temp_dir.name, "local_test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(py_code)

        # Mock AST check to fail to force regex fallback
        with patch("context_builder.ast_engine.resolve_local_variable_ast") as mock_ast, \
             patch("context_builder.lsp_client.get_lsp_definition") as mock_lsp, \
             patch("context_builder.lsp_client.get_lsp_type_definition") as mock_type_lsp:
            mock_ast.return_value = (None, None)
            mock_lsp.return_value = []
            mock_type_lsp.return_value = []

            # Resolve 'x' referenced at line 6
            res = resolve_variable_definition(py_file, "x", 6, 14, file_cache=cache)
            self.assertEqual(res["resolved_type"], "local_regex")
            self.assertEqual(len(res["definitions"]), 1)
            self.assertEqual(res["definitions"][0]["line"], 3)  # local definition in foo()

            # Resolve 'y' referenced at line 6 (should not find since it's defined in child 'if' scope)
            # Wait, y is defined at line 5 (which is inside the if block).
            # But line 6 is also inside the same if block! So it should find it!
            res_y = resolve_variable_definition(py_file, "y", 6, 14, file_cache=cache)
            self.assertEqual(res_y["resolved_type"], "local_regex")
            self.assertEqual(res_y["definitions"][0]["line"], 5)

    def test_regex_fallback_member_and_inheritance(self):
        from context_builder.ast_engine import resolve_variable_definition
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        # Multiple files to test member crawling and parent inheritance
        parent_code = (
            "class Base:\n"
            "    base_val = 42\n"
        )
        child_code = (
            "from parent import Base\n"
            "class Child(Base):\n"
            "    child_val = 24\n"
            "    def method(self):\n"
            "        print(self.base_val)\n"  # line 5
        )

        parent_file = os.path.join(self.temp_dir.name, "parent.py")
        child_file = os.path.join(self.temp_dir.name, "child.py")
        with open(parent_file, "w", encoding="utf-8") as f:
            f.write(parent_code)
        with open(child_file, "w", encoding="utf-8") as f:
            f.write(child_code)

        with patch("context_builder.ast_engine.resolve_local_variable_ast") as mock_ast, \
             patch("context_builder.lsp_client.get_lsp_definition") as mock_lsp, \
             patch("context_builder.lsp_client.get_lsp_type_definition") as mock_type_lsp, \
             patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_ast.return_value = (None, None)
            mock_lsp.return_value = []
            mock_type_lsp.return_value = []
            mock_tracked.return_value = [parent_file, child_file]

            # Resolve base_val on child class instance referenced in child_file
            res = resolve_variable_definition(child_file, "base_val", 5, 19, file_cache=cache)
            self.assertEqual(res["resolved_type"], "member_regex")
            self.assertEqual(len(res["definitions"]), 1)
            self.assertEqual(res["definitions"][0]["line"], 2)
            self.assertEqual(res["definitions"][0]["code"], "base_val = 42")

    def test_regex_fallback_global_search(self):
        from context_builder.ast_engine import resolve_variable_definition
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        lib_code = (
            "GLOBAL_CONST = 'HELLO'\n"
            "def helper():\n"
            "    non_global = 'FAIL'\n"
        )
        main_code = (
            "import lib\n"
            "def run():\n"
            "    print(GLOBAL_CONST)\n"
        )

        lib_file = os.path.join(self.temp_dir.name, "lib.py")
        main_file = os.path.join(self.temp_dir.name, "main.py")
        with open(lib_file, "w", encoding="utf-8") as f:
            f.write(lib_code)
        with open(main_file, "w", encoding="utf-8") as f:
            f.write(main_code)

        with patch("context_builder.ast_engine.resolve_local_variable_ast") as mock_ast, \
             patch("context_builder.lsp_client.get_lsp_definition") as mock_lsp, \
             patch("context_builder.lsp_client.get_lsp_type_definition") as mock_type_lsp, \
             patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_ast.return_value = (None, None)
            mock_lsp.return_value = []
            mock_type_lsp.return_value = []
            mock_tracked.return_value = [lib_file, main_file]

            # Should find GLOBAL_CONST in lib_file since it's global
            res = resolve_variable_definition(main_file, "GLOBAL_CONST", 3, 10, file_cache=cache)
            self.assertEqual(res["resolved_type"], "global_regex")
            self.assertEqual(len(res["definitions"]), 1)
            self.assertEqual(res["definitions"][0]["line"], 1)
            self.assertEqual(res["definitions"][0]["code"], "GLOBAL_CONST = 'HELLO'")

            # Should ignore non_global because it is inside helper() scope
            res_fail = resolve_variable_definition(main_file, "non_global", 3, 10, file_cache=cache)
            self.assertEqual(res_fail["resolved_type"], "none")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_identifiers_with_positions_ast_unicode(self, mock_engine):
        from context_builder.ast_engine import extract_identifiers_with_positions_ast

        class FakeNode:
            def __init__(self, node_type, start_line, byte_col, text=None, children=None):
                self.type = node_type
                self.start_point = (start_line - 1, byte_col)
                self.text = text.encode('utf-8') if isinstance(text, str) else text
                self.children = children or []
                self.parent = None
                for child in self.children:
                    child.parent = self

        # 🌟 is 4 bytes in UTF-8, but 2 character units in UTF-16 surrogate pairs.
        # "🌟_x" at byte offset 4 is "_x" at UTF-16 character offset 2.
        x_node = FakeNode("identifier", 2, 4, "_x")
        op_node = FakeNode("=", 2, 8, "=")
        assign_node = FakeNode("assignment_expression", 2, 0, children=[x_node, op_node])

        root = FakeNode("module", 1, 0, children=[assign_node])
        parser = MagicMock()
        parser.parse.return_value = SimpleNamespace(root_node=root)
        mock_engine.parsers = {".py": parser}
        mock_engine.is_supported.return_value = True

        cache = MagicMock()
        cache.get_bytes.return_value = "line 1\n🌟_x = 1".encode("utf-8")
        cache.get_lines.return_value = ["line 1", "🌟_x = 1"]

        res = extract_identifiers_with_positions_ast("file.py", [2], file_cache=cache)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][0], "_x")
        self.assertEqual(res[0][1], 2)
        self.assertEqual(res[0][2], 2)  # Column character offset in UTF-16

    def test_get_directly_included_files_relative_imports(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.python import PYTHON
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        # Create a subdirectory for containment safety
        subdir = os.path.join(self.temp_dir.name, "subdir")
        os.makedirs(subdir, exist_ok=True)

        # Python relative imports with dots
        py_code = (
            "from .sys_utils import something\n"
            "from ..other_module import something_else\n"
        )
        py_file = os.path.join(subdir, "relative_test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(py_code)

        # Mock get_git_tracked_files
        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            # We want tracked files to include sys_utils.py and other_module.py
            sys_utils_path = os.path.join(subdir, "sys_utils.py")
            other_module_path = os.path.join(self.temp_dir.name, "other_module.py")
            with open(sys_utils_path, "w", encoding="utf-8") as f:
                f.write("")
            with open(other_module_path, "w", encoding="utf-8") as f:
                f.write("")
            mock_tracked.return_value = [sys_utils_path, other_module_path]

            res = get_directly_included_files(py_file, PYTHON, cache)
            self.assertIn(os.path.abspath(sys_utils_path), res)
            self.assertIn(os.path.abspath(other_module_path), res)

    def test_get_directly_included_files_nested_python_imports(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.python import PYTHON
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        # Test code containing nested absolute and relative Python imports
        py_code = (
            "import my_package.subpackage.my_module\n"
            "from my_package.subpackage.other_module import x\n"
            "from .sub.nested_module import y\n"
        )
        py_file = os.path.join(self.temp_dir.name, "nested_import_test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(py_code)

        # Set up directories and files simulating the tracked package structure
        my_package_dir = os.path.join(self.temp_dir.name, "my_package", "subpackage")
        os.makedirs(my_package_dir, exist_ok=True)
        sub_dir = os.path.join(self.temp_dir.name, "sub")
        os.makedirs(sub_dir, exist_ok=True)

        my_module_path = os.path.join(my_package_dir, "my_module.py")
        other_module_path = os.path.join(my_package_dir, "other_module.py")
        nested_module_path = os.path.join(sub_dir, "nested_module.py")

        for path in (my_module_path, other_module_path, nested_module_path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

        # Mock get_git_tracked_files to include these paths
        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_tracked.return_value = [my_module_path, other_module_path, nested_module_path]
            res = get_directly_included_files(py_file, PYTHON, cache)

        self.assertIn(os.path.abspath(my_module_path), res)
        self.assertIn(os.path.abspath(other_module_path), res)
        self.assertIn(os.path.abspath(nested_module_path), res)

    def test_resolve_variable_definition_regex_fallback_empty_file(self):
        from context_builder.ast_engine import resolve_variable_definition_regex_fallback
        from context_builder.languages.python import PYTHON

        cache = MagicMock()
        cache.get_lines.return_value = []

        res = resolve_variable_definition_regex_fallback(
            "empty.py", "my_var", 1, cache, PYTHON
        )
        self.assertEqual(res["resolved_type"], "none")
        self.assertEqual(res["definitions"], [])

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    def test_find_class_definition_uses_ripgrep_filter(self, mock_tracked, mock_filter):
        from context_builder.ast_engine import find_class_definition
        from context_builder.languages.python import PYTHON

        mock_tracked.return_value = ["file1.py", "file2.py"]
        mock_filter.return_value = ["file2.py"]

        cache = MagicMock()
        cache.get_lines.side_effect = lambda path: (
            [] if path == "file1.py" else ["class TargetClass:", "    pass"]
        )

        with patch("os.path.exists", return_value=True):
            res_file, res_line = find_class_definition(
                "file1.py", "TargetClass", PYTHON, cache
            )

        self.assertEqual(res_file, "file2.py")
        self.assertEqual(res_line, 1)

        # Verify ripgrep_filter was called to pre-filter file1.py out
        mock_filter.assert_called_once_with(
            ["file2.py"], "TargetClass", fallback_hint="class/struct definition of 'TargetClass'"
        )

    @patch("context_builder.ast_engine.ripgrep_filter")
    @patch("context_builder.sys_utils.get_git_tracked_files")
    def test_resolve_global_definition_uses_ripgrep_filter(self, mock_tracked, mock_filter):
        from context_builder.ast_engine import resolve_global_definition
        from context_builder.languages.python import PYTHON

        mock_tracked.return_value = ["file1.py", "file2.py"]
        mock_filter.return_value = ["file2.py"]

        cache = MagicMock()
        cache.get_lines.side_effect = lambda path: (
            [] if path == "file1.py" else ["GLOBAL_VAR = 42"]
        )

        # Mock build_scopes to return a dummy global scope
        from context_builder.ast_engine import RegexScope
        dummy_scope = RegexScope(1)
        dummy_scope.end_line = 2

        with patch("os.path.exists", return_value=True), \
             patch("context_builder.ast_engine.build_scopes", return_value=(dummy_scope, [dummy_scope])), \
             patch("context_builder.ast_engine.get_lines_directly_in_scope", return_value=[1]):
            res = resolve_global_definition(
                "file1.py", "GLOBAL_VAR", PYTHON, cache
            )

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["path"], "file2.py")
        self.assertEqual(res[0]["line"], 1)

        # Verify ripgrep_filter was called
        mock_filter.assert_called_once_with(
            ["file1.py", "file2.py"], "GLOBAL_VAR", fallback_hint="global definition of 'GLOBAL_VAR'"
        )

    def test_align_clean_to_original(self):
        from context_builder.ast_engine import _align_clean_to_original

        # Test basic alignment where nothing is stripped
        orig1 = "foo bar"
        clean1 = "foo bar"
        self.assertEqual(_align_clean_to_original(orig1, clean1), [0, 1, 2, 3, 4, 5, 6])

        # Test alignment with inline comment stripped
        orig2 = "foo // comment\n"
        clean2 = "foo \n"
        mapping2 = _align_clean_to_original(orig2, clean2)
        # Expected mapping length = len(clean2) = 5
        # 'f' -> 0, 'o' -> 1, 'o' -> 2, ' ' -> 3, '\n' -> 14
        self.assertEqual(mapping2, [0, 1, 2, 3, 14])

        # Test alignment with string literal stripped
        orig3 = 'foo("hello") + bar'
        clean3 = 'foo() + bar'
        mapping3 = _align_clean_to_original(orig3, clean3)
        # Check that characters match correctly
        # clean3[6] is '+' -> should map to orig3[13] which is '+'
        self.assertEqual(mapping3[6], 13)
        # clean3[7] is ' ' -> should map to orig3[14]
        self.assertEqual(mapping3[7], 14)
        # clean3[8] is 'b' -> should map to orig3[15]
        self.assertEqual(mapping3[8], 15)

    def test_align_clean_to_original_equal_length_bypasses_difflib(self):
        from context_builder.ast_engine import _align_clean_to_original

        original = "foo # comment"
        clean = "foo          "

        with patch("context_builder.ast_engine.difflib.SequenceMatcher") as mock_matcher:
            mapping = _align_clean_to_original(original, clean)

        self.assertEqual(mapping, list(range(len(clean))))
        mock_matcher.assert_not_called()

    def test_extract_identifiers_with_positions_regex_alignment(self):
        from context_builder.ast_engine import extract_identifiers_with_positions_regex

        cache = MagicMock()
        # Original line has comment and string stripped
        # "    foo = 'value' # comment"
        # Identifier is "foo"
        cache.get_lines.return_value = ["    foo = 'value' # comment"]

        # Let's test using PYTHON profile (since it strips comments and strings)
        res = extract_identifiers_with_positions_regex("file.py", [1], file_cache=cache)
        # "foo" should start at index 4 in original (UTF-16 code units = 4)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0][0], "foo")
        self.assertEqual(res[0][1], 1)
        self.assertEqual(res[0][2], 4)

        # Let's test with unicode characters and surrogate pairs to verify UTF-16 conversion
        # "🌟_foo = 'val' // comment"
        # "🌟" is 1 char in Python, but 2 UTF-16 code units.
        # "_foo" starts at char index 1, which is UTF-16 index 2.
        cache.get_lines.return_value = ["🌟_foo = 'val' # comment"]
        res_unicode = extract_identifiers_with_positions_regex("file.py", [1], file_cache=cache)
        self.assertEqual(len(res_unicode), 1)
        self.assertEqual(res_unicode[0][0], "_foo")
        self.assertEqual(res_unicode[0][1], 1)
        self.assertEqual(res_unicode[0][2], 2)

    def test_get_class_members_new_line_brace(self):
        from context_builder.ast_engine import get_class_members
        from context_builder.languages.c_family import C_FAMILY

        cache = MagicMock()
        # Class with brace on the next line
        cache.get_lines.return_value = [
            "class TargetClass",
            "{",
            "    int myVar;",
            "};"
        ]

        res = get_class_members("file.cpp", "TargetClass", C_FAMILY, cache)
        self.assertEqual(res, [("myVar", 3)])

    def test_get_class_members_common_augmented_assignment(self):
        from context_builder.ast_engine import get_class_members
        from context_builder.languages.c_family import C_FAMILY

        cache = MagicMock()
        lines = [
            "class TargetClass {",
            "    flags &^= mask;",
            "};"
        ]
        cache.get_lines.return_value = lines
        cache.get_stripped_lines.return_value = lines

        res = get_class_members("file.cpp", "TargetClass", C_FAMILY, cache)
        self.assertIn(("flags", 2), res)
        self.assertNotIn(("mask", 2), res)

    def test_get_class_members_ignores_default_argument_signatures(self):
        from context_builder.ast_engine import get_class_members
        from context_builder.languages.c_family import C_FAMILY

        cache = MagicMock()
        lines = [
            "class TargetClass {",
            "    void myMethod(int x = 0);",
            "    int realMember = 1;",
            "};"
        ]
        cache.get_lines.return_value = lines
        cache.get_stripped_lines.return_value = lines

        res = get_class_members("file.cpp", "TargetClass", C_FAMILY, cache)

        self.assertIn(("realMember", 3), res)
        self.assertNotIn(("myMethod", 2), res)
        self.assertNotIn(("x", 2), res)

    def test_get_class_members_ignores_python_self_comparisons(self):
        from context_builder.ast_engine import get_class_members
        from context_builder.languages.python import PYTHON

        cache = MagicMock()
        lines = [
            "class TargetClass:",
            "    def method(self):",
            "        if self.foo == 1:",
            "            self.bar = 2",
        ]
        cache.get_lines.return_value = lines
        cache.get_stripped_lines.return_value = lines

        res = get_class_members("file.py", "TargetClass", PYTHON, cache)

        self.assertIn(("bar", 4), res)
        self.assertNotIn(("foo", 3), res)

    def test_go_type_struct_class_like_helpers(self):
        from context_builder.ast_engine import (
            find_class_definition,
            get_class_members,
            get_parent_classes,
        )
        from context_builder.languages.go import GO

        cache = MagicMock()
        lines = [
            "package main",
            "type User struct {",
            "    Name string",
            "}",
            "type Reader interface {",
            "    Read() error",
            "}",
        ]
        cache.get_lines.return_value = lines
        cache.get_stripped_lines.return_value = lines
        cache.find_class_definition_cache = {}
        cache.class_members_cache = {}

        members = get_class_members("file.go", "User", GO, cache)
        self.assertIn(("Name", 3), members)
        self.assertEqual(get_parent_classes("file.go", "User", GO, cache), [])
        self.assertEqual(find_class_definition("file.go", "User", GO, cache), ("file.go", 2))
        self.assertEqual(find_class_definition("file.go", "Reader", GO, cache), ("file.go", 5))

    def test_resolve_variable_definition_regex_fallback_new_line_brace(self):
        from context_builder.ast_engine import resolve_variable_definition

        cache = MagicMock()
        # Function/Class member variable resolution with braces on next lines
        lines = [
            "class TargetClass",
            "{",
            "    int myVar;",
            "    void myFunc()",
            "    {",
            "        myVar = 42;",
            "    }",
            "};"
        ]
        cache.get_lines.return_value = lines
        cache.get_stripped_lines.return_value = lines
        cache.get_bytes.return_value = "\n".join(lines).encode('utf-8')

        # We try to resolve "myVar" from line 6 (inside myFunc)
        res = resolve_variable_definition("file.cpp", "myVar", 6, 8, file_cache=cache)
        self.assertEqual(res["resolved_type"], "member_regex")
        self.assertEqual(res["definitions"][0]["line"], 3)

    def test_resolve_variable_definition_regex_fallback_parameter(self):
        from context_builder.ast_engine import resolve_variable_definition

        cache = MagicMock()
        # Function header with parameter in brace-based language (brace on new line)
        lines = [
            "void myFunc(",
            "    int myParam",
            ")",
            "{",
            "    int x = myParam;",
            "}"
        ]
        cache.get_lines.return_value = lines
        cache.get_stripped_lines.return_value = lines
        cache.get_bytes.return_value = "\n".join(lines).encode('utf-8')

        # We try to resolve "myParam" from line 5 (inside myFunc)
        res = resolve_variable_definition("file.cpp", "myParam", 5, 6, file_cache=cache)
        self.assertEqual(res["resolved_type"], "local_regex")
        self.assertEqual(res["definitions"][0]["line"], 2)

    def test_get_directly_included_files_python_as_alias(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.python import PYTHON
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        py_code = (
            "import my_module as alias\n"
            "import package.nested_module as nested_alias, other_module\n"
        )
        py_file = os.path.join(self.temp_dir.name, "python_alias_test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(py_code)

        my_module_path = os.path.join(self.temp_dir.name, "my_module.py")
        other_module_path = os.path.join(self.temp_dir.name, "other_module.py")
        package_dir = os.path.join(self.temp_dir.name, "package")
        os.makedirs(package_dir, exist_ok=True)
        nested_module_path = os.path.join(package_dir, "nested_module.py")

        for path in (my_module_path, other_module_path, nested_module_path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_tracked.return_value = [my_module_path, other_module_path, nested_module_path]
            res = get_directly_included_files(py_file, PYTHON, cache)

        self.assertIn(os.path.abspath(my_module_path), res)
        self.assertIn(os.path.abspath(other_module_path), res)
        self.assertIn(os.path.abspath(nested_module_path), res)

    def test_get_directly_included_files_go_alias(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.go import GO
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        go_code = (
            "package main\n"
            "import m \"math\"\n"
            "import . \"other/pkg\"\n"
        )
        go_file = os.path.join(self.temp_dir.name, "go_alias_test.go")
        with open(go_file, "w", encoding="utf-8") as f:
            f.write(go_code)

        math_path = os.path.join(self.temp_dir.name, "math.go")
        pkg_dir = os.path.join(self.temp_dir.name, "other", "pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        pkg_path = os.path.join(pkg_dir, "pkg.go")

        for path in (math_path, pkg_path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_tracked.return_value = [math_path, pkg_path]
            res = get_directly_included_files(go_file, GO, cache)

        self.assertIn(os.path.abspath(math_path), res)
        self.assertIn(os.path.abspath(pkg_path), res)

    def test_get_directly_included_files_go_grouped_imports(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.go import GO
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        go_code = (
            "package main\n"
            "import (\n"
            "    \"fmt\"\n"
            "    alias \"example.com/project/pkg\"\n"
            "    . \"example.com/project/other\"\n"
            ")\n"
        )
        go_file = os.path.join(self.temp_dir.name, "go_grouped_imports.go")
        with open(go_file, "w", encoding="utf-8") as f:
            f.write(go_code)

        fmt_path = os.path.join(self.temp_dir.name, "fmt.go")
        pkg_dir = os.path.join(self.temp_dir.name, "example.com", "project", "pkg")
        other_dir = os.path.join(self.temp_dir.name, "example.com", "project", "other")
        os.makedirs(pkg_dir, exist_ok=True)
        os.makedirs(other_dir, exist_ok=True)
        pkg_path = os.path.join(pkg_dir, "pkg.go")
        other_path = os.path.join(other_dir, "other.go")

        for path in (fmt_path, pkg_path, other_path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_tracked.return_value = [fmt_path, pkg_path, other_path]
            res = get_directly_included_files(go_file, GO, cache)

        self.assertIn(os.path.abspath(fmt_path), res)
        self.assertIn(os.path.abspath(pkg_path), res)
        self.assertIn(os.path.abspath(other_path), res)

    def test_get_directly_included_files_rust_crate_and_grouped_imports(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.rust import RUST
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        src_dir = os.path.join(self.temp_dir.name, "src")
        nested_dir = os.path.join(src_dir, "nested")
        os.makedirs(nested_dir, exist_ok=True)
        rust_code = (
            "use crate::foo::Thing;\n"
            "use crate::{bar::Bar, nested::{qux::Qux}};\n"
        )
        rust_file = os.path.join(src_dir, "main.rs")
        with open(rust_file, "w", encoding="utf-8") as f:
            f.write(rust_code)

        foo_path = os.path.join(src_dir, "foo.rs")
        bar_path = os.path.join(src_dir, "bar.rs")
        qux_path = os.path.join(nested_dir, "qux.rs")
        for path in (foo_path, bar_path, qux_path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_tracked.return_value = [foo_path, bar_path, qux_path]
            res = get_directly_included_files(rust_file, RUST, cache)

        self.assertIn(os.path.abspath(foo_path), res)
        self.assertIn(os.path.abspath(bar_path), res)
        self.assertIn(os.path.abspath(qux_path), res)

    def test_get_directly_included_files_python_relative_sibling_and_submodule(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.python import PYTHON
        from context_builder.cache import LRUFileCache

        cache = LRUFileCache(capacity=5)
        # Create a subdirectory for containment safety
        subdir = os.path.join(self.temp_dir.name, "subdir")
        os.makedirs(subdir, exist_ok=True)

        py_code = (
            "from . import sibling_module\n"
            "from package import submodule\n"
            "from .package import nested_submodule\n"
        )
        py_file = os.path.join(subdir, "import_test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(py_code)

        sibling_path = os.path.join(subdir, "sibling_module.py")
        package_dir = os.path.join(subdir, "package")
        os.makedirs(package_dir, exist_ok=True)
        submodule_path = os.path.join(subdir, "package", "submodule.py")
        nested_submodule_path = os.path.join(package_dir, "nested_submodule.py")

        for path in (sibling_path, submodule_path, nested_submodule_path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")

        with patch("context_builder.sys_utils.get_git_tracked_files") as mock_tracked:
            mock_tracked.return_value = [sibling_path, submodule_path, nested_submodule_path]
            res = get_directly_included_files(py_file, PYTHON, cache)

        self.assertIn(os.path.abspath(sibling_path), res)
        self.assertIn(os.path.abspath(submodule_path), res)
        self.assertIn(os.path.abspath(nested_submodule_path), res)

    def test_build_scopes_ignores_braces_in_block_comments(self):
        from context_builder.ast_engine import build_scopes
        from context_builder.languages.c_family import C_FAMILY

        # C++ code with braces inside a multiline block comment
        code = (
            "class MyClass {\n"
            "/*\n"
            "    void dummy() {\n"
            "        if (true) {\n"
            "        }\n"
            "    }\n"
            "*/\n"
            "    int realVar;\n"
            "};\n"
        )
        file_path = os.path.join(self.temp_dir.name, "scopes_comments.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        _, all_scopes = build_scopes(file_path, C_FAMILY, cache)

        # The block comment is stripped, so we should only have:
        # 1. Global scope
        # 2. MyClass scope
        # If the block comment wasn't stripped, we would have 3 or more nested scopes.
        self.assertEqual(len(all_scopes), 2)

    def test_get_class_members_ignores_commented_out_members(self):
        from context_builder.ast_engine import get_class_members
        from context_builder.languages.c_family import C_FAMILY

        code = (
            "class MyClass {\n"
            "/*\n"
            "    int commentedVar;\n"
            "*/\n"
            "    int realVar;\n"
            "};\n"
        )
        file_path = os.path.join(self.temp_dir.name, "members_comments.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        members = get_class_members(file_path, "MyClass", C_FAMILY, cache)
        names = [m[0] for m in members]
        self.assertIn("realVar", names)
        self.assertNotIn("commentedVar", names)

    def test_get_parent_classes_ignores_commented_out_inheritance(self):
        from context_builder.ast_engine import get_parent_classes
        from context_builder.languages.c_family import C_FAMILY

        code = (
            "/*\n"
            "class MyClass : public CommentedParent {};\n"
            "*/\n"
            "class MyClass : public RealParent {};\n"
        )
        file_path = os.path.join(self.temp_dir.name, "parent_comments.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        parents = get_parent_classes(file_path, "MyClass", C_FAMILY, cache)
        self.assertEqual(parents, ["RealParent"])

    def test_find_class_definition_ignores_commented_out_class(self):
        from context_builder.ast_engine import find_class_definition
        from context_builder.languages.c_family import C_FAMILY

        code = (
            "/*\n"
            "class MyClass {};\n"
            "*/\n"
        )
        file_path = os.path.join(self.temp_dir.name, "find_comments.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        # Suppress global git search
        with patch("context_builder.sys_utils.get_git_tracked_files", return_value=[file_path]):
            f_path, line = find_class_definition(file_path, "MyClass", C_FAMILY, cache)
            self.assertIsNone(f_path)
            self.assertIsNone(line)

    def test_resolve_global_definition_ignores_commented_out_globals(self):
        from context_builder.ast_engine import resolve_global_definition
        from context_builder.languages.c_family import C_FAMILY

        code = (
            "/*\n"
            "int commentedGlobal = 42;\n"
            "*/\n"
            "int realGlobal = 100;\n"
        )
        file_path = os.path.join(self.temp_dir.name, "global_comments.cpp")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        with patch("context_builder.sys_utils.get_git_tracked_files", return_value=[file_path]):
            defs1 = resolve_global_definition(file_path, "commentedGlobal", C_FAMILY, cache)
            self.assertEqual(defs1, [])

            defs2 = resolve_global_definition(file_path, "realGlobal", C_FAMILY, cache)
            self.assertEqual(len(defs2), 1)
            self.assertEqual(defs2[0]["code"], "int realGlobal = 100;")

    def test_get_directly_included_files_ignores_commented_out_imports(self):
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.python import PYTHON

        code = (
            "\"\"\"\n"
            "import commented_module\n"
            "\"\"\"\n"
            "import real_module\n"
        )
        file_path = os.path.join(self.temp_dir.name, "import_comments.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        real_module_path = os.path.join(self.temp_dir.name, "real_module.py")
        commented_module_path = os.path.join(self.temp_dir.name, "commented_module.py")
        for p in (real_module_path, commented_module_path):
            with open(p, "w", encoding="utf-8") as f:
                f.write("")

        cache = LRUFileCache(capacity=5)
        with patch("context_builder.sys_utils.get_git_tracked_files", return_value=[real_module_path, commented_module_path]):
            res = get_directly_included_files(file_path, PYTHON, cache)
            self.assertIn(os.path.abspath(real_module_path), res)
            self.assertNotIn(os.path.abspath(commented_module_path), res)

    def test_get_stripped_lines_defensive_null_cache(self):
        from context_builder.ast_engine import _get_stripped_lines
        from context_builder.languages.c_family import C_FAMILY
        from context_builder.cache import get_global_cache

        # Setup C++ file in temp dir
        cpp_file = os.path.join(self.temp_dir.name, "test_null_cache.cpp")
        with open(cpp_file, "w", encoding="utf-8") as f:
            f.write("/* comment */\nx = 1;")

        # When file_cache is None, it should default to get_global_cache()
        global_cache = get_global_cache()
        global_cache.get_lines(cpp_file)

        # Call with file_cache=None
        lines = _get_stripped_lines(None, cpp_file, C_FAMILY)
        # The block comment is stripped, leaving only the line ending
        self.assertIn(lines[0], ("\n", "\r\n"))
        self.assertEqual(lines[1], "x = 1;")

    def test_deeply_nested_ast_traversal_no_recursion_error(self):
        from context_builder.ast_engine import extract_identifiers_with_positions_ast, get_lhs_identifiers

        class FakeNode:
            def __init__(self, node_type, start_line, byte_col, text=None, children=None):
                self.type = node_type
                self.start_point = (start_line - 1, byte_col)
                self.text = text.encode('utf-8') if isinstance(text, str) else text
                self.children = children or []
                self.parent = None
                for child in self.children:
                    child.parent = self

            def child_by_field_name(self, _name):
                return None

        # Build a deeply nested structure of depth 2000
        # If it were recursive, it would throw RecursionError
        curr = FakeNode("identifier", 1, 0, "target_var")
        for _ in range(2000):
            curr = FakeNode("nested_block", 1, 0, children=[curr])

        # Test get_lhs_identifiers
        res_lhs = get_lhs_identifiers(curr)
        self.assertEqual(res_lhs, ["target_var"])

        # Test extract_identifiers_with_positions_ast with mocked parser
        with patch("context_builder.ast_engine.AST_ENGINE") as mock_engine:
            parser = MagicMock()
            parser.parse.return_value = SimpleNamespace(root_node=curr)
            mock_engine.parsers = {".py": parser}
            mock_engine.is_supported.return_value = True

            cache = MagicMock()
            cache.get_bytes.return_value = b"target_var"
            cache.get_lines.return_value = ["target_var"]

            res_pos = extract_identifiers_with_positions_ast("file.py", [1], file_cache=cache)
            self.assertEqual(len(res_pos), 1)
            self.assertEqual(res_pos[0][0], "target_var")

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_ast_engine_functions_graceful_null_root_node(self, mock_engine):
        from context_builder.ast_engine import (
            extract_function_bounds_ast,
            _trace_file_ast_dependencies,
            split_massive_block_ast,
            extract_callees_ast,
            extract_identifiers_with_positions_ast,
            resolve_local_variable_ast,
        )

        # Mock the parser to return a tree where root_node is None
        mock_tree = MagicMock()
        mock_tree.root_node = None

        mock_parser = MagicMock()
        mock_parser.parse.return_value = mock_tree

        mock_engine.parsers = {".py": mock_parser}
        mock_engine.is_supported.return_value = True

        cache = MagicMock()
        cache.get_bytes.return_value = b"some code"
        cache.get_lines.return_value = ["some code"]

        # 1. extract_function_bounds_ast
        bounds = extract_function_bounds_ast("file.py", 1, ".py", file_cache=cache)
        self.assertEqual(bounds, (None, None))

        # 2. _trace_file_ast_dependencies
        callers = []
        _trace_file_ast_dependencies("file.py", "my_func", cache, callers)
        # Should return early without modifying callers list
        self.assertEqual(callers, [])

        # 3. split_massive_block_ast
        res_split = split_massive_block_ast("some lines\n" * 10, "file.py", 5)
        self.assertEqual(res_split[0]["suffix"], " (Truncated)")

        # 4. extract_callees_ast
        callees = extract_callees_ast("file.py", 1, 5, ".py", cache)
        self.assertEqual(callees, set())

        # 5. extract_identifiers_with_positions_ast
        ids = extract_identifiers_with_positions_ast("file.py", [1], file_cache=cache)
        self.assertEqual(ids, [])

        # 6. resolve_local_variable_ast
        loc_res = resolve_local_variable_ast("file.py", "x", 5, cache)
        self.assertEqual(loc_res, (None, None))

    def test_resolve_class_member_definition_dynamic_parent_profile(self):
        from context_builder.ast_engine import resolve_class_member_definition
        from context_builder.languages.python import PYTHON
        from context_builder.cache import LRUFileCache

        child_code = (
            "class Child(Base):\n"
            "    def method(self):\n"
            "        pass\n"
        )
        parent_code = (
            "class Base {\n"
            "    int parent_val;\n"
            "};\n"
        )

        child_file = os.path.join(self.temp_dir.name, "child.py")
        parent_file = os.path.join(self.temp_dir.name, "parent.cpp")
        with open(child_file, "w", encoding="utf-8") as f:
            f.write(child_code)
        with open(parent_file, "w", encoding="utf-8") as f:
            f.write(parent_code)

        cache = LRUFileCache(capacity=5)

        with patch("context_builder.ast_engine.find_class_definition") as mock_find:
            # When looking up "Base" starting from child_file, return parent_file (line 1)
            mock_find.return_value = (parent_file, 1)

            res = resolve_class_member_definition(
                child_file, "Child", "parent_val", PYTHON, cache
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["path"], os.path.relpath(parent_file, os.getcwd()))
            self.assertEqual(res["line"], 2)
            self.assertEqual(res["code"], "int parent_val;")
