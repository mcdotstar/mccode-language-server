"""Smoke tests for mclsp.server — importability and initial state."""
from __future__ import annotations


class TestServerModule:
    def test_server_importable(self):
        from mclsp.server import server
        assert server is not None

    def test_docs_dict_initially_empty(self):
        from mclsp.server import _docs
        assert isinstance(_docs, dict)
