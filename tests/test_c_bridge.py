"""Tests for mclsp.c_bridge — virtual C document generation and position mapping."""
from __future__ import annotations

from mclsp.document import parse_document

# Unique component name — deliberately unlike any real McCode component so the
# InMemoryRegistry has unambiguous priority over the file-based registries.
_TEST_COMP_NAME = 'mclsp_test_noop'
_TEST_COMP = f"""\
DEFINE COMPONENT {_TEST_COMP_NAME}
SETTING PARAMETERS (double dummy = 0)
TRACE %{{ /* no-op */ %}}
END
"""

INSTR_WITH_C = f"""\
DEFINE INSTRUMENT BridgeTest(double L = 1.5, int n = 50)
DECLARE
%{{
  double my_var = 0.0;
  int counter = 0;
%}}
INITIALIZE
%{{
  my_var = L * 2.0;
%}}
TRACE
COMPONENT Origin = {_TEST_COMP_NAME}()
AT (0, 0, 0) ABSOLUTE
EXTEND
%{{
  counter++;
%}}
SAVE
%{{
  printf("counter = %d\\n", counter);
%}}
FINALLY
%{{
  my_var = 0.0;
%}}
END
"""

COMP_WITH_C = """\
DEFINE COMPONENT mclsp_test_bridge_comp
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
    _registry = None

    @classmethod
    def _reg(cls):
        if cls._registry is None:
            from mccode_antlr.reader.registry import InMemoryRegistry
            cls._registry = InMemoryRegistry('mclsp_test_components', priority=200)
            cls._registry.add_comp(_TEST_COMP_NAME, _TEST_COMP)
        return cls._registry

    def _bvc(self, doc, flavor='mcstas'):
        from mclsp.c_bridge import build_virtual_c
        return build_virtual_c(doc, flavor=flavor, extra_registries=[self._reg()])

    def test_build_virtual_c_instr(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert vdoc is not None
        assert len(vdoc.regions) >= 3

    def test_virtual_c_contains_line_directives(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert '#line' in vdoc.virtual_source

    def test_virtual_c_contains_filename(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert 'bridge_test.instr' in vdoc.virtual_source

    def test_virtual_c_contains_param_stubs(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert 'double L' in vdoc.virtual_source
        assert 'int n' in vdoc.virtual_source

    def test_virtual_c_contains_c_code(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert 'my_var' in vdoc.virtual_source
        assert 'counter' in vdoc.virtual_source

    def test_declare_region_at_file_scope(self):
        """DECLARE content must appear before the INITIALIZE function."""
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        src = vdoc.virtual_source
        assert src.find('my_var') < src.find('my_var = L * 2.0')

    def test_particle_macros_present(self):
        """Translator should emit #define x (_particle->x) style macros."""
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert '#define vx' in vdoc.virtual_source
        assert '#define x (_particle' in vdoc.virtual_source

    def test_position_map_populated(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        assert len(vdoc.regions) > 0
        for r in vdoc.regions:
            assert r.virtual_line > 0
            assert r.mccode_line > 0

    def test_mccode_to_virtual_inside_declare(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        first = min(vdoc.regions, key=lambda r: r.mccode_line)
        result = vdoc.mccode_to_virtual(first.mccode_line, 0)
        assert result is not None
        vline, vcol = result
        assert vline == first.virtual_line

    def test_mccode_to_virtual_outside_c_block(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        # Line 1 is 'DEFINE INSTRUMENT ...' — not a C block.
        assert vdoc.mccode_to_virtual(1, 0) is None

    def test_virtual_to_mccode_roundtrip(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        first = min(vdoc.regions, key=lambda r: r.mccode_line)
        vline, vcol = vdoc.mccode_to_virtual(first.mccode_line, 5)
        uri, mline, mcol = vdoc.virtual_to_mccode(vline, vcol)
        assert mline == first.mccode_line
        assert mcol == 5

    def test_build_virtual_c_comp(self):
        doc = parse_document('bridge_comp.comp', COMP_WITH_C)
        vdoc = self._bvc(doc)
        assert vdoc is not None
        assert len(vdoc.regions) >= 2

    def test_comp_param_stubs(self):
        doc = parse_document('bridge_comp.comp', COMP_WITH_C)
        vdoc = self._bvc(doc)
        assert 'double width' in vdoc.virtual_source
        assert 'int flag' in vdoc.virtual_source

    def test_mcstas_particle_macros(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc, flavor='mcstas')
        assert '#define vx' in vdoc.virtual_source

    def test_mcxtrace_particle_macros(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc, flavor='mcxtrace')
        assert '#define kx' in vdoc.virtual_source

    def test_no_parse_returns_none(self):
        from mclsp.c_bridge import build_virtual_c
        from mclsp.document import ParsedDocument
        bad_doc = ParsedDocument(
            uri='bad.instr', source='', suffix='.instr',
            tree=None, token_stream=None, errors=[]
        )
        assert build_virtual_c(bad_doc) is None

    def test_region_at_mccode_position(self):
        doc = parse_document('bridge_test.instr', INSTR_WITH_C)
        vdoc = self._bvc(doc)
        first = min(vdoc.regions, key=lambda r: r.mccode_line)
        assert vdoc.region_at_mccode(first.mccode_line, 0) is first
        assert vdoc.region_at_mccode(1, 0) is None
