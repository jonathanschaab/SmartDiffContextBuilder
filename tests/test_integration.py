# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,consider-using-with,line-too-long

import os
import subprocess
import tempfile
import unittest

class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        # 1. Initialize a real git repository
        subprocess.run(["git", "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "ci-test@example.com"], check=True)
        subprocess.run(["git", "config", "user.name", "CI Test User"], check=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_end_to_end_context_extraction(self):
        # 2. Create sample source file
        app_code = (
            "def greet():\n"
            "    print('hello')\n"
            "\n"
            "def run_app():\n"
            "    greet()\n"
        )
        with open("app.py", "w", encoding="utf-8") as f:
            f.write(app_code)

        # 3. Commit it to git
        subprocess.run(["git", "add", "app.py"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "Initial commit"], check=True, stdout=subprocess.DEVNULL)

        # 4. Modify the file to create a diff
        modified_code = (
            "def greet():\n"
            "    print('hello world')\n" # Modified line
            "\n"
            "def run_app():\n"
            "    greet()\n"
        )
        with open("app.py", "w", encoding="utf-8") as f:
            f.write(modified_code)

        # 5. Invoke SmartDiffContextBuilder via subprocess
        # Pass the path to the main script wrapper
        script_path = os.path.join(self.old_cwd, "smart_diff_context_builder.py")

        # Run with default settings (caller-depth = 1)
        res = subprocess.run(
            ["python", script_path, "--base-name", "LensIntegration", "--no-language-server"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        # Ensure command completed successfully
        self.assertEqual(res.returncode, 0, f"Script failed: {res.stderr}")

        # 6. Validate the generated payload
        output_file = "LensIntegration_final.md"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8") as f:
            payload = f.read()

        # Verify crucial sections and names are present
        self.assertIn("# LLM Context Payload", payload)
        self.assertIn("## 1. Raw Diff", payload)
        self.assertIn("## 2. Modified Core Logic", payload)
        self.assertIn("greet()", payload)
        self.assertIn("print('hello world')", payload)

        # Since greet() is called by run_app() at distance 1, run_app()'s call site should show up as a caller
        self.assertIn("run_app()", payload)
        self.assertIn("- `app.py` (L5, Distance 1)", payload)

    def test_end_to_end_caller_depth_zero(self):
        # 2. Create sample source file
        app_code = (
            "def greet():\n"
            "    print('hello')\n"
            "\n"
            "def run_app():\n"
            "    greet()\n"
        )
        with open("app.py", "w", encoding="utf-8") as f:
            f.write(app_code)

        # 3. Commit it to git
        subprocess.run(["git", "add", "app.py"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", "Initial commit"], check=True, stdout=subprocess.DEVNULL)

        # 4. Modify the file to create a diff
        modified_code = (
            "def greet():\n"
            "    print('hello world')\n" # Modified line
            "\n"
            "def run_app():\n"
            "    greet()\n"
        )
        with open("app.py", "w", encoding="utf-8") as f:
            f.write(modified_code)

        # 5. Invoke SmartDiffContextBuilder with --caller-depth 0
        script_path = os.path.join(self.old_cwd, "smart_diff_context_builder.py")
        res = subprocess.run(
            ["python", script_path, "--base-name", "LensDepthZero", "--no-language-server", "--caller-depth", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0)

        # 6. Validate the output file
        output_file = "LensDepthZero_final.md"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r", encoding="utf-8") as f:
            payload = f.read()

        # Core changes are still included
        self.assertIn("greet()", payload)
        self.assertIn("print('hello world')", payload)

        # Callers are omitted because caller-depth = 0
        self.assertNotIn("- `app.py` (L5, Distance 1)", payload)
