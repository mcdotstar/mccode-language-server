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
        # Informal / bare types users are likely to write
        assert _mime_to_language_id('python') == 'python'
        assert _mime_to_language_id('text/x-python') == 'python'
        assert _mime_to_language_id('markdown') == 'markdown'
        assert _mime_to_language_id('text/markdown') == 'markdown'
        assert _mime_to_language_id('text/x-markdown') == 'markdown'

    def test_valid_python_produces_no_diags(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "python" script
%{
def greet(name):
    return f"hello {name}"
%}
TRACE
END
"""
        assert self._compute(source) == []

    def test_invalid_python_produces_error(self):
        source = """\
DEFINE INSTRUMENT T()
METADATA "python" script
%{
def greet(name)
    return name
%}
TRACE
END
"""
        diags = self._compute(source)
        assert len(diags) == 1
        assert 'Python' in diags[0].message


class TestBlockDelimDiags:
    """_update_block_delim_diags warns on {% and %} (Jinja-style typos)."""

    def _compute(self, source: str):
        import mclsp.server as srv
        from mclsp.server import _block_delim_diags, _update_block_delim_diags
        from mclsp.document import parse_document
        uri = 'file:///tmp/test_delim.instr'
        srv._docs[uri] = parse_document(uri, source)
        _update_block_delim_diags(uri)
        return _block_delim_diags.get(uri, [])

    def test_jinja_open_brace_warned(self):
        diags = self._compute('DECLARE {% int x; %}\n')
        messages = [d.message for d in diags]
        assert any('{%' in m for m in messages)

    def test_jinja_close_brace_warned(self):
        diags = self._compute('DECLARE %{ int x; }%\n')
        messages = [d.message for d in diags]
        assert any('}%' in m for m in messages)

    def test_correct_delimiters_no_warning(self):
        diags = self._compute('DECLARE %{\nint x;\n%}\n')
        assert diags == []

    def test_warning_points_at_correct_column(self):
        source = 'DECLARE {% int x; }%\n'
        diags = self._compute(source)
        cols = {d.range.start.character for d in diags}
        assert 8 in cols   # '{%' starts at column 8
        assert 18 in cols  # '}%' starts at column 18
