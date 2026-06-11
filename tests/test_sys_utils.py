import os
import subprocess
import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch
from context_builder.sys_utils import (
    iter_scan_progress,
    run_command,
    get_git_diff_files,
    get_git_tracked_files,
    ripgrep_filter,
)

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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("subprocess.run")
    def test_ripgrep_filter_searches_external_absolute_files(self, mock_run):
        """Absolute candidates outside the working tree are passed directly to ripgrep."""
        external_file = os.path.abspath(os.path.join(os.path.dirname(os.getcwd()), "file.py"))
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = external_file + "\n"
        mock_run.return_value = mock_res

        filtered = ripgrep_filter([external_file], "query")

        self.assertEqual(filtered, [external_file])
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[-2:], ["--", external_file])

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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

    @patch("context_builder.sys_utils.HAS_RG", True)
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

            # Test NaN timeout — float('nan') > 0 is False, so not (nan > 0) correctly rejects it
            CONFIG["ripgrep_timeout"] = float("nan")
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

    @patch("context_builder.sys_utils.run_command")
    @patch("context_builder.sys_utils.warn_once")
    def test_has_rg_checker_missing(self, mock_warn, mock_run):
        """Verify that when ripgrep is missing, HAS_RG evaluates to False and warns."""
        # Force re-evaluation by resetting cached value
        from context_builder.sys_utils import HAS_RG  # pylint: disable=import-outside-toplevel
        HAS_RG._has_rg = None  # pylint: disable=protected-access
        mock_run.return_value = ""  # rg not found/executable failed

        self.assertFalse(bool(HAS_RG))
        mock_run.assert_called_once_with(["rg", "--version"], timeout=5.0)
        mock_warn.assert_called_once_with("ripgrep_missing", unittest.mock.ANY)

    @patch("context_builder.sys_utils.run_command")
    @patch("context_builder.sys_utils.warn_once")
    def test_has_rg_checker_present(self, mock_warn, mock_run):
        """Verify that when ripgrep is present, HAS_RG evaluates to True and does not warn."""
        # Force re-evaluation by resetting cached value
        from context_builder.sys_utils import HAS_RG  # pylint: disable=import-outside-toplevel
        HAS_RG._has_rg = None  # pylint: disable=protected-access
        mock_run.return_value = "ripgrep 14.1.0"

        self.assertTrue(bool(HAS_RG))
        mock_run.assert_called_once_with(["rg", "--version"], timeout=5.0)
        mock_warn.assert_not_called()

    @patch("context_builder.sys_utils.run_command")
    @patch("context_builder.sys_utils.warn_once")
    def test_has_rg_checker_exception_safety(self, mock_warn, mock_run):
        """Verify that HAS_RG evaluates to False and warns if run_command raises an exception."""
        # Force re-evaluation by resetting cached value
        from context_builder.sys_utils import HAS_RG  # pylint: disable=import-outside-toplevel
        HAS_RG._has_rg = None  # pylint: disable=protected-access
        mock_run.side_effect = PermissionError("Access Denied")

        self.assertFalse(bool(HAS_RG))
        mock_warn.assert_called_once_with("ripgrep_missing", unittest.mock.ANY)

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_unexpected_exit_code_warning(self, mock_warn, mock_run):
        """Verify that any non-zero/non-one return code triggers the ripgrep_error warning."""
        mock_res = MagicMock()
        mock_res.returncode = 2
        mock_res.stderr = "Some internal rg error"
        mock_run.return_value = mock_res

        files = ["file1.py", "file2.py"]
        filtered = ripgrep_filter(files, "query")

        # Must fall back to returning all input files
        self.assertEqual(filtered, files)
        mock_warn.assert_any_call(
            "ripgrep_error",
            unittest.mock.ANY
        )
        warn_msg = [c[0][1] for c in mock_warn.call_args_list if c[0][0] == "ripgrep_error"][0]
        self.assertIn("unexpected return code 2", warn_msg)
        self.assertIn("Some internal rg error", warn_msg)

    @patch("context_builder.sys_utils.HAS_RG", False)
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_fallback_hint_no_rg(self, mock_warn):
        """When HAS_RG is False and fallback_hint is provided, the fallback warning is printed."""
        files = ["a.py", "b.py"]
        result = ripgrep_filter(files, "my_func", fallback_hint="callers of 'my_func'")
        self.assertEqual(result, files)
        keys_warned = [c[0][0] for c in mock_warn.call_args_list]
        self.assertIn("ripgrep_fallback", keys_warned)
        hint_msg = [c[0][1] for c in mock_warn.call_args_list
                    if c[0][0] == "ripgrep_fallback"][0]
        self.assertIn("callers of 'my_func'", hint_msg)
        self.assertTrue(result.used_ripgrep_fallback)
        self.assertEqual(result.fallback_label, "callers of 'my_func'")

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_fallback_hint_on_timeout(self, mock_warn, mock_run):
        """When rg times out and fallback_hint is provided, the fallback warning is printed."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["rg"], timeout=10)
        files = ["a.py", "b.py"]
        result = ripgrep_filter(files, "my_func", fallback_hint="callers of 'my_func'")
        self.assertEqual(result, files)
        keys_warned = [c[0][0] for c in mock_warn.call_args_list]
        self.assertIn("ripgrep_timeout", keys_warned)
        self.assertIn("ripgrep_fallback", keys_warned)

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_fallback_hint_on_error(self, mock_warn, mock_run):
        """When rg exits unexpectedly and fallback_hint is provided, the fallback warning fires."""
        mock_res = MagicMock()
        mock_res.returncode = 5
        mock_res.stderr = "something went wrong"
        mock_run.return_value = mock_res
        files = ["a.py", "b.py"]
        result = ripgrep_filter(files, "my_func", fallback_hint="callers of 'my_func'")
        self.assertEqual(result, files)
        keys_warned = [c[0][0] for c in mock_warn.call_args_list]
        self.assertIn("ripgrep_error", keys_warned)
        self.assertIn("ripgrep_fallback", keys_warned)

    @patch("context_builder.sys_utils.HAS_RG", False)
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_no_hint_no_extra_warning(self, mock_warn):
        """When fallback_hint is not provided, no fallback warning is emitted (silent fallback)."""
        files = ["a.py", "b.py"]
        result = ripgrep_filter(files, "my_func")
        self.assertEqual(result, files)
        # Only the ripgrep_missing warn from HAS_RG.__bool__ may fire; no ripgrep_fallback key
        fallback_keys = [c[0][0] for c in mock_warn.call_args_list
                         if c[0][0] == "ripgrep_fallback"]
        self.assertEqual(fallback_keys, [])

    @patch("context_builder.sys_utils.HAS_RG", False)
    @patch("context_builder.sys_utils.warn_once")
    def test_iter_scan_progress_reports_fallback_scan(self, _mock_warn):
        """Fallback scans emit progress for long exhaustive file walks."""
        files = [f"file_{idx}.py" for idx in range(120)]
        result = ripgrep_filter(files, "my_func", fallback_hint="callers of 'my_func'")

        with patch("sys.stderr", new_callable=StringIO) as mock_err:
            scanned = list(iter_scan_progress(result, min_files=100))

        self.assertEqual(scanned, files)
        output = mock_err.getvalue()
        self.assertIn("[Scanning 1/120]", output)
        self.assertIn("callers of 'my_func'", output)

    def test_iter_scan_progress_stays_quiet_for_fast_path_results(self):
        """Regular filtered lists do not emit progress unless explicitly forced."""
        files = [f"file_{idx}.py" for idx in range(120)]

        with patch("sys.stderr", new_callable=StringIO) as mock_err:
            scanned = list(iter_scan_progress(files, label="fast path", min_files=100))

        self.assertEqual(scanned, files)
        self.assertEqual(mock_err.getvalue(), "")

    def test_iter_scan_progress_early_close_does_not_show_complete(self):
        """Closing a progress iterator early does not report 100 percent completion."""
        files = [f"file_{idx}.py" for idx in range(120)]

        class TtyBuffer(StringIO):
            """String buffer that behaves like an interactive terminal."""

            def isatty(self):
                return True

        with patch("sys.stderr", new_callable=TtyBuffer) as mock_err:
            progress_iter = iter_scan_progress(
                files,
                label="early exit",
                min_files=100,
                force=True,
            )
            self.assertEqual(next(progress_iter), "file_0.py")
            progress_iter.close()

        output = mock_err.getvalue()
        self.assertNotIn("100%", output)
        self.assertTrue(output.endswith("\n"))

    def test_iter_scan_progress_zero_total_can_be_forced(self):
        """Empty scans stay inactive even when min_files would otherwise enable progress."""
        with patch("sys.stderr", new_callable=StringIO) as mock_err:
            scanned = list(iter_scan_progress([], label="empty", min_files=0, force=True))

        self.assertEqual(scanned, [])
        self.assertEqual(mock_err.getvalue(), "")
