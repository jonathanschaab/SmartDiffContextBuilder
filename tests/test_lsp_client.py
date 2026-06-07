import unittest
import asyncio
import time
import os
from io import BytesIO
from unittest.mock import MagicMock, patch, AsyncMock
from context_builder.lsp_client import MinimalLSPClient, get_lsp_references, cleanup_zombie_lsps, LSP_INSTANCES
import lsprotocol.types as types

class TestLspClient(unittest.TestCase):
    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_init_and_send(self, mock_lc_class):
        mock_client = MagicMock()
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
        
        async def mock_references_hang(*args, **kwargs):
            await asyncio.sleep(5.0)
            return []
        mock_client.text_document_references_async = mock_references_hang
        
        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client
        
        start = time.time()
        # Query with a very small timeout
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

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_json_decode_robustness(self, mock_lc_class):
        # pygls internally parses JSON, verify start still succeeds
        mock_client = MagicMock()
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
        
        refs = client.get_references("file.py", 10, 0, timeout=1.0)
        self.assertEqual(refs, [])

    @patch("context_builder.lsp_client.LanguageClient")
    def test_lsp_client_startup_timeout_returns_false(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        
        mock_client.start_io = AsyncMock()
        
        async def mock_initialize_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()
            
        mock_client.initialize_async = mock_initialize_timeout
        mock_client.shutdown_async = AsyncMock()
        mock_client.stop = AsyncMock()
        mock_client.stopped = False
        
        client = MinimalLSPClient(["some_lsp_binary"])
        success = client.start()
            
        self.assertFalse(success)
        self.assertIsNone(client.client)
        mock_client.shutdown_async.assert_called_once_with(None)
        mock_client.stop.assert_called_once()

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

    @patch("context_builder.lsp_client.LanguageClient")
    def test_cleanup_synchronous_fallback_force_kills(self, mock_lc_class):
        mock_client = MagicMock()
        mock_lc_class.return_value = mock_client
        
        mock_subproc = MagicMock()
        mock_subproc.returncode = None
        mock_client.subprocess = mock_subproc
        
        client = MinimalLSPClient(["some_lsp_binary"])
        client.client = mock_client
        
        def mock_run_coroutine_threadsafe(coro, loop):
            coro.close()
            raise RuntimeError("Loop closed")
            
        with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_run_coroutine_threadsafe):
            client.cleanup()
            
        mock_subproc.kill.assert_called_once()
        self.assertIsNone(client.client)



