# pylint: disable=too-many-lines,cyclic-import
"""
AST analysis engine utilizing tree-sitter or regex fallback.
Provides syntax-aware function boundary extraction, dependency tracing,
and callee analysis.
"""


import os
import re
import difflib
import importlib
from .sys_utils import iter_scan_progress, warn_once, ripgrep_filter
from .cache import get_global_cache
from .languages import UNKNOWN_LANGUAGE, get_language_profile

try:
    import tree_sitter
    HAS_TREESITTER = True
except ImportError:
    tree_sitter = None
    HAS_TREESITTER = False

from .config import CONFIG, ConfigDictProxy

LANG_MAP = ConfigDictProxy('lang_map')




def _strip_comments_only(line, profile):
    """Strip same-line comments while leaving string literals intact."""
    if not profile or not isinstance(profile.line_comment, str):
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
    if not isinstance(stripped, str):
        return lines
    return stripped.splitlines(keepends=True)


def _get_stripped_lines(file_cache, file_path, profile):
    """Retrieve stripped lines from cache."""
    if file_cache is None:
        file_cache = get_global_cache()

    if profile is None:
        lines = file_cache.get_lines(file_path)
        return lines if isinstance(lines, (list, tuple)) else []

    if hasattr(file_cache, "get_stripped_lines"):
        res = file_cache.get_stripped_lines(file_path, profile)
        if isinstance(res, (list, tuple)):
            return res

    lines = file_cache.get_lines(file_path)
    if not isinstance(lines, (list, tuple)):
        return []

    if hasattr(profile, "strip_block_comments") and callable(profile.strip_block_comments):
        try:
            return _fallback_strip(lines, profile)
        except Exception:  # pylint: disable=broad-exception-caught
            return lines
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
        self.missing_bindings = {}
        self._initialized = False

    def initialize(self):
        """Dynamically load tree-sitter language bindings from configuration."""
        if self._initialized:
            return
        self.parsers.clear()
        self.languages.clear()
        self.missing_bindings.clear()

        if not HAS_TREESITTER:
            warn_once('tree-sitter', "For perfect AST scoping, install tree-sitter bindings.")
            self._initialized = True
            return

        for ext, val in CONFIG['bindings'].items():
            if not isinstance(val, (list, tuple)) or len(val) != 2:
                warn_once(
                    f"invalid_binding_{ext}",
                    f"Invalid tree-sitter binding configuration for {ext}. "
                    f"Expected list/tuple of (module_name, function_name), but got: {val}"
                )
                continue
            module_name, func_name = val
            try:
                mod = importlib.import_module(module_name)
                binding = getattr(mod, func_name)
                binding_obj = binding() if callable(binding) else binding
                try:
                    lang_obj = tree_sitter.Language(binding_obj)
                except Exception:  # pylint: disable=broad-exception-caught
                    lang_obj = binding_obj
                parser = tree_sitter.Parser()
                parser.set_language(lang_obj)
                self.languages[ext] = lang_obj
                self.parsers[ext] = parser
            except Exception:  # pylint: disable=broad-exception-caught
                self.missing_bindings[ext] = module_name
        self._initialized = True

    def is_supported(self, ext):
        """Check if tree-sitter parsing is supported for a given file extension."""
        self.initialize()
        return ext.lower() in self.parsers


AST_ENGINE = AstEngine()


def strip_strings_and_comments(line, file_path_or_extension=None):
    """Strip strings and comments using the registered language profile."""
    profile = get_language_profile(file_path_or_extension)
    return profile.strip_strings_and_comments(line)


def extract_function_bounds_ast(file_path, line_num, ext, file_cache=None):
    """Extract 0-indexed start and end line bounds using tree-sitter AST nodes."""
    ext = ext.lower()
    if not AST_ENGINE.is_supported(ext):
        return None, None
    if file_cache is None:
        file_cache = get_global_cache()
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)
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
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)
    if tree is None or tree.root_node is None:
        return

    escaped_func_name = re.escape(func_name).replace("\\", "\\\\")

    query_strings = CONFIG['dependency_query_strings']
    q_str = query_strings.get(ext)
    if not q_str:
        return

    q_str = q_str.replace("{escaped_func_name}", escaped_func_name)

    try:
        query = tree_sitter.Query(AST_ENGINE.languages[ext], q_str)
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
    except Exception as exc:  # pylint: disable=broad-exception-caught
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


def _semantically_truncate_child(child, lines, profile):
    """Perform semantic truncation of an AST node."""
    uses_indentation_blocks = profile.uses_indentation_blocks
    sig_lines = []
    end_idx = min(child.end_point[0], len(lines) - 1)
    has_brace = False
    for idx in range(child.start_point[0], end_idx + 1):
        line = lines[idx]
        sig_lines.append(line)
        clean_line = profile.strip_strings_and_comments(line)
        if not uses_indentation_blocks and "{" in clean_line:
            has_brace = True
            break
        if uses_indentation_blocks and clean_line.rstrip().endswith(":"):
            break

    truncated_lines = list(sig_lines)
    if sig_lines:
        indent = len(sig_lines[0]) - len(sig_lines[0].lstrip())
        if uses_indentation_blocks:
            truncated_lines.append(
                " " * (indent + 4)
                + profile.format_omission_comment(
                    "Inner Body Omitted for Context Preservation"
                )
            )
            truncated_lines.append(" " * (indent + 4) + "pass")
        else:
            if has_brace:
                truncated_lines.append(
                    " " * (indent + 4)
                    + profile.format_omission_comment(
                        "Inner Body Omitted for Context Preservation"
                    )
                )
                truncated_lines.append(" " * indent + "}")
    return truncated_lines


def _get_fallback_truncated_text(lines, max_lines, profile):
    """Get fallback plain truncation when AST parsing is not supported."""
    omission_comment = profile.format_omission_comment("Lines Omitted due to size")
    return "\n".join(lines[:max_lines]) + f"\n{omission_comment}"


def _get_group_min_lines(group, lines, profile):
    """Get the minimum representation lines for a group of children."""
    start_line = group["start_line"]
    end_line = group["end_line"]
    group_children = group["children"]

    definition_types = {
        'function_definition', 'class_definition', 'function_item', 'impl_item',
        'method_definition', 'function_declaration', 'generator_function',
        'generator_function_declaration', 'arrow_function',
        'decorated_definition', 'class_declaration', 'export_statement'
    }

    # Find all children in the group that are definition types
    defs = [c for c in group_children if c.type in definition_types]

    if not defs:
        # No definition in the group, fallback to default truncation of the group's lines
        group_lines = lines[start_line:end_line + 1]
        min_lines = list(group_lines[:5])
        if len(group_lines) > 5:
            indent = 0
            if min_lines:
                last_line = min_lines[-1]
                indent = len(last_line) - len(last_line.lstrip())
            indent_str = " " * indent
            min_lines.append(
                indent_str
                + profile.format_omission_comment("Data Structure Omitted")
            )
        return min_lines

    # If there are definitions, we want to semantically truncate each definition
    # inside the group's line range.
    # We sort defs by start_line desc to replace slices from end to start safely.
    defs_sorted = sorted(defs, key=lambda c: c.start_point[0], reverse=True)
    group_lines = list(lines[start_line:end_line + 1])

    for d in defs_sorted:
        d_start = d.start_point[0] - start_line
        d_end = d.end_point[0] - start_line
        truncated_def = _semantically_truncate_child(d, lines, profile)
        group_lines[d_start:d_end + 1] = truncated_def

    return group_lines


def _collect_children_info(tree, lines, profile):
    """Collect full and minimum representation lines for each child node,
    merging overlapping ranges."""
    groups = []
    for child in tree.root_node.children:
        c_start = child.start_point[0]
        c_end = child.end_point[0]

        if groups and c_start <= groups[-1]["end_line"]:
            groups[-1]["end_line"] = max(groups[-1]["end_line"], c_end)
            groups[-1]["children"].append(child)
        else:
            groups.append({
                "start_line": c_start,
                "end_line": c_end,
                "children": [child]
            })

    children_info = []
    for group in groups:
        full_lines = lines[group["start_line"]:group["end_line"] + 1]
        min_lines = _get_group_min_lines(group, lines, profile)

        if len(min_lines) >= len(full_lines):
            min_lines = list(full_lines)

        children_info.append({
            "full_lines": full_lines,
            "min_lines": min_lines
        })
    return children_info


def _build_with_omissions(children_info, max_lines, profile):
    """Build list of lines when total minimum lines exceeds budget, showing omissions."""
    output_lines = []
    # Reserve 1 line for omission comment
    budget = max(0, max_lines - 1)
    for info in children_info:
        min_len = len(info["min_lines"])
        if min_len <= budget:
            output_lines.extend(info["min_lines"])
            budget -= min_len
        else:
            output_lines.extend(info["min_lines"][:budget])
            budget = 0
        if budget <= 0:
            break

    # Determine indentation of the last line in output_lines
    indent = 0
    if output_lines:
        last_line = output_lines[-1]
        indent = len(last_line) - len(last_line.lstrip())

    omission_comment = profile.format_omission_comment(
        "Remaining Methods Omitted"
    )
    output_lines.append(" " * indent + omission_comment)
    return output_lines


def _build_upgraded(children_info, max_lines, total_min_lines):
    """Build list of lines by upgrading signatures to full bodies where budget allows."""
    remaining_budget = max_lines - total_min_lines
    upgraded = [False] * len(children_info)
    for i, info in enumerate(children_info):
        upgrade_cost = len(info["full_lines"]) - len(info["min_lines"])
        if upgrade_cost <= remaining_budget:
            upgraded[i] = True
            remaining_budget -= upgrade_cost

    output_lines = []
    for i, info in enumerate(children_info):
        if upgraded[i]:
            output_lines.extend(info["full_lines"])
        else:
            output_lines.extend(info["min_lines"])
    return output_lines


def _allocate_budget_and_build(children_info, max_lines, profile):
    """Allocate budget and build final pruned line list."""
    total_min_lines = sum(len(info["min_lines"]) for info in children_info)
    if total_min_lines > max_lines:
        return _build_with_omissions(children_info, max_lines, profile)
    return _build_upgraded(children_info, max_lines, total_min_lines)


def split_massive_block_ast(source_text, file_path, max_lines):
    """Truncate and omit large AST definition blocks to preserve context budgets."""
    max_lines = max(1, max_lines)
    lines = source_text.splitlines()
    if len(lines) <= max_lines:
        return [{"suffix": "", "text": source_text}]

    ext = os.path.splitext(file_path)[1].lower()
    profile = get_language_profile(file_path)

    if not AST_ENGINE.is_supported(ext):
        fallback_text = _get_fallback_truncated_text(lines, max_lines, profile)
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    tree = AST_ENGINE.parsers[ext].parse(source_text.encode('utf-8'))
    if tree is None or tree.root_node is None:
        fallback_text = _get_fallback_truncated_text(lines, max_lines, profile)
        return [{"suffix": " (Truncated)", "text": fallback_text}]
    children_info = _collect_children_info(tree, lines, profile)
    if not children_info:
        fallback_text = _get_fallback_truncated_text(lines, max_lines, profile)
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    output_lines = _allocate_budget_and_build(children_info, max_lines, profile)

    return [{"suffix": " (AST Semantically Pruned)", "text": "\n".join(output_lines)}]


def extract_callees_ast(file_path, start_line, end_line, ext, file_cache):  # pylint: disable=too-many-branches
    """Extract all functions/methods called inside a specific line range using tree-sitter AST."""
    ext = ext.lower()
    if tree_sitter is None or not AST_ENGINE.is_supported(ext):
        return set()
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)
    if tree is None or tree.root_node is None:
        return set()

    func_node = None
    stack = list(reversed(tree.root_node.children))
    while stack:
        curr = stack.pop()
        if curr.start_point[0] == start_line:
            func_node = curr
            break
        try:
            should_traverse = curr.start_point[0] <= start_line <= curr.end_point[0]
        except (TypeError, IndexError, AttributeError):
            should_traverse = True
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
        query = tree_sitter.Query(AST_ENGINE.languages[ext], q_str)
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
    except Exception as e:  # pylint: disable=broad-exception-caught
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
        elif parent.type in (
            'member_expression', 'attribute', 'selector_expression',
            'field_access', 'field_expression'
        ):
            field_name = 'property' if parent.type == 'member_expression' else (
                'attribute' if parent.type == 'attribute' else 'field'
            )
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
        tree = AST_ENGINE.parsers[ext].parse(source_bytes)
    except Exception:  # pylint: disable=broad-exception-caught
        return []

    if tree is None or tree.root_node is None:
        return []

    results = []
    line_set = set(line_numbers)
    min_line = min(line_set)
    max_line = max(line_set)
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
            if child_end < min_line or child_start > max_line:
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
        except Exception as e:  # pylint: disable=broad-exception-caught
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
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"\n[SmartDiffContextBuilder Warning] AST identifier extraction failed: {e}. "
                  "Falling back to regex-based identifier extraction.")
    return extract_identifiers_regex(file_path, line_numbers, file_cache)


def get_lhs_identifiers(node):
    """Walk a Tree-sitter declaration or assignment node to find LHS target identifiers.

    Excludes initializers, RHS expressions, and operators.
    """
    ids = []

    stack = [node]
    while stack:
        curr = stack.pop()
        if curr.type == 'identifier':
            text = curr.text
            if isinstance(text, bytes):
                text = text.decode('utf-8', errors='ignore')
            if text:
                ids.append(text)
            continue

        # Check for operator child
        operator_idx = -1
        for idx, child in enumerate(curr.children):
            if child.type in ('=', ':=', '+=', '-=', '*=', '/='):
                operator_idx = idx
                break

        # Check for skip fields
        right_fields = {'right', 'init', 'value'}
        skip_nodes = []
        for field in right_fields:
            child = curr.child_by_field_name(field)
            if child:
                skip_nodes.append(child)

        children_to_push = []
        for idx, child in enumerate(curr.children):
            if operator_idx != -1 and idx >= operator_idx:
                break
            if child not in skip_nodes:
                children_to_push.append(child)

        for child in reversed(children_to_push):
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
        tree = AST_ENGINE.parsers[ext].parse(source_bytes)
    except Exception:  # pylint: disable=broad-exception-caught
        return None, None

    if tree is None or tree.root_node is None:
        return None, None

    profile = get_language_profile(file_path)
    if profile is None:
        return None, None
    captures = []
    try:
        query = tree_sitter.Query(
            AST_ENGINE.languages[ext],
            profile.declaration_query
        )
        captures = query.captures(tree.root_node)
    except Exception:  # pylint: disable=broad-exception-caught
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


def is_line_definition_of_var(cleaned_line, var_name, profile):
    """Check if a cleaned line defines var_name using simple regex heuristics."""
    escaped_var = re.escape(var_name)

    # 1. Assignment
    assign_match = re.search(r'(?<![!=<>])=(?!=)|:=|\+=|-=|\*=|\/=', cleaned_line)
    if assign_match:
        lhs = cleaned_line[:assign_match.start()]
        if re.search(r'\b' + escaped_var + r'\b', lhs):
            return True

    # 2. Explicit keywords
    if re.search(r'\b(?:let|const|var|mut)\s+' + escaped_var + r'\b', cleaned_line):
        return True

    # 3. Type-based declarations (C/C++/Java/Go/Rust)
    # Match a leading type name followed by the variable name, but reject
    # flow-control/statement keywords (e.g. 'return', 'if', 'for') that
    # cannot be type names and would otherwise cause false positives such as
    # treating `return x;` as a definition.
    # We use profile.flow_keywords (not profile.keywords) because the full
    # keyword set also includes primitive type names ('int', 'char', etc.)
    # that are perfectly valid as type declaration prefixes.
    flow_kws = getattr(profile, 'flow_keywords', frozenset())
    type_decl_match = re.search(
        r'\b([A-Za-z_][A-Za-z0-9_<>:,*&]*)\s+' + escaped_var + r'\b',
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
            if re.search(r'\b' + escaped_var + r'\b', params):
                return True

    return False


def get_class_members(file_path, class_name, profile, file_cache):  # pylint: disable=too-many-branches,too-many-statements
    """Extract and cache member variables defined in a class."""
    if file_cache is None:
        file_cache = get_global_cache()

    if profile is None:
        return []

    if not isinstance(getattr(file_cache, "class_members_cache", None), dict):
        file_cache.class_members_cache = {}

    cache_key = (os.path.abspath(file_path), class_name)
    if cache_key in file_cache.class_members_cache:
        return file_cache.class_members_cache[cache_key]

    lines = _get_stripped_lines(file_cache, file_path, profile)
    class_line_num = None
    class_pattern = re.compile(r'\b(?:class|struct)\s+' + re.escape(class_name) + r'\b')
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
        assign_match = re.search(r'(?<![!=<>])=(?!=)|:=|\+=|-=|\*=|\/=', cleaned)
        if assign_match:
            lhs = cleaned[:assign_match.start()]
            for m in re.finditer(r'\b[A-Za-z_][A-Za-z0-9_]*\b', lhs):
                name = m.group(0)
                if name not in profile.keywords:
                    members.append((name, ln))
        else:
            decl_match = re.search(
                r'\b[A-Za-z_][A-Za-z0-9_<>:,*&]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*;', cleaned
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
                    self_match = re.search(r'\bself\.([A-Za-z_][A-Za-z0-9_]*)\s*=', cleaned)
                    if self_match:
                        name = self_match.group(1)
                        members.append((name, ln))

    seen = set()
    unique_members = []
    for name, ln in members:
        if name not in seen:
            seen.add(name)
            unique_members.append((name, ln))

    # Bounded cache to avoid leaks
    if len(file_cache.class_members_cache) >= 1024:
        first_key = next(iter(file_cache.class_members_cache))
        file_cache.class_members_cache.pop(first_key, None)

    file_cache.class_members_cache[cache_key] = unique_members
    return unique_members


def get_parent_classes(file_path, class_name, profile, file_cache):
    """Identify the parent class name(s) for a given class."""
    # pylint: disable=too-many-nested-blocks
    if profile is None:
        return []
    lines = _get_stripped_lines(file_cache, file_path, profile)
    class_pattern = re.compile(r'\b(?:class|struct)\s+' + re.escape(class_name) + r'\b')
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

    if not isinstance(getattr(file_cache, "find_class_definition_cache", None), dict):
        file_cache.find_class_definition_cache = {}

    cache_key = (os.path.abspath(start_file), class_name)
    if cache_key in file_cache.find_class_definition_cache:
        return file_cache.find_class_definition_cache[cache_key]

    def _find():  # pylint: disable=too-many-branches
        if profile is None:
            return None, None
        lines = _get_stripped_lines(file_cache, start_file, profile)
        class_pattern = re.compile(r'\b(?:class|struct)\s+' + re.escape(class_name) + r'\b')
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
                lines = _get_stripped_lines(file_cache, inc_file, inc_profile)
                for idx, line in enumerate(lines):
                    cleaned = inc_profile.strip_strings_and_comments(line)
                    if class_pattern.search(cleaned):
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
                lines = _get_stripped_lines(file_cache, f, f_profile)
                for idx, line in enumerate(lines):
                    cleaned = f_profile.strip_strings_and_comments(line)
                    if class_pattern.search(cleaned):
                        return f, idx + 1

        return None, None

    res = _find()
    if len(file_cache.find_class_definition_cache) >= 1024:
        first_key = next(iter(file_cache.find_class_definition_cache))
        file_cache.find_class_definition_cache.pop(first_key, None)
    file_cache.find_class_definition_cache[cache_key] = res
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


def get_directly_included_files(file_path, profile, file_cache):  # pylint: disable=too-many-branches,too-many-statements
    """Find files directly imported or included in the source file."""
    if file_cache is None:
        file_cache = get_global_cache()

    if not isinstance(getattr(file_cache, "get_directly_included_files_cache", None), dict):
        file_cache.get_directly_included_files_cache = {}

    cache_key = os.path.abspath(file_path)
    if cache_key in file_cache.get_directly_included_files_cache:
        return file_cache.get_directly_included_files_cache[cache_key]

    if profile is None:
        return []

    def _get_files():  # pylint: disable=too-many-branches,too-many-statements
        lines = _get_stripped_lines(file_cache, file_path, profile)
        includes = []
        curr_dir = os.path.dirname(file_path)

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
                m = re.match(r'^import\s+(?:[A-Za-z0-9_.]+\s+)?["\']([^"\']+)["\']', cleaned)
                if m:
                    includes.append(m.group(1))
            elif profile.name == 'rust':
                m = re.match(r'^use\s+([A-Za-z0-9_:]+)', cleaned)
                if m:
                    parts = m.group(1).split('::')[0]
                    includes.append(parts)

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
    if len(file_cache.get_directly_included_files_cache) >= 1024:
        first_key = next(iter(file_cache.get_directly_included_files_cache))
        file_cache.get_directly_included_files_cache.pop(first_key, None)
    file_cache.get_directly_included_files_cache[cache_key] = res
    return res


def resolve_global_definition(
    file_path, var_name, profile, file_cache, searched_files=None
):  # pylint: disable=too-many-statements
    """Search globally for var_name in current file, imports, and repo files."""
    if file_cache is None:
        file_cache = get_global_cache()

    if not isinstance(getattr(file_cache, "resolve_global_definition_cache", None), dict):
        file_cache.resolve_global_definition_cache = {}

    cache_key = (os.path.abspath(file_path), var_name)
    if cache_key in file_cache.resolve_global_definition_cache:
        return file_cache.resolve_global_definition_cache[cache_key]

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
    if len(file_cache.resolve_global_definition_cache) >= 1024:
        first_key = next(iter(file_cache.resolve_global_definition_cache))
        file_cache.resolve_global_definition_cache.pop(first_key, None)
    file_cache.resolve_global_definition_cache[cache_key] = res
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
