import os
import time
import json
import atexit
import subprocess
import urllib.parse
from urllib.request import url2pathname
import queue
import threading
from .sys_utils import warn_once
from .cache import get_global_cache

USE_LSP = True
LSP_INSTANCES = {}

class MinimalLSPClient:
    def __init__(self, cmd):
        self.cmd = cmd
        self.proc = None
        self.req_id = 1
        self.msg_queue = queue.Queue()
        self.reader_thread = None

    def start(self):
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            # Start background reader thread to prevent blocking on hangs
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()

            init_msg = {
                "jsonrpc": "2.0", "id": self.req_id, "method": "initialize",
                "params": {"processId": os.getpid(), "rootUri": f"file://{os.path.abspath('.')}", "capabilities": {}}
            }
            self._send(init_msg)
            
            start_time = time.time()
            while time.time() - start_time < 10:
                res = self._recv(timeout=0.1)
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

    def _reader_loop(self):
        # Background thread continuously reads stdout, parsing JSON-RPC messages and queuing them
        try:
            while self.proc and self.proc.poll() is None:
                msg = self._recv_blocking()
                if msg is not None:
                    # Memory Leak Prevention: Only queue responses (which contain an "id").
                    # Notifications (e.g. diagnostics, progress) do not have an "id" and can be discarded safely.
                    if "id" in msg:
                        self.msg_queue.put(msg)
                else:
                    break
        except Exception:
            pass

    def _recv_blocking(self):
        # Performs blocking I/O to read a single JSON-RPC message from stdout
        content_length = 0
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            # Decode stream with errors='ignore' to prevent UnicodeDecodeError on unexpected bytes
            line_str = line.decode('utf-8', errors='ignore')
            if line_str == "\r\n":
                break
            # The LSP/JSON-RPC specification states that header names are case-insensitive.
            if line_str.lower().startswith("content-length:"):
                content_length = int(line_str.split(":")[1].strip())
        
        if content_length == 0:
            return None
        body = self.proc.stdout.read(content_length)
        if not body:
            return None
        # Decode body using errors='ignore' for robustness against malformed/unexpected bytes
        return json.loads(body.decode('utf-8', errors='ignore'))

    def _recv(self, timeout=0.05):
        try:
            return self.msg_queue.get(timeout=timeout)
        except queue.Empty:
            return None

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
        try:
            # Wrap send in try/except to gracefully handle cases where the LSP server
            # has crashed or disconnected, preventing a fatal BrokenPipeError or OSError.
            self._send(req)
        except (BrokenPipeError, OSError) as e:
            warn_once("lsp_send_error", f"LSP server error during send: {e}")
            return []
        self.req_id += 1

        start_time = time.time()
        while time.time() - start_time < timeout:
            res = self._recv(timeout=0.05)
            if not res:
                continue
            if "id" not in res: continue
            if res["id"] == req_id: return res.get("result", [])
                
        warn_once("lsp_timeout", f"LSP query timed out after {timeout}s. Increase --lsp-timeout if indexing takes longer.")
        return []


def cleanup_zombie_lsps():
    for client in LSP_INSTANCES.values():
        if client and client.proc:
            try:
                client._send({"jsonrpc": "2.0", "method": "exit"})
                client.proc.terminate()
                client.proc.wait(timeout=1)
            except Exception:
                try: client.proc.kill()
                except OSError: pass

atexit.register(cleanup_zombie_lsps)

def get_lsp_references(file_path, line_num, func_name, timeout, max_depth, disable_pruning, file_cache=None):
    global USE_LSP
    if not USE_LSP or line_num <= 0: return None
    if file_cache is None:
        file_cache = get_global_cache()
    
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

    lines = file_cache.get_lines(file_path)
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
        ref_path = url2pathname(urllib.parse.urlparse(ref.get("uri", "")).path)
        try: rel_path = os.path.relpath(ref_path, os.getcwd())
        except ValueError: rel_path = ref_path
            
        ref_line = ref["range"]["start"]["line"]
        ref_code = "[Code Unavailable]"
        if os.path.exists(rel_path):
            lines = file_cache.get_lines(rel_path)
            # Add a defensive bounds check to prevent IndexError if the file
            # has been modified or if the LSP returned an out-of-bounds line number.
            if 0 <= ref_line < len(lines):
                ref_code = lines[ref_line].strip()
        
        if rel_path not in callers: callers[rel_path] = []
        callers[rel_path].append({"line": ref_line + 1, "code": ref_code})

    if not disable_pruning and total_refs > max_depth:
        callers["[Pruned Instances]"] = [{"line": 0, "code": f"// Omitted {total_refs - max_depth} additional interface implementations to preserve context window."}]
        
    return callers
