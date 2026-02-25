"""
mclsp Language Server.

Registers LSP capabilities and wires the ANTLR4-backed handlers.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pygls.lsp.server import LanguageServer
from lsprotocol import types as lsp

logger = logging.getLogger(__name__)

from mclsp import __version__
from mclsp.document import parse_document, ParsedDocument
from mclsp.flavor import FlavorResolver, _flavor_from_string
from mclsp.handlers import get_diagnostics, get_completions, get_hover
from mclsp.c_bridge import build_virtual_c, check_virtual_c, VirtualCDocument, _remove_temp_c

# ---------------------------------------------------------------------------
# Server instance + per-session state
# ---------------------------------------------------------------------------

server = LanguageServer(
    'mclsp', __version__,
    text_document_sync_kind=lsp.TextDocumentSyncKind.Full,
)

# Per-URI document store (populated on open/change).
_docs: dict[str, ParsedDocument] = {}

# Virtual C document cache (one per McCode document).
_virtual_c: dict[str, VirtualCDocument] = {}

# Semantic error diagnostics from mccode-antlr (e.g. unknown component parameter).
_semantic_error_diags: dict[str, list[lsp.Diagnostic]] = {}

# McDoc header mismatch diagnostics (for open .comp files).
_mcdoc_diags: dict[str, list[lsp.Diagnostic]] = {}

# Metadata block validation diagnostics (for METADATA ... %{ ... %} blocks).
_metadata_diags: dict[str, list[lsp.Diagnostic]] = {}

# Flavor resolver — single instance, shared across all handlers.
_resolver = FlavorResolver()

# Debounce state: pending asyncio tasks for each URI.
_pending_tasks: dict[str, asyncio.Task] = {}

# Thread pool for the slow CTargetVisitor translation (keeps event loop free).
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mclsp-translate')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import re as _re

# Patterns for RuntimeErrors raised by InstrVisitor during semantic analysis.
_UNKNOWN_PARAM_RE = _re.compile(
    r"^(\w+) is not a known (?:DEFINITION or SETTING) parameter for (\w+)$"
)


def _semantic_diags_from_exception(exc: Exception, source: str) -> list[lsp.Diagnostic]:
    """Convert a known mccode-antlr RuntimeError to LSP diagnostics if possible."""
    msg = str(exc)
    diags: list[lsp.Diagnostic] = []

    m = _UNKNOWN_PARAM_RE.match(msg)
    if m:
        param_name, comp_type = m.group(1), m.group(2)
        # Find the line(s) where this parameter is used in an instantiation of comp_type.
        for line_idx, line in enumerate(source.splitlines()):
            # Look for "<param_name> =" on lines that are near an instantiation of comp_type.
            if _re.search(rf'\b{_re.escape(param_name)}\s*=', line):
                col = _re.search(rf'\b{_re.escape(param_name)}\b', line).start()
                diags.append(lsp.Diagnostic(
                    range=lsp.Range(
                        start=lsp.Position(line=line_idx, character=col),
                        end=lsp.Position(line=line_idx, character=col + len(param_name)),
                    ),
                    message=f'`{param_name}` is not a parameter of `{comp_type}`',
                    severity=lsp.DiagnosticSeverity.Error,
                    source='mclsp',
                ))
                break  # report only the first occurrence

    return diags


def _update_mcdoc_diags(uri: str) -> None:
    """Compute McDoc header mismatch diagnostics for a .comp document.

    For each open ``.comp`` file, check that the ``%P`` parameter section of
    the McDoc block comment lists exactly the parameters declared in the
    component body, and emit LSP Warning diagnostics for any mismatches:

    - A parameter defined in the component but absent from the McDoc ``%P``
      section → warning at the parameter name token.
    - A McDoc ``%P`` entry that names a parameter not defined in the
      component → warning at that line in the block comment.
    """
    doc = _docs.get(uri)
    if doc is None or doc.suffix != '.comp' or doc.tree is None:
        _mcdoc_diags.pop(uri, None)
        return

    # ── Extract declared parameter names from the ANTLR parse tree ──────────
    try:
        from mccode_antlr.grammar.McCompParser import McCompParser
        comp_def = doc.tree.component_definition()
        ps = comp_def.component_parameter_set()
        input_params: list[str] = []
        output_params: list[str] = []
        for section, target in (
            (ps.component_define_parameters(), input_params),
            (ps.component_set_parameters(),    input_params),
            (ps.component_out_parameters(),    output_params),
        ):
            if section is not None:
                for p in section.component_parameters().component_parameter():
                    ident = p.Identifier()
                    target.append(ident[0].getText() if isinstance(ident, list) else ident.getText())
    except Exception:
        logger.warning('_update_mcdoc_diags: failed to extract params for %s', uri, exc_info=True)
        _mcdoc_diags.pop(uri, None)
        return

    # ── Parse the McDoc block comment ────────────────────────────────────────
    try:
        from mccode_antlr.format._mcdoc import check_mcdoc_params, extract_mcdoc_from_token
        import re as _re2
        m = _re2.search(r'/\*.*?\*/', doc.source, _re2.DOTALL)
        existing = extract_mcdoc_from_token(m.group()) if m else None
    except Exception:
        logger.warning('_update_mcdoc_diags: failed to parse mcdoc for %s', uri, exc_info=True)
        _mcdoc_diags.pop(uri, None)
        return

    warnings = check_mcdoc_params(existing, input_params, output_params)
    logger.debug('_update_mcdoc_diags: %s → params=%s warnings=%s', uri, input_params, warnings)
    if not warnings:
        _mcdoc_diags.pop(uri, None)
        return

    source_lines = doc.source.splitlines()
    diags: list[lsp.Diagnostic] = []

    for warning in warnings:
        # "parameter 'X' is not documented in the McDoc header"
        # → point at the parameter token in the SETTING/DEFINITION/OUTPUT lines
        if "is not documented" in warning:
            param = _re.search(r"'(\w+)'", warning)
            if param:
                pname = param.group(1)
                rng = _find_param_in_source(pname, source_lines)
                diags.append(lsp.Diagnostic(
                    range=rng,
                    message=f'Parameter `{pname}` is not documented in the McDoc `%P` section',
                    severity=lsp.DiagnosticSeverity.Warning,
                    source='mclsp-mcdoc',
                ))

        # "McDoc documents 'X' which is not a known parameter"
        # → point at the `* X:` line inside the block comment
        elif "which is not a known parameter" in warning:
            param = _re.search(r"'(\w+)'", warning)
            if param:
                pname = param.group(1)
                rng = _find_mcdoc_param_in_source(pname, source_lines)
                diags.append(lsp.Diagnostic(
                    range=rng,
                    message=f'McDoc `%P` documents `{pname}` which is not a declared parameter',
                    severity=lsp.DiagnosticSeverity.Warning,
                    source='mclsp-mcdoc',
                ))

        # "McDoc header is missing"
        # → point at the DEFINE COMPONENT line; also emit per-parameter warnings
        elif "header is missing" in warning:
            rng = _find_define_component_in_source(source_lines)
            diags.append(lsp.Diagnostic(
                range=rng,
                message='McDoc header comment is missing',
                severity=lsp.DiagnosticSeverity.Warning,
                source='mclsp-mcdoc',
            ))
            # Also warn on each undocumented parameter so the user knows what to add
            for pname in sorted(set(input_params) | set(output_params)):
                prng = _find_param_in_source(pname, source_lines)
                diags.append(lsp.Diagnostic(
                    range=prng,
                    message=f'Parameter `{pname}` is not documented (McDoc `%P` section is missing)',
                    severity=lsp.DiagnosticSeverity.Warning,
                    source='mclsp-mcdoc',
                ))

    if diags:
        _mcdoc_diags[uri] = diags
    else:
        _mcdoc_diags.pop(uri, None)


def _find_define_component_in_source(lines: list[str]) -> lsp.Range:
    """Find the DEFINE COMPONENT line to anchor a 'header is missing' diagnostic."""
    for i, line in enumerate(lines):
        if _re.match(r'\s*DEFINE\s+COMPONENT\b', line, _re.IGNORECASE):
            return lsp.Range(
                start=lsp.Position(line=i, character=0),
                end=lsp.Position(line=i, character=len(line.rstrip())),
            )
    return lsp.Range(start=lsp.Position(line=0, character=0),
                     end=lsp.Position(line=0, character=0))


def _find_param_in_source(name: str, lines: list[str]) -> lsp.Range:
    """Find the parameter name token in SETTING/DEFINITION/OUTPUT parameter lines."""
    in_params = False
    for i, line in enumerate(lines):
        upper = line.upper()
        if ('SETTING PARAMETERS' in upper or 'DEFINITION PARAMETERS' in upper
                or 'OUTPUT PARAMETERS' in upper):
            in_params = True
        if in_params:
            m = _re.search(rf'\b{_re.escape(name)}\b', line)
            if m:
                return lsp.Range(
                    start=lsp.Position(line=i, character=m.start()),
                    end=lsp.Position(line=i, character=m.end()),
                )
            # Stop scanning after 'END' keyword or far from param section
            if _re.match(r'\s*END\s*$', line, _re.IGNORECASE):
                break
    return lsp.Range(start=lsp.Position(line=0, character=0),
                     end=lsp.Position(line=0, character=0))


def _find_mcdoc_param_in_source(name: str, lines: list[str]) -> lsp.Range:
    """Find the `* name:` line for an extra-documented parameter in the block comment."""
    in_block = False
    for i, line in enumerate(lines):
        if '/*' in line:
            in_block = True
        if in_block:
            m = _re.search(rf'\*\s*{_re.escape(name)}\s*:', line)
            if m:
                col = line.index('*', line.find('*'))
                return lsp.Range(
                    start=lsp.Position(line=i, character=col),
                    end=lsp.Position(line=i, character=len(line.rstrip())),
                )
        if '*/' in line:
            in_block = False
    return lsp.Range(start=lsp.Position(line=0, character=0),
                     end=lsp.Position(line=0, character=0))


def _instr_search_dirs(uri: str, tree) -> list[str]:
    """Return an ordered list of directories to search for component files.

    Processes ``SEARCH "path"`` and ``SEARCH SHELL "cmd"`` nodes from the
    instrument parse tree, then appends the document directory and workspace root.
    """
    import subprocess

    dirs: list[str] = []

    # Process SEARCH nodes from the parse tree
    try:
        it = tree.instrument_definition().instrument_trace()
        for child in (it.children or []):
            cname = type(child).__name__
            if cname == 'SearchPathContext':
                # SEARCH "literal-path"
                raw = child.StringLiteral().getText().strip('"\'')
                p = Path(raw).expanduser()
                if p.is_dir():
                    dirs.append(str(p.resolve()))
            elif cname == 'SearchShellContext':
                # SEARCH SHELL "command" — run it and use stdout as path
                cmd = child.StringLiteral().getText().strip('"\'')
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=5
                    )
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if line:
                            p = Path(line).expanduser()
                            if p.is_dir() and str(p.resolve()) not in dirs:
                                dirs.append(str(p.resolve()))
                except Exception as e:
                    logger.debug('_instr_search_dirs: SEARCH SHELL %r failed: %s', cmd, e)
    except Exception:
        pass

    # Always include the document directory and workspace root as fallback.
    if uri.startswith('file://'):
        doc_dir = str(Path(uri[7:]).parent)
        if doc_dir not in dirs:
            dirs.append(doc_dir)
    ws_root = _resolver._workspace_root
    if ws_root and ws_root not in dirs:
        dirs.append(ws_root)

    return dirs


# ---------------------------------------------------------------------------
# Metadata block validation
# ---------------------------------------------------------------------------

def _mime_to_language_id(mime: str) -> str | None:
    """Map a MIME type string to a VS Code language ID, or None if unknown."""
    m = mime.lower().split(';')[0].strip()
    _MAP = {
        'application/json': 'json',
        'text/json': 'json',
        'text/x-yaml': 'yaml',
        'application/yaml': 'yaml',
        'application/x-yaml': 'yaml',
        'text/yaml': 'yaml',
        'text/xml': 'xml',
        'application/xml': 'xml',
        'application/xhtml+xml': 'html',
        'text/html': 'html',
        'text/x-python': 'python',
        'application/x-python': 'python',
        'text/x-csrc': 'c',
        'text/x-c': 'c',
        'text/x-chdr': 'c',
        'text/x-c++src': 'cpp',
        'text/javascript': 'javascript',
        'application/javascript': 'javascript',
        'text/x-sh': 'shellscript',
        'application/x-sh': 'shellscript',
        'application/toml': 'toml',
        'text/markdown': 'markdown',
        'text/plain': 'plaintext',
    }
    return _MAP.get(m)


def _iter_metadata_contexts(tree):
    """Yield every MetadataContext node in the parse tree (depth-first)."""
    if tree is None:
        return
    if type(tree).__name__ == 'MetadataContext':
        yield tree
    children = getattr(tree, 'children', None)
    if children:
        for child in children:
            yield from _iter_metadata_contexts(child)


def _validate_metadata_block(mime: str, content: str, block_start_line: int) -> list[lsp.Diagnostic]:
    """Validate *content* according to *mime*, mapping errors into the original file.

    *block_start_line* is the 0-based LSP line of the ``%{`` sentinel.  The
    content string starts immediately after ``%{`` so its first character (the
    newline) is still on *block_start_line*; subsequent lines start at
    *block_start_line + n* for the *n*-th newline.
    """
    diags: list[lsp.Diagnostic] = []
    m = mime.lower().split(';')[0].strip()

    if 'json' in m:
        import json as _json
        try:
            _json.loads(content)
        except _json.JSONDecodeError as e:
            # e.lineno is 1-based relative to content; line 1 is the char
            # immediately after %{ (still on block_start_line).
            lsp_line = block_start_line + (e.lineno - 1)
            lsp_col = max(0, e.colno - 1)
            diags.append(lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=lsp_line, character=lsp_col),
                    end=lsp.Position(line=lsp_line, character=lsp_col + 1),
                ),
                message=f'JSON: {e.msg}',
                severity=lsp.DiagnosticSeverity.Error,
                source='mclsp-metadata',
            ))

    elif 'yaml' in m:
        try:
            import yaml as _yaml
            _yaml.safe_load(content)
        except Exception as e:
            line = 0
            col = 0
            pm = getattr(e, 'problem_mark', None)
            if pm is not None:
                line = pm.line    # 0-based relative to content
                col = pm.column   # 0-based
            lsp_line = block_start_line + line
            diags.append(lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=lsp_line, character=col),
                    end=lsp.Position(line=lsp_line, character=col + 1),
                ),
                message=f'YAML: {e.problem if hasattr(e, "problem") else e}',
                severity=lsp.DiagnosticSeverity.Error,
                source='mclsp-metadata',
            ))

    elif 'xml' in m:
        import xml.etree.ElementTree as _ET
        try:
            _ET.fromstring(content)
        except _ET.ParseError as e:
            row, col = e.position  # both 1-based
            lsp_line = block_start_line + (row - 1)
            lsp_col = max(0, col - 1)
            diags.append(lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=lsp_line, character=lsp_col),
                    end=lsp.Position(line=lsp_line, character=lsp_col + 1),
                ),
                message=f'XML: {e}',
                severity=lsp.DiagnosticSeverity.Error,
                source='mclsp-metadata',
            ))

    return diags


def _update_metadata_diags(uri: str) -> None:
    """Validate every METADATA block in *uri* and store results in *_metadata_diags*."""
    doc = _docs.get(uri)
    if doc is None or doc.tree is None:
        _metadata_diags.pop(uri, None)
        return

    diags: list[lsp.Diagnostic] = []
    for ctx in _iter_metadata_contexts(doc.tree):
        ub = ctx.unparsed_block()
        if ub is None:
            continue
        mime_tok = getattr(ctx, 'mime', None)
        if mime_tok is None:
            continue
        mime = mime_tok.text.strip('"\'')
        raw = str(ub.UnparsedBlock())
        # Strip %{ ... %} sentinels; content string may start with \n.
        content = raw[2:-2] if raw.startswith('%{') and raw.endswith('%}') else raw
        block_start = (ub.start.line - 1) if ub.start else 0  # convert to 0-based
        block_stop = (ub.stop.line - 1) if ub.stop else block_start

        block_diags = _validate_metadata_block(mime, content, block_start)
        diags.extend(block_diags)
        logger.debug('_update_metadata_diags: %s mime=%r → %d diags', uri, mime, len(block_diags))

    _metadata_diags[uri] = diags


def _metadata_blocks_info(uri: str) -> list[dict]:
    """Return a list of dicts describing every METADATA block in *uri*.

    Each dict has keys: ``mime``, ``languageId``, ``name``, ``content``,
    ``startLine`` (0-based %{ line), ``endLine`` (0-based %} line).  Used by
    the VS Code extension to create virtual documents with the correct language
    so that VS Code's built-in language servers provide completions and hover.
    """
    doc = _docs.get(uri)
    if doc is None or doc.tree is None:
        return []
    blocks = []
    for ctx in _iter_metadata_contexts(doc.tree):
        ub = ctx.unparsed_block()
        if ub is None:
            continue
        mime_tok = getattr(ctx, 'mime', None)
        name_tok = getattr(ctx, 'name', None)
        mime = mime_tok.text.strip('"\'') if mime_tok else ''
        name = name_tok.text.strip('"\'') if name_tok else ''
        raw = str(ub.UnparsedBlock())
        content = raw[2:-2] if raw.startswith('%{') and raw.endswith('%}') else raw
        blocks.append({
            'mime': mime,
            'languageId': _mime_to_language_id(mime),
            'name': name,
            'content': content,
            'startLine': (ub.start.line - 1) if ub.start else 0,
            'endLine': (ub.stop.line - 1) if ub.stop else 0,
        })
    return blocks


def _update_instr_semantic_diags(uri: str) -> None:
    """Check component instantiations in a .instr file for unknown component types
    and unknown parameter names, emitting LSP Error diagnostics for each problem."""
    doc = _docs.get(uri)
    if doc is None or doc.suffix != '.instr' or doc.tree is None:
        _semantic_error_diags.pop(uri, None)
        return

    try:
        it = doc.tree.instrument_definition().instrument_trace()
    except Exception:
        return

    search_dirs = _instr_search_dirs(uri, doc.tree)

    from mclsp.handlers.completion import _cached_reader, _flavor_enum
    flavor = _resolver.resolve(uri, doc.source)
    fenum = _flavor_enum(flavor)
    reader = _cached_reader(fenum)

    diags: list[lsp.Diagnostic] = []

    for ci in it.component_instance():
        ct = ci.component_type()
        comp_name = ct.getText()
        tok = ct.start
        # ANTLR lines are 1-based; LSP is 0-based.
        type_line = tok.line - 1
        type_col = tok.column
        type_end = type_col + len(comp_name)

        # ── Resolve component (local dirs first, then registry) ──────────────
        comp = None
        source_for_comp: str | None = None

        # Check in-memory override (open .comp file)
        from mccode_antlr.reader.reader import component_cache
        override = component_cache.get_override(comp_name)
        if override is not None:
            try:
                from mccode_antlr.reader.reader import Reader as _Reader
                tmp = _Reader(flavor=fenum)
                tmp.inject_source(comp_name, override)
                comp = tmp.get_component(comp_name)
            except Exception:
                pass
        else:
            for d in search_dirs:
                candidate = Path(d) / f'{comp_name}.comp'
                if candidate.is_file():
                    try:
                        src = candidate.read_text(encoding='utf-8', errors='replace')
                        from mccode_antlr.reader.reader import Reader as _Reader
                        tmp = _Reader(flavor=fenum)
                        tmp.inject_source(comp_name, src, filename=str(candidate))
                        comp = tmp.get_component(comp_name)
                    except Exception:
                        pass
                    break
            if comp is None and reader.known(comp_name):
                try:
                    comp = reader.get_component(comp_name)
                except Exception:
                    pass

        if comp is None:
            diags.append(lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=type_line, character=type_col),
                    end=lsp.Position(line=type_line, character=type_end),
                ),
                message=f'Unknown component type `{comp_name}`',
                severity=lsp.DiagnosticSeverity.Error,
                source='mclsp',
            ))
            continue

        # ── Check parameter names ────────────────────────────────────────────
        known_params = set()
        for p in list(comp.define or []) + list(comp.setting or []):
            known_params.add(p.name)

        ip = ci.instance_parameters()
        if ip:
            for assign in ip.instance_parameter():
                ident = assign.Identifier()
                pname = ident.getText()
                if pname not in known_params:
                    pline = ident.symbol.line - 1
                    pcol = ident.symbol.column
                    diags.append(lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line=pline, character=pcol),
                            end=lsp.Position(line=pline, character=pcol + len(pname)),
                        ),
                        message=f'`{pname}` is not a parameter of `{comp_name}`',
                        severity=lsp.DiagnosticSeverity.Error,
                        source='mclsp',
                    ))

    _semantic_error_diags[uri] = diags


def _publish_diagnostics(uri: str) -> None:
    doc = _docs.get(uri)
    if doc is None:
        return
    diags = get_diagnostics(doc)
    # Merge in semantic diagnostics from mccode-antlr (unknown params, etc.)
    diags.extend(_semantic_error_diags.get(uri, []))
    # Merge in McDoc header mismatch diagnostics for .comp files.
    diags.extend(_mcdoc_diags.get(uri, []))
    # Merge in METADATA block validation diagnostics (JSON/YAML/XML syntax checks).
    diags.extend(_metadata_diags.get(uri, []))
    # Merge in C diagnostics from clang -fsyntax-only (if available)
    vdoc = _virtual_c.get(uri)
    if vdoc and vdoc.c_diagnostics:
        for cd in vdoc.c_diagnostics:
            diags.append(lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=cd['line'], character=cd['character']),
                    end=lsp.Position(line=cd['line'], character=cd['character'] + 1),
                ),
                message=cd['message'],
                severity=cd['severity'],
                source='clang',
            ))
    logger.debug('_publish_diagnostics: %s → %d diagnostics', uri, len(diags))
    server.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


def _update_virtual_c(uri: str) -> None:
    """(Re)build the virtual C document for *uri*, cache it, and push to client.
    Runs synchronously — call from the thread executor only."""
    doc = _docs.get(uri)
    if doc is None:
        _virtual_c.pop(uri, None)
        logger.debug('_update_virtual_c: no doc for %s', uri)
        return
    flavor = _resolver.resolve(uri, doc.source)
    flavor_str = flavor.name.lower() if hasattr(flavor, 'name') else str(flavor).lower()
    logger.debug('_update_virtual_c: building for %s (flavor=%s)', uri, flavor_str)
    try:
        vdoc = build_virtual_c(doc, flavor=flavor_str)
    except Exception as e:
        logger.error('_update_virtual_c: build_virtual_c raised:\n%s', traceback.format_exc())
        _virtual_c.pop(uri, None)
        # Surface known semantic errors (e.g. unknown component parameter) as diagnostics.
        semantic_diags = _semantic_diags_from_exception(e, doc.source)
        if semantic_diags:
            _semantic_error_diags[uri] = semantic_diags
        return
    _semantic_error_diags.pop(uri, None)
    if vdoc is not None:
        logger.debug('_update_virtual_c: built %d chars for %s', len(vdoc.virtual_source), uri)
        if vdoc.temp_path:
            vdoc.c_diagnostics = check_virtual_c(vdoc.temp_path, vdoc.source_filename)
            logger.debug('_update_virtual_c: clang found %d diagnostics for %s',
                         len(vdoc.c_diagnostics), uri)
        _virtual_c[uri] = vdoc
        _push_virtual_c(uri, vdoc)
    else:
        logger.warning('_update_virtual_c: build_virtual_c returned None for %s', uri)
        _virtual_c.pop(uri, None)


async def _debounced_update(uri: str, delay: float = 0.5) -> None:
    """Wait *delay* seconds, then publish diagnostics and rebuild virtual C.

    Called via asyncio.create_task so it can be cancelled if the document
    changes again before the delay expires (debounce while typing).
    The slow virtual-C build (+ clang check) runs in a thread so the event
    loop stays free.  We publish diagnostics twice: once immediately with
    ANTLR errors (fast), and again after clang finishes (adds C errors).
    """
    await asyncio.sleep(delay)
    logger.debug('_debounced_update: running for %s', uri)
    _update_mcdoc_diags(uri)               # fast: McDoc header check for .comp files
    _update_instr_semantic_diags(uri)      # fast: unknown component types / parameters
    _update_metadata_diags(uri)            # fast: JSON/YAML/XML syntax in METADATA blocks
    _publish_diagnostics(uri)              # fast: ANTLR + McDoc + semantic errors
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _update_virtual_c, uri)
    _publish_diagnostics(uri)              # slow: ANTLR + McDoc + clang errors


def _schedule_update(uri: str, delay: float = 0.5) -> None:
    """Cancel any pending update for *uri* and schedule a new debounced one."""
    existing = _pending_tasks.pop(uri, None)
    if existing is not None:
        existing.cancel()
    task = asyncio.ensure_future(_debounced_update(uri, delay))
    _pending_tasks[uri] = task
    task.add_done_callback(lambda t: _pending_tasks.pop(uri, None))


def _virtual_uri(uri: str) -> str:
    """Compute the mccode-c:// URI for a McCode file URI."""
    return 'mccode-c://' + uri.replace('file://', '', 1) + '.c'


def _push_virtual_c(uri: str, vdoc) -> None:
    """Push virtual C content to the client via a custom notification."""
    try:
        server.protocol.notify('$/mclsp/virtualCDocumentContent', {
            'uri': uri,
            'virtualUri': _virtual_uri(uri),
            'content': vdoc.virtual_source,
            'tempPath': vdoc.temp_path,  # real filesystem path for clangd
        })
    except Exception:
        pass  # Protocol not connected (e.g. during unit tests)


def _flavor_from_init_options(options) -> str | None:
    """Extract the ``flavor`` key from ``initializationOptions`` if present."""
    if options is None:
        return None
    if isinstance(options, dict):
        return options.get('flavor')
    # Some clients send a typed object; try attribute access
    return getattr(options, 'flavor', None)


def _apply_log_level(raw: str | None) -> None:
    """Set the root logger level from a string like 'debug', 'warning', etc."""
    if not raw:
        return
    level = getattr(logging, raw.upper(), None)
    if isinstance(level, int):
        logging.getLogger().setLevel(level)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@server.feature(lsp.INITIALIZE)
def on_initialize(params: lsp.InitializeParams):
    global _resolver
    workspace_root = None
    if params.root_uri:
        # Strip the file:// scheme for local path use
        uri = params.root_uri
        if uri.startswith('file://'):
            workspace_root = uri[7:]
        else:
            workspace_root = uri

    _resolver = FlavorResolver(workspace_root=workspace_root)

    opts = getattr(params, 'initialization_options', None)
    # Honor an explicit flavor in initializationOptions
    raw = _flavor_from_init_options(opts)
    if raw:
        flavor = _flavor_from_string(raw)
        if flavor is not None:
            _resolver.set_workspace_flavor(flavor)

    # Honor an explicit log level in initializationOptions
    raw_level = opts.get('logLevel') if isinstance(opts, dict) else getattr(opts, 'logLevel', None)
    _apply_log_level(raw_level)


@server.feature(lsp.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(params: lsp.DidChangeConfigurationParams):
    """Handle live config changes (e.g. user changes ``mccode.flavor`` in VS Code)."""
    settings = getattr(params, 'settings', None) or {}
    if isinstance(settings, dict):
        mccode = settings.get('mccode', {})
        raw = mccode.get('flavor', None)
        if raw is not None:
            flavor = _flavor_from_string(raw)
            _resolver.set_workspace_flavor(flavor)  # None clears the override
        _apply_log_level(mccode.get('logLevel'))


@server.feature(lsp.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def did_change_watched_files(params):
    """Acknowledge file-system watch notifications (no action needed for now)."""
    pass


# ---------------------------------------------------------------------------
# Text document synchronisation
# ---------------------------------------------------------------------------

def _uri_to_comp_name(uri: str) -> str | None:
    """Return the component name (stem) if *uri* points to a ``.comp`` file."""
    from urllib.parse import urlparse
    from pathlib import PurePosixPath
    path = PurePosixPath(urlparse(uri).path)
    return path.stem if path.suffix == '.comp' else None


def _invalidate_comp_caches(comp_name: str, *, evict_reader: bool = True) -> None:
    """Flush all caches that hold data derived from *comp_name*'s definition.

    When *evict_reader* is True (default) the component is also removed from
    every cached Reader instance so it will be re-fetched on the next access.
    Call with ``evict_reader=False`` when you are about to call
    ``inject_source`` yourself (avoids a double-parse).
    """
    from mccode_antlr import Flavor
    from mclsp.handlers.completion import _cached_reader
    from mclsp.handlers.hover import _comp_hover_markdown

    for flavor in Flavor:
        try:
            reader = _cached_reader(flavor)
            if evict_reader:
                reader.evict(comp_name)
            else:
                # Still clear reader.components so inject_source can overwrite it.
                reader.components.pop(comp_name, None)
        except Exception:
            pass
    _comp_hover_markdown.cache_clear()


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: lsp.DidOpenTextDocumentParams):
    td = params.text_document
    uri, source = td.uri, td.text
    _docs[uri] = parse_document(uri, source)
    # Run inference eagerly on open so hover/completion get the right flavor fast
    _resolver.resolve(uri, source)
    # If this is a .comp being opened, inject its content into all readers.
    comp_name = _uri_to_comp_name(uri)
    if comp_name:
        from urllib.parse import urlparse
        filename = urlparse(uri).path
        _invalidate_comp_caches(comp_name, evict_reader=False)
        from mccode_antlr import Flavor
        from mclsp.handlers.completion import _cached_reader
        for flavor in Flavor:
            try:
                _cached_reader(flavor).inject_source(comp_name, source, filename=filename)
            except Exception:
                pass
    # Publish immediately on open (not debounced — file is already saved)
    _publish_diagnostics(uri)
    _schedule_update(uri, delay=0.0)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(params: lsp.DidChangeTextDocumentParams):
    uri = params.text_document.uri
    source = params.content_changes[-1].text
    _docs[uri] = parse_document(uri, source)
    # Re-infer flavor: a new COMPONENT line may settle a previously ambiguous doc
    _resolver.re_infer(uri, source)
    # If this is a .comp being edited, inject the live source into all readers.
    comp_name = _uri_to_comp_name(uri)
    if comp_name:
        from urllib.parse import urlparse
        filename = urlparse(uri).path
        _invalidate_comp_caches(comp_name, evict_reader=False)
        from mccode_antlr import Flavor
        from mclsp.handlers.completion import _cached_reader
        for flavor in Flavor:
            try:
                _cached_reader(flavor).inject_source(comp_name, source, filename=filename)
            except Exception:
                pass
    # Debounce: wait for the user to pause typing before doing heavy work
    _schedule_update(uri, delay=0.5)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def did_save(params: lsp.DidSaveTextDocumentParams):
    """When a `.comp` file is saved, evict stale caches so the next hover/
    completion re-reads from the freshly-written disk file."""
    uri = params.text_document.uri
    comp_name = _uri_to_comp_name(uri)
    if comp_name:
        from urllib.parse import urlparse
        from mccode_antlr.reader.reader import component_cache
        # Remove source override — the file is now on disk.
        component_cache.clear_override(comp_name)
        # Evict mtime-keyed entry so it is re-read from the new disk content.
        try:
            abs_path = Path(urlparse(uri).path).resolve()
            component_cache.evict(abs_path)
        except Exception:
            pass
        # Evict from all cached readers (triggers re-parse on next access).
        _invalidate_comp_caches(comp_name, evict_reader=True)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: lsp.DidCloseTextDocumentParams):
    uri = params.text_document.uri
    existing = _pending_tasks.pop(uri, None)
    if existing is not None:
        existing.cancel()
    _docs.pop(uri, None)
    vdoc = _virtual_c.pop(uri, None)
    _remove_temp_c(vdoc.temp_path if vdoc else None)
    _semantic_error_diags.pop(uri, None)
    _mcdoc_diags.pop(uri, None)
    _metadata_diags.pop(uri, None)
    _resolver.forget(uri)
    # If a .comp was closed, clear its source override and evict from readers
    # so next access re-reads from disk (handles external edits too).
    comp_name = _uri_to_comp_name(uri)
    if comp_name:
        from mccode_antlr.reader.reader import component_cache
        component_cache.clear_override(comp_name)
        _invalidate_comp_caches(comp_name, evict_reader=True)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=['=', '(', ',']),
)
def completion(params: lsp.CompletionParams) -> lsp.CompletionList | None:
    uri = params.text_document.uri
    doc = _docs.get(uri)
    if doc is None:
        return None
    flavor = _resolver.resolve(uri, doc.source)
    items = get_completions(doc, params.position, flavor=flavor)
    return lsp.CompletionList(is_incomplete=False, items=items)


# ---------------------------------------------------------------------------
# Hover
# ---------------------------------------------------------------------------

@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def hover(params: lsp.HoverParams) -> lsp.Hover | None:
    uri = params.text_document.uri
    doc = _docs.get(uri)
    if doc is None:
        return None
    flavor = _resolver.resolve(uri, doc.source)
    search_dirs = _instr_search_dirs(uri, doc.tree) if doc.tree else []
    return get_hover(doc, params.position, flavor=flavor, search_dirs=tuple(search_dirs))


# ---------------------------------------------------------------------------
# Go-to-definition
# ---------------------------------------------------------------------------

def _comp_type_at(doc, position: lsp.Position) -> str | None:
    """Return the component type name if *position* is on the type in a COMPONENT line."""
    import re as _re2
    lines = doc.source.splitlines()
    if position.line >= len(lines):
        return None
    line = lines[position.line]
    m = _re2.match(r'COMPONENT\s+\w+\s*=\s*(\w+)', line.strip(), _re2.IGNORECASE)
    if not m:
        return None
    comp_type = m.group(1)
    # Check cursor is on the type token, not the instance name
    start = line.index(comp_type, line.index('='))
    if start <= position.character <= start + len(comp_type):
        return comp_type
    return None


def _resolve_comp_file(comp_name: str, flavor, search_dirs: tuple[str, ...]) -> str | None:
    """Return the absolute file:// URI of the .comp file for *comp_name*, or None."""
    from mclsp.handlers.completion import _cached_reader, _flavor_enum
    from mccode_antlr.reader.reader import component_cache

    # In-memory override: try to find the file path from the reader's components dict
    if component_cache.get_override(comp_name) is not None:
        fenum = _flavor_enum(flavor)
        reader = _cached_reader(fenum)
        comp = reader.components.get(comp_name)
        if comp is not None:
            filename = getattr(comp, 'filename', None) or getattr(comp, 'source', None)
            if filename and Path(filename).is_file():
                return Path(filename).resolve().as_uri()

    # Local directories (document dir, workspace root)
    for d in search_dirs:
        candidate = Path(d) / f'{comp_name}.comp'
        if candidate.is_file():
            return candidate.resolve().as_uri()

    # Registry
    fenum = _flavor_enum(flavor)
    reader = _cached_reader(fenum)
    try:
        path = reader.locate(comp_name, ext='.comp')
        if path and path.is_file():
            return path.resolve().as_uri()
    except Exception:
        pass
    return None


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def definition(params: lsp.DefinitionParams) -> lsp.Location | None:
    """Go-to-definition: navigate to the .comp file for a component type name."""
    uri = params.text_document.uri
    doc = _docs.get(uri)
    if doc is None:
        return None

    comp_name = _comp_type_at(doc, params.position)
    if comp_name is None:
        return None

    flavor = _resolver.resolve(uri, doc.source)
    # Use _instr_search_dirs so SEARCH / SEARCH SHELL paths are honoured.
    search_dirs = _instr_search_dirs(uri, doc.tree) if doc.tree else []

    comp_uri = _resolve_comp_file(comp_name, flavor, tuple(search_dirs))
    if comp_uri is None:
        return None

    # Point to the DEFINE COMPONENT line if possible, otherwise start of file.
    target_range = lsp.Range(
        start=lsp.Position(line=0, character=0),
        end=lsp.Position(line=0, character=0),
    )
    try:
        from urllib.parse import urlparse
        comp_path = urlparse(comp_uri).path
        lines = open(comp_path, encoding='utf-8', errors='replace').readlines()
        import re as _re2
        for i, line in enumerate(lines):
            if _re2.match(r'\s*DEFINE\s+COMPONENT\b', line, _re2.IGNORECASE):
                end_col = len(line.rstrip())
                target_range = lsp.Range(
                    start=lsp.Position(line=i, character=0),
                    end=lsp.Position(line=i, character=end_col),
                )
                break
    except Exception:
        pass

    return lsp.Location(uri=comp_uri, range=target_range)



# pygls v2 handles workspace/executeCommand natively via @server.command().
# The extension calls:
#   client.sendRequest('workspace/executeCommand',
#                      {command: 'mclsp.getVirtualC', arguments: [uri, text?]})
# The server also proactively pushes virtual C content via
#   server.protocol.notify('$/mclsp/virtualCDocumentContent', {...})
# whenever _update_virtual_c() succeeds.

@server.command('mclsp.getVirtualC')
def cmd_get_virtual_c(uri: str, text: str = None):
    """Return (or build) the virtual C document for the given URI.

    pygls unpacks ``workspace/executeCommand`` ``arguments`` as positional
    args, so the signature must match: ``arguments: [uri]`` or
    ``arguments: [uri, source_text]``.
    """
    if uri is None:
        return None

    # Parse on-demand if the document is not in the cache.
    if uri not in _docs and text is not None:
        _docs[uri] = parse_document(uri, text)

    if _virtual_c.get(uri) is None:
        _update_virtual_c(uri)

    # Always recompute McDoc diagnostics here: the extension's refreshVirtualC
    # fires on every open, so this guarantees diagnostics even if the debounced
    # update fired before the document was fully available.
    _update_mcdoc_diags(uri)
    _update_instr_semantic_diags(uri)
    _update_metadata_diags(uri)
    _publish_diagnostics(uri)

    vdoc = _virtual_c.get(uri)
    if vdoc is None:
        return None

    region_descriptors = [
        {
            'section': r.section,
            'label': r.label,
            'mccodeLine': r.mccode_line,
            'virtualLine': r.virtual_line,
            'contentLines': len(r.content.splitlines()),
        }
        for r in vdoc.regions
    ]

    return {
        'uri': vdoc.source_uri,
        'virtualUri': _virtual_uri(uri),
        'content': vdoc.virtual_source,
        'tempPath': vdoc.temp_path,
        'regions': region_descriptors,
    }


@server.command('mclsp.getMetadataBlocks')
def cmd_get_metadata_blocks(uri: str):
    """Return all METADATA blocks for *uri* with mime type, language ID, and content.

    The VS Code extension uses this to create virtual documents for each block
    so that VS Code's built-in language servers (JSON, YAML, XML, …) provide
    completions, hover, and diagnostics inside METADATA blocks.

    Each entry has:
      ``mime``       – raw MIME type string from the METADATA declaration
      ``languageId`` – VS Code language ID (or null if unknown)
      ``name``       – block name token
      ``content``    – text between ``%{`` and ``%}``
      ``startLine``  – 0-based line of the ``%{`` sentinel
      ``endLine``    – 0-based line of the ``%}`` sentinel
    """
    if uri is None:
        return []
    return _metadata_blocks_info(uri)

