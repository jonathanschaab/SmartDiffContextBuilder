"""Command-line interface and orchestrator for SmartDiffContextBuilder.

This module parses command-line arguments, manages temporary git worktrees
for commit range analysis, runs context extraction passes, and outputs context.
"""

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import deque

from .ast_engine import (
    AST_ENGINE,
    extract_function_bounds,
)
from .cache import get_global_cache
from .config import (
    CONFIG,
    DEFAULT_LSP_INIT_TIMEOUT,
    DEFAULT_LSP_QUERY_TIMEOUT,
    WORKTREE_LSP_INIT_TIMEOUT,
    WORKTREE_LSP_QUERY_TIMEOUT,
    generate_commented_config,
    load_json_with_comments,
)
from .lsp_client import cleanup_zombie_lsps
from .languages import get_language_profile
from .path_utils import (
    build_root_replacement_variants,
    clear_path_case_caches,
    detect_root_case_sensitivity,
    find_artifact_path,
    path_is_within_root,
)
from .preprocessor import (
    analyze_compile_commands,
    build_ffi_registry,
    clear_preprocessed_cache,
)
from .sys_utils import (
    get_comment_prefix,
    get_git_diff_files,
    get_git_tracked_files,
    is_in_repo,
    run_git_command,
    run_git_process,
    run_command,
)
from .test_miner import get_coverage_data, mine_relevant_unit_tests
from .volume_manager import VolumeManager
from . import lsp_client
from .graph_tracer import CallGraphTracer, extract_function_name



def resolve_commit_ref(ref):
    """Resolves a git ref (like HEAD~2, my_tag) to a full commit SHA."""
    out = run_git_command(["git", "rev-parse", "--verify", ref])
    if not out.strip():
        out = run_git_command(["git", "rev-parse", ref])
    return out.strip()

def get_default_branch():
    """Queries git for first existing branch from ['main', 'master']."""
    for branch in ["main", "master"]:
        if run_git_command(["git", "rev-parse", "--verify", branch]).strip():
            return branch
    return "main"

def parse_and_resolve_range(range_str):
    """Parses range string into (start_sha, end_sha) commit hashes."""
    range_str = range_str.strip()

    # Format 4: -N (implied HEAD as end)
    if m := re.match(r'^-(\d+)$', range_str):
        end_ref = "HEAD"
        count = int(m.group(1))
        start_ref = f"HEAD~{count}"

    # Format 3: END-N
    elif m := re.match(r'^(.+)-(\d+)$', range_str):
        end_ref = m.group(1)
        count = int(m.group(2))
        start_ref = f"{end_ref}~{count}"

    # Format 2: START+N
    elif m := re.match(r'^(.+)\+(\d+)$', range_str):
        start_ref = m.group(1)
        count = int(m.group(2))

        start_sha = resolve_commit_ref(start_ref)
        if not start_sha:
            raise ValueError(f"Could not resolve start commit: {start_ref}")

        if count == 0:
            # Defensive check: if count is 0, START+0 resolves to START itself
            end_ref = start_ref
        else:
            # Get chronological list of commits from start_sha to HEAD
            commits_out = run_git_command([
                "git", "log", "--reverse", "--format=%H", f"{start_sha}..HEAD"
            ])
            commits = [c.strip() for c in commits_out.splitlines() if c.strip()]
            if len(commits) < count:
                default_branch = get_default_branch()
                commits_out = run_git_command([
                    "git", "log", "--reverse", "--format=%H",
                    f"{start_sha}..{default_branch}"
                ])
                commits = [c.strip() for c in commits_out.splitlines() if c.strip()]

            if len(commits) < count:
                raise ValueError(
                    f"Not enough commits after {start_ref} "
                    f"(requested +{count}, found {len(commits)})"
                )

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



def _extract_line_numbers_from_diff(file_path, start_ref, end_ref):
    """Retrieve modified line numbers for a file from git diff."""
    if start_ref and end_ref:
        diff_lines = run_git_command([
            "git", "diff", "-U0", start_ref, end_ref, "--", file_path
        ]).splitlines()
    else:
        diff_lines = run_git_command(["git", "diff", "-U0", "HEAD", file_path]).splitlines()

    line_numbers = []
    for line in diff_lines:
        if line.startswith("@@"):
            m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                line_numbers.extend(range(start, start + count))
    return line_numbers


def _process_single_diff_line(
    file_path, line_num, file_lines, coverage_data, processed_spans,
    vm, queue, callee_queue, all_repo_files, file_cache
):
    """Analyze a single modified line to identify enclosing function bounds."""
    normalized_file_path = file_path.replace("\\", "/")
    is_cov = (
        normalized_file_path in coverage_data
        and line_num in coverage_data[normalized_file_path]
    )
    cov_status = "Covered" if is_cov else "Unknown"

    start, end = extract_function_bounds(
        file_path, line_num, file_cache=file_cache
    )
    if start is None:
        return
    func_chunk = "".join(file_lines[start:end])

    profile = get_language_profile(file_path)
    cleaned_func_chunk = "\n".join(
        profile.strip_strings_and_comments(line)
        for line in func_chunk.splitlines()
    )
    func_name = extract_function_name(
        cleaned_func_chunk,
        start,
        end,
        file_path=file_path,
    )

    span_signature = f"{file_path}::line_{start}_to_{end}"
    if span_signature in processed_spans:
        return
    processed_spans.add(span_signature)

    comment_prefix = get_comment_prefix(file_path)
    vm.add_modified_object(
        file_path,
        func_name,
        f"{comment_prefix} [Test Coverage: {cov_status}]\n{func_chunk}",
    )

    vm.unit_tests.extend(
        mine_relevant_unit_tests(
            func_name,
            all_repo_files,
            current_source_file=file_path,
            file_cache=file_cache,
        )
    )

    queue.append((file_path, start + 1, func_name, 0))
    callee_queue.append((file_path, start + 1, func_name, 0))


def _process_diff_files(
    diff_files, start_ref, end_ref, file_cache, coverage_data,
    processed_spans, vm, queue, callee_queue, all_repo_files
):
    """Process modified files from git diff and initialize tracking queues."""
    for file_path in diff_files:
        line_numbers = _extract_line_numbers_from_diff(file_path, start_ref, end_ref)
        if not line_numbers or not os.path.exists(file_path):
            continue

        file_lines = file_cache.get_lines(file_path)

        for line_num in line_numbers:
            _process_single_diff_line(
                file_path, line_num, file_lines, coverage_data, processed_spans,
                vm, queue, callee_queue, all_repo_files, file_cache
            )


def _run_comparison_scans(args, start_ref, end_ref, output_dir, repo_root):
    """Run both LSP and Fallback passes in comparison mode."""
    print(
        "\n[SmartDiffContextBuilder] Comparison mode enabled. "
        "Running LSP and Fallback passes..."
    )
    # Pass 1: LSP
    args_lsp = copy.copy(args)
    args_lsp.compare = False
    args_lsp.no_language_server = False
    original_base_name = args.base_name or "SmartDiffContextBuilder"
    args_lsp.base_name = f"{original_base_name}_lsp"

    print("\n--- Running Pass 1: Language Server (LSP) ---")
    run_scan(
        args_lsp,
        start_ref=start_ref,
        end_ref=end_ref,
        output_dir=output_dir,
        repo_root=repo_root,
    )

    # Pass 2: Fallback
    args_fallback = copy.copy(args)
    args_fallback.compare = False
    args_fallback.no_language_server = True
    args_fallback.base_name = f"{original_base_name}_fallback"

    print("\n--- Running Pass 2: Fallback (AST/Regex) ---")
    run_scan(
        args_fallback,
        start_ref=start_ref,
        end_ref=end_ref,
        output_dir=output_dir,
        repo_root=repo_root,
    )

    fmt_opt = getattr(args, "format", "md")
    ext = "json" if str(fmt_opt).lower() == "json" else "md"
    lsp_file = os.path.join(output_dir, f"{args_lsp.base_name}_final.{ext}")
    fallback_file = os.path.join(
        output_dir, f"{args_fallback.base_name}_final.{ext}"
    )
    print("\n[SmartDiffContextBuilder] Comparison files generated:")
    print(f"  - LSP: {lsp_file}")
    print(f"  - Fallback: {fallback_file}")


def run_scan(args, start_ref=None, end_ref=None, output_dir=".", repo_root=None):
    """Execute the context scan."""
    if getattr(args, "compare", False):
        _run_comparison_scans(
            args,
            start_ref=start_ref,
            end_ref=end_ref,
            output_dir=output_dir,
            repo_root=repo_root,
        )
        return


    file_cache = get_global_cache(args.max_cache_size_mb)
    clear_preprocessed_cache()
    lsp_client.USE_LSP = not args.no_language_server

    print(f"\n[SmartDiffContextBuilder] Scanning Git Diff Workspace "
          f"[Format: {args.format.upper()}]")

    diff_files = [
        f for f in get_git_diff_files(start_ref, end_ref) if is_in_repo(f)
    ]
    if not diff_files:
        print("Workspace is clean.")
        return

    if start_ref and end_ref:
        raw_diff = run_command(["git", "diff", start_ref, end_ref])
    else:
        raw_diff = run_command(["git", "diff", "HEAD"])

    all_repo_files = [f for f in get_git_tracked_files() if is_in_repo(f)]
    coverage_data = get_coverage_data()

    ffi_exports = set()
    if not args.skip_ffi:
        ffi_exports = build_ffi_registry(all_repo_files, file_cache=file_cache)

    vm = VolumeManager(
        args.format, args.max_lines, args.max_mb, args.base_name, output_dir=output_dir
    )
    vm.set_raw_diff(raw_diff)
    processed_spans = set()

    cpp_linkages = {}
    for f in diff_files:
        if get_language_profile(f).supports_compile_commands:
            linkages = analyze_compile_commands(f, repo_root=repo_root)
            if linkages:
                cpp_linkages[f] = linkages

    queue = deque()
    callee_queue = deque()

    _process_diff_files(
        diff_files, start_ref, end_ref, file_cache, coverage_data,
        processed_spans, vm, queue, callee_queue, all_repo_files
    )

    tracer = CallGraphTracer(
        file_cache=file_cache,
        all_repo_files=all_repo_files,
        ffi_exports=ffi_exports,
        cpp_linkages=cpp_linkages,
        vm=vm,
        args=args,
    )

    tracer.trace_callers(queue, processed_spans)
    tracer.trace_callees(callee_queue, processed_spans)

    vm.flush_all_volumes()
    cleanup_zombie_lsps()
    print("\n[SmartDiffContextBuilder] Context packaging completed successfully.")


def _validate_config_type(k, v):
    """Validate type of config value v against default in CONFIG."""
    if k not in CONFIG:
        return
    default = CONFIG[k]
    expected_phrase = None
    is_valid = True

    if default is None:
        expected_phrase = "a string"
        is_valid = v is None or isinstance(v, str)
    elif isinstance(default, str):
        expected_phrase = "a string"
        is_valid = isinstance(v, str)
    elif isinstance(default, bool):
        expected_phrase = "a boolean"
        is_valid = isinstance(v, bool)
    elif isinstance(default, int):
        expected_phrase = "an integer"
        is_valid = isinstance(v, int) and not isinstance(v, bool)
    elif isinstance(default, float):
        expected_phrase = "a float"
        is_valid = isinstance(v, (int, float)) and not isinstance(v, bool)

    if expected_phrase and not is_valid:
        print(
            f"[SmartDiffContextBuilder Error] Config key '{k}' must be "
            f"{expected_phrase}, got {type(v).__name__}"
        )
        sys.exit(1)


def _apply_config_override(key, value, error_subject="Config key"):
    """Validate and apply one config override using collection merge semantics."""
    current = CONFIG[key]
    if isinstance(current, dict):
        expected_type = "dictionary"
        valid = isinstance(value, dict)
    elif isinstance(current, list):
        expected_type = "list"
        valid = isinstance(value, list)
    else:
        _validate_config_type(key, value)
        CONFIG[key] = value
        return

    if not valid:
        print(
            f"[SmartDiffContextBuilder Error] {error_subject} '{key}' must be "
            f"a {expected_type}, got {type(value).__name__}"
        )
        sys.exit(1)

    if isinstance(current, dict):
        current.update(value)
    else:
        CONFIG[key] = value


def _parse_config_file(args_config):
    """Load config file if specified and merge into CONFIG."""
    if not args_config or not isinstance(args_config, str):
        return
    try:
        loaded_cfg = load_json_with_comments(args_config)
        if not isinstance(loaded_cfg, dict):
            print(
                f"[SmartDiffContextBuilder Error] Config file {args_config} "
                f"must be a JSON object (dictionary)"
            )
            sys.exit(1)
        for k, v in loaded_cfg.items():
            if k not in CONFIG:
                print(f"[Warning] Unknown config key: {k}")
                continue
            _apply_config_override(k, v)
        clear_path_case_caches()
    except Exception as e:  # pylint: disable=broad-exception-caught
        if isinstance(e, SystemExit):
            raise
        print(f"[SmartDiffContextBuilder Error] Failed to load config from {args_config}: {e}")
        sys.exit(1)


def _merge_cli_mappings(args, active_overrides):
    """Merge basic/primitive command line argument overrides into CONFIG."""
    cli_mappings = {
        "format": "format",
        "max_lines": "max_lines",
        "max_mb": "max_mb",
        "base_name": "base_name",
        "max_cache_size_mb": "max_cache_size_mb",
        "max_interface_depth": "max_interface_depth",
        "disable_pruning": "disable_pruning",
        "lsp_init_timeout": "lsp_init_timeout",
        "lsp_timeout": "lsp_timeout",
        "ripgrep_timeout": "ripgrep_timeout",
        "git_timeout": "git_timeout",
        "git_probe_timeout": "git_probe_timeout",
        "no_language_server": "no_language_server",
        "compare": "compare",
        "skip_ffi": "skip_ffi",
        "skip_macro_expansion": "skip_macro_expansion",
        "caller_depth": "caller_depth",
        "callee_depth": "callee_depth",
        "commit_range": "commit_range",
        "func_decl_pattern": "func_decl_pattern",
        "def_pattern_template": "def_pattern_template",
        "cpp_def_pattern_template": "cpp_def_pattern_template",
        "callee_pattern": "callee_pattern",
        "ffi_rg_pattern": "ffi_rg_pattern",
    }

    for arg_name, cfg_key in cli_mappings.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            _apply_config_override(cfg_key, val)
            active_overrides.append(cfg_key)


def _merge_json_mappings(args, active_overrides):
    """Merge JSON command line overrides into CONFIG."""
    def parse_cli_json(val, name):
        if not isinstance(val, str):
            return val
        try:
            return json.loads(val)
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"[SmartDiffContextBuilder Error] CLI argument {name} is not valid JSON: {e}")
            sys.exit(1)

    json_mappings = {
        "lang_map": "lang_map",
        "bindings": "bindings",
        "dependency_query_strings": "dependency_query_strings",
        "callee_query_strings": "callee_query_strings",
        "callee_ignored_keywords": "callee_ignored_keywords",
        "ffi_patterns": "ffi_patterns",
        "build_directories": "build_directories",
    }

    for arg_name, cfg_key in json_mappings.items():
        val = getattr(args, arg_name, None)
        if val is not None:
            parsed = parse_cli_json(val, f"--{arg_name.replace('_', '-')}")
            _apply_config_override(
                cfg_key,
                parsed,
                error_subject="CLI override for key",
            )
            active_overrides.append(cfg_key)


def _merge_cli_overrides(args):
    """Merge CLI overrides (both direct value and JSON strings) into CONFIG."""
    active_overrides = []
    _merge_cli_mappings(args, active_overrides)
    _merge_json_mappings(args, active_overrides)
    clear_path_case_caches()

    # Force AST engine re-initialization because config might have changed bindings
    # pylint: disable=protected-access
    AST_ENGINE._initialized = False
    return active_overrides


def _create_config_if_requested(args_create_config, active_overrides):
    """Write config file to disk if requested and exit."""
    if args_create_config and isinstance(args_create_config, str):
        try:
            config_content = generate_commented_config(active_overrides)
            parent_dir = os.path.dirname(os.path.abspath(args_create_config))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(args_create_config, "w", encoding="utf-8") as f:
                f.write(config_content)
            print("[SmartDiffContextBuilder] Created configuration template at: "
                  f"{args_create_config}")
            sys.exit(0)
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"[SmartDiffContextBuilder Error] Failed to create configuration file: {e}")
            sys.exit(1)


def _copy_worktree_artifact(filename, original_cwd, temp_worktree_dir, copy_fn):
    """Find and safely copy/rewrite a build artifact to the temporary worktree."""
    artifact_path = find_artifact_path(filename, original_cwd)
    if not artifact_path:
        return
    use_fallback = False
    try:
        rel_path = os.path.relpath(artifact_path, original_cwd)
        if rel_path.startswith("..") or os.path.isabs(rel_path):
            use_fallback = True
        else:
            target_path = os.path.abspath(os.path.join(temp_worktree_dir, rel_path))
            if not path_is_within_root(target_path, temp_worktree_dir):
                use_fallback = True
    except (ValueError, TypeError, AttributeError, OSError):
        use_fallback = True

    if use_fallback:
        target_path = os.path.join(temp_worktree_dir, filename)

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    copy_fn(artifact_path, target_path)


def _setup_temp_worktree(temp_worktree_dir, end_sha, original_cwd):
    """Set up git worktree and copy configuration files."""
    add_res = run_git_process(
        ["git", "worktree", "add", "--detach", temp_worktree_dir, end_sha],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if not add_res or add_res.returncode != 0:
        error_text = (
            add_res.stderr.strip()
            if add_res and add_res.stderr
            else "git worktree add failed"
        )
        print(
            f"\n[SmartDiffContextBuilder Error] Failed to create git worktree: "
            f"{error_text}"
        )
        try:
            shutil.rmtree(temp_worktree_dir, ignore_errors=True)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        sys.exit(1)

    _copy_worktree_artifact(
        "compile_commands.json",
        original_cwd,
        temp_worktree_dir,
        lambda src, dest: _rewrite_worktree_compile_commands(
            src,
            dest,
            original_cwd,
            temp_worktree_dir,
        ),
    )

    _copy_worktree_artifact(
        "coverage.xml",
        original_cwd,
        temp_worktree_dir,
        shutil.copy,
    )

def _build_worktree_root_replacements(original_root, worktree_root):
    """Build boundary-aware root replacements for both slash styles."""
    variants = build_root_replacement_variants(original_root, worktree_root)
    case_sensitive = detect_root_case_sensitivity(original_root)
    replacements = []
    seen_sources = set()
    for source_root, target_root in variants:
        dedupe_key = source_root if case_sensitive else source_root.lower()
        if not source_root or dedupe_key in seen_sources:
            continue
        seen_sources.add(dedupe_key)
        pattern = re.compile(
            re.escape(source_root) + r'(?=[/\\:;\s"\']|$)',
            0 if case_sensitive else re.IGNORECASE,
        )
        replacements.append((source_root, target_root, pattern, case_sensitive))
    return replacements


def _rewrite_compile_commands_payload(payload, original_root, worktree_root):
    """Recursively rewrite compile database paths from the source repo to the worktree."""
    replacements = _build_worktree_root_replacements(original_root, worktree_root)
    return _rewrite_compile_commands_payload_with_replacements(
        payload,
        replacements,
    )


def _rewrite_compile_commands_payload_with_replacements(payload, replacements):
    """Recursively rewrite compile database paths in place using precomputed replacements."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            payload[key] = _rewrite_compile_commands_payload_with_replacements(
                value,
                replacements,
            )
        return payload
    if isinstance(payload, list):
        for idx, item in enumerate(payload):
            payload[idx] = _rewrite_compile_commands_payload_with_replacements(
                item,
                replacements,
            )
        return payload
    if not isinstance(payload, str):
        return payload
    rewritten = payload
    for source_root, target_root, pattern, case_sensitive in replacements:
        haystack = rewritten if case_sensitive else rewritten.lower()
        needle = source_root if case_sensitive else source_root.lower()
        if needle not in haystack:
            continue
        # We benchmarked two pattern.sub replacement styles on a 50k-entry
        # synthetic compile database. A lambda avoids re.sub interpreting
        # backslashes in target_root (common on Windows) as escape sequences,
        # but it adds Python callback overhead on every match. Escaping
        # backslashes directly with .replace("\\", "\\\\") preserves the same
        # output while letting re.sub perform the replacement in C (~10% faster
        # on the full rewrite in scripts/benchmark_rewrite_sub.py).
        rewritten = pattern.sub(
            target_root.replace("\\", "\\\\"),
            rewritten,
        )
    return rewritten


def _rewrite_worktree_compile_commands(
    compile_commands_path,
    worktree_compile_commands_path,
    original_root,
    worktree_root,
):
    """Copy and rewrite compile_commands.json so clangd stays inside the worktree."""
    try:
        with open(compile_commands_path, encoding="utf-8") as source_file:
            payload = json.load(source_file)

        # We benchmarked three approaches here on a 50k-entry synthetic compile
        # database. Raw JSON text replacement was faster than the structured
        # walk, but it increased peak memory and was more fragile around escaped
        # paths and boundary handling. An ijson streaming prototype reduced peak
        # memory dramatically (~0.65 MiB versus ~63 MiB traced peak for the
        # current file-to-file rewrite) but was slower (~4.27s versus ~3.64s).
        # We keep the structured in-memory rewrite because it preserves path
        # correctness, avoids an extra dependency, and is still the faster
        # default unless real-world memory pressure makes the streaming tradeoff
        # worthwhile.
        rewritten_payload = _rewrite_compile_commands_payload(
            payload,
            original_root,
            worktree_root,
        )

        with open(
            worktree_compile_commands_path,
            "w",
            encoding="utf-8",
        ) as target_file:
            json.dump(rewritten_payload, target_file, separators=(",", ":"))
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        shutil.copy(compile_commands_path, worktree_compile_commands_path)


def _cleanup_temp_worktree(temp_worktree_dir, original_cwd):
    """Teardown worktree and release active file handles."""
    os.chdir(original_cwd)
    print("\n[SmartDiffContextBuilder] Cleaning up temporary worktree...")
    try:
        cleanup_zombie_lsps()
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    try:
        run_git_process(
            ["git", "worktree", "remove", "--force", temp_worktree_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    try:
        run_git_process(
            ["git", "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    try:
        shutil.rmtree(temp_worktree_dir, ignore_errors=True)
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _run_commit_range_worktree(args, commit_range):
    """Create a temporary git worktree to run context scan on a commit range."""
    try:
        start_sha, end_sha = parse_and_resolve_range(commit_range)
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"\n[SmartDiffContextBuilder Error] Invalid commit range: {e}")
        sys.exit(1)

    original_cwd = os.getcwd()

    try:
        current_head = run_git_command(["git", "rev-parse", "HEAD"]).strip() or None
    except Exception:  # pylint: disable=broad-exception-caught
        current_head = None

    if current_head and end_sha == current_head:
        print(
            f"\n[SmartDiffContextBuilder] Current HEAD matches final commit {end_sha[:8]}. "
            "Bypassing temporary worktree..."
        )
        run_scan(args, start_ref=start_sha, end_ref=end_sha)
        return

    temp_worktree_dir = os.path.join(
        tempfile.gettempdir(),
        f"smdc_worktree_{uuid.uuid4()}"
    )

    print(
        f"\n[SmartDiffContextBuilder] Setting up temporary worktree for commit "
        f"{end_sha[:8]}..."
    )
    try:
        _setup_temp_worktree(temp_worktree_dir, end_sha, original_cwd)
        os.chdir(temp_worktree_dir)
        if not args.no_language_server:
            print(
                "\n[SmartDiffContextBuilder] Note: Starting a language server in a clean "
                "worktree may take several minutes while the project is indexed. "
                "Use --no-language-server to skip LSP and avoid this delay."
            )
        worktree_args = argparse.Namespace(**vars(args))
        worktree_args.lsp_init_timeout = max(
            getattr(args, "lsp_init_timeout", None) or DEFAULT_LSP_INIT_TIMEOUT,
            WORKTREE_LSP_INIT_TIMEOUT,
        )
        worktree_args.lsp_timeout = max(
            getattr(args, "lsp_timeout", None) or DEFAULT_LSP_QUERY_TIMEOUT,
            WORKTREE_LSP_QUERY_TIMEOUT,
        )
        # Keep the language server rooted in this checkout. In particular, do
        # not point clangd at the original repository with
        # `--compile-commands-dir` or share its writable `.cache/clangd` tree.
        # The option selects a compilation database; it does not remap an
        # existing index to the detached revision. Absolute paths and stale
        # symbol/reference shards could therefore make worktree results describe
        # the original checkout instead of end_sha. A future cache optimization
        # should use a stable, repository-specific analysis worktree so clangd
        # can validate and incrementally replace its own per-file index shards.
        run_scan(
            worktree_args,
            start_ref=start_sha,
            end_ref=end_sha,
            output_dir=original_cwd,
            repo_root=original_cwd,
        )
    finally:
        _cleanup_temp_worktree(temp_worktree_dir, original_cwd)


def main():
    """Main entry point for CLI invocation of SmartDiffContextBuilder."""
    parser = argparse.ArgumentParser(
        description="Compile context-aware git diff tokens optimized for LLMs."
    )
    parser.add_argument("--format", choices=["md", "json"], default=None)
    parser.add_argument("--max-lines", type=int, default=None)
    parser.add_argument("--max-mb", type=float, default=None)
    parser.add_argument("--base-name", type=str, default=None)
    parser.add_argument("--max-cache-size-mb", type=float, default=None)

    parser.add_argument("--max-interface-depth", type=int, default=None)
    parser.add_argument("--disable-pruning", action="store_true", default=None)
    parser.add_argument("--lsp-init-timeout", type=float, default=None)
    parser.add_argument("--lsp-timeout", type=float, default=None)
    parser.add_argument("--ripgrep-timeout", type=float, default=None)
    parser.add_argument("--git-timeout", type=float, default=None)
    parser.add_argument("--git-probe-timeout", type=float, default=None)
    parser.add_argument("--no-language-server", action="store_true", default=None)
    parser.add_argument("--compare", action="store_true", default=None)
    parser.add_argument("--skip-ffi", action="store_true", default=None)
    parser.add_argument("--skip-macro-expansion", action="store_true", default=None)
    parser.add_argument("--caller-depth", type=int, default=None)
    parser.add_argument("--callee-depth", type=int, default=None)
    parser.add_argument(
        "--commit-range",
        type=str,
        default=None,
        help="Sequence of commits to analyze (e.g. START..END, -3, START+2, END-3)",
    )

    parser.add_argument("--config", type=str, default=None, help="Path to config file")
    parser.add_argument(
        "--create-config",
        type=str,
        default=None,
        help="Path to write a commented config file representing current CLI settings",
    )

    parser.add_argument(
        "--lang-map",
        type=str,
        default=None,
        help="JSON string of file extension mappings",
    )
    parser.add_argument(
        "--bindings",
        type=str,
        default=None,
        help="JSON string of tree-sitter bindings",
    )
    parser.add_argument(
        "--dependency-query-strings",
        type=str,
        default=None,
        help="JSON string of dependency query strings",
    )
    parser.add_argument(
        "--callee-query-strings",
        type=str,
        default=None,
        help="JSON string of callee query strings",
    )
    parser.add_argument("--func-decl-pattern", type=str, default=None)
    parser.add_argument("--def-pattern-template", type=str, default=None)
    parser.add_argument("--cpp-def-pattern-template", type=str, default=None)
    parser.add_argument("--callee-pattern", type=str, default=None)
    parser.add_argument(
        "--callee-ignored-keywords",
        type=str,
        default=None,
        help="JSON list of callee ignored keywords",
    )
    parser.add_argument("--ffi-patterns", type=str, default=None, help="JSON list of FFI patterns")
    parser.add_argument(
        "--ffi-rg-pattern",
        type=str,
        default=None,
        help="Ripgrep prefilter that must match every file eligible for --ffi-patterns",
    )
    parser.add_argument(
        "--build-directories",
        type=str,
        default=None,
        help="JSON list of build directories to scan",
    )

    args = parser.parse_args()

    # 1. Load config file if specified
    _parse_config_file(getattr(args, "config", None))

    # 2. Merge CLI overrides
    active_overrides = _merge_cli_overrides(args)

    # 3. Create config file if requested
    _create_config_if_requested(getattr(args, "create_config", None), active_overrides)

    # Populate the Namespace object with merged CONFIG values
    for k, v in CONFIG.items():
        setattr(args, k, v)

    commit_range = getattr(args, "commit_range", None)
    if commit_range:
        _run_commit_range_worktree(args, commit_range)
    else:
        run_scan(args)
