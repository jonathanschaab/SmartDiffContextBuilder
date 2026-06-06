import os
import unittest
import tempfile
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

    def test_extract_function_bounds_defensive(self):
        start, end = extract_function_bounds("some_file.py", 0, file_cache=self.cache)
        self.assertIsNone(start)
        self.assertIsNone(end)

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

    @unittest.mock.patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_callees_node_text_missing(self, mock_ast_engine):
        mock_parser = unittest.mock.MagicMock()
        mock_tree = unittest.mock.MagicMock()
        mock_node = unittest.mock.MagicMock()
        
        # Delete text attribute from mock node to simulate older py-tree-sitter versions
        del mock_node.text
        
        mock_tree.root_node.children = [mock_node]
        mock_node.start_point = (1, 0)
        mock_node.end_point = (2, 0)
        
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True
        
        mock_lang = unittest.mock.MagicMock()
        mock_query = unittest.mock.MagicMock()
        mock_query.captures.return_value = [(mock_node, "id")]
        mock_lang.query.return_value = mock_query
        mock_ast_engine.languages = {".py": mock_lang}
        
        from context_builder.ast_engine import extract_callees_ast
        
        mock_cache = unittest.mock.MagicMock()
        mock_cache.get_bytes.return_value = b"def foo():\n    bar()\n"
        
        with self.assertRaises(AttributeError) as ctx:
            extract_callees_ast("dummy.py", 1, 3, ".py", mock_cache)
            
        self.assertIn("Node object lacks '.text' attribute", str(ctx.exception))
