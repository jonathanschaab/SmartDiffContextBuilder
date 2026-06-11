"""Module lsp_client provides a minimal Language Server Protocol (LSP) client.

It handles starting language server subprocesses, querying them for references,
and managing event loop threads for async communication.
"""

import atexit
import asyncio
import concurrent.futures
import inspect
import os
import re
import urllib.parse
from pathlib import Path
import threading
from urllib.request import url2pathname

from lsprotocol import types
from pygls.lsp.client import LanguageClient

from .cache import get_global_cache
from .languages import get_language_profile
from .sys_utils import warn_once

USE_LSP = True
LSP_INSTANCES = {}


class LSPEventLoopThread(threading.Thread):
    """A background thread hosting an asyncio event loop for language client communication."""

    def __init__(self):
        """Initialize the background event loop thread."""
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()

    def run(self):
        """Run the event loop until stopped."""
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    def stop(self):
        """Stop the running event loop."""
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass


_LOOP_LOCK = threading.Lock()
_LOOP_THREAD = None


def get_lsp_loop():
    """Retrieve or spawn the background event loop for LSP communication.

    Returns:
        asyncio.AbstractEventLoop: The event loop instance.
    """
    with _LOOP_LOCK:
        thread = globals().get("_LOOP_THREAD")
        if thread is not None and (thread.loop.is_closed() or not thread.is_alive()):
            try:
                if thread.is_alive():
                    thread.stop()
                    thread.join(timeout=1.0)
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            globals()["_LOOP_THREAD"] = None

        if globals().get("_LOOP_THREAD") is None:
            new_thread = LSPEventLoopThread()
            new_thread.start()
            globals()["_LOOP_THREAD"] = new_thread
        return globals()["_LOOP_THREAD"].loop


def _call_lsp_method(method, *args):
    """Helper to call pygls client methods dynamically using inspected signature match.

    Args:
        method (callable): The pygls client method.
        *args: Variable length argument list.

    Returns:
        Any: Result of calling the method.
    """
    try:
        sig = inspect.signature(method)
        params = list(sig.parameters.values())
        required_params = [
            p
            for p in params
            if p.default == inspect.Parameter.empty
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if len(required_params) == 0:
            return method()

        call_args = list(args)
        while len(call_args) < len(required_params):
            call_args.append(None)

        return method(*call_args[: len(required_params)])
    except Exception:  # pylint: disable=broad-exception-caught
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
    """A minimal LSP client managing initialize handshake and text document reference queries."""

    def __init__(self, cmd):
        """Initialize the client with language server launch command.

        Args:
            cmd (list): Subprocess command list.
        """
        self.cmd = cmd
        self.client = None
        self.loop = get_lsp_loop()

    def start(self) -> bool:
        """Start the language server subprocess and perform initialize handshake.

        Returns:
            bool: True if initialized successfully, False otherwise.
        """

        async def _async_start():
            self.client = LanguageClient(name="SmartDiffContextBuilder-LSP", version="1.0")
            start_task = asyncio.create_task(
                self.client.start_io(
                    self.cmd[0], *self.cmd[1:], stderr=asyncio.subprocess.DEVNULL
                )
            )
            try:
                await asyncio.sleep(0.1)
                if start_task.done():
                    start_task.result()

                params = types.InitializeParams(
                    process_id=os.getpid(),
                    root_uri=Path(".").absolute().as_uri(),
                    capabilities=types.ClientCapabilities(),
                )
                await asyncio.wait_for(self.client.initialize_async(params), timeout=10.0)
                self.client.initialized(types.InitializedParams())
                return True
            except BaseException:
                start_task.cancel()
                try:
                    await start_task
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
                except asyncio.CancelledError:
                    pass
                raise

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_start(), self.loop)
            return fut.result(timeout=11.0)
        except Exception as e:  # pylint: disable=broad-exception-caught
            if "fut" in locals():
                fut.cancel()
            warn_once("lsp_fail", f"Failed to start LSP {self.cmd[0]}: {e}")
            is_timeout = isinstance(
                e,
                (TimeoutError, asyncio.TimeoutError, concurrent.futures.TimeoutError),
            )
            self.cleanup(force_kill=is_timeout)
            return False

    def get_references(self, file_path, line_num, char_num, timeout) -> list:
        """Query references for a symbol at a specific file, line, and column position.

        Args:
            file_path (str): Path to file.
            line_num (int): 1-based line index.
            char_num (int): Column offset index.
            timeout (float): Query timeout.

        Returns:
            list: List of reference locations.
        """
        if not self.client or getattr(self.client, "stopped", False):
            return []

        async def _async_get_refs():
            params = types.ReferenceParams(
                context=types.ReferenceContext(include_declaration=False),
                text_document=types.TextDocumentIdentifier(
                    uri=Path(file_path).absolute().as_uri()
                ),
                position=types.Position(line=line_num - 1, character=char_num),
            )
            refs = await self.client.text_document_references_async(params)
            if not refs:
                return []

            serialized = []
            for ref in refs:
                if isinstance(ref, dict):
                    serialized.append(ref)
                    continue

                uri = getattr(ref, "uri", None)
                rng_obj = getattr(ref, "range", None)
                if uri and rng_obj:
                    start_pos = getattr(rng_obj, "start", None)
                    end_pos = getattr(rng_obj, "end", None)
                    if start_pos and end_pos:
                        start_line = getattr(start_pos, "line", None)
                        start_char = getattr(start_pos, "character", None)
                        end_line = getattr(end_pos, "line", None)
                        end_char = getattr(end_pos, "character", None)
                        if (
                            start_line is not None
                            and start_char is not None
                            and end_line is not None
                            and end_char is not None
                        ):
                            serialized.append({
                                "uri": uri,
                                "range": {
                                    "start": {
                                        "line": start_line,
                                        "character": start_char,
                                    },
                                    "end": {"line": end_line, "character": end_char},
                                },
                            })
                            continue

                target_uri = (
                    getattr(ref, "target_uri", None)
                    or getattr(ref, "targetUri", None)
                )
                target_range = (
                    getattr(ref, "target_range", None)
                    or getattr(ref, "targetRange", None)
                )
                target_selection_range = (
                    getattr(ref, "target_selection_range", None)
                    or getattr(ref, "targetSelectionRange", None)
                )
                if target_uri:
                    res = {"targetUri": target_uri}
                    rng = target_selection_range or target_range
                    if rng:
                        start_pos = getattr(rng, "start", None)
                        end_pos = getattr(rng, "end", None)
                        if start_pos and end_pos:
                            start_line = getattr(start_pos, "line", None)
                            start_char = getattr(start_pos, "character", None)
                            end_line = getattr(end_pos, "line", None)
                            end_char = getattr(end_pos, "character", None)
                            if (
                                start_line is not None
                                and start_char is not None
                                and end_line is not None
                                and end_char is not None
                            ):
                                res["targetSelectionRange"] = {
                                    "start": {
                                        "line": start_line,
                                        "character": start_char,
                                    },
                                    "end": {"line": end_line, "character": end_char},
                                }
                    serialized.append(res)
            return serialized

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_get_refs(), self.loop)
            return fut.result(timeout=timeout)
        except Exception as e:  # pylint: disable=broad-exception-caught
            if "fut" in locals():
                fut.cancel()
            warn_once(
                "lsp_timeout", f"LSP query timed out after {timeout}s or failed: {e}"
            )
            return []

    def cleanup(self, force_kill=False):
        """Shutdown the language client and terminate the server subprocess."""
        client = self.client
        if not client:
            return
        self.client = None

        if force_kill:
            try:
                server = getattr(client, "subprocess", None) or getattr(
                    client, "_server", None
                )
                if server and server.returncode is None:
                    server.kill()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            for method in ["shutdown_async", "shutdown", "exit"]:
                if hasattr(client, method):
                    try:
                        setattr(client, method, lambda *args, **kwargs: None)
                    except AttributeError:
                        pass

        async def _async_cleanup():
            is_stopped = getattr(client, "stopped", False)
            if not is_stopped:
                if hasattr(client, "shutdown_async"):
                    await _async_clean_method(client, "shutdown_async")
                elif hasattr(client, "shutdown"):
                    await _async_clean_method(client, "shutdown")
                await _async_clean_method(client, "exit", use_wait_for=False)
                await _async_clean_method(client, "stop")

            server = getattr(client, "subprocess", None) or getattr(
                client, "_server", None
            )
            if server and server.returncode is None:
                try:
                    server.kill()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_cleanup(), self.loop)
            fut.result(timeout=2.0)
        except Exception:  # pylint: disable=broad-exception-caught
            if "fut" in locals():
                fut.cancel()
            try:
                server = getattr(client, "subprocess", None) or getattr(
                    client, "_server", None
                )
                if server and server.returncode is None:
                    server.kill()
            except Exception:  # pylint: disable=broad-exception-caught
                pass


async def _async_clean_method(client, method_name, use_wait_for=True):
    """Invoke clean method on the client if it exists."""
    if hasattr(client, method_name):
        try:
            res = _call_lsp_method(getattr(client, method_name))
            if inspect.isawaitable(res) or asyncio.isfuture(res):
                if use_wait_for:
                    await asyncio.wait_for(res, timeout=2.0)
                else:
                    await res
        except Exception:  # pylint: disable=broad-exception-caught
            pass



def cleanup_zombie_lsps():
    """Shut down all language server instances and terminate background threads."""
    for client in LSP_INSTANCES.values():
        if client:
            try:
                client.cleanup()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
    LSP_INSTANCES.clear()

    with _LOOP_LOCK:
        thread = globals().get("_LOOP_THREAD")
        if thread and thread.is_alive():
            thread.stop()
            thread.join(timeout=1.0)
            globals()["_LOOP_THREAD"] = None


atexit.register(cleanup_zombie_lsps)


def _find_lsp_func_start_character(lines, line_num, func_name):
    """Scan line and decorators to find func name starting character index."""
    decorator_lookahead = 10
    actual_line = line_num
    char_idx = -1

    lead_b = r"\b" if func_name and (func_name[0].isalnum() or func_name[0] == "_") else ""
    trail_b = r"\b" if func_name and (func_name[-1].isalnum() or func_name[-1] == "_") else ""
    func_name_pattern = re.compile(lead_b + re.escape(func_name) + trail_b)
    for offset in range(decorator_lookahead):
        candidate_idx = line_num - 1 + offset
        if candidate_idx >= len(lines):
            break
        m = func_name_pattern.search(lines[candidate_idx])
        if m:
            actual_line = line_num + offset
            char_idx = m.start()
            break
    if char_idx == -1:
        actual_line = line_num
        char_idx = 0
    return actual_line, char_idx


def _parse_single_lsp_reference(ref, file_cache):
    """Extract path, line, and code from a raw LSP reference object."""
    ref_uri = ref.get("uri") or ref.get("targetUri", "")
    ref_path = url2pathname(urllib.parse.urlparse(ref_uri).path)
    try:
        rel_path = os.path.relpath(ref_path, os.getcwd())
    except ValueError:
        rel_path = ref_path

    range_obj = (
        ref.get("range")
        or ref.get("targetSelectionRange")
        or ref.get("targetRange")
    )
    if (
        not range_obj
        or "start" not in range_obj
        or "line" not in range_obj["start"]
    ):
        raise KeyError("range/start/line")
    ref_line = range_obj["start"]["line"]

    ref_code = "[Code Unavailable]"
    if os.path.exists(rel_path):
        lines = file_cache.get_lines(rel_path)
        if 0 <= ref_line < len(lines):
            ref_code = lines[ref_line].strip()
    return rel_path, ref_line, ref_code


def _get_or_create_lsp_client(ext, configs):
    """Retrieve or start the MinimalLSPClient for a given file extension."""
    if ext not in LSP_INSTANCES:
        cmd = configs[ext]
        client = MinimalLSPClient(cmd)
        LSP_INSTANCES[ext] = client if client.start() else None
    return LSP_INSTANCES.get(ext)


def get_lsp_references(
    file_path,
    line_num,
    func_name,
    timeout,
    max_depth,
    disable_pruning,
    file_cache=None,
):
    """Find references using the active LSP.

    Args:
        file_path (str): File path to query.
        line_num (int): 1-based start line of function.
        func_name (str): Function name.
        timeout (float): Query timeout.
        max_depth (int): Max number of references to parse.
        disable_pruning (bool): Disable reference pruning.
        file_cache (LRUFileCache, optional): Cache instance.

    Returns:
        dict: Mapping of relative file paths to reference lists.
    """
    if not USE_LSP or line_num <= 0:
        return None
    if file_cache is None:
        file_cache = get_global_cache()

    ext = os.path.splitext(file_path)[1].lower()
    profile = get_language_profile(ext)
    if not profile.lsp_command:
        return None
    command = list(profile.lsp_command)

    client = _get_or_create_lsp_client(ext, {ext: command})
    if not client:
        return None

    lines = file_cache.get_lines(file_path)
    if line_num > len(lines):
        return {}

    actual_line, char_idx = _find_lsp_func_start_character(lines, line_num, func_name)

    print(f" [LSP] Querying {command[0]} for {func_name}() references...")
    refs = client.get_references(file_path, actual_line, char_idx, timeout=timeout)

    callers = {}
    total_refs = len(refs)

    if not disable_pruning and total_refs > max_depth:
        refs = refs[:max_depth]
        warn_once(
            f"prune_{func_name}",
            f"Polymorphic explosion detected for {func_name}. Pruning to {max_depth} callers.",
        )

    for ref in refs:
        try:
            rel_path, ref_line, ref_code = _parse_single_lsp_reference(ref, file_cache)
            if rel_path not in callers:
                callers[rel_path] = []
            callers[rel_path].append({"line": ref_line + 1, "code": ref_code})
        except (KeyError, TypeError, AttributeError) as exc:
            warn_once("lsp_ref_malformed", f"Skipping malformed LSP reference: {exc}")

    if not disable_pruning and total_refs > max_depth:
        callers["[Pruned Instances]"] = [{
            "line": 0,
            "code": (
                f"// Omitted {total_refs - max_depth} additional interface "
                f"implementations to preserve context window."
            ),
        }]

    return callers
