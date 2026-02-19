"""mclsp – McCode DSL Language Server."""
try:
    from importlib.metadata import version, PackageNotFoundError
    try:
        __version__ = version('mclsp')
    except PackageNotFoundError:
        __version__ = '0.0.0.dev0'
except ImportError:
    __version__ = '0.0.0.dev0'
