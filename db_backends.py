"""
Database backend abstraction.

Every backend (SQL Server today, Postgres/MySQL later) implements the same
small interface so the GUI never has to know which engine it's talking to:

    connect(**kwargs)              -> establishes the connection
    disconnect()
    test()                         -> quick "are we alive" check
    list_schema()                  -> {schema.table: [col1, col2, ...]}
    execute(sql)                   -> (columns, rows) for the first result set
    label                          -> display name for the UI

Add a new engine by subclassing DbBackend and registering it in BACKENDS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BackendError(Exception):
    """Raised for connection / query errors; message is shown to the user as-is."""


class DbBackend(ABC):
    label = "Unknown"

    def __init__(self):
        self.conn = None

    @property
    def is_connected(self) -> bool:
        return self.conn is not None

    @abstractmethod
    def connect(self, **kwargs):
        ...

    def disconnect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    @abstractmethod
    def list_schema(self) -> dict:
        """Return {'schema.table': ['col1', 'col2', ...]} for every user table."""

    @abstractmethod
    def execute(self, sql: str):
        """Run one statement; return (column_names, rows) or (None, None) for
        statements that don't produce a result set (INSERT/UPDATE/DDL)."""


class SqlServerBackend(DbBackend):
    label = "SQL Server"

    def connect(self, server, database, auth_mode, username=None, password=None,
                encrypt=True, trust_cert=True, port=None, timeout=10):
        import pyodbc

        drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        preferred_order = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server",
        ]
        driver = next((d for d in preferred_order if d in drivers), None)
        if driver is None:
            if drivers:
                driver = drivers[0]
            else:
                raise BackendError(
                    "No SQL Server ODBC driver is installed.\n"
                    "Install 'ODBC Driver 17 for SQL Server' from Microsoft and try again."
                )

        server_part = server if not port else f"{server},{port}"
        parts = [f"DRIVER={{{driver}}}", f"SERVER={server_part}", f"DATABASE={database}"]

        if auth_mode == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            if not username:
                raise BackendError("Username is required for SQL Server authentication.")
            parts.append(f"UID={username}")
            parts.append(f"PWD={password or ''}")

        parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
        parts.append(f"TrustServerCertificate={'yes' if trust_cert else 'no'}")
        parts.append(f"Connection Timeout={timeout}")

        conn_str = ";".join(parts)
        try:
            self.conn = pyodbc.connect(conn_str, timeout=timeout)
            self.conn.autocommit = True
        except pyodbc.Error as exc:
            self.conn = None
            raise BackendError(_friendly_odbc_error(exc)) from exc

    def list_schema(self) -> dict:
        if not self.is_connected:
            raise BackendError("Not connected.")
        query = """
            SELECT
                s.name AS schema_name,
                t.name AS table_name,
                c.name AS column_name,
                c.column_id
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.columns c ON c.object_id = t.object_id
            ORDER BY s.name, t.name, c.column_id
        """
        schema = {}
        cur = self.conn.cursor()
        cur.execute(query)
        for schema_name, table_name, column_name, _ in cur.fetchall():
            key = f"{schema_name}.{table_name}"
            schema.setdefault(key, []).append(column_name)
        return schema

    def execute(self, sql: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
        except Exception as exc:
            raise BackendError(str(exc)) from exc

        if cur.description is None:
            try:
                rowcount = cur.rowcount
            except Exception:
                rowcount = -1
            return None, rowcount

        columns = [col[0] for col in cur.description]
        rows = cur.fetchall()
        return columns, rows


class PostgresBackend(DbBackend):
    label = "PostgreSQL (coming soon)"

    def connect(self, **kwargs):
        raise BackendError("PostgreSQL support isn't wired up yet — coming in a later update.")

    def list_schema(self):
        raise BackendError("PostgreSQL support isn't wired up yet.")

    def execute(self, sql):
        raise BackendError("PostgreSQL support isn't wired up yet.")


class MySqlBackend(DbBackend):
    label = "MySQL / MariaDB (coming soon)"

    def connect(self, **kwargs):
        raise BackendError("MySQL/MariaDB support isn't wired up yet — coming in a later update.")

    def list_schema(self):
        raise BackendError("MySQL/MariaDB support isn't wired up yet.")

    def execute(self, sql):
        raise BackendError("MySQL/MariaDB support isn't wired up yet.")


BACKENDS = {
    "sqlserver": SqlServerBackend,
    "postgres": PostgresBackend,
    "mysql": MySqlBackend,
}


def _friendly_odbc_error(exc) -> str:
    msg = str(exc)
    if "Login failed" in msg:
        return "Login failed. Check username/password (or Windows auth) and try again."
    if "timeout" in msg.lower():
        return "Connection timed out. Check the server name/port and that it's reachable."
    if "does not exist" in msg or "Cannot open database" in msg:
        return "Database not found. Check the database name."
    return msg
