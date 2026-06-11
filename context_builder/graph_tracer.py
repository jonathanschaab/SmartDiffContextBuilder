"""Graph traversal tracer for SmartDiffContextBuilder.

This module encapsulates call graph traversal algorithms, beginning from initial
modified files and tracing callers/callees.
"""

import os
from .ast_engine import (
    AST_ENGINE,
    extract_callees,
    extract_function_bounds,
    find_callee_definition,
    split_massive_block_ast,
    trace_lexical_dependencies_ast,
    trace_lexical_dependencies_regex,
)
from .config import DEFAULT_LSP_INIT_TIMEOUT, DEFAULT_LSP_QUERY_TIMEOUT
from .languages import get_language_profile
from .lsp_client import get_lsp_references
from .preprocessor import trace_ffi_callers, trace_macro_expansion
from .sys_utils import is_in_repo


def extract_function_name(cleaned_chunk, start, end, file_path=None):
    """Extract a function name using the file's language profile."""
    return get_language_profile(file_path).extract_function_name(
        cleaned_chunk,
        start,
        end,
    )


class CallGraphTracer:
    """Class to handle call graph traversal logic (BFS traversal)."""

    def __init__(self, file_cache, all_repo_files, ffi_exports, cpp_linkages, vm, args):
        self.file_cache = file_cache
        self.all_repo_files = all_repo_files if all_repo_files is not None else []
        self.ffi_exports = ffi_exports if ffi_exports is not None else set()
        self.cpp_linkages = cpp_linkages if cpp_linkages is not None else {}
        self.vm = vm
        self.args = args

    def _arg_or_default(self, name, default):
        """Return an argument value, treating missing and explicit None alike."""
        value = getattr(self.args, name, None)
        return default if value is None else value

    def _process_single_caller_reference(
        self, ref_path, occurrences, processed_spans, queue, depth
    ):
        """Bounds-check, extract name, and enqueue a single caller reference."""
        if ref_path == "[Pruned Instances]":
            return
        for occ in occurrences:
            occ_line = occ["line"]
            if occ_line <= 0:
                continue
            start, end = extract_function_bounds(
                ref_path, occ_line, file_cache=self.file_cache
            )
            if start is None:
                continue

            ref_lines = self.file_cache.get_lines(ref_path)
            if not ref_lines or start >= len(ref_lines):
                continue

            ref_chunk = "".join(ref_lines[start:end])
            profile = get_language_profile(ref_path)
            cleaned_ref_chunk = "\n".join(
                profile.strip_strings_and_comments(line)
                for line in ref_chunk.splitlines()
            )
            occ_func = extract_function_name(
                cleaned_ref_chunk,
                start,
                end,
                file_path=ref_path,
            )

            span_sig = f"{ref_path}::line_{start}_to_{end}"
            if span_sig not in processed_spans:
                processed_spans.add(span_sig)
                queue.append((ref_path, start + 1, occ_func, depth + 1))

    def _resolve_references(self, curr_file, curr_line, curr_func):
        """Retrieve raw reference list from LSP, AST, or Regex fallback."""
        callers = get_lsp_references(
            curr_file,
            curr_line,
            curr_func,
            self._arg_or_default("lsp_timeout", DEFAULT_LSP_QUERY_TIMEOUT),
            self._arg_or_default("max_interface_depth", 15),
            self._arg_or_default("disable_pruning", False),
            file_cache=self.file_cache,
            init_timeout=self._arg_or_default(
                "lsp_init_timeout", DEFAULT_LSP_INIT_TIMEOUT
            ),
        )
        if callers is None:
            ext = os.path.splitext(curr_file)[1].lower()
            if AST_ENGINE.is_supported(ext):
                callers = trace_lexical_dependencies_ast(
                    curr_func, self.all_repo_files, file_cache=self.file_cache
                )
            else:
                callers = trace_lexical_dependencies_regex(
                    curr_func, self.all_repo_files, file_cache=self.file_cache
                )
        return callers

    def _merge_macro_and_build_linkages(self, curr_file, curr_func, ext, callers):
        """Merge macro expansion and C++ build system compilation linkages into callers."""
        if (
            not self._arg_or_default("skip_macro_expansion", False)
            and get_language_profile(ext).supports_macro_expansion
        ):
            macro_results = trace_macro_expansion(
                curr_func, self.all_repo_files, file_cache=self.file_cache
            )
            for f_path, matches in macro_results.items():
                existing_matches = callers.setdefault(f_path, [])
                for m in matches:
                    if not any(c['line'] == m['line'] for c in existing_matches):
                        existing_matches.append(m)

        if curr_file in self.cpp_linkages:
            for req in self.cpp_linkages[curr_file]:
                callers.setdefault(req, []).extend(self.cpp_linkages[curr_file][req])

    def _process_caller_depth_step(
        self, curr_file, curr_line, curr_func, depth, processed_spans, queue
    ):
        """Trace callers for a single queue item at the current depth step."""
        callers = self._resolve_references(curr_file, curr_line, curr_func)
        ext = os.path.splitext(curr_file)[1].lower()
        self._merge_macro_and_build_linkages(curr_file, curr_func, ext, callers)

        filtered_callers = {}
        for fp, occs in callers.items():
            if fp == "[Pruned Instances]" or is_in_repo(fp):
                filtered_callers[fp] = occs

        self.vm.add_callers(
            self.vm.local_callers,
            filtered_callers,
            "Lexical Dependency",
            confidence="MEDIUM",
            distance=depth + 1,
        )

        if (
            not self._arg_or_default("skip_ffi", False)
            and curr_func in self.ffi_exports
        ):
            ffi_callers = trace_ffi_callers(
                curr_func,
                self.all_repo_files,
                source_ext=ext,
                file_cache=self.file_cache,
            )
            filtered_ffi = {fp: occs for fp, occs in ffi_callers.items() if is_in_repo(fp)}
            self.vm.add_callers(
                self.vm.ffi_linkages, filtered_ffi, "FFI Linkage", distance=depth + 1
            )

        for ref_path, occurrences in filtered_callers.items():
            self._process_single_caller_reference(
                ref_path, occurrences, processed_spans, queue, depth
            )

    def trace_callers(self, queue, processed_spans):
        """Perform BFS queue traversal for tracing function callers."""
        caller_depth = self._arg_or_default("caller_depth", 0)

        while queue:
            curr_file, curr_line, curr_func, depth = queue.popleft()

            if depth < caller_depth:
                self._process_caller_depth_step(
                    curr_file, curr_line, curr_func, depth, processed_spans, queue
                )

    def _process_single_callee(
        self, callee_name, depth, processed_callee_spans, callee_queue
    ):
        """Resolve, bounds-check, and semantically split a single called function."""
        def_file, def_line = find_callee_definition(
            callee_name, self.all_repo_files, file_cache=self.file_cache
        )
        if not def_file or not def_line:
            return
        def_start, def_end = extract_function_bounds(
            def_file, def_line, file_cache=self.file_cache
        )
        if def_start is None:
            return
        span_sig = f"{def_file}::line_{def_start}_to_{def_end}"
        if span_sig in processed_callee_spans:
            return
        processed_callee_spans.add(span_sig)

        ref_lines = self.file_cache.get_lines(def_file)
        if not ref_lines or def_start >= len(ref_lines):
            return

        func_chunk = "".join(ref_lines[def_start:def_end])
        max_lines_val = self._arg_or_default("max_lines", 1000)
        subunits = split_massive_block_ast(
            func_chunk, def_file, max(1, max_lines_val - 100)
        )
        for sub in subunits:
            self.vm.local_callees.append({
                "file": def_file,
                "function_name": callee_name + sub["suffix"],
                "distance": depth + 1,
                "code": sub["text"]
            })

        callee_queue.append((def_file, def_start + 1, callee_name, depth + 1))

    def trace_callees(self, callee_queue, processed_spans):
        """Perform BFS queue traversal for tracing function callees."""
        processed_callee_spans = set(processed_spans)
        callee_depth = self._arg_or_default("callee_depth", 0)

        while callee_queue:
            curr_file, curr_line, _, depth = callee_queue.popleft()
            if depth < callee_depth:
                start, end = extract_function_bounds(
                    curr_file, curr_line, file_cache=self.file_cache
                )
                if start is None:
                    continue
                callees = extract_callees(curr_file, start, end, file_cache=self.file_cache)
                for callee_name in callees:
                    self._process_single_callee(
                        callee_name, depth, processed_callee_spans, callee_queue
                    )
