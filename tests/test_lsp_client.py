import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch
from context_builder.lsp_client import MinimalLSPClient, get_lsp_references

class TestLspClient(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_lsp_client_init_and_send(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        # Set stdout to a BytesIO stream
        mock_proc.stdout = BytesIO(b"Content-Length: 45\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}")

        client = MinimalLSPClient(["some_lsp_binary"])
        success = client.start()

        self.assertTrue(success)
        self.assertEqual(client.req_id, 2)
        mock_proc.stdin.write.assert_called()

    @patch("context_builder.lsp_client.USE_LSP", False)
    def test_get_lsp_references_disabled(self):
        # When USE_LSP is false, get_lsp_references should return None immediately
        refs = get_lsp_references("file.py", 10, "my_func", 5, 15, False)
        self.assertIsNone(refs)

    @patch("subprocess.Popen")
    def test_lsp_client_timeout(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.stdout = BytesIO(b"")

        client = MinimalLSPClient(["some_lsp_binary"])
        client.proc = mock_proc
        
        import time
        start = time.time()
        refs = client.get_references("file.py", 10, "my_func", timeout=0.05)
        duration = time.time() - start
        
        self.assertEqual(refs, [])
        self.assertTrue(duration < 0.5)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_uri_parsing_cross_platform(self, mock_instances):
        mock_client = MagicMock()
        mock_instances.get.return_value = mock_client
        mock_instances.__contains__.return_value = True

        # Test Windows URI format
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
            
            # Call get_lsp_references
            res = get_lsp_references("main.py", 5, "foo", timeout=5, max_depth=10, disable_pruning=False, file_cache=mock_cache)
            
            self.assertIsNotNone(res)
            self.assertIn("foo.py", res)

    @patch("subprocess.Popen")
    def test_lsp_client_memory_leak_prevention(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        init_response = b"Content-Length: 45\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}"
        notification = b"Content-Length: 60\r\n\r\n{\"jsonrpc\":\"2.0\",\"method\":\"textDocument/publishDiagnostics\"}"
        response = b"Content-Length: 45\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"ok\":true}}"
        mock_proc.stdout = BytesIO(init_response + notification + response)

        client = MinimalLSPClient(["some_lsp_binary"])
        client.proc = mock_proc
        
        # We manually trigger start to spin up the Popen and background loop
        client.start()
        
        import time
        start = time.time()
        while client.msg_queue.empty() and time.time() - start < 1:
            time.sleep(0.01)
            
        self.assertEqual(client.msg_queue.qsize(), 1)
        queued_msg = client.msg_queue.get()
        self.assertEqual(queued_msg.get("id"), 2)

    @patch("subprocess.Popen")
    def test_lsp_client_case_insensitive_header(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        # Test with a lowercase header name: 'content-length' instead of 'Content-Length'
        init_response = b"content-length: 45\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}"
        mock_proc.stdout = BytesIO(init_response)

        client = MinimalLSPClient(["some_lsp_binary"])
        
        # Start the client. If it correctly parses 'content-length:', it will read
        # the init_response, match id 1, and return True.
        success = client.start()
        self.assertTrue(success)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_skips_decorator_lines(self, mock_instances):
        """When line_num points at a decorator (@my_decorator) rather than the
        actual 'def' line, get_lsp_references must scan forward to find the
        line containing func_name so the LSP receives the correct character
        offset instead of defaulting to column 0 on the decorator."""
        mock_client = MagicMock()
        mock_instances.get.return_value = mock_client
        mock_instances.__contains__.return_value = True

        # func_name is on line 3 (0-indexed: index 2), preceded by two decorators.
        # line_num=1 means the first line (index 0) is where we start scanning.
        lines = [
            "@decorator_one\n",         # line 1 (1-based) — decorator
            "@decorator_two\n",         # line 2
            "def my_func(x, y):\n",     # line 3 — actual def
            "    return x + y\n",
        ]
        func_name = "my_func"
        expected_line = 3    # 1-based line where func_name lives
        expected_char = lines[2].find(func_name)   # character offset within that line

        mock_client.get_references.return_value = []

        mock_cache = MagicMock()
        mock_cache.get_lines.return_value = lines

        import os
        with patch("os.path.splitext", return_value=("", ".py")):
            get_lsp_references(
                "dummy.py", line_num=1, func_name=func_name,
                timeout=1, max_depth=5, disable_pruning=True,
                file_cache=mock_cache
            )

        # Verify the LSP was called with the corrected line and character offset
        mock_client.get_references.assert_called_once()
        call_args = mock_client.get_references.call_args
        _, called_line, called_char = call_args[0][0], call_args[0][1], call_args[0][2]
        self.assertEqual(called_line, expected_line,
                         f"Expected LSP to be queried at line {expected_line}, got {called_line}")
        self.assertEqual(called_char, expected_char,
                         f"Expected char offset {expected_char}, got {called_char}")

    @patch("subprocess.Popen")
    def test_cleanup_zombie_lsps_clears_instances(self, mock_popen):
        from context_builder.lsp_client import LSP_INSTANCES, cleanup_zombie_lsps
        mock_client = MagicMock()
        mock_client.proc = MagicMock()
        LSP_INSTANCES[".py"] = mock_client
        
        cleanup_zombie_lsps()
        
        self.assertEqual(len(LSP_INSTANCES), 0)
        mock_client._send.assert_called_with({"jsonrpc": "2.0", "method": "exit"})
        mock_client.proc.terminate.assert_called_once()

    @patch("subprocess.Popen")
    def test_lsp_client_json_decode_robustness(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        # We need an init response first so client.start() completes instantly
        init_response = b"Content-Length: 45\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}"
        # Test with a malformed JSON message followed by a valid one.
        # {invalid} has exactly 9 bytes, so Content-Length must be 9 to avoid stream desync.
        malformed_msg = b"Content-Length: 9\r\n\r\n{invalid}"
        valid_msg = b"Content-Length: 45\r\n\r\n{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"ok\":true}}"
        mock_proc.stdout = BytesIO(init_response + malformed_msg + valid_msg)

        client = MinimalLSPClient(["some_lsp_binary"])
        client.proc = mock_proc
        
        # Start the thread loop
        client.start()
        
        import time
        start = time.time()
        # It should skip the malformed one (since json.loads raises exception and returns {})
        # and queue the valid one (which contains id 3)
        while client.msg_queue.empty() and time.time() - start < 1:
            time.sleep(0.01)
            
        self.assertEqual(client.msg_queue.qsize(), 1)
        queued_msg = client.msg_queue.get()
        self.assertEqual(queued_msg.get("id"), 3)

    @patch("subprocess.Popen")
    def test_lsp_client_lf_only_headers(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        # We need an init response first so client.start() completes instantly
        init_response = b"Content-Length: 45\n\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"ok\":true}}"
        # Test with headers separated by \n instead of \r\n
        msg = b"Content-Length: 45\n\n{\"jsonrpc\":\"2.0\",\"id\":4,\"result\":{\"ok\":true}}"
        mock_proc.stdout = BytesIO(init_response + msg)

        client = MinimalLSPClient(["some_lsp_binary"])
        client.proc = mock_proc
        
        client.start()
        
        import time
        start = time.time()
        while client.msg_queue.empty() and time.time() - start < 1:
            time.sleep(0.01)
            
        self.assertEqual(client.msg_queue.qsize(), 1)
        queued_msg = client.msg_queue.get()
        self.assertEqual(queued_msg.get("id"), 4)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_out_of_bounds(self, mock_instances):
        from context_builder.lsp_client import get_lsp_references
        
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client
        
        mock_file_cache = MagicMock()
        mock_file_cache.get_lines.return_value = []
        
        # Query line 5 (out of bounds). It must return {} instead of [] to avoid AttributeError
        res = get_lsp_references("empty.cpp", 5, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
        self.assertEqual(res, {})

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_location_link(self, mock_instances):
        from context_builder.lsp_client import get_lsp_references
        from urllib.request import url2pathname
        import urllib.parse
        import os
        
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client
        
        # Mocking references list containing LocationLink structures
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
            # Both LocationLinks should be processed successfully
            path = os.path.relpath(url2pathname(urllib.parse.urlparse("file:///path/to/file.cpp").path), os.getcwd())
            self.assertIn(path, res)
            self.assertEqual(len(res[path]), 2)
            self.assertEqual(res[path][0]["line"], 11)
            self.assertEqual(res[path][1]["line"], 21)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_malformed(self, mock_instances):
        from context_builder.lsp_client import get_lsp_references
        
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client
        
        # Mocking references list with malformed structures
        mock_client.get_references.return_value = [
            {"malformed": "structure"},
            "not_even_a_dict"
        ]
        
        mock_file_cache = MagicMock()
        
        # It should skip both without throwing exceptions and return an empty dict
        res = get_lsp_references("empty.cpp", 1, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
        self.assertEqual(res, {})

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_destructor_boundary(self, mock_instances):
        """Verify that C++ destructors starting with '~' (a non-word character)
        are matched correctly by dynamically adjusting word boundaries,
        allowing the LSP cursor to land on the correct character offset."""
        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client
        mock_client.get_references.return_value = []

        lines = [
            "class MyClass {\n",
            "    MyClass::~MyClass() {}\n", # line 2 (1-based), '~' is preceded by ':'
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

    def test_lsp_client_send_broken_pipe(self):
        """Verify that _send handles BrokenPipeError and OSError gracefully without crashing the tool."""
        mock_proc = MagicMock()
        mock_proc.stdin.write.side_effect = BrokenPipeError("Broken pipe")
        
        client = MinimalLSPClient(["some_lsp_binary"])
        client.proc = mock_proc
        
        # It should not raise an exception even if the process stdin write throws a BrokenPipeError
        try:
            client._send({"jsonrpc": "2.0", "method": "exit"})
        except Exception as e:
            self.fail(f"_send raised an exception: {e}")

    @patch("subprocess.Popen")
    def test_lsp_client_startup_timeout_returns_false(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        mock_proc.stdout = BytesIO(b"")
        mock_proc.poll.return_value = None

        client = MinimalLSPClient(["some_lsp_binary"])

        # Patch time.time to simulate a startup timeout (>10 seconds)
        with patch("time.time") as mock_time:
            mock_time.side_effect = [100.0, 115.0]
            success = client.start()

        self.assertFalse(success)
        self.assertIsNone(client.proc)
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=1)

    @patch("context_builder.lsp_client.USE_LSP", True)
    @patch("context_builder.lsp_client.LSP_INSTANCES")
    def test_get_lsp_references_malformed_string_ref(self, mock_instances):
        from context_builder.lsp_client import get_lsp_references

        mock_client = MagicMock()
        mock_instances.__contains__.return_value = True
        mock_instances.get.return_value = mock_client

        # Mocking references list containing a string and a dictionary without 'uri'
        mock_client.get_references.return_value = [
            "string_reference_structure",
            {"uri": None, "range": None}
        ]

        mock_file_cache = MagicMock()

        # It should handle the AttributeError from the string and return empty dict
        res = get_lsp_references("empty.cpp", 1, "my_func", 5.0, 100, False, file_cache=mock_file_cache)
        self.assertEqual(res, {})

