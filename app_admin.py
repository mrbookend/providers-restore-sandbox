# app_admin.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
HCR Providers — Admin (Option A: performance patches; full functionality retained)

Includes:
- Browse (SQL-side search + pagination, capped renders, CSV export)
- Add / Edit (optimistic concurrency via updated_at, data validation)
- CSV Restore (append-only with validation, logging)
- Category / Service Admin (safe rename/reassign/delete)
- Computed Keywords (CKW) system: seed store, lock/unlock, suggest, recompute (stale/all/override locks)
- Quick Probes & Integrity Self-Test
- Diagnostics (engine + schema), optional dependency banner
- Schema Bootstrap (guarded; use only when intended), ensure_schema idempotent
- Turso/libsql + embedded replica support; deterministic caching via DATA_VER

WARNING: Do NOT run Schema Bootstrap on production unless you intend to create tables.
"""

# ---- Streamlit page config MUST be first ----
import streamlit as st
st.set_page_config(
    page_title="HCR Providers — Admin",
    page_icon="🛠️",
    layout="wide",
    initial_sidebar_state="expanded",
)
# ---- No other st.* calls above this line ----

# ---- Stdlib ----
import os
import re
import sys
import csv
import io
import time
import json
import hmac
import uuid
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse, quote

# ---- Third-party ----
import pandas as pd
import sqlalchemy as sa
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.engine import Engine

# Register libsql dialect if available (non-fatal if missing for non-turso)
try:
    import sqlalchemy_libsql as sa_libsql  # type: ignore
    SA_LIBSQL_VER = getattr(sa_libsql, "__version__", "unknown")
except Exception:  # pragma: no cover
    sa_libsql = None
    SA_LIBSQL_VER = "not installed"

# ---- Optional: dependency banner (safe AFTER page_config) ----
if os.getenv("ADMIN_SHOW_STATUS", "0").strip() == "1":
    st.caption(
        "Deps — "
        f"py: {sys.version.split()[0]} | "
        f"streamlit: {st.__version__} | "
        f"sqlalchemy: {sa.__version__} | "
        f"sqlalchemy-libsql: {SA_LIBSQL_VER}"
    )

# =============================
# Configuration / Constants
# =============================
APP_VER = "admin-2025-10-18.4"
CURRENT_CKW_VER = "ckw-2025-10-16a"  # bump when generator changes
PAGE_SIZE = 200
MAX_RENDER_ROWS = 1000
DATA_VER = os.getenv("DATA_VER", "v1")  # bump to invalidate @st.cache_data

# =============================
# Utilities / Helpers
# =============================

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _s(x: Any) -> str:
    """Safe stringify, strip outer whitespace."""
    if x is None:
        return ""
    return str(x).strip()


def _digits_only(x: str | None) -> str:
    s = _s(x)
    return "".join(ch for ch in s if ch.isdigit())


def _fmt_phone(x: str | None) -> str:
    d = _digits_only(x)
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:10]}"
    return _s(x)


def _user_warn(msg: str) -> None:
    st.warning(msg, icon="⚠️")


def _user_info(msg: str) -> None:
    st.info(msg)


def _user_ok(msg: str) -> None:
    st.success(msg)


# =============================
# Secrets helpers
# =============================

def _get_secret(key: str, default: Any = None) -> Any:
    """Read from Streamlit secrets with ENV fallback. Call AFTER page_config."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.getenv(key, default)


def _bool_secret(key: str, default: bool = False) -> bool:
    v = str(_get_secret(key, "") or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


# =============================
# Engine builder (embedded replica + Turso)
# =============================

def _libsql_url_with_token(url: str, token: str | None) -> str:
    if not url:
        return url
    # If url already has authToken, keep it
    if "authToken=" in url:
        return url
    token = (token or "").strip()
    if not token:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}authToken={quote(token)}&tls=true"


def build_engine() -> tuple[Engine, str]:
    """Create and return SQLAlchemy engine and a human target description.
    Honor optional LIBSQL_URL_FULL; else TURSO_DATABASE_URL + TURSO_AUTH_TOKEN.
    Fallback to embedded replica DB if provided via EMBEDDED_DB_PATH.
    """
    # Prefer a single, explicit URL if provided in secrets
    full = str(_get_secret("LIBSQL_URL_FULL", "") or "").strip()
    if full.startswith("libsql://"):
        url = full
        host = urlparse(url).netloc
        dsn = f"sqlite+libsql:///?url={url}"
        return create_engine(dsn, pool_pre_ping=True, pool_recycle=300), f"turso:{host}"

    # Else assemble from separate pieces
    t_url = str(_get_secret("TURSO_DATABASE_URL", "") or "").strip()
    t_tok = str(_get_secret("TURSO_AUTH_TOKEN", "") or "").strip()
    if t_url.startswith("libsql://"):
        url = _libsql_url_with_token(t_url, t_tok)
        host = urlparse(url).netloc
        dsn = f"sqlite+libsql:///?url={url}"
        return create_engine(dsn, pool_pre_ping=True, pool_recycle=300), f"turso:{host}"

    # Fallback to embedded replica (local sqlite file)
    embedded = str(_get_secret("EMBEDDED_DB_PATH", "vendors-embedded.db") or "vendors-embedded.db")
    if not os.path.isabs(embedded):
        embedded = os.path.join(os.getcwd(), embedded)
    dsn = f"sqlite:///{embedded}"
    return create_engine(dsn, pool_pre_ping=True), f"embedded:{embedded}"


# =============================
# Engine accessor (cached)
# =============================
@st.cache_resource(show_spinner=False)
def get_engine_and_target() -> tuple[Engine, str]:
    # Now safe to touch secrets because page_config already ran.
    return build_engine()

# Provide globals for legacy code that expects ENGINE/TARGET_DESC at module scope.
ENGINE: Engine | None = None     # set in main()
TARGET_DESC: str | None = None   # set in main()


# =============================
# Schema ensure (idempotent)
# =============================

def ensure_schema(engine: Engine) -> None:
    """
    Idempotent schema ensure: create vendors/meta tables, CKW columns, indexes, etc.
    NOTE: This will NOT drop tables on purpose. It only creates what's missing and adds
    safe columns or indexes as needed. For production/MR data safety.
    """
    with engine.begin() as conn:
        # Core tables
        conn.execute(sql_text(
            """
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT
            )
            """
        ))
        conn.execute(sql_text(
            """
            CREATE TABLE IF NOT EXISTS vendors (
                id INTEGER PRIMARY KEY,
                business_name TEXT NOT NULL,
                category TEXT,
                service TEXT,
                contact_name TEXT,
                phone TEXT,
                email TEXT,
                website TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                zip TEXT,
                notes TEXT,
                created_at TEXT,
                updated_at TEXT,
                computed_keywords TEXT,
                ckw_locked INTEGER DEFAULT 0,
                ckw_version TEXT
            )
            """
        ))
        conn.execute(sql_text(
            """
            CREATE TABLE IF NOT EXISTS categories_lib (
                category TEXT PRIMARY KEY
            )
            """
        ))
        conn.execute(sql_text(
            """
            CREATE TABLE IF NOT EXISTS services_lib (
                service TEXT PRIMARY KEY
            )
            """
        ))
        conn.execute(sql_text(
            """
            CREATE TABLE IF NOT EXISTS ckw_seeds (
                category TEXT,
                service TEXT,
                seed TEXT,
                PRIMARY KEY (category, service)
            )
            """
        ))

        # Add columns if missing (idempotent)
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

        # Normalize existing rows so indexes/filters behave predictably
        conn.execute(sql_text(
            """
            UPDATE vendors
               SET ckw_locked = IFNULL(ckw_locked, 0),
                   ckw_version = ckw_version,
                   computed_keywords = computed_keywords
             WHERE 1=1
            """
        ))

        # Helpful indexes
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_vendors_cat ON vendors(category)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_vendors_svc ON vendors(service)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_vendors_ckw ON vendors(ckw_locked, ckw_version)"))
        conn.execute(sql_text("CREATE INDEX IF NOT EXISTS idx_vendors_updated ON vendors(updated_at)"))


# =============================
# CKW generation / seeds
# =============================

@st.cache_data(show_spinner=False)
def _load_ckw_seed(engine: Engine, category: str, service: str) -> str:
    with engine.connect() as cx:
        row = cx.execute(sql_text(
            "SELECT seed FROM ckw_seeds WHERE category=:c AND service=:s"
        ), {"c": category, "s": service}).fetchone()
        return row[0] if row else ""


def _ckw_suggest(engine: Engine, category: str, service: str, business_name: str) -> str:
    seed = _load_ckw_seed(engine, _s(category), _s(service))
    parts = [seed, _s(category), _s(service), _s(business_name)]
    # Deduplicate tokens (very simple tokenizer)
    toks: list[str] = []
    for p in parts:
        for t in re.split(r"[^a-z0-9+]+", p.strip().lower()):
            if t and t not in toks:
                toks.append(t)
    return " ".join(toks)


def _ckw_stats(engine: Engine, ver: str) -> dict[str, int]:
    with engine.connect() as cx:
        total = cx.execute(sql_text("SELECT COUNT(*) FROM vendors")).scalar() or 0
        locked = cx.execute(sql_text("SELECT COUNT(*) FROM vendors WHERE IFNULL(ckw_locked,0)=1")).scalar() or 0
        stale = cx.execute(sql_text("""
            SELECT COUNT(*) FROM vendors
             WHERE IFNULL(ckw_locked,0)=0 AND (
                   ckw_version IS NULL OR ckw_version <> :ver OR
                   computed_keywords IS NULL OR TRIM(computed_keywords)=''
             )
        """), {"ver": ver}).scalar() or 0
    return {"total": int(total), "locked": int(locked), "stale": int(stale)}


def _rows_for_stale(engine: Engine, ver: str) -> list[tuple]:
    with engine.connect() as cx:
        return cx.execute(sql_text(
            """
            SELECT id, category, service, business_name
              FROM vendors
             WHERE IFNULL(ckw_locked,0)=0 AND (
                   ckw_version IS NULL OR ckw_version <> :ver OR
                   computed_keywords IS NULL OR TRIM(computed_keywords)=''
             )
            """
        ), {"ver": ver}).fetchall()


def _rows_for_all_unlocked(engine: Engine) -> list[tuple]:
    with engine.connect() as cx:
        return cx.execute(sql_text(
            """
            SELECT id, category, service, business_name
              FROM vendors
             WHERE IFNULL(ckw_locked,0)=0
            """
        )).fetchall()


def _ckw_update_batch(engine: Engine, rows: list[tuple], ver: str) -> int:
    """Compute and update keywords for a list of (id, category, service, business_name)."""
    if not rows:
        return 0
    count = 0
    with engine.begin() as cx:
        for rid, cat, svc, biz in rows:
            kw = _ckw_suggest(engine, cat or "", svc or "", biz or "")
            cx.execute(sql_text(
                """
                UPDATE vendors
                   SET computed_keywords=:kw, ckw_version=:ver
                 WHERE id=:id
                """
            ), {"kw": kw, "ver": ver, "id": rid})
            count += 1
    return count


# =============================
# Data access helpers / loaders
# =============================

@st.cache_data(show_spinner=False)
def load_df(engine: Engine, data_ver: str) -> pd.DataFrame:
    # SQL-side minimal load (no WHERE); apply WHERE later for pagination/search
    with engine.connect() as cx:
        rows = cx.execute(sql_text(
            """
            SELECT id, business_name, category, service, contact_name,
                   phone, email, website, address, city, state, zip,
                   notes, created_at, updated_at, computed_keywords,
                   IFNULL(ckw_locked,0) AS ckw_locked, ckw_version
              FROM vendors
            """
        )).fetchall()
    cols = [
        "id","business_name","category","service","contact_name",
        "phone","email","website","address","city","state","zip",
        "notes","created_at","updated_at","computed_keywords",
        "ckw_locked","ckw_version",
    ]
    df = pd.DataFrame(rows, columns=cols)
    # Build a search blob for quick client-side fallback filtering
    def _mk_blob(r: pd.Series) -> str:
        parts = [
            r.get("business_name",""), r.get("category",""), r.get("service",""),
            r.get("contact_name",""), r.get("phone",""), r.get("email",""),
            r.get("website",""), r.get("address",""), r.get("city",""),
            r.get("state",""), r.get("zip",""), r.get("notes",""),
            r.get("computed_keywords",""),
        ]
        return " ".join(_s(x).lower() for x in parts if _s(x))
    if not df.empty:
        df["_blob"] = df.apply(_mk_blob, axis=1)
    else:
        df["_blob"] = ""
    return df


# =============================
# Validation helpers
# =============================

def _validate_basic_vendor(d: dict[str, Any]) -> tuple[bool, list[str]]:
    errs: list[str] = []
    if not _s(d.get("business_name")):
        errs.append("Business name is required")
    # Block multiple services in one string (comma / slash)
    svc = _s(d.get("service"))
    if "," in svc or "/" in svc:
        errs.append("Service must be a single value (no commas/slashes)")
    # Phone check (if provided)
    ph = _digits_only(d.get("phone"))
    if ph and len(ph) != 10:
        errs.append("Phone must be 10 digits or left blank")
    return len(errs) == 0, errs


# =============================
# Browse Tab (SQL-side search + pagination, capped render)
# =============================

def _search_where_clause(q: str) -> tuple[str, dict[str, Any]]:
    q = _s(q).lower()
    if not q:
        return "", {}
    # Minimal WHERE using LIKE on a few columns; heavy search can use _blob client-side
    where = textwrap.dedent(
        """
        WHERE (
              lower(business_name) LIKE :qq OR
              lower(category)      LIKE :qq OR
              lower(service)       LIKE :qq OR
              lower(city)          LIKE :qq OR
              lower(state)         LIKE :qq OR
              lower(computed_keywords) LIKE :qq
        )
        """
    ).strip()
    return where, {"qq": f"%{q}%"}


def _sql_count(engine: Engine, q: str) -> int:
    where, params = _search_where_clause(q)
    sql = "SELECT COUNT(*) FROM vendors " + (where or "")
    with engine.connect() as cx:
        return int(cx.execute(sql_text(sql), params).scalar() or 0)


def _sql_page(engine: Engine, q: str, offset: int, limit: int) -> pd.DataFrame:
    where, params = _search_where_clause(q)
    sql = (
        "SELECT id,business_name,category,service,contact_name,phone,email,website,"
        "address,city,state,zip,notes,created_at,updated_at,computed_keywords,"
        "IFNULL(ckw_locked,0) AS ckw_locked, ckw_version FROM vendors "
        + (where + " " if where else "")
        + "ORDER BY business_name COLLATE NOCASE ASC LIMIT :lim OFFSET :off"
    )
    params = dict(params)
    params.update({"lim": limit, "off": offset})
    with engine.connect() as cx:
        rows = cx.execute(sql_text(sql), params).fetchall()
    cols = [
        "id","business_name","category","service","contact_name",
        "phone","email","website","address","city","state","zip",
        "notes","created_at","updated_at","computed_keywords",
        "ckw_locked","ckw_version",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        # optional local format adjustments
        df["phone"] = df["phone"].map(_fmt_phone)
    return df


def tab_browse(engine: Engine) -> None:
    st.subheader("Browse Providers")

    # Session guards
    if "q" not in st.session_state:
        st.session_state["q"] = ""
    if "page" not in st.session_state:
        st.session_state["page"] = 0

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        q = st.text_input("Search", value=st.session_state["q"], placeholder="e.g., roofer, irrigation, Bosch…")
    with c2:
        if st.button("Clear"):
            q = ""
    with c3:
        page_size = st.number_input("Rows / page", min_value=50, max_value=1000, value=PAGE_SIZE, step=50)

    # Sync session
    if q != st.session_state["q"]:
        st.session_state["q"] = q
        st.session_state["page"] = 0

    # Query count & page
    total = _sql_count(engine, q)
    page = int(st.session_state["page"])
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    st.session_state["page"] = page

    offset = page * page_size
    vdf = _sql_page(engine, q, offset, page_size)

    st.caption(f"Matches: {total} · Page {page+1}/{page_count} · Showing up to {len(vdf)} rows")
    if vdf.empty:
        _user_info("No matching providers. Tip: try fewer words.")
        return

    # Cap render for safety
    render_df = vdf.head(MAX_RENDER_ROWS).copy()
    st.dataframe(render_df, use_container_width=True)

    c_prev, c_next, c_dl = st.columns([1, 1, 2])
    with c_prev:
        if st.button("◀ Prev", disabled=page <= 0):
            st.session_state["page"] = max(0, page - 1)
            st.experimental_rerun()
    with c_next:
        if st.button("Next ▶", disabled=page >= page_count - 1):
            st.session_state["page"] = min(page_count - 1, page + 1)
            st.experimental_rerun()
    with c_dl:
        # CSV download of current page
        csv_buf = io.StringIO()
        render_df.to_csv(csv_buf, index=False)
        st.download_button("Download this page (CSV)", data=csv_buf.getvalue(), file_name="providers_page.csv", mime="text/csv")


# =============================
# Add / Edit Tabs
# =============================

def _insert_vendor(engine: Engine, d: dict[str, Any]) -> int:
    ok, errs = _validate_basic_vendor(d)
    if not ok:
        raise ValueError("; ".join(errs))
    d = {k: _s(v) for k, v in d.items()}
    d["phone"] = _digits_only(d.get("phone"))
    now = _now_iso()
    with engine.begin() as cx:
        r = cx.execute(sql_text(
            """
            INSERT INTO vendors (
                business_name, category, service, contact_name, phone,
                email, website, address, city, state, zip, notes,
                created_at, updated_at, computed_keywords, ckw_locked, ckw_version
            ) VALUES (
                :business_name, :category, :service, :contact_name, :phone,
                :email, :website, :address, :city, :state, :zip, :notes,
                :now, :now, :ckw, :ckw_locked, :ckw_version
            )
            """
        ), {
            **d,
            "now": now,
            "ckw": _ckw_suggest(engine, d.get("category",""), d.get("service",""), d.get("business_name","")),
            "ckw_locked": 0,
            "ckw_version": CURRENT_CKW_VER,
        })
        new_id = r.lastrowid if hasattr(r, "lastrowid") else cx.execute(sql_text("SELECT last_insert_rowid()")).scalar()
    return int(new_id or 0)


def _update_vendor(engine: Engine, vid: int, d: dict[str, Any], prev_updated: str | None) -> bool:
    ok, errs = _validate_basic_vendor(d)
    if not ok:
        raise ValueError("; ".join(errs))
    d = {k: _s(v) for k, v in d.items()}
    d["phone"] = _digits_only(d.get("phone"))
    now = _now_iso()
    # optimistic concurrency on updated_at
    with engine.begin() as cx:
        params = {
            **d,
            "id": vid,
            "now": now,
            "prev": prev_updated or "",
            "ckw": _ckw_suggest(engine, d.get("category",""), d.get("service",""), d.get("business_name","")),
            "ckw_version": CURRENT_CKW_VER,
        }
        r = cx.execute(sql_text(textwrap.dedent(
            """
            UPDATE vendors
               SET business_name = :business_name,
                   category      = NULLIF(:category,''),
                   service       = NULLIF(:service,''),
                   contact_name  = NULLIF(:contact_name,''),
                   phone         = NULLIF(:phone,''),
                   email         = NULLIF(:email,''),
                   website       = NULLIF(:website,''),
                   address       = NULLIF(:address,''),
                   city          = NULLIF(:city,''),
                   state         = NULLIF(:state,''),
                   zip           = NULLIF(:zip,''),
                   notes         = NULLIF(:notes,''),
                   updated_at    = :now,
                   computed_keywords = CASE WHEN IFNULL(ckw_locked,0)=1 THEN computed_keywords ELSE :ckw END,
                   ckw_version   = CASE WHEN IFNULL(ckw_locked,0)=1 THEN ckw_version ELSE :ckw_version END
             WHERE id=:id AND COALESCE(updated_at,'') = COALESCE(:prev,'')
            """
        )) , params)
        return r.rowcount > 0


def tab_add(engine: Engine) -> None:
    st.subheader("Add Provider")
    with st.form("add_form"):
        business_name = st.text_input("Business name")
        category = st.text_input("Category")
        service = st.text_input("Service")
        contact_name = st.text_input("Contact name")
        phone = st.text_input("Phone")
        email = st.text_input("Email")
        website = st.text_input("Website")
        address = st.text_input("Address")
        city = st.text_input("City")
        state = st.text_input("State", value="TX")
        zipc = st.text_input("ZIP")
        notes = st.text_area("Notes")
        remember_seed = st.checkbox("Remember these keywords as the seed for this (category, service)")
        submitted = st.form_submit_button("Add")
    if submitted:
        d = dict(business_name=business_name, category=category, service=service,
                 contact_name=contact_name, phone=phone, email=email, website=website,
                 address=address, city=city, state=state, zip=zipc, notes=notes)
        try:
            new_id = _insert_vendor(engine, d)
            if remember_seed:
                seed = _ckw_suggest(engine, category, service, business_name)
                with engine.begin() as cx:
                    cx.execute(sql_text(
                        "INSERT INTO ckw_seeds(category,service,seed) VALUES(:c,:s,:seed)"
                        " ON CONFLICT(category,service) DO UPDATE SET seed=excluded.seed"
                    ), {"c": _s(category), "s": _s(service), "seed": seed})
            _user_ok(f"Added provider #{new_id}")
        except Exception as e:
            _user_warn(f"Add failed: {e}")


def _load_vendor(engine: Engine, vid: int) -> Optional[dict[str, Any]]:
    with engine.connect() as cx:
        r = cx.execute(sql_text("SELECT * FROM vendors WHERE id=:id"), {"id": vid}).mappings().fetchone()
        return dict(r) if r else None


def tab_edit(engine: Engine) -> None:
    st.subheader("Edit Provider")
    vid = st.number_input("Provider ID", min_value=1, step=1)
    if st.button("Load"):
        st.session_state["edit_vendor_id"] = int(vid)
    eid = int(st.session_state.get("edit_vendor_id") or 0)
    if not eid:
        st.caption("Enter an ID and click Load.")
        return
    row = _load_vendor(engine, eid)
    if not row:
        _user_warn("Not found.")
        return

    with st.form("edit_form"):
        business_name = st.text_input("Business name", value=row.get("business_name",""))
        category = st.text_input("Category", value=row.get("category",""))
        service = st.text_input("Service", value=row.get("service",""))
        contact_name = st.text_input("Contact name", value=row.get("contact_name",""))
        phone = st.text_input("Phone", value=_fmt_phone(row.get("phone","")))
        email = st.text_input("Email", value=row.get("email",""))
        website = st.text_input("Website", value=row.get("website",""))
        address = st.text_input("Address", value=row.get("address",""))
        city = st.text_input("City", value=row.get("city",""))
        state = st.text_input("State", value=row.get("state","TX"))
        zipc = st.text_input("ZIP", value=row.get("zip",""))
        notes = st.text_area("Notes", value=row.get("notes",""))
        ckw_locked = st.checkbox("Lock computed keywords", value=bool(row.get("ckw_locked") or 0))
        computed_keywords = st.text_area("Computed keywords (editable only if unlocked)", value=row.get("computed_keywords",""), disabled=ckw_locked)
        submitted = st.form_submit_button("Save")
    if submitted:
        d = dict(business_name=business_name, category=category, service=service,
                 contact_name=contact_name, phone=phone, email=email, website=website,
                 address=address, city=city, state=state, zip=zipc, notes=notes)
        try:
            ok = _update_vendor(engine, eid, d, row.get("updated_at"))
            if not ok:
                _user_warn("Save failed — this record was modified by someone else. Reload and try again.")
                return
            with engine.begin() as cx:
                cx.execute(sql_text("UPDATE vendors SET ckw_locked=:l, computed_keywords=CASE WHEN :l=1 THEN computed_keywords ELSE :kw END WHERE id=:id"),
                           {"l": 1 if ckw_locked else 0, "kw": computed_keywords, "id": eid})
            _user_ok("Saved.")
        except Exception as e:
            _user_warn(f"Save failed: {e}")


# =============================
# CSV Restore (append-only)
# =============================

def _csv_restore_append(engine: Engine, file: io.BytesIO) -> tuple[int, list[str]]:
    """Append-only CSV restore. Validates headers; rejects dangerous columns; logs actions."""
    added = 0
    logs: list[str] = []
    df = pd.read_csv(file)
    allowed = {"business_name","category","service","contact_name","phone","email","website","address","city","state","zip","notes"}
    forbidden = {"id","created_at","updated_at","computed_keywords","ckw_locked","ckw_version"}
    cols = set(df.columns)
    if cols & forbidden:
        raise ValueError(f"Forbidden columns present: {sorted(cols & forbidden)}")
    unknown = cols - allowed
    if unknown:
        logs.append(f"Ignoring unknown columns: {sorted(unknown)}")
    keep_cols = [c for c in df.columns if c in allowed]
    df = df[keep_cols].copy()
    df = df.fillna("")

    for _, r in df.iterrows():
        d = {k: _s(r.get(k,"")) for k in allowed}
        try:
            _insert_vendor(engine, d)
            added += 1
        except Exception as e:
            logs.append(f"Row skipped: {e}")
    return added, logs


def tab_csv_restore(engine: Engine) -> None:
    st.subheader("CSV Restore (Append-Only)")
    up = st.file_uploader("Upload CSV", type=["csv"])
    if up is not None:
        try:
            added, logs = _csv_restore_append(engine, up)
            _user_ok(f"Appended {added} rows.")
            if logs:
                with st.expander("Details"):
                    for line in logs:
                        st.text(line)
        except Exception as e:
            _user_warn(f"Restore failed: {e}")


# =============================
# Category / Service Admin
# =============================

def _rename_category(engine: Engine, old: str, new: str) -> None:
    with engine.begin() as cx:
        cx.execute(sql_text("INSERT OR IGNORE INTO categories_lib(category) VALUES(:c)"), {"c": new})
        cx.execute(sql_text("UPDATE vendors SET category=:n WHERE category=:o"), {"n": new, "o": old})
        cx.execute(sql_text("DELETE FROM categories_lib WHERE category=:o"), {"o": old})


def _rename_service(engine: Engine, old: str, new: str) -> None:
    with engine.begin() as cx:
        cx.execute(sql_text("INSERT OR IGNORE INTO services_lib(service) VALUES(:s)"), {"s": new})
        cx.execute(sql_text("UPDATE vendors SET service=:n WHERE service=:o"), {"n": new, "o": old})
        cx.execute(sql_text("DELETE FROM services_lib WHERE service=:o"), {"o": old})


def tab_category_service_admin(engine: Engine) -> None:
    st.subheader("Category & Service Admin")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Rename Category**")
        oc = st.text_input("Old category")
        nc = st.text_input("New category")
        if st.button("Rename Category"):
            try:
                _rename_category(engine, _s(oc), _s(nc))
                _user_ok("Category renamed.")
            except Exception as e:
                _user_warn(f"Rename failed: {e}")
    with c2:
        st.markdown("**Rename Service**")
        osvc = st.text_input("Old service")
        nsvc = st.text_input("New service")
        if st.button("Rename Service"):
            try:
                _rename_service(engine, _s(osvc), _s(nsvc))
                _user_ok("Service renamed.")
            except Exception as e:
                _user_warn(f"Rename failed: {e}")


# =============================
# Maintenance — CKW recompute, Seeds coverage, Integrity
# =============================

def tab_maintenance(engine: Engine) -> None:
    st.subheader("Maintenance")

    # CKW stats and actions
    stats = _ckw_stats(engine, CURRENT_CKW_VER)
    st.caption(f"CKW — total: {stats['total']}, locked: {stats['locked']}, stale: {stats['stale']}")

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Recompute CKW (stale only)"):
            rows = _rows_for_stale(engine, CURRENT_CKW_VER)
            n = _ckw_update_batch(engine, rows, CURRENT_CKW_VER)
            _user_ok(f"Recomputed {n} rows.")
    with c2:
        if st.button("Force Recompute CKW (all UNLOCKED)"):
            rows = _rows_for_all_unlocked(engine)
            n = _ckw_update_batch(engine, rows, CURRENT_CKW_VER)
            _user_ok(f"Recomputed {n} rows.")
    with c3:
        if st.button("Override Locks (ALL)"):
            with engine.begin() as cx:
                cx.execute(sql_text("UPDATE vendors SET ckw_locked=0"))
            rows = _rows_for_all_unlocked(engine)
            n = _ckw_update_batch(engine, rows, CURRENT_CKW_VER)
            _user_ok(f"Recomputed {n} rows with locks removed.")

    # Seeds coverage probe
    with st.expander("CKW Seed Coverage"):
        with engine.connect() as cx:
            combos = cx.execute(sql_text(textwrap.dedent(
                """
                SELECT category, service, COUNT(*) AS n
                  FROM vendors
                 GROUP BY category, service
                 ORDER BY category, service
                """
            ))).fetchall()
            seeds = cx.execute(sql_text("SELECT category, service FROM ckw_seeds")).fetchall()
        seed_set = {(c[0] or "", c[1] or "") for c in seeds}
        rows: list[dict[str, Any]] = []
        for c in combos:
            key = (_s(c[0]), _s(c[1]))
            rows.append({"category": key[0], "service": key[1], "has_seed": key in seed_set, "count": int(c[2])})
        sdf = pd.DataFrame(rows)
        st.dataframe(sdf, use_container_width=True)

    # Integrity quick probe
    with st.expander("Integrity Self-Test"):
        msgs: list[str] = []
        ok = True
        with engine.connect() as cx:
            # presence
            t = cx.execute(sql_text("SELECT name FROM sqlite_master WHERE type='table' AND name in ('vendors','meta')")).fetchall()
            have = {r[0] for r in t}
            if "vendors" not in have:
                ok = False; msgs.append("Missing vendors table")
            if "meta" not in have:
                ok = False; msgs.append("Missing meta table")
            # check columns
            if ok:
                cols = {r[1] for r in cx.execute(sql_text("PRAGMA table_info(vendors)")).fetchall()}
                for need in ["computed_keywords","ckw_locked","ckw_version","updated_at"]:
                    if need not in cols:
                        ok = False; msgs.append(f"Missing column: {need}")
        if ok:
            _user_ok("Integrity OK")
        else:
            _user_warn("\n".join(msgs))


# =============================
# Diagnostics — engine + schema
# =============================

def tab_diagnostics(engine: Engine, target_desc: str) -> None:
    st.subheader("Diagnostics")
    st.caption(f"Target: {target_desc}")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Engine Info**")
        st.text(f"sqlalchemy: {sa.__version__}")
        st.text(f"sqlalchemy-libsql: {SA_LIBSQL_VER}")
        st.text(f"py: {sys.version}")
    with c2:
        st.markdown("**Tables**")
        with engine.connect() as cx:
            t = cx.execute(sql_text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
            names = [r[0] for r in t.fetchall()]
        st.code("\n".join(names) or "<none>")


# =============================
# Guarded Schema Bootstrap (OFF by default)
# =============================

def _bootstrap_schema(engine: Engine) -> None:
    """Run one-time schema bootstrap. Use only on an empty DB."""

    with engine.begin() as cx:
        cx.execute(sql_text("DELETE FROM sqlite_sequence"))  # noop on libsql
        # We rely on ensure_schema which is idempotent; this is a placeholder.
        pass


def tab_schema_bootstrap(engine: Engine) -> None:
    st.subheader("Schema Bootstrap (Guarded)")
    allow = str(_get_secret("ADMIN_ALLOW_SCHEMA_BOOTSTRAP", "0") or "0").strip() == "1"
    if not allow:
        _user_warn("Bootstrap disabled. Set ADMIN_ALLOW_SCHEMA_BOOTSTRAP=1 in secrets to enable (ONLY for empty DB).")
        return
    st.caption("This will create required tables/indexes if missing. Use on EMPTY databases only.")
    if st.button("Run Bootstrap Now"):
        try:
            ensure_schema(engine)
            _bootstrap_schema(engine)
            _user_ok("Bootstrap completed.")
        except Exception as e:
            _user_warn(f"Bootstrap failed: {e}")


# =============================
# App UI / Tabs
# =============================

def render_app(engine: Engine, target_desc: str) -> None:
    st.markdown(f"### HCR Providers — Admin  \n<small>{APP_VER}</small>", unsafe_allow_html=True)

    tabs = st.tabs([
        "Browse", "Add", "Edit", "CSV Restore", "Category/Service Admin",
        "Maintenance", "Diagnostics", "Schema Bootstrap",
    ])

    with tabs[0]:
        tab_browse(engine)
    with tabs[1]:
        tab_add(engine)
    with tabs[2]:
        tab_edit(engine)
    with tabs[3]:
        tab_csv_restore(engine)
    with tabs[4]:
        tab_category_service_admin(engine)
    with tabs[5]:
        tab_maintenance(engine)
    with tabs[6]:
        tab_diagnostics(engine, target_desc)
    with tabs[7]:
        tab_schema_bootstrap(engine)


# =============================
# main()
# =============================

def main() -> None:
    global ENGINE, TARGET_DESC

    # Create engine lazily and cache it
    ENGINE, TARGET_DESC = get_engine_and_target()

    # Optional non-PII status crumb
    if _bool_secret("SHOW_STATUS", False):
        st.caption(f"DB target: {TARGET_DESC}")

    # Ensure schema idempotently (safe; should not drop data)
    try:
        ensure_schema(ENGINE)
    except Exception as e:
        allow_bootstrap = str(_get_secret("ADMIN_ALLOW_SCHEMA_BOOTSTRAP", "0") or "0").strip() == "1"
        if allow_bootstrap:
            try:
                ensure_schema(ENGINE)
                _bootstrap_schema(ENGINE)
            except Exception as ee:
                st.error(f"Schema ensure/bootstrap failed: {ee}")
                st.stop()
        else:
            st.error("Schema missing or invalid. Bootstrap is disabled (ADMIN_ALLOW_SCHEMA_BOOTSTRAP=0).")
            st.stop()

    # Hand off to the full UI
    render_app(ENGINE, TARGET_DESC or "<unknown>")


# Entry point
if __name__ == "__main__":
    main()
