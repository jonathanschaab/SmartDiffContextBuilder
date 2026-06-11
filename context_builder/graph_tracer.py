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
        lsp_timeout = getattr(self.args, "lsp_timeout", 45)
        if lsp_timeout is None:
            lsp_timeout = 45

        max_interface_depth = getattr(self.args, "max_interface_depth", 15)
        if max_interface_depth is None:
            max_interface_depth = 15

        disable_pruning = getattr(self.args, "disable_pruning", False)
        if disable_pruning is None:
            disable_pruning = False

        callers = get_lsp_references(
            curr_file,
            curr_line,
            curr_func,
            lsp_timeout,
            max_interface_depth,
            disable_pruning,
            file_cache=self.file_cache,
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
        skip_macro_expansion = getattr(self.args, "skip_macro_expansion", False)
        if skip_macro_expansion is None:
            skip_macro_expansion = False

        if (
            not skip_macro_expansion
            and get_language_profile(ext).supports_macro_expansion
        ):
            macro_results = trace_macro_expansion(
                curr_func, self.all_repo_files, file_cache=self.file_cache
            )
            for f_path, matches in macro_results.items():
                if f_path not in callers:
                    callers[f_path] = []
                for m in matches:
                    if not any(c['line'] == m['line'] for c in callers[f_path]):
                        callers[f_path].append(m)

        if curr_file in self.cpp_linkages:
            for req in self.cpp_linkages[curr_file]:
                if req not in callers:
                    callers[req] = []
                callers[req].extend(self.cpp_linkages[curr_file][req])

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

        skip_ffi = getattr(self.args, "skip_ffi", False)
        if skip_ffi is None:
            skip_ffi = False

        if not skip_ffi and curr_func in self.ffi_exports:
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
        caller_depth = getattr(self.args, "caller_depth", 0)
        if caller_depth is None:
            caller_depth = 0

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
        max_lines_val = getattr(self.args, "max_lines", 1000)
        if max_lines_val is None:
            max_lines_val = 1000
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
        callee_depth = getattr(self.args, "callee_depth", 0)
        if callee_depth is None:
            callee_depth = 0

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
