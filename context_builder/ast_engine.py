import os
import re
from .sys_utils import warn_once, run_command, ripgrep_filter, HAS_RG
from .cache import get_global_cache

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
    global _FUNC_DECL_RE, _FUNC_DECL_STR
    current_str = CONFIG['func_decl_pattern']
    if _FUNC_DECL_RE is None or _FUNC_DECL_STR != current_str:
        _FUNC_DECL_RE = re.compile(current_str, re.MULTILINE)
        _FUNC_DECL_STR = current_str
    return _FUNC_DECL_RE

def _get_callee_pattern():
    global _CALLEE_RE, _CALLEE_STR
    current_str = CONFIG['callee_pattern']
    if _CALLEE_RE is None or _CALLEE_STR != current_str:
        _CALLEE_RE = re.compile(current_str)
        _CALLEE_STR = current_str
    return _CALLEE_RE


class AstEngine:
    def __init__(self):
        self.parsers = {}
        self.languages = {}
        self.missing_bindings = {}
        self._initialized = False
        
    def initialize(self):
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
                warn_once(f"invalid_binding_{ext}", f"Invalid tree-sitter binding configuration for {ext}. Expected list/tuple of (module_name, function_name), but got: {val}")
                continue
            module_name, func_name = val
            try:
                mod = __import__(module_name)
                lang_obj = tree_sitter.Language(getattr(mod, func_name)())
                parser = tree_sitter.Parser()
                parser.set_language(lang_obj)
                self.languages[ext] = lang_obj
                self.parsers[ext] = parser
            except Exception:
                self.missing_bindings[ext] = module_name
        self._initialized = True

    def is_supported(self, ext):
        self.initialize()
        return ext in self.parsers

AST_ENGINE = AstEngine()

def strip_strings_and_comments(line, is_python=False):
    # Strip string literals (both single and double quoted)
    line = re.sub(r'(["\'])(?:(?=(\\?))\2.)*?\1', '', line)
    # Strip C-style block comments (/* ... */) on the same line if not Python
    if not is_python:
        line = re.sub(r'/\*.*?\*/', '', line)
    comment_char = "#" if is_python else "//"
    if comment_char in line: line = line.split(comment_char)[0]
    return line

def extract_function_bounds_ast(file_path, line_num, ext, file_cache=None):
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
    if not target_node: return None, None

    current = target_node
    block_types = ['function_definition', 'class_definition', 'function_item', 'impl_item', 'function_declaration', 'method_definition']
    while current and current.type not in block_types and current.parent:
        current = current.parent
        
    if current and current.type in block_types:
        return current.start_point[0], current.end_point[0] + 1
    return None, None

def extract_function_bounds_regex(file_path, line_num, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    lines = file_cache.get_lines(file_path)
    if not lines: return None, None
    target_idx = line_num - 1
    if target_idx >= len(lines): return None, None

    func_decl_pattern = _get_func_decl_pattern()
    start_idx = target_idx
    while start_idx >= 0:
        if func_decl_pattern.search(lines[start_idx]) or (lines[start_idx].strip() and start_idx == 0): break
        start_idx -= 1
    if start_idx < 0: start_idx = max(0, target_idx - 10)

    is_python = file_path.endswith('.py')
    end_idx = target_idx
    
    if is_python:
        base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())
        end_idx = start_idx + 1
        while end_idx < len(lines):
            line_stripped = lines[end_idx].strip()
            if line_stripped and not line_stripped.startswith('#') and (len(lines[end_idx]) - len(lines[end_idx].lstrip())) <= base_indent: break
            end_idx += 1
    else:
        bracket_count, has_opened = 0, False
        for i in range(start_idx, len(lines)):
            clean_line = strip_strings_and_comments(lines[i])
            bracket_count += clean_line.count('{') - clean_line.count('}')
            if '{' in clean_line: has_opened = True
            if has_opened and bracket_count <= 0:
                end_idx = i + 1; break
        else: end_idx = min(len(lines), target_idx + 20)
    return start_idx, end_idx

def extract_function_bounds(file_path, line_num, file_cache=None):
    if line_num <= 0: return None, None
    ext = os.path.splitext(file_path)[1]
    if AST_ENGINE.is_supported(ext):
        ast_bounds = extract_function_bounds_ast(file_path, line_num, ext, file_cache=file_cache)
        if ast_bounds[0] is not None: return ast_bounds
    return extract_function_bounds_regex(file_path, line_num, file_cache=file_cache)

def trace_lexical_dependencies_ast(func_name, repo_files, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    if not func_name or len(func_name) < 3: return callers
    
    fast_files = ripgrep_filter(repo_files, func_name) if HAS_RG else repo_files
    
    for file_path in fast_files:
        ext = os.path.splitext(file_path)[1]
        
        # Python Typing Check
        if ext == '.py':
            content = file_cache.get_content(file_path)
            if "typing" not in content:
                warn_once("python_typing", "Python files found without 'typing' protocols. Dynamic dispatch tracking relies on type hinting for accuracy.")

        if not AST_ENGINE.is_supported(ext): continue
        source_bytes = file_cache.get_bytes(file_path)
        tree = AST_ENGINE.parsers[ext].parse(source_bytes)
        
        # We escape func_name using re.escape and double-escape backslashes for the tree-sitter query parser,
        # because tree-sitter treats backslashes as escape characters inside query patterns.
        escaped_func_name = re.escape(func_name).replace("\\", "\\\\")

        # Included Registry Pattern matching (register_x)
        query_strings = CONFIG['dependency_query_strings']
        q_str = query_strings.get(ext)
        if not q_str: continue
        
        q_str = q_str.replace("{escaped_func_name}", escaped_func_name)
        
        try:
            query = AST_ENGINE.languages[ext].query(q_str)
            captures = query.captures(tree.root_node)
            lines = file_cache.get_lines(file_path)
            
            for capture_node, capture_name in captures:
                # Defensive check: Ensure the captured node has a parent to avoid AttributeError.
                if capture_node.parent is None:
                    continue
                # Ensure the actual function name is in the text (either as the caller or an argument)
                node_text = source_bytes[capture_node.parent.start_byte:capture_node.parent.end_byte].decode('utf-8', errors='ignore')
                if func_name not in node_text: continue
                
                line_idx = capture_node.start_point[0]
                if file_path not in callers: callers[file_path] = []
                if not any(c['line'] == line_idx + 1 for c in callers[file_path]):
                    callers[file_path].append({"line": line_idx + 1, "code": lines[line_idx].strip()})
        except Exception as exc:
            warn_once("ast_query_fail", f"AST query failed on {file_path}: {exc}")
            
    return callers

def trace_lexical_dependencies_regex(func_name, repo_files, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    callers = {}
    if not func_name or len(func_name) < 3: return callers
    fast_files = ripgrep_filter(repo_files, func_name) if HAS_RG else repo_files
    # Pre-compile the word-boundary pattern once so we don't recompile it per line.
    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore). This avoids boundary mismatch for C++ destructors
    # (e.g. ~MyClass) or C++ operator overloads (e.g. operator+).
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
    for file_path in fast_files:
        ext = os.path.splitext(file_path)[1]
        if ext not in LANG_MAP or file_path.endswith('.md'): continue
        content = file_cache.get_content(file_path)
        if call_pattern.search(content):
            is_cpp = ext in ['.c', '.cpp', '.hpp', '.h']
            for idx, line in enumerate(content.splitlines()):
                # Match with word boundaries to avoid substring false positives,
                # and exclude definition lines (they are not callers).
                if call_pattern.search(line):
                    is_def = False
                    if def_keyword_pattern.search(line):
                        is_def = True
                    elif is_cpp and def_cpp_pattern.search(line) and not line.strip().endswith(';'):
                        is_def = True

                    if not is_def:
                        if file_path not in callers: callers[file_path] = []
                        callers[file_path].append({"line": idx + 1, "code": line.strip()})
    return callers

def split_massive_block_ast(source_text, file_path, max_lines):
    """Replaces dumb slicing by using AST nodes to cleanly truncate bodies."""
    # Ensure max_lines is at least 1 to prevent negative indexing or invalid slicing bounds
    max_lines = max(1, max_lines)
    lines = source_text.splitlines()
    if len(lines) <= max_lines: return [{"suffix": "", "text": source_text}]

    ext = os.path.splitext(file_path)[1]
    is_python = (ext == '.py')

    if not AST_ENGINE.is_supported(ext):
        # Dumb fallback
        if is_python:
            fallback_text = "\n".join(lines[:max_lines]) + "\n# ... [Lines Omitted due to size] ..."
        else:
            fallback_text = "\n".join(lines[:max_lines]) + "\n/* ... [Lines Omitted due to size] ... */"
        return [{"suffix": " (Truncated)", "text": fallback_text}]

    tree = AST_ENGINE.parsers[ext].parse(source_text.encode('utf-8'))
    
    # We walk the children. If a function/class is huge, we replace its body with an omission.
    output_lines = []
    budget = max_lines
    
    for child in tree.root_node.children:
        child_lines = lines[child.start_point[0]:child.end_point[0] + 1]
        if len(child_lines) < budget:
            output_lines.extend(child_lines)
            budget -= len(child_lines)
        else:
            # Semantic Truncation
            if child.type in [
                'function_definition', 'class_definition', 'function_item', 'impl_item',
                'method_definition', 'function_declaration', 'generator_function',
                'generator_function_declaration', 'arrow_function'
            ]:
                # Extract the full multi-line signature by scanning until colon (Python) or opening brace (others)
                sig_lines = []
                # Add a defensive min check to ensure we do not index out of bounds
                end_idx = min(child.end_point[0], len(lines) - 1)
                has_brace = False
                for idx in range(child.start_point[0], end_idx + 1):
                    line = lines[idx]
                    sig_lines.append(line)
                    # Strip comments and strings to ensure we don't match colons/braces inside them
                    clean_line = strip_strings_and_comments(line, is_python=is_python)
                    if not is_python and "{" in clean_line:
                        has_brace = True
                        break
                    if is_python and clean_line.rstrip().endswith(":"):
                        break
                output_lines.extend(sig_lines)
                if sig_lines:
                    indent = len(sig_lines[0]) - len(sig_lines[0].lstrip())
                    if is_python:
                        # Provide indentation and pass to keep Python syntax valid
                        output_lines.append(" " * (indent + 4) + "# ... [Inner Body Omitted for Context Preservation] ...")
                        output_lines.append(" " * (indent + 4) + "pass")
                    else:
                        if has_brace:
                            output_lines.append(" " * (indent + 4) + "/* ... [Inner Body Omitted for Context Preservation] ... */")
                            output_lines.append(" " * indent + "}") # Generic close
            else:
                output_lines.extend(child_lines[:5])
                if is_python:
                    output_lines.append("# ... [Data Structure Omitted] ...")
                else:
                    output_lines.append("/* ... [Data Structure Omitted] ... */")
            budget -= 3
            if budget <= 0: break

    return [{"suffix": " (AST Semantically Pruned)", "text": "\n".join(output_lines)}]

def extract_callees_ast(file_path, start_line, end_line, ext, file_cache):
    source_bytes = file_cache.get_bytes(file_path)
    tree = AST_ENGINE.parsers[ext].parse(source_bytes)
    
    # Restrict traversal to function node matching start_line
    def walk(node):
        for child in node.children:
            if child.start_point[0] == start_line:
                return child
            found = walk(child)
            if found: return found
        return None
    func_node = walk(tree.root_node) or tree.root_node
        
    query_strings = CONFIG['callee_query_strings']
    q_str = query_strings.get(ext)
    if not q_str: return set()
    
    callees = set()
    try:
        query = AST_ENGINE.languages[ext].query(q_str)
        captures = query.captures(func_node)
        for node, _ in captures:
            if start_line <= node.start_point[0] < end_line:
                if not hasattr(node, 'text'):
                    # In py-tree-sitter v0.20.x and below, Node objects do not have a .text attribute.
                    # We raise AttributeError to inform the user to upgrade to version >= 0.21.0.
                    raise AttributeError("Node object lacks '.text' attribute. Please upgrade py-tree-sitter to version 0.21.0 or newer.")
                callees.add(node.text.decode('utf-8', errors='ignore'))
    except AttributeError as ae:
        raise ae
    except Exception as e:
        # Propagate other tree-sitter failures or query syntax exceptions as a RuntimeError
        # so that extract_callees can catch them and fall back to the regex parser.
        raise RuntimeError(f"AST callee extraction failed: {e}") from e
    return callees

def extract_callees_regex(file_path, start_line, end_line, file_cache):
    lines = file_cache.get_lines(file_path)[start_line:end_line]
    callees = set()
    is_python = file_path.endswith('.py')
    callee_pattern = _get_callee_pattern()
    for line in lines:
        line_clean = strip_strings_and_comments(line, is_python)
        for match in re.finditer(callee_pattern, line_clean):
            name = match.group(1)
            if name not in CONFIG['callee_ignored_keywords']:
                callees.add(name)
    return callees

def extract_callees(file_path, start_line, end_line, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    ext = os.path.splitext(file_path)[1]
    if AST_ENGINE.is_supported(ext):
        try:
            callees = extract_callees_ast(file_path, start_line, end_line, ext, file_cache)
            # Return AST results unconditionally — even an empty set is valid (the
            # function genuinely has no callees).  Only fall back to regex when the
            # AST parser itself raised an error (e.g. old py-tree-sitter without .text).
            return list(callees)
        except (AttributeError, RuntimeError) as e:
            # Catch AttributeError and RuntimeError to gracefully fall back on systems using
            # older py-tree-sitter versions or when query syntax / tree-sitter errors occur.
            print(f"\n[ContextLens Warning] {e} Falling back to regex-based callee extraction.")
    return list(extract_callees_regex(file_path, start_line, end_line, file_cache))

def find_callee_definition(callee_name, all_repo_files, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    if not callee_name or len(callee_name) < 3: return None, None
    
    candidate_files = ripgrep_filter(all_repo_files, callee_name) if HAS_RG else all_repo_files
    
    # We dynamically construct boundaries so \b is only applied if the adjacent character
    # is a word character (alphanumeric or underscore). This avoids boundary mismatch for C++ destructors
    # (e.g. ~MyClass) or C++ operator overloads (e.g. operator+).
    lead_b = r'\b' if callee_name[0].isalnum() or callee_name[0] == '_' else ''
    trail_b = r'\b' if callee_name[-1].isalnum() or callee_name[-1] == '_' else ''
    escaped_callee = re.escape(callee_name)

    # Precise patterns for definitions
    pattern = CONFIG['def_pattern_template'].replace("{lead_b}", lead_b).replace("{escaped_callee}", escaped_callee).replace("{trail_b}", trail_b)
    cpp_pattern = CONFIG['cpp_def_pattern_template'].replace("{lead_b}", lead_b).replace("{escaped_callee}", escaped_callee).replace("{trail_b}", trail_b)

    for file_path in candidate_files:
        ext = os.path.splitext(file_path)[1]
        if ext not in LANG_MAP: continue
        
        lines = file_cache.get_lines(file_path)
        is_python = (ext == '.py')
        for idx, line in enumerate(lines):
            clean_line = strip_strings_and_comments(line, is_python)
            if re.search(pattern, clean_line) or (ext in ['.c', '.cpp', '.hpp', '.h'] and re.search(cpp_pattern, clean_line) and not clean_line.strip().endswith(';')):
                return file_path, idx + 1
    return None, None

