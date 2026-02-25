"""Tests for mclsp.document — parse_document."""
from __future__ import annotations

from mclsp.document import parse_document

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
