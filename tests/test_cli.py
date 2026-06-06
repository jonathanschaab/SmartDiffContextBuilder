import os
import unittest
from unittest.mock import patch, MagicMock, ANY
import sys
from context_builder.cli import main

class TestCLI(unittest.TestCase):
    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.cli.get_lsp_references")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_bfs_traversal(
        self, mock_vm_cls, mock_get_lsp, mock_bounds, mock_run, mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        # 1. Setup argparse mock options
        mock_args = MagicMock()
        mock_args.format = "md"
        mock_args.max_lines = 100
        mock_args.max_mb = 1.0
        mock_args.base_name = "ContextLens"
        mock_args.max_cache_size = 100
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
        mock_bounds.side_effect = mock_bounds_fn

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
        mock_args = MagicMock()
        mock_args.format = "md"
        mock_args.max_lines = 100
        mock_args.max_mb = 1.0
        mock_args.base_name = "ContextLens"
        mock_args.max_cache_size = 100
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
    @patch("context_builder.cli.extract_callees")
    @patch("context_builder.cli.find_callee_definition")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_callee_depth_bfs_traversal(
        self, mock_vm_cls, mock_find_def, mock_extract_callees, mock_bounds, mock_run, mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        mock_args = MagicMock()
        mock_args.format = "md"
        mock_args.max_lines = 1000
        mock_args.max_mb = 1.0
        mock_args.base_name = "ContextLens"
        mock_args.max_cache_size = 100
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
        mock_bounds.side_effect = mock_bounds_fn

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
    @patch("context_builder.cli.VolumeManager")
    def test_cli_decorator_and_multiline_parsing(
        self, mock_vm_cls, mock_bounds, mock_run, mock_git_tracked, mock_git_diff, mock_parse_args
    ):
        mock_args = MagicMock()
        mock_args.format = "md"
        mock_args.max_lines = 1000
        mock_args.max_mb = 1.0
        mock_args.base_name = "ContextLens"
        mock_args.max_cache_size = 100
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
        mock_bounds.side_effect = mock_bounds_fn

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
        with patch("context_builder.cli.get_lsp_references") as mock_get_lsp:
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

