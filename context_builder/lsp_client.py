"""Module lsp_client provides a minimal Language Server Protocol (LSP) client.

It handles starting language server subprocesses, querying them for references,
and managing event loop threads for async communication.
"""

import atexit
import asyncio
import inspect
import math
import os
import re
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
import threading
from urllib.request import url2pathname

from lsprotocol import types
from pygls.lsp.client import LanguageClient

from .cache import get_global_cache
from .config import DEFAULT_LSP_INIT_TIMEOUT, DEFAULT_LSP_QUERY_TIMEOUT
from .languages import get_language_profile
from .sys_utils import warn_once

USE_LSP = True
LSP_INSTANCES = {}
_LSP_PROGRESS_BAR_WIDTH = 24


def _is_timeout_error(exc):
    """Return whether an exception represents an asyncio/future timeout."""
    return isinstance(exc, TimeoutError)


def _validate_lsp_timeout(value, default, config_key, cli_option):
    """Validate a timeout and provide the same recovery guidance as ripgrep."""
    is_valid = (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )
    if is_valid:
        return value
    warn_once(
        f"{config_key}_invalid",
        f"Configured {config_key} ({value}) must be a positive number. "
        f"Falling back to {default} seconds. You can set this limit using "
        f"{cli_option} or by setting '{config_key}' in your config file.",
    )
    return default


def _progress_field(value, name, default=None):
    """Read a work-done progress field from protocol objects or raw mappings."""
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class LSPProgressReporter:
    """Render standard LSP work-done progress without requiring server-specific APIs."""

    def __init__(self, server_name):
        self.server_name = server_name
        self._states = {}
        self._lock = threading.Lock()
        try:
            self._is_tty = bool(sys.stderr.isatty())
        except Exception:  # pylint: disable=broad-exception-caught
            self._is_tty = False

    def create(self, token):
        """Record a server-created progress token."""
        with self._lock:
            self._states.setdefault(token, {})

    def update(self, token, value):
        """Render one begin, report, or end progress notification."""
        kind = _progress_field(value, "kind")
        with self._lock:
            state = self._states.setdefault(token, {})
            if kind == "begin":
                state["title"] = _progress_field(
                    value, "title", f"{self.server_name} indexing"
                )
                state["last_bucket"] = -1
                state["last_message"] = None
                self._render(state, value)
            elif kind == "report":
                self._render(state, value)
            elif kind == "end":
                self._finish(state, value)
                self._states.pop(token, None)

    def _render(self, state, value):
        title = state.get("title", f"{self.server_name} indexing")
        message = _progress_field(value, "message") or ""
        percentage = _progress_field(value, "percentage")
        if (
            not isinstance(percentage, bool)
            and isinstance(percentage, (int, float))
            and math.isfinite(percentage)
        ):
            percentage = max(0, min(100, int(percentage)))
            bucket = percentage // 5
            if self._is_tty:
                filled = (percentage * _LSP_PROGRESS_BAR_WIDTH) // 100
                progress_bar = (
                    "#" * filled
                    + "-" * (_LSP_PROGRESS_BAR_WIDTH - filled)
                )
                print(
                    f"\r  [{progress_bar}] {percentage:3d}%  [LSP] {title}"
                    f"{': ' + message if message else ''}\033[K",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
            elif bucket != state.get("last_bucket"):
                print(
                    f"  [LSP {percentage:3d}%] {title}"
                    f"{': ' + message if message else ''}",
                    file=sys.stderr,
                )
            state["last_bucket"] = bucket
        elif message != state.get("last_message"):
            print(
                f"  [LSP] {title}{': ' + message if message else ''}",
                file=sys.stderr,
            )
        state["last_message"] = message

    def _finish(self, state, value):
        title = state.get("title", f"{self.server_name} indexing")
        message = _progress_field(value, "message") or "complete"
        prefix = "\r" if self._is_tty else ""
        clear_line = "\033[K" if self._is_tty else ""
        print(
            f"{prefix}  [LSP] {title}: {message}{clear_line}",
            file=sys.stderr,
            flush=self._is_tty,
        )


def _register_lsp_progress_handlers(client, reporter):
    """Register standard work-done progress request and notification handlers."""
    @client.feature(types.WINDOW_WORK_DONE_PROGRESS_CREATE)
    def create_progress(_client, params):
        reporter.create(_progress_field(params, "token"))

    @client.feature(types.PROGRESS)
    def report_progress(_client, params):
        reporter.update(
            _progress_field(params, "token"),
            _progress_field(params, "value"),
        )


def _register_notebook_filter_compatibility(client):
    """Register a narrow workaround for lsprotocol's optional-filter hook gap."""
    # pygls 2.1.1 pins lsprotocol 2025.0.0, which exposes this attrs model.
    # Feature detection is still intentional: if a future pygls/lsprotocol
    # release changes its model or converter internals, skipping our workaround
    # is safer than preventing every language server from starting. The upstream
    # converter may no longer need the workaround in that environment.
    filter_with_cells = getattr(types, "NotebookDocumentFilterWithCells", None)
    filter_fields = getattr(filter_with_cells, "__attrs_attrs__", ())
    notebook_field = next(
        (
            field
            for field in filter_fields
            if getattr(field, "name", None) == "notebook"
        ),
        None,
    )
    notebook_field_type = getattr(notebook_field, "type", None)
    protocol = getattr(client, "protocol", None)
    converter = getattr(protocol, "_converter", None)
    register_hook = getattr(converter, "register_structure_hook", None)
    structure = getattr(converter, "structure", None)
    filter_types = {
        "notebookType": getattr(
            types, "NotebookDocumentFilterNotebookType", None
        ),
        "scheme": getattr(types, "NotebookDocumentFilterScheme", None),
        "pattern": getattr(types, "NotebookDocumentFilterPattern", None),
    }
    if (
        notebook_field_type is None
        or converter is None
        or not callable(register_hook)
        or not callable(structure)
        or any(filter_type is None for filter_type in filter_types.values())
    ):
        return False

    def structure_notebook_filter(value, _type):
        if value is None or isinstance(value, str):
            return value
        if not isinstance(value, Mapping):
            # Do not silently coerce malformed server capabilities. A clear
            # conversion error is preferable to registering the wrong filter.
            raise TypeError(
                "Notebook filter must be a string, mapping, or None; "
                f"received {type(value).__name__}"
            )
        if "notebookType" in value:
            filter_type = filter_types["notebookType"]
        elif "scheme" in value:
            filter_type = filter_types["scheme"]
        else:
            filter_type = filter_types["pattern"]
        return structure(value, filter_type)

    register_hook(
        notebook_field_type,
        structure_notebook_filter,
    )
    return True


def _get_lsp_process(client):
    """Return the subprocess object exposed by the active pygls client."""
    # pygls 2.1.1, our current minimum, stores the process in the private
    # `_server` attribute. Older releases and some compatible client wrappers
    # expose it as `subprocess` instead. Centralizing that version boundary keeps
    # startup crash detection and every cleanup path from drifting apart.
    return getattr(client, "_server", None) or getattr(client, "subprocess", None)


def _kill_lsp_process(client):
    """Best-effort termination across supported pygls process attributes."""
    try:
        server = _get_lsp_process(client)
        if server and server.returncode is None:
            server.kill()
    except Exception:  # pylint: disable=broad-exception-caught
        pass


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

    def __init__(self, cmd, init_timeout=DEFAULT_LSP_INIT_TIMEOUT):
        """Initialize the client with language server launch command.

        Args:
            cmd (list): Subprocess command list.
            init_timeout (float): Maximum seconds for the initialize handshake.
        """
        self.cmd = cmd
        self.init_timeout = _validate_lsp_timeout(
            init_timeout,
            DEFAULT_LSP_INIT_TIMEOUT,
            "lsp_init_timeout",
            "--lsp-init-timeout",
        )
        self.client = None
        self.progress = LSPProgressReporter(cmd[0])
        self.loop = get_lsp_loop()

    def start(self) -> bool:
        """Start the language server subprocess and perform initialize handshake.

        Returns:
            bool: True if initialized successfully, False otherwise.
        """
        process_start_failed = False

        async def _async_start():
            nonlocal process_start_failed
            self.client = LanguageClient(name="SmartDiffContextBuilder-LSP", version="1.0")
            _register_notebook_filter_compatibility(self.client)
            _register_lsp_progress_handlers(self.client, self.progress)
            # pygls owns stdin/stdout/stderr for its JSON-RPC transport and
            # captures stderr in a pipe. Supplying stderr here is not needed to
            # keep the console clean and breaks pygls 2.x by passing the keyword
            # twice to asyncio.create_subprocess_exec.
            start_task = asyncio.create_task(
                self.client.start_io(self.cmd[0], *self.cmd[1:])
            )
            try:
                await asyncio.sleep(0.1)
                if start_task.done():
                    # start_io completing is healthy: it means pygls spawned the
                    # process and installed background transport tasks. It does
                    # not mean the language-server process itself has exited.
                    try:
                        start_task.result()
                    except BaseException:
                        process_start_failed = True
                        raise

                # Process state is independent of start_io task completion. A
                # compatible client may keep start_io pending briefly while its
                # subprocess has already crashed, so check the process on every
                # startup pass instead of waiting for the task to finish.
                server = _get_lsp_process(self.client)
                returncode = getattr(server, "returncode", None)
                if isinstance(returncode, int):
                    # asyncio subprocess return codes are integers. Checking
                    # the process directly avoids waiting for initialize_async
                    # to time out against a transport that is already dead.
                    process_start_failed = True
                    raise RuntimeError(
                        f"LSP process exited during startup with code {returncode}"
                    )

                params = types.InitializeParams(
                    process_id=os.getpid(),
                    root_uri=Path(".").absolute().as_uri(),
                    capabilities=types.ClientCapabilities(
                        window=types.WindowClientCapabilities(
                            work_done_progress=True
                        )
                    ),
                )
                await asyncio.wait_for(
                    self.client.initialize_async(params),
                    timeout=self.init_timeout,
                )
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
            return fut.result(timeout=self.init_timeout + 1.0)
        except Exception as e:  # pylint: disable=broad-exception-caught
            if "fut" in locals():
                fut.cancel()
            is_timeout = _is_timeout_error(e)
            if is_timeout:
                warn_once(
                    "lsp_init_timeout",
                    f"LSP {self.cmd[0]} initialization timed out after "
                    f"{self.init_timeout} seconds. You can increase this limit "
                    "using --lsp-init-timeout or by setting 'lsp_init_timeout' "
                    "in your config file.",
                )
            else:
                warn_once(
                    "lsp_fail",
                    f"Failed to start LSP {self.cmd[0]} "
                    f"({type(e).__name__}): {e}",
                )
            if process_start_failed:
                # No transport exists yet, so LSP shutdown notifications would
                # only produce secondary "no available transport" errors.
                self.client = None
            else:
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
        timeout = _validate_lsp_timeout(
            timeout,
            DEFAULT_LSP_QUERY_TIMEOUT,
            "lsp_timeout",
            "--lsp-timeout",
        )
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
            if _is_timeout_error(e):
                warn_once(
                    "lsp_timeout",
                    f"LSP query timed out after {timeout} seconds. You can "
                    "increase this limit using --lsp-timeout or by setting "
                    "'lsp_timeout' in your config file.",
                )
            else:
                warn_once(
                    "lsp_query_fail",
                    f"LSP query failed ({type(e).__name__}): {e}",
                )
            return []

    def cleanup(self, force_kill=False):
        """Shutdown the language client and terminate the server subprocess."""
        client = self.client
        if not client:
            return
        self.client = None

        if force_kill:
            _kill_lsp_process(client)
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

            _kill_lsp_process(client)

        try:
            fut = asyncio.run_coroutine_threadsafe(_async_cleanup(), self.loop)
            fut.result(timeout=2.0)
        except Exception:  # pylint: disable=broad-exception-caught
            if "fut" in locals():
                fut.cancel()
            _kill_lsp_process(client)


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


def _find_lsp_func_start_character_ast(
    lines, line_num, func_name, ext, file_path, file_cache, decorator_lookahead
):
    """Attempt to locate function identifier starting character index using AST parsing."""
    # pylint: disable=import-outside-toplevel
    from .ast_engine import AST_ENGINE, HAS_TREESITTER

    if not (HAS_TREESITTER and AST_ENGINE.is_supported(ext)):
        return -1, line_num

    try:
        source_bytes = file_cache.get_bytes(file_path)
        tree = AST_ENGINE.parsers[ext].parse(source_bytes)
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

        query = AST_ENGINE.languages[ext].query(q_str)
        captures = query.captures(tree.root_node)
        for capture_node, _ in captures:
            node_text = source_bytes[
                capture_node.start_byte:capture_node.end_byte
            ].decode("utf-8", errors="ignore")
            if node_text != func_name:
                continue
            node_row = capture_node.start_point[0]
            if node_row < (line_num - 1) or node_row >= (line_num - 1 + decorator_lookahead):
                continue
            line_str = lines[node_row]
            prefix_bytes = line_str.encode("utf-8")[:capture_node.start_point[1]]
            prefix_str = prefix_bytes.decode("utf-8", errors="ignore")
            char_idx = len(prefix_str.encode("utf-16-le")) // 2
            actual_line = node_row + 1
            return char_idx, actual_line
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    return -1, line_num


def _find_lsp_func_start_character_regex(
    lines, line_num, func_name, decorator_lookahead
):
    """Attempt to locate function identifier starting character index using regex search."""
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
            prefix_str = lines[candidate_idx][:m.start()]
            char_idx = len(prefix_str.encode("utf-16-le")) // 2
            return char_idx, actual_line
    return -1, line_num


def _find_lsp_func_start_character(
    lines, line_num, func_name, ext=None, file_path=None, file_cache=None
):
    """Scan line and decorators to find func name starting character index.

    Returns:
        tuple: (actual_line, char_idx) where char_idx is the UTF-16 character
               offset of the function name, or -1 if the function name could not
               be confidently located (signaling the caller to abort the LSP query).
    """
    decorator_lookahead = 10

    # Try AST-based matching for C++ and Rust if tree-sitter is available and supported
    if ext and file_path and file_cache:
        char_idx, actual_line = _find_lsp_func_start_character_ast(
            lines, line_num, func_name, ext, file_path, file_cache, decorator_lookahead
        )
        if char_idx != -1:
            return actual_line, char_idx

    # Regex-based fallback
    char_idx, actual_line = _find_lsp_func_start_character_regex(
        lines, line_num, func_name, decorator_lookahead
    )
    if char_idx != -1:
        return actual_line, char_idx

    return line_num, -1


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


def _get_lsp_instance_key(command):
    """Identify one language-server invocation within the current project."""
    project_root = os.path.normcase(os.path.abspath(os.getcwd()))
    return project_root, tuple(command)


def _get_or_create_lsp_client(
    command, init_timeout=DEFAULT_LSP_INIT_TIMEOUT
):
    """Retrieve or start a client shared by identical server invocations."""
    instance_key = _get_lsp_instance_key(command)
    if instance_key not in LSP_INSTANCES:
        client = MinimalLSPClient(command, init_timeout=init_timeout)
        LSP_INSTANCES[instance_key] = client if client.start() else None
    return LSP_INSTANCES.get(instance_key)


def _count_parts(p):
    """Count components in a normalized relative path using system path separator."""
    if not p or p == ".":
        return 0
    return len(p.split(os.sep))


def _sort_references_by_closeness(refs, target_file_path):
    """Sort a list of LSP reference objects to prioritize those close to the target file.

    Priority order:
    1. Same file as target (category 0)
    2. Same directory as target (category 1)
    3. Closer directories in the directory tree (category 2, ordered by directory distance)
    4. Distant directories/files (category 3)
    5. Malformed/missing path (category 4)

    Within each category, Python's stable sort preserves the original order.
    """
    target_abs = os.path.normcase(os.path.abspath(target_file_path))
    target_dir = os.path.dirname(target_abs)

    distance_cache = {}

    # pylint: disable=too-many-return-statements
    def get_distance(ref):
        if not isinstance(ref, dict):
            return (4, 0)
        ref_uri = ref.get("uri") or ref.get("targetUri", "")
        if not ref_uri:
            return (4, 0)

        if ref_uri in distance_cache:
            return distance_cache[ref_uri]

        try:
            parsed = urllib.parse.urlparse(ref_uri)
            if parsed.scheme != "file":
                distance_cache[ref_uri] = (4, 0)
                return (4, 0)
            ref_path = url2pathname(parsed.path)
            ref_abs = os.path.normcase(os.path.abspath(ref_path))

            if ref_abs == target_abs:
                distance_cache[ref_uri] = (0, 0)
                return (0, 0)

            ref_dir = os.path.dirname(ref_abs)
            if ref_dir == target_dir:
                distance_cache[ref_uri] = (1, 0)
                return (1, 0)

            # Compute distance in directory hierarchy
            try:
                common = os.path.commonpath([target_dir, ref_dir])
                rel_target = os.path.relpath(target_dir, common)
                rel_ref = os.path.relpath(ref_dir, common)

                dist = _count_parts(rel_target) + _count_parts(rel_ref)
                res = (2, dist)
            except ValueError:
                # Different drives on Windows
                res = (3, 0)
            distance_cache[ref_uri] = res
            return res
        except Exception:  # pylint: disable=broad-exception-caught
            distance_cache[ref_uri] = (4, 0)
            return (4, 0)

    refs.sort(key=get_distance)


def get_lsp_references(
    file_path,
    line_num,
    func_name,
    timeout,
    max_depth,
    disable_pruning,
    file_cache=None,
    init_timeout=DEFAULT_LSP_INIT_TIMEOUT,
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
        init_timeout (float): Language-server initialize timeout.

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

    client = _get_or_create_lsp_client(command, init_timeout=init_timeout)
    if not client:
        return None

    lines = file_cache.get_lines(file_path)
    if line_num > len(lines):
        return {}

    actual_line, char_idx = _find_lsp_func_start_character(
        lines,
        line_num,
        func_name,
        ext=ext,
        file_path=file_path,
        file_cache=file_cache,
    )

    # If the character offset cannot be confidently located, we abort the LSP
    # query. Querying the LSP at index 0 (which usually points to whitespace,
    # indentation, access modifiers, or return types) causes slow queries
    # and returns massive, incorrect references (contaminating context).
    # Aborting here allows graph_tracer to safely fall back to lexical tracing.
    if char_idx == -1:
        warn_once(
            f"lsp_abort_{func_name}_{file_path}",
            f"Could not locate character offset for '{func_name}' in {file_path} "
            f"on line {actual_line}. Aborting LSP query to prevent incorrect references; "
            f"falling back to lexical analysis.",
        )
        return None

    print(f" [LSP] Querying {command[0]} for {func_name}() references...")
    refs = client.get_references(file_path, actual_line, char_idx, timeout=timeout)

    callers = {}
    total_refs = len(refs)

    if not disable_pruning and total_refs > max_depth:
        _sort_references_by_closeness(refs, file_path)
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
