import os
import re
from .sys_utils import warn_once, run_command, ripgrep_filter, HAS_RG
from .cache import get_global_cache

try:
    import tree_sitter
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False

LANG_MAP = {
    '.rs': 'rust', '.js': 'javascript', '.ts': 'typescript', '.py': 'python',
    '.cpp': 'cpp', '.hpp': 'cpp', '.c': 'c', '.go': 'go', '.pl': 'perl',
    '.mk': 'makefile', '.cmake': 'cmake', '.sh': 'bash', '.bat': 'batch'
}

class AstEngine:
    def __init__(self):
        self.parsers = {}
        self.languages = {}
        self.missing_bindings = {}
        
        if not HAS_TREESITTER:
            warn_once('tree-sitter', "For perfect AST scoping, install tree-sitter bindings.")
            return
            
        bindings = {
            '.py': ('tree_sitter_python', 'language'),
            '.rs': ('tree_sitter_rust', 'language'),
            '.js': ('tree_sitter_javascript', 'language'),
            '.ts': ('tree_sitter_typescript', 'language_typescript'),
            '.c':  ('tree_sitter_c', 'language'),
            '.cpp': ('tree_sitter_cpp', 'language')
        }
        
        for ext, (module_name, func_name) in bindings.items():
            try:
                mod = __import__(module_name)
                lang_obj = tree_sitter.Language(getattr(mod, func_name)())
                parser = tree_sitter.Parser()
                parser.set_language(lang_obj)
                self.languages[ext] = lang_obj
                self.parsers[ext] = parser
            except ImportError:
                self.missing_bindings[ext] = module_name

    def is_supported(self, ext):
        return ext in self.parsers

AST_ENGINE = AstEngine()

def strip_strings_and_comments(line, is_python=False):
    line = re.sub(r'(["\'])(?:(?=(\\?))\2.)*?\1', '', line)
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

    func_decl_pattern = re.compile(r'\b(fn|function|def|sub|func|class|macro)\b|^\s*([A-Za-z0-9_<>:]+\s+)+[A-Za-z0-9_]+\s*\(', re.MULTILINE)
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
        
        # Included Registry Pattern matching (register_x)
        query_strings = {
            '.py': f'(call function: [(identifier) @id (attribute attribute: (identifier) @id)] (#match? @id ".*({func_name}|register).*"))',
            '.rs': f'(call_expression function: [(identifier) @id (scoped_identifier name: (identifier) @id) (field_expression field: (field_identifier) @id)] (#match? @id ".*({func_name}|register).*"))',
            '.js': f'(call_expression function: [(identifier) @id (member_expression property: (property_identifier) @id)] (#match? @id ".*({func_name}|register).*"))',
            '.ts': f'(call_expression function: [(identifier) @id (member_expression property: (property_identifier) @id)] (#match? @id ".*({func_name}|register).*"))',
            '.c': f'(call_expression function: (identifier) @id (#match? @id ".*({func_name}|register).*"))',
            '.cpp': f'(call_expression function: [(identifier) @id (scoped_identifier name: (identifier) @id) (field_expression field: (field_identifier) @id)] (#match? @id ".*({func_name}|register).*"))'
        }
        
        q_str = query_strings.get(ext)
        if not q_str: continue
        
        try:
            query = AST_ENGINE.languages[ext].query(q_str)
            captures = query.captures(tree.root_node)
            lines = file_cache.get_lines(file_path)
            
            for capture_node, capture_name in captures:
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
    for file_path in fast_files:
        if os.path.splitext(file_path)[1] not in LANG_MAP or file_path.endswith('.md'): continue
        content = file_cache.get_content(file_path)
        if func_name in content:
            pattern = r'\b(fn|def|function|sub|func|class|macro)\s+' + re.escape(func_name)
            for idx, line in enumerate(content.splitlines()):
                if func_name in line and not re.search(pattern, line):
                    if file_path not in callers: callers[file_path] = []
                    callers[file_path].append({"line": idx + 1, "code": line.strip()})
    return callers

def split_massive_block_ast(source_text, file_path, max_lines):
    """Replaces dumb slicing by using AST nodes to cleanly truncate bodies."""
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
            if child.type in ['function_definition', 'class_definition', 'function_item', 'impl_item']:
                # Extract the full multi-line signature by scanning until colon (Python) or opening brace (others)
                sig_lines = []
                for idx in range(child.start_point[0], child.end_point[0] + 1):
                    line = lines[idx]
                    sig_lines.append(line)
                    # Strip comments and strings to ensure we don't match colons/braces inside them
                    clean_line = strip_strings_and_comments(line, is_python=is_python)
                    if (is_python and ":" in clean_line) or (not is_python and "{" in clean_line):
                        break
                output_lines.extend(sig_lines)
                indent = len(sig_lines[0]) - len(sig_lines[0].lstrip())
                if is_python:
                    # Provide indentation and pass to keep Python syntax valid
                    output_lines.append(" " * (indent + 4) + "# ... [Inner Body Omitted for Context Preservation] ...")
                    output_lines.append(" " * (indent + 4) + "pass")
                else:
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
        
    query_strings = {
        '.py': '(call function: [(identifier) @id (attribute attribute: (identifier) @id)])',
        '.rs': '(call_expression function: [(identifier) @id (scoped_identifier name: (identifier) @id) (field_expression field: (field_identifier) @id)])',
        '.js': '(call_expression function: [(identifier) @id (member_expression property: (property_identifier) @id)])',
        '.ts': '(call_expression function: [(identifier) @id (member_expression property: (property_identifier) @id)])',
        '.c': '(call_expression function: (identifier) @id)',
        '.cpp': '(call_expression function: [(identifier) @id (scoped_identifier name: (identifier) @id) (field_expression field: (field_identifier) @id)])'
    }
    
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
    except Exception:
        pass
    return callees

def extract_callees_regex(file_path, start_line, end_line, file_cache):
    lines = file_cache.get_lines(file_path)[start_line:end_line]
    callees = set()
    is_python = file_path.endswith('.py')
    for line in lines:
        line_clean = strip_strings_and_comments(line, is_python)
        for match in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(', line_clean):
            name = match.group(1)
            if name not in ['if', 'while', 'for', 'with', 'print', 'len', 'fn', 'def', 'function', 'class']:
                callees.add(name)
    return callees

def extract_callees(file_path, start_line, end_line, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    ext = os.path.splitext(file_path)[1]
    if AST_ENGINE.is_supported(ext):
        callees = extract_callees_ast(file_path, start_line, end_line, ext, file_cache)
        if callees: return list(callees)
    return list(extract_callees_regex(file_path, start_line, end_line, file_cache))

def find_callee_definition(callee_name, all_repo_files, file_cache=None):
    if file_cache is None:
        file_cache = get_global_cache()
    if not callee_name or len(callee_name) < 3: return None, None
    
    candidate_files = ripgrep_filter(all_repo_files, callee_name) if HAS_RG else all_repo_files
    
    # Precise patterns for definitions
    pattern = rf'\b(?:fn|def|function|sub|func|class|macro)\s+{re.escape(callee_name)}\b'
    cpp_pattern = rf'^\s*[A-Za-z0-9_<>:]+(?:\s+\*?\s*)*{re.escape(callee_name)}\s*\('

    for file_path in candidate_files:
        ext = os.path.splitext(file_path)[1]
        if ext not in LANG_MAP: continue
        
        lines = file_cache.get_lines(file_path)
        is_python = (ext == '.py')
        for idx, line in enumerate(lines):
            clean_line = strip_strings_and_comments(line, is_python)
            if re.search(pattern, clean_line) or (ext in ['.c', '.cpp', '.hpp'] and re.search(cpp_pattern, clean_line)):
                return file_path, idx + 1
    return None, None

