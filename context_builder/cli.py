import os
import re
import argparse
import subprocess
import shutil
import sys
import tempfile
from collections import deque

from .cache import LRUFileCache, get_global_cache
from .sys_utils import run_command, get_git_diff_files, get_git_tracked_files, is_in_repo
from .ast_engine import extract_function_bounds, trace_lexical_dependencies_ast, trace_lexical_dependencies_regex, AST_ENGINE, extract_callees, find_callee_definition, split_massive_block_ast
from .lsp_client import get_lsp_references, cleanup_zombie_lsps
from .preprocessor import build_ffi_registry, trace_ffi_callers, analyze_compile_commands, trace_macro_expansion
from .volume_manager import VolumeManager
from .test_miner import get_coverage_data, mine_relevant_unit_tests
from . import lsp_client

def resolve_commit_ref(ref):
    """Resolves a git ref (like HEAD~2, my_tag) to a full commit SHA."""
    out = run_command(["git", "rev-parse", "--verify", ref])
    if not out.strip():
        out = run_command(["git", "rev-parse", ref])
    return out.strip()

def parse_and_resolve_range(range_str):
    """Parses range string into (start_sha, end_sha) commit hashes."""
    range_str = range_str.strip()
    
    # Format 4: -N (implied HEAD as end)
    m = re.match(r'^-(\d+)$', range_str)
    if m:
        end_ref = "HEAD"
        count = int(m.group(1))
        start_ref = f"HEAD~{count}"
    
    # Format 3: END-N
    elif re.match(r'^(.+)-(\d+)$', range_str):
        m = re.match(r'^(.+)-(\d+)$', range_str)
        end_ref = m.group(1)
        count = int(m.group(2))
        start_ref = f"{end_ref}~{count}"

    # Format 2: START+N
    elif re.match(r'^(.+)\+(\d+)$', range_str):
        m = re.match(r'^(.+)\+(\d+)$', range_str)
        start_ref = m.group(1)
        count = int(m.group(2))
        
        start_sha = resolve_commit_ref(start_ref)
        if not start_sha:
            raise ValueError(f"Could not resolve start commit: {start_ref}")
            
        # Get chronological list of commits from start_sha to HEAD
        commits_out = run_command(["git", "log", "--reverse", "--format=%H", f"{start_sha}..HEAD"])
        commits = [c.strip() for c in commits_out.splitlines() if c.strip()]
        if len(commits) < count:
            commits_out = run_command(["git", "log", "--reverse", "--format=%H", f"{start_sha}..main"])
            commits = [c.strip() for c in commits_out.splitlines() if c.strip()]
            
        if len(commits) < count:
            raise ValueError(f"Not enough commits after {start_ref} (requested +{count}, found {len(commits)})")
            
        end_ref = commits[count - 1]

    # Format 1: START..END
    elif ".." in range_str:
        start_ref, end_ref = range_str.split("..", 1)
        start_ref = start_ref.strip()
        end_ref = end_ref.strip()
    
    else:
        raise ValueError(f"Invalid commit range format: '{range_str}'")

    start_sha = resolve_commit_ref(start_ref)
    end_sha = resolve_commit_ref(end_ref)
    if not start_sha:
        raise ValueError(f"Could not resolve start commit: {start_ref}")
    if not end_sha:
        raise ValueError(f"Could not resolve end commit: {end_ref}")
        
    return start_sha, end_sha

def run_scan(args, start_ref=None, end_ref=None, output_dir="."):
    # Initialize global cache
    file_cache = get_global_cache(args.max_cache_size)
    lsp_client.USE_LSP = not args.no_language_server

    print(f"\n[ContextLens] Scanning Git Diff Workspace [Format: {args.format.upper()}]")
    
    diff_files = [f for f in get_git_diff_files(start_ref, end_ref) if is_in_repo(f)]
    if not diff_files: print("Workspace is clean."); return

    if start_ref and end_ref:
        raw_diff = run_command(["git", "diff", start_ref, end_ref])
    else:
        raw_diff = run_command(["git", "diff", "HEAD"])
        
    all_repo_files = [f for f in get_git_tracked_files() if is_in_repo(f)] 
    coverage_data = get_coverage_data()
    
    # FFI Pre-computation pass
    ffi_exports = set()
    if not args.skip_ffi:
        ffi_exports = build_ffi_registry(all_repo_files, file_cache=file_cache)

    vm = VolumeManager(args.format, args.max_lines, args.max_mb, args.base_name, output_dir=output_dir)
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
    callee_queue = deque()

    # Step 1: Initialize the queue with root-level modified function blocks
    for file_path in diff_files:
        ext = os.path.splitext(file_path)[1]
        if start_ref and end_ref:
            diff_lines = run_command(["git", "diff", "-U0", start_ref, end_ref, "--", file_path]).splitlines()
        else:
            diff_lines = run_command(["git", "diff", "-U0", "HEAD", file_path]).splitlines()
            
        line_numbers = []
        for line in diff_lines:
            if line.startswith("@@"):
                # Parse git unified diff hunk headers specifically (e.g., @@ -1,4 +1,8 @@)
                # to extract the complete range of modified line numbers.
                m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
                if m:
                    start = int(m.group(1))
                    count = int(m.group(2)) if m.group(2) else 1
                    line_numbers.extend(range(start, start + count))
        if not line_numbers or not os.path.exists(file_path): continue

        file_lines = file_cache.get_lines(file_path)

        for line_num in line_numbers:
            cov_status = "Covered" if file_path in coverage_data and line_num in coverage_data[file_path] else "Unknown"

            start, end = extract_function_bounds(file_path, line_num, file_cache=file_cache)
            if start is None: continue
            func_chunk = "".join(file_lines[start:end])
            
            # If a function starts with a decorator or spans multiple lines, searching file_lines[start] might fail.
            # We search in func_chunk instead, using the walrus operator to simplify the assignment and condition.
            func_name = name_match.group(1) if (name_match := re.search(r'\b(?:fn|def|function|sub|func|class|macro)\s+([A-Za-z0-9_]+)', func_chunk)) else f"block_lines_{start}_{end}"

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
            callee_queue.append((file_path, start + 1, func_name, 0))

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
                    if occ_line <= 0: continue
                    start, end = extract_function_bounds(ref_path, occ_line, file_cache=file_cache)
                    if start is None: continue
                    
                    ref_lines = file_cache.get_lines(ref_path)
                    if not ref_lines or start >= len(ref_lines): continue
                    
                    # If a function starts with a decorator or spans multiple lines, searching ref_lines[start] might fail.
                    # We join ref_lines[start:end] to get the full function chunk and search in ref_chunk,
                    # using the walrus operator to simplify the regex matching expression.
                    ref_chunk = "".join(ref_lines[start:end])
                    occ_func = name_match.group(1) if (name_match := re.search(r'\b(?:fn|def|function|sub|func|class|macro)\s+([A-Za-z0-9_]+)', ref_chunk)) else f"block_lines_{start}_{end}"
                    
                    span_sig = f"{ref_path}::line_{start}_to_{end}"
                    if span_sig not in processed_spans:
                        processed_spans.add(span_sig)
                        queue.append((ref_path, start + 1, occ_func, depth + 1))

    # Step 2b: Queue-based BFS callee traversal
    processed_callee_spans = set()
    for span in processed_spans:
        processed_callee_spans.add(span)

    while callee_queue:
        curr_file, curr_line, curr_func, depth = callee_queue.popleft()
        if depth < args.callee_depth:
            start, end = extract_function_bounds(curr_file, curr_line, file_cache=file_cache)
            if start is None: continue
            callees = extract_callees(curr_file, start, end, file_cache=file_cache)
            for callee_name in callees:
                def_file, def_line = find_callee_definition(callee_name, all_repo_files, file_cache=file_cache)
                if not def_file or not def_line: continue
                def_start, def_end = extract_function_bounds(def_file, def_line, file_cache=file_cache)
                if def_start is None: continue
                span_sig = f"{def_file}::line_{def_start}_to_{def_end}"
                if span_sig in processed_callee_spans: continue
                processed_callee_spans.add(span_sig)

                ref_lines = file_cache.get_lines(def_file)
                if not ref_lines or def_start >= len(ref_lines): continue

                func_chunk = "".join(ref_lines[def_start:def_end])
                subunits = split_massive_block_ast(func_chunk, def_file, args.max_lines - 100)
                for sub in subunits:
                    vm.local_callees.append({
                        "file": def_file,
                        "function_name": callee_name + sub["suffix"],
                        "distance": depth + 1,
                        "code": sub["text"]
                    })
                
                callee_queue.append((def_file, def_start + 1, callee_name, depth + 1))

    # Final Execution: Process Funnel and Splice into Volumes
    vm.flush_all_volumes()
    cleanup_zombie_lsps()
    print("\n[ContextLens] Context packaging completed successfully.")

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
    parser.add_argument("--commit-range", type=str, default=None, help="Sequence of commits to analyze (e.g. START..END, -3, START+2, END-3)")
    
    args = parser.parse_args()
    
    if args.commit_range:
        try:
            start_sha, end_sha = parse_and_resolve_range(args.commit_range)
        except Exception as e:
            print(f"\n[ContextLens Error] Invalid commit range: {e}")
            sys.exit(1)

        original_cwd = os.getcwd()
        # Create a unique temp folder inside the system temp directory for the worktree checkout
        temp_worktree_dir = tempfile.mkdtemp(prefix="context_lens_worktree_")
        
        print(f"\n[ContextLens] Setting up temporary worktree for commit {end_sha[:8]}...")
        # Check out end_sha in a detached state to avoid branch checkout conflicts
        add_res = subprocess.run(
            ["git", "worktree", "add", "--detach", temp_worktree_dir, end_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if add_res.returncode != 0:
            print(f"\n[ContextLens Error] Failed to create git worktree: {add_res.stderr.strip()}")
            shutil.rmtree(temp_worktree_dir, ignore_errors=True)
            sys.exit(1)

        # Copy compile_commands.json if it exists in the original repo root
        compile_commands_path = os.path.join(original_cwd, "compile_commands.json")
        if os.path.exists(compile_commands_path):
            shutil.copy(compile_commands_path, os.path.join(temp_worktree_dir, "compile_commands.json"))

        try:
            os.chdir(temp_worktree_dir)
            # Run scan inside the worktree checking out end_ref and diffing against start_ref
            run_scan(args, start_ref=start_sha, end_ref=end_sha, output_dir=original_cwd)
        finally:
            os.chdir(original_cwd)
            print(f"\n[ContextLens] Cleaning up temporary worktree...")
            subprocess.run(
                ["git", "worktree", "remove", "--force", temp_worktree_dir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            shutil.rmtree(temp_worktree_dir, ignore_errors=True)
    else:
        # Run scan directly in current workspace
        run_scan(args)
