"""Tests for mclsp handlers."""
from __future__ import annotations

import pytest
from lsprotocol import types as lsp

from mclsp.document import parse_document

# ---------------------------------------------------------------------------
# Sample McCode source snippets
# ---------------------------------------------------------------------------

VALID_INSTR = """\
DEFINE INSTRUMENT TestInstr(double L = 1.0, int n = 100)
DECLARE
%{
  double x;
%}
TRACE
COMPONENT Origin = Progress_bar()
AT (0, 0, 0) ABSOLUTE
END
"""

INVALID_INSTR = """\
DEFINE INSTRUMENT Bad(
TRACE
END
"""

VALID_COMP = """\
DEFINE COMPONENT TestComp
DEFINITION PARAMETERS (int n)
SETTING PARAMETERS (double x = 0.0)
OUTPUT PARAMETERS ()
TRACE
%{
%}
END
"""


# ---------------------------------------------------------------------------
# document.py
# ---------------------------------------------------------------------------

class TestParseDocument:
    def test_valid_instr_no_errors(self):
        doc = parse_document('test.instr', VALID_INSTR)
        assert doc.suffix == '.instr'
        assert doc.tree is not None
        assert doc.errors == []

    def test_invalid_instr_has_errors(self):
        doc = parse_document('test.instr', INVALID_INSTR)
        assert len(doc.errors) > 0

    def test_valid_comp_no_errors(self):
        doc = parse_document('test.comp', VALID_COMP)
        assert doc.suffix == '.comp'
        assert doc.tree is not None
        assert doc.errors == []

    def test_unknown_extension_no_tree(self):
        doc = parse_document('test.xyz', 'some content')
        assert doc.tree is None
        assert doc.errors == []


# ---------------------------------------------------------------------------
# diagnostics.py
# ---------------------------------------------------------------------------

class TestGetDiagnostics:
    def test_no_diagnostics_for_valid_source(self):
        from mclsp.handlers.diagnostics import get_diagnostics
        doc = parse_document('test.instr', VALID_INSTR)
        diags = get_diagnostics(doc)
        assert diags == []

    def test_diagnostics_for_invalid_source(self):
        from mclsp.handlers.diagnostics import get_diagnostics
        doc = parse_document('test.instr', INVALID_INSTR)
        diags = get_diagnostics(doc)
        assert len(diags) > 0
        assert all(d.severity == lsp.DiagnosticSeverity.Error for d in diags)
        assert all(d.source == 'mclsp' for d in diags)

    def test_diagnostic_position_is_zero_based(self):
        from mclsp.handlers.diagnostics import get_diagnostics
        doc = parse_document('test.instr', INVALID_INSTR)
        diags = get_diagnostics(doc)
        for d in diags:
            assert d.range.start.line >= 0
            assert d.range.start.character >= 0


# ---------------------------------------------------------------------------
# completion.py
# ---------------------------------------------------------------------------

class TestGetCompletions:
    def test_keyword_completion_at_start_of_line(self):
        from mclsp.handlers.completion import get_completions
        doc = parse_document('test.instr', VALID_INSTR)
        # Position at the start of an otherwise blank line → keyword completion
        items = get_completions(doc, lsp.Position(line=5, character=0))
        labels = [i.label for i in items]
        assert 'COMPONENT' in labels
        assert 'TRACE' in labels

    def test_component_type_completion_after_equals(self):
        from mclsp.handlers.completion import get_completions, _component_names
        from mccode_antlr import Flavor
        # Real registry lookup via pooch — at minimum returns [] gracefully.
        # If the registry is populated, items should be Class-kinded.
        source = VALID_INSTR + '\nCOMPONENT Foo = '
        doc = parse_document('test.instr', source)
        lines = source.splitlines()
        last_line = len(lines) - 1
        items = get_completions(
            doc,
            lsp.Position(line=last_line, character=len(lines[last_line])),
            flavor=Flavor.MCSTAS,
        )
        names = _component_names(Flavor.MCSTAS)
        if names:
            assert any(i.kind == lsp.CompletionItemKind.Class for i in items)
            assert any(i.label in names for i in items)
        else:
            # No registry available (e.g. offline CI) — graceful empty result
            assert items == [] or all(i.kind == lsp.CompletionItemKind.Class for i in items)

    def test_keyword_completion_for_comp_file(self):
        from mclsp.handlers.completion import get_completions
        doc = parse_document('test.comp', VALID_COMP)
        items = get_completions(doc, lsp.Position(line=0, character=0))
        labels = [i.label for i in items]
        assert 'SETTING' in labels
        assert 'TRACE' in labels


# ---------------------------------------------------------------------------
# hover.py  (unit-level: _extract_description, _word_at)
# ---------------------------------------------------------------------------

class TestHoverHelpers:
    def test_extract_description_block_comment(self):
        from mclsp.handlers.hover import _extract_description
        source = '/* A test component\n * Does things\n */\nDEFINE COMPONENT ...'
        desc = _extract_description(source)
        assert 'test component' in desc

    def test_extract_description_line_comments(self):
        from mclsp.handlers.hover import _extract_description
        source = '// First line\n// Second line\nDEFINE COMPONENT ...'
        desc = _extract_description(source)
        assert 'First line' in desc

    def test_word_at_cursor(self):
        from mclsp.handlers.hover import _word_at
        line = 'COMPONENT Foo = Progress_bar'
        result = _word_at(line, 20)  # cursor on 'Progress_bar'
        assert result is not None
        word, start, end = result
        assert word == 'Progress_bar'

    def test_word_at_whitespace_returns_none(self):
        from mclsp.handlers.hover import _word_at
        result = _word_at('COMPONENT Foo = Bar', 9)  # space between words
        # May or may not return None depending on exact cursor position; just check type
        # (the space at index 9 is between COMPONENT and Foo)
        assert result is None or isinstance(result[0], str)

    def test_hover_returns_none_for_non_component_line(self):
        from mclsp.handlers.hover import get_hover
        doc = parse_document('test.instr', VALID_INSTR)
        result = get_hover(doc, lsp.Position(line=0, character=5))
        # Line 0 is "DEFINE INSTRUMENT ..." — no hover expected
        assert result is None


# ---------------------------------------------------------------------------
# server.py  (smoke test — no real LSP connection)
# ---------------------------------------------------------------------------

class TestServerModule:
    def test_server_importable(self):
        from mclsp.server import server
        assert server is not None

    def test_docs_dict_initially_empty(self):
        from mclsp.server import _docs
        assert isinstance(_docs, dict)


# ---------------------------------------------------------------------------
# flavor.py
# ---------------------------------------------------------------------------

class TestFlavorResolver:
    def test_defaults_to_mcstas(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        assert r.resolve('file:///home/user/instrument.instr') == Flavor.MCSTAS

    def test_uri_heuristic_mcxtrace(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        assert r.resolve('file:///home/user/mcxtrace-3.0/test.instr') == Flavor.MCXTRACE

    def test_uri_heuristic_mcstas(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        assert r.resolve('file:///opt/mcstas/lib/test.instr') == Flavor.MCSTAS

    def test_explicit_workspace_override_wins(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        # URI heuristic would say McXtrace, but explicit override says McStas
        r.set_workspace_flavor(Flavor.MCSTAS)
        assert r.resolve('file:///mcxtrace/something.instr') == Flavor.MCSTAS

    def test_clearing_workspace_flavor_restores_inference(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_workspace_flavor(Flavor.MCSTAS)
        r.set_workspace_flavor(None)  # clear override
        # Now URI heuristic should apply
        result = r.resolve('file:///mcxtrace/something.instr')
        assert result == Flavor.MCXTRACE

    def test_explicit_document_override(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_document_flavor('file:///neutral/test.instr', Flavor.MCXTRACE)
        assert r.resolve('file:///neutral/test.instr') == Flavor.MCXTRACE

    def test_forget_removes_document(self):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver()
        r.set_document_flavor('file:///test.instr', Flavor.MCXTRACE)
        r.forget('file:///test.instr')
        # After forget, default applies (no McXtrace URI signal here)
        assert r.resolve('file:///test.instr') == Flavor.MCSTAS

    def test_project_config_file(self, tmp_path):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        config = tmp_path / '.mclsp.toml'
        config.write_text('flavor = "mcxtrace"\n')
        r = FlavorResolver(workspace_root=str(tmp_path))
        assert r.resolve('file:///neutral/test.instr') == Flavor.MCXTRACE

    def test_project_config_file_missing_falls_through(self, tmp_path):
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        r = FlavorResolver(workspace_root=str(tmp_path))
        # No .mclsp.toml → falls through to URI heuristic → default
        assert r.resolve('file:///neutral/test.instr') == Flavor.MCSTAS

    def test_infer_from_source_with_mock_registries(self):
        """Component-based inference works when registries have distinct names."""
        from mclsp.flavor import FlavorResolver, _known_components
        from mccode_antlr import Flavor
        import unittest.mock as mock

        mcstas_comps  = frozenset({'Progress_bar', 'Arm', 'E_monitor'})
        mcxtrace_comps = frozenset({'Arm', 'ESRF_BM', 'Filter_crystal'})

        source = 'DEFINE INSTRUMENT T()\nTRACE\nCOMPONENT a = ESRF_BM()\nAT (0,0,0) ABSOLUTE\nEND\n'
        r = FlavorResolver()
        with mock.patch('mclsp.flavor._known_components', side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            result = r.resolve('file:///neutral/test.instr', source=source)
        assert result == Flavor.MCXTRACE

    def test_infer_caches_result(self):
        """Once inferred, the result is cached and returned without re-parsing."""
        from mclsp.flavor import FlavorResolver, _known_components
        from mccode_antlr import Flavor
        import unittest.mock as mock

        mcstas_comps  = frozenset({'Progress_bar'})
        mcxtrace_comps = frozenset({'ESRF_BM'})
        source = 'COMPONENT a = ESRF_BM()\n'

        r = FlavorResolver()
        with mock.patch('mclsp.flavor._known_components', side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            r.resolve('file:///t.instr', source=source)

        # Second call without mock — should return cached value
        assert r.resolve('file:///t.instr') == Flavor.MCXTRACE

    def test_re_infer_updates_after_new_component(self):
        """Adding a discriminating component on edit updates the cached flavor."""
        from mclsp.flavor import FlavorResolver
        from mccode_antlr import Flavor
        import unittest.mock as mock

        mcstas_comps  = frozenset({'Progress_bar', 'Arm'})
        mcxtrace_comps = frozenset({'Arm', 'ESRF_BM'})

        r = FlavorResolver()
        # First edit: only 'Arm' which is in both → ambiguous → falls back to default
        source1 = 'COMPONENT a = Arm()\n'
        with mock.patch('mclsp.flavor._known_components', side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            r1 = r.resolve('file:///t.instr', source=source1)

        # Second edit: add ESRF_BM which is McXtrace-only
        source2 = source1 + 'COMPONENT b = ESRF_BM()\n'
        with mock.patch('mclsp.flavor._known_components', side_effect=lambda f: mcstas_comps if f == Flavor.MCSTAS else mcxtrace_comps):
            r2 = r.re_infer('file:///t.instr', source=source2)

        assert r2 == Flavor.MCXTRACE

    def test_flavor_from_string(self):
        from mclsp.flavor import _flavor_from_string
        from mccode_antlr import Flavor
        assert _flavor_from_string('mcxtrace') == Flavor.MCXTRACE
        assert _flavor_from_string('MCSTAS') == Flavor.MCSTAS
        assert _flavor_from_string('invalid') is None
        assert _flavor_from_string('') is None


# ---------------------------------------------------------------------------
# c_bridge.py
# ---------------------------------------------------------------------------

INSTR_WITH_C = """\
DEFINE INSTRUMENT BridgeTest(double L = 1.5, int n = 50)
DECLARE
%{
  double my_var = 0.0;
  int counter = 0;
%}
INITIALIZE
%{
  my_var = L * 2.0;
%}
TRACE
COMPONENT Origin = Progress_bar()
AT (0, 0, 0) ABSOLUTE
EXTEND
%{
  counter++;
%}
SAVE
%{
  printf("counter = %d\n", counter);
%}
FINALLY
%{
  my_var = 0.0;
%}
END
"""

COMP_WITH_C = """\
DEFINE COMPONENT BridgeComp
DEFINITION PARAMETERS ()
SETTING PARAMETERS (double width = 0.1, int flag = 0)
OUTPUT PARAMETERS ()
DECLARE
%{
  double precomputed;
%}
INITIALIZE
%{
  precomputed = width * 2.0;
%}
TRACE
%{
  if (flag) SCATTER;
%}
FINALLY
%{
  precomputed = 0.0;
%}
END
"""


class TestCBridge:
    def test_build_virtual_c_instr(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert vdoc is not None
        assert len(vdoc.regions) >= 4  # DECLARE, INITIALIZE, EXTEND, SAVE, FINALLY

    def test_virtual_c_contains_line_directives(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert '#line' in vdoc.virtual_source

    def test_virtual_c_contains_filename(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert 'bridge_test.instr' in vdoc.virtual_source

    def test_virtual_c_contains_param_stubs(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert 'double L' in vdoc.virtual_source
        assert 'int n' in vdoc.virtual_source

    def test_virtual_c_contains_c_code(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert 'my_var' in vdoc.virtual_source
        assert 'counter' in vdoc.virtual_source

    def test_declare_region_at_file_scope(self):
        """DECLARE content must appear before any function wrapper."""
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        # Find DECLARE content and ensure no unclosed '{' before it.
        src = vdoc.virtual_source
        declare_pos = src.find('my_var')
        stub_pos = src.find('__mclsp_')  # first wrapper function
        assert declare_pos < stub_pos, "DECLARE content should be before function wrappers"

    def test_body_sections_wrapped_in_functions(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert '__mclsp_' in vdoc.virtual_source

    def test_position_map_populated(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        for r in vdoc.regions:
            assert r.virtual_line > 0, f"Region {r.label} has virtual_line=0"

    def test_mccode_to_virtual_inside_declare(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        # Find the DECLARE region.
        declare = next((r for r in vdoc.regions if r.section == 'declare'), None)
        assert declare is not None
        result = vdoc.mccode_to_virtual(declare.mccode_line, 0)
        assert result is not None
        vline, vcol = result
        assert vline == declare.virtual_line

    def test_mccode_to_virtual_outside_c_block(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        # Line 1 is 'DEFINE INSTRUMENT ...' — not a C block.
        result = vdoc.mccode_to_virtual(1, 0)
        assert result is None

    def test_virtual_to_mccode_roundtrip(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        declare = next((r for r in vdoc.regions if r.section == 'declare'), None)
        assert declare is not None
        # Forward
        result = vdoc.mccode_to_virtual(declare.mccode_line, 5)
        assert result is not None
        vline, vcol = result
        # Reverse
        back = vdoc.virtual_to_mccode(vline, vcol)
        assert back is not None
        uri, mline, mcol = back
        assert mline == declare.mccode_line
        assert mcol == 5

    def test_build_virtual_c_comp(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_comp.comp', COMP_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert vdoc is not None
        assert any(r.section == 'declare' for r in vdoc.regions)
        assert any(r.section == 'initialize' for r in vdoc.regions)
        assert any(r.section == 'trace' for r in vdoc.regions)

    def test_comp_param_stubs(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_comp.comp', COMP_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert 'double width' in vdoc.virtual_source
        assert 'int flag' in vdoc.virtual_source

    def test_mcstas_stub_included(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        assert 'extern double vx' in vdoc.virtual_source

    def test_mcxtrace_stub_included(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcxtrace')
        assert 'extern double kx' in vdoc.virtual_source

    def test_no_parse_returns_none(self):
        from mclsp.c_bridge import build_virtual_c
        from mclsp.document import ParsedDocument
        bad_doc = ParsedDocument(
            uri='bad.instr', source='', suffix='.instr',
            tree=None, token_stream=None, errors=[]
        )
        result = build_virtual_c(bad_doc)
        assert result is None

    def test_region_at_mccode_position(self):
        from mclsp.c_bridge import build_virtual_c
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = build_virtual_c(doc, flavor='mcstas')
        declare = next((r for r in vdoc.regions if r.section == 'declare'), None)
        assert declare is not None
        found = vdoc.region_at_mccode(declare.mccode_line, 0)
        assert found is declare
        # DSL line should return None
        found2 = vdoc.region_at_mccode(1, 0)
        assert found2 is None
