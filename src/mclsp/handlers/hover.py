"""
Hover handler.

When the cursor rests on a component type name in a COMPONENT instantiation
line, resolve the component from the registry and return a Markdown string
describing the component's parameters (type, default, unit, description) and
the McDoc short/long description from the header comment.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from lsprotocol import types as lsp

if TYPE_CHECKING:
    from mclsp.document import ParsedDocument

from mclsp.handlers.completion import _flavor_enum, _cached_reader, _param_detail

# Match a component instantiation line to extract the component type name.
# Group 1: component type
_COMP_INST_RE = re.compile(
    r'COMPONENT\s+\w+\s*=\s*(\w+)',
    re.IGNORECASE,
)

# Match a bare identifier under the cursor (for word-range extraction)
_WORD_RE = re.compile(r'\b\w+\b')


def _word_at(line: str, character: int) -> tuple[str, int, int] | None:
    """Return ``(word, start_col, end_col)`` for the word under *character*."""
    for m in _WORD_RE.finditer(line):
        if m.start() <= character <= m.end():
            return m.group(0), m.start(), m.end()
    return None


@lru_cache(maxsize=256)
def _comp_hover_markdown(comp_name: str, flavor, search_dirs: tuple[str, ...] = ()) -> str | None:
    """Build a Markdown hover string for *comp_name* (cached per comp+flavor+search_dirs).

    *search_dirs* is an ordered tuple of directory paths to search for a local
    ``.comp`` file before falling back to the registry.  This mirrors McCode's
    own local-first lookup order (same dir as the instrument, then workspace root).
    """
    from pathlib import Path
    try:
        reader = _cached_reader(flavor)
        # Check for an in-memory source override first (unsaved LSP edits).
        from mccode_antlr.reader.reader import component_cache
        override_source = component_cache.get_override(comp_name)
        if override_source is not None:
            source = override_source
            from mccode_antlr.reader.reader import Reader as _Reader
            tmp = _Reader(flavor=flavor)
            tmp.inject_source(comp_name, source)
            comp = tmp.get_component(comp_name)
        else:
            # Search local directories in order before falling back to registry.
            local_path = None
            for d in search_dirs:
                candidate = Path(d) / f'{comp_name}.comp'
                if candidate.is_file():
                    local_path = candidate
                    break
            if local_path is not None:
                source = local_path.read_text(encoding='utf-8', errors='replace')
                from mccode_antlr.reader.reader import Reader as _Reader
                tmp = _Reader(flavor=flavor)
                tmp.inject_source(comp_name, source, filename=str(local_path))
                comp = tmp.get_component(comp_name)
            else:
                if not reader.known(comp_name):
                    return None
                comp = reader.get_component(comp_name)
                source = reader.contents(comp_name, ext='.comp', strict=True)
    except Exception:
        return None

    lines: list[str] = [f'### `{comp_name}`']

    if comp.category:
        lines.append(f'*Category: {comp.category}*')

    # Use structured McDoc data for descriptions (short + long).
    try:
        from mccode_antlr.mcdoc import parse_mcdoc_full
        mcdoc = parse_mcdoc_full(source)
        short = ' '.join(s for s in mcdoc.short_desc if s.strip())
        desc_text = '\n'.join(dl for dl in mcdoc.desc_lines if dl.strip())
    except Exception:
        short = ''
        desc_text = ''

    if short:
        lines.append('')
        lines.append(short)
    if desc_text:
        lines.append('')
        # Cap length to avoid enormous hover boxes
        lines.append(desc_text[:800] + ('…' if len(desc_text) > 800 else ''))

    def _fmt_params(params, heading):
        if not params:
            return
        lines.append('')
        lines.append(f'**{heading}**')
        lines.append('')
        for p in params:
            detail = _param_detail(p)
            desc = getattr(p, 'description', None)
            row = f'- `{p.name}`'
            if detail:
                row += f': {detail}'
            if desc:
                row += f' — {desc}'
            lines.append(row)

    _fmt_params(comp.define,   'DEFINITION parameters')
    _fmt_params(comp.setting,  'SETTING parameters')
    _fmt_params(comp.output,   'OUTPUT parameters')

    return '\n'.join(lines)


def get_hover(
    doc: 'ParsedDocument',
    position: lsp.Position,
    flavor='mcstas',
    workspace_root: str | None = None,
    search_dirs: tuple[str, ...] = (),
) -> lsp.Hover | None:
    """Return LSP hover content for *position* in *doc*, or *None*.

    *search_dirs* is an ordered tuple of directories to search for local ``.comp``
    files (e.g. from ``SEARCH``/``SEARCH SHELL`` directives plus doc dir and
    workspace root).  The registry is always searched as a final fallback by
    ``_comp_hover_markdown``.  If *search_dirs* is empty, a minimal fallback is
    built from *doc* and *workspace_root*.
    """
    from pathlib import Path
    fenum = _flavor_enum(flavor)
    lines = doc.source.splitlines()
    if position.line >= len(lines):
        return None
    line_text = lines[position.line]

    result = _word_at(line_text, position.character)
    if result is None:
        return None
    word, start_col, end_col = result

    # If caller didn't supply search_dirs, build a minimal fallback so that
    # doc dir and workspace root are always included.
    if not search_dirs:
        dirs: list[str] = []
        if doc.uri.startswith('file://'):
            dirs.append(str(Path(doc.uri[7:]).parent))
        if workspace_root and workspace_root not in dirs:
            dirs.append(workspace_root)
        search_dirs = tuple(dirs)

    # Check if this line is a COMPONENT instantiation.
    m = _COMP_INST_RE.match(line_text.strip())
    if m:
        comp_type = m.group(1)
        if word == comp_type:
            md = _comp_hover_markdown(comp_type, fenum, search_dirs)
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
