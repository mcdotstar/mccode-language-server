"""
Completion handler.

Provides three kinds of completion items:

1. **McCode DSL keywords** — always offered at the top level.
2. **Component names** — offered after the ``=`` sign in a COMPONENT line
   (and anywhere the cursor is on an identifier that could be a component
   type).  Resolved lazily from the ``Reader`` registry so they reflect
   whatever McStas/McXtrace installation the server found.
3. **Parameter names** — offered inside the ``(…)`` argument list of a
   component instance when the component type can be resolved.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from lsprotocol import types as lsp

if TYPE_CHECKING:
    from mclsp.document import ParsedDocument

# ---------------------------------------------------------------------------
# McCode DSL keyword set (from McCommon.g4 / McInstr.g4 / McComp.g4)
# ---------------------------------------------------------------------------
_INSTR_KEYWORDS = [
    'DEFINE', 'INSTRUMENT', 'COMPONENT', 'DECLARE', 'USERVARS', 'INITIALIZE',
    'TRACE', 'SAVE', 'FINALLY', 'END',
    'AT', 'ROTATED', 'RELATIVE', 'ABSOLUTE', 'PREVIOUS', 'NEXT',
    'GROUP', 'EXTEND', 'JUMP', 'WHEN', 'ITERATE', 'RESTORE',
    'NEUTRON', 'XRAY', 'SPLIT', 'COPY', 'INHERIT',
]
_COMP_KEYWORDS = [
    'DEFINE', 'COMPONENT', 'DEFINITION', 'SETTING', 'OUTPUT', 'PARAMETERS',
    'DECLARE', 'SHARE', 'USERVARS', 'INITIALIZE', 'TRACE', 'SAVE', 'FINALLY',
    'DISPLAY', 'END',
]

_KEYWORD_ITEMS_INSTR = [
    lsp.CompletionItem(
        label=kw,
        kind=lsp.CompletionItemKind.Keyword,
        insert_text=kw,
    )
    for kw in sorted(set(_INSTR_KEYWORDS))
]
_KEYWORD_ITEMS_COMP = [
    lsp.CompletionItem(
        label=kw,
        kind=lsp.CompletionItemKind.Keyword,
        insert_text=kw,
    )
    for kw in sorted(set(_COMP_KEYWORDS))
]

# ---------------------------------------------------------------------------
# Component-name completion (lazy, cached per Flavor value)
# ---------------------------------------------------------------------------

# Match:  COMPONENT  <instance_name>  =  <cursor>
# or anything after "= " on a COMPONENT line where there's no open paren yet.
_COMPONENT_TYPE_RE = re.compile(
    r'COMPONENT\s+\w+\s*=\s*(\w*)$',
    re.IGNORECASE,
)

# Match:  <component_type>  (  …text without closing paren…  <cursor>
# Captures the component type name so we can look up its parameters.
_COMPONENT_ARGS_RE = re.compile(
    r'COMPONENT\s+\w+\s*=\s*(\w+)\s*\([^)]*$',
    re.IGNORECASE,
)


def _flavor_enum(flavor):
    """Convert a string flavor name or Flavor enum to a Flavor enum value."""
    from mccode_antlr import Flavor
    if isinstance(flavor, Flavor):
        return flavor
    name = str(flavor).upper().replace('-', '_')
    return Flavor[name] if name in Flavor.__members__ else Flavor.MCSTAS


@lru_cache(maxsize=4)
def _component_names(flavor) -> list[str]:
    """Return all component names known to the default Reader for *flavor*.

    Results are cached per flavor value.
    Network/filesystem access only happens on the first call.
    """
    try:
        from mccode_antlr.reader import Reader
        from pathlib import PurePosixPath
        reader = Reader(flavor=_flavor_enum(flavor))
        names: set[str] = set()
        for reg in reader.registries:
            try:
                for fname in reg.filenames():
                    p = PurePosixPath(fname)
                    if p.suffix == '.comp':
                        names.add(p.stem)
            except Exception:
                pass
        return sorted(names)
    except Exception:
        return []


def _component_completion_items(flavor) -> list[lsp.CompletionItem]:
    return [
        lsp.CompletionItem(
            label=name,
            kind=lsp.CompletionItemKind.Class,
            detail='McCode component',
            insert_text=name,
        )
        for name in _component_names(flavor)
    ]


def _parameter_completion_items(
    comp_name: str, flavor
) -> list[lsp.CompletionItem]:
    """Return completion items for the DEFINE+SETTING parameters of *comp_name*."""
    try:
        from mccode_antlr.reader import Reader
        reader = Reader(flavor=_flavor_enum(flavor))
        if not reader.known(comp_name):
            return []
        source = reader.contents(comp_name, ext='.comp', strict=True)
        from antlr4 import InputStream, CommonTokenStream
        from mccode_antlr.grammar.McCompLexer import McCompLexer
        from mccode_antlr.grammar.McCompParser import McCompParser
        from mccode_antlr.comp import CompVisitor
        from antlr4.error.ErrorListener import ErrorListener

        class _Silent(ErrorListener):
            def syntaxError(self, *a): pass

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

        items: list[lsp.CompletionItem] = []
        for p in list(comp.define) + list(comp.setting):
            items.append(lsp.CompletionItem(
                label=p.name,
                kind=lsp.CompletionItemKind.Field,
                detail=str(p.value) if p.value is not None else '',
                insert_text=f'{p.name} = ',
            ))
        return items
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_completions(
    doc: ParsedDocument,
    position: lsp.Position,
    flavor='mcstas',
) -> list[lsp.CompletionItem]:
    """Return completion items for *position* in *doc*."""
    lines = doc.source.splitlines()
    if position.line >= len(lines):
        return []
    line_up_to_cursor = lines[position.line][:position.character]

    # Are we typing inside a component argument list?
    m = _COMPONENT_ARGS_RE.search(line_up_to_cursor)
    if m:
        comp_name = m.group(1)
        params = _parameter_completion_items(comp_name, flavor)
        if params:
            return params

    # Are we typing a component type name (after "COMPONENT <id> =")?
    if _COMPONENT_TYPE_RE.search(line_up_to_cursor):
        return _component_completion_items(flavor)

    # Fall back to keyword completion
    keyword_items = (
        _KEYWORD_ITEMS_INSTR if doc.suffix == '.instr' else _KEYWORD_ITEMS_COMP
    )
    return keyword_items
