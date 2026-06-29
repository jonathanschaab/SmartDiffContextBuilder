# pylint: disable=too-many-lines
"""
AST analysis engine utilizing tree-sitter or regex fallback.
Provides syntax-aware function boundary extraction, dependency tracing,
and callee analysis.
"""


import os
import re
import difflib
import importlib
import bisect
import sys
import threading
from collections import OrderedDict
from contextlib import nullcontext
from .sys_utils import iter_scan_progress, warn_once, ripgrep_filter
from .cache import get_global_cache
from .languages import UNKNOWN_LANGUAGE, get_language_profile
from .ast_pruning import split_massive_block_ast as _split_massive_block_ast
from .config import CONFIG, ConfigDictProxy

try:
    import tree_sitter
    HAS_TREESITTER = True
    TreeSitterQueryError = tree_sitter.QueryError
except ImportError:
    tree_sitter = None
    HAS_TREESITTER = False
    TreeSitterQueryError = ValueError

TREE_SITTER_BINDING_ERRORS = (
    ImportError,
    AttributeError,
    TypeError,
    RuntimeError,
    ValueError,
)

LANG_MAP = ConfigDictProxy('lang_map')

ASSIGNMENT_OPERATORS = (
    '>>>=', '<<=', '>>=', '**=', '//=', '&^=', '&&=', '||=', '??=',
    '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '@=', ':=', '=',
)
ASSIGNMENT_OPERATOR_RE = re.compile(
    '|'.join(
        r'(?<![=!<>])=(?![=>])' if op == '=' else re.escape(op)
        for op in ASSIGNMENT_OPERATORS
    )
)

MEMBER_FIELD_BY_NODE_TYPE = {
    'member_expression': 'property',
    'attribute': 'attribute',
    'selector_expression': 'field',
    'field_access': 'field',
    'field_expression': 'field',
}

AST_ENGINE_CACHE_LIMIT = 1024
AST_ENGINE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_LRU_TOTAL_BYTES_ATTR = '_total_bytes'
_LRU_ENTRY_SIZES_ATTR = '_entry_sizes'
_LRU_LOCK_ATTR = '_owner_lock'


class LRUCache(OrderedDict):
    """Subclass of OrderedDict that supports arbitrary attributes."""


def _estimate_cache_entry_size(value, seen=None):
    """Estimate cache value memory usage for bounded eviction decisions."""
    if seen is None:
        seen = set()
    obj_id = id(value)
    if obj_id in seen:
        return 0
    seen.add(obj_id)

    try:
        size = sys.getsizeof(value)
    except TypeError:
        return 0

    if isinstance(value, dict):
        for key, item in value.items():
            size += _estimate_cache_entry_size(key, seen)
            size += _estimate_cache_entry_size(item, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            size += _estimate_cache_entry_size(item, seen)
    return size


def _estimate_lru_cache_size(cache):
    """Estimate total memory held by cache keys and values."""
    with _lru_lock(cache):
        _ensure_lru_size_tracking(cache)
        return getattr(cache, _LRU_TOTAL_BYTES_ATTR)


def _ensure_lru_size_tracking(cache):
    """Initialize O(1) byte accounting metadata on an LRU cache."""
    if hasattr(cache, _LRU_ENTRY_SIZES_ATTR) and hasattr(cache, _LRU_TOTAL_BYTES_ATTR):
        return
    entry_sizes = {
        key: _lru_entry_size(key, value)
        for key, value in cache.items()
    }
    setattr(cache, _LRU_ENTRY_SIZES_ATTR, entry_sizes)
    setattr(cache, _LRU_TOTAL_BYTES_ATTR, sum(entry_sizes.values()))


def _lru_entry_size(key, value):
    """Estimate total memory held by one LRU key/value pair."""
    return _estimate_cache_entry_size(key) + _estimate_cache_entry_size(value)


def _lru_pop_oldest(cache):
    """Evict one LRU entry and update byte accounting."""
    key, _ = cache.popitem(last=False)
    entry_sizes = getattr(cache, _LRU_ENTRY_SIZES_ATTR)
    total_bytes = getattr(cache, _LRU_TOTAL_BYTES_ATTR)
    setattr(cache, _LRU_TOTAL_BYTES_ATTR, total_bytes - entry_sizes.pop(key, 0))


def _get_lru_cache(owner, attr_name):
    """Return an LRUCache attribute, upgrading plain dicts in place."""
    owner_lock = getattr(owner, '_lock', None)
    with owner_lock if owner_lock is not None else nullcontext():
        cache = getattr(owner, attr_name, None)
        if not isinstance(cache, LRUCache):
            if isinstance(cache, dict):
                cache = LRUCache(cache)
            else:
                cache = LRUCache()
        if owner_lock is not None:
            setattr(cache, _LRU_LOCK_ATTR, owner_lock)
        setattr(owner, attr_name, cache)
        _ensure_lru_size_tracking(cache)
        return cache


def _lru_lock(cache):
    """Return an optional owner lock for an LRU cache."""
    lock = getattr(cache, _LRU_LOCK_ATTR, None)
    return lock if lock is not None else nullcontext()


def _lru_get(cache, key):
    """Return a cached value and mark it recently used."""
    with _lru_lock(cache):
        if key not in cache:
            return None, False
        cache.move_to_end(key)
        return cache[key], True


def _lru_set(
    cache,
    key,
    value,
    max_size=AST_ENGINE_CACHE_LIMIT,
    max_bytes=AST_ENGINE_CACHE_MAX_BYTES,
):
    """Set a cache value, evicting the least recently used entry if needed."""
    with _lru_lock(cache):
        _ensure_lru_size_tracking(cache)
        entry_sizes = getattr(cache, _LRU_ENTRY_SIZES_ATTR)
        total_bytes = getattr(cache, _LRU_TOTAL_BYTES_ATTR)
        if key in cache:
            total_bytes -= entry_sizes.pop(key, 0)
        cache[key] = value
        entry_size = _lru_entry_size(key, value)
        entry_sizes[key] = entry_size
        total_bytes += entry_size
        setattr(cache, _LRU_TOTAL_BYTES_ATTR, total_bytes)
        cache.move_to_end(key)
        while len(cache) > max_size:
            _lru_pop_oldest(cache)
        while len(cache) > 1 and getattr(cache, _LRU_TOTAL_BYTES_ATTR) > max_bytes:
            _lru_pop_oldest(cache)



def _strip_comments_only(line, profile):
    """Strip same-line comments while leaving string literals intact."""
    if (
        not profile
        or not isinstance(profile.line_comment, str)
        or not profile.line_comment
    ):
        return line
    if not hasattr(profile, 'strip_string_literals') or not callable(
        profile.strip_string_literals
    ):
        return line
    if getattr(profile, '_cached_string_literal_pattern', None) is None:
        try:
            profile.strip_string_literals('')
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    string_pattern = getattr(profile, '_cached_string_literal_pattern', None)
    if not string_pattern or not hasattr(string_pattern, 'pattern') or not hasattr(
        string_pattern, 'flags'
    ):
        return line
    combined = getattr(profile, '_cached_comment_pattern', None)
    if combined is None:
        # Use a non-capturing group for the string pattern so that internal
        # backreferences (e.g. \1 for matching quotes) are not broken by an
        # extra capturing group.  Use a named group for the comment so we can
        # reference it robustly regardless of how many groups the string
        # pattern contains.
        comment_pattern_str = re.escape(profile.line_comment) + r'.*'
        combined = re.compile(
            f'(?:{string_pattern.pattern})|(?P<comment>{comment_pattern_str})',
            flags=string_pattern.flags,
        )
        setattr(profile, '_cached_comment_pattern', combined)

    def replace(match):
        # If the match is a comment, replace it with spaces to preserve offsets.
        comment = match.group('comment')
        if comment:
            return ' ' * len(comment)
        return match.group(0)

    return combined.sub(replace, line)


def _fallback_strip(lines, profile):
    """Fallback utility to strip comments from line lists without trailing newlines."""
    if not lines:
        return []
    lines_with_nl = [
        (l if l.endswith(('\n', '\r')) else l + '\n')
        for l in lines
    ]
    content = "".join(lines_with_nl)
    stripped = profile.strip_block_comments(content)
    return _align_stripped_to_original_lines(lines, stripped)


def _align_stripped_to_original_lines(lines, stripped):
    """Align block-comment-stripped content back to original line positions."""
    if not isinstance(stripped, str):
        return lines
    stripped_lines = stripped.splitlines(keepends=True)
    original_line_count = len(lines)
    if len(stripped_lines) < original_line_count:
        aligned_lines = [
            "\n" if line.endswith(('\n', '\r')) else ""
            for line in lines
        ]
        search_start = 0
        lookahead = max(1, CONFIG.get('fallback_strip_lookahead', 20))
        missed_alignment = False
        for stripped_line in stripped_lines:
            stripped_text = stripped_line.strip()
            if not stripped_text:
                continue
            best_idx = None
            best_score = 0.0
            search_end = min(original_line_count, search_start + lookahead)
            for idx in range(search_start, search_end):
                original_text = lines[idx].strip()
                score = _line_alignment_score(original_text, stripped_text)
                if score > best_score:
                    best_idx = idx
                    best_score = score
            if best_idx is not None and best_score >= 0.5:
                aligned_lines[best_idx] = stripped_line
                search_start = best_idx + 1
            else:
                missed_alignment = True
        if missed_alignment:
            warn_once(
                "fallback_strip_alignment_missed",
                "[SmartDiffContextBuilder Warning] Block-comment fallback "
                f"alignment could not place some stripped lines within the next "
                f"{lookahead} source lines. If valid code is missing, raise the "
                "limit with --fallback-strip-lookahead N or set "
                "'fallback_strip_lookahead' in your config file.",
            )
        stripped_lines = aligned_lines
    elif len(stripped_lines) > original_line_count:
        stripped_lines = stripped_lines[:original_line_count]
    return stripped_lines


def _estimate_aligned_lines_size(aligned_lines):
    """Estimate memory used by an aligned stripped-lines cache entry."""
    try:
        if sys.implementation.name != "cpython":
            return sum(len(line) for line in aligned_lines)
        return sys.getsizeof(aligned_lines) + sum(
            sys.getsizeof(line) for line in aligned_lines
        )
    except Exception:  # pylint: disable=broad-exception-caught
        return sum(len(line) for line in aligned_lines)


def _get_cached_aligned_stripped_lines(file_cache, file_path, profile):  # pylint: disable=too-many-return-statements
    """Return aligned stripped lines cached on LRUFileCache entries when possible."""
    get_aligned = getattr(file_cache, "get_aligned_stripped_lines", None)
    if callable(get_aligned):
        return get_aligned(file_path, profile, _align_stripped_to_original_lines)

    cache = getattr(file_cache, 'cache', None)
    load_func = getattr(file_cache, '_load', None)
    if not isinstance(cache, dict) or not callable(load_func):
        return None

    abs_path = os.path.abspath(file_path)
    lock = getattr(file_cache, '_lock', None)
    with lock if lock is not None else nullcontext():
        entry = load_func(abs_path)
        if "aligned_stripped_lines" in entry:
            return entry["aligned_stripped_lines"]
        lines = entry.get("lines")
        if not isinstance(lines, (list, tuple)):
            return []
        content = entry.get("content", "".join(lines))
        strip_block_comments = getattr(profile, "strip_block_comments", None)
        if not callable(strip_block_comments):
            return None
    stripped = (
        file_cache.get_stripped_content(abs_path, profile)
        if hasattr(file_cache, "get_stripped_content")
        else strip_block_comments(content)
    )
    aligned_lines = _align_stripped_to_original_lines(lines, stripped)
    added_bytes = _estimate_aligned_lines_size(aligned_lines)

    with lock if lock is not None else nullcontext():
        entry = load_func(abs_path)
        if "aligned_stripped_lines" in entry:
            return entry["aligned_stripped_lines"]
        entry["aligned_stripped_lines"] = aligned_lines
        entry["size_bytes"] = entry.get("size_bytes", 0) + added_bytes
        if (
            abs_path in cache
            and hasattr(file_cache, "current_size_bytes")
            and hasattr(file_cache, "evict_to_limit")
        ):
            file_cache.current_size_bytes += added_bytes
            file_cache.evict_to_limit()
        return aligned_lines


def _line_alignment_score(original_text, stripped_text):
    """Score how likely stripped_text came from original_text."""
    if original_text == stripped_text:
        return 1.0
    if original_text.startswith(stripped_text):
        return 0.95
    if stripped_text in original_text:
        return 0.9
    return difflib.SequenceMatcher(None, original_text, stripped_text).ratio()


def _get_stripped_lines(file_cache, file_path, profile):  # pylint: disable=too-many-return-statements
    """Retrieve stripped lines from cache."""
    if file_cache is None:
        file_cache = get_global_cache()

    if profile is None:
        lines = file_cache.get_lines(file_path)
        return lines if isinstance(lines, (list, tuple)) else []

    strip_block_comments = getattr(profile, "strip_block_comments", None)
    if not callable(strip_block_comments):
        lines = file_cache.get_lines(file_path)
        return lines if isinstance(lines, (list, tuple)) else []

    cached_aligned_lines = _get_cached_aligned_stripped_lines(
        file_cache, file_path, profile
    )
    if isinstance(cached_aligned_lines, (list, tuple)):
        return cached_aligned_lines

    if hasattr(file_cache, "get_stripped_lines"):
        res = file_cache.get_stripped_lines(file_path, profile)
        if isinstance(res, (list, tuple)):
            return res

    lines = file_cache.get_lines(file_path)
    if not isinstance(lines, (list, tuple)):
        return []

    try:
        return _fallback_strip(lines, profile)
    except Exception:  # pylint: disable=broad-exception-caught
        return lines


_CONFIG_PATTERN_CACHE = {}


def _get_config_pattern(config_key, flags=0):
    """Retrieve a regex cached against its current configuration value."""
    pattern_text = CONFIG[config_key]
    cache_key = (config_key, flags)
    cached_text, cached_pattern = _CONFIG_PATTERN_CACHE.get(
        cache_key, (None, None)
    )
    if cached_pattern is None or cached_text != pattern_text:
        cached_pattern = re.compile(pattern_text, flags)
        _CONFIG_PATTERN_CACHE[cache_key] = (pattern_text, cached_pattern)
    return cached_pattern


def _get_func_decl_pattern():
    """Retrieve or compile the cached regex for function declarations."""
    return _get_config_pattern('func_decl_pattern', re.MULTILINE)


def _get_callee_pattern():
    """Retrieve or compile the cached regex for callee matching."""
    return _get_config_pattern('callee_pattern')


class AstEngine:
    """Manages tree-sitter parser initialization and language support."""

    def __init__(self):
        """Initialize parser, language, and missing binding trackers."""
        self.parsers = {}
        self.languages = {}
        self.queries = OrderedDict()
        self.missing_bindings = {}
        self._initialized = False
        self._lock = threading.RLock()

    def initialize(self):
        """Dynamically load tree-sitter language bindings from configuration."""
        with self._lock:
            if self._initialized:
                return
            self.parsers.clear()
            self.languages.clear()
            self.queries = OrderedDict()
            self.missing_bindings.clear()

            if not HAS_TREESITTER:
                warn_once(
                    'tree-sitter',
                    "For perfect AST scoping, install tree-sitter bindings.",
                )
                self._initialized = True
                return

            for ext, val in CONFIG['bindings'].items():
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    warn_once(
                        f"invalid_binding_{ext}",
                        f"Invalid tree-sitter binding configuration for {ext}. "
                        "Expected list/tuple of (module_name, function_name), "
                        f"but got: {val}"
                    )
                    continue
                module_name, func_name = val
                try:
                    mod = importlib.import_module(module_name)
                    binding = getattr(mod, func_name)
                    binding_obj = binding() if callable(binding) else binding
                    try:
                        lang_obj = tree_sitter.Language(binding_obj)
                    except (TypeError, ValueError, RuntimeError):
                        lang_obj = binding_obj
                    parser = tree_sitter.Parser()
                    parser.set_language(lang_obj)
                    self.languages[ext] = lang_obj
                    self.parsers[ext] = parser
                except TREE_SITTER_BINDING_ERRORS:
                    self.missing_bindings[ext] = module_name
            self._initialized = True

    def is_supported(self, ext):
        """Check if tree-sitter parsing is supported for a given file extension."""
        with self._lock:
            self.initialize()
            return ext.lower() in self.parsers

    def get_query(self, ext, query_string):
        """Return a cached compiled tree-sitter query for ext/query_string."""
        if not query_string or not isinstance(query_string, str):
            raise ValueError("Query string must be a non-empty string")
        with self._lock:
            self.initialize()
            ext = ext.lower()
            query_key = (ext, query_string)
            query, found = _lru_get(self.queries, query_key)
            if found:
                return query
            query = tree_sitter.Query(self.languages[ext], query_string)
            _lru_set(self.queries, query_key, query)
            return query

    def get_language(self, ext):
        """Return a tree-sitter language object under the engine lock."""
        with self._lock:
            self.initialize()
            return self.languages[ext.lower()]

    def parse(self, ext, source_bytes):
        """Parse source bytes with the shared parser under the engine lock."""
        with self._lock:
            self.initialize()
            return self.parsers[ext.lower()].parse(source_bytes)


AST_ENGINE = AstEngine()


def _parse_ast_bytes(ext, source_bytes, ast_engine=None):
    """Parse source bytes through a locked AstEngine-compatible object."""
    ast_engine = ast_engine or AST_ENGINE
    ext = ext.lower()
    parse_func = getattr(ast_engine, 'parse', None)
    if callable(parse_func) and not hasattr(parse_func, 'mock_calls'):
        return parse_func(ext, source_bytes)

    lock = getattr(ast_engine, '_lock', None)
    with lock if lock is not None else nullcontext():
        return ast_engine.parsers[ext].parse(source_bytes)


def strip_strings_and_comments(line, file_path_or_extension=None):
    """Strip strings and comments using the registered language profile."""
    profile = get_language_profile(file_path_or_extension)
    return profile.strip_strings_and_comments(line)


def _parse_ast_source(file_path, ext, file_cache):
    """Parse source bytes for an AST-supported file, returning None for missing bytes."""
    source_bytes = file_cache.get_bytes(file_path)
    if not source_bytes:
        return None, None
    return source_bytes, _parse_ast_bytes(ext, source_bytes)


def extract_function_bounds_ast(file_path, line_num, ext, file_cache=None):
    """Extract 0-indexed start and end line bounds using tree-sitter AST nodes."""
    ext = ext.lower()
    if not AST_ENGINE.is_supported(ext):
        return None, None
    if file_cache is None:
        file_cache = get_global_cache()
    _, tree = _parse_ast_source(file_path, ext, file_cache)
    if tree is None or tree.root_node is None:
        return None, None
    target_row = line_num - 1

    target_node = None
    current = tree.root_node
    while current:
        found_child = None
        for child in current.children:
            try:
                child_start = child.start_point[0]
                child_end = child.end_point[0]
            except (TypeError, IndexError, AttributeError):
                continue
            if child_start <= target_row <= child_end:
                found_child = child
                break
        if found_child:
            target_node = found_child
            current = found_child
        else:
            break

    if not target_node:
        return None, None

    current = target_node
    block_types = [
        'function_definition', 'class_definition', 'function_item',
        'impl_item', 'function_declaration', 'method_definition'
    ]
    while current and current.type not in block_types and current.parent:
        current = current.parent

    if current and current.type in block_types:
        return current.start_point[0], current.end_point[0] + 1
    return None, None


def _extract_bounds_py_regex(lines, start_idx):
    """Fallback bounds extraction for Python using indentation."""
    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
    end_idx = start_idx + 1
    while end_idx < len(lines):
        line_stripped = lines[end_idx].strip()
        if line_stripped and not line_stripped.startswith('#') and \
           (len(lines[end_idx]) - len(lines[end_idx].lstrip())) <= base_indent:
            break
        end_idx += 1
    return start_idx, end_idx


def _extract_bounds_non_py_regex(lines, start_idx, target_idx, profile):
    """Fallback bounds extraction for non-Python using bracket counting."""
    end_idx = target_idx
    bracket_count, has_opened = 0, False
    for i in range(start_idx, len(lines)):
        clean_line = profile.strip_strings_and_comments(lines[i])
        bracket_count += clean_line.count('{') - clean_line.count('}')
        if '{' in clean_line:
            has_opened = True
        if has_opened and bracket_count <= 0:
            end_idx = i + 1
            break
    else:
        end_idx = min(len(lines), target_idx + 20)
    return start_idx, end_idx


def extract_function_bounds_regex(file_path, line_num, file_cache=None):
    """Extract start and end line bounds using linear non-backtracking regex fallback."""
    if line_num <= 0:
        return None, None
    if file_cache is None:
        file_cache = get_global_cache()
    profile = get_language_profile(file_path)
    lines = file_cache.get_stripped_lines(file_path, profile)
    if not lines:
        return None, None
    target_idx = line_num - 1
    if target_idx >= len(lines):
        return None, None

    func_decl_pattern = _get_func_decl_pattern()
    start_idx = target_idx
    while start_idx >= 0:
        if (func_decl_pattern.search(lines[start_idx])
                or (lines[start_idx].strip() and start_idx == 0)):
            break
        start_idx -= 1
    if start_idx < 0:
        start_idx = max(0, target_idx - 10)

    if profile.uses_indentation_blocks:
        return _extract_bounds_py_regex(lines, start_idx)
    return _extract_bounds_non_py_regex(lines, start_idx, target_idx, profile)


def extract_function_bounds(file_path, line_num, file_cache=None):
    """Extract start and end line bounds, preferring AST first and falling back to regex."""
    if line_num <= 0:
        return None, None
    ext = os.path.splitext(file_path)[1].lower()
    if AST_ENGINE.is_supported(ext):
        ast_bounds = extract_function_bounds_ast(file_path, line_num, ext, file_cache=file_cache)
        if ast_bounds[0] is not None:
            return ast_bounds
    return extract_function_bounds_regex(file_path, line_num, file_cache=file_cache)


def _trace_file_ast_dependencies(file_path, func_name, file_cache, callers):
    """Process a single file for AST dependency tracking."""
    ext = os.path.splitext(file_path)[1].lower()
    if get_language_profile(file_path).name == "python":
        content = file_cache.get_content(file_path)
        if "typing" not in content:
            warn_once(
                "python_typing",
                "Python files found without 'typing' protocols. "
                "Dynamic dispatch tracking relies on type hinting for accuracy."
            )

    if tree_sitter is None or not AST_ENGINE.is_supported(ext):
        return
    source_bytes, tree = _parse_ast_source(file_path, ext, file_cache)
    if tree is None or tree.root_node is None:
        return

    escaped_func_name = re.escape(func_name).replace("\\", "\\\\")

    query_strings = CONFIG['dependency_query_strings']
    q_str = query_strings.get(ext)
    if not q_str:
        return

    q_str = q_str.replace("{escaped_func_name}", escaped_func_name)

    try:
        query = AST_ENGINE.get_query(ext, q_str)
        captures = query.captures(tree.root_node)
        lines = file_cache.get_lines(file_path)
        if lines is None:
            return

        for capture_node, _ in captures:
            if capture_node.parent is None:
                continue
            node_text = source_bytes[
                capture_node.parent.start_byte:capture_node.parent.end_byte
            ].decode('utf-8', errors='ignore')
            if func_name not in node_text:
                continue

            line_idx = capture_node.start_point[0]
            if file_path not in callers:
                callers[file_path] = []
            if not any(c['line'] == line_idx + 1 for c in callers[file_path]):
                callers[file_path].append({
                    "line": line_idx + 1,
                    "code": lines[line_idx].strip()
                })
    except (RuntimeError, ValueError, TreeSitterQueryError) as exc:
        warn_once("ast_query_fail", f"AST query failed on {file_path}: {exc}")


def trace_lexical_dependencies_ast(func_name, repo_files, file_cache=None):
    """Trace all calls to func_name across supported files using tree-sitter AST queries."""
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    if not func_name or len(func_name) < 3:
        return callers

    fast_files = ripgrep_filter(
        repo_files, func_name,
        fallback_hint=f"callers of '{func_name}' (AST pass)"
    )

    for file_path in iter_scan_progress(
        fast_files,
        label=f"Scanning callers of '{func_name}' (AST pass)",
        min_files=100,
    ):
        _trace_file_ast_dependencies(file_path, func_name, file_cache, callers)

    return callers


def _process_regex_file(
    file_path,
    profile,
    call_pattern,
    def_patterns,
    def_cpp_pattern,
    callers,
    file_cache,
):
    """Search regex patterns within a single file."""
    stripped_content = file_cache.get_stripped_content(file_path, profile)
    if call_pattern.search(stripped_content):
        lines = file_cache.get_stripped_lines(file_path, profile)
        for idx, line in enumerate(lines):
            if not call_pattern.search(line):
                continue
            clean_line = profile.strip_strings_and_comments(line)
            if call_pattern.search(clean_line):
                is_def = False
                if any(p.search(clean_line) for p in def_patterns):
                    is_def = True
                elif (
                    def_cpp_pattern is not None
                    and def_cpp_pattern.search(clean_line)
                    and not clean_line.strip().endswith(';')
                ):
                    is_def = True

                if not is_def:
                    if file_path not in callers:
                        callers[file_path] = []
                    callers[file_path].append({"line": idx + 1, "code": line.strip()})


def trace_lexical_dependencies_regex(func_name, repo_files, file_cache=None):
    """Trace all calls to func_name across repo_files utilizing regex fallback."""
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    if not func_name or len(func_name) < 3:
        return callers
    fast_files = ripgrep_filter(
        repo_files, func_name,
        fallback_hint=f"callers of '{func_name}' (regex pass)"
    )

    profile_patterns_cache = {}
    for file_path in iter_scan_progress(
        fast_files,
        label=f"Scanning callers of '{func_name}' (regex pass)",
        min_files=100,
    ):
        profile = get_language_profile(file_path)
        if profile is UNKNOWN_LANGUAGE or file_path.endswith('.md'):
            continue
        if profile.name not in profile_patterns_cache:
            def_cpp_pattern = None
            if profile.uses_c_style_definitions:
                # pylint: disable=protected-access
                p_lead_b, _ = profile._get_boundaries(func_name)
                escaped_name = re.escape(func_name)
                def_cpp_pattern = re.compile(
                    r'^\s*(?:(?:[A-Za-z0-9_<>,]+(?:::[A-Za-z0-9_<>,]+)*)'
                    r'(?:\s+|[*&]+))*[\s*&]*'
                    r'(?:(?:[A-Za-z0-9_<>,]+(?:::[A-Za-z0-9_<>,]+)*)::)?'
                    + p_lead_b + escaped_name + r'\s*\('
                )
            profile_patterns_cache[profile.name] = (
                profile.get_call_pattern(func_name),
                profile.get_definition_patterns(func_name),
                def_cpp_pattern,
            )
        call_pattern, def_patterns, def_cpp_pattern = profile_patterns_cache[profile.name]

        _process_regex_file(
            file_path,
            profile,
            call_pattern,
            def_patterns,
            def_cpp_pattern,
            callers,
            file_cache,
        )
    return callers


def split_massive_block_ast(source_text, file_path, max_lines):
    """Truncate and omit large AST definition blocks to preserve context budgets."""
    return _split_massive_block_ast(
        source_text, file_path, max_lines, AST_ENGINE, get_language_profile
    )


def extract_callees_ast(file_path, start_line, end_line, ext, file_cache):  # pylint: disable=too-many-branches
    """Extract all functions/methods called inside a specific line range using tree-sitter AST."""
    ext = ext.lower()
    if tree_sitter is None or not AST_ENGINE.is_supported(ext):
        return set()
    _, tree = _parse_ast_source(file_path, ext, file_cache)
    if tree is None or tree.root_node is None:
        return set()

    func_node = None
    stack = list(reversed(tree.root_node.children))
    while stack:
        curr = stack.pop()
        curr_start = None
        curr_end = None
        try:
            curr_start = curr.start_point[0]
            curr_end = curr.end_point[0]
            should_traverse = curr_start <= start_line <= curr_end
        except (TypeError, IndexError, AttributeError):
            should_traverse = True

        if curr_start == start_line:
            func_node = curr
            break

        if should_traverse:
            for child in reversed(curr.children):
                stack.append(child)

    if func_node is None:
        func_node = tree.root_node

    query_strings = CONFIG['callee_query_strings']
    q_str = query_strings.get(ext)
    if not q_str:
        return set()

    callees = set()
    try:
        query = AST_ENGINE.get_query(ext, q_str)
        captures = query.captures(func_node)
        for node, _ in captures:
            if start_line <= node.start_point[0] < end_line:
                if not hasattr(node, 'text'):
                    raise AttributeError(
                        "Node object lacks '.text' attribute. "
                        "Please upgrade py-tree-sitter to version 0.21.0 or newer."
                    )
                callees.add(node.text.decode('utf-8', errors='ignore'))
    except AttributeError as ae:
        raise ae
    except (RuntimeError, ValueError, TypeError, TreeSitterQueryError) as e:
        raise RuntimeError(f"AST callee extraction failed: {e}") from e
    return callees


def extract_callees_regex(file_path, start_line, end_line, file_cache):
    """Extract all functions/methods called inside a line range using regex fallback."""
    profile = get_language_profile(file_path)
    lines = file_cache.get_stripped_lines(file_path, profile)[start_line:end_line]
    callees = set()
    callee_pattern = _get_callee_pattern()
    for line in lines:
        line_clean = profile.strip_strings_and_comments(line)
        for match in re.finditer(callee_pattern, line_clean):
            name = match.group(1)
            if name not in CONFIG['callee_ignored_keywords']:
                callees.add(name)
    return callees


def extract_callees(file_path, start_line, end_line, file_cache=None):
    """Extract list of callees within line bounds, falling back from AST to regex."""
    if file_cache is None:
        file_cache = get_global_cache()
    ext = os.path.splitext(file_path)[1].lower()
    if AST_ENGINE.is_supported(ext):
        try:
            callees = extract_callees_ast(file_path, start_line, end_line, ext, file_cache)
            return list(callees)
        except (AttributeError, RuntimeError) as e:
            print(f"\n[SmartDiffContextBuilder Warning] {e} "
                  "Falling back to regex-based callee extraction.")
    return list(extract_callees_regex(file_path, start_line, end_line, file_cache))


def find_callee_definition(callee_name, all_repo_files, file_cache=None):
    """Find the defining file and line number for callee_name across the repo."""
    if file_cache is None:
        file_cache = get_global_cache()
    if not callee_name or len(callee_name) < 3:
        return None, None

    candidate_files = ripgrep_filter(
        all_repo_files, callee_name,
        fallback_hint=f"definition of '{callee_name}'"
    )

    patterns_cache = {}

    for file_path in iter_scan_progress(
        candidate_files,
        label=f"Scanning definition of '{callee_name}'",
        min_files=100,
    ):
        profile = get_language_profile(file_path)
        if profile is UNKNOWN_LANGUAGE:
            continue

        if profile.name not in patterns_cache:
            # pylint: disable=protected-access
            p_lead_b, p_trail_b = profile._get_boundaries(callee_name)
            escaped_callee = re.escape(callee_name)
            pattern = CONFIG['def_pattern_template'].replace(
                "{lead_b}", p_lead_b
            ).replace(
                "{escaped_callee}", escaped_callee
            ).replace(
                "{trail_b}", p_trail_b
            )
            cpp_pattern = None
            if profile.uses_c_style_definitions:
                cpp_pattern_str = CONFIG['cpp_def_pattern_template'].replace(
                    "{lead_b}", p_lead_b
                ).replace(
                    "{escaped_callee}", escaped_callee
                ).replace(
                    "{trail_b}", p_trail_b
                )
                cpp_pattern = re.compile(cpp_pattern_str)
            patterns_cache[profile.name] = (
                re.compile(pattern),
                cpp_pattern,
                profile.get_definition_patterns(callee_name)
            )

        pattern, cpp_pattern, lang_def_patterns = patterns_cache[profile.name]

        lines = file_cache.get_stripped_lines(file_path, profile)
        for idx, line in enumerate(lines):
            clean_line = profile.strip_strings_and_comments(line)
            is_match = (
                pattern.search(clean_line)
                or (
                    cpp_pattern is not None
                    and cpp_pattern.search(clean_line)
                    and not clean_line.strip().endswith(';')
                )
                or any(p.search(clean_line) for p in lang_def_patterns)
            )
            if is_match:
                return file_path, idx + 1
    return None, None


def _process_identifier_node(node, lines, line_set, results):
    """Extract identifier info if it lies on a target line and is not a function call.

    Only processes nodes of type ``'identifier'`` – non-leaf nodes (expressions,
    statements, blocks) also carry a non-empty ``text`` attribute containing their
    entire source substring, which must not be treated as a single identifier.

    Args:
        node: Tree-sitter AST node.
        lines: List of source lines.
        line_set: Set of line numbers of interest.
        results: List to append (text, line, char_offset) tuples.
    """
    # Guard: only process leaf identifier nodes.
    if node.type != 'identifier':
        return
    try:
        node_line = node.start_point[0] + 1
        node_start_char = node.start_point[1]
    except (TypeError, IndexError, AttributeError):
        return
    if node_line not in line_set:
        return
    # Determine if identifier is part of a call expression or member access
    # that should be ignored.
    is_invalid = False
    parent = node.parent
    if parent and hasattr(parent, 'child_by_field_name'):
        if (
            parent.type == 'call_expression'
            and parent.child_by_field_name('function') == node
        ):
            is_invalid = True
        elif parent.type in MEMBER_FIELD_BY_NODE_TYPE:
            field_name = MEMBER_FIELD_BY_NODE_TYPE[parent.type]
            if parent.child_by_field_name(field_name) == node:
                is_invalid = True
        elif (
            parent.type == 'function_declarator'
            and parent.child_by_field_name('declarator') == node
        ):
            is_invalid = True
    if is_invalid:
        return
    text = node.text
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='ignore')
    if not text:
        return
    # Calculate character offset respecting UTF-16 code units (as used by many editors)
    char_offset = node_start_char
    if lines and 1 <= node_line <= len(lines):
        line_str = lines[node_line - 1]
        prefix_bytes = line_str.encode('utf-8')[:node_start_char]
        prefix_str = prefix_bytes.decode('utf-8', errors='ignore')
        char_offset = len(prefix_str.encode('utf-16-le')) // 2
    results.append((text, node_line, char_offset))


def _node_intersects_target_lines(child_start, child_end, sorted_target_lines):
    """Return whether a node range contains at least one target line."""
    idx = bisect.bisect_left(sorted_target_lines, child_start)
    return idx < len(sorted_target_lines) and sorted_target_lines[idx] <= child_end


def extract_identifiers_with_positions_ast(file_path, line_numbers, file_cache=None):  # pylint: disable=too-many-branches,too-many-nested-blocks
    """Query Tree-sitter for raw (identifier) nodes with their positions within modified lines."""
    if not line_numbers:
        return []
    if file_cache is None:
        file_cache = get_global_cache()
    ext = os.path.splitext(file_path)[1].lower()
    if not AST_ENGINE.is_supported(ext):
        return []

    source_bytes = file_cache.get_bytes(file_path)
    if not source_bytes:
        return []

    try:
        tree = _parse_ast_bytes(ext, source_bytes)
    except (RuntimeError, ValueError, TreeSitterQueryError):
        return []

    if tree is None or tree.root_node is None:
        return []

    results = []
    sorted_target_lines = sorted(set(line_numbers))
    line_set = set(sorted_target_lines)
    lines = file_cache.get_lines(file_path)

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        # Process identifier nodes
        _process_identifier_node(node, lines, line_set, results)

        # Traverse children whose line ranges intersect the target lines
        for child in reversed(node.children):
            try:
                child_start = child.start_point[0] + 1
                child_end = child.end_point[0] + 1
            except (TypeError, IndexError, AttributeError):
                stack.append(child)
                continue
            # If the start/end are not concrete integers (e.g., MagicMock in tests),
            # fall back to traversing the child.
            if not isinstance(child_start, int) or not isinstance(child_end, int):
                stack.append(child)
                continue
            if not _node_intersects_target_lines(
                    child_start, child_end, sorted_target_lines
            ):
                continue
            stack.append(child)
    return results


def _align_clean_to_original(original, clean):
    """Map indices of 'clean' back to their corresponding indices in 'original'.

    Returns a list of the same length as *clean* where each element is the
    index of the corresponding character in *original*.  Skipping the
    ``'replace'`` opcode (as was done previously) leaves the mapping list
    shorter than *clean*, causing subsequent index lookups to be shifted or
    to raise ``IndexError``.
    """
    if len(original) == len(clean):
        return list(range(len(clean)))

    matcher = difflib.SequenceMatcher(None, original, clean)
    mapping = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for offset in range(i2 - i1):
                mapping.append(i1 + offset)
        elif tag == 'replace':
            # Map each clean character to the closest original character.
            orig_len = i2 - i1
            clean_len = j2 - j1
            for k in range(clean_len):
                orig_idx = i1 + min(k, orig_len - 1)
                mapping.append(orig_idx)
        elif tag == 'delete':
            pass  # Characters only in original; nothing added to mapping.
        elif tag == 'insert':
            for _ in range(j2 - j1):
                mapping.append(i1)
    return mapping


def extract_identifiers_with_positions_regex(file_path, line_numbers, file_cache=None):
    """Extract standalone word boundaries with positions using regex."""
    if file_cache is None:
        file_cache = get_global_cache()
    lines = file_cache.get_lines(file_path)
    if not lines:
        return []

    profile = get_language_profile(file_path)
    if profile is None:
        return []
    results = []
    keywords = profile.keywords
    line_set = set(line_numbers)

    word_pattern = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*\b')

    for line_num in line_set:
        if 1 <= line_num <= len(lines):
            line_content = lines[line_num - 1]
            line_clean = profile.strip_strings_and_comments(line_content)
            mapping = _align_clean_to_original(line_content, line_clean)
            for match in word_pattern.finditer(line_clean):
                word = match.group(0)
                if word in keywords:
                    continue
                clean_prefix = line_clean[:match.start()].rstrip()
                if clean_prefix.endswith('.') or clean_prefix.endswith('->'):
                    continue
                suffix = line_clean[match.end():]
                suffix_stripped = suffix.lstrip()
                if suffix_stripped.startswith('(') or suffix_stripped.startswith('::'):
                    continue
                clean_start = match.start()
                orig_start = (
                    mapping[clean_start]
                    if clean_start < len(mapping)
                    else len(line_content)
                )
                prefix_str = line_content[:orig_start]
                char_offset = len(prefix_str.encode("utf-16-le")) // 2
                results.append((word, line_num, char_offset))

    return results


def extract_identifiers_with_positions(file_path, line_numbers, file_cache=None):
    """Unified entrypoint to extract identifiers with positions, falling back to regex."""
    if file_cache is None:
        file_cache = get_global_cache()
    ext = os.path.splitext(file_path)[1].lower()
    if AST_ENGINE.is_supported(ext):
        try:
            return extract_identifiers_with_positions_ast(file_path, line_numbers, file_cache)
        except (RuntimeError, ValueError, TreeSitterQueryError) as e:
            print(f"\n[SmartDiffContextBuilder Warning] AST position extraction failed: {e}. "
                  "Falling back to regex-based position extraction.")
    return extract_identifiers_with_positions_regex(file_path, line_numbers, file_cache)


def extract_identifiers_ast(file_path, line_numbers, file_cache=None):
    """Query Tree-sitter for raw (identifier) nodes within the modified diff lines.

    Filter out any node that is a child of a call_expression or function_declarator.
    """
    pos_ids = extract_identifiers_with_positions_ast(file_path, line_numbers, file_cache)
    return {name for name, _, _ in pos_ids}


def extract_identifiers_regex(file_path, line_numbers, file_cache=None):
    """Extract standalone word boundaries from modified diff lines using regex.

    Filter out any word followed immediately by ( or ::, and filter against
    a strict list of language keywords (if, return, while, auto, int).
    """
    pos_ids = extract_identifiers_with_positions_regex(file_path, line_numbers, file_cache)
    return {name for name, _, _ in pos_ids}


def extract_identifiers(file_path, line_numbers, file_cache=None):
    """Extract list of identifiers within line numbers, falling back from AST to regex."""
    if file_cache is None:
        file_cache = get_global_cache()
    ext = os.path.splitext(file_path)[1].lower()
    if AST_ENGINE.is_supported(ext):
        try:
            return extract_identifiers_ast(file_path, line_numbers, file_cache)
        except (RuntimeError, ValueError, TreeSitterQueryError) as e:
            print(f"\n[SmartDiffContextBuilder Warning] AST identifier extraction failed: {e}. "
                  "Falling back to regex-based identifier extraction.")
    return extract_identifiers_regex(file_path, line_numbers, file_cache)


def _decode_identifier_text(node):
    """Return identifier text from a Tree-sitter-like node."""
    text = node.text
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='ignore')
    return text


def _lhs_operator_index(node):
    """Return the first assignment operator child index, or -1."""
    for idx, child in enumerate(node.children):
        if child.type in ASSIGNMENT_OPERATORS:
            return idx
    return -1


def _lhs_skip_nodes(node):
    """Return RHS/init/value field nodes to skip during LHS traversal."""
    if not hasattr(node, 'child_by_field_name'):
        return []
    skip_nodes = []
    for field in ('right', 'init', 'value'):
        child = node.child_by_field_name(field)
        if child:
            skip_nodes.append(child)
    return skip_nodes


def _lhs_children_to_visit(node):
    """Return child nodes that can contain LHS identifiers."""
    operator_idx = _lhs_operator_index(node)
    skip_nodes = _lhs_skip_nodes(node)
    children_to_push = []
    for idx, child in enumerate(node.children):
        if operator_idx != -1 and idx >= operator_idx:
            break
        if child not in skip_nodes:
            children_to_push.append(child)
    return children_to_push


def get_lhs_identifiers(node):
    """Walk a Tree-sitter declaration or assignment node to find LHS target identifiers.

    Excludes initializers, RHS expressions, and operators.
    """
    ids = []

    stack = [node]
    while stack:
        curr = stack.pop()
        if curr.type == 'identifier':
            text = _decode_identifier_text(curr)
            if text:
                ids.append(text)
            continue

        for child in reversed(_lhs_children_to_visit(curr)):
            stack.append(child)

    return ids


def resolve_local_variable_ast(file_path, var_name, ref_line, file_cache=None):  # pylint: disable=too-many-return-statements,too-many-branches
    """Search locally within the enclosing function for the variable definition.

    Returns:
        tuple: (line_number, line_code) if found, else (None, None).
    """
    if file_cache is None:
        file_cache = get_global_cache()

    ext = os.path.splitext(file_path)[1].lower()
    if tree_sitter is None or not AST_ENGINE.is_supported(ext):
        return None, None

    source_bytes = file_cache.get_bytes(file_path)
    if not source_bytes:
        return None, None

    # Get function bounds
    func_start, _ = extract_function_bounds(file_path, ref_line, file_cache=file_cache)
    start_line = func_start + 1 if func_start is not None else 1

    try:
        tree = _parse_ast_bytes(ext, source_bytes)
    except (RuntimeError, ValueError, TreeSitterQueryError):
        return None, None

    if tree is None or tree.root_node is None:
        return None, None

    profile = get_language_profile(file_path)
    if profile is None:
        return None, None
    captures = []
    use_fallback = not profile.declaration_query
    if profile.declaration_query:
        try:
            query = AST_ENGINE.get_query(ext, profile.declaration_query)
            captures = query.captures(tree.root_node)
        except (RuntimeError, ValueError, TreeSitterQueryError):
            use_fallback = True

    if use_fallback:
        # Fallback to manual AST traversal
        nodes = []
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type in (
                'variable_declaration', 'assignment_expression', 'assignment',
                'short_var_declaration', 'assignment_statement',
                'local_variable_declaration', 'lexical_declaration', 'declaration'
            ):
                nodes.append(n)
            for c in reversed(n.children):
                stack.append(c)
        captures = [(n, None) for n in nodes]

    instantiations = []
    for node, _ in captures:
        lhs_ids = get_lhs_identifiers(node)
        if var_name in lhs_ids:
            node_line = node.start_point[0] + 1
            if start_line <= node_line <= ref_line:
                instantiations.append(node_line)

    if not instantiations:
        return None, None

    # Find closest instantiation before ref_line
    def_line = max(instantiations)
    lines = file_cache.get_lines(file_path)
    if lines is not None and 1 <= def_line <= len(lines):
        return def_line, lines[def_line - 1].strip()

    return None, None


def resolve_variable_definition(
    file_path, var_name, line_num, char_offset, file_cache=None, timeout=None
):
    """Resolve variable definition using local scope check, LSP, or Regex fallback.

    Returns:
        dict: Resolved definitions format.
    """
    if file_cache is None:
        file_cache = get_global_cache()

    # 1. Local Scope Check (AST First)
    local_line, local_code = resolve_local_variable_ast(file_path, var_name, line_num, file_cache)
    if local_line is not None:
        try:
            rel_path = os.path.relpath(file_path, os.getcwd())
        except ValueError:
            rel_path = file_path
        return {
            "resolved_type": "local",
            "definitions": [
                {
                    "path": rel_path,
                    "line": local_line,
                    "code": local_code
                }
            ]
        }

    # 2. Global & Member Check (LSP Handoff)
    from .lsp_client import (  # pylint: disable=import-outside-toplevel
        get_lsp_definition, get_lsp_type_definition, _parse_single_lsp_reference
    )

    resolved_defs = []
    defs = get_lsp_definition(file_path, line_num, char_offset, timeout)
    for d in defs:
        try:
            rel_path, def_line, def_code = _parse_single_lsp_reference(d, file_cache)
            resolved_defs.append({
                "path": rel_path,
                "line": def_line + 1,
                "code": def_code
            })
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    resolved_type_defs = []
    type_defs = get_lsp_type_definition(file_path, line_num, char_offset, timeout)
    for td in type_defs:
        try:
            rel_path, def_line, def_code = _parse_single_lsp_reference(td, file_cache)
            resolved_type_defs.append({
                "path": rel_path,
                "line": def_line + 1,
                "code": def_code
            })
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    result_defs = []
    resolved_type = "none"
    if resolved_defs:
        result_defs.extend(resolved_defs)
        resolved_type = "global"
    if resolved_type_defs:
        result_defs.extend(resolved_type_defs)
        if resolved_type == "none":
            resolved_type = "type"
        else:
            resolved_type = "global_and_type"

    if resolved_type != "none":
        return {
            "resolved_type": resolved_type,
            "definitions": result_defs
        }

    # 3. Constrained Regex Fallback (Safety Net)
    profile = get_language_profile(file_path)
    return resolve_variable_definition_regex_fallback(
        file_path, var_name, line_num, file_cache, profile
    )


class RegexScope:  # pylint: disable=too-few-public-methods
    """Representation of a lexical scope block for fallback resolution."""

    def __init__(self, start_line, parent=None, indent=-1):
        self.start_line = start_line
        self.end_line = None
        self.parent = parent
        self.indent = indent
        self.children = []

    def contains(self, line_num):
        """Check if this scope contains the specified line number."""
        if self.end_line is None:
            return self.start_line <= line_num
        return self.start_line <= line_num <= self.end_line


def build_scopes(file_path, profile, file_cache):  # pylint: disable=too-many-branches
    """Build scope tree for a file using language profile rules."""
    lines = _get_stripped_lines(file_cache, file_path, profile)
    if profile is None:
        global_scope = RegexScope(1)
        global_scope.end_line = len(lines)
        return global_scope, [global_scope]
    if profile.uses_indentation_blocks:
        global_scope = RegexScope(1, indent=-1)
        stack = [global_scope]
        all_scopes = [global_scope]
        for line_idx, line in enumerate(lines):
            line_num = line_idx + 1
            stripped = line.strip()
            comment_marker = profile.comment_prefix or "#"
            if not stripped or stripped.startswith(comment_marker):
                continue
            cleaned = profile.strip_strings_and_comments(line)
            if not cleaned.strip():
                continue
            indent = len(line) - len(line.lstrip())
            while len(stack) > 1 and indent <= stack[-1].indent:
                closed = stack.pop()
                closed.end_line = line_num - 1
            if cleaned.rstrip().endswith(':'):
                new_scope = RegexScope(line_num, parent=stack[-1], indent=indent)
                stack[-1].children.append(new_scope)
                stack.append(new_scope)
                all_scopes.append(new_scope)
        for s in stack:
            if s.end_line is None:
                s.end_line = len(lines)
        return global_scope, all_scopes

    # Brace-based
    global_scope = RegexScope(1)
    stack = [global_scope]
    all_scopes = [global_scope]
    for line_idx, line in enumerate(lines):
        line_num = line_idx + 1
        cleaned = profile.strip_strings_and_comments(line)
        for char in cleaned:
            if char == '{':
                new_scope = RegexScope(line_num, parent=stack[-1])
                stack[-1].children.append(new_scope)
                stack.append(new_scope)
                all_scopes.append(new_scope)
            elif char == '}':
                if len(stack) > 1:
                    closed = stack.pop()
                    closed.end_line = line_num
    for s in stack:
        if s.end_line is None:
            s.end_line = len(lines)
    return global_scope, all_scopes


def find_innermost_scope(scope, line_num):
    """Recursively search for the deepest child scope containing line_num."""
    for child in scope.children:
        if child.contains(line_num):
            return find_innermost_scope(child, line_num)
    return scope


def get_lines_directly_in_scope(scope, lines):
    """Get line numbers (1-based) directly within scope, excluding sub-scopes."""
    direct = []
    for line_num in range(scope.start_line, (scope.end_line or len(lines)) + 1):
        in_child = False
        for child in scope.children:
            if child.contains(line_num):
                in_child = True
                break
        if not in_child:
            direct.append(line_num)
    return direct


def _class_like_pattern(class_name, profile):
    """Return a class/struct declaration pattern for the language profile."""
    escaped = re.escape(class_name)
    if profile and profile.name == 'go':
        return re.compile(r'\btype\s+' + escaped + r'\s+(?:struct|interface)\b')
    return re.compile(r'\b(?:class|struct)\s+' + escaped + r'\b')


def _split_top_level_semicolon_statements(line):
    """Split a line into top-level semicolon-separated statement fragments."""
    parts = []
    start = 0
    paren_depth = bracket_depth = brace_depth = 0
    for idx, char in enumerate(line):
        if char == '(':
            paren_depth += 1
        elif char == ')':
            paren_depth = max(0, paren_depth - 1)
        elif char == '[':
            bracket_depth += 1
        elif char == ']':
            bracket_depth = max(0, bracket_depth - 1)
        elif char == '{':
            brace_depth += 1
        elif char == '}':
            brace_depth = max(0, brace_depth - 1)
        elif char == ';' and not (paren_depth or bracket_depth or brace_depth):
            parts.append(line[start:idx])
            start = idx + 1
    parts.append(line[start:])
    return parts


def _is_assignment_definition(cleaned_line, standalone_var, flow_kws):
    """Return whether an assignment-like statement defines the target variable."""
    assign_match = ASSIGNMENT_OPERATOR_RE.search(cleaned_line)
    if not assign_match:
        return False
    lhs = cleaned_line[:assign_match.start()]
    lhs_first = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)\b', lhs)
    lhs_starts_with_flow = lhs_first and lhs_first.group(1) in flow_kws
    return (
        (assign_match.group(0) == ':=' or not lhs_starts_with_flow)
        and re.search(standalone_var, lhs)
    )


def is_line_definition_of_var(cleaned_line, var_name, profile):
    """Check if a cleaned line defines var_name using simple regex heuristics."""
    escaped_var = re.escape(var_name)
    standalone_var = r'(?<!\.)(?<!->)(?<!::)\b' + escaped_var + r'\b'
    flow_kws = getattr(profile, 'flow_keywords', frozenset())

    # 1. Assignment
    if _is_assignment_definition(cleaned_line, standalone_var, flow_kws):
        return True

    for statement in _split_top_level_semicolon_statements(cleaned_line)[1:]:
        if _is_assignment_definition(statement, standalone_var, flow_kws):
            return True

    # 2. Explicit keywords
    if re.search(r'\b(?:let|const|var|mut)\s+' + standalone_var, cleaned_line):
        return True

    # 3. Type-based declarations (C/C++/Java/Go/Rust)
    # Match a leading type name followed by the variable name, but reject
    # flow-control/statement keywords (e.g. 'return', 'if', 'for') that
    # cannot be type names and would otherwise cause false positives such as
    # treating `return x;` as a definition.
    # We use profile.flow_keywords (not profile.keywords) because the full
    # keyword set also includes primitive type names ('int', 'char', etc.)
    # that are perfectly valid as type declaration prefixes.
    type_decl_match = re.search(
        r'\b([A-Za-z_][A-Za-z0-9_<>:,*&]*)\s+' + standalone_var,
        cleaned_line,
    )
    if type_decl_match and type_decl_match.group(1) not in flow_kws:
        return True

    # 4. Parameters in function headers
    if (
        re.search(r'\b(?:def|fn|function|sub|func)\s+[A-Za-z0-9_]+', cleaned_line)
        or profile.uses_c_style_definitions
    ):
        param_match = re.search(r'\(([^)]*)\)', cleaned_line)
        if param_match:
            params = param_match.group(1)
            if re.search(standalone_var, params):
                return True

    return False


def get_class_members(file_path, class_name, profile, file_cache):  # pylint: disable=too-many-branches,too-many-statements
    """Extract and cache member variables defined in a class."""
    if file_cache is None:
        file_cache = get_global_cache()

    if profile is None:
        return []

    class_members_cache = _get_lru_cache(file_cache, "class_members_cache")

    cache_key = (os.path.abspath(file_path), class_name)
    cached_members, found = _lru_get(class_members_cache, cache_key)
    if found:
        return cached_members

    lines = _get_stripped_lines(file_cache, file_path, profile)
    class_line_num = None
    class_pattern = _class_like_pattern(class_name, profile)
    for idx, line in enumerate(lines):
        cleaned = profile.strip_strings_and_comments(line)
        if class_pattern.search(cleaned):
            class_line_num = idx + 1
            break

    if class_line_num is None:
        return []

    _, all_scopes = build_scopes(file_path, profile, file_cache)
    class_scope = None
    for s in all_scopes:
        if s.start_line >= class_line_num and s.parent is not None:
            class_scope = s
            break

    if class_scope is None:
        return []

    direct_lines = get_lines_directly_in_scope(class_scope, lines)
    members = []
    for ln in direct_lines:
        line = lines[ln - 1]
        cleaned = profile.strip_strings_and_comments(line)
        assign_match = ASSIGNMENT_OPERATOR_RE.search(cleaned)
        if assign_match:
            lhs = cleaned[:assign_match.start()]
            if '(' in lhs:
                continue
            for m in re.finditer(r'\b[A-Za-z_][A-Za-z0-9_]*\b', lhs):
                name = m.group(0)
                if name not in profile.keywords:
                    members.append((name, ln))
        else:
            if profile.name == 'go':
                decl_match = re.search(
                    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s+[\*\[\]A-Za-z_]',
                    cleaned,
                )
            else:
                decl_match = re.search(
                    r'\b[A-Za-z_][A-Za-z0-9_<>:,*&]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*;',
                    cleaned,
                )
            if decl_match:
                name = decl_match.group(1)
                if name not in profile.keywords:
                    members.append((name, ln))

    if profile.name == 'python':
        for child in class_scope.children:
            start_line_text = lines[child.start_line - 1]
            if 'def ' in start_line_text:
                for ln in range(child.start_line, (child.end_line or len(lines)) + 1):
                    line = lines[ln - 1]
                    cleaned = profile.strip_strings_and_comments(line)
                    self_match = re.search(
                        r'\bself\.([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)',
                        cleaned,
                    )
                    if self_match:
                        name = self_match.group(1)
                        members.append((name, ln))

    seen = set()
    unique_members = []
    for name, ln in members:
        if name not in seen:
            seen.add(name)
            unique_members.append((name, ln))

    _lru_set(class_members_cache, cache_key, unique_members)
    return unique_members


def get_parent_classes(file_path, class_name, profile, file_cache):
    """Identify the parent class name(s) for a given class."""
    # pylint: disable=too-many-nested-blocks
    if profile is None:
        return []
    lines = _get_stripped_lines(file_cache, file_path, profile)
    class_pattern = _class_like_pattern(class_name, profile)
    for line in lines:
        cleaned = profile.strip_strings_and_comments(line)
        if class_pattern.search(cleaned):
            if profile.name == 'python':
                m = re.search(r'\bclass\s+' + re.escape(class_name) + r'\s*\(([^)]+)\)', cleaned)
                if m:
                    return [p.strip() for p in m.group(1).split(',') if p.strip()]
            elif profile.name in ('java', 'javascript', 'typescript'):
                m = re.search(
                    r'\bclass\s+' + re.escape(class_name) + r'\s+extends\s+([A-Za-z0-9_]+)', cleaned
                )
                if m:
                    return [m.group(1).strip()]
            elif profile.name in ('c_family', 'c-family'):
                m = re.search(
                    r'\b(?:class|struct)\s+' + re.escape(class_name) + r'\s*:\s*([^{]+)', cleaned
                )
                if m:
                    parents_part = m.group(1)
                    parents = []
                    for p in parents_part.split(','):
                        p = p.strip()
                        p_clean = re.sub(
                            r'\b(?:public|protected|private|virtual)\s+', '', p
                        ).strip()
                        p_name = re.match(r'^([A-Za-z0-9_]+)', p_clean)
                        if p_name:
                            parents.append(p_name.group(1))
                    return parents
    return []


def find_class_definition(start_file, class_name, profile, file_cache):
    """Locate the file and line number where class_name is defined."""
    if file_cache is None:
        file_cache = get_global_cache()

    find_class_definition_cache = _get_lru_cache(
        file_cache, "find_class_definition_cache"
    )

    cache_key = (os.path.abspath(start_file), class_name)
    cached_definition, found = _lru_get(find_class_definition_cache, cache_key)
    if found:
        return cached_definition

    def _find():  # pylint: disable=too-many-branches
        if profile is None:
            return None, None
        lines = _get_stripped_lines(file_cache, start_file, profile)
        class_pattern = _class_like_pattern(class_name, profile)
        for idx, line in enumerate(lines):
            cleaned = profile.strip_strings_and_comments(line)
            if class_pattern.search(cleaned):
                return start_file, idx + 1

        included_files = get_directly_included_files(start_file, profile, file_cache)
        for inc_file in included_files:
            if os.path.exists(inc_file):
                inc_profile = get_language_profile(inc_file)
                if inc_profile is None:
                    continue
                inc_class_pattern = _class_like_pattern(class_name, inc_profile)
                lines = _get_stripped_lines(file_cache, inc_file, inc_profile)
                for idx, line in enumerate(lines):
                    cleaned = inc_profile.strip_strings_and_comments(line)
                    if inc_class_pattern.search(cleaned):
                        return inc_file, idx + 1

        from .sys_utils import get_git_tracked_files  # pylint: disable=import-outside-toplevel
        ext = os.path.splitext(start_file)[1].lower()
        tracked_files = get_git_tracked_files()
        same_ext_files = [
            f for f in tracked_files
            if os.path.splitext(f)[1].lower() == ext and f != start_file
        ]
        candidate_files = ripgrep_filter(
            same_ext_files, class_name,
            fallback_hint=f"class/struct definition of '{class_name}'"
        )
        for f in candidate_files:
            if os.path.exists(f):
                f_profile = get_language_profile(f)
                if f_profile is None:
                    continue
                f_class_pattern = _class_like_pattern(class_name, f_profile)
                lines = _get_stripped_lines(file_cache, f, f_profile)
                for idx, line in enumerate(lines):
                    cleaned = f_profile.strip_strings_and_comments(line)
                    if f_class_pattern.search(cleaned):
                        return f, idx + 1

        return None, None

    res = _find()
    _lru_set(find_class_definition_cache, cache_key, res)
    return res


def resolve_class_member_definition(
    file_path, class_name, var_name, profile, file_cache, searched_classes=None
):
    """Recursively search for var_name in class_name and parent inheritance tree."""
    # Normalize to absolute path so that the recursion guard (searched_classes)
    # is not bypassed when relative and absolute paths to the same file are mixed.
    file_path = os.path.abspath(file_path)

    if searched_classes is None:
        searched_classes = set()

    class_key = (file_path, class_name)
    if class_key in searched_classes:
        return None
    searched_classes.add(class_key)

    members = get_class_members(file_path, class_name, profile, file_cache)
    for name, ln in members:
        if name == var_name:
            lines = file_cache.get_lines(file_path)
            code_line = ""
            if lines is not None and 1 <= ln <= len(lines):
                code_line = lines[ln - 1].strip()
            try:
                rel_path = os.path.relpath(file_path, os.getcwd())
            except ValueError:
                rel_path = file_path
            return {
                "path": rel_path,
                "line": ln,
                "code": code_line
            }

    parents = get_parent_classes(file_path, class_name, profile, file_cache)
    for parent in parents:
        parent_file, _ = find_class_definition(file_path, parent, profile, file_cache)
        if parent_file:
            parent_profile = get_language_profile(parent_file)
            if parent_profile is None:
                continue
            res = resolve_class_member_definition(
                parent_file, parent, var_name, parent_profile, file_cache, searched_classes
            )
            if res:
                return res

    return None


def _parse_python_imports(cleaned, includes):
    """Parse python import statements from cleaned line and add to includes."""
    m1 = re.match(r'^import\s+([A-Za-z0-9_.,\s]+)', cleaned)
    if m1:
        for parts in m1.group(1).split(','):
            parts = parts.strip()
            parts = re.split(r'\s+as\s+', parts)[0].strip()
            parts = parts.replace('.', '/')
            includes.append(parts)
        return

    m2 = re.match(r'^from\s+([A-Za-z0-9_.]+)\s+import\s+(.+)', cleaned)
    if not m2:
        return

    raw_module = m2.group(1)
    names_part = m2.group(2).strip().replace('(', '').replace(')', '')
    imported_names = []
    for name in names_part.split(','):
        name = re.split(r'\s+as\s+', name.strip())[0].strip()
        if name and re.match(r'^[A-Za-z0-9_.]+$', name):
            imported_names.append(name.replace('.', '/'))

    dots_match = re.match(r'^(\.+)', raw_module)
    if dots_match:
        dots = dots_match.group(1)
        remainder_raw = raw_module[len(dots):]
        remainder = remainder_raw.replace('.', '/') if remainder_raw else ''
        climb = "../" * (len(dots) - 1) if len(dots) > 1 else ""
        base = climb + remainder
        is_relative = True
    else:
        remainder = raw_module.replace('.', '/')
        base = remainder
        is_relative = False

    has_suffix = bool(remainder or not is_relative)
    if base and has_suffix:
        includes.append(base)

    suffix = "/" if has_suffix else ""
    for name in imported_names:
        includes.append(base + suffix + name)


def _parse_go_imports(cleaned, includes, in_import_block):
    """Parse Go import declarations, including grouped import blocks."""
    if in_import_block:
        for imported in re.findall(r'["\']([^"\']+)["\']', cleaned):
            includes.append(imported)
        return ')' not in cleaned

    block_match = re.match(r'^import\s*\((.*)', cleaned)
    if block_match:
        remainder = block_match.group(1)
        for imported in re.findall(r'["\']([^"\']+)["\']', remainder):
            includes.append(imported)
        return ')' not in remainder

    m = re.match(r'^import\s+(?:[A-Za-z0-9_.]+\s+)?["\']([^"\']+)["\']', cleaned)
    if m:
        includes.append(m.group(1))
    return False


def _split_top_level_commas(value):
    """Split comma-separated import fragments while respecting nested braces."""
    parts = []
    depth = 0
    start = 0
    for idx, char in enumerate(value):
        if char == '{':
            depth += 1
        elif char == '}':
            depth = max(0, depth - 1)
        elif char == ',' and depth == 0:
            parts.append(value[start:idx])
            start = idx + 1
    parts.append(value[start:])
    return parts


def _normalize_rust_import_path(path):
    """Normalize a Rust use path into a dependency lookup candidate."""
    path = path.strip().strip(';').strip()
    path = re.sub(r'\s+as\s+[A-Za-z_][A-Za-z0-9_]*$', '', path).strip()
    if not path:
        return None

    parts = [p for p in path.split('::') if p]
    if not parts:
        return None

    if parts[0] in ('crate', 'self'):
        parts = parts[1:]
    else:
        parent_prefix = []
        while parts and parts[0] == 'super':
            parent_prefix.append('..')
            parts = parts[1:]
        parts = parent_prefix + parts

    if not parts:
        return None
    if parts[-1] in ('self', '*'):
        parts = parts[:-1]
    if not parts:
        return None
    return '/'.join(parts)


def _rust_import_candidates(path):
    """Return progressive dependency lookup candidates for a Rust use path."""
    normalized = _normalize_rust_import_path(path)
    if not normalized:
        return []
    parts = normalized.split('/')
    candidates = []
    for idx in range(1, len(parts) + 1):
        candidate = '/'.join(parts[:idx])
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _collect_rust_use_paths(use_tree, prefix=''):
    """Collect module lookup candidates from a Rust use tree."""
    use_tree = use_tree.strip().strip(';').strip()
    if not use_tree:
        return []

    brace_idx = use_tree.find('{')
    if brace_idx == -1:
        return _rust_import_candidates(prefix + use_tree)

    base = use_tree[:brace_idx].strip()
    base_prefix = prefix + base
    if base_prefix and not base_prefix.endswith('::'):
        base_prefix += '::'

    depth = 0
    end_idx = None
    for idx in range(brace_idx, len(use_tree)):
        if use_tree[idx] == '{':
            depth += 1
        elif use_tree[idx] == '}':
            depth -= 1
            if depth == 0:
                end_idx = idx
                break

    if end_idx is None:
        return []

    paths = []
    paths.extend(_rust_import_candidates(base_prefix.rstrip(':')))
    inner = use_tree[brace_idx + 1:end_idx]
    for fragment in _split_top_level_commas(inner):
        paths.extend(_collect_rust_use_paths(fragment, base_prefix))
    return paths


def _parse_rust_use_imports(cleaned, includes, pending_use):
    """Parse Rust use declarations, including grouped use trees."""
    if pending_use:
        pending_use = f"{pending_use} {cleaned}"
    else:
        match = re.match(r'^use\s+(.+)', cleaned)
        if not match:
            return None
        pending_use = match.group(1)

    if ';' not in pending_use:
        return pending_use

    statement, _, remainder = pending_use.partition(';')
    includes.extend(_collect_rust_use_paths(statement))
    remainder = remainder.strip()
    if remainder.startswith('use '):
        return _parse_rust_use_imports(remainder, includes, None)
    return None


def get_directly_included_files(file_path, profile, file_cache):  # pylint: disable=too-many-branches,too-many-statements
    """Find files directly imported or included in the source file."""
    if file_cache is None:
        file_cache = get_global_cache()

    included_files_cache = _get_lru_cache(
        file_cache, "get_directly_included_files_cache"
    )

    cache_key = os.path.abspath(file_path)
    cached_files, found = _lru_get(included_files_cache, cache_key)
    if found:
        return cached_files

    if profile is None:
        return []

    def _get_files():  # pylint: disable=too-many-branches,too-many-statements
        lines = _get_stripped_lines(file_cache, file_path, profile)
        includes = []
        curr_dir = os.path.dirname(file_path)
        in_go_import_block = False
        pending_rust_use = None

        for line in lines:
            cleaned = _strip_comments_only(line, profile).strip()
            if not cleaned:
                continue

            if profile.name in ('c_family', 'c-family'):
                m = re.match(r'#\s*include\s*["<]([^">]+)[">]', cleaned)
                if m:
                    includes.append(m.group(1))
            elif profile.name == 'python':
                _parse_python_imports(cleaned, includes)
            elif profile.name == 'java':
                m = re.match(r'^import\s+([A-Za-z0-9_.]+)\s*;', cleaned)
                if m:
                    parts = m.group(1).split('.')
                    if parts:
                        includes.append('/'.join(parts))
            elif profile.name in ('javascript', 'typescript'):
                m1 = re.match(r'^import\s+.*\s+from\s+["\']([^"\']+)["\']', cleaned)
                if m1:
                    includes.append(m1.group(1))
                m2 = re.search(r'\brequire\s*\(\s*["\']([^"\']+)["\']\s*\)', cleaned)
                if m2:
                    includes.append(m2.group(1))
            elif profile.name == 'go':
                in_go_import_block = _parse_go_imports(
                    cleaned, includes, in_go_import_block
                )
            elif profile.name == 'rust':
                pending_rust_use = _parse_rust_use_imports(
                    cleaned, includes, pending_rust_use
                )

        from .sys_utils import get_git_tracked_files  # pylint: disable=import-outside-toplevel
        resolved_paths = []
        ext = os.path.splitext(file_path)[1].lower()
        tracked_files = get_git_tracked_files()

        for inc in includes:
            rel_candidate = os.path.abspath(os.path.join(curr_dir, inc))
            if os.path.exists(rel_candidate) and os.path.isfile(rel_candidate):
                resolved_paths.append(rel_candidate)
                continue
            if not inc.endswith(ext):
                rel_candidate_ext = rel_candidate + ext
                if os.path.exists(rel_candidate_ext) and os.path.isfile(rel_candidate_ext):
                    resolved_paths.append(rel_candidate_ext)
                    continue

            inc_norm = inc.replace('\\', '/').rstrip('/')
            for tf in tracked_files:
                tf_norm = tf.replace('\\', '/')
                is_match = (
                    tf_norm == inc_norm
                    or tf_norm.endswith('/' + inc_norm)
                    or tf_norm == inc_norm + ext
                    or tf_norm.endswith('/' + inc_norm + ext)
                )
                if not is_match:
                    is_match = (
                        tf_norm.endswith('/' + inc_norm + '/' + os.path.basename(tf_norm))
                        or tf_norm == inc_norm + '/' + os.path.basename(tf_norm)
                    )
                if is_match:
                    full_tf = os.path.abspath(tf)
                    if os.path.exists(full_tf):
                        resolved_paths.append(full_tf)
                        break

        return resolved_paths

    res = _get_files()
    _lru_set(included_files_cache, cache_key, res)
    return res


def resolve_global_definition(
    file_path, var_name, profile, file_cache, searched_files=None
):  # pylint: disable=too-many-statements
    """Search globally for var_name in current file, imports, and repo files."""
    if file_cache is None:
        file_cache = get_global_cache()

    global_definition_cache = _get_lru_cache(
        file_cache, "resolve_global_definition_cache"
    )

    cache_key = (os.path.abspath(file_path), var_name)
    cached_definition, found = _lru_get(global_definition_cache, cache_key)
    if found:
        return cached_definition

    def _resolve():
        if searched_files is None:
            searched_files_local = set()
        else:
            searched_files_local = searched_files

        file_abs = os.path.abspath(file_path)
        if file_abs in searched_files_local:
            return []
        searched_files_local.add(file_abs)

        def search_file_globals(f):
            if not os.path.exists(f):
                return None
            f_profile = get_language_profile(f)
            if f_profile is None:
                return None
            lines = _get_stripped_lines(file_cache, f, f_profile)
            if not lines:
                return None
            global_scope, _ = build_scopes(f, f_profile, file_cache)
            global_lines = get_lines_directly_in_scope(global_scope, lines)
            for ln in global_lines:
                line = lines[ln - 1]
                cleaned = f_profile.strip_strings_and_comments(line)
                if is_line_definition_of_var(cleaned, var_name, f_profile):
                    try:
                        rel_path = os.path.relpath(f, os.getcwd())
                    except ValueError:
                        rel_path = f
                    original_lines = file_cache.get_lines(f)
                    code_snippet = ""
                    if original_lines and len(original_lines) >= ln:
                        code_snippet = original_lines[ln - 1].strip()
                    else:
                        code_snippet = line.strip()
                    return {
                        "path": rel_path,
                        "line": ln,
                        "code": code_snippet
                    }
            return None

        res = search_file_globals(file_path)
        if res:
            return [res]

        included_files = get_directly_included_files(file_path, profile, file_cache)
        for inc_file in included_files:
            res = search_file_globals(inc_file)
            if res:
                return [res]

        from .sys_utils import get_git_tracked_files  # pylint: disable=import-outside-toplevel
        ext = os.path.splitext(file_path)[1].lower()
        tracked_files = get_git_tracked_files()
        same_ext_files = [f for f in tracked_files if os.path.splitext(f)[1].lower() == ext]
        candidate_files = ripgrep_filter(
            same_ext_files, var_name,
            fallback_hint=f"global definition of '{var_name}'"
        )
        for f in candidate_files:
            f_abs = os.path.abspath(f)
            if f_abs not in searched_files_local:
                res = search_file_globals(f_abs)
                if res:
                    return [res]

        return []

    res = _resolve()
    _lru_set(global_definition_cache, cache_key, res)
    return res


def resolve_variable_definition_regex_fallback(
    file_path, var_name, line_num, file_cache, profile
):  # pylint: disable=too-many-branches
    """Fall back to regex resolution scanning local scopes, class members, and globals."""
    if profile is None:
        return {"resolved_type": "none", "definitions": []}
    lines = file_cache.get_lines(file_path)
    if not lines:
        return {"resolved_type": "none", "definitions": []}
    global_scope, all_scopes = build_scopes(file_path, profile, file_cache)

    func_start, _ = extract_function_bounds(file_path, line_num, file_cache=file_cache)
    func_start_line = func_start + 1 if func_start is not None else 1

    innermost = find_innermost_scope(global_scope, line_num)

    outermost_func_scope = None
    for s in all_scopes:
        if s.start_line >= func_start_line and s.parent is not None:
            outermost_func_scope = s
            break

    scope_chain = []
    curr = innermost
    while curr is not None and curr.start_line >= func_start_line:
        scope_chain.append(curr)
        curr = curr.parent

    for scope in scope_chain:
        direct_lines = get_lines_directly_in_scope(scope, lines)
        valid_lines = [ln for ln in direct_lines if ln < line_num]
        if scope == outermost_func_scope and not profile.uses_indentation_blocks:
            valid_lines.extend(
                ln for ln in range(func_start_line, outermost_func_scope.start_line)
                if ln < line_num
            )
        for ln in sorted(valid_lines, reverse=True):
            line = lines[ln - 1]
            cleaned = profile.strip_strings_and_comments(line)
            if is_line_definition_of_var(cleaned, var_name, profile):
                try:
                    rel_path = os.path.relpath(file_path, os.getcwd())
                except ValueError:
                    rel_path = file_path
                return {
                    "resolved_type": "local_regex",
                    "definitions": [{
                        "path": rel_path,
                        "line": ln,
                        "code": line.strip()
                    }]
                }

    class_name = None
    func_header = lines[func_start_line - 1]
    cpp_match = re.search(r'\b([A-Za-z0-9_]+)::[A-Za-z0-9_]+\s*\(', func_header)
    if cpp_match:
        class_name = cpp_match.group(1)
    else:
        if outermost_func_scope and outermost_func_scope.parent:
            parent_scope = outermost_func_scope.parent
            limit = parent_scope.parent.start_line if parent_scope.parent else 1
            for l_idx in range(parent_scope.start_line - 1, limit - 2, -1):
                parent_header = lines[l_idx]
                class_match = re.search(r'\b(?:class|struct)\s+([A-Za-z0-9_]+)\b', parent_header)
                if class_match:
                    class_name = class_match.group(1)
                    break

    if class_name:
        res = resolve_class_member_definition(file_path, class_name, var_name, profile, file_cache)
        if res:
            return {
                "resolved_type": "member_regex",
                "definitions": [res]
            }

    global_defs = resolve_global_definition(file_path, var_name, profile, file_cache)
    if global_defs:
        return {
            "resolved_type": "global_regex",
            "definitions": global_defs
        }

    return {
        "resolved_type": "none",
        "definitions": []
    }
