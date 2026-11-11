"""
Database layer.

The app stores everything in a database instead of CSV files, because Render's
free plan wipes local files. Two modes, chosen automatically:

  * DATABASE_URL is set (postgres://...)  -> PostgreSQL (use this on Render)
  * DATABASE_URL is not set               -> a local SQLite file, hotel.db

All SQL in app.py uses ? as the placeholder. This file converts it for
PostgreSQL, so the same queries work in both modes.
"""
import os
import sqlite3

from flask import g

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "hotel.db"))

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    DatabaseError = psycopg2.Error
else:
    DatabaseError = sqlite3.Error


def _connect():
    if USE_POSTGRES:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    """One connection per web request (stored in flask.g)."""
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _sql(sql):
    return sql.replace("?", "%s") if USE_POSTGRES else sql


def _cursor(conn):
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def query(sql, params=()):
    """Run a SELECT and return a list of dictionaries."""
    conn = get_db()
    cur = _cursor(conn)
    try:
        cur.execute(_sql(sql), params)
        rows = [dict(r) for r in cur.fetchall()]
        if USE_POSTGRES:
            conn.rollback()  # end the read-only transaction
        return rows
    finally:
        cur.close()


def one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """Run INSERT / UPDATE / DELETE and save the change."""
    conn = get_db()
    cur = _cursor(conn)
    try:
        cur.execute(_sql(sql), params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# ------------------------------------------------------------------
# Tables
# ------------------------------------------------------------------
def _schema():
    pk = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
        """CREATE TABLE IF NOT EXISTS users (
               username TEXT PRIMARY KEY,
               password_hash TEXT NOT NULL,
               role TEXT NOT NULL,
               created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS housekeeping (
               room_no TEXT PRIMARY KEY,
               status TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS bookings (
               booking_id TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               phone TEXT,
               email TEXT,
               id_proof TEXT,
               room_no TEXT NOT NULL,
               room_type TEXT NOT NULL,
               check_in TEXT NOT NULL,
               check_out TEXT NOT NULL,
               nights INTEGER NOT NULL,
               room_charge INTEGER NOT NULL,
               source TEXT,
               status TEXT NOT NULL,
               created_at TEXT,
               checked_in_at TEXT,
               checked_out_at TEXT,
               cancel_reason TEXT,
               refund_amount INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS charges (
               id %s,
               booking_id TEXT NOT NULL,
               kind TEXT NOT NULL,
               description TEXT NOT NULL,
               qty INTEGER NOT NULL,
               unit_price INTEGER NOT NULL,
               amount INTEGER NOT NULL,
               created_at TEXT)""" % pk,
        """CREATE TABLE IF NOT EXISTS payments (
               id %s,
               booking_id TEXT NOT NULL,
               amount INTEGER NOT NULL,
               method TEXT NOT NULL,
               created_at TEXT)""" % pk,
        """CREATE TABLE IF NOT EXISTS messages (
               id %s,
               created_at TEXT,
               channel TEXT,
               booking_id TEXT,
               to_addr TEXT,
               subject TEXT,
               body_text TEXT,
               body_html TEXT,
               mode TEXT)""" % pk,
        """CREATE TABLE IF NOT EXISTS feedback (
               id %s,
               booking_id TEXT NOT NULL UNIQUE,
               rating INTEGER NOT NULL,
               comment TEXT,
               created_at TEXT)""" % pk,
        "CREATE INDEX IF NOT EXISTS idx_bookings_room ON bookings (room_no, status)",
        "CREATE INDEX IF NOT EXISTS idx_charges_booking ON charges (booking_id)",
        "CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments (booking_id)",
    ]


def init_db(room_numbers, default_users):
    """Create the tables (if missing) and add the first accounts and rooms.

    default_users is a list of (username, password_hash, role) tuples that
    are only added when the users table is empty.
    """
    conn = _connect()
    cur = conn.cursor()
    try:
        for statement in _schema():
            cur.execute(statement)
        for room_no in room_numbers:
            cur.execute(_sql("INSERT INTO housekeeping (room_no, status) "
                             "VALUES (?, 'Clean') "
                             "ON CONFLICT (room_no) DO NOTHING"), (room_no,))
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        if count == 0:
            for username, pw_hash, role in default_users:
                cur.execute(_sql("INSERT INTO users (username, password_hash, "
                                 "role, created_at) VALUES (?, ?, ?, ?)"),
                            (username, pw_hash, role, ""))
        conn.commit()
    finally:
        cur.close()
        conn.close()
