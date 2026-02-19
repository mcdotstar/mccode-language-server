"""handlers/__init__.py — re-export handler functions for convenience."""
from .diagnostics import get_diagnostics
from .completion import get_completions
from .hover import get_hover

__all__ = ['get_diagnostics', 'get_completions', 'get_hover']
