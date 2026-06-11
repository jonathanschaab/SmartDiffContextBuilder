# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,consider-using-with,line-too-long

import os
import subprocess
import tempfile
import unittest

class TestCommitRangeIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        # Initialize a real git repository
        subprocess.run(["git", "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "ci-test@example.com"], check=True)
        subprocess.run(["git", "config", "user.name", "CI Test User"], check=True)

        # We will create 3 commits (A, B, C)
        # Commit A: Add file.py with hello()
        with open("file.py", "w", encoding="utf-8") as f:
            f.write("def hello():\n    print('A')\n")
        subprocess.run(["git", "add", "file.py"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "Commit A"], check=True, stdout=subprocess.DEVNULL)
        self.commit_a = subprocess.run(["git", "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

        # Commit B: Modify hello() to print B
        with open("file.py", "w", encoding="utf-8") as f:
            f.write("def hello():\n    print('B')\n")
        subprocess.run(["git", "add", "file.py"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "Commit B"], check=True, stdout=subprocess.DEVNULL)
        self.commit_b = subprocess.run(["git", "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

        # Commit C: Modify hello() to print C and add world()
        with open("file.py", "w", encoding="utf-8") as f:
            f.write("def hello():\n    print('C')\n\ndef world():\n    print('world')\n")
        subprocess.run(["git", "add", "file.py"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "Commit C"], check=True, stdout=subprocess.DEVNULL)
        self.commit_c = subprocess.run(["git", "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

        # Reset HEAD back to Commit B so the working directory has some modifications
        # (this tests that our tool will look at the worktree instead of the current dirty state, or that the current HEAD can be different)
        # Actually, let's keep it clean or make a modification on HEAD to make sure worktree handles detached commits cleanly.
        self.script_path = os.path.join(self.old_cwd, "smart_diff_context_builder.py")

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()
        # Ensure no dangling worktrees in the host system git repository or test directory.
        # But since the git repo was in the temp directory, it will be deleted.

    def test_range_dots(self):
        # Format: CommitA..CommitC
        # We diff Commit C against Commit A.
        # Commit A is print('A'). Commit C is print('C') + world().
        # Let's run with --commit-range CommitA..CommitC
        res = subprocess.run(
            ["python", self.script_path, "--base-name", "LensDots", "--no-language-server", "--commit-range", f"{self.commit_a}..{self.commit_c}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")

        output_file = "LensDots_final.md"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8") as f:
            payload = f.read()

        # The diff is between A and C.
        self.assertIn("print('C')", payload)
        self.assertIn("def world()", payload)
        self.assertNotIn("print('B')", payload)  # Commit B intermediate states might not be explicitly diffed, but C has C.

        # Verify no temporary worktree leaked
        worktree_list = subprocess.run(["git", "worktree", "list"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertNotIn("smdc_worktree_", worktree_list)

    def test_range_minus_relative(self):
        # Format: -2 (from HEAD~2, which is Commit A, to HEAD, which is Commit C)
        res = subprocess.run(
            ["python", self.script_path, "--base-name", "LensMinus", "--no-language-server", "--commit-range", "-2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")

        output_file = "LensMinus_final.md"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8") as f:
            payload = f.read()

        self.assertIn("print('C')", payload)
        self.assertIn("def world()", payload)

        worktree_list = subprocess.run(["git", "worktree", "list"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertNotIn("smdc_worktree_", worktree_list)

    def test_range_plus_relative(self):
        # Format: CommitA+2 (from Commit A, plus 2 chronological commits, which lands on Commit C)
        res = subprocess.run(
            ["python", self.script_path, "--base-name", "LensPlus", "--no-language-server", "--commit-range", f"{self.commit_a}+2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")

        output_file = "LensPlus_final.md"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8") as f:
            payload = f.read()

        self.assertIn("print('C')", payload)
        self.assertIn("def world()", payload)

        worktree_list = subprocess.run(["git", "worktree", "list"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertNotIn("smdc_worktree_", worktree_list)

    def test_range_end_minus_relative(self):
        # Format: CommitC-2 (Commit C minus 2 commits, starts at Commit A, ends at Commit C)
        res = subprocess.run(
            ["python", self.script_path, "--base-name", "LensEndMinus", "--no-language-server", "--commit-range", f"{self.commit_c}-2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")

        output_file = "LensEndMinus_final.md"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8") as f:
            payload = f.read()

        self.assertIn("print('C')", payload)
        self.assertIn("def world()", payload)

        worktree_list = subprocess.run(["git", "worktree", "list"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertNotIn("smdc_worktree_", worktree_list)

    def test_invalid_range_handling(self):
        # Invalid format or unresolved commit ref
        res = subprocess.run(
            ["python", self.script_path, "--commit-range", "non_existent_ref..HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Invalid commit range", res.stdout)
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,consider-using-with,line-too-long
