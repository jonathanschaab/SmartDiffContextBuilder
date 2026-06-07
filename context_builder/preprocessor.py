import os
import re
import json
from .sys_utils import warn_once, run_command, ripgrep_filter, HAS_RG, get_comment_prefix
from .cache import get_global_cache

def trace_macro_expansion(func_name, repo_files, file_cache=None):
    """Pass 2: Pre-expands C/C++ files and source-maps the linkages back to macros."""
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    print(f" [Pre-Expansion] Searching expanded ASTs for {func_name}...")
    fast_files = ripgrep_filter(repo_files, func_name) if HAS_RG else repo_files
    
    for f in fast_files:
        ext = os.path.splitext(f)[1]
        if ext not in ['.c', '.cpp', '.hpp', '.h']: continue
        
        # Pass 1: Expand
        expanded_code = run_command(["clang", "-E", f], timeout=5)
        if not expanded_code: continue
        
        # Pass 2: Map
        expanded_lines = expanded_code.splitlines()
        # Use a word-boundary regex so that a short func_name (e.g. "init") does
        # not match inside longer identifiers (e.g. "reinitialize"), which would
        # create spurious Macro Expansion Link entries.
        func_pattern = re.compile(r'\b' + re.escape(func_name) + r'\b')
        for idx, line in enumerate(expanded_lines):
            if func_pattern.search(line):
                # Walk backward to find linemarker
                for marker_idx in range(idx, -1, -1):
                    if expanded_lines[marker_idx].startswith("#"):
                        marker_match = re.match(r'#\s+(\d+)\s+"([^"]+)"', expanded_lines[marker_idx])
                        if marker_match:
                            orig_line = int(marker_match.group(1))
                            # Wrap in try/except ValueError to handle Windows drive mismatches
                            # (e.g. system headers on C: when the project is on D:).
                            try:
                                orig_file = os.path.relpath(marker_match.group(2), os.getcwd())
                            except ValueError:
                                orig_file = marker_match.group(2)
                            if os.path.exists(orig_file):
                                lines = file_cache.get_lines(orig_file)
                                if 1 <= orig_line <= len(lines):
                                    orig_code = lines[orig_line - 1].strip()
                                    if orig_file not in callers: callers[orig_file] = []
                                    if not any(c['line'] == orig_line for c in callers[orig_file]):
                                        callers[orig_file].append({"line": orig_line, "code": f"// [Macro Expansion Link] {orig_code}"})
                        break
    return callers

def build_ffi_registry(repo_files, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    ffi_symbols = set()
    print(" [FFI] Running pre-computation pass for cross-language boundaries...")
    fast_files = ripgrep_filter(repo_files, "no_mangle|wasm_bindgen|extern \"C\"|EMSCRIPTEN_KEEPALIVE|PYBIND11_MODULE|m.def") if HAS_RG else repo_files
    
    for f in fast_files:
        content = file_cache.get_content(f)
        for m in re.finditer(r'#\[(?:no_mangle|wasm_bindgen)\].*?(?:fn|static)\s+([A-Za-z0-9_]+)', content, re.DOTALL):
            ffi_symbols.add(m.group(1))
        # Support arbitrary return types, namespaces, pointers, references, etc.
        for m in re.finditer(r'(?:extern\s+"C"|EMSCRIPTEN_KEEPALIVE).*?\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', content, re.DOTALL):
            ffi_symbols.add(m.group(1))
        for m in re.finditer(r'm\.def\(\s*"([^"]+)"', content):
            ffi_symbols.add(m.group(1))
            
    if ffi_symbols: print(f" [FFI] Registered {len(ffi_symbols)} exported symbols.")
    return ffi_symbols

def trace_ffi_callers(func_name, repo_files, source_ext, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    print(f" [FFI] Cross-language tracing triggered for exported symbol: {func_name}()")
    fast_files = ripgrep_filter(repo_files, func_name) if HAS_RG else repo_files
    
    for f in fast_files:
        ext = os.path.splitext(f)[1]
        if ext == source_ext: continue
        lines = file_cache.get_lines(f)
        for idx, line in enumerate(lines):
            # Match using word boundaries to prevent substring false-positives
            if re.search(rf'\b{re.escape(func_name)}\b', line):
                if f not in callers: callers[f] = []
                comment_prefix = get_comment_prefix(f)
                callers[f].append({"line": idx + 1, "code": f"{comment_prefix} [FFI Bridge] {line.strip()}"})
    return callers

_COMPILE_COMMANDS_CACHE = None
_COMPILE_COMMANDS_MTIME = None

def analyze_compile_commands(target_file, file_cache=None, repo_root=None):
    """Identify translation units in compile_commands.json that are linked to target_file.

    Args:
        target_file: The header/source file to find linkages for.
        file_cache:  Optional shared LRU cache.
        repo_root:   The root directory of the original repository.  This must
                     be supplied when running inside a temporary git worktree
                     (e.g. with --commit-range), because compile_commands.json
                     entries will reference absolute paths under the *original*
                     repo, not the worktree.  When repo_root is provided it is
                     used as the base for os.path.relpath so that the returned
                     keys are valid relative paths within the project tree and
                     will pass is_in_repo() checks.
    """
    global _COMPILE_COMMANDS_CACHE, _COMPILE_COMMANDS_MTIME
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    if not os.path.exists("compile_commands.json"): return callers
    try:
        # Cache the parsed database to avoid repeatedly reading/parsing it in a loop for every file.
        mtime = os.path.getmtime("compile_commands.json")
        if _COMPILE_COMMANDS_CACHE is None or _COMPILE_COMMANDS_MTIME != mtime:
            with open("compile_commands.json", "r") as f:
                _COMPILE_COMMANDS_CACHE = json.load(f)
            _COMPILE_COMMANDS_MTIME = mtime
        db = _COMPILE_COMMANDS_CACHE
        target_base = os.path.basename(target_file)
        target_name = os.path.splitext(target_base)[0]
        abs_target_file = os.path.abspath(target_file)
        
        for entry in db:
            ref_file = entry.get("file")
            if not ref_file:
                continue
            
            comp_dir = entry.get("directory")
            # Resolve relative paths in compile_commands relative to the directory specified
            if comp_dir and not os.path.isabs(ref_file):
                ref_file = os.path.join(comp_dir, ref_file)
            
            abs_ref_file = os.path.abspath(ref_file)
            
            # If repo_root is provided, we are running inside a temporary worktree.
            # compile_commands.json contains absolute paths pointing to the original repo.
            # To analyze the correct historical state of the files, we must map those
            # paths to the corresponding location in the temporary worktree (the current CWD).
            if repo_root:
                norm_ref = abs_ref_file.replace("\\", "/").lower()
                norm_root = os.path.abspath(repo_root).replace("\\", "/").lower()
                if norm_ref.startswith(norm_root):
                    # Extract relative path from original repo root
                    rel_to_root = abs_ref_file[len(os.path.abspath(repo_root)):].lstrip("\\/")
                    # Map to the current temporary worktree CWD
                    abs_ref_file = os.path.abspath(os.path.join(os.getcwd(), rel_to_root))
            
            if abs_ref_file == abs_target_file:
                continue
            
            ref_base = os.path.basename(abs_ref_file)
            ref_name = os.path.splitext(ref_base)[0]
            
            is_linked = False
            # Check if base names match (e.g. foo.h and foo.cpp)
            if ref_name == target_name:
                is_linked = True
            elif os.path.exists(abs_ref_file):
                # Check if the translation unit includes target_base
                content = file_cache.get_content(abs_ref_file)
                pattern = rf'#\s*include\s*["<]{re.escape(target_base)}[">]'
                if re.search(pattern, content):
                    is_linked = True
                    
            if is_linked:
                # Compute a path relative to the active worktree root (CWD)
                # Wrap in try/except ValueError to catch drive mismatches on Windows.
                try:
                    rel_ref_file = os.path.relpath(abs_ref_file, os.getcwd()).replace("\\", "/")
                except ValueError:
                    # Fallback to absolute path using forward slashes if drives differ
                    rel_ref_file = abs_ref_file.replace("\\", "/")
                if rel_ref_file not in callers: callers[rel_ref_file] = []
                callers[rel_ref_file].append({"line": 0, "code": f"// [Compilation Link via compile_commands.json]"})
    except Exception as exc:
        warn_once("compile_commands_parse_fail", f"Failed to parse compile_commands.json: {exc}")
    return callers

