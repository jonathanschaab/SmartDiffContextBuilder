import unittest
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
