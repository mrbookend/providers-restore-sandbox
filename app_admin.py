# app_admin.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import hmac
import time
import uuid
import sys
from datetime import datetime
from typing import Any

import pandas as pd

# ---- Page config MUST be the first Streamlit command ----
import streamlit as st
st.set_page_config(
    page_title="HCR Providers — Admin",
    page_icon="🛠️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- SQLAlchemy + libsql imports (safe after page_config) ----
import sqlalchemy as sa
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.engine import Engine

# Register libsql dialect if available (non-fatal if missing for non-turso)
try:
    import sqlalchemy_libsql as sa_libsql  # type: ignore
except Exception:
    sa_libsql = None

# Resolve sqlalchemy-libsql version (more reliable than module attr)
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
except Exception:  # very old Pythons only; 3.11 has it
    _pkg_version = None
    class PackageNotFoundError(Exception): ...
try:
    sa_libsql_ver = _pkg_version("sqlalchemy-libsql") if _pkg_version else (
        getattr(sa_libsql, "__version__", "not-installed") if sa_libsql else "not-installed"
    )
except PackageNotFoundError:
    sa_libsql_ver = "not-installed"

# ---- Optional: dependency banner (OK after page_config) ----
st.caption(
    "Deps — "
    f"py: {sys.version.split()[0]} | "
    f"streamlit: {st.__version__} | "
    f"sqlalchemy: {sa.__version__} | "
    f"sqlalchemy-libsql: {sa_libsql_ver}"
)
# ==== BEGIN: perf constants ====
PAGE_SIZE  = 200     # rows per page in Browse
MAX_RENDER = 1000    # final UI safety cap
MIN_Q_LEN  = 2       # avoid heavy scans on single letters
# ==== END: perf constants ====

# ---- Session-state safety defaults (defensive) ----
for _k, _v in {
    "q": "",
    "_prev_q": "",
    "edit_vendor_id": None,
}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ---- Adopt Streamlit secrets into env (defensive) ----
try:
    for k, v in st.secrets.items():
        os.environ.setdefault(str(k), str(v))
except Exception:
    pass
# ==== BEGIN: cached-query DSN helper (for hashing) ====
def _dsn_for_cache() -> str:
    """
    Build a stable DSN string for cache hashing without changing how build_engine() connects.
    Prefers explicit LIBSQL_URL_FULL / TURSO_DATABASE_URL+TOKEN, otherwise embedded/local.
    """
    url_full = (_get_secret("LIBSQL_URL_FULL", "") or "").strip()
    if url_full.startswith("libsql://"):
        return f"sqlite+libsql:///?url={url_full}"

    turso_url = (_get_secret("TURSO_DATABASE_URL", "") or "").strip()
    turso_tok = (_get_secret("TURSO_AUTH_TOKEN", "") or "").strip()
    if turso_url.startswith("libsql://"):
        sep = "&" if "?" in turso_url else "?"
        url = turso_url
        if "authToken=" not in url and turso_tok:
            url += f"{sep}authToken={turso_tok}"
            sep = "&"
        if "tls=" not in url:
            url += f"{sep}tls=true"
        return f"sqlite+libsql:///?url={url}"

    # Fallback to embedded/local file paths used by build_engine()
    embedded = os.path.abspath(_resolve_str("EMBEDDED_DB_PATH", "vendors-embedded.db") or "vendors-embedded.db")
    return f"sqlite:///{embedded}"
# ==== END: cached-query DSN helper ====

# -----------------------------
# Helpers
# -----------------------------
def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _get_secret(name: str, default: str | None = None) -> str | None:
    """Prefer Streamlit secrets, fallback to environment, then default."""
    try:
        if name in st.secrets:
            return st.secrets[name]  # type: ignore[index]
    except Exception:
        pass
    return os.getenv(name, default)

def _resolve_bool(name: str, code_default: bool) -> bool:
    v = _get_secret(name, None)
    return _as_bool(v, default=code_default)

def _resolve_str(name: str, code_default: str | None) -> str | None:
    v = _get_secret(name, None)
    return v if v is not None else code_default

def _ct_equals(a: str, b: str) -> bool:
    """Constant-time string compare for secrets."""
    return hmac.compare_digest((a or ""), (b or ""))

# -----------------------------
# Hrana/libSQL transient error retry
# -----------------------------
def _is_hrana_stale_stream_error(err: Exception) -> bool:
    s = str(err).lower()
    return ("hrana" in s and "404" in s and "stream not found" in s) or ("stream not found" in s)

def _exec_with_retry(engine: Engine, sql: str, params: dict[str, Any] | None = None, *, tries: int = 2):
    """
    Execute a write (INSERT/UPDATE/DELETE) with a one-time retry on Hrana 'stream not found'.
    Returns the result proxy so you can read .rowcount.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.begin() as conn:
                return conn.execute(sql_text(sql), params or {})
        except Exception as e:
            if attempt < tries and _is_hrana_stale_stream_error(e):
                try:
                    engine.dispose()  # drop pooled connections
                except Exception:
                    pass
                time.sleep(0.2)
                continue
            raise

def _fetch_with_retry(engine: Engine, sql: str, params: dict[str, Any] | None = None, *, tries: int = 2) -> pd.DataFrame:
    """
    Execute a read (SELECT) with a one-time retry on Hrana 'stream not found'.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                res = conn.execute(sql_text(sql), params or {})
                return pd.DataFrame(res.mappings().all())
        except Exception as e:
            if attempt < tries and _is_hrana_stale_stream_error(e):
                try:
                    engine.dispose()
                except Exception:
                    pass
                time.sleep(0.2)
                continue
            raise

# ---------- Form state helpers (Add / Edit / Delete) ----------
# Add form keys
ADD_FORM_KEYS: list[str] = [
    "add_business_name", "add_category", "add_service", "add_contact_name",
    "add_phone", "add_address", "add_website", "add_notes", "add_keywords",
]

def _init_add_form_defaults():
    for k in ADD_FORM_KEYS:
        if k not in st.session_state:
            st.session_state[k] = ""
    st.session_state.setdefault("add_form_version", 0)
    st.session_state.setdefault("_pending_add_reset", False)
    st.session_state.setdefault("add_last_done", None)
    st.session_state.setdefault("add_nonce", uuid.uuid4().hex)

def _apply_add_reset_if_needed():
    """Apply queued reset BEFORE rendering widgets to avoid invalid-option errors."""
    if st.session_state.get("_pending_add_reset"):
        for k in ADD_FORM_KEYS:
            st.session_state[k] = ""
        st.session_state["_pending_add_reset"] = False
        st.session_state["add_form_version"] += 1

def _queue_add_form_reset():
    st.session_state["_pending_add_reset"] = True

# Edit form keys
EDIT_FORM_KEYS: list[str] = [
    "edit_vendor_id", "edit_business_name", "edit_category", "edit_service",
    "edit_contact_name", "edit_phone", "edit_address", "edit_website",
    "edit_notes", "edit_keywords", "edit_row_updated_at", "edit_last_loaded_id",
]

def _init_edit_form_defaults():
    defaults: dict[str, Any] = {
        "edit_vendor_id": None,
        "edit_business_name": "",
        "edit_category": "",
        "edit_service": "",
        "edit_contact_name": "",
        "edit_phone": "",
        "edit_address": "",
        "edit_website": "",
        "edit_notes": "",
        "edit_keywords": "",
        "edit_row_updated_at": None,
        "edit_last_loaded_id": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)
    st.session_state.setdefault("edit_form_version", 0)
    st.session_state.setdefault("_pending_edit_reset", False)
    st.session_state.setdefault("edit_last_done", None)
    st.session_state.setdefault("edit_nonce", uuid.uuid4().hex)

def _apply_edit_reset_if_needed():
    """
    Apply queued reset BEFORE rendering edit widgets.
    Also clear the selection (edit_vendor_id) and the selectbox key so the UI returns to “— Select —”.
    """
    if st.session_state.get("_pending_edit_reset"):
        # Clear all edit fields AND selection
        for k in EDIT_FORM_KEYS:
            if k == "edit_vendor_id":
                st.session_state[k] = None
            elif k in ("edit_row_updated_at", "edit_last_loaded_id"):
                st.session_state[k] = None
            else:
                st.session_state[k] = ""
        # Also drop the legacy selectbox label key if present (from older builds)
        if "edit_provider_label" in st.session_state:
            del st.session_state["edit_provider_label"]
        st.session_state["_pending_edit_reset"] = False
        st.session_state["edit_form_version"] += 1

def _queue_edit_form_reset():
    st.session_state["_pending_edit_reset"] = True

# Delete form keys
DELETE_FORM_KEYS: list[str] = ["delete_vendor_id"]

def _init_delete_form_defaults():
    st.session_state.setdefault("delete_vendor_id", None)
    st.session_state.setdefault("delete_form_version", 0)
    st.session_state.setdefault("_pending_delete_reset", False)
    st.session_state.setdefault("delete_last_done", None)
    st.session_state.setdefault("delete_nonce", uuid.uuid4().hex)

def _apply_delete_reset_if_needed():
    if st.session_state.get("_pending_delete_reset"):
        st.session_state["delete_vendor_id"] = None
        # Also clear the delete selectbox UI key so it resets to sentinel
        if "delete_provider_label" in st.session_state:
            del st.session_state["delete_provider_label"]
        st.session_state["_pending_delete_reset"] = False
        st.session_state["delete_form_version"] += 1

def _queue_delete_form_reset():
    st.session_state["_pending_delete_reset"] = True

# Nonce helpers
def _nonce(name: str) -> str:
    return st.session_state.get(f"{name}_nonce")

def _nonce_rotate(name: str) -> None:
    st.session_state[f"{name}_nonce"] = uuid.uuid4().hex

# General-purpose key helpers (used in Category/Service admins)
def _clear_keys(*keys: str) -> None:
    for k in keys:
        if k in st.session_state:
            del st.session_state[k]

def _set_empty(*keys: str) -> None:
    for k in keys:
        st.session_state[k] = ""

def _reset_select(key: str, sentinel: str = "— Select —") -> None:
    st.session_state[key] = sentinel

# ---------- Category / Service queued reset helpers ----------
def _init_cat_defaults():
    st.session_state.setdefault("cat_form_version", 0)
    st.session_state.setdefault("_pending_cat_reset", False)

def _apply_cat_reset_if_needed():
    if st.session_state.get("_pending_cat_reset"):
        # Clear text inputs
        st.session_state["cat_add"] = ""
        st.session_state["cat_rename"] = ""
        # Reset selects by dropping keys so they render at sentinel on next run
        for k in ("cat_old", "cat_del", "cat_reassign_to"):
            if k in st.session_state:
                del st.session_state[k]
        st.session_state["_pending_cat_reset"] = False
        st.session_state["cat_form_version"] += 1

def _queue_cat_reset():
    st.session_state["_pending_cat_reset"] = True

def _init_svc_defaults():
    st.session_state.setdefault("svc_form_version", 0)
    st.session_state.setdefault("_pending_svc_reset", False)

def _apply_svc_reset_if_needed():
    if st.session_state.get("_pending_svc_reset"):
        st.session_state["svc_add"] = ""
        st.session_state["svc_rename"] = ""
        for k in ("svc_old", "svc_del", "svc_reassign_to"):
            if k in st.session_state:
                del st.session_state[k]
        st.session_state["_pending_svc_reset"] = False
        st.session_state["svc_form_version"] += 1

def _queue_svc_reset():
    st.session_state["_pending_svc_reset"] = True

# -----------------------------
# Page config & CSS
# -----------------------------
PAGE_TITLE = _resolve_str("page_title", "Vendors Admin") or "Vendors Admin"
SIDEBAR_STATE = _resolve_str("sidebar_state", "expanded") or "expanded"

LEFT_PAD_PX = int(_resolve_str("page_left_padding_px", "40") or "40")

st.markdown(
    f"""
    <style>
      [data-testid="stAppViewContainer"] .main .block-container {{
        padding-left: {LEFT_PAD_PX}px !important;
        padding-right: 0 !important;
      }}
      div[data-testid="stDataFrame"] table {{ white-space: nowrap; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------
# Admin sign-in gate (deterministic toggle)
# -----------------------------
# Code defaults (lowest precedence) — change here if you want different code-fallbacks.
DISABLE_ADMIN_PASSWORD_DEFAULT = True      # True = bypass, False = require password
ADMIN_PASSWORD_DEFAULT = "admin"

DISABLE_LOGIN = _resolve_bool("DISABLE_ADMIN_PASSWORD", DISABLE_ADMIN_PASSWORD_DEFAULT)
ADMIN_PASSWORD = (_resolve_str("ADMIN_PASSWORD", ADMIN_PASSWORD_DEFAULT) or "").strip()

if DISABLE_LOGIN:
    # Bypass gate
    pass
else:
    if not ADMIN_PASSWORD:
        st.error("ADMIN_PASSWORD is not set (Secrets/Env).")
        st.stop()
    if "auth_ok" not in st.session_state:
        st.session_state["auth_ok"] = False
    if not st.session_state["auth_ok"]:
        st.subheader("Admin sign-in")
        pw = st.text_input("Password", type="password", key="admin_pw")
        if st.button("Sign in"):
            if _ct_equals((pw or "").strip(), ADMIN_PASSWORD):
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()

# -----------------------------
# DB helpers
# -----------------------------
REQUIRED_VENDOR_COLUMNS: list[str] = ["business_name", "category"]  # service optional

def build_engine() -> tuple[Engine, dict[str, Any]]:
    """Prefer Turso/libsql embedded replica; otherwise local sqlite if FORCE_LOCAL=1."""
    info: dict[str, Any] = {}

    url = (_resolve_str("TURSO_DATABASE_URL", "") or "").strip()
    token = (_resolve_str("TURSO_AUTH_TOKEN", "") or "").strip()
    embedded_path = os.path.abspath(_resolve_str("EMBEDDED_DB_PATH", "vendors-embedded.db") or "vendors-embedded.db")

    if not url:
        # No remote configured → plain local file DB
        eng = create_engine(
            "sqlite:///vendors.db",
            pool_pre_ping=True,
            pool_recycle=300,
            pool_reset_on_return="commit",
        )
        info.update(
            {
                "using_remote": False,
                "sqlalchemy_url": "sqlite:///vendors.db",
                "dialect": eng.dialect.name,
                "driver": getattr(eng.dialect, "driver", ""),
            }
        )
        return eng, info

    # Embedded replica: local file that syncs to your remote Turso DB
    try:
        # Normalize sync_url: embedded REQUIRES libsql:// (no sqlite+libsql, no ?secure=true)
        raw = url
        if raw.startswith("sqlite+libsql://"):
            host = raw.split("://", 1)[1].split("?", 1)[0]  # drop any ?secure=true
            sync_url = f"libsql://{host}"
        else:
            sync_url = raw.split("?", 1)[0]  # already libsql://...

        eng = create_engine(
            f"sqlite+libsql:///{embedded_path}",
            connect_args={
                "auth_token": token,
                "sync_url": sync_url,
            },
            pool_pre_ping=True,
            pool_recycle=300,
            pool_reset_on_return="commit",
        )
        with eng.connect() as c:
            c.exec_driver_sql("select 1;")

        info.update(
            {
                "using_remote": True,
                "strategy": "embedded_replica",
                "sqlalchemy_url": f"sqlite+libsql:///{embedded_path}",
                "dialect": eng.dialect.name,
                "driver": getattr(eng.dialect, "driver", ""),
                "sync_url": sync_url,
            }
        )
        return eng, info

    except Exception as e:
        info["remote_error"] = f"{e}"
        allow_local = _as_bool(os.getenv("FORCE_LOCAL"), False)
        if allow_local:
            eng = create_engine(
                "sqlite:///vendors.db",
                pool_pre_ping=True,
                pool_recycle=300,
                pool_reset_on_return="commit",
            )
            info.update(
                {
                    "using_remote": False,
                    "sqlalchemy_url": "sqlite:///vendors.db",
                    "dialect": eng.dialect.name,
                    "driver": getattr(eng.dialect, "driver", ""),
                }
            )
            return eng, info

        st.error("Remote DB unavailable and FORCE_LOCAL is not set. Aborting to protect data.")
        raise

def ensure_schema(engine: Engine) -> None:
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS vendors (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL,
          service TEXT,
          business_name TEXT NOT NULL,
          contact_name TEXT,
          phone TEXT,
          address TEXT,
          website TEXT,
          notes TEXT,
          keywords TEXT,
          created_at TEXT,
          updated_at TEXT,
          updated_by TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS categories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS services (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_vendors_cat ON vendors(category)",
        "CREATE INDEX IF NOT EXISTS idx_vendors_bus ON vendors(business_name)",
        "CREATE INDEX IF NOT EXISTS idx_vendors_kw  ON vendors(keywords)",
        "CREATE INDEX IF NOT EXISTS idx_vendors_bus_lower ON vendors(lower(business_name))",
        "CREATE INDEX IF NOT EXISTS idx_vendors_cat_lower ON vendors(lower(category))",
        "CREATE INDEX IF NOT EXISTS idx_vendors_svc_lower ON vendors(lower(service))",
        "CREATE INDEX IF NOT EXISTS idx_vendors_phone ON vendors(phone)",
    ]

    with engine.begin() as conn:
        for s in stmts:
            conn.execute(sql_text(s))

        # ---- CKW columns (idempotent ALTERs) ----
        try:
            cols = {r[1] for r in conn.execute(sql_text("PRAGMA table_info(vendors)")).fetchall()}
            alters: list[str] = []
            if "computed_keywords" not in cols:
                alters.append("ALTER TABLE vendors ADD COLUMN computed_keywords TEXT")
            if "ckw_locked" not in cols:
                alters.append("ALTER TABLE vendors ADD COLUMN ckw_locked INTEGER DEFAULT 0")
            if "ckw_version" not in cols:
                alters.append("ALTER TABLE vendors ADD COLUMN ckw_version TEXT")
            for stmt in alters:
                conn.execute(sql_text(stmt))
        except Exception:
            # Non-fatal: if the table is new those columns will exist after first boot;
            # PRAGMA behavior can differ across drivers. Ignore quietly.
            pass

        # Normalize existing rows so indexes/filters behave predictably
        conn.execute(sql_text("UPDATE vendors SET ckw_locked = IFNULL(ckw_locked, 0)"))
        conn.execute(sql_text("UPDATE vendors SET ckw_version = IFNULL(ckw_version, '')"))

        # ---- CKW indexes (match prod) ----
        conn.execute(sql_text(
            "CREATE INDEX IF NOT EXISTS idx_vendors_ckw ON vendors(computed_keywords)"
        ))
        # Compact “status” index that helps maintenance probes
        conn.execute(sql_text(
            "CREATE INDEX IF NOT EXISTS idx_vendors_ckw_status ON vendors(IFNULL(ckw_locked,0), ckw_version)"
        ))
        # Simple service index to mirror prod
        conn.execute(sql_text(
            "CREATE INDEX IF NOT EXISTS idx_vendors_svc ON vendors(service)"
        ))
        # ---- Additional helpful indexes (perf) ----
        conn.execute(sql_text(
            "CREATE INDEX IF NOT EXISTS idx_vendors_updated_at ON vendors(updated_at)"
        ))
        conn.execute(sql_text(
            "CREATE INDEX IF NOT EXISTS idx_vendors_bus_lower2 ON vendors(lower(business_name))"
        ))

        # ---- Optional FTS5 virtual table + triggers (idempotent; skip if driver lacks FTS) ----
        try:
            conn.execute(sql_text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS vendors_fts USING fts5(
                    business_name, category, service, computed_keywords, keywords,
                    content='vendors', content_rowid='id'
                );
            """))
            conn.execute(sql_text("""
                CREATE TRIGGER IF NOT EXISTS vendors_ai AFTER INSERT ON vendors BEGIN
                    INSERT INTO vendors_fts(rowid, business_name, category, service, computed_keywords, keywords)
                    VALUES (new.id, new.business_name, new.category, new.service, new.computed_keywords, new.keywords);
                END;
            """))
            conn.execute(sql_text("""
                CREATE TRIGGER IF NOT EXISTS vendors_ad AFTER DELETE ON vendors BEGIN
                    INSERT INTO vendors_fts(vendors_fts, rowid, business_name, category, service, computed_keywords, keywords)
                    VALUES('delete', old.id, old.business_name, old.category, old.service, old.computed_keywords, old.keywords);
                END;
            """))
            conn.execute(sql_text("""
                CREATE TRIGGER IF NOT EXISTS vendors_au AFTER UPDATE ON vendors BEGIN
                    INSERT INTO vendors_fts(vendors_fts, rowid, business_name, category, service, computed_keywords, keywords)
                    VALUES('delete', old.id, old.business_name, old.category, old.service, old.computed_keywords, old.keywords);
                    INSERT INTO vendors_fts(rowid, business_name, category, service, computed_keywords, keywords)
                    VALUES (new.id, new.business_name, new.category, new.service, new.computed_keywords, new.keywords);
                END;
            """))
        except Exception:
            # If FTS5 is not available (driver/platform), skip quietly
            pass

def _normalize_phone(val: str | None) -> str:
    if not val:
        return ""
    digits = re.sub(r"\D", "", str(val))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else digits

def _format_phone(val: str | None) -> str:
    s = re.sub(r"\D", "", str(val or ""))
    if len(s) == 10:
        return f"({s[0:3]}) {s[3:6]}-{s[6:10]}"
    return (val or "").strip()

def _sanitize_url(url: str | None) -> str:
    if not url:
        return ""
    url = url.strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url

def load_df(engine: Engine) -> pd.DataFrame:
    with engine.begin() as conn:
        df = pd.read_sql(sql_text("SELECT * FROM vendors ORDER BY lower(business_name)"), conn)

    for col in [
        "contact_name",
        "phone",
        "address",
        "website",
        "notes",
        "keywords",
        "service",
        "created_at",
        "updated_at",
        "updated_by",
    ]:
        if col not in df.columns:
            df[col] = ""

    # Display-friendly phone; storage remains digits
    df["phone_fmt"] = df["phone"].apply(_format_phone)

    return df

def list_names(engine: Engine, table: str) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(sql_text(f"SELECT name FROM {table} ORDER BY lower(name)")).fetchall()
    return [r[0] for r in rows]

def usage_count(engine: Engine, col: str, name: str) -> int:
    with engine.begin() as conn:
        cnt = conn.execute(sql_text(f"SELECT COUNT(*) FROM vendors WHERE {col} = :n"), {"n": name}).scalar()
    return int(cnt or 0)
# ==== BEGIN: Cached SQL query layer for Browse ====
def _supports_fts(engine: Engine) -> bool:
    try:
        with engine.connect() as cx:
            cx.execute(sql_text("SELECT count(*) FROM vendors_fts LIMIT 1"))
        return True
    except Exception:
        return False

def _count_sql(q: str, use_fts: bool) -> str:
    if use_fts and len(q) >= MIN_Q_LEN and q:
        return """
            SELECT COUNT(*) FROM vendors v
            JOIN vendors_fts f ON f.rowid = v.id
            WHERE vendors_fts MATCH :q
        """
    return """
        SELECT COUNT(*) FROM vendors
        WHERE (:q = '')
           OR (LOWER(business_name)     LIKE '%' || :q || '%')
           OR (LOWER(computed_keywords) LIKE '%' || :q || '%')
           OR (LOWER(keywords)          LIKE '%' || :q || '%')
    """

def _page_sql(q: str, use_fts: bool) -> str:
    if use_fts and len(q) >= MIN_Q_LEN and q:
        return """
            SELECT v.* FROM vendors v
            JOIN vendors_fts f ON f.rowid = v.id
            WHERE vendors_fts MATCH :q
            ORDER BY v.business_name
            LIMIT :limit OFFSET :offset
        """
    return """
        SELECT * FROM vendors
        WHERE (:q = '')
           OR (LOWER(business_name)     LIKE '%' || :q || '%')
           OR (LOWER(computed_keywords) LIKE '%' || :q || '%')
           OR (LOWER(keywords)          LIKE '%' || :q || '%')
        ORDER BY business_name
        LIMIT :limit OFFSET :offset
    """

@st.cache_data(show_spinner=False)
def query_count_cached(engine_dsn: str, q: str, use_fts: bool, version: str) -> int:
    eng = create_engine(engine_dsn, pool_pre_ping=True, pool_recycle=300)
    with eng.connect() as cx:
        res = cx.execute(sql_text(_count_sql(q, use_fts)), {"q": (q or "").lower()})
        return int(res.scalar() or 0)

@st.cache_data(show_spinner=False)
def query_page_cached(engine_dsn: str, q: str, use_fts: bool, version: str,
                      page: int, page_size: int) -> pd.DataFrame:
    eng = create_engine(engine_dsn, pool_pre_ping=True, pool_recycle=300)
    with eng.connect() as cx:
        offset = max(0, int(page)) * max(1, int(page_size))
        return pd.read_sql(
            sql_text(_page_sql(q, use_fts)),
            cx,
            params={"q": (q or "").lower(), "limit": int(page_size), "offset": int(offset)},
        )
# ==== END: Cached SQL query layer for Browse ====

# -----------------------------
# CSV Restore helpers (append-only, ID-checked)
# -----------------------------
def _get_table_columns(engine: Engine, table: str) -> list[str]:
    with engine.connect() as conn:
        res = conn.execute(sql_text(f"SELECT * FROM {table} LIMIT 0"))
        return list(res.keys())

def _fetch_existing_ids(engine: Engine, table: str = "vendors") -> set[int]:
    with engine.connect() as conn:
        rows = conn.execute(sql_text(f"SELECT id FROM {table}")).all()
    return {int(r[0]) for r in rows if r[0] is not None}

def _prepare_csv_for_append(
    engine: Engine,
    csv_df: pd.DataFrame,
    *,
    normalize_phone: bool,
    trim_strings: bool,
    treat_missing_id_as_autoincrement: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, list[int], list[str]]:
    """
    Returns: (with_id_df, without_id_df, rejected_existing_ids, insertable_columns)
    DataFrames are already filtered to allowed columns and safe to insert.
    """
    df = csv_df.copy()

    # Trim strings
    if trim_strings:
        for c in df.columns:
            if pd.api.types.is_object_dtype(df[c]):
                df[c] = df[c].astype(str).str.strip()

    # Normalize phone to digits
    if normalize_phone and "phone" in df.columns:
        df["phone"] = df["phone"].astype(str).str.replace(r"\D+", "", regex=True)

    db_cols = _get_table_columns(engine, "vendors")
    insertable_cols = [c for c in df.columns if c in db_cols]

    # Required columns present?
    missing_req = [c for c in REQUIRED_VENDOR_COLUMNS if c not in df.columns]
    if missing_req:
        raise ValueError(f"Missing required column(s) in CSV: {missing_req}")

    # Handle id column
    has_id = "id" in df.columns
    existing_ids = _fetch_existing_ids(engine)

    if has_id:
        df["id"] = pd.to_numeric(df["id"], errors="coerce").astype("Int64")
        # Reject rows colliding with existing ids
        mask_conflict = df["id"].notna() & df["id"].astype("Int64").astype("int", errors="ignore").isin(existing_ids)
        rejected_existing_ids = df.loc[mask_conflict, "id"].dropna().astype(int).tolist()
        df_ok = df.loc[~mask_conflict].copy()

        # Split by having id vs. not
        with_id_df = df_ok[df_ok["id"].notna()].copy()
        without_id_df = df_ok[df_ok["id"].isna()].copy() if treat_missing_id_as_autoincrement else pd.DataFrame(columns=df.columns)
    else:
        rejected_existing_ids = []
        with_id_df = pd.DataFrame(columns=df.columns)
        without_id_df = df.copy()

    # Limit to insertable columns and coerce NaN->None for DB
    def _prep_cols(d: pd.DataFrame, drop_id: bool) -> pd.DataFrame:
        cols = [c for c in insertable_cols if (c != "id" if drop_id else True)]
        if not cols:
            return pd.DataFrame(columns=[])
        dd = d[cols].copy()
        for c in cols:
            dd[c] = dd[c].where(pd.notnull(dd[c]), None)
        return dd

    with_id_df = _prep_cols(with_id_df, drop_id=False)
    without_id_df = _prep_cols(without_id_df, drop_id=True)

    # Duplicate ids inside the CSV itself?
    if "id" in csv_df.columns:
        dup_ids = (
            csv_df["id"]
            .pipe(pd.to_numeric, errors="coerce")
            .dropna()
            .astype(int)
            .duplicated(keep=False)
        )
        if dup_ids.any():
            dups = sorted(csv_df.loc[dup_ids, "id"].dropna().astype(int).unique().tolist())
            raise ValueError(f"Duplicate id(s) inside CSV: {dups}")

    return with_id_df, without_id_df, rejected_existing_ids, insertable_cols

def _execute_append_only(
    engine: Engine,
    with_id_df: pd.DataFrame,
    without_id_df: pd.DataFrame,
    insertable_cols: list[str],
) -> int:
    """Executes INSERTs in a single transaction. Returns total inserted rows."""
    inserted = 0
    with engine.begin() as conn:
        # with explicit id
        if not with_id_df.empty:
            cols = list(with_id_df.columns)  # includes 'id' by construction
            placeholders = ", ".join(":" + c for c in cols)
            stmt = sql_text(f"INSERT INTO vendors ({', '.join(cols)}) VALUES ({placeholders})")
            conn.execute(stmt, with_id_df.to_dict(orient="records"))
            inserted += len(with_id_df)

        # without id (autoincrement)
        if not without_id_df.empty:
            cols = list(without_id_df.columns)  # 'id' removed already
            placeholders = ", ".join(":" + c for c in cols)
            stmt = sql_text(f"INSERT INTO vendors ({', '.join(cols)}) VALUES ({placeholders})")
            conn.execute(stmt, without_id_df.to_dict(orient="records"))
            inserted += len(without_id_df)

    return inserted

# -----------------------------
# UI
# -----------------------------
engine, engine_info = build_engine()
ensure_schema(engine)

# Apply WAL PRAGMAs for local SQLite (not libsql driver)
try:
    if not engine_info.get("using_remote", False) and engine_info.get("driver", "") != "libsql":
        with engine.begin() as _conn:
            _conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            _conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
except Exception:
    pass

_tabs = st.tabs(
    [
        "Browse Vendors",
        "Add / Edit / Delete Vendor",
        "Category Admin",
        "Service Admin",
        "Maintenance",
        "Debug",
    ]
)

# ---- Safe search-blob builder (works across pandas 2.1/2.2/2.3) ----
def _safe_search_blob(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    use = [c for c in columns if c in df.columns]
    if not use:
        return pd.Series([""] * len(df), index=df.index, dtype="object")

    tmp = (
        df[use]
        .astype("string")
        .fillna("")
        .replace({"<NA>": "", "None": "", "nan": ""})
    )
    # Join row values with spaces and lowercase
    return tmp.apply(lambda row: " ".join(v for v in row if v), axis=1).str.lower()
# ====== END FUNCTION ======

with _tabs[0]:
    # --- Load data for Browse Vendors tab ---
    df = load_df(engine)

    # --- Build a lowercase search blob once (guarded) ---
    if "_blob" not in df.columns:
        _blob_cols = [
            "business_name", "category", "service", "contact_name", "phone",
            "address", "website", "notes", "keywords", "computed_keywords"
        ]
        df["_blob"] = _safe_search_blob(df, _blob_cols)

    # --- Search input at 25% width (table remains full width) ---
    left, right = st.columns([1, 3])
    with left:
        st.text_input(
            "Search",
            placeholder="Search providers… (press Enter)",
            label_visibility="collapsed",
            key="q",
        )

    # ---- Resolve current query from session ----
    q = (st.session_state.get("q") or "").strip()

    # ---- Reset any edit selection when search changes ----
    _prev_q = st.session_state.get("_prev_q", None)
    if q != (_prev_q or ""):
        st.session_state["edit_vendor_id"] = None
    st.session_state["_prev_q"] = q

    # ---- Keep ?q synchronized with session state (guarded; avoids rerun loops) ----
    try:
        if hasattr(st, "query_params"):
            existing_q = ""
            try:
                existing_q = st.query_params.get("q") or ""
            except Exception:
                existing_q = ""
            if existing_q != q:
                st.query_params["q"] = q
    except Exception:
        # Never let param sync crash the app
        pass

    # ---- Build filtered view (fast substring over prebuilt _blob) ----
    qq = q.lower()
    if "_blob" not in df.columns:
        # Fallback: build a minimal blob on the fly (slower)
        cols = [c for c in ["category", "service", "business_name", "notes", "keywords"] if c in df.columns]
        df["_blob"] = df[cols].astype(str).agg(" ".join, axis=1).str.lower()

    vdf = df
    if qq:
        vdf = df[df["_blob"].str.contains(qq, na=False)]
    _render = None

    # ==== BEGIN: Browse render (safe) ====
    MAX_ROWS = 1000
    if vdf is None or vdf.empty:
        st.info("No matching providers. Tip: try fewer words.")
    else:
        _render = vdf.head(MAX_ROWS).copy()
        # Optional: quiet debug breadcrumb (gated)
        if os.getenv("ADMIN_SHOW_DEBUG", "").strip() == "1" or st.session_state.get("show_debug"):
            st.caption(f"Browse — showing {len(_render)}/{len(vdf)} (cap {MAX_ROWS}); total df: {len(df)}")
        st.dataframe(_render, use_container_width=True)
    # ==== END: Browse render (safe) ====

    # Optional: CSV download of the currently rendered subset (_render)
    try:
        if _render is not None and not _render.empty:
            ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            st.download_button(
                "Download filtered view (CSV)",
                data=_render.to_csv(index=False).encode("utf-8"),
                file_name=f"providers_{ts}.csv",
                mime="text/csv",
            )
    except Exception:
        # _render only exists when vdf is non-empty; safe to ignore if not defined
        pass

# ---------- Add/Edit/Delete Vendor
with _tabs[1]:
    # ===== Add Vendor =====
    st.subheader("Add Vendor")
    _init_add_form_defaults()
    _apply_add_reset_if_needed()  # apply queued reset BEFORE creating widgets

    cats = list_names(engine, "categories")
    servs = list_names(engine, "services")

    add_form_key = f"add_vendor_form_{st.session_state['add_form_version']}"
    with st.form(add_form_key, clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            st.text_input("Provider *", key="add_business_name")

            # Category select—options include "" placeholder; we DON'T pass index when using session state
            _add_cat_options = [""] + (cats or [])
            if (st.session_state.get("add_category") or "") not in _add_cat_options:
                st.session_state["add_category"] = ""
            st.selectbox("Category *", options=_add_cat_options, key="add_category", placeholder="Select category")

            # Service select—same pattern
            _add_svc_options = [""] + (servs or [])
            if (st.session_state.get("add_service") or "") not in _add_svc_options:
                st.session_state["add_service"] = ""
            st.selectbox("Service (optional)", options=_add_svc_options, key="add_service")

            st.text_input("Contact Name", key="add_contact_name")
            st.text_input("Phone (10 digits or blank)", key="add_phone")
        with col2:
            st.text_area("Address", height=80, key="add_address")
            st.text_input("Website (https://…)", key="add_website")
            st.text_area("Notes", height=100, key="add_notes")
            st.text_input("Keywords (comma separated)", key="add_keywords")

        submitted = st.form_submit_button("Add Vendor")

    if submitted:
        add_nonce = _nonce("add")
        if st.session_state.get("add_last_done") == add_nonce:
            st.info("Add already processed.")
            st.stop()

        business_name = (st.session_state["add_business_name"] or "").strip()
        category      = (st.session_state["add_category"] or "").strip()
        service       = (st.session_state["add_service"] or "").strip()
        contact_name  = (st.session_state["add_contact_name"] or "").strip()
        phone_norm    = _normalize_phone(st.session_state["add_phone"])
        address       = (st.session_state["add_address"] or "").strip()
        website       = _sanitize_url(st.session_state["add_website"])
        notes         = (st.session_state["add_notes"] or "").strip()
        keywords      = (st.session_state["add_keywords"] or "").strip()

        # Minimal-change validation: phone must be 10 digits or blank
        if phone_norm and len(phone_norm) != 10:
            st.error("Phone must be 10 digits or blank.")
        elif not business_name or not category:
            st.error("Business Name and Category are required.")
        else:
            try:
                now = datetime.utcnow().isoformat(timespec="seconds")
                _exec_with_retry(
                    engine,
                    """
                    INSERT INTO vendors(category, service, business_name, contact_name, phone, address,
                                        website, notes, keywords, created_at, updated_at, updated_by)
                    VALUES(:category, NULLIF(:service, ''), :business_name, :contact_name, :phone, :address,
                           :website, :notes, :keywords, :now, :now, :user)
                    """,
                    {
                        "category": category,
                        "service": service,
                        "business_name": business_name,
                        "contact_name": contact_name,
                        "phone": phone_norm,
                        "address": address,
                        "website": website,
                        "notes": notes,
                        "keywords": keywords,
                        "now": now,
                        "user": os.getenv("USER", "admin"),
                    },
                )
                st.session_state["add_last_done"] = add_nonce
                st.success(f"Vendor added: {business_name}")
                _queue_add_form_reset()
                _nonce_rotate("add")
                st.rerun()
            except Exception as e:
                st.error(f"Add failed: {e}")

    st.divider()
    st.subheader("Edit / Delete Vendor")

    df_all = load_df(engine)

    if df_all.empty:
        st.info("No vendors yet. Use 'Add Vendor' above to create your first record.")
    else:
        # Init + apply resets BEFORE rendering widgets
        _init_edit_form_defaults()
        _init_delete_form_defaults()
        _apply_edit_reset_if_needed()
        _apply_delete_reset_if_needed()

        # ----- EDIT: ID-backed selection with format_func -----
        ids = df_all["id"].astype(int).tolist()
        id_to_row = {int(r["id"]): r for _, r in df_all.iterrows()}

        def _fmt_vendor(i: int | None) -> str:
            if i is None:
                return "— Select —"
            r = id_to_row.get(int(i), None)
            if r is None:
                return f"{i}"
            cat = (r.get("category") or "")
            svc = (r.get("service") or "")
            tail = " / ".join([x for x in (cat, svc) if x]).strip(" /")
            name = str(r.get("business_name") or "")
            return f"{name} — {tail}" if tail else name

        st.selectbox(
            "Select provider to edit (type to search)",
            options=[None] + ids,
            format_func=_fmt_vendor,
            key="edit_vendor_id",
        )

        # Prefill only when selection changes
        if st.session_state["edit_vendor_id"] is not None:
            if st.session_state["edit_last_loaded_id"] != st.session_state["edit_vendor_id"]:
                row = id_to_row[int(st.session_state["edit_vendor_id"])]
                st.session_state.update({
                    "edit_business_name": row.get("business_name") or "",
                    "edit_category": row.get("category") or "",
                    "edit_service": row.get("service") or "",
                    "edit_contact_name": row.get("contact_name") or "",
                    "edit_phone": row.get("phone") or "",
                    "edit_address": row.get("address") or "",
                    "edit_website": row.get("website") or "",
                    "edit_notes": row.get("notes") or "",
                    "edit_keywords": row.get("keywords") or "",
                    "edit_row_updated_at": row.get("updated_at") or "",
                    "edit_last_loaded_id": st.session_state["edit_vendor_id"],
                })

        # -------- Edit form --------
        edit_form_key = f"edit_vendor_form_{st.session_state['edit_form_version']}"
        with st.form(edit_form_key, clear_on_submit=False):
            col1, col2 = st.columns(2)
            with col1:
                st.text_input("Provider *", key="edit_business_name")

                cats = list_names(engine, "categories")
                servs = list_names(engine, "services")

                _edit_cat_options = [""] + (cats or [])
                if (st.session_state.get("edit_category") or "") not in _edit_cat_options:
                    st.session_state["edit_category"] = ""
                st.selectbox("Category *", options=_edit_cat_options, key="edit_category", placeholder="Select category")

                _edit_svc_options = [""] + (servs or [])
                if (st.session_state.get("edit_service") or "") not in _edit_svc_options:
                    st.session_state["edit_service"] = ""
                st.selectbox("Service (optional)", options=_edit_svc_options, key="edit_service")

                st.text_input("Contact Name", key="edit_contact_name")
                st.text_input("Phone (10 digits or blank)", key="edit_phone")
            with col2:
                st.text_area("Address", height=80, key="edit_address")
                st.text_input("Website (https://…)", key="edit_website")
                st.text_area("Notes", height=100, key="edit_notes")
                st.text_input("Keywords (comma separated)", key="edit_keywords")

            edited = st.form_submit_button("Save Changes")

        if edited:
            edit_nonce = _nonce("edit")
            if st.session_state.get("edit_last_done") == edit_nonce:
                st.info("Edit already processed.")
                st.stop()

            vid = st.session_state.get("edit_vendor_id")
            if vid is None:
                st.error("Select a vendor first.")
            else:
                bn  = (st.session_state["edit_business_name"] or "").strip()
                cat = (st.session_state["edit_category"] or "").strip()
                phone_norm = _normalize_phone(st.session_state["edit_phone"])
                if phone_norm and len(phone_norm) != 10:
                    st.error("Phone must be 10 digits or blank.")
                elif not bn or not cat:
                    st.error("Business Name and Category are required.")
                else:
                    try:
                        prev_updated = st.session_state.get("edit_row_updated_at") or ""
                        now = datetime.utcnow().isoformat(timespec="seconds")
                        res = _exec_with_retry(engine, """
                            UPDATE vendors
                               SET category=:category,
                                   service=NULLIF(:service, ''),
                                   business_name=:business_name,
                                   contact_name=:contact_name,
                                   phone=:phone,
                                   address=:address,
                                   website=:website,
                                   notes=:notes,
                                   keywords=:keywords,
                                   updated_at=:now,
                                   updated_by=:user
                             WHERE id=:id AND (updated_at=:prev_updated OR :prev_updated='')
                        """, {
                            "category": cat,
                            "service": (st.session_state["edit_service"] or "").strip(),
                            "business_name": bn,
                            "contact_name": (st.session_state["edit_contact_name"] or "").strip(),
                            "phone": phone_norm,
                            "address": (st.session_state["edit_address"] or "").strip(),
                            "website": _sanitize_url(st.session_state["edit_website"]),
                            "notes": (st.session_state["edit_notes"] or "").strip(),
                            "keywords": (st.session_state["edit_keywords"] or "").strip(),
                            "now": now, "user": os.getenv("USER", "admin"),
                            "id": int(vid),
                            "prev_updated": prev_updated,
                        })
                        rowcount = res.rowcount or 0

                        if rowcount == 0:
                            st.warning("No changes applied (stale selection or already updated). Refresh and try again.")
                        else:
                            st.session_state["edit_last_done"] = edit_nonce
                            st.success(f"Vendor updated: {bn}")
                            _queue_edit_form_reset()
                            _nonce_rotate("edit")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Update failed: {e}")

        st.markdown("---")
        # Use separate delete selection (ID-backed similar approach could be added later)
        sel_label_del = st.selectbox(
            "Select provider to delete (type to search)",
            options=["— Select —"] + [_fmt_vendor(i) for i in ids],
            key="delete_provider_label",
        )
        if sel_label_del != "— Select —":
            # map back to id cheaply
            rev = {_fmt_vendor(i): i for i in ids}
            st.session_state["delete_vendor_id"] = int(rev.get(sel_label_del))
        else:
            st.session_state["delete_vendor_id"] = None

        del_form_key = f"delete_vendor_form_{st.session_state['delete_form_version']}"
        with st.form(del_form_key, clear_on_submit=False):
            deleted = st.form_submit_button("Delete Vendor")

        if deleted:
            del_nonce = _nonce("delete")
            if st.session_state.get("delete_last_done") == del_nonce:
                st.info("Delete already processed.")
                st.stop()

            vid = st.session_state.get("delete_vendor_id")
            if vid is None:
                st.error("Select a vendor first.")
            else:
                try:
                    row = df_all.loc[df_all["id"] == int(vid)]
                    prev_updated = (row.iloc[0]["updated_at"] if not row.empty else "") or ""
                    res = _exec_with_retry(engine, """
                        DELETE FROM vendors
                         WHERE id=:id AND (updated_at=:prev_updated OR :prev_updated='')
                    """, {"id": int(vid), "prev_updated": prev_updated})
                    rowcount = res.rowcount or 0

                    if rowcount == 0:
                        st.warning("No delete performed (stale selection). Refresh and try again.")
                    else:
                        st.session_state["delete_last_done"] = del_nonce
                        st.success("Vendor deleted.")
                        _queue_delete_form_reset()
                        _nonce_rotate("delete")
                        st.rerun()
                except Exception as e:
                    st.error(f"Delete failed: {e}")

# ---------- Category Admin
with _tabs[2]:
    st.caption("Category is required. Manage the reference list and reassign vendors safely.")
    _init_cat_defaults()
    _apply_cat_reset_if_needed()

    cats = list_names(engine, "categories")
    cat_opts = ["— Select —"] + cats  # sentinel first

    colA, colB = st.columns(2)
    with colA:
        st.subheader("Add Category")
        new_cat = st.text_input("New category name", key="cat_add")
        if st.button("Add Category", key="cat_add_btn"):
            if not (new_cat or "").strip():
                st.error("Enter a name.")
            else:
                try:
                    _exec_with_retry(engine, "INSERT OR IGNORE INTO categories(name) VALUES(:n)", {"n": new_cat.strip()})
                    st.success("Added (or already existed).")
                    _queue_cat_reset()
                    st.rerun()
                except Exception as e:
                    st.error(f"Add category failed: {e}")

        st.subheader("Rename Category")
        if cats:
            old = st.selectbox("Current", options=cat_opts, key="cat_old")  # no index
            new = st.text_input("New name", key="cat_rename")
            if st.button("Rename", key="cat_rename_btn"):
                if old == "— Select —":
                    st.error("Pick a category to rename.")
                elif not (new or "").strip():
                    st.error("Enter a new name.")
                else:
                    try:
                        _exec_with_retry(engine, "UPDATE categories SET name=:new WHERE name=:old", {"new": new.strip(), "old": old})
                        _exec_with_retry(engine, "UPDATE vendors SET category=:new WHERE category=:old", {"new": new.strip(), "old": old})
                        st.success("Renamed and reassigned.")
                        _queue_cat_reset()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Rename category failed: {e}")

    with colB:
        st.subheader("Delete / Reassign")
        if cats:
            tgt = st.selectbox("Category to delete", options=cat_opts, key="cat_del")  # no index
            if tgt == "— Select —":
                st.write("Select a category.")
            else:
                cnt = usage_count(engine, "category", tgt)
                st.write(f"In use by {cnt} vendor(s).")
                if cnt == 0:
                    if st.button("Delete category (no usage)", key="cat_del_btn"):
                        try:
                            _exec_with_retry(engine, "DELETE FROM categories WHERE name=:n", {"n": tgt})
                            st.success("Deleted.")
                            _queue_cat_reset()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Delete category failed: {e}")
                else:
                    repl_options = ["— Select —"] + [c for c in cats if c != tgt]
                    repl = st.selectbox("Reassign vendors to…", options=repl_options, key="cat_reassign_to")  # no index
                    if st.button("Reassign vendors then delete", key="cat_reassign_btn"):
                        if repl == "— Select —":
                            st.error("Choose a category to reassign to.")
                        else:
                            try:
                                _exec_with_retry(engine, "UPDATE vendors SET category=:r WHERE category=:t", {"r": repl, "t": tgt})
                                _exec_with_retry(engine, "DELETE FROM categories WHERE name=:t", {"t": tgt})
                                st.success("Reassigned and deleted.")
                                _queue_cat_reset()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Reassign+delete failed: {e}")

# ---------- Service Admin
with _tabs[3]:
    st.caption("Service is optional on vendors. Manage the reference list here.")
    _init_svc_defaults()
    _apply_svc_reset_if_needed()

    servs = list_names(engine, "services")
    svc_opts = ["— Select —"] + servs  # sentinel first

    colA, colB = st.columns(2)
    with colA:
        st.subheader("Add Service")
        new_s = st.text_input("New service name", key="svc_add")
        if st.button("Add Service", key="svc_add_btn"):
            if not (new_s or "").strip():
                st.error("Enter a name.")
            else:
                try:
                    _exec_with_retry(engine, "INSERT OR IGNORE INTO services(name) VALUES(:n)", {"n": new_s.strip()})
                    st.success("Added (or already existed).")
                    _queue_svc_reset()
                    st.rerun()
                except Exception as e:
                    st.error(f"Add service failed: {e}")

        st.subheader("Rename Service")
        if servs:
            old = st.selectbox("Current", options=svc_opts, key="svc_old")  # no index
            new = st.text_input("New name", key="svc_rename")
            if st.button("Rename Service", key="svc_rename_btn"):
                if old == "— Select —":
                    st.error("Pick a service to rename.")
                elif not (new or "").strip():
                    st.error("Enter a new name.")
                else:
                    try:
                        _exec_with_retry(engine, "UPDATE services SET name=:new WHERE name=:old", {"new": new.strip(), "old": old})
                        _exec_with_retry(engine, "UPDATE vendors SET service=:new WHERE service=:old", {"new": new.strip(), "old": old})
                        st.success(f"Renamed service: {old} → {new.strip()}")
                        _queue_svc_reset()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Rename service failed: {e}")

    with colB:
        st.subheader("Delete / Reassign")
        if servs:
            tgt = st.selectbox("Service to delete", options=svc_opts, key="svc_del")  # no index
            if tgt == "— Select —":
                st.write("Select a service.")
            else:
                cnt = usage_count(engine, "service", tgt)
                st.write(f"In use by {cnt} vendor(s).")
                if cnt == 0:
                    if st.button("Delete service (no usage)", key="svc_del_btn"):
                        try:
                            _exec_with_retry(engine, "DELETE FROM services WHERE name=:n", {"n": tgt})
                            st.success("Deleted.")
                            _queue_svc_reset()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Delete service failed: {e}")
                else:
                    repl_options = ["— Select —"] + [s for s in servs if s != tgt]
                    repl = st.selectbox("Reassign vendors to…", options=repl_options, key="svc_reassign_to")  # no index
                    if st.button("Reassign vendors then delete service", key="svc_reassign_btn"):
                        if repl == "— Select —":
                            st.error("Choose a service to reassign to.")
                        else:
                            try:
                                _exec_with_retry(engine, "UPDATE vendors SET service=:r WHERE service=:t", {"r": repl, "t": tgt})
                                _exec_with_retry(engine, "DELETE FROM services WHERE name=:t", {"t": tgt})
                                st.success("Reassigned and deleted.")
                                _queue_svc_reset()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Reassign+delete service failed: {e}")

# ---------- Maintenance
with _tabs[4]:
    st.caption("One-click cleanups for legacy data.")

    st.subheader("Export / Import")

    # Export full, untruncated CSV of all columns/rows
    query = "SELECT * FROM vendors ORDER BY lower(business_name)"
    with engine.begin() as conn:
        full = pd.read_sql(sql_text(query), conn)

    # Dual exports: full dataset — formatted phones and digits-only
    full_formatted = full.copy()

    def _format_phone_digits(x: str | int | None) -> str:
        s = re.sub(r"\D+", "", str(x or ""))
        return f"({s[0:3]}) {s[3:6]}-{s[6:10]}" if len(s) == 10 else s

    if "phone" in full_formatted.columns:
        full_formatted["phone"] = full_formatted["phone"].apply(_format_phone_digits)

    colA, colB = st.columns([1, 1])
    with colA:
        st.download_button(
            "Export all vendors (formatted phones)",
            data=full_formatted.to_csv(index=False).encode("utf-8"),
            file_name=f"providers_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv",
            mime="text/csv",
        )
    with colB:
        st.download_button(
            "Export all vendors (digits-only phones)",
            data=full.to_csv(index=False).encode("utf-8"),
            file_name=f"providers_raw_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv",
            mime="text/csv",
        )

    # CSV Restore UI (Append-only, ID-checked)
    with st.expander("CSV Restore (Append-only, ID-checked)", expanded=False):
        st.caption(
            "WARNING: This tool only **appends** rows. "
            "Rows whose `id` already exists are **rejected**. No updates, no deletes."
        )
        uploaded = st.file_uploader("Upload CSV to append into `vendors`", type=["csv"], accept_multiple_files=False)

        col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
        with col1:
            dry_run = st.checkbox("Dry run (validate only)", value=True)
        with col2:
            trim_strings = st.checkbox("Trim strings", value=True)
        with col3:
            normalize_phone = st.checkbox("Normalize phone to digits", value=True)
        with col4:
            auto_id = st.checkbox("Missing `id` ➜ autoincrement", value=True)

        if uploaded is not None:
            try:
                df_in = pd.read_csv(uploaded)
                with_id_df, without_id_df, rejected_ids, insertable_cols = _prepare_csv_for_append(
                    engine,
                    df_in,
                    normalize_phone=normalize_phone,
                    trim_strings=trim_strings,
                    treat_missing_id_as_autoincrement=auto_id,
                )

                planned_inserts = len(with_id_df) + len(without_id_df)

                st.write("**Validation summary**")
                st.write(
                    {
                        "csv_rows": int(len(df_in)),
                        "insertable_columns": insertable_cols,
                        "rows_with_explicit_id": int(len(with_id_df)),
                        "rows_autoincrement_id": int(len(without_id_df)),
                        "rows_rejected_due_to_existing_id": rejected_ids,
                        "planned_inserts": int(planned_inserts),
                    }
                )

                if dry_run:
                    st.success("Dry run complete. No changes applied.")
                else:
                    if planned_inserts == 0:
                        st.info("Nothing to insert (all rows rejected or CSV empty after filters).")
                    else:
                        inserted = _execute_append_only(engine, with_id_df, without_id_df, insertable_cols)
                        st.success(f"Inserted {inserted} row(s). Rejected existing id(s): {rejected_ids or 'None'}")
            except Exception as e:
                st.error(f"CSV restore failed: {e}")

    st.divider()
    st.subheader("Data cleanup")

    if st.button("Normalize phone numbers & Title Case (vendors + categories/services)"):
        def to_title(s: str | None) -> str:
            return ((s or "").strip()).title()

        TEXT_COLS_TO_TITLE = [
            "category",
            "service",
            "business_name",
            "contact_name",
            "address",
            "notes",
            "keywords",
        ]

        changed_vendors = 0
        try:
            with engine.begin() as conn:
                # --- vendors table ---
                rows = conn.execute(sql_text("SELECT * FROM vendors")).fetchall()
                for r in rows:
                    row = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
                    pid = int(row["id"])

                    vals = {c: to_title(row.get(c)) for c in TEXT_COLS_TO_TITLE}
                    vals["website"] = _sanitize_url((row.get("website") or "").strip())
                    vals["phone"] = _normalize_phone(row.get("phone") or "")
                    vals["id"] = pid

                    conn.execute(
                        sql_text(
                            """
                            UPDATE vendors
                               SET category=:category,
                                   service=NULLIF(:service,''),
                                   business_name=:business_name,
                                   contact_name=:contact_name,
                                   phone=:phone,
                                   address=:address,
                                   website=:website,
                                   notes=:notes,
                                   keywords=:keywords
                             WHERE id=:id
                            """
                        ),
                        vals,
                    )
                    changed_vendors += 1

                # --- categories table: retitle + reconcile duplicates by case ---
                cat_rows = conn.execute(sql_text("SELECT name FROM categories")).fetchall()
                for (old_name,) in cat_rows:
                    new_name = to_title(old_name)
                    if new_name != old_name:
                        conn.execute(sql_text("INSERT OR IGNORE INTO categories(name) VALUES(:n)"), {"n": new_name})
                        conn.execute(
                            sql_text("UPDATE vendors SET category=:new WHERE category=:old"),
                            {"new": new_name, "old": old_name},
                        )
                        conn.execute(sql_text("DELETE FROM categories WHERE name=:old"), {"old": old_name})

                # --- services table: retitle + reconcile duplicates by case ---
                svc_rows = conn.execute(sql_text("SELECT name FROM services")).fetchall()
                for (old_name,) in svc_rows:
                    new_name = to_title(old_name)
                    if new_name != old_name:
                        conn.execute(sql_text("INSERT OR IGNORE INTO services(name) VALUES(:n)"), {"n": new_name})
                        conn.execute(
                            sql_text("UPDATE vendors SET service=:new WHERE service=:old"),
                            {"new": new_name, "old": old_name},
                        )
                        conn.execute(sql_text("DELETE FROM services WHERE name=:old"), {"old": old_name})
            st.success(f"Vendors normalized: {changed_vendors}. Categories/services retitled and reconciled.")
        except Exception as e:
            st.error(f"Normalization failed: {e}")

    # Backfill timestamps (fix NULL and empty-string)
    if st.button("Backfill created_at/updated_at when missing"):
        try:
            now = datetime.utcnow().isoformat(timespec="seconds")
            with engine.begin() as conn:
                conn.execute(
                    sql_text(
                        """
                        UPDATE vendors
                           SET created_at = CASE WHEN created_at IS NULL OR created_at = '' THEN :now ELSE created_at END,
                               updated_at = CASE WHEN updated_at IS NULL OR updated_at = '' THEN :now ELSE updated_at END
                        """
                    ),
                    {"now": now},
                )
            st.success("Backfill complete.")
        except Exception as e:
            st.error(f"Backfill failed: {e}")

    # Trim extra whitespace across common text fields (preserves newlines in notes)
    if st.button("Trim whitespace in text fields (safe)"):
        try:
            changed = 0
            with engine.begin() as conn:
                rows = conn.execute(
                    sql_text(
                        """
                        SELECT id, category, service, business_name, contact_name, address, website, notes, keywords, phone
                        FROM vendors
                        """
                    )
                ).fetchall()

                def clean_soft(s: str | None) -> str:
                    s = (s or "").strip()
                    # collapse runs of spaces/tabs only; KEEP line breaks
                    s = re.sub(r"[ \t]+", " ", s)
                    return s

                for r in rows:
                    pid = int(r[0])
                    vals = {
                        "category": clean_soft(r[1]),
                        "service": clean_soft(r[2]),
                        "business_name": clean_soft(r[3]),
                        "contact_name": clean_soft(r[4]),
                        "address": clean_soft(r[5]),
                        "website": _sanitize_url(clean_soft(r[6])),
                        "notes": clean_soft(r[7]),  # preserves newlines
                        "keywords": clean_soft(r[8]),
                        "phone": r[9],  # leave phone unchanged here
                        "id": pid,
                    }
                    conn.execute(
                        sql_text(
                            """
                            UPDATE vendors
                               SET category=:category,
                                   service=NULLIF(:service,''),
                                   business_name=:business_name,
                                   contact_name=:contact_name,
                                   phone=:phone,
                                   address=:address,
                                   website=:website,
                                   notes=:notes,
                                   keywords=:keywords
                             WHERE id=:id
                            """
                        ),
                        vals,
                    )
                    changed += 1
            st.success(f"Whitespace trimmed on {changed} row(s).")
        except Exception as e:
            st.error(f"Trim failed: {e}")

# ---------- Debug
with _tabs[5]:
    st.subheader("Status & Secrets (debug)")
    st.json(engine_info)

    with engine.begin() as conn:
        vendors_cols = conn.execute(sql_text("PRAGMA table_info(vendors)")).fetchall()
        categories_cols = conn.execute(sql_text("PRAGMA table_info(categories)")).fetchall()
        services_cols = conn.execute(sql_text("PRAGMA table_info(services)")).fetchall()

        # --- Index presence (vendors) ---
        idx_rows = conn.execute(sql_text("PRAGMA index_list(vendors)")).fetchall()
        vendors_indexes = [
            {"seq": r[0], "name": r[1], "unique": bool(r[2]), "origin": r[3], "partial": bool(r[4])} for r in idx_rows
        ]

        # --- Null timestamp counts (quick sanity) ---
        created_at_nulls = conn.execute(
            sql_text("SELECT COUNT(*) FROM vendors WHERE created_at IS NULL OR created_at=''")
        ).scalar() or 0
        updated_at_nulls = conn.execute(
            sql_text("SELECT COUNT(*) FROM vendors WHERE updated_at IS NULL OR updated_at=''")
        ).scalar() or 0

        counts = {
            "vendors": conn.execute(sql_text("SELECT COUNT(*) FROM vendors")).scalar() or 0,
            "categories": conn.execute(sql_text("SELECT COUNT(*) FROM categories")).scalar() or 0,
            "services": conn.execute(sql_text("SELECT COUNT(*) FROM services")).scalar() or 0,
        }

    st.subheader("DB Probe")
    st.json(
        {
            "vendors_columns": [c[1] for c in vendors_cols],
            "categories_columns": [c[1] for c in categories_cols],
            "services_columns": [c[1] for c in services_cols],
            "counts": counts,
            "vendors_indexes": vendors_indexes,
            "timestamp_nulls": {"created_at": int(created_at_nulls), "updated_at": int(updated_at_nulls)},
        }
    )
