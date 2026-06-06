import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch
from context_builder.lsp_client import MinimalLSPClient, get_lsp_references

class TestLspClient(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_lsp_client_init_and_send(self, mock_popen):
        mock_proc = MagicMock()
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


