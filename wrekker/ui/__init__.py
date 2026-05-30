__all__ = ["launch", "main"]


def __getattr__(name: str):
    if name in ("launch", "main"):
        from wrekker.ui import app as _app
        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
