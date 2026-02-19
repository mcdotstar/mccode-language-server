"""Convert ANTLR4 parse errors into LSP Diagnostic objects."""
from __future__ import annotations

from lsprotocol import types as lsp

from mclsp.document import ParsedDocument


def get_diagnostics(doc: ParsedDocument) -> list[lsp.Diagnostic]:
    """Return LSP ``Diagnostic`` objects for every syntax error in *doc*."""
    diags: list[lsp.Diagnostic] = []
    for err in doc.errors:
        line = max(0, err.line - 1)          # LSP is 0-based; ANTLR4 is 1-based
        col  = max(0, err.column)
        diags.append(
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=line, character=col),
                    end=lsp.Position(line=line, character=col + 1),
                ),
                message=err.message,
                severity=lsp.DiagnosticSeverity.Error,
                source='mclsp',
            )
        )
    return diags
