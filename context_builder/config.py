"""
Configuration settings and default language definitions.
"""

import json
from collections.abc import MutableMapping

# Defaults
DEFAULT_LANG_MAP = {
    '.rs': 'rust', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
    '.cjs': 'javascript', '.ts': 'typescript', '.tsx': 'typescript',
    '.mts': 'typescript', '.cts': 'typescript', '.py': 'python',
    '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp',
    '.hpp': 'cpp', '.hxx': 'cpp', '.c': 'c', '.h': 'cpp',
    '.go': 'go', '.pl': 'perl', '.java': 'java',
    '.mk': 'makefile', '.cmake': 'cmake', '.sh': 'bash', '.bat': 'batch'
}

DEFAULT_BINDINGS = {
    '.py': ('tree_sitter_python', 'language'),
    '.rs': ('tree_sitter_rust', 'language'),
    '.js': ('tree_sitter_javascript', 'language'),
    '.jsx': ('tree_sitter_javascript', 'language'),
    '.mjs': ('tree_sitter_javascript', 'language'),
    '.cjs': ('tree_sitter_javascript', 'language'),
    '.ts': ('tree_sitter_typescript', 'language_typescript'),
    '.tsx': ('tree_sitter_typescript', 'language_tsx'),
    '.mts': ('tree_sitter_typescript', 'language_typescript'),
    '.cts': ('tree_sitter_typescript', 'language_typescript'),
    '.c':  ('tree_sitter_c', 'language'),
    '.cc': ('tree_sitter_cpp', 'language'),
    '.cpp': ('tree_sitter_cpp', 'language'),
    '.cxx': ('tree_sitter_cpp', 'language'),
    '.hpp': ('tree_sitter_cpp', 'language'),
    '.hxx': ('tree_sitter_cpp', 'language'),
    '.h':   ('tree_sitter_cpp', 'language'),
    '.java': ('tree_sitter_java', 'language')
}

DEFAULT_DEPENDENCY_QUERY_STRINGS = {
    '.py': (
        '(call function: [(identifier) @id '
        '(attribute attribute: (identifier) @id)] '
        '(#match? @id ".*({escaped_func_name}|register).*"))'
    ),
    '.rs': (
        '(call_expression function: [(identifier) @id '
        '(scoped_identifier name: (identifier) @id) '
        '(field_expression field: (field_identifier) @id)] '
        '(#match? @id ".*({escaped_func_name}|register).*"))'
    ),
    '.js': (
        '(call_expression function: [(identifier) @id '
        '(member_expression property: (property_identifier) @id)] '
        '(#match? @id ".*({escaped_func_name}|register).*"))'
    ),
    '.ts': (
        '(call_expression function: [(identifier) @id '
        '(member_expression property: (property_identifier) @id)] '
        '(#match? @id ".*({escaped_func_name}|register).*"))'
    ),
    '.c': (
        '(call_expression function: (identifier) @id '
        '(#match? @id ".*({escaped_func_name}|register).*"))'
    ),
    '.cpp': (
        '(call_expression function: [(identifier) @id '
        '(scoped_identifier name: (identifier) @id) '
        '(field_expression field: (field_identifier) @id)] '
        '(#match? @id ".*({escaped_func_name}|register).*"))'
    ),
    '.java': (
        '[(method_invocation name: (identifier) @id) '
        '(method_reference (identifier) @id)] '
        '(#match? @id ".*({escaped_func_name}|register).*")'
    )
}

DEFAULT_CALLEE_QUERY_STRINGS = {
    '.py': (
        '(call function: [(identifier) @id '
        '(attribute attribute: (identifier) @id)])'
    ),
    '.rs': (
        '(call_expression function: [(identifier) @id '
        '(scoped_identifier name: (identifier) @id) '
        '(field_expression field: (field_identifier) @id)])'
    ),
    '.js': (
        '(call_expression function: [(identifier) @id '
        '(member_expression property: (property_identifier) @id)])'
    ),
    '.ts': (
        '(call_expression function: [(identifier) @id '
        '(member_expression property: (property_identifier) @id)])'
    ),
    '.c': (
        '(call_expression function: (identifier) @id)'
    ),
    '.cpp': (
        '(call_expression function: [(identifier) @id '
        '(scoped_identifier name: (identifier) @id) '
        '(field_expression field: (field_identifier) @id)])'
    ),
    '.java': (
        '[(method_invocation name: (identifier) @id) '
        '(method_reference (identifier) @id)]'
    )
}

for _cpp_extension in ('.cc', '.cxx', '.hpp', '.hxx', '.h'):
    DEFAULT_DEPENDENCY_QUERY_STRINGS[_cpp_extension] = (
        DEFAULT_DEPENDENCY_QUERY_STRINGS['.cpp']
    )
    DEFAULT_CALLEE_QUERY_STRINGS[_cpp_extension] = (
        DEFAULT_CALLEE_QUERY_STRINGS['.cpp']
    )

for _js_extension in ('.jsx', '.mjs', '.cjs'):
    DEFAULT_DEPENDENCY_QUERY_STRINGS[_js_extension] = (
        DEFAULT_DEPENDENCY_QUERY_STRINGS['.js']
    )
    DEFAULT_CALLEE_QUERY_STRINGS[_js_extension] = (
        DEFAULT_CALLEE_QUERY_STRINGS['.js']
    )

for _ts_extension in ('.tsx', '.mts', '.cts'):
    DEFAULT_DEPENDENCY_QUERY_STRINGS[_ts_extension] = (
        DEFAULT_DEPENDENCY_QUERY_STRINGS['.ts']
    )
    DEFAULT_CALLEE_QUERY_STRINGS[_ts_extension] = (
        DEFAULT_CALLEE_QUERY_STRINGS['.ts']
    )

DEFAULT_FUNC_DECL_PATTERN = (
    r'\b(fn|function|def|sub|func|class|macro)\b|'
    r'^\s*[A-Za-z0-9_<>:&*~]+\s+[A-Za-z0-9_<>:&*~]+'
    r'(?:\s+[A-Za-z0-9_<>:&*~]+)*\s*\('
)

DEFAULT_DEF_PATTERN_TEMPLATE = (
    r"\b(?:fn|def|function|sub|func|class|macro)\s+{lead_b}{escaped_callee}{trail_b}"
)
DEFAULT_CPP_DEF_PATTERN_TEMPLATE = (
    r"^\s*(?:[A-Za-z0-9_<>:]+(?:\s+\*?\s*)*)?{lead_b}{escaped_callee}\s*\("
)

DEFAULT_CALLEE_PATTERN = r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\('
DEFAULT_CALLEE_IGNORED_KEYWORDS = [
    'if', 'while', 'for', 'with', 'print', 'len', 'fn', 'def', 'function', 'class'
]

DEFAULT_FFI_PATTERNS = [
    r'#\[(?:no_mangle|wasm_bindgen)\].*?(?:fn|static)\s+([A-Za-z0-9_]+)',
    r'(?:extern\s+"C"|EMSCRIPTEN_KEEPALIVE).*?\b([A-Za-z_][A-Za-z0-9_]*)\s*\(',
    r'm\.def\(\s*"([^"]+)"'
]
DEFAULT_FFI_RG_PATTERN = (
    "no_mangle|wasm_bindgen|extern \"C\"|EMSCRIPTEN_KEEPALIVE|PYBIND11_MODULE|m.def"
)
DEFAULT_PATH_CASE_RULES = []
DEFAULT_GIT_TIMEOUT = 30.0
DEFAULT_GIT_PROBE_TIMEOUT = 5.0
DEFAULT_LSP_INIT_TIMEOUT = 60.0
DEFAULT_LSP_QUERY_TIMEOUT = 150.0
WORKTREE_LSP_INIT_TIMEOUT = 120.0
WORKTREE_LSP_QUERY_TIMEOUT = 300.0

# Global Configuration Dictionary
CONFIG = {}


def reset_config():
    """Resets the global configuration dictionary to its default state."""
    CONFIG.clear()
    CONFIG.update({
        # CLI parameters
        'format': 'md',
        'max_lines': 1500,
        'max_mb': 2.0,
        'base_name': 'SmartDiffContextBuilder',
        'max_cache_size_mb': 200.0,
        'max_interface_depth': 15,
        'disable_pruning': False,
        'lsp_init_timeout': DEFAULT_LSP_INIT_TIMEOUT,
        'lsp_timeout': DEFAULT_LSP_QUERY_TIMEOUT,
        'ripgrep_timeout': 10.0,
        'git_timeout': DEFAULT_GIT_TIMEOUT,
        'git_probe_timeout': DEFAULT_GIT_PROBE_TIMEOUT,
        'no_language_server': False,
        'compare': False,
        'skip_ffi': False,
        'skip_macro_expansion': False,
        'path_case_rules': DEFAULT_PATH_CASE_RULES.copy(),
        'caller_depth': 1,
        'callee_depth': 1,
        'commit_range': None,
        'build_directories': [
            "build", "out", "target", "cmake-build-debug", "cmake-build-release"
        ],

        # Externalized language mappings & queries
        'lang_map': DEFAULT_LANG_MAP.copy(),
        'bindings': DEFAULT_BINDINGS.copy(),
        'dependency_query_strings': DEFAULT_DEPENDENCY_QUERY_STRINGS.copy(),
        'callee_query_strings': DEFAULT_CALLEE_QUERY_STRINGS.copy(),

        # Regex configurations
        'func_decl_pattern': DEFAULT_FUNC_DECL_PATTERN,
        'def_pattern_template': DEFAULT_DEF_PATTERN_TEMPLATE,
        'cpp_def_pattern_template': DEFAULT_CPP_DEF_PATTERN_TEMPLATE,
        'callee_pattern': DEFAULT_CALLEE_PATTERN,
        'callee_ignored_keywords': DEFAULT_CALLEE_IGNORED_KEYWORDS.copy(),

        # FFI configurations
        'ffi_patterns': DEFAULT_FFI_PATTERNS.copy(),
        'ffi_rg_pattern': DEFAULT_FFI_RG_PATTERN,
    })
# Initialize configuration
reset_config()


def load_json_with_comments(filepath):
    """Loads a JSON file, stripping single-line comments prefixed with '//' or '#'."""
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('#'):
            continue
        clean_lines.append(line)

    return json.loads("".join(clean_lines))


def generate_commented_config(active_options):
    """Generates a JSONC string where only options specified in active_options are uncommented."""
    lines = ["{"]
    groups = {
        "General Settings": [
            'format', 'max_lines', 'max_mb', 'base_name', 'max_cache_size_mb',
            'max_interface_depth', 'disable_pruning', 'lsp_init_timeout',
            'lsp_timeout', 'ripgrep_timeout', 'git_timeout',
            'git_probe_timeout', 'no_language_server', 'compare',
            'skip_ffi', 'skip_macro_expansion', 'path_case_rules',
            'caller_depth', 'callee_depth', 'commit_range', 'build_directories'
        ],
        "Language Definitions": [
            'lang_map', 'bindings', 'dependency_query_strings', 'callee_query_strings'
        ],
        "Regex Logic": [
            'func_decl_pattern', 'def_pattern_template', 'cpp_def_pattern_template',
            'callee_pattern', 'callee_ignored_keywords'
        ],
        "FFI Registry Settings": [
            'ffi_patterns', 'ffi_rg_pattern'
        ]
    }

    for g_idx, (group_name, keys) in enumerate(groups.items()):
        lines.append(f"  // === {group_name} ===")
        for k_idx, key in enumerate(keys):
            default_val = CONFIG[key]
            is_active = key in active_options

            # Serialize the value nicely
            val_str = json.dumps(default_val, indent=4)
            if "\n" in val_str:
                val_str = val_str.replace("\n", "\n  ")
                if not is_active:
                    val_str = val_str.replace("\n  ", "\n  // ")

            # Add comma if not the very last item of the very last group
            is_last = (g_idx == len(groups) - 1) and (k_idx == len(keys) - 1)
            comma = "," if not is_last else ""

            if is_active:
                lines.append(f"  \"{key}\": {val_str}{comma}")
            else:
                lines.append(f"  // \"{key}\": {val_str}{comma}")
        lines.append("")

    if lines[-1] == "":
        lines.pop()
    lines.append("}")
    return "\n".join(lines)


class ConfigDictProxy(MutableMapping):
    """A dictionary-like proxy that routes queries dynamically to active global config."""

    def __init__(self, key):
        """Initialize proxy with the target config key."""
        self._key = key

    def _get_dict(self):
        """Retrieve the current dictionary reference from CONFIG."""
        return CONFIG[self._key]

    def __getitem__(self, item):
        """Get item from the active dictionary."""
        return self._get_dict()[item]

    def __setitem__(self, key, value):
        """Set item in the active dictionary."""
        self._get_dict()[key] = value

    def __delitem__(self, key):
        """Delete item from the active dictionary."""
        del self._get_dict()[key]

    def __iter__(self):
        """Iterate over the keys of the active dictionary."""
        return iter(self._get_dict())

    def __len__(self):
        """Return the length of the active dictionary."""
        return len(self._get_dict())

    def __repr__(self):
        """Return representation of the active dictionary."""
        return repr(self._get_dict())
