"""
AST analysis engine utilizing tree-sitter or regex fallback.
Provides syntax-aware function boundary extraction, dependency tracing,
and callee analysis.
"""

import os
import re
import importlib
from .sys_utils import iter_scan_progress, warn_once, ripgrep_filter
from .cache import get_global_cache
from .languages import UNKNOWN_LANGUAGE, get_language_profile

try:
    import tree_sitter
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False

from .config import CONFIG, ConfigDictProxy

LANG_MAP = ConfigDictProxy('lang_map')

_FUNC_DECL_RE = None
_FUNC_DECL_STR = None
_CALLEE_RE = None
_CALLEE_STR = None


def _get_func_decl_pattern():
    """Retrieve or compile the cached regex for function declarations."""
    global _FUNC_DECL_RE, _FUNC_DECL_STR  # pylint: disable=global-statement
    current_str = CONFIG['func_decl_pattern']
    if _FUNC_DECL_RE is None or _FUNC_DECL_STR != current_str:
        _FUNC_DECL_RE = re.compile(current_str, re.MULTILINE)
        _FUNC_DECL_STR = current_str
    return _FUNC_DECL_RE


def _get_callee_pattern():
    """Retrieve or compile the cached regex for callee matching."""
    global _CALLEE_RE, _CALLEE_STR  # pylint: disable=global-statement
    current_str = CONFIG['callee_pattern']
    if _CALLEE_RE is None or _CALLEE_STR != current_str:
        _CALLEE_RE = re.compile(current_str)
        _CALLEE_STR = current_str
    return _CALLEE_RE


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
        return ext in self.parsers


AST_ENGINE = AstEngine()


def strip_strings_and_comments(line, is_python=False):
    """Compatibility wrapper around language-profile comment stripping."""
    profile = get_language_profile(".py") if is_python else UNKNOWN_LANGUAGE
    return profile.strip_strings_and_comments(line)


def extract_function_bounds_ast(file_path, line_num, ext, file_cache=None):
    """Extract 0-indexed start and end line bounds using tree-sitter AST nodes."""
    if file_cache is None:
        file_cache = get_global_cache()
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)
    target_row = line_num - 1

    def walk(node):
        found = None
        for child in node.children:
            if child.start_point[0] <= target_row <= child.end_point[0]:
                found = walk(child) or child
        return found

    target_node = walk(tree.root_node)
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


def _extract_bounds_non_py_regex(lines, start_idx, target_idx):
    """Fallback bounds extraction for non-Python using bracket counting."""
    end_idx = target_idx
    bracket_count, has_opened = 0, False
    for i in range(start_idx, len(lines)):
        clean_line = strip_strings_and_comments(lines[i])
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
    if file_cache is None:
        file_cache = get_global_cache()
    lines = file_cache.get_lines(file_path)
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

    if get_language_profile(file_path).uses_indentation_blocks:
        return _extract_bounds_py_regex(lines, start_idx)
    return _extract_bounds_non_py_regex(lines, start_idx, target_idx)


def extract_function_bounds(file_path, line_num, file_cache=None):
    """Extract start and end line bounds, preferring AST first and falling back to regex."""
    if line_num <= 0:
        return None, None
    ext = os.path.splitext(file_path)[1]
    if AST_ENGINE.is_supported(ext):
        ast_bounds = extract_function_bounds_ast(file_path, line_num, ext, file_cache=file_cache)
        if ast_bounds[0] is not None:
            return ast_bounds
    return extract_function_bounds_regex(file_path, line_num, file_cache=file_cache)


def _trace_file_ast_dependencies(file_path, func_name, file_cache, callers):
    """Process a single file for AST dependency tracking."""
    ext = os.path.splitext(file_path)[1]
    if get_language_profile(ext).name == "python":
        content = file_cache.get_content(file_path)
        if "typing" not in content:
            warn_once(
                "python_typing",
                "Python files found without 'typing' protocols. "
                "Dynamic dispatch tracking relies on type hinting for accuracy."
            )

    if not AST_ENGINE.is_supported(ext):
        return
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)

    escaped_func_name = re.escape(func_name).replace("\\", "\\\\")

    query_strings = CONFIG['dependency_query_strings']
    q_str = query_strings.get(ext)
    if not q_str:
        return

    q_str = q_str.replace("{escaped_func_name}", escaped_func_name)

    try:
        query = AST_ENGINE.languages[ext].query(q_str)
        captures = query.captures(tree.root_node)
        lines = file_cache.get_lines(file_path)

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
    file_path, content, ext, call_pattern, def_keyword_pattern, def_cpp_pattern, callers
):
    """Search regex patterns within a single file."""
    if call_pattern.search(content):
        profile = get_language_profile(ext)
        for idx, line in enumerate(content.splitlines()):
            clean_line = profile.strip_strings_and_comments(line)
            if call_pattern.search(clean_line):
                is_def = False
                if def_keyword_pattern.search(clean_line):
                    is_def = True
                elif (
                    profile.uses_c_style_definitions
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

    lead_b = r'\b' if func_name[0].isalnum() or func_name[0] == '_' else ''
    trail_b = r'\b' if func_name[-1].isalnum() or func_name[-1] == '_' else ''
    escaped_name = re.escape(func_name)

    call_pattern = re.compile(lead_b + escaped_name + trail_b)
    def_keyword_pattern = re.compile(
        r'\b(?:fn|def|function|sub|func|class|macro)\s+' + lead_b + escaped_name + trail_b
    )
    def_cpp_pattern = re.compile(
        r'^\s*(?:[A-Za-z0-9_<>:]+(?:\s+\*?\s*)*)?' + lead_b + escaped_name + r'\s*\('
    )
    for file_path in iter_scan_progress(
        fast_files,
        label=f"Scanning callers of '{func_name}' (regex pass)",
        min_files=100,
    ):
        ext = os.path.splitext(file_path)[1]
        if ext not in LANG_MAP or file_path.endswith('.md'):
            continue
        content = file_cache.get_content(file_path)
        _process_regex_file(
            file_path, content, ext, call_pattern, def_keyword_pattern, def_cpp_pattern, callers
        )
    return callers


def _semantically_truncate_child(child, lines, is_python):
    """Perform semantic truncation of an AST node."""
    sig_lines = []
    end_idx = min(child.end_point[0], len(lines) - 1)
    has_brace = False
    for idx in range(child.start_point[0], end_idx + 1):
        line = lines[idx]
        sig_lines.append(line)
        clean_line = strip_strings_and_comments(line, is_python=is_python)
        if not is_python and "{" in clean_line:
            has_brace = True
            break
        if is_python and clean_line.rstrip().endswith(":"):
            break

    truncated_lines = list(sig_lines)
    if sig_lines:
        indent = len(sig_lines[0]) - len(sig_lines[0].lstrip())
        if is_python:
            truncated_lines.append(
                " " * (indent + 4) +
                "# ... [Inner Body Omitted for Context Preservation] ..."
            )
            truncated_lines.append(" " * (indent + 4) + "pass")
        else:
            if has_brace:
                truncated_lines.append(
                    " " * (indent + 4) +
                    "/* ... [Inner Body Omitted for Context Preservation] ... */"
                )
                truncated_lines.append(" " * indent + "}")
    return truncated_lines


def _get_fallback_truncated_text(lines, max_lines, is_python):
    """Get fallback plain truncation when AST parsing is not supported."""
    if is_python:
        return "\n".join(lines[:max_lines]) + "\n# ... [Lines Omitted due to size] ..."
    return "\n".join(lines[:max_lines]) + "\n/* ... [Lines Omitted due to size] ... */"


def _get_group_min_lines(group, lines, is_python):
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
            if is_python:
                min_lines.append(indent_str + "# ... [Data Structure Omitted] ...")
            else:
                min_lines.append(indent_str + "/* ... [Data Structure Omitted] ... */")
        return min_lines

    # If there are definitions, we want to semantically truncate each definition
    # inside the group's line range.
    # We sort defs by start_line desc to replace slices from end to start safely.
    defs_sorted = sorted(defs, key=lambda c: c.start_point[0], reverse=True)
    group_lines = list(lines[start_line:end_line + 1])

    for d in defs_sorted:
        d_start = d.start_point[0] - start_line
        d_end = d.end_point[0] - start_line
        truncated_def = _semantically_truncate_child(d, lines, is_python)
        group_lines[d_start:d_end + 1] = truncated_def

    return group_lines


def _collect_children_info(tree, lines, is_python):
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
        min_lines = _get_group_min_lines(group, lines, is_python)

        if len(min_lines) >= len(full_lines):
            min_lines = list(full_lines)

        children_info.append({
            "full_lines": full_lines,
            "min_lines": min_lines
        })
    return children_info


def _build_with_omissions(children_info, max_lines, is_python):
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

    omission_comment = (
        "# ... [Remaining Methods Omitted] ..."
        if is_python
        else "/* ... [Remaining Methods Omitted] ... */"
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


def _allocate_budget_and_build(children_info, max_lines, is_python):
    """Allocate budget and build final pruned line list."""
    total_min_lines = sum(len(info["min_lines"]) for info in children_info)
    if total_min_lines > max_lines:
        return _build_with_omissions(children_info, max_lines, is_python)
    return _build_upgraded(children_info, max_lines, total_min_lines)


def split_massive_block_ast(source_text, file_path, max_lines):
    """Truncate and omit large AST definition blocks to preserve context budgets."""
    max_lines = max(1, max_lines)
    lines = source_text.splitlines()
    if len(lines) <= max_lines:
        return [{"suffix": "", "text": source_text}]

    ext = os.path.splitext(file_path)[1]
    is_python = get_language_profile(ext).uses_indentation_blocks

    if not AST_ENGINE.is_supported(ext):
        fallback_text = _get_fallback_truncated_text(lines, max_lines, is_python)
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    tree = AST_ENGINE.parsers[ext].parse(source_text.encode('utf-8'))
    children_info = _collect_children_info(tree, lines, is_python)
    if not children_info:
        fallback_text = _get_fallback_truncated_text(lines, max_lines, is_python)
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    output_lines = _allocate_budget_and_build(children_info, max_lines, is_python)

    return [{"suffix": " (AST Semantically Pruned)", "text": "\n".join(output_lines)}]


def extract_callees_ast(file_path, start_line, end_line, ext, file_cache):
    """Extract all functions/methods called inside a specific line range using tree-sitter AST."""
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)

    def walk(node):
        for child in node.children:
            if child.start_point[0] == start_line:
                return child
            found = walk(child)
            if found:
                return found
        return None
    func_node = walk(tree.root_node) or tree.root_node

    query_strings = CONFIG['callee_query_strings']
    q_str = query_strings.get(ext)
    if not q_str:
        return set()

    callees = set()
    try:
        query = AST_ENGINE.languages[ext].query(q_str)
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
    lines = file_cache.get_lines(file_path)[start_line:end_line]
    callees = set()
    profile = get_language_profile(file_path)
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
    ext = os.path.splitext(file_path)[1]
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

    lead_b = r'\b' if callee_name[0].isalnum() or callee_name[0] == '_' else ''
    trail_b = r'\b' if callee_name[-1].isalnum() or callee_name[-1] == '_' else ''
    escaped_callee = re.escape(callee_name)

    # Precise patterns for definitions
    pattern = CONFIG['def_pattern_template'].replace(
        "{lead_b}", lead_b
    ).replace(
        "{escaped_callee}", escaped_callee
    ).replace(
        "{trail_b}", trail_b
    )
    cpp_pattern = CONFIG['cpp_def_pattern_template'].replace(
        "{lead_b}", lead_b
    ).replace(
        "{escaped_callee}", escaped_callee
    ).replace(
        "{trail_b}", trail_b
    )

    for file_path in iter_scan_progress(
        candidate_files,
        label=f"Scanning definition of '{callee_name}'",
        min_files=100,
    ):
        ext = os.path.splitext(file_path)[1]
        if ext not in LANG_MAP:
            continue

        lines = file_cache.get_lines(file_path)
        profile = get_language_profile(ext)
        for idx, line in enumerate(lines):
            clean_line = profile.strip_strings_and_comments(line)
            is_match = re.search(pattern, clean_line) or (
                profile.uses_c_style_definitions and
                re.search(cpp_pattern, clean_line) and
                not clean_line.strip().endswith(';')
            )
            if is_match:
                return file_path, idx + 1
    return None, None
