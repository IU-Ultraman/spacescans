"""Errors raised by the upload path. Both messages are written for the end user."""
from __future__ import annotations


class CustomExposomeError(ValueError):
    """An upload or a dataset definition was rejected.

    The message is shown to the user as is (the web returns it as the 400
    detail; the bridge returns it as a ``config`` error).
    """


class RasterError(CustomExposomeError):
    """A GeoTIFF cannot serve as a C3 template grid, or cannot be read back.

    A subclass of :class:`CustomExposomeError`, so one ``except`` covers the
    whole upload path. The web kept the two apart (both were plain
    ``ValueError`` subclasses); code that catches either still works.
    """
