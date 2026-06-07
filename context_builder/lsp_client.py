import os
import re
import time
import json
import atexit
import subprocess
import urllib.parse
from pathlib import Path
from urllib.request import url2pathname
import queue
import threading
import asyncio
import inspect
from pygls.lsp.client import LanguageClient
import lsprotocol.types as types
from .sys_utils import warn_once
from .cache import get_global_cache

USE_LSP = True
LSP_INSTANCES = {}

class LSPEventLoopThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()
        
    def run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass
        
    def stop(self):
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass

_LOOP_THREAD = None
_LOOP_LOCK = threading.Lock()

def get_lsp_loop():
    global _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP_THREAD is None or not _LOOP_THREAD.is_alive() or _LOOP_THREAD.loop.is_closed():
            _LOOP_THREAD = LSPEventLoopThread()
            _LOOP_THREAD.start()
        return _LOOP_THREAD.loop

def _call_lsp_method(method, *args):
    try:
        sig = inspect.signature(method)
        params = list(sig.parameters.values())
        required_params = [
            p for p in params 
            if p.default == inspect.Parameter.empty 
            and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if len(required_params) == 0:
            return method()
        
        call_args = list(args)
        while len(call_args) < len(required_params):
            call_args.append(None)
            
        return method(*call_args[:len(required_params)])
    except Exception:
        try:
            return method(*args)
        except TypeError:
            try:
                return method()
            except TypeError:
                if len(args) > 0:
                    return method(args[0])
                return method(None)

class MinimalLSPClient:
    def __init__(self, cmd):
        self.cmd = cmd
        self.client = None
        self.loop = get_lsp_loop()

    def start(self) -> bool:
        async def _async_start():
            self.client = LanguageClient(name="SmartDiffContextBuilder-LSP", version="1.0")
            # Start subprocess in a background task to avoid blocking the message loop
            start_task = asyncio.create_task(
                self.client.start_io(self.cmd[0], *self.cmd[1:], stderr=asyncio.subprocess.DEVNULL)
            )
            try:
                # Yield control briefly to allow the subprocess to start or fail immediately
                await asyncio.sleep(0.1)
                if start_task.done():
                    # Raise any immediate startup exception (e.g. FileNotFoundError)
                    start_task.result()
                
                # Send initialize request
                params = types.InitializeParams(
                    process_id=os.getpid(),
                    root_uri=Path('.').absolute().as_uri(),
                    capabilities=types.ClientCapabilities()
                )
                # 10 second timeout for initialization
                await asyncio.wait_for(self.client.initialize_async(params), timeout=10.0)
                
                # Send initialized notification
                self.client.initialized(types.InitializedParams())
                return True
            except BaseException:
                # Cancel the start_task to avoid leaking subprocesses / message loops
                start_task.cancel()
                try:
                    await start_task
                except Exception:
                    pass
                except asyncio.CancelledError:
                    pass
                raise

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_start(), self.loop)
            return fut.result(timeout=11.0)
        except Exception as e:
            if 'fut' in locals():
                fut.cancel()
            warn_once("lsp_fail", f"Failed to start LSP {self.cmd[0]}: {e}")
            self.cleanup()
            return False

    def get_references(self, file_path, line_num, char_num, timeout) -> list:
        if not self.client or getattr(self.client, "stopped", False):
            return []

        async def _async_get_refs():
            params = types.ReferenceParams(
                context=types.ReferenceContext(include_declaration=False),
                text_document=types.TextDocumentIdentifier(uri=Path(file_path).absolute().as_uri()),
                position=types.Position(line=line_num - 1, character=char_num)
            )
            refs = await self.client.text_document_references_async(params)
            if not refs:
                return []
            
            # Serialize types.Location and types.LocationLink to standard dicts
            serialized = []
            for ref in refs:
                if isinstance(ref, dict):
                    serialized.append(ref)
                    continue
                # Location
                if hasattr(ref, "uri") and hasattr(ref, "range"):
                    serialized.append({
                        "uri": ref.uri,
                        "range": {
                            "start": {
                                "line": ref.range.start.line,
                                "character": ref.range.start.character
                            },
                            "end": {
                                "line": ref.range.end.line,
                                "character": ref.range.end.character
                            }
                        }
                    })
                    continue
                # LocationLink
                target_uri = getattr(ref, "target_uri", None) or getattr(ref, "targetUri", None)
                target_range = getattr(ref, "target_range", None) or getattr(ref, "targetRange", None)
                target_selection_range = getattr(ref, "target_selection_range", None) or getattr(ref, "targetSelectionRange", None)
                if target_uri:
                    res = {"targetUri": target_uri}
                    rng = target_selection_range or target_range
                    if rng:
                        res["targetSelectionRange"] = {
                            "start": {
                                "line": rng.start.line,
                                "character": rng.start.character
                            },
                            "end": {
                                "line": rng.end.line,
                                "character": rng.end.character
                            }
                        }
                    serialized.append(res)
            return serialized

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_get_refs(), self.loop)
            return fut.result(timeout=timeout)
        except Exception as e:
            if 'fut' in locals():
                fut.cancel()
            warn_once("lsp_timeout", f"LSP query timed out after {timeout}s or failed: {e}")
            return []

    def cleanup(self):
        client = self.client
        if not client:
            return

        async def _async_cleanup():
            # Check stopped status safely
            is_stopped = getattr(client, "stopped", False)
            if not is_stopped:
                # Try shutdown_async
                if hasattr(client, "shutdown_async"):
                    try:
                        res = _call_lsp_method(client.shutdown_async)
                        if inspect.isawaitable(res) or asyncio.isfuture(res):
                            await asyncio.wait_for(res, timeout=2.0)
                    except Exception:
                        pass
                elif hasattr(client, "shutdown"):
                    try:
                        res = _call_lsp_method(client.shutdown)
                        if inspect.isawaitable(res) or asyncio.isfuture(res):
                            await asyncio.wait_for(res, timeout=2.0)
                    except Exception:
                        pass
                
                # Try exit
                if hasattr(client, "exit"):
                    try:
                        res = _call_lsp_method(client.exit)
                        if inspect.isawaitable(res) or asyncio.isfuture(res):
                            await res
                    except Exception:
                        pass
                
                # Try stop
                if hasattr(client, "stop"):
                    try:
                        res = _call_lsp_method(client.stop)
                        if inspect.isawaitable(res) or asyncio.isfuture(res):
                            await asyncio.wait_for(res, timeout=2.0)
                    except Exception:
                        pass
            
            # Force kill subprocess if still running
            server = getattr(client, "subprocess", None) or getattr(client, "_server", None)
            if server and server.returncode is None:
                try:
                    server.kill()
                except Exception:
                    pass

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_cleanup(), self.loop)
            fut.result(timeout=2.0)
        except Exception:
            if 'fut' in locals():
                fut.cancel()
            # Synchronous fallback: force-kill the subprocess directly from the calling thread
            try:
                server = getattr(client, "subprocess", None) or getattr(client, "_server", None)
                if server and server.returncode is None:
                    server.kill()
            except Exception:
                pass
        self.client = None

def cleanup_zombie_lsps():
    for client in LSP_INSTANCES.values():
        if client:
            try:
                client.cleanup()
            except Exception:
                pass
    LSP_INSTANCES.clear()
    
    # Stop the loop thread
    global _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP_THREAD and _LOOP_THREAD.is_alive():
            _LOOP_THREAD.stop()
            _LOOP_THREAD.join(timeout=1.0)
            _LOOP_THREAD = None

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
        '.hpp': ["clangd", "--background-index"],
        '.h': ["clangd", "--background-index"],
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
    if line_num > len(lines):
        # Must return an empty dictionary to match callers mapping type and avoid AttributeErrors in callers.items() iteration
        return {}

    # line_num points to the start of the function definition block, which may be a
    # decorator (e.g. @my_decorator).  Scan forward up to DECORATOR_LOOKAHEAD lines
    # to find the actual line that contains func_name so the LSP cursor lands on it.
    DECORATOR_LOOKAHEAD = 10
    actual_line = line_num  # 1-based
    char_idx = -1
    # Use a word-boundary regex instead of str.find() so that a short func_name
    # (e.g. "run") cannot match inside a longer identifier (e.g. "runner" or
    # "decorator_run"), which would produce an incorrect character offset and
    # send the LSP cursor to the wrong symbol.
    #
    # We dynamically apply the word boundary \b constraint. If the adjacent character
    # in func_name is a non-word character (e.g. C++ destructor starting with '~' or C++
    # operators), applying \b would fail if it is preceded or followed by a non-word
    # character like a colon or space (e.g. MyClass::~MyClass or operator++).
    lead_b = r'\b' if func_name and (func_name[0].isalnum() or func_name[0] == '_') else ''
    trail_b = r'\b' if func_name and (func_name[-1].isalnum() or func_name[-1] == '_') else ''
    func_name_pattern = re.compile(lead_b + re.escape(func_name) + trail_b)
    for offset in range(DECORATOR_LOOKAHEAD):
        candidate_idx = line_num - 1 + offset  # 0-based
        if candidate_idx >= len(lines):
            break
        m = func_name_pattern.search(lines[candidate_idx])
        if m:
            actual_line = line_num + offset
            char_idx = m.start()
            break
    if char_idx == -1:
        # func_name not found in any nearby line; fall back to the original line at col 0
        actual_line = line_num
        char_idx = 0

    print(f" [LSP] Querying {cmd[0]} for {func_name}() references...")
    refs = client.get_references(file_path, actual_line, char_idx, timeout=timeout)
    
    callers = {}
    total_refs = len(refs)
    
    # Heuristic Pruning & Depth Limiting
    if not disable_pruning and total_refs > max_depth:
        refs = refs[:max_depth]
        warn_once(f"prune_{func_name}", f"Polymorphic explosion detected for {func_name}. Pruning to {max_depth} callers.")

    for ref in refs:
        # Wrap in try...except block to handle potentially malformed responses, missing keys,
        # or alternative LSP structures (like LocationLink) without crashing the scan.
        try:
            ref_uri = ref.get("uri") or ref.get("targetUri", "")
            ref_path = url2pathname(urllib.parse.urlparse(ref_uri).path)
            try: rel_path = os.path.relpath(ref_path, os.getcwd())
            except ValueError: rel_path = ref_path
                
            range_obj = ref.get("range") or ref.get("targetSelectionRange") or ref.get("targetRange")
            if not range_obj or "start" not in range_obj or "line" not in range_obj["start"]:
                raise KeyError("range/start/line")
            ref_line = range_obj["start"]["line"]
            
            ref_code = "[Code Unavailable]"
            if os.path.exists(rel_path):
                lines = file_cache.get_lines(rel_path)
                # Add a defensive bounds check to prevent IndexError if the file
                # has been modified or if the LSP returned an out-of-bounds line number.
                if 0 <= ref_line < len(lines):
                    ref_code = lines[ref_line].strip()
            
            if rel_path not in callers: callers[rel_path] = []
            callers[rel_path].append({"line": ref_line + 1, "code": ref_code})
        except (KeyError, TypeError, AttributeError) as exc:
            warn_once("lsp_ref_malformed", f"Skipping malformed LSP reference structure: {exc}")

    if not disable_pruning and total_refs > max_depth:
        callers["[Pruned Instances]"] = [{"line": 0, "code": f"// Omitted {total_refs - max_depth} additional interface implementations to preserve context window."}]
        
    return callers
