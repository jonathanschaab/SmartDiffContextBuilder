"""Module preprocessor handles macro expansions, compile commands, and cross-language FFI."""

import json
import os
import re
import subprocess
from collections import OrderedDict

from .cache import get_global_cache
from .config import CONFIG
from .languages import get_language_profile
from .path_utils import (
    detect_root_case_sensitivity,
    normalize_for_path_match,
    path_is_within_root,
    to_forward_slashes,
)
from .sys_utils import (
    get_comment_prefix,
    iter_scan_progress,
    ripgrep_filter,
    warn_once,
)

# This cache is cleared at the start of each repository scan, where source
# snapshots are assumed stable. The byte limit bounds successful expansions;
# the entry limit also bounds empty negative results, which consume no byte budget.
_PREPROCESSED_CACHE_MAX_BYTES = 64 * 1024 * 1024
_PREPROCESSED_CACHE_MAX_ENTRIES = 4096
_PREPROCESSED_CACHE = OrderedDict()
_PREPROCESSED_CACHE_STATE = {"size_bytes": 0}
_PREPROCESS_TIMEOUT_COUNTS = {}


def clear_preprocessed_cache():
    """Clear cached preprocessor output before starting a new repository scan."""
    _PREPROCESSED_CACHE.clear()
    _PREPROCESSED_CACHE_STATE["size_bytes"] = 0
    _PREPROCESS_TIMEOUT_COUNTS.clear()
    _run_clang_preprocessor._clang_missing = False  # pylint: disable=protected-access


def _cache_preprocessed_code(cache_key, signature, expanded_code):
    """Store successful preprocessor output in a bounded LRU cache."""
    previous = _PREPROCESSED_CACHE.pop(cache_key, None)
    if previous:
        _PREPROCESSED_CACHE_STATE["size_bytes"] -= len(previous["code"])

    _PREPROCESSED_CACHE[cache_key] = {
        "signature": signature,
        "code": expanded_code,
    }
    _PREPROCESSED_CACHE_STATE["size_bytes"] += len(expanded_code)

    while (
        _PREPROCESSED_CACHE
        and (
            _PREPROCESSED_CACHE_STATE["size_bytes"] > _PREPROCESSED_CACHE_MAX_BYTES
            or len(_PREPROCESSED_CACHE) > _PREPROCESSED_CACHE_MAX_ENTRIES
        )
    ):
        _, evicted = _PREPROCESSED_CACHE.popitem(last=False)
        _PREPROCESSED_CACHE_STATE["size_bytes"] -= len(evicted["code"])


def _run_clang_preprocessor(file_path):
    """Run clang preprocessing and distinguish retryable timeouts from results."""
    if getattr(_run_clang_preprocessor, "_clang_missing", False):
        return "", "failed"

    cmd = ["clang", "-E", file_path]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Preprocessor output may contain source bytes outside the locale
            # encoding. Preserve the scan by replacing only undecodable bytes.
            errors="replace",
            check=True,
            timeout=5,
        )
        return result.stdout, "success"
    except subprocess.TimeoutExpired:
        warn_once("timeout_clang", f"Command '{' '.join(cmd)}' timed out.")
        return "", "timeout"
    except FileNotFoundError:
        # Macro expansion requires clang, but the graph tracer has already run
        # its normal tree-sitter/regex C/C++ analysis. Cache this scan-wide
        # capability failure so every remaining source file does not spawn the
        # same failing process; only generated macro linkages are unavailable.
        _run_clang_preprocessor._clang_missing = True  # pylint: disable=protected-access
        warn_once(
            "clang_missing",
            "clang is unavailable; continuing C/C++ analysis without macro expansion.",
        )
        return "", "failed"
    except (subprocess.CalledProcessError, OSError):
        return "", "failed"


def _get_preprocessed_code(file_path):
    """Return cached clang preprocessor output for the current source snapshot."""
    try:
        stat = os.stat(file_path)
    except OSError:
        # Repository candidates can become stale or inaccessible during a scan.
        # If Python cannot stat the file, spawning clang for the same path only
        # adds subprocess overhead and cannot produce a cacheable snapshot.
        return ""

    cache_key = os.path.normcase(os.path.abspath(file_path))
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _PREPROCESSED_CACHE.get(cache_key)
    if cached and cached["signature"] == signature:
        _PREPROCESSED_CACHE.move_to_end(cache_key)
        return cached["code"]

    expanded_code, status = _run_clang_preprocessor(file_path)
    timeout_key = (cache_key, signature)
    if status == "timeout":
        timeout_count = _PREPROCESS_TIMEOUT_COUNTS.get(timeout_key, 0) + 1
        _PREPROCESS_TIMEOUT_COUNTS[timeout_key] = timeout_count
        if timeout_count < 2:
            # Unlike a deterministic clang failure, a timeout may be transient.
            # Permit one recovery attempt, then favor bounded scan time over
            # repeatedly spending the full timeout on this unchanged snapshot.
            return ""

    # Successful empty output and deterministic failures are stable negative
    # results for this scan. A second timeout is also cached after its retry
    # allowance is exhausted, preventing repeated expensive subprocesses.
    _PREPROCESS_TIMEOUT_COUNTS.pop(timeout_key, None)
    _cache_preprocessed_code(cache_key, signature, expanded_code)
    return expanded_code


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


def _process_single_macro_file(file_path, func_pattern, callers, file_cache):
    """Process a single file for macro expansion mapping."""
    if not get_language_profile(file_path).supports_macro_expansion:
        return

    # Pass 1: Expand
    expanded_code = _get_preprocessed_code(file_path)
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
    macro_files = [
        file_path
        for file_path in repo_files
        if get_language_profile(file_path).supports_macro_expansion
    ]
    fast_files = ripgrep_filter(
        repo_files, func_name,
        fallback_hint=f"macro callers of '{func_name}'"
    )

    if not fast_files and not getattr(fast_files, "used_ripgrep_fallback", False):
        # A literal miss normally means preprocessing cannot produce func_name.
        # Token-pasting macros are the important exception: `prefix ## suffix`
        # can synthesize an identifier that never appears verbatim in source.
        # Keep the expensive scan whenever `##` exists or its safety check fails;
        # only skip when both ripgrep searches completed successfully with no match.
        token_paste_files = ripgrep_filter(
            macro_files,
            "##",
            fallback_hint=f"token-pasting macros relevant to '{func_name}'",
        )
        if (
            not token_paste_files
            and not getattr(token_paste_files, "used_ripgrep_fallback", False)
        ):
            return callers

    fast_file_set = set(fast_files)
    scan_files = [
        file_path
        for file_path in fast_files
        if get_language_profile(file_path).supports_macro_expansion
    ]
    scan_files.extend(
        file_path for file_path in macro_files if file_path not in fast_file_set
    )
    exhaustive_scan = (
        getattr(fast_files, "used_ripgrep_fallback", False)
        or len(scan_files) > len(fast_file_set.intersection(macro_files))
    )

    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore). This avoids boundary mismatch for C++
    # destructors (e.g. ~MyClass) or C++ operator overloads (e.g. operator+).
    lead_b = r"\b" if func_name and (func_name[0].isalnum() or func_name[0] == "_") else ""
    trail_b = r"\b" if func_name and (func_name[-1].isalnum() or func_name[-1] == "_") else ""
    func_pattern = re.compile(lead_b + re.escape(func_name) + trail_b)

    for file_path in iter_scan_progress(
        scan_files,
        label=f"Scanning macro callers of '{func_name}'",
        min_files=50,
        force=exhaustive_scan,
    ):
        _process_single_macro_file(file_path, func_pattern, callers, file_cache)
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
    if ffi_rg_pattern:
        fast_files = ripgrep_filter(
            repo_files,
            ffi_rg_pattern,
            fixed_strings=False,
            fallback_hint="FFI export pre-computation",
        )
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
        except (re.error, TypeError) as exc:
            warn_once(
                "ffi_regex_compile_fail",
                f"Failed to compile FFI regex pattern '{pat}': {exc}",
            )

    for file_path in iter_scan_progress(
        fast_files,
        label="Scanning FFI export pre-computation",
        min_files=100,
        force=fast_files is repo_files,
    ):
        content = file_cache.get_content(file_path)
        for pattern in compiled_patterns:
            for match in re.finditer(pattern, content):
                # Ensure the pattern actually captured at least one group and is not None
                if match.groups() and match.group(1) is not None:
                    ffi_symbols.add(match.group(1))

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
    )

    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore).
    lead_b = r"\b" if func_name and (func_name[0].isalnum() or func_name[0] == "_") else ""
    trail_b = r"\b" if func_name and (func_name[-1].isalnum() or func_name[-1] == "_") else ""
    func_pattern = re.compile(lead_b + re.escape(func_name) + trail_b)

    for file_path in iter_scan_progress(
        fast_files,
        label=f"Scanning FFI callers of '{func_name}'",
        min_files=100,
    ):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == source_ext.lower():
            continue
        lines = file_cache.get_lines(file_path)
        for idx, line in enumerate(lines):
            # Match using word boundaries to prevent substring false-positives
            if func_pattern.search(line):
                if file_path not in callers:
                    callers[file_path] = []
                comment_prefix = get_comment_prefix(file_path)
                callers[file_path].append({
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
    root_case_sensitive,
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
    if repo_root and root_case_sensitive is not None:
        if path_is_within_root(
            abs_ref_file,
            repo_root,
            case_sensitive=root_case_sensitive,
        ):
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
            rel_ref_file = to_forward_slashes(os.path.relpath(abs_ref_file, cwd))
        except ValueError:
            rel_ref_file = to_forward_slashes(abs_ref_file)
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
        root_case_sensitive = None
        if repo_root:
            repo_root = os.path.abspath(repo_root)
            root_case_sensitive = detect_root_case_sensitivity(repo_root)
        cwd = os.getcwd()

        for entry in db:
            _process_compilation_entry(
                entry,
                include_pattern,
                abs_target_file,
                target_base,
                repo_root,
                root_case_sensitive,
                cwd,
                callers,
                file_cache,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        warn_once("compile_commands_parse_fail", f"Failed to parse compile_commands.json: {exc}")
    return callers
