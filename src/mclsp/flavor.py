"""
Flavor resolution for mclsp.

Determines whether an open document belongs to McStas (Flavor.MCSTAS) or
McXtrace (Flavor.MCXTRACE) using a cascading set of strategies:

1. Explicit workspace configuration supplied by the LSP client via
   ``initializationOptions`` or ``workspace/didChangeConfiguration``.
2. A ``.mclsp.toml`` project config file in the workspace root.
3. Document-level inference: scan COMPONENT instantiation lines and look each
   component type up in both registries — the first type found in *exactly one*
   registry settles the question for the whole document.
4. URI path heuristic (``mcxtrace`` substring → McXtrace).
5. Default: ``Flavor.MCSTAS``.

Resolved flavors are cached per document URI.  Changing the workspace-level
setting invalidates all inferred (non-explicit) entries.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from functools import lru_cache

from mccode_antlr import Flavor

# Matches COMPONENT <instance> = <Type> (ignoring the argument list)
_COMP_INST_RE = re.compile(r'(?i)COMPONENT\s+\w+\s*=\s*(\w+)')


# ---------------------------------------------------------------------------
# Registry helpers (cached, hit once per flavor)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=2)
def _known_components(flavor: Flavor) -> frozenset[str]:
    """Return the set of component stem-names for *flavor* (cached)."""
    try:
        from mccode_antlr.reader import Reader
        reader = Reader(flavor=flavor)
        names: set[str] = set()
        for reg in reader.registries:
            try:
                for fname in reg.filenames():
                    p = PurePosixPath(fname)
                    if p.suffix == '.comp':
                        names.add(p.stem)
            except Exception:
                pass
        return frozenset(names)
    except Exception:
        return frozenset()


# ---------------------------------------------------------------------------
# Project config file
# ---------------------------------------------------------------------------

def _read_project_config(workspace_root: str | None) -> Flavor | None:
    """Parse ``.mclsp.toml`` in *workspace_root* and return the flavor, or None."""
    if not workspace_root:
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # fallback
        except ImportError:
            return None

    config_path = Path(workspace_root) / '.mclsp.toml'
    if not config_path.exists():
        return None

    try:
        data = tomllib.loads(config_path.read_text(encoding='utf-8'))
        raw = data.get('flavor', '')
        return _flavor_from_string(raw)
    except Exception:
        return None


def _flavor_from_string(value: str) -> Flavor | None:
    """Convert a string like ``'mcxtrace'`` or ``'mcstas'`` to a :class:`Flavor`."""
    if not value:
        return None
    name = value.upper().replace('-', '_')
    return Flavor[name] if name in Flavor.__members__ else None


# ---------------------------------------------------------------------------
# URI heuristic
# ---------------------------------------------------------------------------

def _uri_heuristic(uri: str) -> Flavor | None:
    """Return Flavor based on substrings in *uri*, or None if ambiguous."""
    lower = uri.lower()
    if 'mcxtrace' in lower:
        return Flavor.MCXTRACE
    if 'mcstas' in lower:
        return Flavor.MCSTAS
    return None


# ---------------------------------------------------------------------------
# Document inference
# ---------------------------------------------------------------------------

def _infer_from_source(source: str) -> Flavor | None:
    """Scan *source* for COMPONENT lines; return flavor if unambiguously resolved.

    Checks each component type against both registries.  The first type that
    is present in exactly one flavor's registry resolves the document.
    Returns *None* if no unambiguous component is found (e.g. all components
    exist in both registries, or none have been written yet).
    """
    mcstas_names  = _known_components(Flavor.MCSTAS)
    mcxtrace_names = _known_components(Flavor.MCXTRACE)

    for m in _COMP_INST_RE.finditer(source):
        comp_type = m.group(1)
        in_mcstas  = comp_type in mcstas_names
        in_mcxtrace = comp_type in mcxtrace_names

        if in_mcstas and not in_mcxtrace:
            return Flavor.MCSTAS
        if in_mcxtrace and not in_mcstas:
            return Flavor.MCXTRACE
        # Both or neither — keep looking

    return None


# ---------------------------------------------------------------------------
# FlavorResolver
# ---------------------------------------------------------------------------

class FlavorResolver:
    """Resolves and caches the :class:`Flavor` for each open document.

    Thread-safety note: the server's request handlers run in threads.  All
    mutations to ``_by_uri`` and ``_workspace_flavor`` should be treated as
    protected; for this server's single-threaded dispatch model this is fine.
    """

    def __init__(self, workspace_root: str | None = None):
        self._workspace_root = workspace_root
        # Explicit override set by the user/client (highest priority)
        self._workspace_flavor: Flavor | None = None
        # Per-URI cached results (only from inference/heuristic — not explicit)
        self._by_uri: dict[str, Flavor] = {}
        # Whether each cached entry was explicitly set or inferred
        self._explicit: set[str] = set()

    # ------------------------------------------------------------------
    # Configuration entry points
    # ------------------------------------------------------------------

    def set_workspace_flavor(self, flavor: Flavor | None) -> None:
        """Set (or clear) an explicit workspace-level flavor override.

        Clears all inferred (non-explicit) per-document entries so they are
        re-evaluated on next access.
        """
        self._workspace_flavor = flavor
        inferred = [uri for uri in self._by_uri if uri not in self._explicit]
        for uri in inferred:
            del self._by_uri[uri]

    def set_document_flavor(self, uri: str, flavor: Flavor) -> None:
        """Explicitly pin the flavor for a single document."""
        self._by_uri[uri] = flavor
        self._explicit.add(uri)

    def forget(self, uri: str) -> None:
        """Remove a document from the cache (called on ``textDocument/didClose``)."""
        self._by_uri.pop(uri, None)
        self._explicit.discard(uri)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, uri: str, source: str | None = None) -> Flavor:
        """Return the best :class:`Flavor` for *uri*, updating the cache.

        If *source* is provided and no cached/explicit result exists yet,
        component-based inference is attempted.
        """
        # 1. Explicit workspace config (user override — always wins)
        if self._workspace_flavor is not None:
            return self._workspace_flavor

        # 2. Already resolved explicitly for this document
        if uri in self._explicit:
            return self._by_uri[uri]

        # 3. Project config file (checked lazily once)
        project_flavor = _read_project_config(self._workspace_root)
        if project_flavor is not None:
            return project_flavor

        # 4. Component-based inference from document source
        if source is not None:
            inferred = _infer_from_source(source)
            if inferred is not None:
                self._by_uri[uri] = inferred
                return inferred

        # 5. Return already-cached inferred result (from a previous call)
        if uri in self._by_uri:
            return self._by_uri[uri]

        # 6. URI heuristic
        heuristic = _uri_heuristic(uri)
        if heuristic is not None:
            self._by_uri[uri] = heuristic
            return heuristic

        # 7. Default
        return Flavor.MCSTAS

    def re_infer(self, uri: str, source: str) -> Flavor:
        """Re-run inference for *uri* (called on document change).

        Respects explicit overrides but refreshes any previously inferred
        result so that adding a new COMPONENT line can settle the flavor.
        """
        if uri in self._explicit or self._workspace_flavor is not None:
            return self.resolve(uri, source)
        # Drop cached inferred result and re-resolve
        self._by_uri.pop(uri, None)
        return self.resolve(uri, source)
