"""
Per-document parse cache.

Each open document is stored as a ``ParsedDocument``.  Parsing is performed
synchronously (McCode files are typically small) whenever the content changes.
Errors collected by the ANTLR4 error listener are stored alongside the parse
tree so they can be published as LSP diagnostics without re-parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

if TYPE_CHECKING:
    from lsprotocol import types as lsp


@dataclass
class ParseError:
    line: int        # 1-based
    column: int      # 0-based
    message: str


@dataclass
class ParsedDocument:
    uri: str
    source: str
    suffix: str                        # '.instr' or '.comp'
    tree: object | None                # ANTLR4 parse tree root, or None on fatal error
    token_stream: CommonTokenStream | None
    errors: list[ParseError] = field(default_factory=list)


class _CollectingErrorListener(ErrorListener):
    def __init__(self):
        super().__init__()
        self.errors: list[ParseError] = []

    def syntaxError(self, recognizer, offending_symbol, line, column, msg, e):
        self.errors.append(ParseError(line=line, column=column, message=msg))


def parse_document(uri: str, source: str) -> ParsedDocument:
    """Parse *source* and return a :class:`ParsedDocument`.

    The suffix is inferred from *uri* (``.instr`` → McInstr grammar,
    ``.comp`` → McComp grammar).  Files with any other extension are stored
    with ``tree=None`` and no errors.
    """
    from pathlib import PurePosixPath
    suffix = PurePosixPath(uri).suffix.lower()

    if suffix == '.instr':
        from mccode_antlr.grammar.McInstrLexer import McInstrLexer
        from mccode_antlr.grammar.McInstrParser import McInstrParser
        LexerCls, ParserCls, start = McInstrLexer, McInstrParser, 'prog'
    elif suffix == '.comp':
        from mccode_antlr.grammar.McCompLexer import McCompLexer
        from mccode_antlr.grammar.McCompParser import McCompParser
        LexerCls, ParserCls, start = McCompLexer, McCompParser, 'prog'
    else:
        return ParsedDocument(uri=uri, source=source, suffix=suffix,
                              tree=None, token_stream=None)

    listener = _CollectingErrorListener()
    input_stream = InputStream(source)
    lexer = LexerCls(input_stream)
    lexer.removeErrorListeners()
    lexer.addErrorListener(listener)

    token_stream = CommonTokenStream(lexer)
    parser = ParserCls(token_stream)
    parser.removeErrorListeners()
    parser.addErrorListener(listener)

    tree = getattr(parser, start)()

    return ParsedDocument(
        uri=uri,
        source=source,
        suffix=suffix,
        tree=tree,
        token_stream=token_stream,
        errors=listener.errors,
    )
