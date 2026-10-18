# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Dror Maor
"""
Database backend abstraction.

Every backend (SQL Server, PostgreSQL, MySQL/MariaDB, Oracle, SQLite)
implements the same small interface so the GUI never has to know which
engine it's talking to:

    connect(**kwargs)              -> establishes the connection
    disconnect()
    test()                         -> quick "are we alive" check
    list_schema()                  -> {schema.table: [col1, col2, ...]}
    execute(sql)                   -> (columns, rows) for the first result set
    label                          -> display name for the UI

Add a new engine by subclassing DbBackend and registering it in BACKENDS.
"""

from __future__ import annotations

import os
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
    def list_databases(self) -> list:
        """Return the names of every database on the server."""

    @abstractmethod
    def use_database(self, name: str):
        """Switch the connection's active database to `name`."""

    @abstractmethod
    def list_schema(self, database: str = None) -> dict:
        """Return {'schema.table': ['col1', 'col2', ...]} for every user table
        in `database` (or the active database if not given)."""

    @abstractmethod
    def execute(self, sql: str):
        """Run one statement; return (column_names, rows) or (None, None) for
        statements that don't produce a result set (INSERT/UPDATE/DDL)."""

    def cancel(self) -> bool:
        """Interrupt a query that's currently blocking inside execute() on
        another thread. Returns whether the cancel request was sent
        successfully (not whether the query actually stopped in time)."""
        return False

    def select_top_sql(self, table_key: str, limit: int = 1000) -> str:
        """Return a quick 'peek at this table' query. `table_key` is whatever
        list_schema() used as a key (bare table name, or 'schema.table')."""
        return f'SELECT * FROM {_quote_dotted(table_key)} LIMIT {limit}'

    def explain_statement(self, stmt: str) -> str:
        """Return SQL that, when run through execute(), shows this engine's
        query plan for `stmt` instead of actually running it."""
        return f"EXPLAIN {stmt}"


def _quote_ident(name: str) -> str:
    return f'"{name}"'


def _quote_dotted(table_key: str) -> str:
    if "." in table_key:
        schema, table = table_key.split(".", 1)
        return f"{_quote_ident(schema)}.{_quote_ident(table)}"
    return _quote_ident(table_key)


class SqlServerBackend(DbBackend):
    label = "SQL Server"

    def __init__(self):
        super().__init__()
        self.current_database = None
        self._active_cursor = None

    def connect(self, server, database=None, auth_mode="windows", username=None, password=None,
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
        parts = [f"DRIVER={{{driver}}}", f"SERVER={server_part}"]
        if database:
            parts.append(f"DATABASE={database}")

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
        self.current_database = database or None

    def list_databases(self) -> list:
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM sys.databases ORDER BY name")
        return [row[0] for row in cur.fetchall()]

    def use_database(self, name: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        try:
            cur.execute(f"USE [{_escape_bracket(name)}]")
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        self.current_database = name

    def list_schema(self, database: str = None) -> dict:
        """Return {'schema.table': [(col_name, type_str), ...]}."""
        if not self.is_connected:
            raise BackendError("Not connected.")
        db = database or self.current_database
        if not db:
            raise BackendError("No database selected.")
        prefix = f"[{_escape_bracket(db)}]."
        query = f"""
            SELECT
                s.name AS schema_name,
                t.name AS table_name,
                c.name AS column_name,
                c.column_id,
                ty.name AS type_name,
                c.max_length,
                c.precision,
                c.scale
            FROM {prefix}sys.tables t
            JOIN {prefix}sys.schemas s ON s.schema_id = t.schema_id
            JOIN {prefix}sys.columns c ON c.object_id = t.object_id
            JOIN {prefix}sys.types ty ON ty.user_type_id = c.user_type_id
            ORDER BY s.name, t.name, c.column_id
        """
        schema = {}
        cur = self.conn.cursor()
        cur.execute(query)
        for schema_name, table_name, column_name, _, type_name, max_length, precision, scale in cur.fetchall():
            key = f"{schema_name}.{table_name}"
            type_str = _format_sql_type(type_name, max_length, precision, scale)
            schema.setdefault(key, []).append((column_name, type_str))
        return schema

    def execute(self, sql: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        self._active_cursor = cur
        try:
            try:
                cur.execute(sql)
            except Exception as exc:
                raise BackendError(str(exc)) from exc

            # A batch can contain several statements (e.g. "USE db" followed by a
            # SELECT); each produces its own result set, so keep advancing past
            # the ones with no rows (USE, DDL, ...) until we find the last one
            # that returned data, or run out.
            columns, rows, rowcount = None, None, -1
            has_result = True
            while has_result:
                if cur.description is not None:
                    columns = [col[0] for col in cur.description]
                    rows = cur.fetchall()
                else:
                    try:
                        rowcount = cur.rowcount
                    except Exception:
                        pass
                try:
                    has_result = cur.nextset()
                except Exception:
                    has_result = False

            if columns is None:
                return None, rowcount
            return columns, rows
        finally:
            self._active_cursor = None

    def cancel(self) -> bool:
        cur = getattr(self, "_active_cursor", None)
        if cur is None:
            return False
        try:
            cur.cancel()
            return True
        except Exception:
            return False

    def select_top_sql(self, table_key: str, limit: int = 1000) -> str:
        if "." in table_key:
            schema, table = table_key.split(".", 1)
            name = f"[{_escape_bracket(schema)}].[{_escape_bracket(table)}]"
        else:
            name = f"[{_escape_bracket(table_key)}]"
        return f"SELECT TOP {limit} * FROM {name}"

    def explain_statement(self, stmt: str) -> str:
        return f"SET SHOWPLAN_ALL ON;\n{stmt};\nSET SHOWPLAN_ALL OFF;"


class PostgresBackend(DbBackend):
    """Needs psycopg2 (`pip install psycopg2-binary`).

    Unlike SQL Server, a Postgres connection is bound to one database for
    its whole life — there's no "USE" equivalent. Switching the active
    database, or browsing a different one's schema, opens a fresh
    connection with the same host/credentials rather than reusing `conn`.
    """

    label = "PostgreSQL"

    def __init__(self):
        super().__init__()
        self.current_database = None
        self._conn_kwargs = None

    def connect(self, server, database=None, auth_mode="windows", username=None, password=None,
                encrypt=True, trust_cert=True, port=None, timeout=10):
        import psycopg2

        if encrypt:
            sslmode = "require" if trust_cert else "verify-full"
        else:
            sslmode = "disable"

        kwargs = dict(
            host=server,
            dbname=database or "postgres",
            connect_timeout=timeout,
            sslmode=sslmode,
        )
        if port:
            kwargs["port"] = port
        if auth_mode == "sql":
            if not username:
                raise BackendError("Username is required for SQL login authentication.")
            kwargs["user"] = username
            kwargs["password"] = password or ""
        elif username:
            kwargs["user"] = username

        try:
            conn = psycopg2.connect(**kwargs)
            conn.autocommit = True
        except psycopg2.Error as exc:
            raise BackendError(_friendly_pg_error(exc)) from exc

        self.conn = conn
        self._conn_kwargs = kwargs
        self.current_database = kwargs["dbname"] if database else None

    def list_databases(self) -> list:
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
        return [row[0] for row in cur.fetchall()]

    def use_database(self, name: str):
        import psycopg2

        if not self.is_connected:
            raise BackendError("Not connected.")
        kwargs = dict(self._conn_kwargs)
        kwargs["dbname"] = name
        try:
            new_conn = psycopg2.connect(**kwargs)
            new_conn.autocommit = True
        except psycopg2.Error as exc:
            raise BackendError(_friendly_pg_error(exc)) from exc
        old_conn = self.conn
        self.conn = new_conn
        self._conn_kwargs = kwargs
        self.current_database = name
        try:
            old_conn.close()
        except Exception:
            pass

    def list_schema(self, database: str = None) -> dict:
        """Return {'schema.table': [(col_name, type_str), ...]}."""
        if not self.is_connected:
            raise BackendError("Not connected.")
        db = database or self.current_database
        if not db:
            raise BackendError("No database selected.")

        query = """
            SELECT table_schema, table_name, column_name, ordinal_position,
                   data_type, character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name, ordinal_position
        """

        if db == self.current_database:
            cur = self.conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()
        else:
            import psycopg2

            kwargs = dict(self._conn_kwargs)
            kwargs["dbname"] = db
            try:
                temp_conn = psycopg2.connect(**kwargs)
            except psycopg2.Error as exc:
                raise BackendError(_friendly_pg_error(exc)) from exc
            try:
                cur = temp_conn.cursor()
                cur.execute(query)
                rows = cur.fetchall()
            finally:
                temp_conn.close()

        schema = {}
        for schema_name, table_name, column_name, _, data_type, char_len, num_prec, num_scale in rows:
            key = f"{schema_name}.{table_name}"
            type_str = _format_pg_type(data_type, char_len, num_prec, num_scale)
            schema.setdefault(key, []).append((column_name, type_str))
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

    def cancel(self) -> bool:
        if self.conn is None:
            return False
        try:
            self.conn.cancel()
            return True
        except Exception:
            return False


class MySqlBackend(DbBackend):
    """Needs mysql-connector-python (`pip install mysql-connector-python`).
    Works for MariaDB too, which speaks the same wire protocol."""

    label = "MySQL / MariaDB"

    def __init__(self):
        super().__init__()
        self.current_database = None
        self._conn_kwargs = None

    def connect(self, server, database=None, auth_mode="windows", username=None, password=None,
                encrypt=True, trust_cert=True, port=None, timeout=10):
        import mysql.connector

        kwargs = dict(
            host=server,
            connection_timeout=timeout,
            ssl_disabled=not encrypt,
        )
        if port:
            kwargs["port"] = port
        if database:
            kwargs["database"] = database
        if encrypt and not trust_cert:
            kwargs["ssl_verify_cert"] = True
        if auth_mode == "sql":
            if not username:
                raise BackendError("Username is required for SQL login authentication.")
            kwargs["user"] = username
            kwargs["password"] = password or ""
        elif username:
            kwargs["user"] = username

        try:
            self.conn = mysql.connector.connect(**kwargs)
        except mysql.connector.Error as exc:
            self.conn = None
            raise BackendError(_friendly_mysql_error(exc)) from exc
        self.current_database = database or None
        self._conn_kwargs = kwargs

    def list_databases(self) -> list:
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        cur.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
        return [row[0] for row in cur.fetchall()]

    def use_database(self, name: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        try:
            self.conn.database = name
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        self.current_database = name

    def list_schema(self, database: str = None) -> dict:
        """Return {table_name: [(col_name, type_str), ...]}. MySQL has no
        separate schema layer above the database, so keys are bare table
        names rather than 'schema.table'."""
        if not self.is_connected:
            raise BackendError("Not connected.")
        db = database or self.current_database
        if not db:
            raise BackendError("No database selected.")
        query = """
            SELECT table_name, column_name, ordinal_position,
                   data_type, character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
        """
        schema = {}
        cur = self.conn.cursor()
        cur.execute(query, (db,))
        for table_name, column_name, _, data_type, char_len, num_prec, num_scale in cur.fetchall():
            type_str = _format_mysql_type(data_type, char_len, num_prec, num_scale)
            schema.setdefault(table_name, []).append((column_name, type_str))
        return schema

    def execute(self, sql: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        cur = self.conn.cursor()
        try:
            # multi=True returns a generator of one cursor-state per
            # statement; it must be exhausted to actually run them all
            # (e.g. "USE db; SELECT ..." — two statements in one batch).
            results = cur.execute(sql, multi=True)
            columns, rows, rowcount = None, None, -1
            for _ in results:
                if cur.description is not None:
                    columns = [col[0] for col in cur.description]
                    rows = cur.fetchall()
                else:
                    try:
                        rowcount = cur.rowcount
                    except Exception:
                        pass
        except Exception as exc:
            raise BackendError(str(exc)) from exc

        if columns is None:
            return None, rowcount
        return columns, rows

    def cancel(self) -> bool:
        """mysql-connector has no in-band cancel, so interrupt the running
        query the same way the `mysql` CLI does: open a throwaway connection
        with the same credentials and issue KILL QUERY against this
        connection's id."""
        if self.conn is None or self._conn_kwargs is None:
            return False
        try:
            conn_id = self.conn.connection_id
        except Exception:
            return False

        import mysql.connector

        try:
            killer = mysql.connector.connect(**self._conn_kwargs)
            try:
                killer.cursor().execute(f"KILL QUERY {int(conn_id)}")
            finally:
                killer.close()
            return True
        except Exception:
            return False

    def select_top_sql(self, table_key: str, limit: int = 1000) -> str:
        return f"SELECT * FROM `{table_key}` LIMIT {limit}"


class OracleBackend(DbBackend):
    """Needs python-oracledb (`pip install oracledb`). Runs in "thin" mode by
    default, i.e. no Oracle Instant Client install required.

    Oracle has no SQL-Server-style "list every database, USE one" model — a
    connection is bound to one service/pluggable database for its life, same
    as Postgres. `database` here means the service name (or SID). Switching
    it, like Postgres, opens a fresh connection rather than reusing `conn`.
    `list_schema` spans every user schema visible to the connected account
    (via ALL_TAB_COLUMNS), keyed 'owner.table', skipping Oracle-maintained
    schemas (SYS, SYSTEM, ...).
    """

    label = "Oracle"

    _SYSTEM_SCHEMAS = {
        "SYS", "SYSTEM", "OUTLN", "XDB", "ORDSYS", "ORDDATA", "MDSYS", "CTXSYS",
        "DBSNMP", "APPQOSSYS", "GSMADMIN_INTERNAL", "ORACLE_OCM", "DVSYS",
        "DVF", "LBACSYS", "AUDSYS", "OJVMSYS", "WMSYS", "ANONYMOUS",
        "FLOWS_FILES", "APEX_PUBLIC_USER", "GGSYS", "REMOTE_SCHEDULER_AGENT",
        "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "DIP", "XS$NULL",
    }

    def __init__(self):
        super().__init__()
        self.current_database = None
        self._conn_kwargs = None

    def connect(self, server, database=None, auth_mode="windows", username=None, password=None,
                encrypt=True, trust_cert=True, port=None, timeout=10):
        import oracledb

        if not database:
            raise BackendError("Service name (database) is required for Oracle.")
        if not username:
            raise BackendError("Username is required for Oracle.")

        kwargs = dict(
            host=server,
            port=port or 1521,
            service_name=database,
            user=username,
            password=password or "",
            protocol="tcps" if encrypt else "tcp",
            tcp_connect_timeout=timeout,
        )
        if encrypt and trust_cert:
            kwargs["ssl_server_dn_match"] = False

        try:
            conn = oracledb.connect(**kwargs)
            conn.autocommit = True
        except oracledb.Error as exc:
            raise BackendError(_friendly_oracle_error(exc)) from exc

        self.conn = conn
        self._conn_kwargs = kwargs
        self.current_database = database

    def list_databases(self) -> list:
        if not self.is_connected:
            raise BackendError("Not connected.")
        return [self.current_database]

    def use_database(self, name: str):
        import oracledb

        if not self.is_connected:
            raise BackendError("Not connected.")
        if name == self.current_database:
            return
        kwargs = dict(self._conn_kwargs)
        kwargs["service_name"] = name
        try:
            new_conn = oracledb.connect(**kwargs)
            new_conn.autocommit = True
        except oracledb.Error as exc:
            raise BackendError(_friendly_oracle_error(exc)) from exc
        old_conn = self.conn
        self.conn = new_conn
        self._conn_kwargs = kwargs
        self.current_database = name
        try:
            old_conn.close()
        except Exception:
            pass

    def list_schema(self, database: str = None) -> dict:
        """Return {'owner.table': [(col_name, type_str), ...]}."""
        if not self.is_connected:
            raise BackendError("Not connected.")
        db = database or self.current_database
        if not db:
            raise BackendError("No database selected.")
        if db != self.current_database:
            # Oracle has no in-session way to query another service; the
            # caller would need to connect/use_database into it first.
            raise BackendError(f"Not connected to '{db}'.")

        query = """
            SELECT owner, table_name, column_name, column_id,
                   data_type, data_length, data_precision, data_scale
            FROM all_tab_columns
            WHERE owner NOT IN ({placeholders})
            ORDER BY owner, table_name, column_id
        """.format(placeholders=", ".join(f":{i+1}" for i in range(len(self._SYSTEM_SCHEMAS))))

        schema = {}
        cur = self.conn.cursor()
        cur.execute(query, list(self._SYSTEM_SCHEMAS))
        for owner, table_name, column_name, _, data_type, length, precision, scale in cur.fetchall():
            key = f"{owner}.{table_name}"
            type_str = _format_oracle_type(data_type, length, precision, scale)
            schema.setdefault(key, []).append((column_name, type_str))
        return schema

    def execute(self, sql: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        # A trailing ";" is SQL*Plus/IDE sugar, not valid in a single
        # statement sent over OCI — strip it or Oracle raises ORA-00911.
        sql = sql.strip()
        if sql.endswith(";"):
            sql = sql[:-1]

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

    def cancel(self) -> bool:
        if self.conn is None:
            return False
        try:
            self.conn.cancel()
            return True
        except Exception:
            return False

    def select_top_sql(self, table_key: str, limit: int = 1000) -> str:
        return f"SELECT * FROM {_quote_dotted(table_key)} FETCH FIRST {limit} ROWS ONLY"

    def explain_statement(self, stmt: str) -> str:
        return f"EXPLAIN PLAN FOR {stmt};\nSELECT * FROM TABLE(DBMS_XPLAN.DISPLAY());"


class SqliteBackend(DbBackend):
    """Uses the stdlib `sqlite3` module — no driver install needed.

    SQLite is a local file, not a client/server engine: there's no host,
    port, or login, and the "database" is simply the file on disk (passed
    in as `server`, since the GUI's connect dialog repurposes that field as
    the file path). Like MySQL, there's no schema layer above the database,
    so `list_schema` keys are bare table names. Only one file is ever open
    at a time — `list_databases` reports just that one.
    """

    label = "SQLite"

    def __init__(self):
        super().__init__()
        self.current_database = None
        self._path = None

    def connect(self, server, database=None, auth_mode="windows", username=None, password=None,
                encrypt=True, trust_cert=True, port=None, timeout=10):
        import sqlite3

        if not server:
            raise BackendError("File path is required for SQLite.")
        try:
            # check_same_thread=False: every async operation in the GUI runs
            # on its own worker thread, so the connection outlives any one
            # of them. isolation_level=None turns off sqlite3's implicit
            # transactions so each statement commits immediately, matching
            # the autocommit behavior of the other backends.
            conn = sqlite3.connect(server, timeout=timeout, check_same_thread=False)
            conn.isolation_level = None
        except sqlite3.Error as exc:
            raise BackendError(str(exc)) from exc

        self.conn = conn
        self._path = server
        self.current_database = database or os.path.basename(server)

    def list_databases(self) -> list:
        if not self.is_connected:
            raise BackendError("Not connected.")
        return [self.current_database]

    def use_database(self, name: str):
        if not self.is_connected:
            raise BackendError("Not connected.")
        if name != self.current_database:
            raise BackendError("A SQLite connection is bound to a single file; reconnect to open another one.")

    def list_schema(self, database: str = None) -> dict:
        """Return {table_name: [(col_name, type_str), ...]}."""
        if not self.is_connected:
            raise BackendError("Not connected.")
        db = database or self.current_database
        if not db:
            raise BackendError("No database selected.")
        if db != self.current_database:
            raise BackendError(f"Not connected to '{db}'.")

        cur = self.conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        table_names = [row[0] for row in cur.fetchall()]

        schema = {}
        for table_name in table_names:
            cur.execute(f'PRAGMA table_info("{table_name}")')
            cols = [(col_name, (col_type or "").lower()) for _, col_name, col_type, *_rest in cur.fetchall()]
            schema[table_name] = cols
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

    def cancel(self) -> bool:
        if self.conn is None:
            return False
        try:
            self.conn.interrupt()
            return True
        except Exception:
            return False

    def explain_statement(self, stmt: str) -> str:
        return f"EXPLAIN QUERY PLAN {stmt}"


BACKENDS = {
    "sqlserver": SqlServerBackend,
    "postgres": PostgresBackend,
    "mysql": MySqlBackend,
    "oracle": OracleBackend,
    "sqlite": SqliteBackend,
}


def _escape_bracket(name: str) -> str:
    return name.replace("]", "]]")


_SIZED_TYPES = {"char", "varchar", "binary", "varbinary"}
_NCHAR_TYPES = {"nchar", "nvarchar"}
_PRECISION_SCALE_TYPES = {"decimal", "numeric"}
_SCALE_ONLY_TYPES = {"datetime2", "time", "datetimeoffset"}


def _format_sql_type(type_name, max_length, precision, scale) -> str:
    name = type_name.lower()
    if name in _SIZED_TYPES:
        return f"{name}({'max' if max_length == -1 else max_length})"
    if name in _NCHAR_TYPES:
        return f"{name}({'max' if max_length == -1 else max_length // 2})"
    if name in _PRECISION_SCALE_TYPES:
        return f"{name}({precision},{scale})"
    if name == "float" and precision not in (53, None):
        return f"float({precision})"
    if name in _SCALE_ONLY_TYPES and scale is not None:
        return f"{name}({scale})"
    return name


def _friendly_odbc_error(exc) -> str:
    msg = str(exc)
    if "Login failed" in msg:
        return "Login failed. Check username/password (or Windows auth) and try again."
    if "timeout" in msg.lower():
        return "Connection timed out. Check the server name/port and that it's reachable."
    if "does not exist" in msg or "Cannot open database" in msg:
        return "Database not found. Check the database name."
    return msg


_PG_SIZED_TYPES = {"character varying": "varchar", "character": "char"}
_PG_PRECISION_SCALE_TYPES = {"numeric", "decimal"}


def _format_pg_type(data_type, char_len, num_precision, num_scale) -> str:
    short = _PG_SIZED_TYPES.get(data_type)
    if short is not None:
        return f"{short}({char_len})" if char_len is not None else short
    if data_type in _PG_PRECISION_SCALE_TYPES:
        if num_precision is not None and num_scale is not None:
            return f"{data_type}({num_precision},{num_scale})"
        return data_type
    return data_type


def _friendly_pg_error(exc) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "password authentication failed" in low or "authentication failed" in low:
        return "Login failed. Check username/password and try again."
    if "timeout" in low:
        return "Connection timed out. Check the server name/port and that it's reachable."
    if "does not exist" in low and "database" in low:
        return "Database not found. Check the database name."
    return msg


_MYSQL_SIZED_TYPES = {"varchar", "char", "binary", "varbinary"}
_MYSQL_PRECISION_SCALE_TYPES = {"decimal", "numeric"}


def _format_mysql_type(data_type, char_len, num_precision, num_scale) -> str:
    name = data_type.lower()
    if name in _MYSQL_SIZED_TYPES:
        return f"{name}({char_len})" if char_len is not None else name
    if name in _MYSQL_PRECISION_SCALE_TYPES:
        if num_precision is not None and num_scale is not None:
            return f"{name}({num_precision},{num_scale})"
        return name
    return name


def _friendly_mysql_error(exc) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "access denied" in low:
        return "Login failed. Check username/password and try again."
    if "timeout" in low or "timed out" in low:
        return "Connection timed out. Check the server name/port and that it's reachable."
    if "unknown database" in low:
        return "Database not found. Check the database name."
    return msg


_ORACLE_SIZED_TYPES = {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}
_ORACLE_PRECISION_SCALE_TYPES = {"NUMBER"}


def _format_oracle_type(data_type, length, precision, scale) -> str:
    name = (data_type or "").lower()
    upper = (data_type or "").upper()
    if upper in _ORACLE_SIZED_TYPES:
        return f"{name}({length})" if length is not None else name
    if upper in _ORACLE_PRECISION_SCALE_TYPES:
        if precision is not None and scale is not None:
            return f"{name}({precision},{scale})" if scale else f"{name}({precision})"
        return name
    return name


def _friendly_oracle_error(exc) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "ora-01017" in low:
        return "Login failed. Check username/password and try again."
    if "ora-12170" in low or "ora-12535" in low or "timeout" in low:
        return "Connection timed out. Check the server name/port and that it's reachable."
    if "ora-12514" in low or "ora-12505" in low:
        return "Service name not found. Check the database (service name) and try again."
    if "ora-12541" in low:
        return "No listener found. Check the server name/port and that the Oracle listener is running."
    return msg
