# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,protected-access
# pylint: disable=redefined-outer-name,reimported,unused-argument,consider-using-from-import
# pylint: disable=unspecified-encoding,too-few-public-methods,too-many-public-methods
# pylint: disable=broad-exception-caught

import os
import subprocess
import tempfile
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch
import context_builder.sys_utils as sys_utils
from context_builder.sys_utils import (
    iter_scan_progress,
    run_git_process,
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

    @patch("subprocess.run")
    def test_run_git_process_sets_non_interactive_env_and_timeout(self, mock_run):
        from context_builder.config import CONFIG

        mock_run.return_value = MagicMock(returncode=0)
        old_timeout = CONFIG.get("git_timeout", 30.0)
        CONFIG["git_timeout"] = 42
        try:
            run_git_process(["git", "status"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        finally:
            CONFIG["git_timeout"] = old_timeout

        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 42)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GCM_INTERACTIVE"], "never")

    @patch("context_builder.sys_utils.warn_once")
    @patch("subprocess.run")
    def test_run_git_process_timeout_warns_with_adjustment_help(self, mock_run, mock_warn):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=30)

        result = run_git_process(["git", "status"])

        self.assertIsNone(result)
        mock_warn.assert_called_once()
        self.assertEqual(mock_warn.call_args.args[0], "git_timeout")
        self.assertIn("--git-timeout", mock_warn.call_args.args[1])
        self.assertIn("'git_timeout'", mock_warn.call_args.args[1])

    @patch("context_builder.sys_utils.warn_once")
    @patch("subprocess.run")
    def test_run_git_process_invalid_timeout_warns_and_uses_default(self, mock_run, mock_warn):
        from context_builder.config import CONFIG

        mock_run.return_value = MagicMock(returncode=0)
        old_timeout = CONFIG.get("git_timeout", 30.0)
        CONFIG["git_timeout"] = "bad"
        try:
            run_git_process(["git", "status"])
        finally:
            CONFIG["git_timeout"] = old_timeout

        self.assertEqual(mock_warn.call_args.args[0], "git_timeout_invalid")
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 30.0)

    @patch("context_builder.sys_utils.run_git_command")
    def test_run_command_routes_git_through_git_helper(self, mock_git_command):
        mock_git_command.return_value = "ok"

        output = run_command(["git", "status"])

        self.assertEqual(output, "ok")
        mock_git_command.assert_called_once_with(
            ["git", "status"],
            exit_on_fail=False,
            timeout=None,
        )

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
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[-3:], ["--", "file1.py", "file2.py"])
        self.assertNotIn(".", cmd)

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

    @patch("context_builder.sys_utils.detect_root_case_sensitivity", return_value=True)
    def test_is_in_repo_honors_case_sensitive_root(self, _mock_case_sensitive):
        from context_builder.config import reset_config
        from context_builder.sys_utils import is_in_repo

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                reset_config()

                in_repo_file = "file.py"
                with open(in_repo_file, "w") as f:
                    f.write("pass")

                mismatched_root = temp_dir.swapcase()
                if mismatched_root == temp_dir:
                    mismatched_root = temp_dir.upper()
                mismatched_case_path = os.path.join(mismatched_root, "file.py")
                self.assertFalse(is_in_repo(mismatched_case_path))
            finally:
                reset_config()
                os.chdir(old_cwd)

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

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("subprocess.run")
    def test_ripgrep_filter_searches_external_relative_files(self, mock_run):
        """Relative candidates escaping the working tree are passed directly to ripgrep."""
        external_file = os.path.join("..", "sibling", "file.py")
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = external_file + "\n"
        mock_run.return_value = mock_res

        filtered = ripgrep_filter([external_file], "query")

        self.assertEqual(filtered, [external_file])
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[-2:], ["--", external_file])

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("context_builder.sys_utils._build_rg_file_batches")
    @patch("subprocess.run")
    def test_ripgrep_filter_merges_explicit_file_batches(
        self, mock_run, mock_batches
    ):
        """Matches from multiple explicit-file batches are merged in input order."""
        files = ["one.py", "two.py", "three.py"]
        mock_batches.return_value = [["one.py", "two.py"], ["three.py"]]
        first_res = MagicMock(returncode=0, stdout="two.py\n")
        second_res = MagicMock(returncode=0, stdout="three.py\n")
        mock_run.side_effect = [first_res, second_res]

        filtered = ripgrep_filter(files, "query")

        self.assertEqual(filtered, ["two.py", "three.py"])
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(
            mock_run.call_args_list[0].args[0][-3:],
            ["--", "one.py", "two.py"],
        )
        self.assertEqual(
            mock_run.call_args_list[1].args[0][-2:],
            ["--", "three.py"],
        )

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("subprocess.run")
    def test_ripgrep_filter_replaces_undecodable_output(self, mock_run):
        """Undecodable path bytes do not force an exhaustive fallback scan."""
        replacement_path = "legacy-\ufffd.cpp"

        def decoding_sensitive_run(*_args, **kwargs):
            self.assertEqual(kwargs.get("errors"), "replace")
            return MagicMock(
                returncode=0,
                stdout=replacement_path + "\n",
                stderr="",
            )

        mock_run.side_effect = decoding_sensitive_run

        filtered = ripgrep_filter([replacement_path, "other.cpp"], "query")

        self.assertEqual(filtered, [replacement_path])
        self.assertFalse(
            getattr(filtered, "used_ripgrep_fallback", False),
        )

    @patch("context_builder.sys_utils.HAS_RG", True)
    @patch("context_builder.sys_utils._build_rg_file_batches")
    @patch("subprocess.run")
    @patch("context_builder.sys_utils.warn_once")
    def test_ripgrep_filter_later_batch_error_falls_back(
        self, mock_warn, mock_run, mock_batches
    ):
        """A later batch error discards partial matches and scans all candidates."""
        files = ["one.py", "two.py"]
        mock_batches.return_value = [["one.py"], ["two.py"]]
        first_res = MagicMock(returncode=0, stdout="one.py\n")
        second_res = MagicMock(returncode=2, stdout="", stderr="batch failed")
        mock_run.side_effect = [first_res, second_res]

        filtered = ripgrep_filter(files, "query", fallback_hint="callers")

        self.assertEqual(filtered, files)
        mock_warn.assert_any_call("ripgrep_error", unittest.mock.ANY)
        mock_warn.assert_any_call("ripgrep_fallback", unittest.mock.ANY)

    def test_build_rg_file_batches_respects_character_budget(self):
        """File batches split based on command length rather than file count."""
        files = ["a.py", "longer-name.py", "third.py"]
        base_cmd = ["rg", "-l", "-F", "query"]
        single_batch_length = len(subprocess.list2cmdline(base_cmd + ["--", files[0]]))
        batches = sys_utils._build_rg_file_batches(
            base_cmd,
            files,
            max_chars=single_batch_length + 1,
        )

        self.assertEqual(batches, [["a.py"], ["longer-name.py"], ["third.py"]])

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
        self.assertTrue(output.rstrip().endswith("[Scanning 120/120]  callers of 'my_func'"))

    def test_iter_scan_progress_preserves_iterable_fallback_label(self):
        """Materializing a custom iterable does not discard progress metadata."""
        class FallbackIterable:
            """Non-list fallback candidates carrying a progress label."""

            fallback_label = "custom fallback scan"

            def __iter__(self):
                return iter(["one.py", "two.py"])

        with patch("sys.stderr", new_callable=StringIO) as mock_err:
            scanned = list(iter_scan_progress(FallbackIterable(), min_files=1))

        self.assertEqual(scanned, ["one.py", "two.py"])
        self.assertIn("custom fallback scan", mock_err.getvalue())

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

    def test_iter_scan_progress_handles_raising_isatty(self):
        """Custom stderr wrappers cannot crash progress initialization."""
        class RaisingStream(StringIO):
            """Stream wrapper whose terminal probe is unsupported."""

            def isatty(self):
                raise RuntimeError("terminal state unavailable")

        with patch("sys.stderr", new_callable=RaisingStream) as mock_err:
            scanned = list(
                iter_scan_progress(
                    ["one.py", "two.py"],
                    label="wrapped stream",
                    min_files=1,
                    force=True,
                )
            )

        self.assertEqual(scanned, ["one.py", "two.py"])
        self.assertIn("[Scanning 2/2]", mock_err.getvalue())

    def test_stream_is_tty_handles_missing_method(self):
        """Streams without isatty are treated as non-interactive."""
        self.assertFalse(sys_utils._stream_is_tty(object()))

    def test_normalized_search_paths_are_cached_per_cwd(self):
        """Repeated path normalization avoids repeated abspath work."""
        sys_utils._NORMALIZED_PATH_CACHE.clear()  # pylint: disable=protected-access
        actual_abspath = os.path.abspath
        cwd = os.path.normcase(actual_abspath(os.getcwd()))
        expected_input = os.path.join(cwd, "src/a.py")

        with patch(
            "context_builder.sys_utils.os.path.abspath",
            wraps=actual_abspath,
        ) as mock_abspath:
            first = sys_utils._normalize_search_result("src/a.py", cwd)
            second = sys_utils._normalize_search_result("src/a.py", cwd)

        self.assertEqual(first, second)
        normalized_calls = [
            call for call in mock_abspath.call_args_list
            if call.args == (expected_input,)
        ]
        self.assertEqual(len(normalized_calls), 1)

    def test_normalized_search_paths_resolve_relative_to_supplied_cwd(self):
        """Relative results use the search cwd, not the process cwd."""
        sys_utils._NORMALIZED_PATH_CACHE.clear()  # pylint: disable=protected-access
        search_cwd = os.path.abspath(os.path.join(os.getcwd(), "search-root"))
        expected = os.path.normcase(
            os.path.abspath(os.path.join(search_cwd, "src", "a.py"))
        ).replace("\\", "/")

        with tempfile.TemporaryDirectory() as process_cwd:
            old_cwd = os.getcwd()
            try:
                os.chdir(process_cwd)
                normalized = sys_utils._normalize_search_result(
                    os.path.join("src", "a.py"),
                    search_cwd,
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(normalized, expected)
