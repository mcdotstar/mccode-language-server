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

# Match a COMPONENT line to extract the component type name.
# Used when scanning backward for an unmatched '('.
_COMPONENT_DEF_RE = re.compile(
    r'COMPONENT\s+\w+\s*=\s*(\w+)\s*\(',
    re.IGNORECASE,
)


def _component_type_for_open_paren(lines: list[str], cursor_line: int, cursor_char: int) -> str | None:
    """Scan backward from the cursor to find if we're inside an unmatched '('.

    Returns the component type name if the cursor is inside a component
    argument list, even when the opening '(' is on a different line.
    """
    # Count parens from the cursor backward to find the matching '('
    depth = 0
    for line_idx in range(cursor_line, max(cursor_line - 50, -1), -1):
        text = lines[line_idx] if line_idx < cursor_line else lines[line_idx][:cursor_char]
        # Scan characters right-to-left
        for ch in reversed(text):
            if ch == ')':
                depth += 1
            elif ch == '(':
                if depth == 0:
                    # Found the unmatched opening paren — check this line for a COMPONENT definition
                    m = _COMPONENT_DEF_RE.search(lines[line_idx])
                    if m:
                        return m.group(1)
                    return None  # '(' belongs to something else
                depth -= 1
    return None


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
        from pathlib import PurePosixPath
        reader = _cached_reader(flavor)
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


@lru_cache(maxsize=4)
def _cached_reader(flavor):
    """A Reader instance cached per flavor value (hashable enum)."""
    from mccode_antlr.reader import Reader
    return Reader(flavor=flavor)


def _param_detail(p) -> str:
    """Return a short human-readable type+default string for a ComponentParameter."""
    try:
        dt = p.value.data_type
        from mccode_antlr.common.expression import DataType
        if p.value.is_vector:
            type_str = f'vector {dt.name.lower()}'
        else:
            type_str = dt.name.lower()  # 'float', 'int', 'str', 'undefined'
        detail = f'{type_str} = {p.value}' if p.value.has_value else type_str
    except Exception:
        detail = str(p.value) if p.value is not None else ''
    unit = getattr(p, 'unit', None)
    if unit:
        detail = f'{detail}  [{unit}]' if detail else f'[{unit}]'
    return detail


def _parameter_completion_items(
    comp_name: str, flavor
) -> list[lsp.CompletionItem]:
    """Return completion items for the DEFINE+SETTING parameters of *comp_name*."""
    try:
        reader = _cached_reader(flavor)
        if not reader.known(comp_name):
            return []
        comp = reader.get_component(comp_name)

        items: list[lsp.CompletionItem] = []
        for p in list(comp.define) + list(comp.setting):
            detail = _param_detail(p)
            desc = getattr(p, 'description', None)
            items.append(lsp.CompletionItem(
                label=p.name,
                kind=lsp.CompletionItemKind.Field,
                detail=detail,
                documentation=lsp.MarkupContent(
                    kind=lsp.MarkupKind.PlainText,
                    value=desc,
                ) if desc else None,
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
    fenum = _flavor_enum(flavor)
    lines = doc.source.splitlines()
    if position.line >= len(lines):
        return []
    line_up_to_cursor = lines[position.line][:position.character]

    # Are we typing inside a component argument list (possibly multi-line)?
    comp_name = _component_type_for_open_paren(lines, position.line, position.character)
    if comp_name:
        params = _parameter_completion_items(comp_name, fenum)
        if params:
            return params

    # Are we typing a component type name (after "COMPONENT <id> =")?
    if _COMPONENT_TYPE_RE.search(line_up_to_cursor):
        return _component_completion_items(fenum)

    # Fall back to keyword completion
    keyword_items = (
        _KEYWORD_ITEMS_INSTR if doc.suffix == '.instr' else _KEYWORD_ITEMS_COMP
    )
    return keyword_items
