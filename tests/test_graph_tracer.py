# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,unused-argument,consider-using-with
# pylint: disable=import-outside-toplevel,protected-access,too-few-public-methods,too-many-public-methods

"""Unit tests for CallGraphTracer."""

import unittest
import concurrent.futures
from unittest.mock import MagicMock, patch, ANY
from collections import deque
from types import SimpleNamespace

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
            "file1.cpp",
            1,
            "foo",
            150,
            15,
            False,
            file_cache=file_cache,
            init_timeout=60,
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
            lsp_init_timeout = None
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
            "file1.cpp",
            1,
            "foo",
            150,
            15,
            False,
            file_cache=file_cache,
            init_timeout=60,
        )
        mock_macro.assert_called_once()
        mock_ffi.assert_called_once()

    @patch("context_builder.graph_tracer.trace_lexical_dependencies_regex")
    @patch("context_builder.graph_tracer.AST_ENGINE.is_supported", return_value=False)
    @patch("context_builder.graph_tracer.get_lsp_references", return_value=None)
    def test_resolve_references_uses_regex_when_lsp_and_ast_are_unavailable(
        self, mock_lsp, _mock_supported, mock_regex
    ):
        mock_regex.return_value = {"caller.txt": [{"line": 2, "code": "target()"}]}
        tracer = CallGraphTracer(
            MagicMock(), ["caller.txt"], set(), {}, MagicMock(), None
        )

        callers = tracer._resolve_references("source.txt", 1, "target")

        self.assertEqual(callers, mock_regex.return_value)
        mock_lsp.assert_called_once_with(
            "source.txt",
            1,
            "target",
            150,
            15,
            False,
            file_cache=tracer.file_cache,
            init_timeout=60,
        )
        mock_regex.assert_called_once_with(
            "target", ["caller.txt"], file_cache=tracer.file_cache
        )

    @patch("context_builder.graph_tracer.trace_macro_expansion")
    @patch("context_builder.graph_tracer.get_language_profile")
    def test_macro_and_build_linkages_merge_without_duplicate_macro_lines(
        self, mock_profile, mock_macro
    ):
        mock_profile.return_value.supports_macro_expansion = True
        mock_macro.return_value = {
            "macro.cpp": [
                {"line": 4, "code": "existing"},
                {"line": 8, "code": "new"},
            ]
        }
        build_match = {"line": 12, "code": "linked"}
        tracer = CallGraphTracer(
            MagicMock(),
            ["macro.cpp"],
            set(),
            {"source.cpp": {"build.cpp": [build_match]}},
            MagicMock(),
            SimpleNamespace(skip_macro_expansion=False),
        )
        callers = {"macro.cpp": [{"line": 4, "code": "existing"}]}

        tracer._merge_macro_and_build_linkages("source.cpp", "target", callers)

        self.assertEqual([item["line"] for item in callers["macro.cpp"]], [4, 8])
        self.assertEqual(callers["build.cpp"], [build_match])
        mock_profile.assert_called_once_with("source.cpp")

    @patch("context_builder.graph_tracer.trace_ffi_callers")
    @patch("context_builder.graph_tracer.is_in_repo")
    @patch.object(CallGraphTracer, "_merge_macro_and_build_linkages")
    @patch.object(CallGraphTracer, "_resolve_references")
    def test_caller_step_filters_external_references_and_ffi(
        self, mock_resolve, _mock_merge, mock_is_in_repo, mock_ffi
    ):
        mock_resolve.return_value = {
            "inside.py": [{"line": 2, "code": "target()"}],
            "outside.py": [{"line": 3, "code": "target()"}],
            "[Pruned Instances]": [{"line": 1, "code": "omitted"}],
        }
        mock_ffi.return_value = {
            "bridge.py": [{"line": 5, "code": "target()"}],
            "external_bridge.py": [{"line": 6, "code": "target()"}],
        }
        mock_is_in_repo.side_effect = lambda path: path in {"inside.py", "bridge.py"}
        file_cache = MagicMock()
        file_cache.get_lines.return_value = ["def caller():\n", "    target()\n"]
        vm = MagicMock()
        vm.local_callers = []
        vm.ffi_linkages = []
        tracer = CallGraphTracer(
            file_cache,
            [],
            {"target"},
            {},
            vm,
            SimpleNamespace(skip_ffi=False),
        )

        with patch(
            "context_builder.graph_tracer.extract_function_bounds",
            return_value=(0, 2),
        ):
            tracer._process_caller_depth_step(
                "source.py", 1, "target", 0, set(), deque()
            )

        local_callers = vm.add_callers.call_args_list[0].args[1]
        ffi_callers = vm.add_callers.call_args_list[1].args[1]
        self.assertEqual(set(local_callers), {"inside.py", "[Pruned Instances]"})
        self.assertEqual(set(ffi_callers), {"bridge.py"})

    @patch("context_builder.graph_tracer.extract_function_bounds")
    def test_single_caller_reference_rejects_invalid_occurrences(self, mock_bounds):
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, MagicMock(), None)
        queue = deque()
        processed = set()

        tracer._process_single_caller_reference(
            "[Pruned Instances]", [{"line": 1}], processed, queue, 0
        )
        tracer._process_single_caller_reference(
            "caller.py", [{"line": 0}], processed, queue, 0
        )
        mock_bounds.return_value = (None, None)
        tracer._process_single_caller_reference(
            "caller.py", [{"line": 1}], processed, queue, 0
        )
        mock_bounds.return_value = (3, 4)
        tracer.file_cache.get_lines.return_value = ["short\n"]
        tracer._process_single_caller_reference(
            "caller.py", [{"line": 1}], processed, queue, 0
        )

        self.assertEqual(queue, deque())
        self.assertEqual(processed, set())

    @patch("context_builder.graph_tracer.extract_function_bounds", return_value=(None, None))
    @patch(
        "context_builder.graph_tracer.find_callee_definition",
        return_value=("callee.py", 4),
    )
    def test_single_callee_ignores_definition_without_function_bounds(
        self, _mock_find, _mock_bounds
    ):
        vm = MagicMock()
        vm.local_callees = []
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, vm, None)
        queue = deque()

        tracer._process_single_callee("helper", 0, set(), queue)

        self.assertEqual(vm.local_callees, [])
        self.assertEqual(queue, deque())

    @patch("context_builder.graph_tracer.extract_function_bounds", return_value=(None, None))
    def test_trace_callees_skips_source_without_function_bounds(self, _mock_bounds):
        tracer = CallGraphTracer(
            MagicMock(),
            [],
            set(),
            {},
            MagicMock(),
            SimpleNamespace(callee_depth=1),
        )
        queue = deque([("source.py", 3, "target", 0)])

        tracer.trace_callees(queue, set())

        self.assertEqual(queue, deque())

    @patch("context_builder.graph_tracer.extract_identifiers_with_positions")
    @patch("context_builder.graph_tracer.resolve_variable_definition")
    @patch("os.path.exists", return_value=True)
    def test_trace_data_flow_basic(self, mock_exists, mock_resolve, mock_extract):
        from unittest.mock import call

        # Setup mock returns
        mock_extract.side_effect = [
            [("var_a", 10, 5)],  # Initial diff file identifiers
            [("var_b", 20, 8)],  # Definition file identifiers (child)
        ]

        mock_resolve.side_effect = [
            {"definitions": [{"path": "file_b.py", "line": 20, "code": "var_b = 2"}]},
            {"definitions": [{"path": "file_c.py", "line": 30, "code": "var_c = 3"}]},
        ]

        vm = MagicMock()
        vm.data_states = []

        args = SimpleNamespace(data_depth=2, lsp_timeout=5.0)
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, vm, args)

        diff_files_lines = {"file_a.py": [10]}
        tracer.trace_data_flow(diff_files_lines)

        vm.add_data_state.assert_has_calls([
            call("file_b.py", 20, "var_b = 2"),
            call("file_c.py", 30, "var_c = 3")
        ])

    @patch("context_builder.graph_tracer.extract_identifiers_with_positions")
    def test_trace_data_flow_batches_same_depth_identifiers(self, mock_extract):
        mock_extract.return_value = [
            ("var_a", 10, 5),
            ("var_b", 11, 6),
        ]

        vm = MagicMock()
        args = SimpleNamespace(
            data_depth=1,
            lsp_timeout=5.0,
            data_flow_batch_size=2,
        )
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, vm, args)
        seen_batches = []

        def resolve_batch(batch, _timeout, _batch_size):
            seen_batches.append(list(batch))
            return [
                (
                    batch[0],
                    {"definitions": [{"path": "file_a.py", "line": 20, "code": "a = 1"}]},
                    None,
                ),
                (
                    batch[1],
                    {"definitions": [{"path": "file_b.py", "line": 30, "code": "b = 2"}]},
                    None,
                ),
            ]

        with patch.object(tracer, "_resolve_data_flow_batch", side_effect=resolve_batch):
            tracer.trace_data_flow({"file.py": [10, 11]})

        self.assertEqual(len(seen_batches), 1)
        self.assertEqual(len(seen_batches[0]), 2)
        self.assertEqual(vm.add_data_state.call_count, 2)

    def test_next_data_flow_batch_groups_without_depth_filter(self):
        queue = deque([
            ("file.py", "a", 1, 1, 0),
            ("file.py", "b", 2, 1, 0),
            ("file.py", "c", 3, 1, 1),
        ])

        batch = CallGraphTracer._next_data_flow_batch(queue, batch_size=4)

        self.assertEqual([item[1] for item in batch], ["a", "b"])
        self.assertEqual([item[1] for item in queue], ["c"])

    def test_data_flow_executor_is_reused_for_same_worker_count(self):
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, MagicMock(), None)
        try:
            executor = tracer._get_data_flow_executor(2)
            same_executor = tracer._get_data_flow_executor(2)

            self.assertIs(same_executor, executor)
        finally:
            tracer.close()

    def test_data_flow_executor_reused_when_worker_count_shrinks(self):
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, MagicMock(), None)
        try:
            executor = tracer._get_data_flow_executor(4)
            same_executor = tracer._get_data_flow_executor(2)

            self.assertIs(same_executor, executor)
            self.assertEqual(tracer._data_flow_executor_workers, 4)
        finally:
            tracer.close()

    def test_data_flow_executor_creation_holds_owner_lock(self):
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, MagicMock(), None)
        observed_lock_states = []
        real_executor = concurrent.futures.ThreadPoolExecutor

        def tracked_executor(*args, **kwargs):
            observed_lock_states.append(tracer._executor_lock._is_owned())
            return real_executor(*args, **kwargs)

        try:
            with patch(
                "context_builder.graph_tracer.concurrent.futures.ThreadPoolExecutor",
                side_effect=tracked_executor,
            ):
                tracer._get_data_flow_executor(2)
        finally:
            tracer.close()

        self.assertEqual(observed_lock_states, [True])

    @patch("context_builder.graph_tracer.extract_identifiers_with_positions")
    @patch("context_builder.graph_tracer.resolve_variable_definition")
    @patch("os.path.exists", return_value=True)
    def test_trace_data_flow_zero_depth(self, mock_exists, mock_resolve, mock_extract):
        vm = MagicMock()
        args = SimpleNamespace(data_depth=0)
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, vm, args)

        diff_files_lines = {"file_a.py": [10]}
        tracer.trace_data_flow(diff_files_lines)

        mock_extract.assert_not_called()
        mock_resolve.assert_not_called()

    @patch("context_builder.graph_tracer.extract_identifiers_with_positions")
    @patch("context_builder.graph_tracer.resolve_variable_definition")
    def test_trace_data_flow_exception_handling(self, mock_resolve, mock_extract):
        mock_extract.return_value = [("var_a", 10, 5), ("var_b", 12, 5)]

        # First call raises an exception, second call succeeds
        mock_resolve.side_effect = [
            Exception("Simulated LSP crash"),
            {"definitions": [{"path": "file_b.py", "line": 20, "code": "var_b = 2"}]}
        ]

        vm = MagicMock()
        vm.data_states = []

        args = SimpleNamespace(data_depth=1, lsp_timeout=5.0)
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, vm, args)

        diff_files_lines = {"file_a.py": [10]}
        tracer.trace_data_flow(diff_files_lines)

        # var_b should still be resolved and added
        vm.add_data_state.assert_called_once_with("file_b.py", 20, "var_b = 2")

    @patch("context_builder.graph_tracer.extract_identifiers_with_positions")
    def test_enqueue_identifiers_normalizes_to_abspath(self, mock_extract):
        import os
        mock_extract.return_value = [("var_x", 5, 2)]

        vm = MagicMock()
        tracer = CallGraphTracer(MagicMock(), [], set(), {}, vm, None)

        queue = deque()
        processed_vars = set()

        # Enqueue using a relative path
        tracer._enqueue_identifiers("relative_dir/file.py", [5], queue, processed_vars, 1)

        self.assertEqual(len(queue), 1)
        enqueued_path, var_name, line_num, char_offset, depth = queue[0]

        # Verify it became absolute path
        self.assertTrue(os.path.isabs(enqueued_path))
        self.assertEqual(var_name, "var_x")
        self.assertEqual(line_num, 5)
        self.assertEqual(char_offset, 2)
        self.assertEqual(depth, 1)

        # Check processed_vars set has absolute path as well
        self.assertEqual(len(processed_vars), 1)
        key = list(processed_vars)[0]
        self.assertTrue(os.path.isabs(key[0]))
        self.assertEqual(key[1], "var_x")
