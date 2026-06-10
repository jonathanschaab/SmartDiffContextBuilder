"""Module preprocessor handles macro expansions, compile commands, and cross-language FFI."""

import json
import os
import re

from .cache import get_global_cache
from .config import CONFIG
from .sys_utils import HAS_RG, get_comment_prefix, ripgrep_filter, run_command, warn_once


def _add_macro_caller(orig_file, orig_line, callers, file_cache):
    """Add a macro expansion caller to the results dictionary."""
    lines = file_cache.get_lines(orig_file)
    if not 1 <= orig_line <= len(lines):
        return
    orig_code = lines[orig_line - 1].strip()
    if orig_file not in callers:
        callers[orig_file] = []
    if not any(c["line"] == orig_line for c in callers[orig_file]):
        callers[orig_file].append({
            "line": orig_line,
            "code": f"// [Macro Expansion Link] {orig_code}",
        })


def _map_expanded_line_to_source(expanded_lines, idx, callers, file_cache):
    """Walk backward to find the linemarker and map to the source file."""
    for marker_idx in range(idx, -1, -1):
        marker_line = expanded_lines[marker_idx]
        if not marker_line.startswith("#"):
            continue
        marker_match = re.match(r'#\s+(\d+)\s+"([^"]+)"', marker_line)
        if not marker_match:
            continue
        orig_line = int(marker_match.group(1))
        try:
            orig_file = os.path.relpath(marker_match.group(2), os.getcwd())
        except ValueError:
            orig_file = marker_match.group(2)
        if os.path.exists(orig_file):
            _add_macro_caller(orig_file, orig_line, callers, file_cache)
        break


def _process_single_macro_file(f, func_pattern, callers, file_cache):
    """Process a single file for macro expansion mapping."""
    ext = os.path.splitext(f)[1].lower()
    if ext not in [".c", ".cpp", ".hpp", ".h"]:
        return

    # Pass 1: Expand
    expanded_code = run_command(["clang", "-E", f], timeout=5)
    if not expanded_code:
        return

    # Pass 2: Map
    expanded_lines = expanded_code.splitlines()
    for idx, line in enumerate(expanded_lines):
        if func_pattern.search(line):
            _map_expanded_line_to_source(expanded_lines, idx, callers, file_cache)


def trace_macro_expansion(func_name, repo_files, file_cache=None):
    """Pass 2: Pre-expands C/C++ files and source-maps the linkages back to macros.

    Args:
        func_name (str): The name of the macro/function.
        repo_files (list): List of repository files.
        file_cache (LRUFileCache, optional): File cache singleton.

    Returns:
        dict: Callers mapping files to matched lines.
    """
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    print(f" [Pre-Expansion] Searching expanded ASTs for {func_name}...")
    fast_files = ripgrep_filter(
        repo_files, func_name,
        fallback_hint=f"macro callers of '{func_name}'"
    ) if HAS_RG else repo_files

    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore). This avoids boundary mismatch for C++
    # destructors (e.g. ~MyClass) or C++ operator overloads (e.g. operator+).
    lead_b = r"\b" if func_name and (func_name[0].isalnum() or func_name[0] == "_") else ""
    trail_b = r"\b" if func_name and (func_name[-1].isalnum() or func_name[-1] == "_") else ""
    func_pattern = re.compile(lead_b + re.escape(func_name) + trail_b)

    total = len(fast_files)
    for idx, f in enumerate(fast_files):
        if total > 200 and idx % 100 == 0:
            print(f"  [Scanning {idx + 1}/{total} files for '{func_name}'...]")
        _process_single_macro_file(f, func_pattern, callers, file_cache)
    return callers


def build_ffi_registry(repo_files, file_cache=None):
    """Build the FFI registry by parsing exported symbols matching FFI patterns.

    Args:
        repo_files (list): List of repository files.
        file_cache (LRUFileCache, optional): File cache singleton.

    Returns:
        set: Registry of FFI symbols.
    """
    if file_cache is None:
        file_cache = get_global_cache()
    ffi_symbols = set()
    print(" [FFI] Running pre-computation pass for cross-language boundaries...")

    ffi_rg_pattern = CONFIG.get("ffi_rg_pattern")
    if HAS_RG and ffi_rg_pattern:
        fast_files = ripgrep_filter(repo_files, ffi_rg_pattern, fixed_strings=False)
    else:
        fast_files = repo_files

    compiled_patterns = []
    ffi_patterns = CONFIG.get("ffi_patterns")
    for pat in ffi_patterns or []:
        if not isinstance(pat, str):
            warn_once("ffi_pattern_non_string", f"FFI pattern must be a string, got: {pat}")
            continue
        try:
            compiled_patterns.append(re.compile(pat, re.DOTALL))
        except (re.error, TypeError) as e:
            warn_once("ffi_regex_compile_fail", f"Failed to compile FFI regex pattern '{pat}': {e}")

    for f in fast_files:
        content = file_cache.get_content(f)
        for pattern in compiled_patterns:
            for m in re.finditer(pattern, content):
                # Ensure the pattern actually captured at least one group and is not None
                if m.groups() and m.group(1) is not None:
                    ffi_symbols.add(m.group(1))

    if ffi_symbols:
        print(f" [FFI] Registered {len(ffi_symbols)} exported symbols.")
    return ffi_symbols


def trace_ffi_callers(func_name, repo_files, source_ext, file_cache=None):
    """Find callers of FFI methods in non-source language files.

    Args:
        func_name (str): The FFI method name.
        repo_files (list): List of repository files.
        source_ext (str): Extension of the file where the symbol was defined.
        file_cache (LRUFileCache, optional): File cache singleton.

    Returns:
        dict: Callers mapping files to matched FFI lines.
    """
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    print(f" [FFI] Cross-language tracing triggered for exported symbol: {func_name}()")
    fast_files = ripgrep_filter(
        repo_files, func_name,
        fallback_hint=f"FFI callers of '{func_name}'"
    ) if HAS_RG else repo_files

    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore).
    lead_b = r"\b" if func_name and (func_name[0].isalnum() or func_name[0] == "_") else ""
    trail_b = r"\b" if func_name and (func_name[-1].isalnum() or func_name[-1] == "_") else ""
    func_pattern = re.compile(lead_b + re.escape(func_name) + trail_b)

    for f in fast_files:
        ext = os.path.splitext(f)[1].lower()
        if ext == source_ext.lower():
            continue
        lines = file_cache.get_lines(f)
        for idx, line in enumerate(lines):
            # Match using word boundaries to prevent substring false-positives
            if func_pattern.search(line):
                if f not in callers:
                    callers[f] = []
                comment_prefix = get_comment_prefix(f)
                callers[f].append({
                    "line": idx + 1,
                    "code": f"{comment_prefix} [FFI Bridge] {line.strip()}",
                })
    return callers


# State container to avoid 'global' keyword warning in analyze_compile_commands
_COMPILE_COMMANDS_STATE = {"cache": None, "mtime": None, "path": None}


def _process_compilation_entry(
    entry,
    include_pattern,
    abs_target_file,
    target_base,
    repo_root,
    norm_root,
    cwd,
    callers,
    file_cache,
):
    """Process a single compilation command entry from compile_commands.json."""
    ref_file = entry.get("file")
    if not ref_file:
        return

    comp_dir = entry.get("directory")
    # Resolve relative paths in compile_commands relative to the directory specified
    if comp_dir and not os.path.isabs(ref_file):
        ref_file = os.path.join(comp_dir, ref_file)

    abs_ref_file = os.path.abspath(ref_file)

    # If repo_root is provided, we are running inside a temporary worktree.
    if repo_root and norm_root:
        norm_ref = abs_ref_file.replace("\\", "/").lower()
        if norm_ref.startswith(norm_root):
            try:
                rel_to_root = os.path.relpath(abs_ref_file, repo_root)
                # Map to the current temporary worktree CWD
                abs_ref_file = os.path.abspath(os.path.join(cwd, rel_to_root))
            except ValueError:
                pass

    if abs_ref_file == abs_target_file:
        return

    content = file_cache.get_content(abs_ref_file)
    is_linked = bool(
        content
        and ("\\" in content or target_base in content)
        and include_pattern.search(content)
    )

    if is_linked:
        # Compute a path relative to the active worktree root (CWD)
        try:
            rel_ref_file = os.path.relpath(abs_ref_file, cwd).replace("\\", "/")
        except ValueError:
            rel_ref_file = abs_ref_file.replace("\\", "/")
        if rel_ref_file not in callers:
            callers[rel_ref_file] = []
        callers[rel_ref_file].append({
            "line": 0,
            "code": "// [Compilation Link via compile_commands.json]",
        })


def analyze_compile_commands(target_file, file_cache=None, repo_root=None):
    """Identify translation units in compile_commands.json that are linked to target_file.

    Args:
        target_file: The header/source file to find linkages for.
        file_cache:  Optional shared LRU cache.
        repo_root:   The root directory of the original repository.
    """
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    if not target_file:
        return callers
    target_base = os.path.basename(target_file)
    if not target_base:
        return callers
    if not os.path.exists("compile_commands.json"):
        return callers
    try:
        # Cache the parsed database to avoid repeatedly reading/parsing it in a loop.
        abs_db_path = os.path.abspath("compile_commands.json")
        mtime = os.path.getmtime("compile_commands.json")
        if (
            _COMPILE_COMMANDS_STATE["cache"] is None
            or _COMPILE_COMMANDS_STATE["path"] != abs_db_path
            or _COMPILE_COMMANDS_STATE["mtime"] != mtime
        ):
            with open("compile_commands.json", "r", encoding="utf-8") as f:
                _COMPILE_COMMANDS_STATE["cache"] = json.load(f)
            _COMPILE_COMMANDS_STATE["mtime"] = mtime
            _COMPILE_COMMANDS_STATE["path"] = abs_db_path
        db = _COMPILE_COMMANDS_STATE["cache"] or []
        # Build target pattern to allow line continuations between any characters
        # of the target base name. E.g. 'helper.h' -> 'h(?:\\\r?\n)?e...'
        # Space character is explicitly escaped because re.VERBOSE ignores unescaped space.
        target_chars = []
        for c in target_base:
            escaped = re.escape(c)
            if c.isspace() and escaped == c:
                escaped = "\\" + c
            target_chars.append(escaped)
        target_pattern = r'(?:\\\r?\n)?'.join(target_chars)

        # Construct a regex that matches include directives, restricting raw
        # newlines but allowing line continuations. We use re.VERBOSE (re.X)
        # to allow clean formatting and comments. Changing [^">] to [^"\n>\\]
        # ensures we do not match across lines unless there is a proper
        # backslash line-continuation ('\').
        pattern = rf"""
            ^                                      # Start of a line
            [ \t]* (?: \\\r?\n [ \t]* )*           # Spaces and line continuations before '#'
            \#                                     # Preprocessor hash symbol
            [ \t]* (?: \\\r?\n [ \t]* )*           # Spaces and line continuations before 'include'
            include                                # 'include' keyword
            [ \t]* (?: \\\r?\n [ \t]* )*           # Spaces and line continuations before opening delim
            (?:
                "                                  # Double quoted include
                (?: [^"\n>\\] | \\\r?\n | \\. )*   # Preceding path chars (no raw newline)
                [/\\]                              # Directory separator
                (?: \\\r?\n )*                     # Only line continuations allowed here
                {target_pattern}                   # Target base name
                (?: \\\r?\n )*                     # Line continuations before closing quote
                "                                  # Closing double quote
            |
                <                                  # Angle bracketed include
                (?: [^"\n>\\] | \\\r?\n | \\. )*   # Preceding path chars
                [/\\]                              # Directory separator
                (?: \\\r?\n )*                     # Only line continuations allowed here
                {target_pattern}                   # Target base name
                (?: \\\r?\n )*                     # Line continuations before closing bracket
                >                                  # Closing angle bracket
            |
                "                                  # Directly quoted include
                {target_pattern}
                (?: \\\r?\n )*
                "
            |
                <                                  # Directly bracketed include
                {target_pattern}
                (?: \\\r?\n )*
                >
            )
        """
        include_pattern = re.compile(pattern, re.M | re.X)
        abs_target_file = os.path.abspath(target_file)
        norm_root = None
        if repo_root:
            norm_root = os.path.abspath(repo_root).replace("\\", "/").lower()
            if not norm_root.endswith("/"):
                norm_root += "/"
        cwd = os.getcwd()

        for entry in db:
            _process_compilation_entry(
                entry,
                include_pattern,
                abs_target_file,
                target_base,
                repo_root,
                norm_root,
                cwd,
                callers,
                file_cache,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        warn_once("compile_commands_parse_fail", f"Failed to parse compile_commands.json: {exc}")
    return callers
