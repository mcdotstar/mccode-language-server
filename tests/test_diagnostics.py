"""Tests for mclsp.handlers.diagnostics — ANTLR parse-error diagnostics."""
from __future__ import annotations

import pytest
from lsprotocol import types as lsp

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
