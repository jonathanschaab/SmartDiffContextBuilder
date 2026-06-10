import os
import unittest
from unittest.mock import patch, MagicMock, ANY
import sys
import argparse
from context_builder.cli import main

class CliNamespace(argparse.Namespace):
    def __getattr__(self, name):
        return None

class TestCLI(unittest.TestCase):
    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.get_lsp_references")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_bfs_traversal(
        self, mock_vm_cls, mock_get_lsp, mock_tracer_bounds, mock_cli_bounds,
        mock_run, mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        # 1. Setup argparse mock options
        mock_args = CliNamespace()
        mock_args.format = "md"
        mock_args.max_lines = 100
        mock_args.max_mb = 1.0
        mock_args.base_name = "SmartDiffContextBuilder"
        mock_args.max_cache_size_mb = 200
        mock_args.max_interface_depth = 15
        mock_args.disable_pruning = False
        mock_args.lsp_timeout = 5
        mock_args.no_language_server = True
        mock_args.skip_ffi = True
        mock_args.skip_macro_expansion = True
        mock_args.caller_depth = 2
        mock_args.callee_depth = 1
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        # 2. Setup mock files & bounds
        mock_git_diff.return_value = ["file1.py"]
        mock_git_tracked.return_value = ["file1.py", "file2.py", "file3.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "@@ -9,1 +10,1 @@\n" if "diff" in cmd else ""

        # Function bounds mock:
        # file1.py line 10 -> starts at line 9 (0-indexed), ends at 15
        # file2.py line 5 -> starts at line 4, ends at 8
        def mock_bounds_fn(file_path, line_num, file_cache=None):
            if file_path == "file1.py":
                return 9, 15
            elif file_path == "file2.py":
                return 4, 8
            return None, None
        mock_cli_bounds.side_effect = mock_bounds_fn
        mock_tracer_bounds.side_effect = mock_bounds_fn

        # Mock cache lines
        mock_cache = MagicMock()
        mock_cache.get_lines.side_effect = lambda path: [
            "def root_func():\n" if path == "file1.py" else "def caller_func():\n"
        ] * 20
        
        # Mock LSP reference queries:
        # depth 0: file1.py, root_func -> caller in file2.py at line 5
        # depth 1: file2.py, caller_func -> caller in file3.py at line 30 (which exceeds caller_depth=2 limit)
        def mock_lsp_fn(file_path, line_num, func_name, *args, **kwargs):
            if file_path == "file1.py" and func_name == "root_func":
                return {"file2.py": [{"line": 5, "code": "root_func()"}]}
            elif file_path == "file2.py" and func_name == "caller_func":
                return {"file3.py": [{"line": 30, "code": "caller_func()"}]}
            return {}
        mock_get_lsp.side_effect = mock_lsp_fn

        # VolumeManager mock instance
        mock_vm = MagicMock()
        mock_vm_cls.return_value = mock_vm

        # Run main
        with patch("context_builder.cli.get_global_cache", return_value=mock_cache), \
             patch("context_builder.cli.is_in_repo", return_value=True), \
             patch("os.path.exists", return_value=True):
            main()

        # Verify calls to VolumeManager.add_callers
        # It should have called add_callers for depth 1 (file2.py caller) and depth 2 (file3.py caller)
        # Verify that add_callers was called with distance=1 for file2.py, and distance=2 for file3.py
        calls = mock_vm.add_callers.call_args_list
        self.assertTrue(len(calls) >= 2)
        
        # First caller addition (depth 1)
        first_call_args = calls[0][0]
        self.assertEqual(first_call_args[1], {"file2.py": [{"line": 5, "code": "root_func()"}]})
        self.assertEqual(calls[0][1]["distance"], 1)

        # Second caller addition (depth 2)
        second_call_args = calls[1][0]
        self.assertEqual(second_call_args[1], {"file3.py": [{"line": 30, "code": "caller_func()"}]})
        self.assertEqual(calls[1][1]["distance"], 2)
        
        # Verify that flush was called
        mock_vm.flush_all_volumes.assert_called_once()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_hunk_header_parsing(
        self, mock_vm_cls, mock_bounds, mock_run, mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        mock_args = CliNamespace()
        mock_args.format = "md"
        mock_args.max_lines = 100
        mock_args.max_mb = 1.0
        mock_args.base_name = "SmartDiffContextBuilder"
        mock_args.max_cache_size_mb = 200
        mock_args.max_interface_depth = 15
        mock_args.disable_pruning = False
        mock_args.lsp_timeout = 5
        mock_args.no_language_server = True
        mock_args.skip_ffi = True
        mock_args.skip_macro_expansion = True
        mock_args.caller_depth = 1
        mock_args.callee_depth = 1
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        # A diff output that has:
        # - A real hunk header (@@ -5,2 +10,3 @@)
        # - Code additions containing + followed by digits (+ x = 10 + 20)
        # - Another hunk header (@@ -20 +30 @@)
        diff_output = (
            "@@ -5,2 +10,3 @@\n"
            "+ x = 10 + 20\n"
            "+ y = 30 + 40\n"
            "@@ -20 +30 @@\n"
            "+ z = 50\n"
        )

        mock_git_diff.return_value = ["file1.py"]
        mock_git_tracked.return_value = ["file1.py"]
        mock_run.side_effect = lambda cmd, **kwargs: diff_output if "diff" in cmd else ""

        # Function bounds mock: we want to capture what line_numbers were queried!
        queried_lines = []
        def mock_bounds_fn(file_path, line_num, file_cache=None):
            queried_lines.append(line_num)
            return None, None
        mock_bounds.side_effect = mock_bounds_fn

        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = ["\n"] * 100

        mock_vm = MagicMock()
        mock_vm_cls.return_value = mock_vm

        with patch("context_builder.cli.get_global_cache", return_value=mock_cache), \
             patch("context_builder.cli.is_in_repo", return_value=True), \
             patch("os.path.exists", return_value=True):
            main()

        # The parsed line numbers from "@@ -5,2 +10,3 @@" should be: 10, 11, 12
        # The parsed line numbers from "@@ -20 +30 @@" should be: 30
        # The literal +20 and +40 inside code lines should NOT be parsed.
        self.assertEqual(queried_lines, [10, 11, 12, 30])

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_callees")
    @patch("context_builder.graph_tracer.find_callee_definition")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_callee_depth_bfs_traversal(
        self, mock_vm_cls, mock_find_def, mock_extract_callees,
        mock_tracer_bounds, mock_cli_bounds, mock_run, mock_git_tracked,
        mock_git_diff, mock_parse_args
    ):
        mock_args = CliNamespace()
        mock_args.format = "md"
        mock_args.max_lines = 1000
        mock_args.max_mb = 1.0
        mock_args.base_name = "SmartDiffContextBuilder"
        mock_args.max_cache_size_mb = 200
        mock_args.max_interface_depth = 15
        mock_args.disable_pruning = False
        mock_args.lsp_timeout = 5
        mock_args.no_language_server = True
        mock_args.skip_ffi = True
        mock_args.skip_macro_expansion = True
        mock_args.caller_depth = 0
        mock_args.callee_depth = 2
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        mock_git_diff.return_value = ["root.py"]
        mock_git_tracked.return_value = ["root.py", "callee1.py", "callee2.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n" if "diff" in cmd else ""

        # Setup bounds mock:
        # root.py: line 1 -> start=0, end=4 (def foo)
        # callee1.py: line 2 -> start=1, end=5 (def bar)
        # callee2.py: line 3 -> start=2, end=6 (def baz)
        def mock_bounds_fn(file_path, line_num, file_cache=None):
            if file_path == "root.py":
                return 0, 4
            elif file_path == "callee1.py":
                return 1, 5
            elif file_path == "callee2.py":
                return 2, 6
            return None, None
        mock_cli_bounds.side_effect = mock_bounds_fn
        mock_tracer_bounds.side_effect = mock_bounds_fn

        # Setup callees mock:
        # root.py -> calls "bar"
        # callee1.py -> calls "baz"
        # callee2.py -> calls nothing
        def mock_extract_callees_fn(file_path, start, end, file_cache=None):
            if file_path == "root.py":
                return ["bar"]
            elif file_path == "callee1.py":
                return ["baz"]
            return []
        mock_extract_callees.side_effect = mock_extract_callees_fn

        # Setup find definition mock:
        # "bar" -> callee1.py, line 2
        # "baz" -> callee2.py, line 3
        def mock_find_def_fn(name, files, file_cache=None):
            if name == "bar":
                return "callee1.py", 2
            elif name == "baz":
                return "callee2.py", 3
            return None, None
        mock_find_def.side_effect = mock_find_def_fn

        mock_cache = MagicMock()
        mock_cache.get_lines.side_effect = lambda path: [
            "def foo():\n" if path == "root.py" else ("def bar():\n" if path == "callee1.py" else "def baz():\n")
        ] * 10

        mock_vm = MagicMock()
        mock_vm.local_callees = []
        mock_vm_cls.return_value = mock_vm

        with patch("context_builder.cli.get_global_cache", return_value=mock_cache), \
             patch("context_builder.cli.is_in_repo", return_value=True), \
             patch("os.path.exists", return_value=True):
            main()

        # Check local_callees additions
        self.assertEqual(len(mock_vm.local_callees), 2)
        # first callee: bar, distance 1
        self.assertEqual(mock_vm.local_callees[0]["function_name"], "bar")
        self.assertEqual(mock_vm.local_callees[0]["distance"], 1)
        self.assertEqual(mock_vm.local_callees[0]["file"], "callee1.py")
        # second callee: baz, distance 2
        self.assertEqual(mock_vm.local_callees[1]["function_name"], "baz")
        self.assertEqual(mock_vm.local_callees[1]["distance"], 2)
        self.assertEqual(mock_vm.local_callees[1]["file"], "callee2.py")

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_decorator_and_multiline_parsing(
        self, mock_vm_cls, mock_tracer_bounds, mock_cli_bounds, mock_run,
        mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        mock_args = CliNamespace()
        mock_args.format = "md"
        mock_args.max_lines = 1000
        mock_args.max_mb = 1.0
        mock_args.base_name = "SmartDiffContextBuilder"
        mock_args.max_cache_size_mb = 200
        mock_args.max_interface_depth = 15
        mock_args.disable_pruning = False
        mock_args.lsp_timeout = 5
        mock_args.no_language_server = True
        mock_args.skip_ffi = True
        mock_args.skip_macro_expansion = True
        mock_args.caller_depth = 1
        mock_args.callee_depth = 0
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        # root.py has a decorator and multiline declaration
        mock_git_diff.return_value = ["root.py"]
        mock_git_tracked.return_value = ["root.py", "caller.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n" if "diff" in cmd else ""

        # root.py bounds: start=0, end=5
        # caller.py bounds: start=0, end=5
        def mock_bounds_fn(file_path, line_num, file_cache=None):
            return 0, 5
        mock_cli_bounds.side_effect = mock_bounds_fn
        mock_tracer_bounds.side_effect = mock_bounds_fn

        # Mock cache lines
        mock_cache = MagicMock()
        # root.py starts with a decorator on first line, so file_lines[start] is '@decorator'
        mock_cache.get_lines.side_effect = lambda path: [
            "@my_decorator\n",
            "def foo(\n",
            "    x, y\n",
            "):\n",
            "    pass\n"
        ] if path == "root.py" else [
            "@other_decorator\n",
            "def caller_func(\n",
            "    a\n",
            "):\n",
            "    foo(a)\n"
        ]

        # LSP returns a caller in caller.py
        with patch("context_builder.graph_tracer.get_lsp_references") as mock_get_lsp:
            mock_get_lsp.return_value = {"caller.py": [{"line": 5, "code": "foo(a)"}]}
            
            mock_vm = MagicMock()
            mock_vm_cls.return_value = mock_vm

            with patch("context_builder.cli.get_global_cache", return_value=mock_cache), \
                 patch("context_builder.cli.is_in_repo", return_value=True), \
                 patch("os.path.exists", return_value=True):
                main()

            # The function name for root.py should be correctly parsed as "foo"
            # (which we can verify because vm.add_modified_object is called with "foo")
            mock_vm.add_modified_object.assert_called_with("root.py", "foo", ANY)

            # The function name for caller.py should be correctly parsed as "caller_func"
            # (which we can verify because the BFS queue will append "caller_func" and call add_callers)
            calls = mock_vm.add_callers.call_args_list
            self.assertTrue(len(calls) >= 1)

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_function_name_extraction_comments_and_strings(
        self, mock_vm_cls, mock_bounds, mock_run, mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        mock_args = CliNamespace()
        mock_args.format = "md"
        mock_args.max_lines = 1000
        mock_args.max_mb = 1.0
        mock_args.base_name = "SmartDiffContextBuilder"
        mock_args.max_cache_size_mb = 200
        mock_args.max_interface_depth = 15
        mock_args.disable_pruning = False
        mock_args.lsp_timeout = 5
        mock_args.no_language_server = True
        mock_args.skip_ffi = True
        mock_args.skip_macro_expansion = True
        mock_args.caller_depth = 0
        mock_args.callee_depth = 0
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        mock_git_diff.return_value = ["root.py"]
        mock_git_tracked.return_value = ["root.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n" if "diff" in cmd else ""

        mock_bounds.return_value = (0, 5)

        # Mock cache lines where the chunk starts with comments and string literals containing keywords
        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = [
            "# This is a def of dummy function\n",
            "\"\"\"def another_dummy_string:\"\"\"\n",
            "@my_decorator\n",
            "def real_func():\n",
            "    pass\n"
        ]

        mock_vm = MagicMock()
        mock_vm_cls.return_value = mock_vm

        with patch("context_builder.cli.get_global_cache", return_value=mock_cache), \
             patch("context_builder.cli.is_in_repo", return_value=True), \
             patch("os.path.exists", return_value=True):
            main()

        # The function name should be correctly extracted as "real_func" despite the keywords in comments and strings
        mock_vm.add_modified_object.assert_called_with("root.py", "real_func", ANY)

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("subprocess.run")
    @patch("shutil.rmtree")
    def test_cli_robust_worktree_cleanup(
        self, mock_rmtree, mock_sub_run, mock_run_scan, mock_resolve_range, mock_parse_args
    ):
        mock_args = CliNamespace()
        mock_args.commit_range = "-3"
        mock_parse_args.return_value = mock_args
        
        mock_resolve_range.return_value = ("start_sha", "end_sha")
        
        # Simulating run_scan raising an exception (original error)
        mock_run_scan.side_effect = RuntimeError("Original scan error")
        
        # Simulating git worktree remove failing in the finally block
        # We also need to recreate the directory if git worktree add is run in the test because
        # it was deleted via os.rmdir in cli.py, and os.chdir requires it to exist.
        def sub_run_side_effect_robust(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "worktree" in cmd:
                if "add" in cmd:
                    os.makedirs(cmd[4], exist_ok=True)
                if "remove" in cmd:
                    return MagicMock(returncode=1)
            return MagicMock(returncode=0)
        mock_sub_run.side_effect = sub_run_side_effect_robust
        mock_rmtree.side_effect = PermissionError("Permission denied on Windows cleanup")

        with self.assertRaises(RuntimeError) as ctx:
            main()
            
        # Verify the original exception is preserved, not masked by cleanup failures
        self.assertEqual(str(ctx.exception), "Original scan error")

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.cleanup_zombie_lsps")
    @patch("subprocess.run")
    @patch("shutil.rmtree")
    def test_cli_worktree_cleanup_calls_lsp_cleanup_before_remove(
        self, mock_rmtree, mock_sub_run, mock_cleanup_lsps, mock_run_scan, mock_resolve_range, mock_parse_args
    ):
        """cleanup_zombie_lsps() must be called BEFORE git worktree remove.

        On Windows, LSP server processes hold open file handles to files inside
        the temporary worktree directory.  If those processes are still running
        when shutil.rmtree / git worktree remove execute, the locked files cause
        the cleanup to fail.  This test records the order of all side-effectful
        calls and asserts that cleanup_zombie_lsps() precedes worktree removal.
        """
        mock_args = CliNamespace()
        mock_args.commit_range = "-1"
        mock_parse_args.return_value = mock_args
        mock_resolve_range.return_value = ("sha_start", "sha_end")

        # run_scan completes without error so we reach the normal finally path
        mock_run_scan.return_value = None

        # Record the global call order across all three mocks
        call_order = []
        mock_cleanup_lsps.side_effect = lambda: call_order.append("cleanup_zombie_lsps")
        mock_rmtree.side_effect = lambda *a, **kw: call_order.append("rmtree")

        def sub_run_side_effect(*args, **kwargs):
            # Record only the worktree-related subprocess calls
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "worktree" in cmd:
                call_order.append(f"subprocess.run:{' '.join(cmd)}")
                if "add" in cmd:
                    os.makedirs(cmd[4], exist_ok=True)
            return MagicMock(returncode=0)
        mock_sub_run.side_effect = sub_run_side_effect

        main()

        # cleanup_zombie_lsps must appear in the list before any worktree removal
        self.assertIn("cleanup_zombie_lsps", call_order,
                      "cleanup_zombie_lsps() was never called in the finally block")
        lsp_idx = call_order.index("cleanup_zombie_lsps")
        worktree_remove_indices = [
            i for i, s in enumerate(call_order)
            if "worktree" in s and "remove" in s
        ]
        for rm_idx in worktree_remove_indices:
            self.assertLess(lsp_idx, rm_idx,
                            f"cleanup_zombie_lsps (pos {lsp_idx}) must precede "
                            f"worktree remove (pos {rm_idx}) in call order")

    def test_extract_function_name_c_style(self):
        from context_builder.graph_tracer import extract_function_name
        
        # Test standard Python/Rust with keyword
        res = extract_function_name("def my_python_func(x):", 0, 5)
        self.assertEqual(res, "my_python_func")
        
        # Test C-style (no keyword, identifier followed by parenthesis)
        res = extract_function_name("void my_c_func(int x) {", 10, 15)
        self.assertEqual(res, "my_c_func")
        
        # Test C-style with spaces before parenthesis
        res = extract_function_name("int spaced_func   (double y)", 20, 25)
        self.assertEqual(res, "spaced_func")
        
        # Test exclusion of control flow keywords
        res = extract_function_name("if (x > y) {", 30, 35)
        self.assertEqual(res, "block_lines_30_35")
        
        # Test another control flow
        res = extract_function_name("while (true)", 40, 45)
        self.assertEqual(res, "block_lines_40_45")
        
        # Test C++ destructor (~MyClass)
        res = extract_function_name("MyClass::~MyClass() {", 50, 55)
        self.assertEqual(res, "~MyClass")

    @patch("context_builder.cli.run_command")
    def test_get_default_branch(self, mock_run):
        from context_builder.cli import get_default_branch
        
        # Test case 1: main exists
        def run_side_effect(cmd, **kwargs):
            if "rev-parse" in cmd and "main" in cmd:
                return "some_sha_for_main\n"
            return ""
        mock_run.side_effect = run_side_effect
        self.assertEqual(get_default_branch(), "main")
        
        # Test case 2: main does not exist, but master does
        def run_side_effect_master(cmd, **kwargs):
            if "rev-parse" in cmd and "master" in cmd:
                return "some_sha_for_master\n"
            return ""
        mock_run.side_effect = run_side_effect_master
        self.assertEqual(get_default_branch(), "master")
        
        # Test case 3: neither exist (fallback to main)
        mock_run.side_effect = lambda cmd, **kwargs: ""
        self.assertEqual(get_default_branch(), "main")

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.cleanup_zombie_lsps")
    @patch("subprocess.run")
    @patch("shutil.rmtree")
    @patch("shutil.copy")
    @patch("os.path.exists")
    def test_cli_worktree_cleanup_on_copy_exception(
        self, mock_exists, mock_copy, mock_rmtree, mock_sub_run, mock_cleanup_lsps, mock_run_scan, mock_resolve_range, mock_parse_args
    ):
        """If shutil.copy raises an exception, the worktree must still be cleaned up."""
        mock_args = CliNamespace()
        mock_args.commit_range = "-1"
        mock_parse_args.return_value = mock_args
        mock_resolve_range.return_value = ("sha_start", "sha_end")

        mock_exists.return_value = True
        mock_copy.side_effect = IOError("Disk full or permission denied")

        # Record call order to check cleanup is run
        call_order = []
        mock_cleanup_lsps.side_effect = lambda: call_order.append("cleanup_zombie_lsps")
        mock_rmtree.side_effect = lambda *a, **kw: call_order.append("rmtree")

        def sub_run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "worktree" in cmd:
                call_order.append(f"subprocess.run:{' '.join(cmd)}")
                if "add" in cmd:
                    os.makedirs(cmd[4], exist_ok=True)
            return MagicMock(returncode=0)
        mock_sub_run.side_effect = sub_run_side_effect

        with self.assertRaises(IOError):
            main()

        # cleanup_zombie_lsps and worktree removal must be called in finally
        self.assertIn("cleanup_zombie_lsps", call_order)
        self.assertTrue(any("worktree remove" in s for s in call_order))

    @patch("context_builder.cli.resolve_commit_ref")
    def test_parse_and_resolve_range_start_plus_zero(self, mock_resolve):
        from context_builder.cli import parse_and_resolve_range
        
        mock_resolve.side_effect = lambda ref: f"sha_{ref}"
        
        # HEAD+0 should resolve to ("sha_HEAD", "sha_HEAD")
        start, end = parse_and_resolve_range("HEAD+0")
        self.assertEqual(start, "sha_HEAD")
        self.assertEqual(end, "sha_HEAD")

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.cleanup_zombie_lsps")
    @patch("subprocess.run")
    @patch("shutil.rmtree")
    @patch("shutil.copy")
    @patch("os.path.exists")
    def test_cli_worktree_copies_coverage_xml(
        self, mock_exists, mock_copy, mock_rmtree, mock_sub_run, mock_cleanup_lsps, mock_run_scan, mock_resolve_range, mock_parse_args
    ):
        """Verify that coverage.xml is copied to the temporary worktree if it exists in the original repo root."""
        mock_args = CliNamespace()
        mock_args.commit_range = "-1"
        mock_parse_args.return_value = mock_args
        mock_resolve_range.return_value = ("sha_start", "sha_end")

        # Mock HEAD parse return to not match end_sha (so it doesn't bypass worktree creation)
        def sub_run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "worktree" in cmd:
                if "add" in cmd:
                    os.makedirs(cmd[4], exist_ok=True)
            res = MagicMock(returncode=0)
            res.stdout = "different_head_sha\n"
            return res
        mock_sub_run.side_effect = sub_run_side_effect

        # Mock existence of compile_commands.json and coverage.xml
        mock_exists.side_effect = lambda path: "compile_commands.json" in path or "coverage.xml" in path

        main()

        # Check that shutil.copy was called for both compile_commands.json and coverage.xml
        copy_calls = [call[0][0] for call in mock_copy.call_args_list]
        self.assertTrue(any("compile_commands.json" in c for c in copy_calls))
        self.assertTrue(any("coverage.xml" in c for c in copy_calls))

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("subprocess.run")
    def test_worktree_bypassed_if_head_matches_end_sha(
        self, mock_sub_run, mock_run_scan, mock_resolve_range, mock_parse_args
    ):
        """Verify that temporary worktree creation is bypassed if HEAD matches end_sha."""
        mock_args = CliNamespace()
        mock_args.commit_range = "-1"
        mock_parse_args.return_value = mock_args
        
        # Resolve range returns "sha_end" as the end SHA
        mock_resolve_range.return_value = ("sha_start", "sha_end")
        
        # Mock git rev-parse HEAD subprocess call to return "sha_end"
        mock_res = MagicMock(returncode=0)
        mock_res.stdout = "sha_end\n"
        mock_sub_run.return_value = mock_res
        
        main()
        
        # Verify that git worktree add was NOT called
        for call in mock_sub_run.call_args_list:
            cmd = call[0][0]
            self.assertFalse("worktree" in cmd and "add" in cmd)
            
        # Verify that run_scan was called directly
        mock_run_scan.assert_called_once_with(mock_args, start_ref="sha_start", end_ref="sha_end")

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_config_file_loading_and_merging(self, mock_run_scan, mock_parse_args):
        """Verify that loading a config file updates CONFIG and merges keys correctly."""
        from context_builder.config import CONFIG, reset_config
        reset_config()
        
        # Create temp config file with comments
        config_content = """
        // custom test config
        {
            "max_lines": 4200,
            "lang_map": {
                ".custom": "custom_lang"
            }
        }
        """
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write(config_content)
            config_path = f.name
            
        try:
            mock_args = CliNamespace()
            mock_args.config = config_path
            mock_parse_args.return_value = mock_args
            
            main()
            
            # Assert CONFIG is updated
            self.assertEqual(CONFIG["max_lines"], 4200)
            self.assertEqual(CONFIG["lang_map"][".custom"], "custom_lang")
            
            # Assert args namespace passed to run_scan is populated
            passed_args = mock_run_scan.call_args[0][0]
            self.assertEqual(passed_args.max_lines, 4200)
            self.assertEqual(passed_args.lang_map[".custom"], "custom_lang")
        finally:
            os.remove(config_path)
            reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_argument_overrides(self, mock_run_scan, mock_parse_args):
        """Verify that CLI overrides override config and merge JSON inputs correctly."""
        from context_builder.config import CONFIG, reset_config
        reset_config()
        
        mock_args = CliNamespace()
        mock_args.max_lines = 3300
        mock_args.lang_map = '{".overridden": "overridden_lang"}'
        mock_parse_args.return_value = mock_args
        
        main()
        
        self.assertEqual(CONFIG["max_lines"], 3300)
        self.assertEqual(CONFIG["lang_map"][".overridden"], "overridden_lang")
        # Default keys should remain intact since dictionary merges are used
        self.assertEqual(CONFIG["lang_map"][".py"], "python")
        
        passed_args = mock_run_scan.call_args[0][0]
        self.assertEqual(passed_args.max_lines, 3300)
        self.assertEqual(passed_args.lang_map[".overridden"], "overridden_lang")
        reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    def test_create_config_generation(self, mock_parse_args):
        """Verify that --create-config generates a commented config file with overrides uncommented."""
        from context_builder.config import reset_config
        reset_config()
        
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name
            
        try:
            mock_args = CliNamespace()
            mock_args.create_config = temp_path
            mock_args.max_lines = 1234
            mock_args.format = "json"
            mock_parse_args.return_value = mock_args
            
            with self.assertRaises(SystemExit) as cm:
                main()
                
            self.assertEqual(cm.exception.code, 0)
            
            # Read generated file
            with open(temp_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            # Verify uncommented overrides
            self.assertIn('"max_lines": 1234', content)
            self.assertIn('"format": "json"', content)
            # Verify commented out defaults
            self.assertIn('// "max_mb": 2.0', content)
            self.assertIn('// "base_name": "SmartDiffContextBuilder"', content)
        finally:
            os.remove(temp_path)
            reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_parse_cli_json_type_guard(self, mock_run_scan, mock_parse_args):
        """Verify that parse_cli_json doesn't crash if passed a python dict or list directly."""
        from context_builder.config import CONFIG, reset_config
        reset_config()
        
        mock_args = CliNamespace()
        mock_args.lang_map = {".direct": "direct_lang"}
        mock_parse_args.return_value = mock_args
        
        main()
        
        self.assertEqual(CONFIG["lang_map"][".direct"], "direct_lang")
        reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    def test_cli_config_file_loading_non_list_for_list_key(self, mock_parse_args):
        """Verify that loading a config file with a non-list value for a list key

        exits with an error.
        """
        from context_builder.config import reset_config
        import tempfile
        import shutil
        reset_config()

        # Write invalid config to temp file
        temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(temp_dir, "invalid_config.json")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write('{"callee_ignored_keywords": "not_a_list_string"}')

            mock_args = CliNamespace()
            mock_args.config = temp_path
            mock_parse_args.return_value = mock_args

            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    def test_cli_json_override_non_list_for_list_key(self, mock_parse_args):
        """Verify that a CLI override with a non-list JSON string for a list key

        exits with an error.
        """
        from context_builder.config import reset_config
        reset_config()

        mock_args = CliNamespace()
        mock_args.callee_ignored_keywords = '"not_a_list_string"'
        mock_parse_args.return_value = mock_args

        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)
        reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_partial_namespace_safety(self, mock_run_scan, mock_parse_args):
        """Verify that main() executes safely when passed a partial namespace

        missing several typical attributes (no AttributeError raised).
        """
        from context_builder.config import reset_config
        reset_config()

        # Create a raw namespace with only format defined, missing all other keys
        mock_args = argparse.Namespace()
        mock_args.format = "json"
        mock_parse_args.return_value = mock_args

        # Calling main should run fine because of getattr default fallbacks
        try:
            main()
        except AttributeError as e:
            self.fail(f"main() raised AttributeError: {e}")

        reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    def test_cli_config_file_loading_invalid_primitive_types(self, mock_parse_args):
        """Verify that config overrides with mismatched primitive types cause error exits."""
        from context_builder.config import reset_config
        reset_config()

        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # Test string instead of boolean
            temp_path = os.path.join(temp_dir, "invalid_config_bool.json")
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write('{"disable_pruning": "false"}')
            mock_args = CliNamespace()
            mock_args.config = temp_path
            mock_parse_args.return_value = mock_args
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

            # Test boolean instead of integer
            temp_path2 = os.path.join(temp_dir, "invalid_config_int.json")
            with open(temp_path2, "w", encoding="utf-8") as f:
                f.write('{"max_lines": true}')
            mock_args = CliNamespace()
            mock_args.config = temp_path2
            mock_parse_args.return_value = mock_args
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    def test_cli_json_override_invalid_primitive_types(self, mock_parse_args):
        """Verify that CLI JSON overrides with mismatched types cause error exits."""
        from context_builder.config import reset_config
        reset_config()

        # Test string instead of float (max_mb)
        mock_args = CliNamespace()
        mock_args.max_mb = '"not_a_float"'
        mock_parse_args.return_value = mock_args
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

        reset_config()

    def test_validate_config_type_max_cache_size_mb(self):
        """Verify that _validate_config_type accepts both float and int for max_cache_size_mb."""
        from context_builder.cli import _validate_config_type
        from context_builder.config import reset_config
        reset_config()

        # Should not raise SystemExit
        _validate_config_type("max_cache_size_mb", 200)
        _validate_config_type("max_cache_size_mb", 1.5)

        # Should raise SystemExit on invalid type (like bool or string)
        with self.assertRaises(SystemExit) as cm:
            _validate_config_type("max_cache_size_mb", "not_a_float")
        self.assertEqual(cm.exception.code, 1)

        with self.assertRaises(SystemExit) as cm2:
            _validate_config_type("max_cache_size_mb", True)
        self.assertEqual(cm2.exception.code, 1)

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_ripgrep_timeout_mapping(self, mock_run_scan, mock_parse_args):
        """Verify that CLI parameter --ripgrep-timeout updates CONFIG['ripgrep_timeout']."""
        from context_builder.config import CONFIG, reset_config
        reset_config()

        # 1. Test integer timeout
        mock_args = CliNamespace()
        mock_args.ripgrep_timeout = 35
        mock_parse_args.return_value = mock_args

        main()

        self.assertEqual(CONFIG["ripgrep_timeout"], 35)
        args_passed = mock_run_scan.call_args[0][0]
        self.assertEqual(args_passed.ripgrep_timeout, 35)

        # 2. Test float/fractional timeout
        reset_config()
        mock_args = CliNamespace()
        mock_args.ripgrep_timeout = 1.5
        mock_parse_args.return_value = mock_args

        main()

        self.assertEqual(CONFIG["ripgrep_timeout"], 1.5)

        # 3. Verify type validation (should fail if not an int/float, e.g. a string)
        mock_args.ripgrep_timeout = "not_a_float"
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)
