import os
import unittest
from unittest.mock import patch, MagicMock
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
            "    pass\n"
        )
        
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()
        
        mock_tree.root_node.children = [mock_child]
        mock_child.type = "function_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (6, 0)
        
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True
        
        # We truncate with max_lines=4. The body is larger (7 lines), so it should be semantically truncated.
        res = split_massive_block_ast(source, "test.py", max_lines=4)
        
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
            "    pass\n"
        )
        
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()
        
        mock_tree.root_node.children = [mock_child]
        mock_child.type = "function_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (5, 0)
        
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".py": mock_parser}
        mock_ast_engine.is_supported.return_value = True
        
        res = split_massive_block_ast(source, "test.py", max_lines=3)
        
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
            "    }\n"
        )
        
        mock_parser = MagicMock()
        mock_tree = MagicMock()
        mock_child = MagicMock()
        
        mock_tree.root_node.children = [mock_child]
        mock_child.type = "method_definition"
        mock_child.start_point = (0, 0)
        mock_child.end_point = (2, 0)
        
        mock_parser.parse.return_value = mock_tree
        mock_ast_engine.parsers = {".js": mock_parser}
        mock_ast_engine.is_supported.return_value = True
        
        res = split_massive_block_ast(source, "test.js", max_lines=2)
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
