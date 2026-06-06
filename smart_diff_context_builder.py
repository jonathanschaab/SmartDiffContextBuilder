#!/usr/bin/env python3
import os
import subprocess
import re
import sys
import json
import argparse
import glob
import fnmatch
import time
import urllib.parse
import xml.etree.ElementTree as ET
import atexit
import signal
from pathlib import Path
from collections import OrderedDict

# =====================================================================
# OPTIONAL DEPENDENCY LOADERS
# =====================================================================
try:
    import tree_sitter
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False

WARNED_MISSING_DEPS = set()

def warn_once(key, message):
    if key not in WARNED_MISSING_DEPS:
        print(f"\n[Notice] {message}")
        WARNED_MISSING_DEPS.add(key)

LANG_MAP = {
    '.rs': 'rust', '.js': 'javascript', '.ts': 'typescript', '.py': 'python',
    '.cpp': 'cpp', '.hpp': 'cpp', '.c': 'c', '.go': 'go', '.pl': 'perl',
    '.mk': 'makefile', '.cmake': 'cmake', '.sh': 'bash', '.bat': 'batch'
}

# =====================================================================
# LRU FILE CACHE & SYSTEM UTILS
# =====================================================================
class LRUFileCache:
    def __init__(self, capacity):
        self.cache = OrderedDict()
        self.capacity = capacity

    def get_lines(self, file_path):
        if file_path in self.cache:
            self.cache.move_to_end(file_path)
            return self.cache[file_path]
            
        if not os.path.exists(file_path): return []
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            
        self.cache[file_path] = lines
        self.cache.move_to_end(file_path)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)
        return lines

    def get_content(self, file_path):
        return "".join(self.get_lines(file_path))

    def get_bytes(self, file_path):
        return self.get_content(file_path).encode('utf-8')

FILE_CACHE = None 
USE_LSP = True

def run_command(cmd, exit_on_fail=False, timeout=None):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=timeout)
        return res.stdout
    except subprocess.TimeoutExpired:
        warn_once(f"timeout_{cmd[0]}", f"Command '{' '.join(cmd)}' timed out.")
        return ""
    except subprocess.CalledProcessError:
        if exit_on_fail: sys.exit(1)
        return ""
    except FileNotFoundError:
        return ""
		
def get_git_diff_files():
    # Gets all files modified in the current diff
    out = run_command(["git", "diff", "--name-only", "HEAD"])
    return [f for f in out.splitlines() if f.strip() and os.path.exists(f)]

def get_git_tracked_files():
    stdout = run_command(["git", "ls-files"], exit_on_fail=True)
    return [l.strip() for l in stdout.splitlines() if l.strip()]

def ripgrep_filter(files, token):
    rg_out = run_command(["rg", "-l", "-F", token])
    if not rg_out: return []
    rg_files = set(rg_out.splitlines())
    return [f for f in files if f in rg_files]
	
def analyze_compile_commands(target_file):
    callers = {}
    if not os.path.exists("compile_commands.json"): return callers
    try:
        with open("compile_commands.json", "r") as f: db = json.load(f)
        target_base = os.path.basename(target_file)
        for entry in db:
            if target_base in entry.get("file", ""):
                ref_file = entry.get("file")
                if ref_file and ref_file != target_file:
                    if ref_file not in callers: callers[ref_file] = []
                    callers[ref_file].append({"line": 0, "code": f"// [Compilation Link via compile_commands.json]"})
    except: pass
    return callers

# =====================================================================
# NATIVE JSON-RPC LSP CLIENT & CLEANUP
# =====================================================================
class MinimalLSPClient:
    def __init__(self, cmd):
        self.cmd = cmd
        self.proc = None
        self.req_id = 1

    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            init_msg = {
                "jsonrpc": "2.0", "id": self.req_id, "method": "initialize",
                "params": {"processId": os.getpid(), "rootUri": f"file://{os.path.abspath('.')}", "capabilities": {}}
            }
            self._send(init_msg)
            
            start_time = time.time()
            while time.time() - start_time < 10:
                res = self._recv()
                if res and res.get("id") == self.req_id: break
            self.req_id += 1
            
            self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            return True
        except Exception as e:
            warn_once("lsp_fail", f"Failed to start LSP {self.cmd[0]}: {e}")
            return False

    def _send(self, msg_dict):
        body = json.dumps(msg_dict).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('utf-8')
        self.proc.stdin.write(header + body)
        self.proc.stdin.flush()

    def _recv(self):
        content_length = 0
        while True:
            line = self.proc.stdout.readline().decode('utf-8')
            if not line or line == "\r\n": break
            if line.startswith("Content-Length:"):
                content_length = int(line.split(":")[1].strip())
        
        if content_length == 0: return None
        body = self.proc.stdout.read(content_length).decode('utf-8')
        return json.loads(body)

    def get_references(self, file_path, line_num, char_num, timeout):
        req_id = self.req_id
        req = {
            "jsonrpc": "2.0", "id": req_id, "method": "textDocument/references",
            "params": {
                "textDocument": {"uri": f"file://{os.path.abspath(file_path)}"},
                "position": {"line": line_num - 1, "character": char_num},
                "context": {"includeDeclaration": False}
            }
        }
        self._send(req)
        self.req_id += 1

        start_time = time.time()
        while time.time() - start_time < timeout:
            res = self._recv()
            if not res: 
                time.sleep(0.05)
                continue
            if "id" not in res: continue
            if res["id"] == req_id: return res.get("result", [])
                
        warn_once("lsp_timeout", f"LSP query timed out after {timeout}s. Increase --lsp-timeout if indexing takes longer.")
        return []

LSP_INSTANCES = {}

def cleanup_zombie_lsps():
    for client in LSP_INSTANCES.values():
        if client and client.proc:
            try:
                client._send({"jsonrpc": "2.0", "method": "exit"})
                client.proc.terminate()
                client.proc.wait(timeout=1)
            except Exception:
                try: os.kill(client.proc.pid, signal.SIGKILL)
                except OSError: pass

atexit.register(cleanup_zombie_lsps)

def get_lsp_references(file_path, line_num, func_name, timeout, max_depth, disable_pruning):
    if not USE_LSP: return None
    
    ext = os.path.splitext(file_path)[1]
    configs = {
        '.cpp': ["clangd", "--background-index"],
        '.c': ["clangd", "--background-index"],
        '.rs': ["rust-analyzer"],
        '.py': ["pylsp"],
        '.ts': ["typescript-language-server", "--stdio"]
    }
    
    if ext not in configs: return None
    cmd = configs[ext]
    
    if ext not in LSP_INSTANCES:
        client = MinimalLSPClient(cmd)
        if client.start(): LSP_INSTANCES[ext] = client
        else: LSP_INSTANCES[ext] = None
            
    client = LSP_INSTANCES.get(ext)
    if not client: return None

    lines = FILE_CACHE.get_lines(file_path)
    if line_num > len(lines): return []
    char_idx = lines[line_num - 1].find(func_name)
    if char_idx == -1: char_idx = 0

    print(f" [LSP] Querying {cmd[0]} for {func_name}() references...")
    refs = client.get_references(file_path, line_num, char_idx, timeout=timeout)
    
    callers = {}
    total_refs = len(refs)
    
    # Heuristic Pruning & Depth Limiting
    if not disable_pruning and total_refs > max_depth:
        refs = refs[:max_depth]
        warn_once(f"prune_{func_name}", f"Polymorphic explosion detected for {func_name}. Pruning to {max_depth} callers.")

    for ref in refs:
        ref_path = urllib.parse.unquote(ref.get("uri", "").replace("file://", ""))
        try: rel_path = os.path.relpath(ref_path, os.getcwd())
        except ValueError: rel_path = ref_path
            
        ref_line = ref["range"]["start"]["line"]
        ref_code = FILE_CACHE.get_lines(rel_path)[ref_line].strip() if os.path.exists(rel_path) else "[Code Unavailable]"
        
        if rel_path not in callers: callers[rel_path] = []
        callers[rel_path].append({"line": ref_line + 1, "code": ref_code})

    if not disable_pruning and total_refs > max_depth:
        callers["[Pruned Instances]"] = [{"line": 0, "code": f"// Omitted {total_refs - max_depth} additional interface implementations to preserve context window."}]
        
    return callers

# =====================================================================
# MULTIPASS MACRO EXPANSION & FFI
# =====================================================================
def trace_macro_expansion(func_name, repo_files):
    """Pass 2: Pre-expands C/C++ files and source-maps the linkages back to macros."""
    callers = {}
    print(f" [Pre-Expansion] Searching expanded ASTs for {func_name}...")
    fast_files = ripgrep_filter(repo_files, func_name) if run_command(["rg", "--version"]) else repo_files
    
    for f in fast_files:
        ext = os.path.splitext(f)[1]
        if ext not in ['.c', '.cpp', '.hpp']: continue
        
        # Pass 1: Expand
        expanded_code = run_command(["clang", "-E", f], timeout=5)
        if not expanded_code: continue
        
        # Pass 2: Map
        expanded_lines = expanded_code.splitlines()
        for idx, line in enumerate(expanded_lines):
            if func_name in line:
                # Walk backward to find linemarker
                for marker_idx in range(idx, -1, -1):
                    if expanded_lines[marker_idx].startswith("#"):
                        marker_match = re.match(r'#\s+(\d+)\s+"([^"]+)"', expanded_lines[marker_idx])
                        if marker_match:
                            orig_line = int(marker_match.group(1))
                            orig_file = os.path.relpath(marker_match.group(2), os.getcwd())
                            if os.path.exists(orig_file):
                                orig_code = FILE_CACHE.get_lines(orig_file)[orig_line - 1].strip()
                                if orig_file not in callers: callers[orig_file] = []
                                if not any(c['line'] == orig_line for c in callers[orig_file]):
                                    callers[orig_file].append({"line": orig_line, "code": f"// [Macro Expansion Link] {orig_code}"})
                        break
    return callers

def build_ffi_registry(repo_files):
    ffi_symbols = set()
    print(" [FFI] Running pre-computation pass for cross-language boundaries...")
    fast_files = ripgrep_filter(repo_files, "no_mangle|wasm_bindgen|extern \"C\"|EMSCRIPTEN_KEEPALIVE|PYBIND11_MODULE|m.def") if run_command(["rg", "--version"]) else repo_files
    
    for f in fast_files:
        content = FILE_CACHE.get_content(f)
        for m in re.finditer(r'#\[(?:no_mangle|wasm_bindgen)\].*?(?:fn|static)\s+([A-Za-z0-9_]+)', content, re.DOTALL):
            ffi_symbols.add(m.group(1))
        for m in re.finditer(r'(?:extern\s+"C"|EMSCRIPTEN_KEEPALIVE).*?(?:void|int|char|double|float|bool|auto)\s+([A-Za-z0-9_]+)\s*\(', content, re.DOTALL):
            ffi_symbols.add(m.group(1))
        for m in re.finditer(r'm\.def\(\s*"([^"]+)"', content):
            ffi_symbols.add(m.group(1))
            
    if ffi_symbols: print(f" [FFI] Registered {len(ffi_symbols)} exported symbols.")
    return ffi_symbols

def trace_ffi_callers(func_name, repo_files, source_ext):
    callers = {}
    print(f" [FFI] Cross-language tracing triggered for exported symbol: {func_name}()")
    fast_files = ripgrep_filter(repo_files, func_name) if run_command(["rg", "--version"]) else repo_files
    
    for f in fast_files:
        ext = os.path.splitext(f)[1]
        if ext == source_ext: continue
        lines = FILE_CACHE.get_lines(f)
        for idx, line in enumerate(lines):
            if func_name in line:
                if f not in callers: callers[f] = []
                callers[f].append({"line": idx + 1, "code": f"// [FFI Bridge] {line.strip()}"})
    return callers

# =====================================================================
# AST ENGINE & SEMANTIC SLICING
# =====================================================================
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

def extract_function_bounds_ast(file_path, line_num, ext):
    source_bytes = FILE_CACHE.get_bytes(file_path)
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

def extract_function_bounds_regex(file_path, line_num):
    lines = FILE_CACHE.get_lines(file_path)
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

def extract_function_bounds(file_path, line_num):
    ext = os.path.splitext(file_path)[1]
    if AST_ENGINE.is_supported(ext):
        ast_bounds = extract_function_bounds_ast(file_path, line_num, ext)
        if ast_bounds[0] is not None: return ast_bounds
    return extract_function_bounds_regex(file_path, line_num)

def trace_lexical_dependencies_ast(func_name, repo_files):
    callers = {}
    if not func_name or len(func_name) < 3: return callers
    
    fast_files = ripgrep_filter(repo_files, func_name) if run_command(["rg", "--version"]) else repo_files
    
    for file_path in fast_files:
        ext = os.path.splitext(file_path)[1]
        
        # Python Typing Check
        if ext == '.py':
            content = FILE_CACHE.get_content(file_path)
            if "typing" not in content:
                warn_once("python_typing", "Python files found without 'typing' protocols. Dynamic dispatch tracking relies on type hinting for accuracy.")

        if not AST_ENGINE.is_supported(ext): continue
        source_bytes = FILE_CACHE.get_bytes(file_path)
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
            lines = FILE_CACHE.get_lines(file_path)
            
            for capture_node, capture_name in captures:
                # Ensure the actual function name is in the text (either as the caller or an argument)
                node_text = source_bytes[capture_node.parent.start_byte:capture_node.parent.end_byte].decode('utf-8', errors='ignore')
                if func_name not in node_text: continue
                
                line_idx = capture_node.start_point[0]
                if file_path not in callers: callers[file_path] = []
                if not any(c['line'] == line_idx + 1 for c in callers[file_path]):
                    callers[file_path].append({"line": line_idx + 1, "code": lines[line_idx].strip()})
        except Exception: pass 
            
    return callers

def trace_lexical_dependencies_regex(func_name, repo_files):
    callers = {}
    if not func_name or len(func_name) < 3: return callers
    fast_files = ripgrep_filter(repo_files, func_name) if run_command(["rg", "--version"]) else repo_files
    for file_path in fast_files:
        if os.path.splitext(file_path)[1] not in LANG_MAP or file_path.endswith('.md'): continue
        content = FILE_CACHE.get_content(file_path)
        if func_name in content:
            for idx, line in enumerate(content.splitlines()):
                if func_name in line and not re.search(r'\b(fn|def|function|sub|func|class|macro)\s+' + func_name, line):
                    if file_path not in callers: callers[file_path] = []
                    callers[file_path].append({"line": idx + 1, "code": line.strip()})
    return callers

# =====================================================================
# AST-AWARE SEMANTIC SLICING
# =====================================================================
def split_massive_block_ast(source_text, file_path, max_lines):
    """Replaces dumb slicing by using AST nodes to cleanly truncate bodies."""
    lines = source_text.splitlines()
    if len(lines) <= max_lines: return [{"suffix": "", "text": source_text}]

    ext = os.path.splitext(file_path)[1]
    if not AST_ENGINE.is_supported(ext):
        # Dumb fallback
        return [{"suffix": " (Truncated)", "text": "\n".join(lines[:max_lines]) + "\n/* ... [Lines Omitted due to size] ... */"}]

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
                signature = lines[child.start_point[0]]
                output_lines.append(signature)
                output_lines.append("    /* ... [Inner Body Omitted for Context Preservation] ... */")
                output_lines.append("}") # Generic close
            else:
                output_lines.extend(child_lines[:5])
                output_lines.append("/* ... [Data Structure Omitted] ... */")
            budget -= 3
            if budget <= 0: break

    return [{"suffix": " (AST Semantically Pruned)", "text": "\n".join(output_lines)}]

# =====================================================================
# STRUCTURAL PRIORITIZATION FUNNEL (VOLUME MANAGER)
# =====================================================================
class VolumeManager:
    def __init__(self, fmt, max_lines, max_mb, base_name="llm_payload"):
        self.fmt = fmt.lower()
        self.max_lines = max_lines
        self.max_bytes = max_mb * 1024 * 1024
        self.base_name = base_name
        self.raw_diff_text = ""
        
        # Categorical Storage for Funnel Sorting
        self.modified_objects = []
        self.unit_tests = []
        self.local_callers = []
        self.ffi_linkages = []

    def set_raw_diff(self, diff_text):
        self.raw_diff_text = diff_text

    def add_modified_object(self, file_path, func_name, source_block):
        subunits = split_massive_block_ast(source_block, file_path, self.max_lines - 100)
        for sub in subunits:
            self.modified_objects.append({"file": file_path, "function_name": func_name + sub["suffix"], "source_block": sub["text"]})

    def add_callers(self, category_list, callers_dict, category_label, confidence="HIGH"):
        for f, occs in callers_dict.items():
            for occ in occs:
                # Ghost Detection: Tag FFI bridges with a warning
                is_ffi = "FFI Bridge" in occ["code"]
                conf = "LOW (Ghost Risk)" if is_ffi else confidence
                category_list.append({
                    "file": f, "type": category_label, 
                    "line": occ["line"], "code": occ["code"], 
                    "confidence": conf
                })

    def flush_all_volumes(self):
        """Builds payloads chronologically: Diff -> Objects -> Tests -> Callers -> FFI"""
        payload = f"# LLM Context Payload\n## 1. Raw Diff\n```diff\n{self.raw_diff_text}\n```\n"
        
        # Level 1: Core Logic
        if self.modified_objects:
            payload += "## 2. Modified Core Logic\n"
            for obj in self.modified_objects:
                lang = LANG_MAP.get(os.path.splitext(obj['file'])[1], 'text')
                payload += f"### `{obj['file']}` -> `{obj['function_name']}()`\n```{lang}\n{obj['source_block']}\n```\n"

        # Level 2: Unit Tests
        if self.unit_tests:
            payload += "## 3. Validating Unit Tests\n"
            for t in self.unit_tests:
                lang = LANG_MAP.get(os.path.splitext(t['file'])[1], 'text')
                payload += f"### `{t['file']}` (Line {t['line']})\n```{lang}\n{t['code']}\n```\n"

        # Level 3: Upstream Callers
        if self.local_callers:
            payload += "## 4. Upstream Dependent Callers\n"
            for c in self.local_callers:
                payload += f"- `{c['file']}` (L{c['line']}): `{c['code']}` **[Confidence: {c['confidence']}]**\n"

        # Level 4: FFI
        if self.ffi_linkages:
            payload += "## 5. Cross-Language FFI Linkages\n"
            for f in self.ffi_linkages:
                payload += f"- `{f['file']}` (L{f['line']}): `{f['code']}` **[Confidence: {f['confidence']}]**\n"
        
        with open(f"{self.base_name}_final.md", "w", encoding="utf-8") as f: f.write(payload)
        print(f"\nSuccessfully generated {self.base_name}_final.md")

# =====================================================================
# TEST COVERAGE & MINING
# =====================================================================
def get_coverage_data():
    cov_map = {}
    if os.path.exists("coverage.xml"):
        try:
            tree = ET.parse("coverage.xml")
            root = tree.getroot()
            for cls in root.iter('class'):
                filename = cls.get('filename')
                cov_map[filename] = [int(line.get('number')) for line in cls.iter('line') if int(line.get('hits', 0)) > 0]
        except Exception: pass
    return cov_map

def mine_relevant_unit_tests(func_name, repo_files, current_source_file=None):
    discovered_tests = []
    if not func_name or len(func_name) < 3: return discovered_tests

    files_to_scan = ripgrep_filter(repo_files, func_name) if run_command(["rg", "--version"]) else repo_files
    if current_source_file and current_source_file not in files_to_scan: files_to_scan.append(current_source_file)

    for file_path in files_to_scan:
        path_lower = file_path.lower()
        is_test_file = ("test" in path_lower or "spec" in path_lower or (file_path.endswith('.rs') and file_path == current_source_file))
        if not is_test_file: continue

        lines = FILE_CACHE.get_lines(file_path)
        ext = os.path.splitext(file_path)[1]

        if AST_ENGINE.is_supported(ext):
            source_bytes = FILE_CACHE.get_bytes(file_path)
            tree = AST_ENGINE.parsers[ext].parse(source_bytes)
            test_queries = {
                '.rs': '(attribute_item (attribute (identifier) @attr (#eq? @attr "test")))',
                '.py': '(decorator (identifier) @dec (#match? @dec "^(pytest|test)"))',
            }
            if ext in test_queries:
                try:
                    query = AST_ENGINE.languages[ext].query(test_queries[ext])
                    captures = query.captures(tree.root_node)
                    for node, _ in captures:
                        func_node = node
                        while func_node and func_node.type not in ['function_item', 'function_definition']:
                            func_node = func_node.parent
                        if func_node and func_name.encode() in source_bytes[func_node.start_byte:func_node.end_byte]:
                            start_l, end_l = func_node.start_point[0], func_node.end_point[0] + 1
                            test_body = "".join(lines[start_l:end_l])
                            if not any(t["code"].strip() == test_body.strip() for t in discovered_tests):
                                discovered_tests.append({"file": file_path, "line": start_l + 1, "code": test_body})
                except Exception: pass

        for idx, line in enumerate(lines):
            if func_name in line and any(term in line.lower() for term in ["test", "it(", "describe"]):
                start, end = extract_function_bounds_regex(file_path, idx + 1)
                if start is not None:
                    test_body = "".join(lines[start:end])
                    if not any(t["code"].strip() == test_body.strip() for t in discovered_tests):
                        discovered_tests.append({"file": file_path, "line": start + 1, "code": test_body})
                        
    return discovered_tests

# =====================================================================
# MAIN EXECUTION
# =====================================================================
def main():
    global FILE_CACHE, USE_LSP
    parser = argparse.ArgumentParser(description="Compile context-aware git diff tokens optimized for LLMs.")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("--max-lines", type=int, default=1500)
    parser.add_argument("--max-mb", type=float, default=2.0)
    parser.add_argument("--base-name", type=str, default="llm_payload")
    parser.add_argument("--max-cache-size", type=int, default=100)
    
    # Configurable Flags
	parser.add_argument("--max-interface-depth", type=int, default=15)
    parser.add_argument("--disable-pruning", action="store_true")
    parser.add_argument("--lsp-timeout", type=int, default=45)
    parser.add_argument("--no-language-server", action="store_true")
    parser.add_argument("--skip-ffi", action="store_true")
    parser.add_argument("--skip-macro-expansion", action="store_true")
    
    args = parser.parse_args()
    FILE_CACHE = LRUFileCache(capacity=args.max_cache_size)
    USE_LSP = not args.no_language_server

    print(f"\nScanning Git Diff Workspace [Format: {args.format.upper()}]")
    
    diff_files = get_git_diff_files()
    if not diff_files: print("Workspace is clean."); return

    raw_diff = run_command(["git", "diff", "HEAD"])
    all_repo_files = get_git_tracked_files() 
    coverage_data = get_coverage_data()
    
    # FFI Pre-computation pass
    ffi_exports = set()
    if not args.skip_ffi:
        ffi_exports = build_ffi_registry(all_repo_files)

    vm = VolumeManager(args.format, args.max_lines, args.max_mb, args.base_name)
    vm.set_raw_diff(raw_diff)
    processed_spans = set() # Upgraded to track exact structural spans to prevent duplicate macro traces
	
	# Initialize linkage map for C++ files if compile_commands exists
    cpp_linkages = {}
    if any(f.endswith('.cpp') or f.endswith('.c') for f in diff_files):
        cpp_linkages = analyze_compile_commands(diff_files[0]) # Helper from previous block

    for file_path in diff_files:
        ext = os.path.splitext(file_path)[1]
        diff_lines = run_command(["git", "diff", "-U0", "HEAD", file_path]).splitlines()
        line_numbers = [int(m.group(1)) for l in diff_lines for m in [re.search(r'\+(\d+)', l)] if m]
        if not line_numbers or not os.path.exists(file_path): continue

        file_lines = FILE_CACHE.get_lines(file_path)

        for line_num in line_numbers:
            cov_status = "Covered" if file_path in coverage_data and line_num in coverage_data[file_path] else "Unknown"

            start, end = extract_function_bounds(file_path, line_num)
            if start is None: continue
            func_chunk = "".join(file_lines[start:end])
            
            name_match = re.search(r'\b(?:fn|def|function|sub|func|class|macro)\s+([A-Za-z0-9_]+)', file_lines[start])
            func_name = name_match.group(1) if name_match else f"block_lines_{start}_{end}"

            # Deduplication
            span_signature = f"{file_path}::line_{start}_to_{end}"
            if span_signature in processed_spans: continue
            processed_spans.add(span_signature)

            # 1. Funnel: Add Object
            vm.add_modified_object(file_path, func_name, f"// [Test Coverage: {cov_status}]\n{func_chunk}")

            # 2. Funnel: Add Tests
            vm.tests.extend(mine_relevant_unit_tests(func_name, all_repo_files, current_source_file=file_path))

            # 3. Funnel: Trace Callers (LSP -> AST -> Regex)
            callers = get_lsp_references(file_path, line_num, func_name, args.lsp_timeout, args.max_interface_depth, args.disable_pruning)
            if callers is None:
                callers = trace_lexical_dependencies_ast(func_name, all_repo_files) if AST_ENGINE.is_supported(ext) else trace_lexical_dependencies_regex(func_name, all_repo_files)
            
            # Merge Build Linkages (C++)
            if file_path in cpp_linkages:
                for req in cpp_linkages[file_path]:
                    if req not in callers: callers[req] = []
                    callers[req].extend(cpp_linkages[file_path][req])
            
            vm.add_callers(vm.local_callers, callers, "Lexical Dependency", confidence="MEDIUM")

            # 4. Funnel: FFI
            if func_name in ffi_exports:
                ffi_callers = trace_ffi_callers(func_name, all_repo_files, source_ext=ext)
                vm.add_callers(vm.ffi_linkages, ffi_callers, "FFI Linkage")

    # Final Execution: Process Funnel and Splice into Volumes
    vm.flush_all_volumes()
    cleanup_zombie_lsps()
    print("\nContext packaging completed successfully.")

if __name__ == "__main__":
    main()