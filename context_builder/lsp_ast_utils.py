"""AST helpers used by LSP query positioning."""

import sys

try:
    import tree_sitter
    TreeSitterQueryError = tree_sitter.QueryError
except ImportError:
    tree_sitter = None
    TreeSitterQueryError = ValueError


def _get_ast_engine():
    """Return the already-loaded shared AST engine without creating an import cycle."""
    ast_engine_module = sys.modules.get("context_builder.ast_engine")
    return getattr(ast_engine_module, "AST_ENGINE", None)


# pylint: disable=too-many-branches
def find_lsp_func_start_character_ast(
    lines, line_num, func_name, ext, file_path, file_cache, decorator_lookahead
):
    """Attempt to locate function identifier starting character index using AST parsing."""
    ext = ext.lower()
    ast_engine = _get_ast_engine()
    if tree_sitter is None or ast_engine is None or not ast_engine.is_supported(ext):
        return -1, line_num

    try:
        source_bytes = file_cache.get_bytes(file_path)
        if not source_bytes:
            return -1, line_num
        tree = ast_engine.parse(ext, source_bytes)
        if not tree or not hasattr(tree, "root_node"):
            return -1, line_num
        q_str = None
        if ext in (".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".h", ".c"):
            q_str = """
            (function_declarator
              declarator: [
                (identifier) @func_name
                (field_identifier) @func_name
                (destructor_name) @func_name
                (qualified_identifier
                  name: [
                    (identifier) @func_name
                    (field_identifier) @func_name
                    (destructor_name) @func_name
                  ]
                )
              ]
            )
            """
        elif ext == ".rs":
            q_str = """
            (function_item
              name: (identifier) @func_name
            )
            (function_signature_item
              name: (identifier) @func_name
            )
            """
        if not q_str:
            return -1, line_num

        query = ast_engine.get_query(ext, q_str)
        if hasattr(query, "captures"):
            captures = query.captures(tree.root_node)
        else:
            cursor = tree_sitter.QueryCursor(query)
            res = cursor.captures(tree.root_node)
            if isinstance(res, dict):
                captures_list = []
                for name, nodes in res.items():
                    for n in nodes:
                        captures_list.append((n, name))
                captures = captures_list
            else:
                captures = res
        for capture_node, _ in captures:
            node_text = source_bytes[
                capture_node.start_byte:capture_node.end_byte
            ].decode("utf-8", errors="ignore")
            if node_text != func_name:
                continue
            node_row = capture_node.start_point[0]
            search_start = line_num - 1
            search_end = search_start + decorator_lookahead
            if node_row < search_start or node_row >= search_end:
                continue
            if node_row < 0 or node_row >= len(lines):
                continue
            line_str = lines[node_row]
            prefix_bytes = line_str.encode("utf-8")[:capture_node.start_point[1]]
            prefix_str = prefix_bytes.decode("utf-8", errors="ignore")
            char_idx = len(prefix_str.encode("utf-16-le")) // 2
            actual_line = node_row + 1
            return char_idx, actual_line
    except (RuntimeError, ValueError, TreeSitterQueryError):
        pass
    return -1, line_num
