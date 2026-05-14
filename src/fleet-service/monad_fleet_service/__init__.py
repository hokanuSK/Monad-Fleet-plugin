__all__ = ["serve"]


def serve() -> None:
    from .main import serve as _serve

    _serve()
