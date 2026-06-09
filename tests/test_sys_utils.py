import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch
from context_builder.sys_utils import run_command, get_git_diff_files, get_git_tracked_files, ripgrep_filter

class TestSysUtils(unittest.TestCase):
    def test_run_command_success(self):
        output = run_command(["python", "-c", "print('hello')"])
        self.assertEqual(output.strip(), "hello")

    def test_run_command_failure(self):
        output = run_command(["nonexistent_command_12345"])
        self.assertEqual(output, "")

    @patch("context_builder.sys_utils.run_command")
    def test_get_git_diff_files(self, mock_run):
        mock_run.return_value = "file1.py\nfile2.py\n"
        with patch("os.path.exists", return_value=True):
            files = get_git_diff_files()
            self.assertEqual(files, ["file1.py", "file2.py"])

    @patch("context_builder.sys_utils.run_command")
    def test_get_git_tracked_files(self, mock_run):
        mock_run.return_value = "src/a.py\nsrc/b.py\n"
        files = get_git_tracked_files()
        self.assertEqual(files, ["src/a.py", "src/b.py"])

    @patch("subprocess.run")
    def test_ripgrep_filter(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "file1.py\n"
        mock_run.return_value = mock_res

        filtered = ripgrep_filter(["file1.py", "file2.py"], "query")
        self.assertEqual(filtered, ["file1.py"])

    def test_is_in_repo(self):
        import tempfile
        import os
        from context_builder.sys_utils import is_in_repo

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                # Create a file inside repo
                in_repo_file = "file.py"
                with open(in_repo_file, "w") as f:
                    f.write("pass")
                
                # Create a file in an ignored path
                ignored_dir = os.path.join(temp_dir, "node_modules")
                os.makedirs(ignored_dir, exist_ok=True)
                ignored_file = os.path.join(ignored_dir, "lib.js")
                with open(ignored_file, "w") as f:
                    f.write("console.log(1)")
                
                # Check results
                self.assertTrue(is_in_repo(in_repo_file))
                self.assertFalse(is_in_repo("nonexistent.py"))
                self.assertFalse(is_in_repo(ignored_file))
                # Check external file
                self.assertFalse(is_in_repo("/usr/include/stdio.h"))
                
                # Check sibling directory with matching prefix (e.g., project vs project_extra)
                sibling_dir = temp_dir + "_extra"
                os.makedirs(sibling_dir, exist_ok=True)
                sibling_file = os.path.join(sibling_dir, "file.py")
                with open(sibling_file, "w") as f:
                    f.write("pass")
                self.assertFalse(is_in_repo(sibling_file))
            finally:
                os.chdir(old_cwd)
                # Clean up sibling folder
                try:
                    import shutil
                    if 'sibling_dir' in locals() and os.path.exists(sibling_dir):
                        shutil.rmtree(sibling_dir)
                except Exception:
                    pass

    @patch("subprocess.run")
    def test_ripgrep_filter_windows_separator(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        # Simulating Windows backslash output from rg
        mock_res.stdout = "src\\a.py\nsrc\\b.py\n"
        mock_run.return_value = mock_res

        files = ["src/a.py", "src/b.py", "src/c.py"]
        filtered = ripgrep_filter(files, "query")
        
        self.assertEqual(filtered, ["src/a.py", "src/b.py"])

    def test_exit_on_fail_prints_error_before_exit(self):
        """When exit_on_fail=True, a helpful error message (with the command and
        stderr) must be printed before sys.exit(1) is called so that users
        running outside a git repo (or with missing permissions) can diagnose the
        problem without grepping logs."""
        import subprocess
        error = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "ls-files"],
            stderr="fatal: not a git repository (or any of the parent directories): .git"
        )
        with patch("subprocess.run", side_effect=error), \
             patch("sys.stdout", new_callable=StringIO) as mock_out, \
             self.assertRaises(SystemExit) as ctx:
            run_command(["git", "ls-files"], exit_on_fail=True)

        # Should exit with code 1
        self.assertEqual(ctx.exception.code, 1)

        # Should have printed both the command and the reason
        output = mock_out.getvalue()
        self.assertIn("git ls-files", output)
        self.assertIn("fatal: not a git repository", output)

    def test_get_comment_prefix(self):
        from context_builder.sys_utils import get_comment_prefix
        self.assertEqual(get_comment_prefix("main.py"), "#")
        self.assertEqual(get_comment_prefix("script.sh"), "#")
        self.assertEqual(get_comment_prefix("Makefile"), "#")
        self.assertEqual(get_comment_prefix("run.bat"), "REM")
        self.assertEqual(get_comment_prefix("main.cpp"), "//")
        self.assertEqual(get_comment_prefix("main.rs"), "//")

    @patch("subprocess.run")
    def test_run_command_file_not_found_exit(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        with patch("sys.stdout", new_callable=StringIO) as mock_out, \
             self.assertRaises(SystemExit) as ctx:
            run_command(["nonexistent_binary"], exit_on_fail=True)
            
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Executable not found: nonexistent_binary", mock_out.getvalue())

    @patch("subprocess.run")
    def test_ripgrep_filter_regex_alternation(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "file1.py\n"
        mock_run.return_value = mock_res

        filtered = ripgrep_filter(["file1.py", "file2.py"], "query|another", fixed_strings=False)
        self.assertEqual(filtered, ["file1.py"])
        # Verify that "-F" was NOT in the command arguments
        cmd_args = mock_run.call_args[0][0]
        self.assertNotIn("-F", cmd_args)

    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_timeout_warning(self, mock_warn, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["rg"], timeout=5)

        files = ["file1.py", "file2.py"]
        filtered = ripgrep_filter(files, "query")

        # Must fall back to returning all input files
        self.assertEqual(filtered, files)
        # Verify warning was issued
        mock_warn.assert_any_call(
            "ripgrep_timeout",
            unittest.mock.ANY
        )
        # Verify the warning contains the word "timed out" and adjustment options
        warn_msg = [c[0][1] for c in mock_warn.call_args_list if c[0][0] == "ripgrep_timeout"][0]
        self.assertIn("timed out", warn_msg)
        self.assertIn("--ripgrep-timeout", warn_msg)

    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_unexpected_fail_warning(self, mock_warn, mock_run):
        mock_run.side_effect = RuntimeError("Something bad happened")

        files = ["file1.py", "file2.py"]
        filtered = ripgrep_filter(files, "query")

        # Must fall back to returning all input files
        self.assertEqual(filtered, files)
        # Verify warning was issued
        mock_warn.assert_any_call(
            "ripgrep_fail",
            unittest.mock.ANY
        )

    @patch("subprocess.run")
    def test_ripgrep_filter_respects_configured_timeout(self, mock_run):
        from context_builder.config import CONFIG
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        # Save and set config timeout
        old_timeout = CONFIG.get("ripgrep_timeout", 10)
        CONFIG["ripgrep_timeout"] = 25
        try:
            ripgrep_filter(["file1.py"], "query")
            # Verify subprocess.run was called with timeout=25
            self.assertTrue(mock_run.called)
            kwargs = mock_run.call_args[1]
            self.assertEqual(kwargs.get("timeout"), 25)
        finally:
            CONFIG["ripgrep_timeout"] = old_timeout

    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_invalid_timeout_fallback(self, mock_warn, mock_run):
        from context_builder.config import CONFIG
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        old_timeout = CONFIG.get("ripgrep_timeout", 10)
        try:
            # Test negative timeout
            CONFIG["ripgrep_timeout"] = -5
            mock_warn.reset_mock()
            ripgrep_filter(["file1.py"], "query")
            self.assertEqual(mock_run.call_args[1].get("timeout"), 10)
            mock_warn.assert_any_call("ripgrep_timeout_invalid", unittest.mock.ANY)

            # Test boolean timeout
            CONFIG["ripgrep_timeout"] = True
            mock_warn.reset_mock()
            ripgrep_filter(["file1.py"], "query")
            self.assertEqual(mock_run.call_args[1].get("timeout"), 10)
            mock_warn.assert_any_call("ripgrep_timeout_invalid", unittest.mock.ANY)

            # Test string timeout
            CONFIG["ripgrep_timeout"] = "invalid_string"
            mock_warn.reset_mock()
            ripgrep_filter(["file1.py"], "query")
            self.assertEqual(mock_run.call_args[1].get("timeout"), 10)
            mock_warn.assert_any_call("ripgrep_timeout_invalid", unittest.mock.ANY)
        finally:
            CONFIG["ripgrep_timeout"] = old_timeout

    @patch("subprocess.run")
    def test_ripgrep_filter_empty_files_early_exit(self, mock_run):
        # Empty input files list should return immediately without calling subprocess
        filtered = ripgrep_filter([], "query")
        self.assertEqual(filtered, [])
        self.assertFalse(mock_run.called)

