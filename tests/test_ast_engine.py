# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,protected-access
# pylint: disable=redefined-outer-name,reimported,too-many-lines,too-many-public-methods
# pylint: disable=consider-using-with,line-too-long,consider-using-from-import
# pylint: disable=too-few-public-methods

import os
import unittest
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
    find_callee_definition
)

class TestAstEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = LRUFileCache(capacity=5)

    def tearDown(self):
        self.temp_dir.cleanup()

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
        # Verify that a long sequence of spaces after type does not cause catastrophic backtracking
        import re
        import time

        func_name = "my_func"
        lead_b = r'\b'
        escaped_name = re.escape(func_name)
        pattern = re.compile(
            r'^\s*(?:[A-Za-z0-9_<>:]+(?:\s+|[*&]+)[\s*&]*)?' + lead_b + escaped_name + r'\s*\('
        )

        malicious_input = "void " + " " * 500

        start_time = time.perf_counter()
        match = pattern.search(malicious_input)
        duration = time.perf_counter() - start_time

        self.assertFalse(match)
        # Should finish extremely fast (under 100 milliseconds)
        self.assertLess(duration, 0.1)

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
        mock_lang.query.return_value = mock_query
        mock_ast_engine.languages = {".py": mock_lang}

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
        # Raise an exception (e.g. tree-sitter QuerySyntaxError or similar) when compiling query
        mock_lang.query.side_effect = Exception("Query syntax error")
        mock_ast_engine.languages = {".py": mock_lang}

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
        mock_lang.query.return_value = mock_query
        mock_ast_engine.languages = {".cpp": mock_lang}

        mock_cache = MagicMock()
        mock_cache.get_bytes.return_value = b"void operator+();"

        with patch(
            "context_builder.ast_engine.ripgrep_filter",
            return_value=["file.cpp"],
        ):
            trace_lexical_dependencies_ast("operator+", ["file.cpp"], file_cache=mock_cache)

        # Verify that AST_ENGINE.languages[".cpp"].query was called with double-escaped operator\\+
        mock_lang.query.assert_called_once()
        query_str = mock_lang.query.call_args[0][0]
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
        with patch.dict(AST_ENGINE.languages, {".cpp": mock_lang}), \
             patch.dict(AST_ENGINE.parsers, {".cpp": mock_parser}), \
             patch.object(AST_ENGINE, "is_supported", return_value=True):
            trace_lexical_dependencies_ast("foo", [file_path], file_cache=self.cache)
            mock_lang.query.assert_called_once()
            query_arg = mock_lang.query.call_args[0][0]
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
        """Verify JS/TS definitions (arrow functions, shorthands) are not treated as callers."""
        js_file = os.path.join(self.temp_dir.name, "app.js")
        content = (
            "const myFunc = () => {\n"
            "  console.log('arrow');\n"
            "};\n"
            "class Controller {\n"
            "  async myFunc(arg) {\n"
            "    console.log(arg);\n"
            "  }\n"
            "}\n"
            "function myFunc() {\n"
            "  return 42;\n"
            "}\n"
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
        # Only the actual call on line 12 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 12)
        self.assertEqual(occurrences[0]["code"], "myFunc(); // This is the actual caller")

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
        """Verify that class, struct, union, enum, and namespace are treated as definitions in C++."""
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
            "}\n"
            "void test() {\n"
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
        # Only the actual call on line 15 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 15)
        self.assertEqual(occurrences[0]["code"], "myFunc(); // Actual call")

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
        # Only the actual call on line 9 should be matched
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0]["line"], 9)
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
