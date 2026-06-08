"""Unit tests for CallGraphTracer."""

import unittest
from unittest.mock import MagicMock, patch
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

    @patch("context_builder.graph_tracer.get_lsp_references")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    def test_trace_callers_bfs(self, mock_bounds, mock_lsp):
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
            all_repo_files=["file1.py", "file2.py"],
            ffi_exports=set(),
            cpp_linkages={},
            vm=vm,
            args=args,
        )

        mock_bounds.return_value = (0, 5)
        mock_lsp.return_value = {"file2.py": [{"line": 2, "code": "foo()"}]}

        queue = deque([("file1.py", 1, "foo", 0)])
        processed_spans = set()

        tracer.trace_callers(queue, processed_spans)

        # Assert add_callers was called
        vm.add_callers.assert_called_once()
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
