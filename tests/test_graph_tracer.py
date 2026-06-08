"""Unit tests for CallGraphTracer."""

import unittest
from unittest.mock import MagicMock, patch, ANY
from collections import deque
from context_builder.graph_tracer import CallGraphTracer


class TestCallGraphTracer(unittest.TestCase):
    """Unit tests for CallGraphTracer class."""

    def test_tracer_initialization(self):
        """Test that CallGraphTracer initializes all attributes correctly."""
        file_cache = MagicMock()
        all_repo_files = ["file1.py"]
        ffi_exports = {"foo"}
        cpp_linkages = {}
        vm = MagicMock()
        args = MagicMock()

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=all_repo_files,
            ffi_exports=ffi_exports,
            cpp_linkages=cpp_linkages,
            vm=vm,
            args=args,
        )

        self.assertEqual(tracer.file_cache, file_cache)
        self.assertEqual(tracer.all_repo_files, all_repo_files)
        self.assertEqual(tracer.ffi_exports, ffi_exports)
        self.assertEqual(tracer.cpp_linkages, cpp_linkages)
        self.assertEqual(tracer.vm, vm)
        self.assertEqual(tracer.args, args)

    @patch("context_builder.graph_tracer.is_in_repo")
    @patch("context_builder.graph_tracer.get_lsp_references")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    def test_trace_callers_bfs(self, mock_bounds, mock_lsp, mock_is_in_repo):
        """Test trace_callers method with mocked references."""
        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["def caller_func():\n"] * 10
        vm = MagicMock()
        vm.local_callers = {}
        args = MagicMock()
        args.caller_depth = 2
        args.lsp_timeout = 5
        args.max_interface_depth = 15
        args.disable_pruning = False
        args.skip_ffi = True
        args.skip_macro_expansion = True

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=["file1.py", "file2.py", "file3.py"],
            ffi_exports=set(),
            cpp_linkages={},
            vm=vm,
            args=args,
        )

        mock_is_in_repo.return_value = True
        mock_bounds.return_value = (0, 5)

        def mock_lsp_side_effect(curr_file, *args_lsp, **kwargs):
            if curr_file == "file1.py":
                return {"file2.py": [{"line": 2, "code": "foo()"}]}
            if curr_file == "file2.py":
                return {"file3.py": [{"line": 3, "code": "bar()"}]}
            return {}
        mock_lsp.side_effect = mock_lsp_side_effect

        queue = deque([("file1.py", 1, "foo", 0)])
        processed_spans = set()

        tracer.trace_callers(queue, processed_spans)

        # Assert add_callers was called twice (for depth 0 and depth 1)
        self.assertEqual(vm.add_callers.call_count, 2)
        # Assert queue is empty (processed)
        self.assertEqual(len(queue), 0)

    @patch("context_builder.graph_tracer.extract_callees")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.find_callee_definition")
    @patch("context_builder.graph_tracer.split_massive_block_ast")
    def test_trace_callees_bfs(self, mock_split, mock_find_def, mock_bounds, mock_callees):
        """Test trace_callees method with mocked callees."""
        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["def callee():\n"] * 10
        vm = MagicMock()
        vm.local_callees = []
        args = MagicMock()
        args.callee_depth = 2
        args.max_lines = 100

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=["file1.py", "file2.py"],
            ffi_exports=set(),
            cpp_linkages={},
            vm=vm,
            args=args,
        )

        mock_bounds.return_value = (0, 5)
        mock_callees.return_value = ["bar"]
        mock_find_def.return_value = ("file2.py", 1)
        mock_split.return_value = [{"suffix": "", "text": "def bar():\n    pass"}]

        callee_queue = deque([("file1.py", 1, "foo", 0)])
        processed_spans = set()

        tracer.trace_callees(callee_queue, processed_spans)

        # Assert local_callees has been updated
        self.assertEqual(len(vm.local_callees), 1)
        self.assertEqual(vm.local_callees[0]["function_name"], "bar")

    @patch("context_builder.graph_tracer.get_lsp_references")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.is_in_repo")
    @patch("context_builder.graph_tracer.trace_macro_expansion")
    @patch("context_builder.graph_tracer.trace_lexical_dependencies_ast")
    @patch("context_builder.graph_tracer.AST_ENGINE.is_supported")
    def test_case_insensitive_extension_checks(
        self, mock_is_supported, mock_ast, mock_macro, mock_is_in_repo, mock_bounds, mock_lsp
    ):
        """Test that upper/mixed case extensions are handled correctly."""
        file_cache = MagicMock()
        # Mock lines starting with a comment or string we want to strip
        file_cache.get_lines.return_value = [
            "# comment\n",
            "def foo():\n",
            "    pass\n"
        ]
        vm = MagicMock()
        vm.local_callers = {}
        args = MagicMock()
        args.caller_depth = 2  # Set to 2 so FILE2.CPP at depth 1 is traversed
        args.lsp_timeout = 5
        args.max_interface_depth = 15
        args.disable_pruning = False
        args.skip_ffi = True
        args.skip_macro_expansion = False  # Enable macro expansion to test .CPP extension

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=["FILE1.PY", "FILE2.CPP"],
            ffi_exports=set(),
            cpp_linkages={"FILE2.CPP": {}},
            vm=vm,
            args=args,
        )

        mock_is_supported.return_value = True
        mock_is_in_repo.return_value = True
        mock_bounds.return_value = (0, 3)
        # Mock LSP references returning None to force AST fallback
        mock_lsp.return_value = None
        mock_ast.return_value = {"FILE2.CPP": [{"line": 1, "code": "foo()"}]}
        mock_macro.return_value = {}

        queue = deque([("FILE1.PY", 1, "foo", 0)])
        processed_spans = set()

        tracer.trace_callers(queue, processed_spans)

        # 1. Verify AST_ENGINE.is_supported was checked with lowercase extension
        # since FILE2.CPP is a C++ file supported by AST, and we fell back to AST.
        self.assertEqual(mock_ast.call_count, 2)

        # 2. Verify trace_macro_expansion was called for FILE2.CPP despite uppercase extension
        mock_macro.assert_called_once()

    @patch("context_builder.graph_tracer.extract_callees")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.find_callee_definition")
    @patch("context_builder.graph_tracer.split_massive_block_ast")
    def test_trace_callees_bfs_none_max_lines(
        self, mock_split, mock_find_def, mock_bounds, mock_callees
    ):
        """Test trace_callees method when max_lines is None."""
        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["def callee():\n"] * 10
        vm = MagicMock()
        vm.local_callees = []
        args = MagicMock()
        args.callee_depth = 2
        args.max_lines = None

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=["file1.py", "file2.py"],
            ffi_exports=set(),
            cpp_linkages={},
            vm=vm,
            args=args,
        )

        mock_bounds.return_value = (0, 5)
        mock_callees.return_value = ["bar"]
        mock_find_def.return_value = ("file2.py", 1)
        mock_split.return_value = [{"suffix": "", "text": "def bar():\n    pass"}]

        callee_queue = deque([("file1.py", 1, "foo", 0)])
        processed_spans = set()

        tracer.trace_callees(callee_queue, processed_spans)

        # Assert local_callees has been updated and mock_split was called with fallback limit 900
        self.assertEqual(len(vm.local_callees), 1)
        mock_split.assert_called_once_with(ANY, ANY, 900)
