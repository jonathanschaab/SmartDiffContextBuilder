import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock, ANY
import json
from context_builder.cache import LRUFileCache
import context_builder.preprocessor as _preprocessor_mod
from context_builder.sys_utils import FileScanCandidates
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
        # test's cached mtime/content/path.
        _preprocessor_mod._COMPILE_COMMANDS_STATE["cache"] = None
        _preprocessor_mod._COMPILE_COMMANDS_STATE["mtime"] = None
        _preprocessor_mod._COMPILE_COMMANDS_STATE["path"] = None
        _preprocessor_mod.clear_preprocessed_cache()

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

        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=(expanded, "success"),
        ), \
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
            f.write("#define JOIN(a, b) a ## b\n")

        # Fake clang -E output
        expanded = (
            f'# 1 "{header_path}"\n'
            "void my_func() {}\n"
        )
        
        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = ["#define MY_MACRO 10"]
        
        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=(expanded, "success"),
        ), \
             patch("context_builder.preprocessor.os.path.exists", return_value=True):
            result = trace_macro_expansion("my_func", [header_path], file_cache=mock_cache)
            
        self.assertIn("my_header.h", result)
        self.assertEqual(result["my_header.h"][0]["code"], "// [Macro Expansion Link] #define MY_MACRO 10")

        # Test case-insensitivity of macro expansion
        header_path_upper = "MY_HEADER.H"
        with open(header_path_upper, "w") as f:
            f.write("#define JOIN(a, b) a ## b\n")
            
        expanded_upper = (
            f'# 1 "{header_path_upper}"\n'
            "void my_func() {}\n"
        )
        
        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=(expanded_upper, "success"),
        ), \
             patch("context_builder.preprocessor.os.path.exists", return_value=True):
            result_upper = trace_macro_expansion("my_func", [header_path_upper], file_cache=mock_cache)
            
        self.assertIn("MY_HEADER.H", result_upper)

    def test_trace_macro_expansion_skips_safe_empty_prefilter(self):
        """Literal and token-paste misses avoid exhaustive preprocessing."""
        repo_files = ["one.h", "two.cpp"]
        with patch(
            "context_builder.preprocessor.ripgrep_filter",
            side_effect=[[], []],
        ) as mock_filter, patch(
            "context_builder.preprocessor._process_single_macro_file",
        ) as mock_process:
            result = trace_macro_expansion(
                "generated_symbol",
                repo_files,
                file_cache=MagicMock(),
            )

        self.assertEqual(result, {})
        self.assertEqual(mock_filter.call_count, 2)
        mock_filter.assert_any_call(
            repo_files,
            "generated_symbol",
            fallback_hint="macro callers of 'generated_symbol'",
        )
        mock_filter.assert_any_call(
            repo_files,
            "##",
            fallback_hint="token-pasting macros relevant to 'generated_symbol'",
        )
        mock_process.assert_not_called()

    def test_trace_macro_expansion_scans_after_token_paste_match(self):
        """Token-pasting can synthesize a symbol absent from source text."""
        repo_files = ["macros.h", "main.cpp"]

        with patch(
            "context_builder.preprocessor.ripgrep_filter",
            side_effect=[[], ["macros.h"]],
        ), patch(
            "context_builder.preprocessor._process_single_macro_file",
        ) as mock_process:
            trace_macro_expansion(
                "generated_symbol",
                repo_files,
                file_cache=MagicMock(),
            )

        scanned_files = [call.args[0] for call in mock_process.call_args_list]
        self.assertEqual(scanned_files, repo_files)

    def test_trace_macro_expansion_scans_when_token_paste_check_fails(self):
        """An unreliable safety check must preserve exhaustive preprocessing."""
        repo_files = ["macros.h", "main.cpp"]
        fallback_files = FileScanCandidates(
            repo_files,
            "token-pasting macros relevant to 'generated_symbol'",
        )

        with patch(
            "context_builder.preprocessor.ripgrep_filter",
            side_effect=[[], fallback_files],
        ), patch(
            "context_builder.preprocessor._process_single_macro_file",
        ) as mock_process:
            trace_macro_expansion(
                "generated_symbol",
                repo_files,
                file_cache=MagicMock(),
            )

        scanned_files = [call.args[0] for call in mock_process.call_args_list]
        self.assertEqual(scanned_files, repo_files)

    def test_trace_macro_expansion_scans_callers_without_lexical_match(self):
        """Header macro matches do not exclude source files that invoke the macro."""
        repo_files = ["macros.h", "main.c"]

        with patch(
            "context_builder.preprocessor.ripgrep_filter",
            return_value=["macros.h"],
        ), patch(
            "context_builder.preprocessor._process_single_macro_file",
        ) as mock_process:
            trace_macro_expansion("my_func", repo_files, file_cache=MagicMock())

        scanned_files = [call.args[0] for call in mock_process.call_args_list]
        self.assertEqual(scanned_files, ["macros.h", "main.c"])

    def test_trace_macro_expansion_reuses_preprocessed_output(self):
        """Multiple symbol searches preprocess each source snapshot only once."""
        source_path = "main.c"
        with open(source_path, "w", encoding="utf-8") as source_file:
            source_file.write("#define BOTH() foo(); bar()\n")

        expanded = "void test() { foo(); bar(); }\n"
        with patch(
            "context_builder.preprocessor.ripgrep_filter",
            return_value=[source_path],
        ), patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=(expanded, "success"),
        ) as mock_run, patch(
            "context_builder.preprocessor._map_expanded_line_to_source",
        ):
            trace_macro_expansion("foo", [source_path], file_cache=MagicMock())
            trace_macro_expansion("bar", [source_path], file_cache=MagicMock())

        mock_run.assert_called_once_with(source_path)

    def test_preprocessed_output_cache_invalidates_on_source_change(self):
        """Changing a source file invalidates its cached preprocessor output."""
        source_path = "main.c"
        with open(source_path, "w", encoding="utf-8") as source_file:
            source_file.write("int first;\n")

        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            side_effect=[
                ("first expansion", "success"),
                ("second expansion", "success"),
            ],
        ) as mock_run:
            first = _preprocessor_mod._get_preprocessed_code(source_path)
            with open(source_path, "w", encoding="utf-8") as source_file:
                source_file.write("int second_value;\n")
            second = _preprocessor_mod._get_preprocessed_code(source_path)

        self.assertEqual(first, "first expansion")
        self.assertEqual(second, "second expansion")
        self.assertEqual(mock_run.call_count, 2)

    def test_preprocessed_code_skips_clang_when_stat_fails(self):
        """Missing or inaccessible candidates do not spawn clang."""
        with patch(
            "context_builder.preprocessor.os.stat",
            side_effect=OSError("file unavailable"),
        ), patch(
            "context_builder.preprocessor._run_clang_preprocessor",
        ) as mock_run:
            expanded = _preprocessor_mod._get_preprocessed_code("missing.cpp")

        self.assertEqual(expanded, "")
        mock_run.assert_not_called()

    def test_preprocessed_code_caches_successful_empty_output(self):
        """A valid empty expansion is a reusable negative result."""
        source_path = "empty.c"
        with open(source_path, "w", encoding="utf-8"):
            pass

        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=("", "success"),
        ) as mock_run:
            first = _preprocessor_mod._get_preprocessed_code(source_path)
            second = _preprocessor_mod._get_preprocessed_code(source_path)

        self.assertEqual(first, "")
        self.assertEqual(second, "")
        mock_run.assert_called_once_with(source_path)

    def test_preprocessed_code_caches_deterministic_failure(self):
        """A clang failure is not retried for an unchanged source snapshot."""
        source_path = "invalid.c"
        with open(source_path, "w", encoding="utf-8") as source_file:
            source_file.write("#error invalid\n")

        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=("", "failed"),
        ) as mock_run:
            first = _preprocessor_mod._get_preprocessed_code(source_path)
            second = _preprocessor_mod._get_preprocessed_code(source_path)

        self.assertEqual(first, "")
        self.assertEqual(second, "")
        mock_run.assert_called_once_with(source_path)

    def test_preprocessed_code_retries_timeout_once(self):
        """A timeout gets one retry before its empty result is cached."""
        source_path = "slow.c"
        with open(source_path, "w", encoding="utf-8") as source_file:
            source_file.write("#include \"slow.h\"\n")

        with patch(
            "context_builder.preprocessor._run_clang_preprocessor",
            return_value=("", "timeout"),
        ) as mock_run:
            first = _preprocessor_mod._get_preprocessed_code(source_path)
            second = _preprocessor_mod._get_preprocessed_code(source_path)
            third = _preprocessor_mod._get_preprocessed_code(source_path)

        self.assertEqual(first, "")
        self.assertEqual(second, "")
        self.assertEqual(third, "")
        self.assertEqual(mock_run.call_count, 2)

    def test_clang_preprocessor_reports_timeout_separately(self):
        """The clang wrapper preserves timeout status for retry decisions."""
        with patch(
            "context_builder.preprocessor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["clang", "-E"], 5),
        ), patch("context_builder.preprocessor.warn_once") as mock_warn:
            output, status = _preprocessor_mod._run_clang_preprocessor("slow.c")

        self.assertEqual(output, "")
        self.assertEqual(status, "timeout")
        mock_warn.assert_called_once()

    def test_clang_preprocessor_reports_deterministic_failure(self):
        """Non-timeout clang failures can be negatively cached."""
        failure = subprocess.CalledProcessError(1, ["clang", "-E", "invalid.c"])
        with patch(
            "context_builder.preprocessor.subprocess.run",
            side_effect=failure,
        ):
            output, status = _preprocessor_mod._run_clang_preprocessor("invalid.c")

        self.assertEqual(output, "")
        self.assertEqual(status, "failed")

    def test_clang_preprocessor_replaces_undecodable_output(self):
        """Clang output decoding is tolerant of legacy or invalid source bytes."""
        replacement_output = "const char *value = \"\ufffd\";\n"

        def decoding_sensitive_run(*_args, **kwargs):
            self.assertEqual(kwargs.get("errors"), "replace")
            return MagicMock(stdout=replacement_output)

        with patch(
            "context_builder.preprocessor.subprocess.run",
            side_effect=decoding_sensitive_run,
        ):
            output, status = _preprocessor_mod._run_clang_preprocessor("legacy.c")

        self.assertEqual(output, replacement_output)
        self.assertEqual(status, "success")

    def test_clang_preprocessor_caches_missing_executable_for_scan(self):
        """A missing clang executable is probed only once per repository scan."""
        with patch(
            "context_builder.preprocessor.subprocess.run",
            side_effect=FileNotFoundError("clang unavailable"),
        ) as mock_run, patch(
            "context_builder.preprocessor.warn_once",
        ) as mock_warn:
            first = _preprocessor_mod._run_clang_preprocessor("one.c")
            second = _preprocessor_mod._run_clang_preprocessor("two.cpp")

        self.assertEqual(first, ("", "failed"))
        self.assertEqual(second, ("", "failed"))
        mock_run.assert_called_once()
        mock_warn.assert_called_once_with(
            "clang_missing",
            "clang is unavailable; continuing C/C++ analysis without macro expansion.",
        )

    def test_clear_preprocessed_cache_rechecks_clang_availability(self):
        """A new repository scan gets one fresh chance to discover clang."""
        with patch(
            "context_builder.preprocessor.subprocess.run",
            side_effect=FileNotFoundError("clang unavailable"),
        ) as mock_run, patch(
            "context_builder.preprocessor.warn_once",
        ):
            _preprocessor_mod._run_clang_preprocessor("one.c")
            _preprocessor_mod.clear_preprocessed_cache()
            _preprocessor_mod._run_clang_preprocessor("two.c")

        self.assertEqual(mock_run.call_count, 2)

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
                r'INVALID_REGEX_(' # Intentional invalid regex
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
            CONFIG['ffi_rg_pattern'] = r"\bfn\b"
            with patch("context_builder.preprocessor.warn_once") as mock_warn:
                symbols = build_ffi_registry([file_path], file_cache=cache)
                self.assertIn("test", symbols)
                # Should warn about non-string patterns
                mock_warn.assert_any_call("ffi_pattern_non_string", ANY)
        finally:
            reset_config()

    def test_build_ffi_registry_forces_progress_without_prefilter(self):
        """An exhaustive FFI registry scan shows progress when no rg pattern is configured."""
        from context_builder.config import CONFIG
        repo_files = ["one.rs", "two.cpp"]

        with patch.dict(CONFIG, {"ffi_rg_pattern": None}), \
             patch("context_builder.preprocessor.iter_scan_progress", return_value=[]) as mock_iter:
            build_ffi_registry(repo_files, file_cache=MagicMock())

        self.assertTrue(mock_iter.call_args.kwargs["force"])

    def test_build_ffi_registry_trusts_empty_prefilter_result(self):
        """A successful ripgrep miss avoids an exhaustive FFI registry scan."""
        from context_builder.config import CONFIG
        repo_files = ["one.rs", "two.cpp"]
        mock_cache = MagicMock()

        with patch.dict(
            CONFIG,
            {
                "ffi_rg_pattern": "FFI_EXPORT",
                "ffi_patterns": [r"FFI_EXPORT\s+([A-Za-z0-9_]+)"],
            },
        ), patch(
            "context_builder.preprocessor.ripgrep_filter",
            return_value=[],
        ):
            symbols = build_ffi_registry(repo_files, file_cache=mock_cache)

        self.assertEqual(symbols, set())
        mock_cache.get_content.assert_not_called()

    def test_build_ffi_registry_scans_all_files_after_ripgrep_failure(self):
        """Ripgrep fallback candidates preserve exhaustive FFI extraction."""
        from context_builder.config import CONFIG
        repo_files = ["one.rs", "two.cpp"]
        fallback_files = FileScanCandidates(
            repo_files,
            "FFI export pre-computation",
        )
        mock_cache = MagicMock()
        mock_cache.get_content.side_effect = [
            "FFI_EXPORT exported_symbol",
            "ordinary source",
        ]

        with patch.dict(
            CONFIG,
            {
                "ffi_rg_pattern": "FFI_EXPORT",
                "ffi_patterns": [r"FFI_EXPORT\s+([A-Za-z0-9_]+)"],
            },
        ), patch(
            "context_builder.preprocessor.ripgrep_filter",
            return_value=fallback_files,
        ):
            symbols = build_ffi_registry(repo_files, file_cache=mock_cache)

        self.assertEqual(symbols, {"exported_symbol"})
        self.assertEqual(mock_cache.get_content.call_count, len(repo_files))

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
            r"""#include \
"utils/helper.h"
""",
            r"""#include "utils/\
helper.h"
""",
            r"""#\
include "helper.h"
""",
            r"""#include "hel\
per.h"
""",
            # Test Windows-style line continuation explicitly
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
            """#include 
"utils/helper.h"
""",
            """#include "utils/
helper.h"
""",
            """#include "helper.h
"
""",
            """#
include "helper.h"
""",
            # Line continuation acting as directory separator should not match
            r"""#include "utils\
helper.h"
""",
            '#include "utils/some_other_helper.h"\n',
            '#include <common/some_other_helper.h>\n',
        ]

        for inc in non_matching_includes:
            with open("src/other.cpp", "w", encoding="utf-8", newline="") as f:
                f.write(inc)
            
            cache = LRUFileCache()
            callers = analyze_compile_commands("src/helper.h", file_cache=cache)
            self.assertNotIn("src/other.cpp", callers, f"Incorrectly matched: {inc.strip()}")

    def test_analyze_compile_commands_invalid_target(self):
        """Verify that analyze_compile_commands returns early and empty for invalid target files."""
        # Empty target file
        callers = analyze_compile_commands("")
        self.assertEqual(callers, {})

        # None target file
        callers = analyze_compile_commands(None)
        self.assertEqual(callers, {})

        # Target file that has no basename (e.g. just a separator or root directory)
        callers = analyze_compile_commands("/")
        self.assertEqual(callers, {})

    def test_analyze_compile_commands_target_with_spaces(self):
        """Verify that targets containing space characters are matched correctly."""
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

        # 1. Matching case: include contains a space matching the target
        with open("src/other.cpp", "w", encoding="utf-8", newline="") as f:
            f.write('#include "my helper.h"\n')
        
        cache = LRUFileCache()
        callers = analyze_compile_commands("src/my helper.h", file_cache=cache)
        self.assertIn("src/other.cpp", callers)

        # 2. Non-matching case: include does not contain the space (un-spaced filename)
        with open("src/other.cpp", "w", encoding="utf-8", newline="") as f:
            f.write('#include "myhelper.h"\n')
        
        cache = LRUFileCache()
        callers = analyze_compile_commands("src/my helper.h", file_cache=cache)
        self.assertNotIn("src/other.cpp", callers)

    def test_analyze_compile_commands_cache_invalidation_on_directory_change(self):
        """Verify that cache invalidates correctly when the directory of compile_commands.json changes,
        even if the modification times (mtime) are identical."""
        # 1. Create dir1 with compile_commands.json
        dir1 = os.path.join(self.temp_dir.name, "dir1")
        os.makedirs(os.path.join(dir1, "src"), exist_ok=True)
        db1 = [
            {
                "directory": dir1,
                "command": "clang++ -c src/file1.cpp",
                "file": "src/file1.cpp"
            }
        ]
        db1_path = os.path.join(dir1, "compile_commands.json")
        with open(db1_path, "w") as f:
            json.dump(db1, f)
        with open(os.path.join(dir1, "src/file1.cpp"), "w") as f:
            f.write('#include "helper.h"\n')

        # 2. Create dir2 with different compile_commands.json
        dir2 = os.path.join(self.temp_dir.name, "dir2")
        os.makedirs(os.path.join(dir2, "src"), exist_ok=True)
        db2 = [
            {
                "directory": dir2,
                "command": "clang++ -c src/file2.cpp",
                "file": "src/file2.cpp"
            }
        ]
        db2_path = os.path.join(dir2, "compile_commands.json")
        with open(db2_path, "w") as f:
            json.dump(db2, f)
        with open(os.path.join(dir2, "src/file2.cpp"), "w") as f:
            f.write('#include "helper.h"\n')

        # Set the same mtime on both files to simulate the cache collision condition
        mtime = 123456789.0
        os.utime(db1_path, (mtime, mtime))
        os.utime(db2_path, (mtime, mtime))

        # 3. Change directory to dir1 and run analysis
        os.chdir(dir1)
        cache1 = LRUFileCache()
        callers1 = analyze_compile_commands("src/helper.h", file_cache=cache1)
        self.assertIn("src/file1.cpp", callers1)
        self.assertNotIn("src/file2.cpp", callers1)

        # 4. Change directory to dir2 and run analysis (should invalidate path and load dir2's database)
        os.chdir(dir2)
        cache2 = LRUFileCache()
        callers2 = analyze_compile_commands("src/helper.h", file_cache=cache2)
        self.assertIn("src/file2.cpp", callers2)
        self.assertNotIn("src/file1.cpp", callers2)

    @patch("os.path.relpath")
    def test_analyze_compile_commands_worktree_repo_root_prefix_bug(self, mock_relpath):
        """Verify that files in sibling directories sharing a prefix with repo_root
        are not incorrectly matched and processed by the worktree mapping logic."""
        # Setup a dummy compile commands database pointing to a file in a sibling directory
        db = [
            {
                "directory": ".",
                "command": "clang++ -c /path/to/repo_addon/src/other.cpp",
                "file": "/path/to/repo_addon/src/other.cpp"
            }
        ]
        with open("compile_commands.json", "w", encoding="utf-8") as f:
            json.dump(db, f)

        # Set up a side effect for mock_relpath to behave normally for other calls
        # (e.g. relpath calls when computing return values), but we want to make sure
        # it is not called with the sibling directory path relative to the repo_root.
        mock_relpath.side_effect = os.path.relpath

        # Run analysis looking for helper.h, with repo_root=/path/to/repo
        # Since /path/to/repo_addon does not start with /path/to/repo/ (with trailing slash),
        # it should NOT trigger the worktree mapping logic.
        cache = LRUFileCache()
        analyze_compile_commands("src/helper.h", repo_root="/path/to/repo", file_cache=cache)

        # Assert that relpath was never called to map /path/to/repo_addon relative to /path/to/repo
        for call_args in mock_relpath.call_args_list:
            args = call_args[0]
            if len(args) >= 2:
                self.assertNotEqual(args[1], "/path/to/repo")
