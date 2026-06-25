# pylint: disable=import-outside-toplevel,too-few-public-methods,too-many-public-methods
# pylint: disable=consider-using-from-import,missing-function-docstring

"""Unit tests covering dotted imports, UTF-8 BOM, config dictionary exceptions,
and FFI None capture guards.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from context_builder.config import CONFIG, reset_config


class TestSafetyRefactors(unittest.TestCase):
    """Test suite for security, correctness, and reliability refactors."""

    def setUp(self):
        reset_config()

    def tearDown(self):
        reset_config()

    def test_dotted_module_import(self):
        """Verify that dynamic imports can handle dotted module names using importlib."""
        import context_builder.ast_engine as ast_engine

        binding_obj = object()
        mock_module = SimpleNamespace(dummy=lambda: binding_obj)
        mock_lang = object()

        class FakeParser:
            """Minimal successful parser used to isolate import behavior."""

            def __init__(self):
                self.language = None

            def set_language(self, language):
                self.language = language

        mock_tree_sitter = SimpleNamespace(
            Language=MagicMock(return_value=mock_lang),
            Parser=FakeParser,
        )

        engine = ast_engine.AstEngine()
        orig_bindings = CONFIG["bindings"].copy()
        try:
            CONFIG["bindings"] = {".dummy": ("pkg.sub", "dummy")}
            with patch.object(
                ast_engine.importlib,
                "import_module",
                return_value=mock_module,
            ) as mock_import_module, patch.object(
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
            mock_import_module.assert_called_with("pkg.sub")
            self.assertIn(".dummy", engine.languages)
        finally:
            CONFIG["bindings"] = orig_bindings

    def test_utf8_bom_support(self):
        """Verify that UTF-8 BOM signature is successfully stripped from config files."""
        from context_builder.config import load_json_with_comments

        jsonc_content = "\ufeff// UTF-8 BOM comment\n{\n  \"format\": \"md\"\n}\n"

        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            f.write(jsonc_content.encode("utf-8"))
            temp_path = f.name

        try:
            cfg = load_json_with_comments(temp_path)
            self.assertEqual(cfg["format"], "md")
        finally:
            os.remove(temp_path)

    def test_config_non_dict_structure(self):
        """Verify that a non-dict config structure causes a graceful error and exits."""
        from context_builder.cli import main

        json_content = '["invalid", "list"]'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json_content)
            temp_path = f.name

        try:
            with patch("sys.argv", ["smart_diff_context_builder.py", "--config", temp_path]):
                with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                    with self.assertRaises(SystemExit):
                        main()
                    mock_exit.assert_called_with(1)
        finally:
            os.remove(temp_path)

    def test_config_non_dict_override(self):
        """Verify passing non-dict override to dict config key raises an error and exits.
        """
        from context_builder.cli import main

        json_content = '{"lang_map": "not_a_dictionary"}'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(json_content)
            temp_path = f.name

        try:
            with patch("sys.argv", ["smart_diff_context_builder.py", "--config", temp_path]):
                with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                    with self.assertRaises(SystemExit):
                        main()
                    mock_exit.assert_called_with(1)
        finally:
            os.remove(temp_path)

    def test_cli_non_dict_override(self):
        """Verify that passing a non-dict CLI override to a dict key raises an error and exits."""
        from context_builder.cli import main

        with patch("sys.argv", ["smart_diff_context_builder.py", "--lang-map", '"not_a_dict"']):
            with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                with self.assertRaises(SystemExit):
                    main()
                mock_exit.assert_called_with(1)

    def test_ffi_optional_capture_none(self):
        """Verify that FFI optional capture groups that return None do not crash and are skipped."""
        from context_builder.cache import get_global_cache
        from context_builder.preprocessor import build_ffi_registry

        orig_patterns = CONFIG.get("ffi_patterns")
        # Group 1 is optional: (none_group)?my_symbol
        CONFIG["ffi_patterns"] = [r"(none_group)?(my_symbol)"]

        with tempfile.TemporaryDirectory() as temp_dir_path:
            file_path = os.path.join(temp_dir_path, "source.c")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("my_symbol\n")

            cache = get_global_cache()
            cache.get_content(file_path)

            try:
                symbols = build_ffi_registry([file_path], file_cache=cache)
                self.assertNotIn(None, symbols)
                self.assertNotIn("my_symbol", symbols)
            finally:
                if orig_patterns is not None:
                    CONFIG["ffi_patterns"] = orig_patterns

    def test_fallback_strip_guards(self):
        """Verify that _fallback_strip returns [] when lines is None or empty."""
        from context_builder.ast_engine import _fallback_strip

        profile = MagicMock()
        self.assertEqual(_fallback_strip(None, profile), [])
        self.assertEqual(_fallback_strip([], profile), [])

    def test_fallback_strip_preserves_line_count_when_comments_removed(self):
        """Verify _fallback_strip keeps line alignment if comments are removed."""
        from context_builder.ast_engine import _fallback_strip

        profile = MagicMock()
        profile.strip_block_comments.return_value = "int before;\nint after;\n"

        lines = [
            "int before; /* comment */\n",
            "/* full line comment */\n",
            "int after;\n",
        ]
        stripped = _fallback_strip(lines, profile)

        self.assertEqual(len(stripped), len(lines))
        self.assertEqual(stripped[0], "int before;\n")
        self.assertEqual(stripped[1], "\n")
        self.assertEqual(stripped[2], "int after;\n")

    def test_fallback_strip_uses_best_line_alignment(self):
        """Verify substring matches do not greedily steal later stripped code."""
        from context_builder.ast_engine import _fallback_strip

        profile = MagicMock()
        profile.strip_block_comments.return_value = "foo = 1;\n"

        lines = [
            "bar(foo)\n",
            "foo = 1; /* comment */\n",
        ]
        stripped = _fallback_strip(lines, profile)

        self.assertEqual(stripped, ["\n", "foo = 1;\n"])

    def test_fallback_strip_substring_alignment_bypasses_difflib(self):
        """Verify common substring alignment avoids expensive fuzzy matching."""
        from context_builder.ast_engine import _fallback_strip

        profile = MagicMock()
        profile.strip_block_comments.return_value = "foo = 1;\n"

        with patch("context_builder.ast_engine.difflib.SequenceMatcher") as mock_matcher:
            stripped = _fallback_strip(["foo = 1; /* comment */\n"], profile)

        self.assertEqual(stripped, ["foo = 1;\n"])
        mock_matcher.assert_not_called()

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_ast_parse_helpers_return_early_without_source_bytes(self, mock_engine):
        """Verify AST helpers do not parse None or empty source bytes."""
        from context_builder.ast_engine import (
            _trace_file_ast_dependencies,
            extract_callees_ast,
            extract_function_bounds_ast,
        )

        parser = MagicMock()
        mock_engine.parsers = {".py": parser}
        mock_engine.is_supported.return_value = True

        for missing_source in (None, b""):
            file_cache = MagicMock()
            file_cache.get_bytes.return_value = missing_source
            callers = {}

            self.assertEqual(
                extract_function_bounds_ast("dummy.py", 1, ".py", file_cache),
                (None, None),
            )
            _trace_file_ast_dependencies("dummy.py", "target", file_cache, callers)
            self.assertEqual(callers, {})
            self.assertEqual(
                extract_callees_ast("dummy.py", 1, 5, ".py", file_cache),
                set(),
            )

        parser.parse.assert_not_called()

    def test_get_stripped_lines_guards(self):
        """Verify that _get_stripped_lines returns [] when file_cache returns None."""
        from context_builder.ast_engine import _get_stripped_lines

        file_cache = MagicMock()
        file_cache.get_lines.return_value = None
        # Remove get_stripped_lines attribute if present to test get_lines fallback path
        if hasattr(file_cache, "get_stripped_lines"):
            del file_cache.get_stripped_lines

        profile = MagicMock()
        self.assertEqual(_get_stripped_lines(file_cache, "dummy.py", profile), [])

        # Test path where get_stripped_lines is present but returns Mock/MagicMock
        file_cache_with_stripped = MagicMock()
        file_cache_with_stripped.get_stripped_lines.return_value = MagicMock()
        file_cache_with_stripped.get_lines.return_value = None
        self.assertEqual(_get_stripped_lines(file_cache_with_stripped, "dummy.py", profile), [])

    @patch("context_builder.graph_tracer.resolve_variable_definition", return_value=None)
    def test_graph_tracer_resolve_definition_none_guard(self, _mock_resolve):
        """Verify that graph tracer trace_data_flow does not crash when
        variable definition resolves to None.
        """
        from context_builder.graph_tracer import CallGraphTracer

        vm = MagicMock()
        tracer = CallGraphTracer(
            file_cache=MagicMock(),
            all_repo_files=["dummy.py"],
            ffi_exports=set(),
            cpp_linkages={},
            vm=vm,
            args=None
        )
        # Should not crash with AttributeError
        tracer.trace_data_flow({"dummy.py": [10]})

    @patch("context_builder.ast_engine.AST_ENGINE")
    @patch("context_builder.ast_engine.extract_function_bounds", return_value=(0, 10))
    def test_resolve_local_variable_ast_none_lines(self, _mock_bounds, mock_engine):
        """Verify resolve_local_variable_ast does not crash when lines is None."""
        from context_builder.ast_engine import resolve_local_variable_ast

        file_cache = MagicMock()
        file_cache.get_bytes.return_value = b"x = 5"
        file_cache.get_lines.return_value = None

        node = MagicMock()
        node.type = 'assignment_expression'
        node.start_point = (2, 0)

        mock_tree = SimpleNamespace(root_node=node)
        mock_parser = MagicMock()
        mock_parser.parse.return_value = mock_tree
        mock_engine.parsers = {".py": mock_parser}
        mock_engine.is_supported.return_value = True

        with patch("context_builder.ast_engine.tree_sitter.Query", side_effect=Exception):
            with patch("context_builder.ast_engine.get_lhs_identifiers", return_value={"x"}):
                res_line, res_code = resolve_local_variable_ast(
                    "dummy.py", "x", 5, file_cache=file_cache
                )
                self.assertIsNone(res_line)
                self.assertIsNone(res_code)

    def test_resolve_class_member_definition_none_lines(self):
        """Verify resolve_class_member_definition handles None lines from cache gracefully."""
        from context_builder.ast_engine import resolve_class_member_definition

        file_cache = MagicMock()
        file_cache.get_lines.return_value = None
        profile = MagicMock()

        with patch("context_builder.ast_engine.get_class_members", return_value=[("my_var", 2)]):
            res = resolve_class_member_definition(
                "dummy.py", "MyClass", "my_var", profile, file_cache
            )
            self.assertIsNotNone(res)
            self.assertEqual(res["code"], "")

    @patch("context_builder.ast_engine.AST_ENGINE")
    @patch("context_builder.ast_engine.tree_sitter")
    def test_trace_file_ast_dependencies_none_lines(self, mock_tree_sitter, mock_engine):
        """Verify _trace_file_ast_dependencies returns early without crashing
        when get_lines returns None.
        """
        from context_builder.ast_engine import _trace_file_ast_dependencies

        file_cache = MagicMock()
        file_cache.get_bytes.return_value = b"some code"
        file_cache.get_lines.return_value = None

        node = MagicMock()
        mock_tree = SimpleNamespace(root_node=node)
        mock_parser = MagicMock()
        mock_parser.parse.return_value = mock_tree
        mock_engine.parsers = {".py": mock_parser}
        mock_engine.is_supported.return_value = True

        mock_query = MagicMock()
        # Mock some capture to try getting the lines
        capture_node = MagicMock()
        capture_node.parent = MagicMock()
        capture_node.parent.start_byte = 0
        capture_node.parent.end_byte = 5
        mock_query.captures.return_value = [(capture_node, None)]
        mock_tree_sitter.Query.return_value = mock_query

        mock_engine.languages = {".py": MagicMock()}

        callers = {}
        _trace_file_ast_dependencies(
            "dummy.py", "my_func", file_cache, callers
        )
        self.assertEqual(callers, {})

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_ext_lowercase_normalization(self, mock_engine):
        """Verify ext is normalized to lowercase in bounds and callee extraction."""
        from context_builder.ast_engine import (
            extract_function_bounds_ast,
            extract_callees_ast,
        )

        file_cache = MagicMock()
        file_cache.get_bytes.return_value = b"def foo():\n    pass"

        mock_parser = MagicMock()
        mock_parser.parse.return_value = None
        mock_engine.parsers = {".py": mock_parser}

        res_line, _ = extract_function_bounds_ast(
            "dummy.py", 1, ".PY", file_cache=file_cache
        )
        self.assertIsNone(res_line)

        res_callees = extract_callees_ast(
            "dummy.py", 1, 2, ".PY", file_cache=file_cache
        )
        self.assertEqual(res_callees, set())

    @patch("context_builder.ast_engine.get_language_profile", return_value=None)
    def test_profile_none_guards(self, _mock_profile):
        """Verify functions handle None language profile gracefully."""
        from context_builder.ast_engine import (
            extract_identifiers_with_positions_regex,
            resolve_local_variable_ast,
            find_class_definition,
            resolve_class_member_definition,
            resolve_global_definition,
        )
        from context_builder.lsp_client import (
            get_lsp_references,
            get_lsp_definition,
            get_lsp_type_definition,
        )

        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["line1"]
        file_cache.get_bytes.return_value = b"x = 5"

        self.assertEqual(
            extract_identifiers_with_positions_regex(
                "dummy.py", [1], file_cache=file_cache
            ),
            [],
        )
        self.assertEqual(
            resolve_local_variable_ast("dummy.py", "x", 1, file_cache=file_cache),
            (None, None),
        )

        with patch(
            "context_builder.ast_engine.ripgrep_filter", return_value=["candidate.py"]
        ):
            with patch("os.path.exists", return_value=True):
                self.assertEqual(
                    find_class_definition(
                        "dummy.py", "MyClass", None, file_cache=file_cache
                    ),
                    (None, None),
                )

        with patch(
            "context_builder.ast_engine.get_parent_classes", return_value=["ParentClass"]
        ):
            with patch(
                "context_builder.ast_engine.find_class_definition",
                return_value=("parent.py", 1),
            ):
                self.assertIsNone(
                    resolve_class_member_definition(
                        "dummy.py", "MyClass", "var", None, file_cache=file_cache
                    )
                )

        with patch("os.path.exists", return_value=True):
            self.assertEqual(
                resolve_global_definition(
                    "dummy.py", "var", None, file_cache=file_cache
                ),
                [],
            )

        with patch("context_builder.lsp_client.USE_LSP", True):
            self.assertIsNone(
                get_lsp_references(
                    "dummy.py",
                    1,
                    "x",
                    timeout=1,
                    max_depth=1,
                    disable_pruning=False,
                    file_cache=file_cache,
                )
            )

        with patch("context_builder.lsp_client.USE_LSP", True):
            self.assertEqual(get_lsp_definition("dummy.py", 1, 0, timeout=1), [])

        with patch("context_builder.lsp_client.USE_LSP", True):
            self.assertEqual(get_lsp_type_definition("dummy.py", 1, 0, timeout=1), [])

    def test_get_stripped_lines_returns_empty_list_when_none(self):
        """Verify _get_stripped_lines returns [] if file_cache.get_stripped_lines returns None."""
        from context_builder.ast_engine import _get_stripped_lines

        file_cache = MagicMock()
        file_cache.get_stripped_lines.return_value = None
        profile = MagicMock()

        res = _get_stripped_lines(file_cache, "dummy.py", profile)
        self.assertEqual(res, [])

    def test_unsupported_extensions_ast_graceful(self):
        """Verify extract_function_bounds_ast and extract_callees_ast handle
        unsupported file extensions gracefully without raising a KeyError.
        """
        from context_builder.ast_engine import (
            extract_function_bounds_ast,
            extract_callees_ast,
        )

        file_cache = MagicMock()
        file_cache.get_bytes.return_value = b"some content"

        res_bounds = extract_function_bounds_ast(
            "dummy.unsupported", 1, ".unsupported", file_cache=file_cache
        )
        self.assertEqual(res_bounds, (None, None))

        res_callees = extract_callees_ast(
            "dummy.unsupported", 1, 10, ".unsupported", file_cache=file_cache
        )
        self.assertEqual(res_callees, set())

    def test_get_directly_included_files_inline_comments(self):
        """Verify get_directly_included_files strips comments and parses imports correctly."""
        from context_builder.ast_engine import get_directly_included_files
        from context_builder.languages.python import PythonProfile

        profile = PythonProfile()
        file_cache = MagicMock()
        file_cache.get_directly_included_files_cache = {}
        file_cache.get_stripped_lines.return_value = [
            "import os # this is a comment",
            "from path import name # inline comment",
            "# import ignored_module",
        ]
        file_cache.get_lines.return_value = file_cache.get_stripped_lines.return_value

        expected_files = {
            os.path.abspath("os.py"),
            os.path.abspath("path.py"),
            os.path.abspath("active_module.js"),
        }

        def mock_exists(p):
            return os.path.abspath(p) in expected_files

        with patch("context_builder.ast_engine.os.path.exists", side_effect=mock_exists), \
             patch("context_builder.ast_engine.os.path.isfile", side_effect=mock_exists), \
             patch(
                 "context_builder.sys_utils.get_git_tracked_files",
                 return_value=["os.py", "path.py"]
             ):
            res = get_directly_included_files("dummy.py", profile, file_cache)
            self.assertEqual(len(res), 2)
            self.assertTrue(any(f.endswith("os.py") for f in res))
            self.assertTrue(any(f.endswith("path.py") for f in res))

        from context_builder.languages.javascript import JavaScriptProfile
        js_profile = JavaScriptProfile()
        file_cache_js = MagicMock()
        file_cache_js.get_directly_included_files_cache = {}
        file_cache_js.get_stripped_lines.return_value = [
            "// const x = require('commented_out')",
            "const y = require('active_module') // active import",
        ]
        file_cache_js.get_lines.return_value = (
            file_cache_js.get_stripped_lines.return_value
        )
        with patch("context_builder.ast_engine.os.path.exists", side_effect=mock_exists), \
             patch("context_builder.ast_engine.os.path.isfile", side_effect=mock_exists), \
             patch(
                 "context_builder.sys_utils.get_git_tracked_files",
                 return_value=["active_module.js"]
             ):
            res_js = get_directly_included_files("dummy.js", js_profile, file_cache_js)
            self.assertEqual(len(res_js), 1)
            self.assertTrue(any(f.endswith("active_module.js") for f in res_js))

    def test_resolve_variable_definition_regex_fallback_none_profile(self):
        """Verify resolve_variable_definition_regex_fallback handles None profile gracefully."""
        from context_builder.ast_engine import resolve_variable_definition_regex_fallback
        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["x = 5"]
        res = resolve_variable_definition_regex_fallback("dummy.py", "x", 1, file_cache, None)
        self.assertEqual(res, {"resolved_type": "none", "definitions": []})

    @patch("context_builder.ast_engine.AST_ENGINE")
    def test_extract_identifiers_with_positions_ast_generalized_members(
        self, mock_engine
    ):
        """Verify extract_identifiers_with_positions_ast filters out member property/field
        identifiers for multiple languages.
        """
        from context_builder.ast_engine import extract_identifiers_with_positions_ast

        file_cache = MagicMock()
        file_cache.get_bytes.return_value = b"some bytes"
        file_cache.get_lines.return_value = ["obj.prop"]

        node_prop = MagicMock()
        node_prop.type = 'identifier'
        node_prop.start_point = (0, 4)
        node_prop.text = b"prop"

        parent = MagicMock()
        parent.type = 'attribute'
        parent.child_by_field_name.return_value = node_prop
        node_prop.parent = parent

        root = MagicMock()
        root.children = [parent, node_prop]

        mock_tree = SimpleNamespace(root_node=root)
        mock_parser = MagicMock()
        mock_parser.parse.return_value = mock_tree
        mock_engine.parsers = {".py": mock_parser}
        mock_engine.is_supported.return_value = True

        res = extract_identifiers_with_positions_ast("dummy.py", [1], file_cache=file_cache)
        self.assertEqual(res, [])

        parent.child_by_field_name.return_value = None
        res_valid = extract_identifiers_with_positions_ast("dummy.py", [1], file_cache=file_cache)
        self.assertEqual(len(res_valid), 1)
        self.assertEqual(res_valid[0][0], "prop")

    def test_resolve_global_definition_original_code(self):
        """Verify resolve_global_definition uses the original code from file cache."""
        from context_builder.ast_engine import resolve_global_definition
        from context_builder.languages.python import PythonProfile

        profile = PythonProfile()
        file_cache = MagicMock()
        file_cache.get_stripped_lines.return_value = ["GLOBAL_VAR = 10"]
        file_cache.get_lines.return_value = ["GLOBAL_VAR = 10  # original comment"]

        with patch("context_builder.ast_engine.build_scopes") as mock_build_scopes:
            from context_builder.ast_engine import RegexScope
            g_scope = RegexScope(start_line=1)
            g_scope.end_line = 2
            mock_build_scopes.return_value = (g_scope, [g_scope])

            res = resolve_global_definition("dummy.py", "GLOBAL_VAR", profile, file_cache)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["code"], "GLOBAL_VAR = 10  # original comment")
