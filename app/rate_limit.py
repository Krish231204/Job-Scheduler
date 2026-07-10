"""Shared slowapi Limiter instance.

Kept in its own module (rather than defined in app/main.py) so router
modules can import it for the `@limiter.limit(...)` decorator without
importing app.main and risking a circular import.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
