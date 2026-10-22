"""Identify transient SQLite contention without hiding other database errors."""
import sqlite3


def is_database_busy(error):
    cause = error.__cause__ or error
    code = getattr(cause, 'sqlite_errorcode', None)
    if isinstance(code, int):
        return (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(cause).lower() in {
        'database is locked', 'database table is locked', 'database schema is locked',
    }
