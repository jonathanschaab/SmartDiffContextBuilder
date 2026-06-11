# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,unused-argument,consider-using-with
# pylint: disable=import-outside-toplevel,protected-access,too-few-public-methods

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

    def test_extract_function_name_compiler_specifiers(self):
        """Test extract_function_name ignores compiler specifiers and C++ keywords."""
        from context_builder.graph_tracer import extract_function_name

        # pure specifiers
        self.assertEqual(
            extract_function_name("__attribute__((always_inline))", 1, 10),
            "block_lines_1_10",
        )
        self.assertEqual(
            extract_function_name("__declspec(dllexport)", 2, 20),
            "block_lines_2_20",
        )

        # specifiers followed by C-style function definitions
        self.assertEqual(
            extract_function_name("__declspec(dllexport) void foo()", 1, 10),
            "foo",
        )
        self.assertEqual(
            extract_function_name("alignas(16) int bar()", 1, 10),
            "bar",
        )
        self.assertEqual(
            extract_function_name("noexcept(true) void baz()", 1, 10),
            "baz",
        )
        self.assertEqual(
            extract_function_name("throw(exception) void qux()", 1, 10),
            "qux",
        )
        self.assertEqual(
            extract_function_name("typeid(obj) void fn()", 1, 10),
            "fn",
        )
        self.assertEqual(
            extract_function_name("typeid(int)", 1, 10),
            "block_lines_1_10",
        )

    def test_tracer_defensive_initialization(self):
        """Test that CallGraphTracer defaults None collections to empty ones."""
        file_cache = MagicMock()
        vm = MagicMock()
        args = MagicMock()

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=None,
            ffi_exports=None,
            cpp_linkages=None,
            vm=vm,
            args=args,
        )

        self.assertEqual(tracer.all_repo_files, [])
        self.assertEqual(tracer.ffi_exports, set())
        self.assertEqual(tracer.cpp_linkages, {})

    def test_tracer_defensive_args_missing_or_none(self):
        """Test that missing/None args default safely."""
        file_cache = MagicMock()
        vm = MagicMock()

        # Test case: args is None
        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=None,
            ffi_exports=None,
            cpp_linkages=None,
            vm=vm,
            args=None,
        )

        # Should not raise AttributeError when tracing callers/callees
        queue = deque()
        tracer.trace_callers(queue, set())  # Runs fine, returns immediately
        callee_queue = deque()
        tracer.trace_callees(callee_queue, set())  # Runs fine, returns immediately

        # Test case: args has attributes but they are None or missing
        class PartialArgs:
            pass

        args = PartialArgs()
        args.caller_depth = None
        args.callee_depth = None
        args.max_lines = None

        tracer_partial = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=None,
            ffi_exports=None,
            cpp_linkages=None,
            vm=vm,
            args=args,
        )

        tracer_partial.trace_callers(queue, set())
        tracer_partial.trace_callees(callee_queue, set())

    @patch("context_builder.graph_tracer.get_lsp_references")
    def test_trace_callers_none_caller_depth(self, mock_lsp):
        """Test trace_callers defaults None or missing caller_depth to 0."""
        file_cache = MagicMock()
        vm = MagicMock()
        args = MagicMock()
        args.caller_depth = None

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=None,
            ffi_exports=None,
            cpp_linkages=None,
            vm=vm,
            args=args,
        )

        queue = deque([("file1.py", 1, "foo", 0)])
        tracer.trace_callers(queue, set())

        # If caller_depth defaulted to 0, get_lsp_references should not be called
        mock_lsp.assert_not_called()

    @patch("context_builder.graph_tracer.extract_function_bounds")
    def test_trace_callees_none_callee_depth(self, mock_bounds):
        """Test trace_callees defaults None or missing callee_depth to 0."""
        file_cache = MagicMock()
        vm = MagicMock()
        args = MagicMock()
        args.callee_depth = None

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=None,
            ffi_exports=None,
            cpp_linkages=None,
            vm=vm,
            args=args,
        )

        callee_queue = deque([("file1.py", 1, "foo", 0)])
        tracer.trace_callees(callee_queue, set())

        # If callee_depth defaulted to 0, extract_function_bounds should not be called
        mock_bounds.assert_not_called()

    @patch("context_builder.graph_tracer.trace_ffi_callers")
    @patch("context_builder.graph_tracer.trace_macro_expansion")
    @patch("context_builder.graph_tracer.get_lsp_references")
    def test_trace_callers_defensive_getattr_fallbacks(
        self, mock_lsp, mock_macro, mock_ffi
    ):
        """Test that trace_callers defaults missing/None configuration attributes."""
        file_cache = MagicMock()
        vm = MagicMock()
        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=["file1.cpp"],
            ffi_exports={"foo"},
            cpp_linkages={},
            vm=vm,
            args=None,
        )

        mock_lsp.return_value = {}
        mock_macro.return_value = {}
        mock_ffi.return_value = {}

        tracer._process_caller_depth_step("file1.cpp", 1, "foo", 0, set(), deque())

        mock_lsp.assert_called_once_with(
            "file1.cpp", 1, "foo", 45, 15, False, file_cache=file_cache
        )
        mock_macro.assert_called_once()
        mock_ffi.assert_called_once()

    @patch("context_builder.graph_tracer.trace_ffi_callers")
    @patch("context_builder.graph_tracer.trace_macro_expansion")
    @patch("context_builder.graph_tracer.get_lsp_references")
    def test_trace_callers_defensive_getattr_explicit_none(
        self, mock_lsp, mock_macro, mock_ffi
    ):
        """Test that trace_callers defaults explicit None configuration attributes."""
        file_cache = MagicMock()
        vm = MagicMock()

        class MockArgs:
            lsp_timeout = None
            max_interface_depth = None
            disable_pruning = None
            skip_macro_expansion = None
            skip_ffi = None

        tracer = CallGraphTracer(
            file_cache=file_cache,
            all_repo_files=["file1.cpp"],
            ffi_exports={"foo"},
            cpp_linkages={},
            vm=vm,
            args=MockArgs(),
        )

        mock_lsp.return_value = {}
        mock_macro.return_value = {}
        mock_ffi.return_value = {}

        tracer._process_caller_depth_step("file1.cpp", 1, "foo", 0, set(), deque())

        mock_lsp.assert_called_once_with(
            "file1.cpp", 1, "foo", 45, 15, False, file_cache=file_cache
        )
        mock_macro.assert_called_once()
        mock_ffi.assert_called_once()
