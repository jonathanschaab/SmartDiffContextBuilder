import os
import re
import argparse
from collections import deque

from .cache import LRUFileCache, get_global_cache
from .sys_utils import run_command, get_git_diff_files, get_git_tracked_files, is_in_repo
from .ast_engine import extract_function_bounds, trace_lexical_dependencies_ast, trace_lexical_dependencies_regex, AST_ENGINE
from .lsp_client import get_lsp_references, cleanup_zombie_lsps
from .preprocessor import build_ffi_registry, trace_ffi_callers, analyze_compile_commands, trace_macro_expansion
from .volume_manager import VolumeManager
from .test_miner import get_coverage_data, mine_relevant_unit_tests
from . import lsp_client

def main():
    parser = argparse.ArgumentParser(description="Compile context-aware git diff tokens optimized for LLMs.")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("--max-lines", type=int, default=1500)
    parser.add_argument("--max-mb", type=float, default=2.0)
    parser.add_argument("--base-name", type=str, default="ContextLens")
    parser.add_argument("--max-cache-size", type=int, default=100)
    
    # Configurable Flags
    parser.add_argument("--max-interface-depth", type=int, default=15)
    parser.add_argument("--disable-pruning", action="store_true")
    parser.add_argument("--lsp-timeout", type=int, default=45)
    parser.add_argument("--no-language-server", action="store_true")
    parser.add_argument("--skip-ffi", action="store_true")
    parser.add_argument("--skip-macro-expansion", action="store_true")
    parser.add_argument("--caller-depth", type=int, default=1)
    parser.add_argument("--callee-depth", type=int, default=1)
    
    args = parser.parse_args()
    
    # Initialize global cache
    file_cache = get_global_cache(args.max_cache_size)
    lsp_client.USE_LSP = not args.no_language_server

    print(f"\n[ContextLens] Scanning Git Diff Workspace [Format: {args.format.upper()}]")
    
    diff_files = [f for f in get_git_diff_files() if is_in_repo(f)]
    if not diff_files: print("Workspace is clean."); return

    raw_diff = run_command(["git", "diff", "HEAD"])
    all_repo_files = [f for f in get_git_tracked_files() if is_in_repo(f)] 
    coverage_data = get_coverage_data()
    
    # FFI Pre-computation pass
    ffi_exports = set()
    if not args.skip_ffi:
        ffi_exports = build_ffi_registry(all_repo_files, file_cache=file_cache)

    vm = VolumeManager(args.format, args.max_lines, args.max_mb, args.base_name)
    vm.set_raw_diff(raw_diff)
    processed_spans = set() # Upgraded to track exact structural spans to prevent duplicate macro traces
	
    # Initialize linkage map for C++ files if compile_commands exists
    cpp_linkages = {}
    for f in diff_files:
        if f.endswith(('.cpp', '.c', '.hpp', '.h')):
            linkages = analyze_compile_commands(f)
            if linkages:
                cpp_linkages[f] = linkages

    queue = deque()

    # Step 1: Initialize the queue with root-level modified function blocks
    for file_path in diff_files:
        ext = os.path.splitext(file_path)[1]
        diff_lines = run_command(["git", "diff", "-U0", "HEAD", file_path]).splitlines()
        line_numbers = [int(m.group(1)) for l in diff_lines for m in [re.search(r'\+(\d+)', l)] if m]
        if not line_numbers or not os.path.exists(file_path): continue

        file_lines = file_cache.get_lines(file_path)

        for line_num in line_numbers:
            cov_status = "Covered" if file_path in coverage_data and line_num in coverage_data[file_path] else "Unknown"

            start, end = extract_function_bounds(file_path, line_num, file_cache=file_cache)
            if start is None: continue
            func_chunk = "".join(file_lines[start:end])
            
            name_match = re.search(r'\b(?:fn|def|function|sub|func|class|macro)\s+([A-Za-z0-9_]+)', file_lines[start])
            func_name = name_match.group(1) if name_match else f"block_lines_{start}_{end}"

            # Deduplication
            span_signature = f"{file_path}::line_{start}_to_{end}"
            if span_signature in processed_spans: continue
            processed_spans.add(span_signature)

            # 1. Funnel: Add Object
            vm.add_modified_object(file_path, func_name, f"// [Test Coverage: {cov_status}]\n{func_chunk}")

            # 2. Funnel: Add Tests
            vm.unit_tests.extend(mine_relevant_unit_tests(func_name, all_repo_files, current_source_file=file_path, file_cache=file_cache))

            # Push to queue for BFS caller tracing
            queue.append((file_path, start + 1, func_name, 0))

    # Step 2: Queue-based BFS caller traversal
    while queue:
        curr_file, curr_line, curr_func, depth = queue.popleft()

        if depth < args.caller_depth:
            # 3. Funnel: Trace Callers (LSP -> AST -> Regex)
            callers = get_lsp_references(curr_file, curr_line, curr_func, args.lsp_timeout, args.max_interface_depth, args.disable_pruning, file_cache=file_cache)
            ext = os.path.splitext(curr_file)[1]
            if callers is None:
                callers = trace_lexical_dependencies_ast(curr_func, all_repo_files, file_cache=file_cache) if AST_ENGINE.is_supported(ext) else trace_lexical_dependencies_regex(curr_func, all_repo_files, file_cache=file_cache)
            
            # INTEGRATION: Invoke Macro Expansion only if requested and C/C++
            if not args.skip_macro_expansion and ext in ['.c', '.cpp', '.hpp']:
                macro_results = trace_macro_expansion(curr_func, all_repo_files, file_cache=file_cache)
                for f_path, matches in macro_results.items():
                    if f_path not in callers: callers[f_path] = []
                    # Avoid duplicates if lexical trace already found it
                    for m in matches:
                        if not any(c['line'] == m['line'] for c in callers[f_path]):
                            callers[f_path].append(m)

            # Merge existing Build Linkages
            if curr_file in cpp_linkages:
                for req in cpp_linkages[curr_file]:
                    if req not in callers: callers[req] = []
                    callers[req].extend(cpp_linkages[curr_file][req])

            # Filter callers through is_in_repo
            filtered_callers = {}
            for fp, occs in callers.items():
                if fp == "[Pruned Instances]" or is_in_repo(fp):
                    filtered_callers[fp] = occs
            
            vm.add_callers(vm.local_callers, filtered_callers, "Lexical Dependency", confidence="MEDIUM", distance=depth + 1)

            # 4. Funnel: FFI
            if not args.skip_ffi and curr_func in ffi_exports:
                ffi_callers = trace_ffi_callers(curr_func, all_repo_files, source_ext=ext, file_cache=file_cache)
                filtered_ffi = {fp: occs for fp, occs in ffi_callers.items() if is_in_repo(fp)}
                vm.add_callers(vm.ffi_linkages, filtered_ffi, "FFI Linkage", distance=depth + 1)

            # Map discovered callers back to their containing functions and enqueue for next depth
            for ref_path, occurrences in filtered_callers.items():
                if ref_path == "[Pruned Instances]":
                    continue
                for occ in occurrences:
                    occ_line = occ["line"]
                    start, end = extract_function_bounds(ref_path, occ_line, file_cache=file_cache)
                    if start is None: continue
                    
                    ref_lines = file_cache.get_lines(ref_path)
                    if not ref_lines or start >= len(ref_lines): continue
                    
                    name_match = re.search(r'\b(?:fn|def|function|sub|func|class|macro)\s+([A-Za-z0-9_]+)', ref_lines[start])
                    occ_func = name_match.group(1) if name_match else f"block_lines_{start}_{end}"
                    
                    span_sig = f"{ref_path}::line_{start}_to_{end}"
                    if span_sig not in processed_spans:
                        processed_spans.add(span_sig)
                        queue.append((ref_path, start + 1, occ_func, depth + 1))

    # Final Execution: Process Funnel and Splice into Volumes
    vm.flush_all_volumes()
    cleanup_zombie_lsps()
    print("\n[ContextLens] Context packaging completed successfully.")
