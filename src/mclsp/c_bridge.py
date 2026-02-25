"""
C bridge: generate a virtual C document from a McCode source file by
delegating to the ``mccode_antlr`` translator.

The translator already handles all the complexity — particle structs,
``#define``/``#undef`` macros, component structs, ``#line`` directives —
so we just call it and parse its output to build a position map.

For ``.instr`` files the full ``CTargetVisitor`` pipeline is used (which
resolves real components from the on-disk registry).  For ``.comp`` files
a lightweight mock instrument is synthesised so the same pipeline can be
reused without having a real instrument.

Position map
------------
Each :class:`CRegion` records:

* ``mccode_line``  – 1-based line of the ``#line`` directive target in the
  McCode file.
* ``virtual_line`` – 1-based line in the virtual C document where the
  content following that directive appears.

Given a McCode (line, col) inside a region the virtual position is::

    virtual_line = region.virtual_line + (mccode_line - region.mccode_line)

The reverse mapping (virtual → McCode) translates clangd diagnostics/hover
back to editor positions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mclsp.document import ParsedDocument


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CRegion:
    """A contiguous block of C code in the virtual document originating from
    a single McCode source file."""
    section: str         # 'declare', 'initialize', 'trace', etc. (best-effort)
    label: str           # human-readable, e.g. 'DECLARE', 'EXTEND first'

    mccode_line: int     # 1-based line in the McCode file (from #line directive)
    virtual_line: int    # 1-based line in the virtual C document (content start)
    content: str         # the C text for this region


@dataclass
class VirtualCDocument:
    """A complete C document stitched together by the mccode_antlr translator."""
    source_uri: str
    source_filename: str
    virtual_source: str
    regions: list[CRegion] = field(default_factory=list)
    # Path to the temp .c file written for clangd (set after construction).
    temp_path: str | None = None
    # C diagnostics from clang -fsyntax-only (list of dicts, set after check).
    c_diagnostics: list[dict] = field(default_factory=list)

    def mccode_to_virtual(self, line: int, col: int) -> tuple[int, int] | None:
        """Map a McCode (line, col) to a virtual-C (line, col), or None."""
        for reg in self.regions:
            last = reg.mccode_line + len(reg.content.splitlines()) - 1
            if reg.mccode_line <= line <= last:
                return reg.virtual_line + (line - reg.mccode_line), col
        return None

    def virtual_to_mccode(self, vline: int, vcol: int) -> tuple[str, int, int] | None:
        """Map a virtual-C (vline, vcol) to (source_uri, line, col), or None."""
        for reg in self.regions:
            vlast = reg.virtual_line + len(reg.content.splitlines()) - 1
            if reg.virtual_line <= vline <= vlast:
                return self.source_uri, reg.mccode_line + (vline - reg.virtual_line), vcol
        return None

    def region_at_mccode(self, line: int, col: int) -> CRegion | None:
        for reg in self.regions:
            last = reg.mccode_line + len(reg.content.splitlines()) - 1
            if reg.mccode_line <= line <= last:
                return reg
        return None


# ---------------------------------------------------------------------------
# Position-map extraction
# ---------------------------------------------------------------------------

# Matches  #line N "filename"
_LINE_RE = re.compile(r'^#line\s+(\d+)\s+"([^"]+)"', re.MULTILINE)


def _build_regions(virtual_source: str, source_filename: str) -> list[CRegion]:
    """Scan ``virtual_source`` for ``#line`` directives that reference
    ``source_filename`` and build a :class:`CRegion` for each run of lines."""
    regions: list[CRegion] = []
    vlines = virtual_source.splitlines()
    n = len(vlines)
    i = 0
    while i < n:
        m = _LINE_RE.match(vlines[i])
        if m and m.group(2) == source_filename:
            mccode_line = int(m.group(1))
            virtual_content_start = i + 1  # 0-based index of first content line
            # Collect content until the next #line or end of file
            j = virtual_content_start
            while j < n and not _LINE_RE.match(vlines[j]):
                j += 1
            content = '\n'.join(vlines[virtual_content_start:j])
            if content.strip():  # skip empty regions
                regions.append(CRegion(
                    section='',    # best-effort — not critical for position mapping
                    label='',
                    mccode_line=mccode_line,
                    virtual_line=virtual_content_start + 1,  # 1-based
                    content=content,
                ))
            i = j
        else:
            i += 1
    return regions


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------

def _safe_registries(flavor_enum, have: list) -> list:
    """Like ``ensure_registries`` but never raises on missing pooch config.

    Falls back to local installation registries (via ``$MCSTAS``/``$MCXTRACE``
    environment variables) when the pooch config key is absent, so the LSP
    works without any mccode_antlr configuration file.
    """
    import os
    from mccode_antlr.reader.registry import ensure_registries, LocalRegistry
    try:
        return ensure_registries(flavor_enum, have)
    except Exception:
        pass  # confuse NotFoundError or network error — try local fallback

    # Build a minimal set of registries from the local McCode installation.
    registries = list(have)
    env_vars = ['MCSTAS', 'MCXTRACE', 'MCCODE']
    for var in env_vars:
        path_str = os.environ.get(var, '')
        if path_str:
            from pathlib import Path
            p = Path(path_str)
            if p.is_dir():
                registries.append(LocalRegistry(var.lower(), str(p), priority=50))
    return registries


def _translate_instr(source: str, source_filename: str, flavor_enum,
                     extra_registries=None, search_dirs: list[str] | None = None) -> str | None:
    """Parse and translate a ``.instr`` source string to C using mccode_antlr.
    Returns the C text on success, or a C comment describing the error.

    *search_dirs* is an ordered list of directories to prepend as
    ``LocalRegistry`` entries so that components found via ``SEARCH`` /
    ``SEARCH SHELL`` directives (and the document's own directory) are
    available to the translator at the same priority as in the LSP handlers.
    """
    import sys
    from pathlib import Path as _Path
    from mccode_antlr.loader.loader import parse_mccode_instr
    from mccode_antlr.reader.registry import LocalRegistry
    from mccode_antlr.translators.c import CTargetVisitor
    # Prepend a LocalRegistry for each extra search directory (doc dir, SEARCH
    # dirs) so the translator finds local .comp files before the remote registry.
    local_regs = [
        LocalRegistry(f'mclsp_local_{i}', d, priority=150)
        for i, d in enumerate(search_dirs or [])
        if _Path(d).is_dir()
    ]
    registries = _safe_registries(flavor_enum, local_regs + list(extra_registries or []))
    # mccode_antlr prints progress messages to stdout; redirect to stderr so
    # they don't corrupt the LSP stdio stream.
    _stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        try:
            instr = parse_mccode_instr(source, registries, source=source_filename)
        except Exception as e:
            return f'/* mclsp: failed to parse {source_filename}:\n   {e}\n*/\n'
        try:
            return CTargetVisitor(instr, flavor=flavor_enum, line_directives=True).translate().getvalue()
        except Exception as e:
            return f'/* mclsp: failed to translate {source_filename}:\n   {e}\n*/\n'
    finally:
        sys.stdout = _stdout


def _translate_comp(source: str, source_filename: str, flavor_enum,
                    extra_registries=None) -> str | None:
    """Translate a ``.comp`` source string to C by wrapping it in a minimal
    mock instrument and using mccode_antlr's full pipeline.

    We register the component under its real filename so that ``#line``
    directives in the translator output reference ``source_filename`` rather
    than the ``InMemoryRegistry``'s fake ``/proc/memory/`` path.
    """
    from pathlib import Path
    from textwrap import dedent
    from mccode_antlr.loader.loader import parse_mccode_instr
    from mccode_antlr.reader.registry import InMemoryRegistry
    from mccode_antlr.translators.c import CTargetVisitor

    comp_name_match = re.search(r'DEFINE\s+COMPONENT\s+(\w+)', source)
    comp_name = comp_name_match.group(1) if comp_name_match else 'mclsp_comp'

    mock_instr = dedent(f"""\
        DEFINE INSTRUMENT _mclsp_mock_instrument()
        DECLARE %{{ %}}
        INITIALIZE %{{ %}}
        TRACE
        COMPONENT _mclsp_instance = {comp_name}() AT (0,0,0) ABSOLUTE
        FINALLY %{{ %}}
        END
    """)

    # Subclass InMemoryRegistry to return the real path for #line directives.
    class _NamedInMemoryRegistry(InMemoryRegistry):
        def path(self, name: str, ext: str = None) -> Path:
            full = self.fullname(name, ext)
            if full is not None:
                return Path(source_filename)
            return None

    in_memory = _NamedInMemoryRegistry('_mclsp_comp_registry', priority=200)
    in_memory.add_comp(comp_name, source)

    registries = _safe_registries(flavor_enum, [in_memory] + list(extra_registries or []))
    import sys
    _stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        try:
            instr = parse_mccode_instr(mock_instr, registries, source='_mclsp_mock.instr')
        except Exception as e:
            return f'/* mclsp: failed to parse {source_filename}:\n   {e}\n*/\n'
        try:
            return CTargetVisitor(instr, flavor=flavor_enum, line_directives=True).translate().getvalue()
        except Exception as e:
            return f'/* mclsp: failed to translate {source_filename}:\n   {e}\n*/\n'
    finally:
        sys.stdout = _stdout


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_virtual_c(doc: 'ParsedDocument', flavor: str = 'mcstas',
                    extra_registries=None,
                    search_dirs: list[str] | None = None) -> VirtualCDocument | None:
    """Build a :class:`VirtualCDocument` from a parsed McCode document.

    *extra_registries* is an optional list of
    :class:`~mccode_antlr.reader.registry.Registry` objects (e.g.
    :class:`~mccode_antlr.reader.registry.InMemoryRegistry`) that are
    prepended to the default on-disk registries.  This is mainly useful in
    tests to supply stub components without touching real component files.

    *search_dirs* is an ordered list of directory paths (strings) that are
    each wrapped in a :class:`~mccode_antlr.reader.registry.LocalRegistry`
    and prepended ahead of the normal registries.  This should include the
    document's own directory and any paths produced by ``SEARCH`` /
    ``SEARCH SHELL`` directives so the translator finds the same components
    that the LSP handlers find.

    Returns ``None`` if translation fails or produces no C output.
    """
    if doc.tree is None:
        return None

    try:
        from mccode_antlr import Flavor as McFlavor
        flavor_enum = McFlavor.MCXTRACE if flavor == 'mcxtrace' else McFlavor.MCSTAS
    except Exception:
        return None

    # Use the full path so that #line directives in the virtual C reference
    # the absolute path that clangd can resolve back to the open editor file.
    uri = doc.uri
    if uri.startswith('file://'):
        filename = uri[7:]   # '/absolute/path/to/foo.instr'
    else:
        filename = PurePosixPath(uri).name

    if doc.suffix == '.instr':
        virtual_source = _translate_instr(doc.source, filename, flavor_enum,
                                          extra_registries, search_dirs=search_dirs)
    elif doc.suffix == '.comp':
        virtual_source = _translate_comp(doc.source, filename, flavor_enum, extra_registries)
    else:
        return None

    if not virtual_source:
        return None

    regions = _build_regions(virtual_source, filename)
    vdoc = VirtualCDocument(
        source_uri=doc.uri,
        source_filename=filename,
        virtual_source=virtual_source,
        regions=regions,
    )
    vdoc.temp_path = _write_temp_c(doc.uri, virtual_source)
    return vdoc


def _write_temp_c(source_uri: str, content: str) -> str | None:
    """Write *content* to a stable temp ``.c`` file for clangd to analyse.

    The file lives in the system temp directory and is named after a hash of
    the source URI so it's stable across reloads.  Returns the file path, or
    ``None`` on failure.
    """
    import hashlib
    import tempfile
    from pathlib import Path

    try:
        name = 'mclsp_' + hashlib.md5(source_uri.encode()).hexdigest()[:12] + '.c'
        path = Path(tempfile.gettempdir()) / name
        path.write_text(content, encoding='utf-8')
        return str(path)
    except Exception:
        return None


def _remove_temp_c(temp_path: str | None) -> None:
    """Delete the clangd temp file when its McCode document is closed."""
    if temp_path is None:
        return
    try:
        from pathlib import Path
        Path(temp_path).unlink(missing_ok=True)
    except Exception:
        pass


# Regex matching a clang diagnostic line: file:line:col: severity: message
_CLANG_DIAG_RE = re.compile(
    r'^(.+?):(\d+):(\d+):\s+(error|warning|note):\s+(.+)$'
)


def check_virtual_c(temp_path: str, source_filename: str) -> list[dict]:
    """Run ``clang -fsyntax-only`` on *temp_path* and return diagnostics.

    Only diagnostics mapped back to *source_filename* (via ``#line``
    directives) are returned.  Each entry is a dict with keys:
    ``line`` (0-based), ``character`` (0-based), ``severity`` (LSP int),
    ``message`` (str).

    Returns an empty list if clang is not available or the check fails.
    """
    import os
    import subprocess
    from lsprotocol import types as lsp

    severity_map = {
        'error':   lsp.DiagnosticSeverity.Error,
        'warning': lsp.DiagnosticSeverity.Warning,
        'note':    lsp.DiagnosticSeverity.Hint,
    }

    # Try common clang names.
    clang = None
    for candidate in ('clang', 'clang-18', 'clang-17', 'clang-16', 'clang-15'):
        if any(
            os.path.isfile(os.path.join(d, candidate))
            for d in os.environ.get('PATH', '/usr/bin').split(os.pathsep)
        ):
            clang = candidate
            break
    if clang is None:
        return []

    try:
        result = subprocess.run(
            [clang, '-fsyntax-only', '-ferror-limit=50', temp_path],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []

    source_abs = os.path.abspath(source_filename)
    diagnostics: list[dict] = []
    for line in result.stderr.splitlines():
        m = _CLANG_DIAG_RE.match(line)
        if not m:
            continue
        file_ref, lineno, col, severity, message = m.groups()
        if os.path.abspath(file_ref) != source_abs:
            continue
        diagnostics.append({
            'line':      max(0, int(lineno) - 1),   # LSP is 0-based
            'character': max(0, int(col) - 1),
            'severity':  severity_map.get(severity, lsp.DiagnosticSeverity.Error),
            'message':   message,
        })
    return diagnostics
