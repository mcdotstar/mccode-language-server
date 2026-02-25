"""Tests for mclsp.handlers.completion — keyword and component-type completions."""
from __future__ import annotations

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


class TestGetCompletions:
    def test_keyword_completion_at_start_of_line(self):
        from mclsp.handlers.completion import get_completions
        doc = parse_document('test.instr', VALID_INSTR)
        items = get_completions(doc, lsp.Position(line=5, character=0))
        labels = [i.label for i in items]
        assert 'COMPONENT' in labels
        assert 'TRACE' in labels

    def test_component_type_completion_after_equals(self):
        from mclsp.handlers.completion import get_completions, _component_names
        from mccode_antlr import Flavor
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
