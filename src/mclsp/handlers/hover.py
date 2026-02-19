"""
Hover handler.

When the cursor rests on a component type name in a COMPONENT instantiation
line, resolve the component from the registry, parse its ``.comp`` file, and
return a Markdown string describing the component's parameters.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from lsprotocol import types as lsp

if TYPE_CHECKING:
    from mclsp.document import ParsedDocument


def _flavor_enum(flavor):
    """Convert a string flavor name or Flavor enum to a Flavor enum value."""
    from mccode_antlr import Flavor
    if isinstance(flavor, Flavor):
        return flavor
    name = str(flavor).upper().replace('-', '_')
    return Flavor[name] if name in Flavor.__members__ else Flavor.MCSTAS

# Match a component instantiation line to extract the component type name.
# Group 1: component type
_COMP_INST_RE = re.compile(
    r'COMPONENT\s+\w+\s*=\s*(\w+)',
    re.IGNORECASE,
)

# Match a bare identifier under the cursor (for word-range extraction)
_WORD_RE = re.compile(r'\b\w+\b')


def _word_at(line: str, character: int) -> tuple[str, lsp.Range, int] | None:
    """Return ``(word, lsp_range, line_number)`` for the word under *character*."""
    for m in _WORD_RE.finditer(line):
        if m.start() <= character <= m.end():
            return m.group(0), m.start(), m.end()
    return None


@lru_cache(maxsize=256)
def _comp_hover_markdown(comp_name: str, flavor) -> str | None:
    """Build a Markdown hover string for *comp_name* (cached)."""
    try:
        from mccode_antlr.reader import Reader
        from antlr4 import InputStream, CommonTokenStream
        from mccode_antlr.grammar.McCompLexer import McCompLexer
        from mccode_antlr.grammar.McCompParser import McCompParser
        from mccode_antlr.comp import CompVisitor
        from antlr4.error.ErrorListener import ErrorListener

        class _Silent(ErrorListener):
            def syntaxError(self, *a): pass

        reader = Reader(flavor=_flavor_enum(flavor))
        if not reader.known(comp_name):
            return None

        source = reader.contents(comp_name, ext='.comp', strict=True)

        stream = InputStream(source)
        lexer = McCompLexer(stream)
        lexer.removeErrorListeners(); lexer.addErrorListener(_Silent())
        ts = CommonTokenStream(lexer)
        parser = McCompParser(ts)
        parser.removeErrorListeners(); parser.addErrorListener(_Silent())
        tree = parser.prog()
        visitor = CompVisitor()
        visitor.visit(tree)
        comp = visitor.comp
    except Exception:
        return None

    lines: list[str] = [f'### `{comp_name}`']

    if comp.category:
        lines.append(f'*Category: {comp.category}*')

    # Extract leading comment from source (first /* … */ or sequence of //)
    desc = _extract_description(source)
    if desc:
        lines.append('')
        lines.append(desc)

    def _fmt_params(params, heading):
        if not params:
            return
        lines.append('')
        lines.append(f'**{heading}**')
        lines.append('')
        for p in params:
            default = str(p.value) if p.value is not None else ''
            if default and default != 'None':
                lines.append(f'- `{p.name}` = `{default}`')
            else:
                lines.append(f'- `{p.name}`')

    _fmt_params(comp.define,   'DEFINITION parameters')
    _fmt_params(comp.setting,  'SETTING parameters')
    _fmt_params(comp.output,   'OUTPUT parameters')

    return '\n'.join(lines)


def _extract_description(source: str) -> str:
    """Return the first block comment (or leading // comments) as plain text."""
    # Try block comment first
    m = re.search(r'/\*+\s*(.*?)\s*\*+/', source, re.DOTALL)
    if m:
        text = m.group(1)
        # Collapse leading * on each line (doxygen style)
        text = re.sub(r'^\s*\*+\s?', '', text, flags=re.MULTILINE)
        return text.strip()[:800]  # cap length
    # Fall back to leading // comments
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith('//'):
            lines.append(stripped.lstrip('/').strip())
        elif lines:
            break
    return ' '.join(lines)[:800]


def get_hover(
    doc: ParsedDocument,
    position: lsp.Position,
    flavor='mcstas',
) -> lsp.Hover | None:
    """Return LSP hover content for *position* in *doc*, or *None*."""
    lines = doc.source.splitlines()
    if position.line >= len(lines):
        return None
    line_text = lines[position.line]

    result = _word_at(line_text, position.character)
    if result is None:
        return None
    word, start_col, end_col = result

    # Check if this line is a COMPONENT instantiation
    m = _COMP_INST_RE.match(line_text.strip())
    if m:
        comp_type = m.group(1)
        # Only provide hover when cursor is on the component type token
        if word == comp_type:
            md = _comp_hover_markdown(comp_type, flavor)
            if md:
                return lsp.Hover(
                    contents=lsp.MarkupContent(
                        kind=lsp.MarkupKind.Markdown, value=md,
                    ),
                    range=lsp.Range(
                        start=lsp.Position(line=position.line, character=start_col),
                        end=lsp.Position(line=position.line, character=end_col),
                    ),
                )

    return None
