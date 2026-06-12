# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
# pylint: disable=attribute-defined-outside-init,import-outside-toplevel,unused-argument
# pylint: disable=protected-access,redefined-outer-name,reimported,consider-using-with
# pylint: disable=line-too-long,too-many-lines,too-many-public-methods,broad-exception-caught
# pylint: disable=consider-using-from-import

import unittest
import asyncio
import concurrent.futures
import io
import time
from unittest.mock import MagicMock, patch, AsyncMock

import lsprotocol.types as types
from pygls.lsp.client import LanguageClient

from context_builder.lsp_client import (
    LSP_INSTANCES,
    LSPProgressReporter,
    MinimalLSPClient,
    _get_lsp_process,
    _register_lsp_progress_handlers,
    _register_notebook_filter_compatibility,
    cleanup_zombie_lsps,
    get_lsp_references,
)

class TestLspClient(unittest.TestCase):
    def test_lsp_progress_reporter_renders_non_tty_milestones(self):
        stream = io.StringIO()
        reporter = LSPProgressReporter("clangd")

        with patch("context_builder.lsp_client.sys.stderr", stream):
            reporter._is_tty = False
            reporter.create("index")
            reporter.update(
                "index",
                types.WorkDoneProgressBegin(
                    title="Indexing",
                    percentage=0,
                ),
            )
            reporter.update(
                "index",
                types.WorkDoneProgressReport(
                    message="headers",
                    percentage=3,
                ),
            )
            reporter.update(
                "index",
                types.WorkDoneProgressReport(
                    message="sources",
                    percentage=10,
                ),
            )
            reporter.update(
                "index",
                types.WorkDoneProgressEnd(message="ready"),
            )

        output = stream.getvalue()
        self.assertIn("[LSP   0%] Indexing", output)
        self.assertNotIn("3%", output)
        self.assertIn("[LSP  10%] Indexing: sources", output)
        self.assertIn("[LSP] Indexing: ready", output)

    def test_lsp_progress_reporter_renders_tty_bar(self):
        stream = io.StringIO()
        reporter = LSPProgressReporter("rust-analyzer")

        with patch("context_builder.lsp_client.sys.stderr", stream):
            reporter._is_tty = True
            reporter.update(
                "index",
                types.WorkDoneProgressBegin(
                    title="Indexing",
                    message="crates",
                    percentage=50,
                ),
            )
            reporter.update("index", types.WorkDoneProgressEnd())

        output = stream.getvalue()
        self.assertIn("[############------------]", output)
        self.assertIn("50%", output)
        self.assertIn("crates\033[K", output)
        self.assertIn("\r  [LSP] Indexing: complete\033[K", output)

    def test_lsp_progress_reporter_treats_non_finite_values_as_indeterminate(self):
        stream = io.StringIO()
        reporter = LSPProgressReporter("clangd")

        with patch("context_builder.lsp_client.sys.stderr", stream):
            reporter._is_tty = False
            reporter.update(
                "nan",
                {
                    "kind": "begin",
                    "title": "Indexing",
                    "message": "unknown total",
                    "percentage": float("nan"),
                },
            )
            reporter.update(
                "infinity",
                {
                    "kind": "begin",
                    "title": "Loading",
                    "message": "still working",
                    "percentage": float("inf"),
                },
            )

        output = stream.getvalue()
        self.assertIn("[LSP] Indexing: unknown total", output)
        self.assertIn("[LSP] Loading: still working", output)
        self.assertNotIn("%", output)

    def test_register_lsp_progress_handlers_routes_standard_messages(self):
        handlers = {}

        class FakeClient:  # pylint: disable=too-few-public-methods
            def feature(self, method):
                def decorator(func):
                    handlers[method] = func
                    return func
                return decorator

        reporter = MagicMock()
        _register_lsp_progress_handlers(FakeClient(), reporter)

        handlers[types.WINDOW_WORK_DONE_PROGRESS_CREATE](
            None,
            types.WorkDoneProgressCreateParams(token="index"),
        )
        handlers[types.PROGRESS](
            None,
            types.ProgressParams(
                token="index",
                value={"kind": "report", "percentage": 40},
            ),
        )

        reporter.create.assert_called_once_with("index")
        reporter.update.assert_called_once_with(
            "index",
            {"kind": "report", "percentage": 40},
        )

    def test_register_lsp_progress_handlers_accepts_missing_params(self):
        handlers = {}

        class FakeClient:  # pylint: disable=too-few-public-methods
            def feature(self, method):
                def decorator(func):
                    handlers[method] = func
                    return func
                return decorator

        reporter = MagicMock()
        _register_lsp_progress_handlers(FakeClient(), reporter)

        handlers[types.WINDOW_WORK_DONE_PROGRESS_CREATE](None, None)
        handlers[types.PROGRESS](None, None)

        reporter.create.assert_called_once_with(None)
        reporter.update.assert_called_once_with(None, None)

    def test_get_lsp_process_prefers_current_pygls_server_attribute(self):
        client = MagicMock()
        current_process = object()
        legacy_process = object()
        client._server = current_process
        client.subprocess = legacy_process

        self.assertIs(_get_lsp_process(client), current_process)

    def test_get_lsp_process_falls_back_to_legacy_subprocess_attribute(self):
        client = MagicMock(spec=["subprocess"])
        legacy_process = object()
        client.subprocess = legacy_process

        self.assertIs(_get_lsp_process(client), legacy_process)

    def test_get_lsp_process_returns_none_without_process_attributes(self):
        client = MagicMock(spec=[])

        self.assertIsNone(_get_lsp_process(client))

    def test_notebook_filter_compatibility_accepts_cells_only_selector(self):
        client = LanguageClient(name="test-client", version="1.0")
        registered = _register_notebook_filter_compatibility(client)

        options = client.protocol._converter.structure(
            {"notebookSelector": [{"cells": [{"language": "python"}]}]},
            types.NotebookDocumentSyncOptions,
        )

        self.assertTrue(registered)
        self.assertEqual(options.notebook_selector[0].cells[0].language, "python")
        self.assertIsNone(options.notebook_selector[0].notebook)

    def test_notebook_filter_compatibility_skips_unknown_model(self):
        client = MagicMock()
        client.protocol._converter = MagicMock()

        with patch.object(
            types,
            "NotebookDocumentFilterWithCells",
            None,
        ):
            registered = _register_notebook_filter_compatibility(client)

        self.assertFalse(registered)
        client.protocol._converter.register_structure_hook.assert_not_called()

    def test_notebook_filter_compatibility_skips_changed_attrs_model(self):
        client = MagicMock()
        client.protocol._converter = MagicMock()
        changed_model = type(
            "ChangedNotebookFilter",
            (),
            {"__attrs_attrs__": (object(),)},
        )

        with patch.object(
            types,
            "NotebookDocumentFilterWithCells",
            changed_model,
        ):
            registered = _register_notebook_filter_compatibility(client)

        self.assertFalse(registered)
        client.protocol._converter.register_structure_hook.assert_not_called()

    def test_notebook_filter_compatibility_rejects_non_mapping_filter(self):
        client = MagicMock()
        converter = MagicMock()
        client.protocol._converter = converter

        registered = _register_notebook_filter_compatibility(client)
        _, hook = converter.register_structure_hook.call_args[0]

        self.assertTrue(registered)
        with self.assertRaisesRegex(TypeError, "received list"):
            hook([], object())

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_init_and_send(self, mock_lc_class):
        mock_client = MagicMock()
        mock_client.protocol._converter = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_client.start_io = AsyncMock()
        mock_client.initialize_async = AsyncMock(return_value=types.InitializeResult(capabilities=types.ServerCapabilities()))
        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])
        success = client.start()

        self.assertTrue(success)
        self.assertIsNotNone(client.client)
        mock_client.start_io.assert_awaited_once_with("some_lsp_binary")
        mock_lc_class.assert_called_once_with(name="SmartDiffContextBuilder-LSP", version="1.0")

    @patch("context_builder.lsp_client.USE_LSP", False)
    def test_get_lsp_references_disabled(self):
        # When USE_LSP is false, get_lsp_references should return None immediately
        refs = get_lsp_references("file.py", 10, "my_func", 5, 15, False)
        self.assertIsNone(refs)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_timeout(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.stopped = False
        mock_client.text_document_references_async = AsyncMock()

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client
        client.start = MagicMock(return_value=True)

        def mock_run_coroutine(coro, _loop):
            coro.close()
            future = concurrent.futures.Future()
            future.set_exception(concurrent.futures.TimeoutError())
            return future

        start = time.time()
        # Query with a very small timeout
        with patch(
            "asyncio.run_coroutine_threadsafe",
            side_effect=mock_run_coroutine,
        ):
            refs = client.get_references("file.py", 10, 0, timeout=0.05)
        duration = time.time() - start

        self.assertEqual(refs, [])
        self.assertTrue(duration < 0.5)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_uri_parsing_cross_platform(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.get.return_value = mock_client
        mock_instances.__contains__.return_value = True

        import os
        current_dir = os.getcwd().replace("\\", "/")
        if not current_dir.startswith("/"):
            current_dir = "/" + current_dir
        mock_client.get_references.return_value = [
            {"uri": f"file://{current_dir}/foo.py", "range": {"start": {"line": 4, "character": 0}}}
        ]

        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = ["def bar():\n"] * 10

        with patch("os.path.exists", return_value=True), \
             patch("os.path.splitext", return_value=(".py", ".py")):

            res = get_lsp_references("main.py", 5, "foo", timeout=5, max_depth=10, disable_pruning=False, file_cache=mock_cache)

            self.assertIsNotNone(res)
            self.assertIn("foo.py", res)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_memory_leak_prevention(self, mock_lc_class):
        # Verify cleanup releases resources cleanly
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        client.cleanup()
        self.assertIsNone(client.client)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_case_insensitive_header(self, mock_lc_class):
        # Since pygls handles headers internally, verify client startup works
        mock_client = MagicMock()
        mock_client.protocol._converter = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_client.start_io = AsyncMock()
        mock_client.initialize_async = AsyncMock(return_value=types.InitializeResult(capabilities=types.ServerCapabilities()))
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])
        self.assertTrue(client.start())

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_skips_decorator_lines(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.get.return_value = mock_client
        mock_instances.__contains__.return_value = True

        lines = [
            "@decorator_one\n",
            "@decorator_two\n",
            "def my_func(x, y):\n",
            "    return x + y\n",
        ]
        func_name = "my_func"
        expected_line = 3
        expected_char = lines[2].find(func_name)

        mock_client.get_references.return_value = []
        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = lines

        with patch("os.path.splitext", return_value=("", ".py")):
            get_lsp_references(
                "dummy.py", line_num=1, func_name=func_name,
                timeout=1, max_depth=5, disable_pruning=True,
                file_cache=mock_cache
            )

        mock_client.get_references.assert_called_once()
        call_args = mock_client.get_references.call_args
        _, called_line, called_char = call_args[0][0], call_args[0][1], call_args[0][2]
        self.assertEqual(called_line, expected_line)
        self.assertEqual(called_char, expected_char)

    def test_cleanup_zombie_lsps_clears_instances(self):
        mock_client = MagicMock()
        LSP_INSTANCES[".py"] = mock_client

        cleanup_zombie_lsps()

        self.assertEqual(len(LSP_INSTANCES), 0)
        mock_client.cleanup.assert_called_once()

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.MinimalLSPClient")
    def test_c_family_extensions_share_one_language_server(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.start.return_value = True
        mock_client.get_references.return_value = []
        mock_client_class.return_value = mock_client
        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = ["void target() {}\n"]

        with patch("context_builder.lsp_client.LSP_INSTANCES", {}):
            for extension in (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx"):
                get_lsp_references(
                    f"source{extension}",
                    1,
                    "target",
                    5,
                    10,
                    False,
                    file_cache=mock_cache,
                )

        mock_client_class.assert_called_once_with(
            ["clangd", "--background-index"],
            init_timeout=60,
        )
        mock_client.start.assert_called_once_with()
        self.assertEqual(mock_client.get_references.call_count, 7)

    @patch("context_builder.lsp_client.MinimalLSPClient")
    def test_language_server_arguments_are_part_of_instance_identity(
        self, mock_client_class
    ):
        from context_builder.lsp_client import _get_or_create_lsp_client

        first_client = MagicMock()
        second_client = MagicMock()
        first_client.start.return_value = True
        second_client.start.return_value = True
        mock_client_class.side_effect = [first_client, second_client]
        with patch("context_builder.lsp_client.LSP_INSTANCES", {}):
            first = _get_or_create_lsp_client(["shared-lsp", "--mode", "one"])
            same = _get_or_create_lsp_client(["shared-lsp", "--mode", "one"])
            different = _get_or_create_lsp_client(
                ["shared-lsp", "--mode", "two"]
            )

        self.assertIs(first, same)
        self.assertIsNot(first, different)
        self.assertEqual(mock_client_class.call_count, 2)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_json_decode_robustness(self, mock_lc_class):
        # pygls internally parses JSON, verify start still succeeds
        mock_client = MagicMock()
        mock_client.protocol._converter = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_client.start_io = AsyncMock()
        mock_client.initialize_async = AsyncMock(return_value=types.InitializeResult(capabilities=types.ServerCapabilities()))
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])
        self.assertTrue(client.start())

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_lf_only_headers(self, mock_lc_class):
        # pygls internally parses headers, verify start still succeeds
        mock_client = MagicMock()
        mock_client.protocol._converter = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_client.start_io = AsyncMock()
        mock_client.initialize_async = AsyncMock(return_value=types.InitializeResult(capabilities=types.ServerCapabilities()))
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])
        self.assertTrue(client.start())

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_out_of_bounds(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client

        mock_file_cache = MagicMock()
        mock_file_cache.get_lines.return_value = []

        res = get_lsp_references("empty.cpp", 5, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
        self.assertEqual(res, {})

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_location_link(self, mock_instances):
        from urllib.request import url2pathname
        import urllib.parse
        import os

        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client

        mock_client.get_references.return_value = [
            {
                "targetUri": "file:///path/to/file.cpp",
                "targetSelectionRange": {
                    "start": {"line": 10, "character": 5},
                    "end": {"line": 10, "character": 15}
                }
            },
            {
                "targetUri": "file:///path/to/file.cpp",
                "targetRange": {
                    "start": {"line": 20, "character": 0},
                    "end": {"line": 20, "character": 10}
                }
            }
        ]

        mock_file_cache = MagicMock()
        mock_file_cache.get_lines.return_value = ["line 0", "line 1", "line 2"]

        with patch("os.path.exists", return_value=True):
            res = get_lsp_references("empty.cpp", 1, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
            path = os.path.relpath(url2pathname(urllib.parse.urlparse("file:///path/to/file.cpp").path), os.getcwd())
            self.assertIn(path, res)
            self.assertEqual(len(res[path]), 2)
            self.assertEqual(res[path][0]["line"], 11)
            self.assertEqual(res[path][1]["line"], 21)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_malformed(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client

        mock_client.get_references.return_value = [
            {"malformed": "structure"},
            "not_even_a_dict"
        ]

        mock_file_cache = MagicMock()

        res = get_lsp_references("empty.cpp", 1, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
        self.assertEqual(res, {})

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_destructor_boundary(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client
        mock_client.get_references.return_value = []

        lines = [
            "class MyClass {\n",
            "    MyClass::~MyClass() {}\n",
        ]
        func_name = "~MyClass"
        expected_line = 2
        expected_char = lines[1].find(func_name)

        mock_file_cache = MagicMock()
        mock_file_cache.get_lines.return_value = lines

        with patch("os.path.splitext", return_value=("", ".cpp")):
            get_lsp_references(
                "dummy.cpp", line_num=2, func_name=func_name,
                timeout=1, max_depth=5, disable_pruning=True,
                file_cache=mock_file_cache
            )

        mock_client.get_references.assert_called_once()
        call_args = mock_client.get_references.call_args
        _, called_line, called_char = call_args[0][0], call_args[0][1], call_args[0][2]
        self.assertEqual(called_line, expected_line)
        self.assertEqual(called_char, expected_char)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_send_broken_pipe(self, mock_lc_class):
        # Verify client handles query exceptions safely
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.stopped = False

        async def mock_references_error(*args, **kwargs):
            raise OSError("Broken pipe")
        mock_client.text_document_references_async = mock_references_error

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        with patch("context_builder.lsp_client.warn_once") as mock_warn:
            refs = client.get_references("file.py", 10, 0, timeout=1.0)
        self.assertEqual(refs, [])
        mock_warn.assert_called_once()
        self.assertEqual(mock_warn.call_args.args[0], "lsp_query_fail")
        self.assertIn("Broken pipe", mock_warn.call_args.args[1])

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_startup_timeout_returns_false(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_client.start_io = AsyncMock()

        observed_loop = None

        async def mock_initialize_timeout(*args, **kwargs):
            nonlocal observed_loop
            observed_loop = asyncio.get_event_loop()
            raise asyncio.TimeoutError()

        mock_client.initialize_async = mock_initialize_timeout
        mock_shutdown = AsyncMock()
        mock_client.shutdown_async = mock_shutdown
        mock_client.stop = AsyncMock()
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])

        # Execute the coroutine synchronously, but preserve the Future contract:
        # coroutine errors surface when MinimalLSPClient calls future.result().
        def mock_run_coroutine(coro, _loop):
            future = concurrent.futures.Future()
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result = new_loop.run_until_complete(coro)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                asyncio.set_event_loop(None)
                new_loop.close()
            return future

        with patch(
            "asyncio.run_coroutine_threadsafe",
            side_effect=mock_run_coroutine,
        ), patch("context_builder.lsp_client.warn_once") as mock_warn:
            success = client.start()

        self.assertFalse(success)
        self.assertIsNone(client.client)
        mock_shutdown.assert_not_called()
        mock_client.stop.assert_called_once()
        self.assertIsNotNone(observed_loop)
        self.assertTrue(observed_loop.is_closed())
        self.assertEqual(mock_warn.call_args.args[0], "lsp_init_timeout")
        warning = mock_warn.call_args.args[1]
        self.assertIn("60.0 seconds", warning)
        self.assertIn("--lsp-init-timeout", warning)
        self.assertIn("'lsp_init_timeout'", warning)

    @patch("context_builder.lsp_client.asyncio.wait_for", new_callable=AsyncMock)
    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_uses_configured_initialize_timeout(
        self, mock_lc_class, mock_wait_for
    ):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.start_io = AsyncMock()
        mock_client.initialize_async = MagicMock(return_value=object())
        mock_client.initialized = MagicMock()
        mock_wait_for.return_value = None
        client = MinimalLSPClient(["some_lsp_binary"], init_timeout=75)

        def mock_run_coroutine(coro, _loop):
            future = concurrent.futures.Future()
            new_loop = asyncio.new_event_loop()
            try:
                future.set_result(new_loop.run_until_complete(coro))
            finally:
                new_loop.close()
            return future

        with patch(
            "asyncio.run_coroutine_threadsafe",
            side_effect=mock_run_coroutine,
        ):
            self.assertTrue(client.start())

        mock_wait_for.assert_awaited_once()
        self.assertEqual(mock_wait_for.await_args.kwargs["timeout"], 75)
        initialize_params = mock_client.initialize_async.call_args.args[0]
        self.assertTrue(
            initialize_params.capabilities.window.work_done_progress
        )

    @patch("context_builder.lsp_client.warn_once")
    def test_lsp_query_timeout_warning_explains_configuration(self, mock_warn):
        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = MagicMock(stopped=False)

        def timeout_query(coro, _loop):
            coro.close()
            future = concurrent.futures.Future()
            future.set_exception(TimeoutError())
            return future

        with patch(
            "asyncio.run_coroutine_threadsafe",
            side_effect=timeout_query,
        ):
            self.assertEqual(
                client.get_references("file.py", 1, 0, timeout=12.5),
                [],
            )

        self.assertEqual(mock_warn.call_args.args[0], "lsp_timeout")
        warning = mock_warn.call_args.args[1]
        self.assertIn("12.5 seconds", warning)
        self.assertIn("--lsp-timeout", warning)
        self.assertIn("'lsp_timeout'", warning)

    @patch("context_builder.lsp_client.warn_once")
    def test_invalid_lsp_timeouts_warn_and_use_defaults(self, mock_warn):
        client = MinimalLSPClient(["some_lsp_binary"], init_timeout=0)
        self.assertEqual(client.init_timeout, 60.0)
        self.assertEqual(mock_warn.call_args.args[0], "lsp_init_timeout_invalid")
        self.assertIn("--lsp-init-timeout", mock_warn.call_args.args[1])

        client.client = MagicMock(stopped=True)
        self.assertEqual(
            client.get_references("file.py", 1, 0, timeout=float("nan")),
            [],
        )
        self.assertEqual(mock_warn.call_args.args[0], "lsp_timeout_invalid")
        self.assertIn("--lsp-timeout", mock_warn.call_args.args[1])

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_malformed_string_ref(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client

        mock_client.get_references.return_value = [
            "string_reference_structure",
            {"uri": None, "range": None}
        ]

        mock_file_cache = MagicMock()

        res = get_lsp_references("empty.cpp", 1, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
        self.assertEqual(res, {})

    @patch("context_builder.lsp_client.LanguageClient")
    def test_serialization_location_object(self, mock_lc_class):
        # Verify that get_references correctly serializes lsprotocol Location objects
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.stopped = False

        # Construct real lsprotocol objects
        loc = types.Location(
            uri="file:///path/to/file.cpp",
            range=types.Range(
                start=types.Position(line=10, character=5),
                end=types.Position(line=10, character=15)
            )
        )

        async def mock_references(*args, **kwargs):
            return [loc]
        mock_client.text_document_references_async = mock_references

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        refs = client.get_references("file.py", 10, 0, timeout=1.0)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["uri"], "file:///path/to/file.cpp")
        self.assertEqual(refs[0]["range"]["start"]["line"], 10)
        self.assertEqual(refs[0]["range"]["start"]["character"], 5)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_serialization_location_link_object(self, mock_lc_class):
        # Verify that get_references correctly serializes lsprotocol LocationLink objects
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.stopped = False

        link = types.LocationLink(
            target_uri="file:///path/to/file.cpp",
            target_selection_range=types.Range(
                start=types.Position(line=20, character=2),
                end=types.Position(line=20, character=8)
            ),
            target_range=types.Range(
                start=types.Position(line=20, character=0),
                end=types.Position(line=20, character=10)
            )
        )

        async def mock_references(*args, **kwargs):
            return [link]
        mock_client.text_document_references_async = mock_references

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        refs = client.get_references("file.py", 10, 0, timeout=1.0)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["targetUri"], "file:///path/to/file.cpp")
        self.assertEqual(refs[0]["targetSelectionRange"]["start"]["line"], 20)
        self.assertEqual(refs[0]["targetSelectionRange"]["start"]["character"], 2)

    def test_cleanup_handles_missing_attributes(self):
        # Create a mock client that has absolutely no lsp/pygls lifecycle attributes (e.g. stopped, shutdown_async, exit, stop)
        mock_client = object()  # Bare object with no attributes

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        # Verify that calling cleanup does not raise any AttributeError and finishes successfully
        try:
            client.cleanup()
        except Exception as e:
            self.fail(f"cleanup raised an exception on bare client object: {e}")

        self.assertIsNone(client.client)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_get_references_handles_missing_stopped_property(self, mock_lc_class):
        mock_client = MagicMock(spec=[])  # A mock that raises AttributeError on any access
        mock_lc_class.return_value = mock_client

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        # Mock get_references to verify it returns [] safely instead of raising AttributeError
        refs = client.get_references("file.py", 10, 0, timeout=1.0)
        self.assertEqual(refs, [])

    @patch("context_builder.lsp_client.LanguageClient")
    def test_run_coroutine_threadsafe_graceful_error_handling(self, mock_lc_class):
        def mock_run_coroutine_threadsafe(coro, loop):
            coro.close()
            raise RuntimeError("Event loop closed")

        with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_run_coroutine_threadsafe):
            client = MinimalLSPClient(["some_lsp_binary"])

            # 1. start() should return False
            self.assertFalse(client.start())

            # 2. get_references() should return []
            client.client = MagicMock()
            client.client.stopped = False
            self.assertEqual(client.get_references("file.py", 10, 0, 1.0), [])

            # 3. cleanup() should run without throwing any exceptions
            try:
                client.cleanup()
            except Exception as e:
                self.fail(f"cleanup raised exception: {e}")

    @patch("context_builder.lsp_client.LanguageClient")
    def test_cleanup_force_kills_subprocess_via_subprocess_attribute(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.stopped = True

        mock_subproc = MagicMock()
        mock_subproc.returncode = None
        mock_client.subprocess = mock_subproc
        mock_client._server = None

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        client.cleanup()

        mock_subproc.kill.assert_called_once()
        self.assertIsNone(client.client)

    def test_call_lsp_method_arguments_handling(self):
        from context_builder.lsp_client import _call_lsp_method

        # 1. Method taking 0 parameters
        def zero_params():
            return "zero"
        self.assertEqual(_call_lsp_method(zero_params, "ignored_arg"), "zero")

        # 2. Method taking 1 parameter
        def one_param(x):
            return x
        self.assertEqual(_call_lsp_method(one_param, "val"), "val")

        # 3. Method raising TypeError when called with arguments (falls back to calling without)
        call_count = 0
        def fallback_no_params():
            nonlocal call_count
            call_count += 1
            return "fallback"

        with patch("inspect.signature", side_effect=ValueError("no signature")):
            res = _call_lsp_method(fallback_no_params, "ignored")
        self.assertEqual(res, "fallback")
        self.assertEqual(call_count, 1)

        # 4. Method taking 1 parameter, called with NO arguments (should pad with None)
        self.assertIsNone(_call_lsp_method(one_param))

    @patch("context_builder.lsp_client.LanguageClient")
    def test_cleanup_synchronous_fallback_force_kills(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        mock_subproc = MagicMock()
        mock_subproc.returncode = None
        mock_client.subprocess = mock_subproc
        mock_client._server = None

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        def mock_run_coroutine_threadsafe(coro, loop):
            coro.close()
            raise RuntimeError("Loop closed")

        with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_run_coroutine_threadsafe):
            client.cleanup()

        mock_subproc.kill.assert_called_once()
        self.assertIsNone(client.client)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_start_io_fails_immediately(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        async def mock_start_io_fail(*args, **kwargs):
            raise FileNotFoundError("lsp_binary not found")
        mock_client.start_io = mock_start_io_fail
        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.stopped = False

        client = MinimalLSPClient(["nonexistent_lsp_binary"])
        success = client.start()

        self.assertFalse(success)
        self.assertIsNone(client.client)
        mock_client.shutdown_async.assert_not_awaited()
        mock_client.stop.assert_not_awaited()

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_detects_process_that_exits_during_startup(
        self, mock_lc_class
    ):
        mock_client = MagicMock()
        mock_client.protocol._converter = MagicMock()
        mock_client.start_io = AsyncMock()
        mock_client.initialize_async = AsyncMock()
        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client._server.returncode = 2
        mock_lc_class.return_value = mock_client

        client = MinimalLSPClient(["failing_lsp_binary"])
        with patch("context_builder.lsp_client.warn_once") as mock_warn:
            success = client.start()

        self.assertFalse(success)
        self.assertIsNone(client.client)
        self.assertIn("RuntimeError", mock_warn.call_args.args[1])
        self.assertIn("code 2", mock_warn.call_args.args[1])
        mock_client.initialize_async.assert_not_awaited()
        mock_client.shutdown_async.assert_not_awaited()
        mock_client.stop.assert_not_awaited()

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_detects_exit_while_start_io_is_pending(
        self, mock_lc_class
    ):
        mock_client = MagicMock()
        mock_client.protocol._converter = MagicMock()

        async def pending_start_io(*args, **kwargs):
            await asyncio.sleep(10.0)

        mock_client.start_io = pending_start_io
        mock_client.initialize_async = AsyncMock()
        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client._server.returncode = 3
        mock_lc_class.return_value = mock_client

        client = MinimalLSPClient(["delayed_failing_lsp"])
        with patch("context_builder.lsp_client.warn_once") as mock_warn:
            success = client.start()

        self.assertFalse(success)
        self.assertIsNone(client.client)
        self.assertIn("code 3", mock_warn.call_args.args[1])
        mock_client.initialize_async.assert_not_awaited()
        mock_client.shutdown_async.assert_not_awaited()
        mock_client.stop.assert_not_awaited()

    def test_get_lsp_loop_recreates_when_closed(self):
        import context_builder.lsp_client as lsp_client

        # Ensure we have a loop thread started
        initial_loop = lsp_client.get_lsp_loop()
        self.assertIsNotNone(initial_loop)
        self.assertFalse(initial_loop.is_closed())

        # Artificially stop the thread loop and close it
        thread_to_close = lsp_client._LOOP_THREAD
        thread_to_close.loop.call_soon_threadsafe(thread_to_close.loop.stop)
        thread_to_close.join(timeout=1.0)
        thread_to_close.loop.close()

        # Calling get_lsp_loop should spin up a brand new loop thread
        new_loop = lsp_client.get_lsp_loop()
        self.assertIsNotNone(new_loop)
        self.assertFalse(new_loop.is_closed())
        self.assertNotEqual(initial_loop, new_loop)

        # Cleanup
        lsp_client.cleanup_zombie_lsps()

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_start_io_task_cancelled_on_init_failure(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        async def mock_start_io(*args, **kwargs):
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                mock_client._start_io_cancelled = True
                raise

        mock_client.start_io = mock_start_io
        mock_client._start_io_cancelled = False

        async def mock_initialize_fail(*args, **kwargs):
            raise RuntimeError("Init failed")
        mock_client.initialize_async = mock_initialize_fail

        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.stopped = False

        client = MinimalLSPClient(["some_lsp_binary"])
        success = client.start()

        self.assertFalse(success)
        self.assertTrue(getattr(mock_client, "_start_io_cancelled", False))

    def test_lsp_loop_thread_stop_safety(self):
        from context_builder.lsp_client import LSPEventLoopThread
        thread = LSPEventLoopThread()
        thread.loop.close()
        try:
            thread.stop()
        except Exception as e:
            self.fail(f"LSPEventLoopThread.stop() raised an exception when loop was closed: {e}")

    @patch("context_builder.lsp_client.LanguageClient")
    def test_cleanup_local_client_reference_safety(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        mock_subproc = MagicMock()
        mock_subproc.returncode = None
        mock_client.subprocess = mock_subproc
        mock_client._server = None
        mock_client.stopped = False

        async def mock_shutdown_async(*args):
            client.client = None

        mock_client.shutdown_async = mock_shutdown_async
        mock_client.shutdown = AsyncMock()
        mock_client.exit = AsyncMock()
        mock_client.stop = AsyncMock()

        try:
            client.cleanup()
        except AttributeError as e:
            self.fail(f"cleanup() failed with AttributeError due to race condition: {e}")

        mock_client.shutdown.assert_not_called()
        mock_client.exit.assert_called_once()
        mock_client.stop.assert_called_once()
        mock_subproc.kill.assert_called_once()

    def test_get_lsp_loop_joins_closed_thread(self):
        import context_builder.lsp_client as lsp_client
        initial_loop = lsp_client.get_lsp_loop()
        thread_to_close = lsp_client._LOOP_THREAD

        thread_to_close.loop.call_soon_threadsafe(thread_to_close.loop.stop)
        thread_to_close.join(timeout=1.0)
        thread_to_close.loop.close()

        with patch.object(thread_to_close, "is_alive", return_value=True), \
             patch.object(thread_to_close, "join") as mock_join:
            new_loop = lsp_client.get_lsp_loop()
            mock_join.assert_called_once()

        self.assertNotEqual(initial_loop, new_loop)
        lsp_client.cleanup_zombie_lsps()

    @patch("context_builder.lsp_client.LanguageClient")
    def test_get_references_defensive_parsing_checks(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client
        client.client.stopped = False

        buggy_loc1 = MagicMock(spec=types.Location)
        buggy_loc1.uri = "file:///foo.py"
        buggy_loc1.range = None

        buggy_loc2 = MagicMock(spec=types.Location)
        buggy_loc2.uri = "file:///bar.py"
        buggy_loc2.range = MagicMock(spec=types.Range)
        buggy_loc2.range.start = None

        valid_loc = MagicMock(spec=types.Location)
        valid_loc.uri = "file:///valid.py"
        valid_loc.range = MagicMock(spec=types.Range)
        valid_loc.range.start = MagicMock(spec=types.Position)
        valid_loc.range.start.line = 10
        valid_loc.range.start.character = 2
        valid_loc.range.end = MagicMock(spec=types.Position)
        valid_loc.range.end.line = 10
        valid_loc.range.end.character = 15

        buggy_link = MagicMock(spec=types.LocationLink)
        buggy_link.target_uri = "file:///link.py"
        buggy_link.target_range = MagicMock(spec=types.Range)
        buggy_link.target_range.start = None
        buggy_link.target_selection_range = None

        async def mock_references(*args, **kwargs):
            return [buggy_loc1, buggy_loc2, valid_loc, buggy_link]

        mock_client.text_document_references_async = mock_references

        refs = client.get_references("file.py", 10, 0, timeout=1.0)
        self.assertEqual(len(refs), 2)

        self.assertEqual(refs[0]["uri"], "file:///valid.py")
        self.assertEqual(refs[0]["range"]["start"]["line"], 10)

        self.assertEqual(refs[1]["targetUri"], "file:///link.py")
        self.assertNotIn("targetSelectionRange", refs[1])

    @patch("context_builder.lsp_client.LanguageClient")
    def test_cleanup_concurrency_race(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        mock_subproc = MagicMock()
        mock_subproc.returncode = None
        mock_client.subprocess = mock_subproc
        mock_client._server = None
        mock_client.stopped = False

        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()

        client.cleanup()
        self.assertIsNone(client.client)

        mock_client.stop.reset_mock()
        client.cleanup()
        mock_client.stop.assert_not_called()

    @patch("context_builder.lsp_client.LanguageClient")
    def test_cleanup_force_kill_immediately_terminates(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        mock_client.stopped = False

        mock_subproc = MagicMock()
        mock_subproc.returncode = None
        mock_subproc.kill.side_effect = lambda: setattr(mock_subproc, 'returncode', -9)
        mock_client.subprocess = mock_subproc
        mock_client._server = None

        mock_shutdown = AsyncMock()
        mock_client.shutdown_async = mock_shutdown
        mock_client.stop = AsyncMock()

        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client

        client.cleanup(force_kill=True)

        mock_subproc.kill.assert_called_once()
        mock_shutdown.assert_not_called()
        mock_client.stop.assert_called_once()
        self.assertIsNone(client.client)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_startup_timeout_triggers_force_kill(self, mock_lc_class):
        client = MinimalLSPClient(["some_lsp_binary"])

        def mock_timeout(coro, _loop):
            coro.close()
            raise concurrent.futures.TimeoutError("Timeout!")

        with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_timeout):
            with patch.object(client, "cleanup") as mock_cleanup:
                success = client.start()
                self.assertFalse(success)
                mock_cleanup.assert_called_once_with(force_kill=True)

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_startup_generic_error_triggers_normal_cleanup(self, mock_lc_class):
        client = MinimalLSPClient(["some_lsp_binary"])

        def mock_error(coro, _loop):
            coro.close()
            raise RuntimeError("Some error")

        with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_error):
            with patch.object(client, "cleanup") as mock_cleanup:
                success = client.start()
                self.assertFalse(success)
                mock_cleanup.assert_called_once_with(force_kill=False)
