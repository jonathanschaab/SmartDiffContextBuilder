"""AST helpers used by LSP query positioning."""

import importlib
import threading

from .config import CONFIG

try:
    import tree_sitter
    TreeSitterQueryError = tree_sitter.QueryError
except ImportError:
    tree_sitter = None
    TreeSitterQueryError = ValueError

TREE_SITTER_BINDING_ERRORS = (
    ImportError,
    AttributeError,
    TypeError,
    RuntimeError,
    ValueError,
)

_PARSERS = {}
_LANGUAGES = {}
_MISSING_BINDINGS = set()
_LOCK = threading.RLock()


def _get_parser_and_language(ext):
    """Return cached tree-sitter parser/language objects for ext."""
    ext = ext.lower()
    with _LOCK:
        if tree_sitter is None:
            return None, None
        if ext in _PARSERS:
            return _PARSERS[ext], _LANGUAGES[ext]
        if ext in _MISSING_BINDINGS:
            return None, None

        binding_info = CONFIG['bindings'].get(ext)
        if not isinstance(binding_info, (list, tuple)) or len(binding_info) != 2:
            _MISSING_BINDINGS.add(ext)
            return None, None

        module_name, func_name = binding_info
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
        except TREE_SITTER_BINDING_ERRORS:
            _MISSING_BINDINGS.add(ext)
            return None, None

        _LANGUAGES[ext] = lang_obj
        _PARSERS[ext] = parser
        return parser, lang_obj


def _parse_with_cached_parser(parser, source_bytes):
    """Parse with a shared LSP positioning parser under the module lock."""
    with _LOCK:
        return parser.parse(source_bytes)


def find_lsp_func_start_character_ast(
    lines, line_num, func_name, ext, file_path, file_cache, decorator_lookahead
):
    """Attempt to locate function identifier starting character index using AST parsing."""
    ext = ext.lower()
    parser, language = _get_parser_and_language(ext)
    if parser is None or language is None:
        return -1, line_num

    try:
        source_bytes = file_cache.get_bytes(file_path)
        if not source_bytes:
            return -1, line_num
        tree = _parse_with_cached_parser(parser, source_bytes)
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

        query = tree_sitter.Query(language, q_str)
        captures = query.captures(tree.root_node)
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
            line_str = lines[node_row]
            prefix_bytes = line_str.encode("utf-8")[:capture_node.start_point[1]]
            prefix_str = prefix_bytes.decode("utf-8", errors="ignore")
            char_idx = len(prefix_str.encode("utf-16-le")) // 2
            actual_line = node_row + 1
            return char_idx, actual_line
    except (RuntimeError, ValueError, TreeSitterQueryError):
        pass
    return -1, line_num
