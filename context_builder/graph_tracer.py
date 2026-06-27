"""Graph traversal tracer for SmartDiffContextBuilder.

This module encapsulates call graph traversal algorithms, beginning from initial
modified files and tracing callers/callees.
"""

import os
import concurrent.futures
from collections import deque
from .ast_engine import (
    AST_ENGINE,
    extract_callees,
    extract_function_bounds,
    extract_identifiers_with_positions,
    find_callee_definition,
    resolve_variable_definition,
    split_massive_block_ast,
    trace_lexical_dependencies_ast,
    trace_lexical_dependencies_regex,
)
from .config import DEFAULT_LSP_INIT_TIMEOUT, DEFAULT_LSP_QUERY_TIMEOUT
from .languages import get_language_profile
from .lsp_client import get_lsp_references
from .preprocessor import trace_ffi_callers, trace_macro_expansion
from .sys_utils import is_in_repo

DEFAULT_DATA_FLOW_BATCH_SIZE = 32


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
        """Return an argument value, treating absent args and values alike."""
        # Three-argument getattr already returns its fallback when self.args is
        # None. Keep the explicit branch so that this supported tracer state is
        # clear and does not look like an unsafe attribute access.
        if self.args is None:
            return default
        value = getattr(self.args, name, None)
        return default if value is None else value

    def _positive_int_arg_or_default(self, name, default):
        """Return a positive integer argument value or default."""
        value = self._arg_or_default(name, default)
        if isinstance(value, bool):
            return default
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

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

    def _merge_macro_and_build_linkages(self, curr_file, curr_func, callers):
        """Merge macro expansion and C++ build system compilation linkages into callers."""
        if (
            not self._arg_or_default("skip_macro_expansion", False)
            and get_language_profile(curr_file).supports_macro_expansion
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
        self._merge_macro_and_build_linkages(curr_file, curr_func, callers)

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

    def _enqueue_identifiers(self, file_path, line_numbers, queue, processed_vars, depth):
        """Extract identifiers from lines in a file and enqueue them for tracing."""
        abs_path = os.path.abspath(file_path)
        pos_ids = extract_identifiers_with_positions(abs_path, line_numbers, self.file_cache)
        for var_name, ln, char_off in pos_ids:
            var_key = (abs_path, var_name, ln, char_off)
            if var_key not in processed_vars:
                processed_vars.add(var_key)
                queue.append((abs_path, var_name, ln, char_off, depth))

    def _resolve_data_flow_item(self, item, lsp_timeout):
        """Resolve one queued data-flow identifier, capturing failures."""
        file_path, var_name, line_num, char_offset, _ = item
        try:
            res = resolve_variable_definition(
                file_path,
                var_name,
                line_num,
                char_offset,
                file_cache=self.file_cache,
                timeout=lsp_timeout,
            )
            return item, res, None
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return item, None, exc

    def _resolve_data_flow_batch(self, batch, lsp_timeout, batch_size):
        """Resolve a bounded batch of data-flow identifiers concurrently."""
        if len(batch) == 1:
            return [self._resolve_data_flow_item(batch[0], lsp_timeout)]
        max_workers = min(batch_size, len(batch))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(
                executor.map(
                    lambda item: self._resolve_data_flow_item(item, lsp_timeout),
                    batch,
                )
            )

    def _process_data_flow_result(
        self, item, res, exc, processed_defs, processed_vars, queue, data_depth
    ):
        """Add resolved definitions and enqueue follow-up identifiers."""
        _file_path, var_name, _line_num, _char_offset, depth = item
        if exc is not None:
            print(
                f"\n[SmartDiffContextBuilder Warning] Failed to resolve "
                f"variable definition for {var_name}: {exc}"
            )
            return
        if not res:
            return

        for definition in res.get("definitions", []):
            def_path = definition["path"]
            def_line = definition["line"]
            def_code = definition["code"]

            abs_def_path = os.path.abspath(def_path)
            def_key = (abs_def_path, def_line)
            if def_key in processed_defs:
                continue
            processed_defs.add(def_key)

            try:
                rel_path = os.path.relpath(abs_def_path, os.getcwd())
            except ValueError:
                rel_path = abs_def_path
            self.vm.add_data_state(rel_path, def_line, def_code)

            if depth + 1 >= data_depth or not os.path.exists(abs_def_path):
                continue

            self._enqueue_identifiers(
                abs_def_path, [def_line], queue, processed_vars, depth + 1
            )

    @staticmethod
    def _next_data_flow_batch(queue, batch_size):
        """Pop the next same-depth batch from the BFS queue."""
        batch = []
        while queue and not batch:
            batch.append(queue.popleft())
        if not batch:
            return batch

        depth = batch[0][4]
        while queue and len(batch) < batch_size and queue[0][4] == depth:
            batch.append(queue.popleft())
        return batch

    def trace_data_flow(self, diff_files_lines):
        """Trace data flow / variable definitions recursively from modified diff lines."""
        data_depth = self._arg_or_default("data_depth", 1)
        if data_depth <= 0:
            return

        queue = deque()
        processed_vars = set()  # set of (file_path, var_name, line_num, char_offset)
        processed_defs = set()  # set of (path, line) to avoid duplicate resolution output

        # 1. Initialize queue with identifiers from modified diff lines
        for file_path, line_numbers in diff_files_lines.items():
            self._enqueue_identifiers(file_path, line_numbers, queue, processed_vars, 0)

        # 2. BFS queue traversal
        lsp_timeout = self._arg_or_default("lsp_timeout", DEFAULT_LSP_QUERY_TIMEOUT)
        batch_size = self._positive_int_arg_or_default(
            "data_flow_batch_size", DEFAULT_DATA_FLOW_BATCH_SIZE
        )

        while queue:
            batch = self._next_data_flow_batch(queue, batch_size)
            if not batch:
                continue
            results = self._resolve_data_flow_batch(batch, lsp_timeout, batch_size)
            for item, res, exc in results:
                self._process_data_flow_result(
                    item, res, exc, processed_defs, processed_vars, queue, data_depth
                )
