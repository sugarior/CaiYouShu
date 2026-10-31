"""SQLite procurement data and offline workflow interfaces for the email agent."""

from .store import Database, DataError

__all__ = ["Database", "DataError"]
