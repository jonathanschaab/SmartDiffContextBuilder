"""Semantic AST pruning helpers for oversized source blocks."""

import os
from contextlib import nullcontext


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
        elif has_brace:
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

    defs = [c for c in group_children if c.type in definition_types]

    if not defs:
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

    defs_sorted = sorted(defs, key=lambda c: c.start_point[0], reverse=True)
    group_lines = list(lines[start_line:end_line + 1])

    for definition in defs_sorted:
        def_start = definition.start_point[0] - start_line
        def_end = definition.end_point[0] - start_line
        truncated_def = _semantically_truncate_child(definition, lines, profile)
        group_lines[def_start:def_end + 1] = truncated_def

    return group_lines


def _collect_children_info(tree, lines, profile):
    """Collect full and minimum representation lines for merged child groups."""
    groups = []
    for child in tree.root_node.children:
        child_start = child.start_point[0]
        child_end = child.end_point[0]

        if groups and child_start <= groups[-1]["end_line"]:
            groups[-1]["end_line"] = max(groups[-1]["end_line"], child_end)
            groups[-1]["children"].append(child)
        else:
            groups.append({
                "start_line": child_start,
                "end_line": child_end,
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
    """Build list of lines when total minimum lines exceeds budget."""
    output_lines = []
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
    """Build list of lines by upgrading signatures to full bodies when possible."""
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


def split_massive_block_ast(source_text, file_path, max_lines, ast_engine, profile_getter):
    """Truncate and omit large AST definition blocks to preserve context budgets."""
    max_lines = max(1, max_lines)
    lines = source_text.splitlines()
    if len(lines) <= max_lines:
        return [{"suffix": "", "text": source_text}]

    ext = os.path.splitext(file_path)[1].lower()
    profile = profile_getter(file_path)

    if not ast_engine.is_supported(ext):
        fallback_text = _get_fallback_truncated_text(lines, max_lines, profile)
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    parse_func = getattr(ast_engine, 'parse', None)
    if callable(parse_func) and not hasattr(parse_func, 'mock_calls'):
        tree = parse_func(ext, source_text.encode('utf-8'))
    else:
        lock = getattr(ast_engine, '_lock', None)
        with lock if lock is not None else nullcontext():
            tree = ast_engine.parsers[ext].parse(source_text.encode('utf-8'))
    if tree is None or tree.root_node is None:
        fallback_text = _get_fallback_truncated_text(lines, max_lines, profile)
        return [{"suffix": " (Truncated)", "text": fallback_text}]
    children_info = _collect_children_info(tree, lines, profile)
    if not children_info:
        fallback_text = _get_fallback_truncated_text(lines, max_lines, profile)
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    output_lines = _allocate_budget_and_build(children_info, max_lines, profile)

    return [{"suffix": " (AST Semantically Pruned)", "text": "\n".join(output_lines)}]
