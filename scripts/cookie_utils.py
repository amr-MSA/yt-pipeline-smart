"""Utilities for handling browser cookies without persisting them in repository state."""
from contextlib import contextmanager
import os
import tempfile


@contextmanager
def temporary_cookie_file(contents: str | None, directory: str | None = None):
    """Yield a private temporary cookie file path and remove it on exit."""
    if not contents:
        yield None
        return

    fd, path = tempfile.mkstemp(prefix="yt-cookies-", suffix=".txt", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as cookie_file:
            cookie_file.write(contents)
        os.chmod(path, 0o600)
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
