import os
import unittest
from unittest.mock import patch, MagicMock
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
        mock_parse_args.return_value = mock_args

        # 2. Setup mock files & bounds
        mock_git_diff.return_value = ["file1.py"]
        mock_git_tracked.return_value = ["file1.py", "file2.py", "file3.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "+10\n" if "diff" in cmd else ""

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
