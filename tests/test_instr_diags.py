"""Tests for server-side semantic diagnostics:
- Unknown component types / parameters in .instr files
- METADATA block syntax validation (JSON, YAML, XML)
"""
from __future__ import annotations

from lsprotocol import types as lsp

from mclsp.document import parse_document


class TestInstrSemanticDiags:
    """Unknown component types and unknown parameter names in .instr files."""

    def _compute(self, source: str, workspace_root: str | None = None):
        import mclsp.server as srv
        from mclsp.flavor import FlavorResolver
        uri = 'file:///tmp/test_semantic.instr'
        srv._docs[uri] = parse_document(uri, source)
        srv._resolver = FlavorResolver(workspace_root=workspace_root)
        srv._semantic_error_diags.pop(uri, None)
        srv._update_instr_semantic_diags(uri)
        return srv._semantic_error_diags.get(uri, [])

    def test_unknown_component_type(self):
        source = """\
DEFINE INSTRUMENT T()
TRACE
COMPONENT a = NoSuchComponent()
AT (0,0,0) ABSOLUTE
END
"""
        diags = self._compute(source)
        assert any('NoSuchComponent' in d.message for d in diags)
        assert any(d.severity == lsp.DiagnosticSeverity.Error for d in diags)

    def test_unknown_parameter(self):
        source = """\
DEFINE INSTRUMENT T()
TRACE
COMPONENT a = Arm(bad_param=42)
AT (0,0,0) ABSOLUTE
END
"""
        diags = self._compute(source)
        assert any('bad_param' in d.message for d in diags)
        assert any(d.severity == lsp.DiagnosticSeverity.Error for d in diags)

    def test_valid_component_and_params_produce_no_error(self):
        source = """\
DEFINE INSTRUMENT T()
TRACE
COMPONENT a = Arm()
AT (0,0,0) ABSOLUTE
END
"""
        diags = self._compute(source)
        assert diags == [] or all(d.severity != lsp.DiagnosticSeverity.Error for d in diags)

    def test_not_instr_suffix_produces_no_diags(self):
        import mclsp.server as srv
        from mclsp.flavor import FlavorResolver
        uri = 'file:///tmp/test.comp'
        srv._docs[uri] = parse_document(uri, "DEFINE COMPONENT C\nSETTING PARAMETERS (x=0)\nTRACE %{ %}\nEND\n")
        srv._resolver = FlavorResolver()
        srv._semantic_error_diags.pop(uri, None)
        srv._update_instr_semantic_diags(uri)
        assert srv._semantic_error_diags.get(uri) in (None, [])


class TestMetadataDiags:
    """METADATA block syntax validation for JSON, YAML, and unknown MIME types."""

    def _compute(self, source, suffix='.instr'):
        import mclsp.server as srv
        from mclsp.flavor import FlavorResolver
        uri = f'file:///tmp/test{suffix}'
        srv._docs[uri] = parse_document(uri, source)
        srv._resolver = FlavorResolver()
        srv._metadata_diags.pop(uri, None)
        srv._update_metadata_diags(uri)
        return srv._metadata_diags.get(uri, [])

    def test_valid_json_produces_no_diags(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "application/json" cfg
%{
{"key": "value"}
%}
TRACE
END
"""
        assert self._compute(source) == []

    def test_invalid_json_produces_error(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "application/json" cfg
%{
{bad json
%}
TRACE
END
"""
        diags = self._compute(source)
        assert any('JSON' in d.message for d in diags)
        assert any(d.severity == lsp.DiagnosticSeverity.Error for d in diags)

    def test_valid_yaml_produces_no_diags(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "text/x-yaml" cfg
%{
key: value
other: 42
%}
TRACE
END
"""
        assert self._compute(source) == []

    def test_invalid_yaml_produces_error(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "text/x-yaml" cfg
%{
key: [unclosed
%}
TRACE
END
"""
        diags = self._compute(source)
        assert any('YAML' in d.message for d in diags)
        assert any(d.severity == lsp.DiagnosticSeverity.Error for d in diags)

    def test_unknown_mime_produces_no_diags(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "text/plain" notes
%{
anything goes here — no validation
%}
TRACE
END
"""
        assert self._compute(source) == []

    def test_mime_to_language_id(self):
        from mclsp.server import _mime_to_language_id
        assert _mime_to_language_id('application/json') == 'json'
        assert _mime_to_language_id('text/x-yaml') == 'yaml'
        assert _mime_to_language_id('text/xml') == 'xml'
        assert _mime_to_language_id('text/x-csrc') == 'c'
        assert _mime_to_language_id('application/x-unknown') is None
