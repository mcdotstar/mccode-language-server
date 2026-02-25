"""Smoke tests for mclsp.server — importability and initial state."""
from __future__ import annotations


class TestServerModule:
    def test_server_importable(self):
        from mclsp.server import server
        assert server is not None

    def test_docs_dict_initially_empty(self):
        from mclsp.server import _docs
        assert isinstance(_docs, dict)


class TestFoldingRange:
    def _compute(self, source: str):
        import mclsp.server as srv
        from mclsp.server import folding_range
        from mclsp.document import parse_document
        import lsprotocol.types as lsp
        uri = 'file:///tmp/test_fold.instr'
        srv._docs[uri] = parse_document(uri, source)
        params = lsp.FoldingRangeParams(text_document=lsp.TextDocumentIdentifier(uri=uri))
        return folding_range(params)

    def test_single_block(self):
        source = "DECLARE %{\nint x;\n%}\n"
        ranges = self._compute(source)
        assert len(ranges) == 1
        assert ranges[0].start_line == 0   # DECLARE %{
        assert ranges[0].end_line == 1     # int x; (line before %})

    def test_closing_delimiter_not_in_range(self):
        """end_line must be < the %} line so %} stays visible."""
        source = "DECLARE %{\nint x;\nint y;\n%}\n"
        ranges = self._compute(source)
        assert ranges[0].end_line == 2     # last content line, not the %} line (3)

    def test_multiple_blocks(self):
        source = "DECLARE %{\nint x;\n%}\nINITIALIZE %{\nx=0;\n%}\n"
        ranges = self._compute(source)
        assert len(ranges) == 2
