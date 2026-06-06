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

