"""
mclsp Language Server.

Registers LSP capabilities and wires the ANTLR4-backed handlers.
"""
from __future__ import annotations

from pygls.lsp.server import LanguageServer
from lsprotocol import types as lsp

from mclsp import __version__
from mclsp.document import parse_document, ParsedDocument
from mclsp.flavor import FlavorResolver, _flavor_from_string
from mclsp.handlers import get_diagnostics, get_completions, get_hover
from mclsp.c_bridge import build_virtual_c, VirtualCDocument

# ---------------------------------------------------------------------------
# Server instance + per-session state
# ---------------------------------------------------------------------------

server = LanguageServer('mclsp', __version__)

# Per-URI document store (populated on open/change).
_docs: dict[str, ParsedDocument] = {}

# Virtual C document cache (one per McCode document).
_virtual_c: dict[str, VirtualCDocument] = {}

# Flavor resolver — single instance, shared across all handlers.
_resolver = FlavorResolver()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _publish_diagnostics(uri: str) -> None:
    doc = _docs.get(uri)
    if doc is None:
        return
    diags = get_diagnostics(doc)
    server.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
    )


def _update_virtual_c(uri: str) -> None:
    """(Re)build the virtual C document for *uri* and cache it."""
    doc = _docs.get(uri)
    if doc is None:
        _virtual_c.pop(uri, None)
        return
    flavor = _resolver.resolve(uri, doc.source)
    flavor_str = flavor.name.lower() if hasattr(flavor, 'name') else str(flavor).lower()
    vdoc = build_virtual_c(doc, flavor=flavor_str)
    if vdoc is not None:
        _virtual_c[uri] = vdoc
    else:
        _virtual_c.pop(uri, None)


def _flavor_from_init_options(options) -> str | None:
    """Extract the ``flavor`` key from ``initializationOptions`` if present."""
    if options is None:
        return None
    if isinstance(options, dict):
        return options.get('flavor')
    # Some clients send a typed object; try attribute access
    return getattr(options, 'flavor', None)


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

    # Honor an explicit flavor in initializationOptions
    raw = _flavor_from_init_options(
        getattr(params, 'initialization_options', None)
    )
    if raw:
        flavor = _flavor_from_string(raw)
        if flavor is not None:
            _resolver.set_workspace_flavor(flavor)


@server.feature(lsp.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(params: lsp.DidChangeConfigurationParams):
    """Handle live config changes (e.g. user changes ``mccode.flavor`` in VS Code)."""
    settings = getattr(params, 'settings', None) or {}
    if isinstance(settings, dict):
        raw = settings.get('mccode', {}).get('flavor', None)
        if raw is not None:
            flavor = _flavor_from_string(raw)
            _resolver.set_workspace_flavor(flavor)  # None clears the override


# ---------------------------------------------------------------------------
# Text document synchronisation
# ---------------------------------------------------------------------------

@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: lsp.DidOpenTextDocumentParams):
    td = params.text_document
    uri, source = td.uri, td.text
    _docs[uri] = parse_document(uri, source)
    # Run inference eagerly on open so hover/completion get the right flavor fast
    _resolver.resolve(uri, source)
    _update_virtual_c(uri)
    _publish_diagnostics(uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(params: lsp.DidChangeTextDocumentParams):
    uri = params.text_document.uri
    source = params.content_changes[-1].text
    _docs[uri] = parse_document(uri, source)
    # Re-infer flavor: a new COMPONENT line may settle a previously ambiguous doc
    _resolver.re_infer(uri, source)
    _update_virtual_c(uri)
    _publish_diagnostics(uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: lsp.DidCloseTextDocumentParams):
    uri = params.text_document.uri
    _docs.pop(uri, None)
    _virtual_c.pop(uri, None)
    _resolver.forget(uri)


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
    return get_hover(doc, params.position, flavor=flavor)


# ---------------------------------------------------------------------------
# Custom request: $/mclsp/virtualCDocument
# ---------------------------------------------------------------------------
# The VS Code extension (or any client) can call this to obtain the stitched
# virtual C source for a given McCode document.  The response contains the
# full text plus a position map so the extension can open it and correlate
# positions with the original file.

MCLSP_VIRTUAL_C_REQUEST = '$/mclsp/virtualCDocument'


@server.feature(MCLSP_VIRTUAL_C_REQUEST)
def virtual_c_document(params):
    """Return the virtual C document for a given McCode URI.

    Expected params: ``{"uri": "file:///path/to/foo.instr"}``

    Response (dict):
    - ``"uri"``         – the original McCode URI
    - ``"virtualUri"``  – suggested URI for the virtual C document
                          (``"mccode-c://..."`` scheme)
    - ``"content"``     – full virtual C source text
    - ``"regions"``     – list of region descriptors for position mapping
    """
    uri = params.get('uri') if isinstance(params, dict) else getattr(params, 'uri', None)
    if uri is None:
        return None

    vdoc = _virtual_c.get(uri)
    if vdoc is None:
        # Try to build on demand if the document was open before the handler existed.
        doc = _docs.get(uri)
        if doc is None:
            return None
        _update_virtual_c(uri)
        vdoc = _virtual_c.get(uri)
        if vdoc is None:
            return None

    # Build a mccode-c:// URI by replacing the scheme and appending .c
    from pathlib import PurePosixPath
    path = uri.replace('file://', '', 1)
    virtual_uri = f'mccode-c://{path}.c'

    region_descriptors = [
        {
            'section': r.section,
            'label': r.label,
            'mccodeTokenLine': r.mccode_token_line,
            'mccodeLine': r.mccode_line,
            'virtualLine': r.virtual_line,
            'contentLines': len(r.content.splitlines()),
        }
        for r in vdoc.regions
    ]

    return {
        'uri': vdoc.source_uri,
        'virtualUri': virtual_uri,
        'content': vdoc.virtual_source,
        'regions': region_descriptors,
    }


# ---------------------------------------------------------------------------
# Custom notification: $/mclsp/positionInCRegion
# ---------------------------------------------------------------------------
# Given a McCode (uri, line, col) the server responds with the corresponding
# virtual-C (line, col) so the extension can redirect to clangd.

MCLSP_POSITION_IN_C = '$/mclsp/positionInCRegion'


@server.feature(MCLSP_POSITION_IN_C)
def position_in_c_region(params):
    """Map a McCode cursor position to the virtual C document.

    Expected params::

        {"uri": "...", "line": <1-based>, "col": <0-based>}

    Returns::

        {"inCRegion": true, "virtualUri": "...", "virtualLine": N, "virtualCol": N}
        {"inCRegion": false}
    """
    if isinstance(params, dict):
        uri = params.get('uri')
        line = params.get('line', 0)
        col = params.get('col', 0)
    else:
        uri = getattr(params, 'uri', None)
        line = getattr(params, 'line', 0)
        col = getattr(params, 'col', 0)

    if uri is None:
        return {'inCRegion': False}

    vdoc = _virtual_c.get(uri)
    if vdoc is None:
        return {'inCRegion': False}

    result = vdoc.mccode_to_virtual(line, col)
    if result is None:
        return {'inCRegion': False}

    vline, vcol = result
    path = uri.replace('file://', '', 1)
    virtual_uri = f'mccode-c://{path}.c'
    return {
        'inCRegion': True,
        'virtualUri': virtual_uri,
        'virtualLine': vline,
        'virtualCol': vcol,
    }

