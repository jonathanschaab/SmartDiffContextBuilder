import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, ANY
import json
from context_builder.cache import LRUFileCache
import context_builder.preprocessor as _preprocessor_mod
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
        # Reset the module-level compile_commands.json cache so each test
        # starts with a clean state and cannot be contaminated by a previous
        # test's cached mtime/content.
        _preprocessor_mod._COMPILE_COMMANDS_CACHE = None
        _preprocessor_mod._COMPILE_COMMANDS_MTIME = None

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

        # Create main.cpp that includes main.h
        with open("main.cpp", "w") as f:
            f.write('#include "main.h"\n')

        # Create other.cpp that includes main.h
        with open("other.cpp", "w") as f:
            f.write('#include "main.h"\n')

        # Target file is main.h.
        # It should link to main.cpp and other.cpp via include match.
        callers = analyze_compile_commands("main.h")
        self.assertIn("main.cpp", callers)
        self.assertIn("other.cpp", callers)
        self.assertEqual(
            callers["main.cpp"][0]["code"],
            "// [Compilation Link via compile_commands.json]"
        )

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

        # Create src/main.cpp that includes main.h
        with open("src/main.cpp", "w") as f:
            f.write('#include "main.h"\n')

        # Target file is src/main.h. Since ../src/main.cpp resolves to src/main.cpp,
        # it should link correctly by checking include match.
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

        # Test case-insensitivity of FFI caller tracing extension check
        # We pass source_ext=".RS" (uppercase) and check that lib.rs is still skipped,
        # and caller.cpp is still searched (even if named CALLER.CPP)
        caller_upper_path = "CALLER.CPP"
        with open(caller_upper_path, "w", encoding="utf-8") as f:
            f.write(cpp_code)
        ffi_callers_upper = trace_ffi_callers(
            "export_rust_func",
            ["lib.rs", caller_upper_path],
            source_ext=".RS",
            file_cache=cache
        )
        self.assertNotIn("lib.rs", ffi_callers_upper)
        self.assertIn(caller_upper_path, ffi_callers_upper)

    @patch("os.path.relpath")
    def test_analyze_compile_commands_drive_mismatch(self, mock_relpath):
        # Mock relpath to raise ValueError (e.g. drive mismatch on Windows)
        mock_relpath.side_effect = ValueError("path is on another drive")

        # Use absolute paths native to the running OS
        abs_ref = os.path.abspath("other_drive/main.cpp")
        abs_target = os.path.abspath("other_drive/main.h")
        abs_dir = os.path.abspath("project")

        # Create other_drive/main.cpp containing `#include "main.h"`
        os.makedirs(os.path.dirname(abs_ref), exist_ok=True)
        with open(abs_ref, "w") as f:
            f.write('#include "main.h"\n')

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

    def test_analyze_compile_commands_worktree_repo_root(self):
        """When --commit-range is used, cwd is a temporary worktree while
        compile_commands.json entries hold absolute paths inside the *original*
        repository.  Without repo_root, os.path.relpath(abs_ref_file, cwd)
        produces a path full of '../../..' that escapes the worktree and is
        rejected by is_in_repo().  Passing repo_root=original_repo fixes this
        by computing the relpath relative to the project root instead."""
        import tempfile

        # Simulate the original repo directory and worktree directory
        with tempfile.TemporaryDirectory() as original_repo, \
             tempfile.TemporaryDirectory() as worktree:

            # Create a source file in the original repo
            src_dir = os.path.join(original_repo, "src")
            os.makedirs(src_dir, exist_ok=True)
            ref_cpp = os.path.join(src_dir, "other.cpp")
            with open(ref_cpp, "w") as f:
                f.write('#include "other.h"\n')

            # Create the corresponding source file in the worktree
            wt_src_dir = os.path.join(worktree, "src")
            os.makedirs(wt_src_dir, exist_ok=True)
            wt_ref_cpp = os.path.join(wt_src_dir, "other.cpp")
            with open(wt_ref_cpp, "w") as f:
                f.write('#include "other.h"\n')

            # Create compile_commands.json in the *worktree* (it was copied there
            # by main() before chdir-ing into the worktree)
            db = [
                {
                    "directory": src_dir,      # absolute path in original repo
                    "command": f"clang++ -c {ref_cpp}",
                    "file": ref_cpp             # absolute path in original repo
                }
            ]
            ccj_path = os.path.join(worktree, "compile_commands.json")
            with open(ccj_path, "w") as f:
                json.dump(db, f)

            # chdir into the worktree to simulate the --commit-range runtime context
            old_cwd = os.getcwd()
            os.chdir(worktree)
            try:
                # Without repo_root: relpath is computed relative to worktree.
                # The result will start with '../' or be an absolute path and
                # would NOT represent a valid in-project relative path.
                result_no_root = analyze_compile_commands(
                    os.path.join(src_dir, "other.h")
                )
                for key in result_no_root:
                    # At minimum the key should not start with the original repo
                    # absolute path prefix when worktree != original_repo
                    self.assertFalse(
                        key.startswith(worktree.replace("\\", "/")),
                        "Key should not be inside the worktree dir"
                    )

                # With repo_root: relpath is computed relative to original_repo.
                # The result should be a clean path like 'src/other.cpp'.
                result_with_root = analyze_compile_commands(
                    os.path.join(src_dir, "other.h"),
                    repo_root=original_repo
                )
                expected_key = os.path.relpath(ref_cpp, original_repo).replace("\\", "/")
                self.assertIn(expected_key, result_with_root,
                              "With repo_root the key must be a valid project-relative path")
                # Verify the key does NOT escape the project root
                self.assertFalse(expected_key.startswith(".."),
                                 "Path must not start with '..' when repo_root is provided")
            finally:
                os.chdir(old_cwd)

    def test_trace_macro_expansion_header_file(self):
        # Create a header file (.h)
        header_path = "my_header.h"
        with open(header_path, "w") as f:
            f.write("#define MY_MACRO 10\n")

        # Fake clang -E output
        expanded = (
            f'# 1 "{header_path}"\n'
            "void my_func() {}\n"
        )
        
        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = ["#define MY_MACRO 10"]
        
        with patch("context_builder.preprocessor.run_command", return_value=expanded), \
             patch("context_builder.preprocessor.os.path.exists", return_value=True):
            result = trace_macro_expansion("my_func", [header_path], file_cache=mock_cache)
            
        self.assertIn("my_header.h", result)
        self.assertEqual(result["my_header.h"][0]["code"], "// [Macro Expansion Link] #define MY_MACRO 10")

        # Test case-insensitivity of macro expansion
        header_path_upper = "MY_HEADER.H"
        with open(header_path_upper, "w") as f:
            f.write("#define MY_MACRO 10\n")
            
        expanded_upper = (
            f'# 1 "{header_path_upper}"\n'
            "void my_func() {}\n"
        )
        
        with patch("context_builder.preprocessor.run_command", return_value=expanded_upper), \
             patch("context_builder.preprocessor.os.path.exists", return_value=True):
            result_upper = trace_macro_expansion("my_func", [header_path_upper], file_cache=mock_cache)
            
        self.assertIn("MY_HEADER.H", result_upper)

    def test_analyze_compile_commands_worktree_mapping(self):
        """Verify that when repo_root is passed, absolute paths in compile_commands.json
        pointing to the original repo are mapped to the active worktree (CWD) and read correctly."""
        original_repo = os.path.abspath("original_repo")
        worktree = os.path.abspath("worktree")
        os.makedirs(os.path.join(original_repo, "src"), exist_ok=True)
        os.makedirs(os.path.join(worktree, "src"), exist_ok=True)

        ref_cpp_orig = os.path.join(original_repo, "src", "other.cpp")
        ref_cpp_wt = os.path.join(worktree, "src", "other.cpp")
        
        # Write different contents to the original and worktree files
        # Since we want to analyze the worktree file, the include match should succeed on the worktree content but fail on the original
        with open(ref_cpp_orig, "w") as f:
            f.write("// original file content - no include\n")
        with open(ref_cpp_wt, "w") as f:
            f.write('#include "target.h"\n')

        db = [
            {
                "directory": os.path.join(original_repo, "src"),
                "command": f"clang++ -c {ref_cpp_orig}",
                "file": ref_cpp_orig
            }
        ]
        
        # Write compile_commands.json in the worktree directory (simulating main behavior)
        with open(os.path.join(worktree, "compile_commands.json"), "w") as f:
            json.dump(db, f)

        old_cwd = os.getcwd()
        os.chdir(worktree)
        try:
            # We look for target.h. It should find other.cpp in the worktree because
            # it has mapped the path, checked its existence in the worktree, and read its includes.
            callers = analyze_compile_commands("src/target.h", repo_root=original_repo)
            self.assertIn("src/other.cpp", callers)
        finally:
            os.chdir(old_cwd)

    @patch("context_builder.preprocessor.os.path.relpath")
    def test_analyze_compile_commands_repo_root_drive_mismatch(self, mock_relpath):
        """Verify that when repo_root is provided and os.path.relpath raises ValueError
        due to drive mismatch, the exception is caught and it handles it gracefully."""
        mock_relpath.side_effect = ValueError("path is on another drive")

        original_repo = os.path.abspath("original_repo")
        os.makedirs(os.path.join(original_repo, "src"), exist_ok=True)
        ref_cpp = os.path.join(original_repo, "src", "main.cpp")
        target_h = os.path.join(original_repo, "src", "main.h")

        # Create src/main.cpp containing `#include "main.h"`
        with open(ref_cpp, "w") as f:
            f.write('#include "main.h"\n')

        db = [
            {
                "directory": os.path.join(original_repo, "src"),
                "command": f"clang++ -c {ref_cpp}",
                "file": ref_cpp
            }
        ]
        with open("compile_commands.json", "w") as f:
            json.dump(db, f)

        callers = analyze_compile_commands(target_h, repo_root=original_repo)
        # Since relpath raised ValueError, the file mapping should fall back gracefully
        self.assertIsInstance(callers, dict)

    def test_custom_ffi_patterns(self):
        """Verify that build_ffi_registry respects custom ffi_rg_pattern and ffi_patterns in CONFIG."""
        from context_builder.config import CONFIG, reset_config
        orig_rg = CONFIG['ffi_rg_pattern']
        orig_patterns = CONFIG['ffi_patterns'].copy()
        
        try:
            # Setup custom FFI settings
            CONFIG['ffi_rg_pattern'] = "MY_FFI_EXPORT"
            CONFIG['ffi_patterns'] = [
                r'MY_FFI_EXPORT\s+([A-Za-z0-9_]+)',
                r'INVALID_REGEX_[[[' # Intentional invalid regex
            ]
            
            code = (
                "MY_FFI_EXPORT my_custom_func\n"
                "extern \"C\" not_matched_func\n"
            )
            file_path = "ffi_test.rs"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
                
            cache = LRUFileCache(capacity=5)
            cache.get_content(file_path)
            
            with patch("context_builder.preprocessor.warn_once") as mock_warn:
                symbols = build_ffi_registry([file_path], file_cache=cache)
                self.assertIn("my_custom_func", symbols)
                self.assertNotIn("not_matched_func", symbols)
                # Verify that warning was triggered for invalid regex
                mock_warn.assert_called_once()
                self.assertIn("ffi_regex_compile_fail", mock_warn.call_args[0][0])
                
        finally:
            reset_config()

    def test_ffi_patterns_safety(self):
        """Verify that build_ffi_registry doesn't crash on null or non-string FFI patterns."""
        from context_builder.config import CONFIG, reset_config
        reset_config()
        
        file_path = "ffi_safety.rs"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("fn test() {}\n")
            
        cache = LRUFileCache(capacity=5)
        cache.get_content(file_path)
        
        try:
            # 1. Null pattern test
            CONFIG['ffi_patterns'] = None
            symbols = build_ffi_registry([file_path], file_cache=cache)
            self.assertEqual(len(symbols), 0)
            
            # 2. Non-string pattern test
            CONFIG['ffi_patterns'] = [123, True, "fn\\s+([a-z]+)"]
            with patch("context_builder.preprocessor.warn_once") as mock_warn:
                symbols = build_ffi_registry([file_path], file_cache=cache)
                self.assertIn("test", symbols)
                # Should warn about non-string patterns
                mock_warn.assert_any_call("ffi_pattern_non_string", ANY)
        finally:
            reset_config()

    def test_analyze_compile_commands_include_with_directory_prefix(self):
        """Verify that translation units are successfully linked to target files
        when includes use relative, absolute, forward-slashed, or back-slashed
        directory prefixes, while avoiding substring matching false positives."""
        # Create compile_commands.json
        db = [
            {
                "directory": ".",
                "command": "clang++ -c src/other.cpp",
                "file": "src/other.cpp"
            }
        ]
        with open("compile_commands.json", "w") as f:
            json.dump(db, f)

        os.makedirs("src", exist_ok=True)

        # Test cases for matching includes
        matching_includes = [
            '#include "utils/helper.h"\n',
            '#include <common/helper.h>\n',
            '#include "a/b/c/helper.h"\n',
            '#include "/usr/include/helper.h"\n',
            '#include "C:\\project\\src\\helper.h"\n',
            '#include "helper.h"\n',
            '#  include   <helper.h>\n',
            '#include \\\n"utils/helper.h"\n',
            '#include "utils/\\\nhelper.h"\n',
            '#\\\ninclude "helper.h"\n',
            '#include "hel\\\nper.h"\n',
            '#include \\\r\n"helper.h"\n',
        ]

        for inc in matching_includes:
            # Write to src/other.cpp
            with open("src/other.cpp", "w", encoding="utf-8", newline="") as f:
                f.write(inc)
            
            # Pass a fresh cache instance to force reload of file content
            cache = LRUFileCache()
            callers = analyze_compile_commands("src/helper.h", file_cache=cache)
            self.assertIn("src/other.cpp", callers, f"Failed to match: {inc.strip()}")

        # Test cases for non-matching includes
        non_matching_includes = [
            '#include "some_other_helper.h"\n',
            '#include "helper.h/other.h"\n',
            '#include "helper.h_suffix.h"\n',
            '// #include "helper.h"\n',
            '// #include <helper.h>\n',
            '  // #include "helper.h"\n',
            '/* #include "helper.h" */\n',
            'int x = 0; #include "helper.h"\n',
            '#include \n"utils/helper.h"\n',
            '#include "utils/\nhelper.h"\n',
            '#include "helper.h\n"\n',
            '#\ninclude "helper.h"\n',
        ]

        for inc in non_matching_includes:
            with open("src/other.cpp", "w", encoding="utf-8", newline="") as f:
                f.write(inc)
            
            cache = LRUFileCache()
            callers = analyze_compile_commands("src/helper.h", file_cache=cache)
            self.assertNotIn("src/other.cpp", callers, f"Incorrectly matched: {inc.strip()}")
