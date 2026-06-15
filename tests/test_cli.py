# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,unused-argument
# pylint: disable=protected-access,redefined-outer-name,reimported,consider-using-with
# pylint: disable=line-too-long,too-many-lines,too-many-public-methods,broad-exception-caught
# pylint: disable=too-few-public-methods,no-else-return

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, ANY
import argparse
from collections import deque

from context_builder.cli import main

class CliNamespace(argparse.Namespace):
    def __getattr__(self, name):
        return None

class TestCLI(unittest.TestCase):
    def test_rewrite_compile_commands_payload_rewrites_both_slash_styles(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [
            {
                "directory": "C:/repo/build",
                "file": r"C:\repo\src\main.cpp",
                "command": 'clang++ -I C:/repo/include "C:/repo/src/main.cpp"',
                "arguments": [
                    "clang++",
                    r"C:\repo\src\main.cpp",
                    "-I",
                    "C:/repo/include",
                ],
                "output": "C:/repo/build/main.o",
            }
        ]

        rewritten = _rewrite_compile_commands_payload(
            payload,
            r"C:\repo",
            r"D:\worktree",
        )

        entry = rewritten[0]
        self.assertEqual(entry["directory"], "D:/worktree/build")
        self.assertEqual(entry["file"], r"D:\worktree\src\main.cpp")
        self.assertIn('D:/worktree/src/main.cpp', entry["command"])
        self.assertEqual(entry["arguments"][1], r"D:\worktree\src\main.cpp")
        self.assertEqual(entry["arguments"][3], "D:/worktree/include")
        self.assertEqual(entry["output"], "D:/worktree/build/main.o")

    def test_rewrite_compile_commands_payload_does_not_rewrite_path_prefixes(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [
            {
                "directory": "/repo/build",
                "file": "/repo/src/main.cpp",
                "command": "clang++ /repo-utils/generated.cpp /repo/src/main.cpp",
                "output": "/repo-utils/build/main.o",
            }
        ]

        rewritten = _rewrite_compile_commands_payload(payload, "/repo", "/worktree")
        entry = rewritten[0]

        self.assertEqual(entry["directory"], "/worktree/build")
        self.assertEqual(entry["file"], "/worktree/src/main.cpp")
        self.assertIn("/repo-utils/generated.cpp", entry["command"])
        self.assertIn("/worktree/src/main.cpp", entry["command"])
        self.assertEqual(entry["output"], "/repo-utils/build/main.o")

    def test_rewrite_compile_commands_payload_rewrites_in_place(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [{"file": "/repo/src/main.cpp"}]

        rewritten = _rewrite_compile_commands_payload(payload, "/repo", "/worktree")

        self.assertIs(rewritten, payload)
        self.assertEqual(payload[0]["file"], "/worktree/src/main.cpp")

    def test_rewrite_compile_commands_payload_normalizes_mixed_slash_roots(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [{"file": "C:/repo/src/main.cpp"}]

        rewritten = _rewrite_compile_commands_payload(
            payload,
            r"C:\repo",
            r"D:\worktree",
        )

        self.assertEqual(rewritten[0]["file"], "D:/worktree/src/main.cpp")

    def test_rewrite_compile_commands_payload_handles_trailing_root_separators(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [{"file": "/repo/src/main.cpp"}]

        rewritten = _rewrite_compile_commands_payload(
            payload,
            "/repo/",
            "/worktree/",
        )

        self.assertEqual(rewritten[0]["file"], "/worktree/src/main.cpp")

    def test_rewrite_compile_commands_payload_rewrites_colon_separated_path_lists(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [
            {
                "command": "-Wl,-rpath,/repo/lib:/repo/third_party/lib:/repo-utils/lib",
            }
        ]

        rewritten = _rewrite_compile_commands_payload(payload, "/repo", "/worktree")

        self.assertEqual(
            rewritten[0]["command"],
            "-Wl,-rpath,/worktree/lib:/worktree/third_party/lib:/repo-utils/lib",
        )

    def test_rewrite_compile_commands_payload_keeps_windows_drive_letter_paths_valid(self):
        from context_builder.cli import _rewrite_compile_commands_payload

        payload = [
            {"file": r"C:\repo\src\main.cpp"},
            {"file": r"c:\repo\include\lib.hpp"},
        ]

        rewritten = _rewrite_compile_commands_payload(
            payload,
            r"C:\repo",
            r"D:\worktree",
        )

        self.assertEqual(rewritten[0]["file"], r"D:\worktree\src\main.cpp")
        self.assertEqual(rewritten[1]["file"], r"D:\worktree\include\lib.hpp")

    @patch("context_builder.path_utils._run_git_probe_process")
    def test_rewrite_compile_commands_payload_handles_full_windows_path_case_mismatch(
        self, mock_run
    ):
        from context_builder.cli import _rewrite_compile_commands_payload
        from context_builder.path_utils import clear_path_case_caches

        mock_run.side_effect = OSError("git unavailable")
        clear_path_case_caches()
        payload = [
            {"file": r"c:\REPO\Src\main.cpp"},
            {"command": r'clang++ "C:/REPO/Src/main.cpp" -I c:/REPO/include'},
        ]

        rewritten = _rewrite_compile_commands_payload(
            payload,
            r"C:\repo",
            r"D:\worktree",
        )

        self.assertEqual(rewritten[0]["file"], r"D:\worktree\Src\main.cpp")
        self.assertEqual(
            rewritten[1]["command"],
            r'clang++ "D:/worktree/Src/main.cpp" -I D:/worktree/include',
        )

    @patch("context_builder.path_utils._run_git_probe_process")
    def test_rewrite_compile_commands_payload_respects_case_sensitive_override(
        self, mock_run
    ):
        from context_builder.cli import _rewrite_compile_commands_payload
        from context_builder.config import CONFIG, reset_config
        from context_builder.path_utils import clear_path_case_caches

        mock_run.side_effect = OSError("git unavailable")
        reset_config()
        CONFIG["path_case_rules"] = [
            {
                "pattern": r"^C:/repo/case-sensitive(?:/|$)",
                "case_sensitive": True,
            }
        ]
        clear_path_case_caches()
        payload = [{"file": r"c:\REPO\case-sensitive\src\main.cpp"}]

        try:
            rewritten = _rewrite_compile_commands_payload(
                payload,
                r"C:\repo\case-sensitive",
                r"D:\worktree",
            )

            self.assertEqual(
                rewritten[0]["file"],
                r"c:\REPO\case-sensitive\src\main.cpp",
            )
        finally:
            reset_config()

    def test_setup_temp_worktree_rewrites_compile_commands_json(self):
        from context_builder.cli import _setup_temp_worktree

        with tempfile.TemporaryDirectory() as original_cwd, tempfile.TemporaryDirectory() as temp_root:
            temp_worktree_dir = os.path.join(temp_root, "worktree")
            os.makedirs(temp_worktree_dir, exist_ok=True)

            compile_commands_path = os.path.join(original_cwd, "compile_commands.json")
            normalized_original_cwd = original_cwd.replace("\\", "/")
            with open(compile_commands_path, "w", encoding="utf-8") as compile_file:
                json.dump(
                    [
                        {
                            "directory": normalized_original_cwd,
                            "file": os.path.join(original_cwd, "src", "main.cpp"),
                            "command": (
                                f"clang++ -I {normalized_original_cwd}/include "
                                f'"{normalized_original_cwd}/src/main.cpp"'
                            ),
                        }
                    ],
                    compile_file,
                )

            with patch("context_builder.cli.subprocess.run") as mock_sub_run:
                mock_sub_run.return_value = MagicMock(returncode=0, stderr="")
                _setup_temp_worktree(temp_worktree_dir, "sha_end", original_cwd)

            rewritten_path = os.path.join(temp_worktree_dir, "compile_commands.json")
            with open(rewritten_path, encoding="utf-8") as rewritten_file:
                rewritten = json.load(rewritten_file)

            entry = rewritten[0]
            normalized_temp_worktree_dir = temp_worktree_dir.replace("\\", "/")
            self.assertEqual(entry["directory"], normalized_temp_worktree_dir)
            self.assertEqual(
                entry["file"],
                os.path.join(temp_worktree_dir, "src", "main.cpp"),
            )
            self.assertIn(
                f"{normalized_temp_worktree_dir}/src/main.cpp",
                entry["command"],
            )

    @patch("context_builder.cli.shutil.copy")
    def test_rewrite_worktree_compile_commands_falls_back_to_copy_on_invalid_json(
        self, mock_copy
    ):
        from context_builder.cli import _rewrite_worktree_compile_commands

        with tempfile.TemporaryDirectory() as original_cwd, tempfile.TemporaryDirectory() as temp_root:
            compile_commands_path = os.path.join(original_cwd, "compile_commands.json")
            rewritten_path = os.path.join(temp_root, "compile_commands.json")
            with open(compile_commands_path, "w", encoding="utf-8") as compile_file:
                compile_file.write("{ not valid json")

            _rewrite_worktree_compile_commands(
                compile_commands_path,
                rewritten_path,
                original_cwd,
                temp_root,
            )

        mock_copy.assert_called_once_with(compile_commands_path, rewritten_path)

    @patch("context_builder.cli.shutil.copy")
    def test_rewrite_worktree_compile_commands_falls_back_to_copy_on_invalid_utf8(
        self, mock_copy
    ):
        from context_builder.cli import _rewrite_worktree_compile_commands

        with tempfile.TemporaryDirectory() as original_cwd, tempfile.TemporaryDirectory() as temp_root:
            compile_commands_path = os.path.join(original_cwd, "compile_commands.json")
            rewritten_path = os.path.join(temp_root, "compile_commands.json")
            with open(compile_commands_path, "wb") as compile_file:
                compile_file.write(b'[\xff{"file":"main.cpp"}]')

            _rewrite_worktree_compile_commands(
                compile_commands_path,
                rewritten_path,
                original_cwd,
                temp_root,
            )

        mock_copy.assert_called_once_with(compile_commands_path, rewritten_path)

    @patch("context_builder.cli.shutil.copy")
    def test_rewrite_worktree_compile_commands_handles_non_string_roots(
        self, mock_copy
    ):
        from context_builder.cli import _rewrite_worktree_compile_commands

        with tempfile.TemporaryDirectory() as original_cwd, tempfile.TemporaryDirectory() as temp_root:
            compile_commands_path = os.path.join(original_cwd, "compile_commands.json")
            rewritten_path = os.path.join(temp_root, "compile_commands.json")
            with open(compile_commands_path, "w", encoding="utf-8") as compile_file:
                json.dump([{"file": "main.cpp"}], compile_file)

            _rewrite_worktree_compile_commands(
                compile_commands_path,
                rewritten_path,
                None,
                temp_root,
            )

            with open(rewritten_path, encoding="utf-8") as rewritten_file:
                rewritten = json.load(rewritten_file)

        self.assertEqual(rewritten, [{"file": "main.cpp"}])
        mock_copy.assert_not_called()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.get_git_diff_files")
    @patch("context_builder.cli.get_git_tracked_files")
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.get_lsp_references")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_bfs_traversal(
        self, mock_vm_cls, mock_get_lsp, mock_tracer_bounds, mock_cli_bounds,
        mock_run, mock_run_git, mock_git_tracked, mock_git_diff, mock_parse_args
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
        mock_run_git.side_effect = (
            lambda cmd, **kwargs: "@@ -9,1 +10,1 @@\n"
            if "diff" in cmd else ""
        )

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
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_hunk_header_parsing(
        self, mock_vm_cls, mock_bounds, mock_run, mock_run_git, mock_git_tracked,
        mock_git_diff, mock_parse_args
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
        mock_run_git.side_effect = (
            lambda cmd, **kwargs: diff_output if "diff" in cmd else ""
        )

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
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_callees")
    @patch("context_builder.graph_tracer.find_callee_definition")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_callee_depth_bfs_traversal(
        self, mock_vm_cls, mock_find_def, mock_extract_callees,
        mock_tracer_bounds, mock_cli_bounds, mock_run, mock_run_git,
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
        mock_args.caller_depth = 0
        mock_args.callee_depth = 2
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        mock_git_diff.return_value = ["root.py"]
        mock_git_tracked.return_value = ["root.py", "callee1.py", "callee2.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n" if "diff" in cmd else ""
        mock_run_git.side_effect = (
            lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n"
            if "diff" in cmd else ""
        )

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
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.graph_tracer.extract_function_bounds")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_decorator_and_multiline_parsing(
        self, mock_vm_cls, mock_tracer_bounds, mock_cli_bounds, mock_run,
        mock_run_git, mock_git_tracked, mock_git_diff, mock_parse_args
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
        mock_run_git.side_effect = (
            lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n"
            if "diff" in cmd else ""
        )

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
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.run_command")
    @patch("context_builder.cli.extract_function_bounds")
    @patch("context_builder.cli.VolumeManager")
    def test_cli_function_name_extraction_comments_and_strings(
        self, mock_vm_cls, mock_bounds, mock_run, mock_run_git, mock_git_tracked,
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
        mock_args.callee_depth = 0
        mock_args.commit_range = None
        mock_parse_args.return_value = mock_args

        mock_git_diff.return_value = ["root.py"]
        mock_git_tracked.return_value = ["root.py"]
        mock_run.side_effect = lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n" if "diff" in cmd else ""
        mock_run_git.side_effect = (
            lambda cmd, **kwargs: "@@ -1,1 +1,1 @@\n"
            if "diff" in cmd else ""
        )

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
    @patch("context_builder.cli.run_git_process")
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
    @patch("context_builder.cli.run_git_process")
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

    @patch("context_builder.cli.run_git_command")
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

    @patch("context_builder.cli.run_git_command")
    def test_resolve_commit_ref_retries_without_verify(self, mock_run):
        from context_builder.cli import resolve_commit_ref

        mock_run.side_effect = ["", " resolved-sha\n"]

        self.assertEqual(resolve_commit_ref("topic"), "resolved-sha")
        self.assertEqual(
            mock_run.call_args_list[0].args[0],
            ["git", "rev-parse", "--verify", "topic"],
        )
        self.assertEqual(
            mock_run.call_args_list[1].args[0],
            ["git", "rev-parse", "topic"],
        )

    @patch("context_builder.cli.resolve_commit_ref")
    def test_parse_and_resolve_range_relative_and_explicit_formats(
        self, mock_resolve
    ):
        from context_builder.cli import parse_and_resolve_range

        mock_resolve.side_effect = lambda ref: f"sha:{ref}"

        self.assertEqual(
            parse_and_resolve_range("-3"),
            ("sha:HEAD~3", "sha:HEAD"),
        )
        self.assertEqual(
            parse_and_resolve_range("release-2"),
            ("sha:release~2", "sha:release"),
        )
        self.assertEqual(
            parse_and_resolve_range("base..tip"),
            ("sha:base", "sha:tip"),
        )

    @patch("context_builder.cli.get_default_branch", return_value="main")
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.resolve_commit_ref", return_value="start-sha")
    def test_parse_start_plus_count_falls_back_to_default_branch(
        self, _mock_resolve, mock_run, _mock_default
    ):
        from context_builder.cli import parse_and_resolve_range

        mock_run.side_effect = ["next-one\n", "next-one\nnext-two\n"]

        self.assertEqual(
            parse_and_resolve_range("base+2"),
            ("start-sha", "start-sha"),
        )
        self.assertIn("start-sha..main", mock_run.call_args_list[1].args[0])

    @patch("context_builder.cli.get_default_branch", return_value="main")
    @patch("context_builder.cli.run_git_command", return_value="")
    @patch("context_builder.cli.resolve_commit_ref", return_value="start-sha")
    def test_parse_start_plus_count_rejects_insufficient_history(
        self, _mock_resolve, _mock_run, _mock_default
    ):
        from context_builder.cli import parse_and_resolve_range

        with self.assertRaisesRegex(ValueError, "Not enough commits"):
            parse_and_resolve_range("base+2")

    @patch("context_builder.cli.resolve_commit_ref")
    def test_parse_and_resolve_range_rejects_invalid_and_unresolved_refs(
        self, mock_resolve
    ):
        from context_builder.cli import parse_and_resolve_range

        with self.assertRaisesRegex(ValueError, "Invalid commit range"):
            parse_and_resolve_range("single-ref")

        mock_resolve.side_effect = ["start-sha", ""]
        with self.assertRaisesRegex(ValueError, "Could not resolve end commit"):
            parse_and_resolve_range("base..missing")

    @patch("context_builder.cli.run_git_command")
    def test_extract_line_numbers_from_diff_parses_hunks(self, mock_run):
        from context_builder.cli import _extract_line_numbers_from_diff

        mock_run.return_value = (
            "@@ -1,2 +4,3 @@\n"
            "@@ -8 +12 @@\n"
            "@@ malformed @@\n"
        )

        self.assertEqual(
            _extract_line_numbers_from_diff("source.py", "base", "tip"),
            [4, 5, 6, 12],
        )
        self.assertEqual(
            mock_run.call_args.args[0],
            ["git", "diff", "-U0", "base", "tip", "--", "source.py"],
        )

    @patch("context_builder.cli.extract_function_bounds", return_value=(0, 2))
    def test_process_single_diff_line_skips_already_processed_span(
        self, _mock_bounds
    ):
        from context_builder.cli import _process_single_diff_line

        vm = MagicMock()
        queue = deque()
        callee_queue = deque()
        processed = {"source.py::line_0_to_2"}

        _process_single_diff_line(
            "source.py",
            1,
            ["def target():\n", "    pass\n"],
            {},
            processed,
            vm,
            queue,
            callee_queue,
            ["source.py"],
            MagicMock(),
        )

        vm.add_modified_object.assert_not_called()
        self.assertEqual(queue, deque())
        self.assertEqual(callee_queue, deque())

    @patch("context_builder.cli._process_single_diff_line")
    @patch("context_builder.cli.os.path.exists", return_value=False)
    @patch("context_builder.cli._extract_line_numbers_from_diff")
    def test_process_diff_files_skips_unmodified_and_missing_files(
        self, mock_line_numbers, _mock_exists, mock_process
    ):
        from context_builder.cli import _process_diff_files

        mock_line_numbers.side_effect = [[], [3]]
        _process_diff_files(
            ["unchanged.py", "missing.py"],
            None,
            None,
            MagicMock(),
            {},
            set(),
            MagicMock(),
            deque(),
            deque(),
            [],
        )

        mock_process.assert_not_called()

    @patch("context_builder.cli.get_git_diff_files", return_value=[])
    @patch("context_builder.cli.clear_preprocessed_cache")
    @patch("context_builder.cli.get_global_cache")
    def test_run_scan_returns_early_for_clean_workspace(
        self, _mock_cache, mock_clear, _mock_diff
    ):
        from context_builder.cli import run_scan

        args = CliNamespace(
            max_cache_size_mb=10,
            no_language_server=True,
            format="md",
        )

        with patch("builtins.print") as mock_print:
            run_scan(args)

        mock_clear.assert_called_once()
        mock_print.assert_any_call("Workspace is clean.")

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.cleanup_zombie_lsps")
    @patch("context_builder.cli.run_git_process")
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
    @patch("context_builder.cli.run_git_process")
    @patch("shutil.rmtree")
    @patch("context_builder.cli._rewrite_worktree_compile_commands")
    @patch("shutil.copy")
    @patch("os.path.exists")
    def test_cli_worktree_copies_coverage_xml(
        self, mock_exists, mock_copy, mock_rewrite_compile_commands, mock_rmtree, mock_sub_run, mock_cleanup_lsps, mock_run_scan, mock_resolve_range, mock_parse_args
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

        # compile_commands.json is rewritten, coverage.xml is still copied directly
        mock_rewrite_compile_commands.assert_called_once()
        copy_calls = [call[0][0] for call in mock_copy.call_args_list]
        self.assertTrue(any("coverage.xml" in c for c in copy_calls))

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.cleanup_zombie_lsps")
    @patch("context_builder.cli.run_git_process")
    @patch("shutil.rmtree")
    @patch("context_builder.cli._rewrite_worktree_compile_commands")
    @patch("shutil.copy")
    @patch("os.path.exists")
    @patch("os.makedirs", wraps=os.makedirs)
    def test_cli_worktree_copies_build_artifacts(
        self,
        mock_makedirs,
        mock_exists,
        mock_copy,
        mock_rewrite_compile_commands,
        mock_rmtree,
        mock_sub_run,
        mock_cleanup_lsps,
        mock_run_scan,
        mock_resolve_range,
        mock_parse_args,
    ):
        """Verify that compile_commands.json and coverage.xml in build dirs are copied/rewritten to correct relative paths."""
        mock_args = CliNamespace()
        mock_args.commit_range = "-1"
        mock_parse_args.return_value = mock_args
        mock_resolve_range.return_value = ("sha_start", "sha_end")

        def sub_run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and "worktree" in cmd:
                if "add" in cmd:
                    os.makedirs(cmd[4], exist_ok=True)
            res = MagicMock(returncode=0)
            res.stdout = "different_head_sha\n"
            return res
        mock_sub_run.side_effect = sub_run_side_effect

        def exists_side_effect(path):
            normalized = path.replace("\\", "/")
            return "build/compile_commands.json" in normalized or "out/coverage.xml" in normalized
        mock_exists.side_effect = exists_side_effect

        main()

        # Check rewrite is called with the build subdirectory path and target subdirectory path
        mock_rewrite_compile_commands.assert_called_once()
        src_path = mock_rewrite_compile_commands.call_args[0][0]
        dest_path = mock_rewrite_compile_commands.call_args[0][1]
        self.assertTrue(src_path.replace("\\", "/").endswith("build/compile_commands.json"))
        self.assertTrue(dest_path.replace("\\", "/").endswith("build/compile_commands.json"))

        # Check copy is called for coverage.xml
        mock_copy.assert_called_once()
        copy_src = mock_copy.call_args[0][0]
        copy_dest = mock_copy.call_args[0][1]
        self.assertTrue(copy_src.replace("\\", "/").endswith("out/coverage.xml"))
        self.assertTrue(copy_dest.replace("\\", "/").endswith("out/coverage.xml"))

        # Check makedirs is called for both target parent directories
        makedirs_calls = [c[0][0].replace("\\", "/") for c in mock_makedirs.call_args_list]
        self.assertTrue(any(c.endswith("/build") for c in makedirs_calls))
        self.assertTrue(any(c.endswith("/out") for c in makedirs_calls))

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.run_git_command")
    @patch("context_builder.cli.run_git_process")
    def test_worktree_bypassed_if_head_matches_end_sha(
        self, mock_git_process, mock_git_command, mock_run_scan, mock_resolve_range, mock_parse_args
    ):
        """Verify that temporary worktree creation is bypassed if HEAD matches end_sha."""
        mock_args = CliNamespace()
        mock_args.commit_range = "-1"
        mock_parse_args.return_value = mock_args

        # Resolve range returns "sha_end" as the end SHA
        mock_resolve_range.return_value = ("sha_start", "sha_end")

        mock_git_command.return_value = "sha_end\n"

        main()

        # Verify that git worktree add was NOT called
        for call in mock_git_process.call_args_list:
            cmd = call[0][0]
            self.assertFalse("worktree" in cmd and "add" in cmd)

        # Verify that run_scan was called directly
        mock_run_scan.assert_called_once_with(mock_args, start_ref="sha_start", end_ref="sha_end")

    @patch("context_builder.cli._cleanup_temp_worktree")
    @patch("context_builder.cli._setup_temp_worktree")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_git_command")
    def test_worktree_warns_when_lsp_is_enabled(
        self,
        mock_sub_run,
        mock_resolve_range,
        mock_run_scan,
        _mock_setup,
        _mock_cleanup,
    ):
        """Detached worktree scans explain the potential LSP indexing delay."""
        from context_builder.cli import _run_commit_range_worktree

        args = CliNamespace(no_language_server=False)
        mock_resolve_range.return_value = ("start_sha", "end_sha")
        mock_sub_run.return_value = "other_sha\n"

        with patch("context_builder.cli.os.chdir"), patch(
            "builtins.print"
        ) as mock_print:
            _run_commit_range_worktree(args, "start..end")

        output = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("may take several minutes", output)
        self.assertIn("--no-language-server", output)
        mock_run_scan.assert_called_once()
        worktree_args = mock_run_scan.call_args.args[0]
        self.assertEqual(worktree_args.lsp_init_timeout, 120)
        self.assertEqual(worktree_args.lsp_timeout, 300)
        self.assertIsNot(worktree_args, args)
        self.assertIsNone(args.lsp_init_timeout)
        self.assertIsNone(args.lsp_timeout)

    @patch("context_builder.cli._cleanup_temp_worktree")
    @patch("context_builder.cli._setup_temp_worktree")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_git_command")
    def test_worktree_preserves_larger_lsp_timeouts(
        self,
        mock_sub_run,
        mock_resolve_range,
        mock_run_scan,
        _mock_setup,
        _mock_cleanup,
    ):
        from context_builder.cli import _run_commit_range_worktree

        args = CliNamespace(
            no_language_server=False,
            lsp_init_timeout=180,
            lsp_timeout=420,
        )
        mock_resolve_range.return_value = ("start_sha", "end_sha")
        mock_sub_run.return_value = "other_sha\n"

        with patch("context_builder.cli.os.chdir"):
            _run_commit_range_worktree(args, "start..end")

        worktree_args = mock_run_scan.call_args.args[0]
        self.assertEqual(worktree_args.lsp_init_timeout, 180)
        self.assertEqual(worktree_args.lsp_timeout, 420)

    @patch("context_builder.cli._cleanup_temp_worktree")
    @patch("context_builder.cli._setup_temp_worktree")
    @patch("context_builder.cli.run_scan")
    @patch("context_builder.cli.parse_and_resolve_range")
    @patch("context_builder.cli.run_git_command")
    def test_worktree_omits_lsp_warning_when_disabled(
        self,
        mock_sub_run,
        mock_resolve_range,
        mock_run_scan,
        _mock_setup,
        _mock_cleanup,
    ):
        """The indexing warning stays quiet when LSP was explicitly disabled."""
        from context_builder.cli import _run_commit_range_worktree

        args = CliNamespace(no_language_server=True)
        mock_resolve_range.return_value = ("start_sha", "end_sha")
        mock_sub_run.return_value = "other_sha\n"

        with patch("context_builder.cli.os.chdir"), patch(
            "builtins.print"
        ) as mock_print:
            _run_commit_range_worktree(args, "start..end")

        output = "\n".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertNotIn("may take several minutes", output)
        mock_run_scan.assert_called_once()

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

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_git_probe_timeout_mapping(self, mock_run_scan, mock_parse_args):
        """Verify that CLI parameter --git-probe-timeout updates CONFIG."""
        from context_builder.config import CONFIG, reset_config
        reset_config()

        mock_args = CliNamespace()
        mock_args.git_probe_timeout = 12.5
        mock_parse_args.return_value = mock_args

        main()

        self.assertEqual(CONFIG["git_probe_timeout"], 12.5)
        args_passed = mock_run_scan.call_args[0][0]
        self.assertEqual(args_passed.git_probe_timeout, 12.5)

        mock_args.git_probe_timeout = "not_a_float"
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_git_timeout_mapping(self, mock_run_scan, mock_parse_args):
        """Verify that CLI parameter --git-timeout updates CONFIG."""
        from context_builder.config import CONFIG, reset_config
        reset_config()

        mock_args = CliNamespace()
        mock_args.git_timeout = 40.5
        mock_parse_args.return_value = mock_args

        main()

        self.assertEqual(CONFIG["git_timeout"], 40.5)
        args_passed = mock_run_scan.call_args[0][0]
        self.assertEqual(args_passed.git_timeout, 40.5)

        mock_args.git_timeout = "not_a_float"
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_cli_lsp_timeout_mappings(self, mock_run_scan, mock_parse_args):
        from context_builder.config import CONFIG, reset_config

        reset_config()
        mock_args = CliNamespace(
            lsp_init_timeout=72.5,
            lsp_timeout=185.5,
        )
        mock_parse_args.return_value = mock_args

        main()

        self.assertEqual(CONFIG["lsp_init_timeout"], 72.5)
        self.assertEqual(CONFIG["lsp_timeout"], 185.5)
        args_passed = mock_run_scan.call_args.args[0]
        self.assertEqual(args_passed.lsp_init_timeout, 72.5)
        self.assertEqual(args_passed.lsp_timeout, 185.5)
        reset_config()

    @patch("context_builder.cli.argparse.ArgumentParser.parse_args")
    @patch("context_builder.cli.run_scan")
    def test_config_file_lsp_timeout_mappings(
        self, mock_run_scan, mock_parse_args
    ):
        from context_builder.config import CONFIG, reset_config

        reset_config()
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as config_file:
            config_file.write(
                '{"lsp_init_timeout": 80.5, "lsp_timeout": 210.5}'
            )
            config_path = config_file.name

        try:
            mock_parse_args.return_value = CliNamespace(config=config_path)
            main()

            self.assertEqual(CONFIG["lsp_init_timeout"], 80.5)
            self.assertEqual(CONFIG["lsp_timeout"], 210.5)
            args_passed = mock_run_scan.call_args.args[0]
            self.assertEqual(args_passed.lsp_init_timeout, 80.5)
            self.assertEqual(args_passed.lsp_timeout, 210.5)
        finally:
            os.remove(config_path)
            reset_config()
