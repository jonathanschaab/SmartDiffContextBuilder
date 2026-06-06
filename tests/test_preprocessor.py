import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import json
from context_builder.cache import LRUFileCache
from context_builder.preprocessor import (
    analyze_compile_commands,
    build_ffi_registry,
    trace_ffi_callers,
    trace_macro_expansion
)

class TestPreprocessor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    def test_analyze_compile_commands(self):
        # Create compile_commands.json
        db = [
            {
                "directory": ".",
                "command": "clang++ -c main.cpp",
                "file": "main.cpp"
            },
            {
                "directory": ".",
                "command": "clang++ -c other.cpp",
                "file": "other.cpp"
            }
        ]
        with open("compile_commands.json", "w") as f:
            json.dump(db, f)

        # Create other.cpp that includes main.h
        with open("other.cpp", "w") as f:
            f.write('#include "main.h"\n')

        # Target file is main.h.
        # It should link to main.cpp (base name match) and other.cpp (include match).
        callers = analyze_compile_commands("main.h")
        self.assertIn("main.cpp", callers)
        self.assertIn("other.cpp", callers)
        self.assertEqual(callers["main.cpp"][0]["code"], "// [Compilation Link via compile_commands.json]")

    def test_analyze_compile_commands_precise_include(self):
        # Create compile_commands.json
        db = [
            {
                "directory": ".",
                "command": "clang++ -c other.cpp",
                "file": "other.cpp"
            }
        ]
        with open("compile_commands.json", "w") as f:
            json.dump(db, f)

        # Create other.cpp that includes main_header.h, but we are looking for a.h
        with open("other.cpp", "w") as f:
            f.write('#include "main_header.h"\n')

        # Target file is a.h. It should NOT match because "a.h" is a substring of "main_header.h" but not the exact include.
        callers = analyze_compile_commands("a.h")
        self.assertNotIn("other.cpp", callers)

    def test_analyze_compile_commands_relative_paths(self):
        import shutil
        os.makedirs("build", exist_ok=True)
        os.makedirs("src", exist_ok=True)
        
        db = [
            {
                "directory": os.path.abspath("build"),
                "command": "clang++ -c ../src/main.cpp",
                "file": "../src/main.cpp"
            }
        ]
        with open("compile_commands.json", "w") as f:
            json.dump(db, f)

        # Target file is src/main.h. Since ../src/main.cpp resolves to src/main.cpp,
        # it should link correctly by matching base name main.cpp to main.h.
        callers = analyze_compile_commands("src/main.h")
        self.assertIn("src/main.cpp", callers)


    def test_build_ffi_registry_and_trace(self):
        # Create a file with FFI exports using arbitrary return type
        code = (
            "#[no_mangle]\n"
            "pub extern \"C\" fn export_rust_func() {}\n"
            "extern \"C\" MyType* another_func();\n"
        )
        file_path = "lib.rs"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        cache = LRUFileCache(capacity=5)
        
        # Build registry
        exports = build_ffi_registry([file_path], file_cache=cache)
        self.assertIn("export_rust_func", exports)
        self.assertIn("another_func", exports)

        # Create FFI caller in C++
        cpp_code = (
            "extern \"C\" void export_rust_func();\n"
            "void test() { export_rust_func(); }\n"
        )
        cpp_path = "caller.cpp"
        with open(cpp_path, "w", encoding="utf-8") as f:
            f.write(cpp_code)

        # Tracing FFI callers in other languages
        ffi_callers = trace_ffi_callers("export_rust_func", [file_path, cpp_path], source_ext=".rs", file_cache=cache)
        self.assertIn(cpp_path, ffi_callers)
        self.assertEqual(ffi_callers[cpp_path][0]["line"], 1)

    @patch("os.path.relpath")
    def test_analyze_compile_commands_drive_mismatch(self, mock_relpath):
        # Mock relpath to raise ValueError (e.g. drive mismatch on Windows)
        mock_relpath.side_effect = ValueError("path is on another drive")

        # Use absolute paths native to the running OS
        abs_ref = os.path.abspath("other_drive/main.cpp")
        abs_target = os.path.abspath("other_drive/main.h")
        abs_dir = os.path.abspath("project")

        db = [
            {
                "directory": abs_dir,
                "command": f"clang++ -c {abs_ref}",
                "file": abs_ref
            }
        ]
        with open("compile_commands.json", "w") as f:
            json.dump(db, f)

        # Since relpath raises ValueError, it should fall back to the absolute path.
        with patch("os.path.exists", return_value=True):
            callers = analyze_compile_commands(abs_target)
            # Should fall back to the absolute path formatted with forward slashes
            expected_key = abs_ref.replace("\\", "/")
            self.assertIn(expected_key, callers)

    def test_trace_macro_expansion_relpath_drive_mismatch(self):
        """On Windows, clang linemarkers can reference absolute paths on a
        different drive from the project root.  os.path.relpath raises
        ValueError in that case.  trace_macro_expansion must catch it and fall
        back to the absolute path so execution continues rather than crashing."""
        # Build a fake clang -E output whose linemarker points to an absolute path.
        # We mock os.path.relpath in the preprocessor module to raise ValueError
        # while still letting os.path.exists return False so the code skips the
        # file body lookup (we only care that no exception is raised).
        abs_header = "/D:/sys/include/stdio.h"
        func_name = "my_macro"
        expanded = (
            f'# 1 "{abs_header}"\n'
            f"void {func_name}() {{}}\n"
        )

        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = []

        with patch("context_builder.preprocessor.run_command", return_value=expanded), \
             patch("context_builder.preprocessor.os.path.relpath",
                   side_effect=ValueError("path is on mount 'D:'")) as mock_rp, \
             patch("context_builder.preprocessor.os.path.exists", return_value=False):
            # Should not raise even though relpath raises ValueError
            result = trace_macro_expansion(
                func_name, ["src/main.c"], file_cache=mock_cache
            )

        # The result dict may be empty (file doesn't exist), but no exception was raised.
        self.assertIsInstance(result, dict)
