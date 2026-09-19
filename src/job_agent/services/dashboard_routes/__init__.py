from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler
    from typing import Callable

_ROUTES: list[tuple[str, str, Callable]] = []


def route(method: str, pattern: str):
    """Register a handler for *method* and URL *pattern*."""
    def decorator(fn):
        _ROUTES.append((method, pattern, fn))
        return fn
    return decorator


def dispatch(handler: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """Try to dispatch *path* to a registered route. Return True if matched."""
    for route_method, pattern, fn in _ROUTES:
        if route_method == method:
            m = re.fullmatch(pattern, path)
            if m:
                fn(handler, *m.groups())
                return True
    return False
