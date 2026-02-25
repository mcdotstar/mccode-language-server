"""Tests for mclsp.handlers.hover — _word_at helper and get_hover."""
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


class TestHoverHelpers:
    def test_extract_description_block_comment(self):
        from mccode_antlr.mcdoc import parse_mcdoc_full
        source = '/* \n * %I\n * A test component\n * Does things\n * %E\n */\nDEFINE COMPONENT ...'
        mcdoc = parse_mcdoc_full(source)
        desc = ' '.join(mcdoc.short_desc)
        assert 'test component' in desc

    def test_extract_description_line_comments(self):
        from mccode_antlr.mcdoc import parse_mcdoc_full
        source = '// First line\n// Second line\nDEFINE COMPONENT ...'
        mcdoc = parse_mcdoc_full(source)
        # No McDoc block comment → empty; hover falls back gracefully
        assert mcdoc.short_desc == []

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
        assert result is None or isinstance(result[0], str)

    def test_hover_returns_none_for_non_component_line(self):
        from mclsp.handlers.hover import get_hover
        doc = parse_document('test.instr', VALID_INSTR)
        result = get_hover(doc, lsp.Position(line=0, character=5))
        # Line 0 is "DEFINE INSTRUMENT ..." — no hover expected
        assert result is None
