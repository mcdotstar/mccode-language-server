"""
mclsp Language Server.

Registers LSP capabilities and wires the ANTLR4-backed handlers.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor

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

# Flavor resolver — single instance, shared across all handlers.
_resolver = FlavorResolver()

# Debounce state: pending asyncio tasks for each URI.
_pending_tasks: dict[str, asyncio.Task] = {}

# Thread pool for the slow CTargetVisitor translation (keeps event loop free).
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mclsp-translate')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _publish_diagnostics(uri: str) -> None:
    doc = _docs.get(uri)
    if doc is None:
        return
    diags = get_diagnostics(doc)
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
    except Exception:
        logger.error('_update_virtual_c: build_virtual_c raised:\n%s', traceback.format_exc())
        _virtual_c.pop(uri, None)
        return
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
    _publish_diagnostics(uri)              # fast: ANTLR errors only
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _update_virtual_c, uri)
    _publish_diagnostics(uri)              # slow: ANTLR + clang errors


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

@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: lsp.DidOpenTextDocumentParams):
    td = params.text_document
    uri, source = td.uri, td.text
    _docs[uri] = parse_document(uri, source)
    # Run inference eagerly on open so hover/completion get the right flavor fast
    _resolver.resolve(uri, source)
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
    # Debounce: wait for the user to pause typing before doing heavy work
    _schedule_update(uri, delay=0.5)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: lsp.DidCloseTextDocumentParams):
    uri = params.text_document.uri
    existing = _pending_tasks.pop(uri, None)
    if existing is not None:
        existing.cancel()
    _docs.pop(uri, None)
    vdoc = _virtual_c.pop(uri, None)
    _remove_temp_c(vdoc.temp_path if vdoc else None)
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
# Server commands (workspace/executeCommand)
# ---------------------------------------------------------------------------
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

