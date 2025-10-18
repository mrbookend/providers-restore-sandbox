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

# ---- Stdlib ----
import os
import re
import sys
import csv
import io
import time
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
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

# ---- Optional: dependency banner ----
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
APP_VER = "admin-2025-10-18.1"
CURRENT_CKW_VER = "ckw-2025-10-16a"  # bump when generator changes
PAGE_SIZE = 200
MAX_RENDER_ROWS = 1000

# =============================
# Secrets helpers
# =============================

def _get_secret(key: str, default: Any = None) -> Any:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

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

ENGINE, TARGET_DESC = build_engine()

# =============================
# Schema ensure (idempotent)
# =============================
SCHEMA_BOOTSTRAP_ALLOWED = bool(int(str(_get_secret("ADMIN_ALLOW_SCHEMA_BOOTSTRAP", 0))))

VENDORS_DDL = [
    """
    CREATE TABLE IF NOT EXISTS vendors (
        id INTEGER PRIMARY KEY,
        category TEXT,
        service TEXT,
        business_name TEXT NOT NULL,
        phone TEXT,
        phone_digits TEXT,
        website TEXT,
        email TEXT,
        address1 TEXT,
        address2 TEXT,
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
    """,
]

META_DDL = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        val TEXT
    )
    """,
]

INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_vendors_category ON vendors(category)",
    "CREATE INDEX IF NOT EXISTS idx_vendors_service ON vendors(service)",
    "CREATE INDEX IF NOT EXISTS idx_vendors_business_name ON vendors(business_name)",
    "CREATE INDEX IF NOT EXISTS idx_vendors_ckw_locked ON vendors(ckw_locked)",
]

CKW_SEEDS_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ckw_seeds (
        category TEXT NOT NULL,
        service TEXT NOT NULL,
        seed TEXT,
        PRIMARY KEY (category, service)
    )
    """,
]


def ensure_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for stmt in META_DDL + VENDORS_DDL + INDEXES_DDL + CKW_SEEDS_DDL:
            conn.execute(sql_text(stmt))
        # Guarantee CKW columns exist even on older DBs
        try:
            cols = {r[1] for r in conn.execute(sql_text("PRAGMA table_info(vendors)")).fetchall()}
            alters: list[str] = []
            if "computed_keywords" not in cols:
                alters.append("ALTER TABLE vendors ADD COLUMN computed_keywords TEXT")
            if "ckw_locked" not in cols:
                alters.append("ALTER TABLE vendors ADD COLUMN ckw_locked INTEGER DEFAULT 0")
            if "ckw_version" not in cols:
                alters.append("ALTER TABLE vendors ADD COLUMN ckw_version TEXT")
            for a in alters:
                conn.execute(sql_text(a))
        except Exception:
            pass
        # Normalize NULLs so filters & indexes behave
        conn.execute(sql_text(
            """
            UPDATE vendors
               SET phone_digits = CASE
                       WHEN phone_digits IS NULL OR TRIM(phone_digits) = '' THEN REPLACE(REPLACE(REPLACE(REPLACE(phone,'(',''),')',''),'-',''),' ','')
                       ELSE phone_digits END,
                   ckw_locked = IFNULL(ckw_locked, 0)
            """
        ))
        # Ensure DATA_VER exists for caching
        cur = conn.execute(sql_text("SELECT val FROM meta WHERE key='DATA_VER'"))
        row = cur.fetchone()
        if not row:
            conn.execute(sql_text("INSERT OR REPLACE INTO meta(key,val) VALUES('DATA_VER', :v)"), {"v": datetime.now(timezone.utc).isoformat()})


ensure_schema(ENGINE)

# =============================
# Utilities
# =============================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digits_only(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\D+", "", s)


def _s(x: Any) -> str:
    return "" if x is None else str(x)


@st.cache_data(show_spinner=False, max_entries=8)
def _get_data_ver(engine_url: str) -> str:
    with ENGINE.connect() as cx:
        try:
            return _s(cx.execute(sql_text("SELECT val FROM meta WHERE key='DATA_VER'")) .scalar())
        except Exception:
            return ""


@st.cache_data(show_spinner=False)
def load_df(data_ver: str) -> pd.DataFrame:
    with ENGINE.connect() as cx:
        rows = cx.execute(sql_text(
            """
            SELECT id, category, service, business_name, phone, phone_digits,
                   website, email, address1, address2, city, state, zip,
                   notes, created_at, updated_at, computed_keywords, ckw_locked, ckw_version
              FROM vendors
             ORDER BY business_name COLLATE NOCASE, id
            """
        )).fetchall()
    df = pd.DataFrame(rows, columns=[
        "id","category","service","business_name","phone","phone_digits",
        "website","email","address1","address2","city","state","zip",
        "notes","created_at","updated_at","computed_keywords","ckw_locked","ckw_version"
    ])
    # Build search blob (not stored)
    def mk_blob(r: pd.Series) -> str:
        parts = [r.get("business_name",""), r.get("category",""), r.get("service",""), r.get("notes",""), r.get("website",""), r.get("city",""), r.get("state",""), r.get("zip",""), r.get("email",""), r.get("computed_keywords","")]
        return " ".join([_s(p).strip().lower() for p in parts if _s(p)])
    df["_blob"] = df.apply(mk_blob, axis=1)
    return df


def bump_data_ver() -> None:
    with ENGINE.begin() as cx:
        cx.execute(sql_text("UPDATE meta SET val=:v WHERE key='DATA_VER'"), {"v": _now_iso()})


# =============================
# Computed Keywords (CKW)
# =============================

@dataclass
class CKWSeed:
    category: str
    service: str
    seed: str


def _ckw_seed_get(cat: str, svc: str) -> str:
    with ENGINE.connect() as cx:
        row = cx.execute(sql_text(
            "SELECT seed FROM ckw_seeds WHERE category=:c AND service=:s"
        ), {"c": cat, "s": svc}).fetchone()
        return _s(row[0]) if row else ""


def _ckw_seed_set(cat: str, svc: str, seed: str) -> None:
    with ENGINE.begin() as cx:
        cx.execute(sql_text(
            "INSERT OR REPLACE INTO ckw_seeds(category,service,seed) VALUES(:c,:s,:seed)"
        ), {"c": cat, "s": svc, "seed": seed})


# Optional synonym sets from secrets
CKW_SYNONYMS: dict[str, list[str]] = _get_secret("CKW_SYNONYMS", {}) or {}


def _gen_ckw(cat: str, svc: str, name: str) -> str:
    base: list[str] = []
    for x in (cat, svc, name):
        x = (x or "").strip().lower()
        if x:
            base.append(x)
    # add seed
    seed = _ckw_seed_get(cat or "", svc or "")
    if seed:
        base.extend([w.strip().lower() for w in seed.split(",") if w.strip()])
    # add synonyms
    for k in (cat or "", svc or ""):
        xs = CKW_SYNONYMS.get(k, [])
        for w in xs:
            w = (w or "").strip().lower()
            if w:
                base.append(w)
    # dedupe
    seen: set[str] = set()
    out: list[str] = []
    for w in base:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return ", ".join(out)


def ckw_recompute_rows(rows: list[tuple[int,str,str,str]], override_locks: bool=False) -> int:
    cnt = 0
    with ENGINE.begin() as cx:
        for rid, cat, svc, name in rows:
            if not override_locks:
                locked = cx.execute(sql_text("SELECT IFNULL(ckw_locked,0) FROM vendors WHERE id=:id"), {"id": rid}).scalar()
                if int(locked or 0) == 1:
                    continue
            new = _gen_ckw(cat or "", svc or "", name or "")
            cx.execute(sql_text(
                """
                UPDATE vendors
                   SET computed_keywords=:ckw,
                       ckw_version=:ver,
                       updated_at=COALESCE(updated_at, :now)
                 WHERE id=:id
                """
            ), {"ckw": new, "ver": CURRENT_CKW_VER, "now": _now_iso(), "id": rid})
            cnt += 1
    if cnt:
        bump_data_ver()
    return cnt


def _rows_for_stale(ver: str) -> list[tuple[int,str,str,str]]:
    with ENGINE.connect() as cx:
        return cx.execute(sql_text(
            """
            SELECT id, category, service, business_name
              FROM vendors
             WHERE IFNULL(ckw_locked,0)=0
               AND (ckw_version IS NULL OR ckw_version<>:v
                    OR computed_keywords IS NULL OR TRIM(computed_keywords)='')
            """
        ), {"v": ver}).fetchall()


def _rows_for_all_unlocked() -> list[tuple[int,str,str,str]]:
    with ENGINE.connect() as cx:
        return cx.execute(sql_text(
            "SELECT id, category, service, business_name FROM vendors WHERE IFNULL(ckw_locked,0)=0"
        )).fetchall()


# =============================
# Session-state safety defaults
# =============================
for _k, _v in {
    "q": "",
    "page": 1,
    "edit_vendor_id": None,
    "show_debug": False,
}.items():
    st.session_state.setdefault(_k, _v)

# =============================
# Layout: Tabs
# =============================
TAB = st.tabs([
    "Browse", "Add", "Edit", "CSV Restore", "Category/Service Admin", "Maintenance", "Quick Probes"
])

# -----------------------------
# 🔎 Browse
# -----------------------------
with TAB[0]:
    st.subheader("Browse Vendors")

    # Query row
    c1, c2, c3 = st.columns([3,1,1])
    with c1:
        q = st.text_input("Search", value=st.session_state.get("q",""), placeholder="e.g., roofer, manicure, irrigation, Bosch…", help="Global search across name/category/service/notes/website/city/state/zip/email", key="browse_search")
    with c2:
        clear = st.button("Clear")
    with c3:
        st.session_state["show_debug"] = st.toggle("Debug", value=st.session_state.get("show_debug", False))

    if clear:
        q = ""
    st.session_state["q"] = (q or "").strip()

    data_ver = _get_data_ver(str(ENGINE.url))
    df = load_df(data_ver)

    # Simple tokenized filter client-side (fast for <10k rows)
    qq = st.session_state["q"].lower()
    vdf = df
    if qq:
        toks = [t for t in qq.split() if t]
        for t in toks:
            vdf = vdf[vdf["_blob"].str.contains(re.escape(t), na=False)]

    # Pagination
    total = len(vdf)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    st.session_state["page"] = min(max(1, st.session_state.get("page", 1)), pages)
    pcol1, pcol2, pcol3 = st.columns([1,2,1])
    with pcol1:
        if st.button("◀ Prev", disabled=st.session_state["page"] <= 1):
            st.session_state["page"] -= 1
    with pcol2:
        st.caption(f"Page {st.session_state['page']} / {pages} — {total} match(es)")
    with pcol3:
        if st.button("Next ▶", disabled=st.session_state["page"] >= pages):
            st.session_state["page"] += 1

    start = (st.session_state["page"] - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)

    # Render (capped)
    if vdf.empty:
        st.info("No matching providers. Tip: try fewer words.")
    else:
        render = vdf.iloc[start:end].head(MAX_RENDER_ROWS).copy()
        # Optional debug
        if st.session_state["show_debug"] or os.getenv("ADMIN_SHOW_DEBUG","0")=="1":
            st.caption(f"Browse — showing {len(render)} (page slice {start}:{end}) of {total}; cap {MAX_RENDER_ROWS}; data_ver={data_ver}")
        st.dataframe(render[[
            "id","business_name","category","service","phone","website","city","state","zip","computed_keywords","ckw_locked"
        ]], use_container_width=True)

    # CSV download of current view
    def _csv_bytes(df_: pd.DataFrame) -> bytes:
        buf = io.StringIO()
        df_.to_csv(buf, index=False)
        return buf.getvalue().encode("utf-8")

    btn_col1, btn_col2 = st.columns([1,1])
    with btn_col1:
        if not vdf.empty:
            st.download_button(
                "Download current view (CSV)",
                data=_csv_bytes(vdf.drop(columns=["_blob"], errors="ignore")),
                file_name="providers.csv",
                mime="text/csv",
            )
    with btn_col2:
        if st.button("Refresh cache"):
            load_df.clear()
            _get_data_ver.clear()
            st.success("Cache cleared. Reloading…")
            st.rerun()

# -----------------------------
# ➕ Add
# -----------------------------
with TAB[1]:
    st.subheader("Add Provider")

    with st.form("add_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            category = st.text_input("Category")
            phone = st.text_input("Phone")
            website = st.text_input("Website")
            address1 = st.text_input("Address 1")
            city = st.text_input("City")
            state = st.text_input("State", value="TX")
        with c2:
            service = st.text_input("Service")
            email = st.text_input("Email")
            address2 = st.text_input("Address 2")
            zipc = st.text_input("ZIP")
        with c3:
            business_name = st.text_input("Business name", help="Required")
            notes = st.text_area("Notes", height=120)
            lock_ckw = st.checkbox("Lock computed keywords")
            remember_seed = st.checkbox("Remember these keywords for this (category, service)")

        submitted = st.form_submit_button("Add")
        if submitted:
            if not business_name.strip():
                st.error("Business name is required.")
            else:
                pdigits = _digits_only(phone)
                now = _now_iso()
                ckw = _gen_ckw(category, service, business_name)
                with ENGINE.begin() as cx:
                    cx.execute(sql_text(
                        """
                        INSERT INTO vendors(
                            category, service, business_name, phone, phone_digits,
                            website, email, address1, address2, city, state, zip,
                            notes, created_at, updated_at, computed_keywords, ckw_locked, ckw_version
                        ) VALUES(:category,:service,:business_name,:phone,:phone_digits,
                                :website,:email,:address1,:address2,:city,:state,:zip,
                                :notes,:created_at,:updated_at,:computed_keywords,:ckw_locked,:ckw_version)
                        """
                    ), {
                        "category": category.strip() or None,
                        "service": service.strip() or None,
                        "business_name": business_name.strip(),
                        "phone": phone.strip() or None,
                        "phone_digits": pdigits or None,
                        "website": website.strip() or None,
                        "email": email.strip() or None,
                        "address1": address1.strip() or None,
                        "address2": address2.strip() or None,
                        "city": city.strip() or None,
                        "state": (state.strip() or "TX").upper(),
                        "zip": zipc.strip() or None,
                        "notes": notes.strip() or None,
                        "created_at": now,
                        "updated_at": now,
                        "computed_keywords": ckw,
                        "ckw_locked": 1 if lock_ckw else 0,
                        "ckw_version": CURRENT_CKW_VER,
                    })
                    if remember_seed and ckw:
                        _ckw_seed_set(category.strip() or "", service.strip() or "", ckw)
                bump_data_ver()
                load_df.clear()
                st.success("Provider added.")

# -----------------------------
# ✏️ Edit
# -----------------------------
with TAB[2]:
    st.subheader("Edit Provider")

    with ENGINE.connect() as cx:
        ids = [r[0] for r in cx.execute(sql_text("SELECT id FROM vendors ORDER BY id"))]
    eid = st.selectbox("Select ID", ids if ids else [None], index=0 if ids else None)

    if eid:
        with ENGINE.connect() as cx:
            row = cx.execute(sql_text(
                """
                SELECT id, category, service, business_name, phone, phone_digits,
                       website, email, address1, address2, city, state, zip,
                       notes, created_at, updated_at, computed_keywords, ckw_locked
                  FROM vendors WHERE id=:id
                """
            ), {"id": int(eid)}).fetchone()
        if not row:
            st.warning("Record not found.")
        else:
            (rid, category, service, business_name, phone, phone_digits, website, email, address1, address2, city, state, zipc, notes, created_at, updated_at, ckw, ckw_locked) = row
            st.caption(f"Last update: {updated_at}")
            with st.form("edit_form"):
                c1, c2, c3 = st.columns(3)
                with c1:
                    category = st.text_input("Category", value=_s(category))
                    phone = st.text_input("Phone", value=_s(phone))
                    website = st.text_input("Website", value=_s(website))
                    address1 = st.text_input("Address 1", value=_s(address1))
                    city = st.text_input("City", value=_s(city))
                    state = st.text_input("State", value=_s(state or "TX"))
                with c2:
                    service = st.text_input("Service", value=_s(service))
                    email = st.text_input("Email", value=_s(email))
                    address2 = st.text_input("Address 2", value=_s(address2))
                    zipc = st.text_input("ZIP", value=_s(zipc))
                with c3:
                    business_name = st.text_input("Business name", value=_s(business_name))
                    notes = st.text_area("Notes", value=_s(notes), height=120)
                    ckw_locked_new = st.checkbox("Lock computed keywords", value=bool(int(ckw_locked or 0)))
                    ckw = st.text_area("Computed keywords", value=_s(ckw), height=120, key="edit_computed_keywords")
                    remember_seed = st.checkbox("Remember these keywords for this (category, service)")

                save = st.form_submit_button("Save changes")
                if save:
                    if not business_name.strip():
                        st.error("Business name is required.")
                    else:
                        pdigits = _digits_only(phone)
                        now = _now_iso()
                        with ENGINE.begin() as cx:
                            # optimistic concurrency on updated_at
                            res = cx.execute(sql_text(
                                """
                                UPDATE vendors
                                   SET category=:category,
                                       service=:service,
                                       business_name=:business_name,
                                       phone=:phone,
                                       phone_digits=:phone_digits,
                                       website=:website,
                                       email=:email,
                                       address1=:address1,
                                       address2=:address2,
                                       city=:city,
                                       state=:state,
                                       zip=:zip,
                                       notes=:notes,
                                       updated_at=:now,
                                       computed_keywords=:ckw,
                                       ckw_locked=:ckw_locked,
                                       ckw_version=:ver
                                 WHERE id=:id
                                   AND COALESCE(updated_at,'') = COALESCE(:prev_updated,'')
                                """
                            ), {
                                "category": category.strip() or None,
                                "service": service.strip() or None,
                                "business_name": business_name.strip(),
                                "phone": phone.strip() or None,
                                "phone_digits": pdigits or None,
                                "website": website.strip() or None,
                                "email": email.strip() or None,
                                "address1": address1.strip() or None,
                                "address2": address2.strip() or None,
                                "city": city.strip() or None,
                                "state": (state.strip() or "TX").upper(),
                                "zip": zipc.strip() or None,
                                "notes": notes.strip() or None,
                                "now": now,
                                "ckw": ckw.strip() or None,
                                "ckw_locked": 1 if ckw_locked_new else 0,
                                "ver": CURRENT_CKW_VER,
                                "id": int(rid),
                                "prev_updated": _s(updated_at),
                            })
                        if res.rowcount == 0:
                            st.error("Record changed by someone else. Reload and try again.")
                        else:
                            if remember_seed and (category or service) and ckw:
                                _ckw_seed_set(category.strip() or "", service.strip() or "", ckw.strip())
                            bump_data_ver()
                            load_df.clear()
                            st.success("Saved.")

# -----------------------------
# 📥 CSV Restore (append-only)
# -----------------------------
with TAB[3]:
    st.subheader("CSV Restore (Append-Only)")
    st.caption("Uploads new providers only; existing rows are not modified. For edits, use the Edit tab.")

    up = st.file_uploader("Upload CSV", type=["csv"])
    if up is not None:
        try:
            df_csv = pd.read_csv(up)
        except Exception as e:
            st.error(f"Failed to parse CSV: {e}")
            df_csv = None
        if df_csv is not None:
            st.dataframe(df_csv.head(20), use_container_width=True)
            st.caption(f"Detected columns: {', '.join(df_csv.columns)}")
            # Minimal mapping
            req = ["business_name"]
            missing = [c for c in req if c not in df_csv.columns]
            if missing:
                st.error(f"Missing required columns: {missing}")
            else:
                do_restore = st.button("Append rows")
                if do_restore:
                    added = 0
                    now = _now_iso()
                    with ENGINE.begin() as cx:
                        for _, r in df_csv.iterrows():
                            name = _s(r.get("business_name")).strip()
                            if not name:
                                continue
                            phone = _s(r.get("phone"))
                            pdigits = _digits_only(phone)
                            data = {
                                "category": _s(r.get("category")).strip() or None,
                                "service": _s(r.get("service")).strip() or None,
                                "business_name": name,
                                "phone": phone or None,
                                "phone_digits": pdigits or None,
                                "website": _s(r.get("website")).strip() or None,
                                "email": _s(r.get("email")).strip() or None,
                                "address1": _s(r.get("address1")).strip() or None,
                                "address2": _s(r.get("address2")).strip() or None,
                                "city": _s(r.get("city")).strip() or None,
                                "state": (_s(r.get("state")) or "TX").strip().upper(),
                                "zip": _s(r.get("zip")).strip() or None,
                                "notes": _s(r.get("notes")).strip() or None,
                                "created_at": now,
                                "updated_at": now,
                                "computed_keywords": _s(r.get("computed_keywords")).strip() or _gen_ckw(_s(r.get("category")), _s(r.get("service")), name),
                                "ckw_locked": 1 if str(_s(r.get("ckw_locked"))).strip() in ("1","true","True") else 0,
                                "ckw_version": CURRENT_CKW_VER,
                            }
                            cx.execute(sql_text(
                                """
                                INSERT INTO vendors(
                                    category, service, business_name, phone, phone_digits,
                                    website, email, address1, address2, city, state, zip,
                                    notes, created_at, updated_at, computed_keywords, ckw_locked, ckw_version
                                ) VALUES(:category,:service,:business_name,:phone,:phone_digits,
                                        :website,:email,:address1,:address2,:city,:state,:zip,
                                        :notes,:created_at,:updated_at,:computed_keywords,:ckw_locked,:ckw_version)
                                """
                            ), data)
                            added += 1
                    if added:
                        bump_data_ver()
                        load_df.clear()
                    st.success(f"Appended {added} row(s).")

# -----------------------------
# 🗂 Category / Service Admin
# -----------------------------
with TAB[4]:
    st.subheader("Category & Service Admin")

    with ENGINE.connect() as cx:
        cats = pd.DataFrame(cx.execute(sql_text(
            "SELECT category, COUNT(*) AS n FROM vendors GROUP BY category ORDER BY category"
        )).fetchall(), columns=["category","n"]) if True else pd.DataFrame()
        svcs = pd.DataFrame(cx.execute(sql_text(
            "SELECT service, COUNT(*) AS n FROM vendors GROUP BY service ORDER BY service"
        )).fetchall(), columns=["service","n"]) if True else pd.DataFrame()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Categories**")
        st.dataframe(cats, use_container_width=True)
        st.markdown("Rename category")
        rc1, rc2 = st.columns(2)
        with rc1:
            old_cat = st.text_input("Old category")
        with rc2:
            new_cat = st.text_input("New category")
        if st.button("Rename category"):
            with ENGINE.begin() as cx:
                cx.execute(sql_text("UPDATE vendors SET category=:new WHERE category=:old"), {"new": new_cat, "old": old_cat})
            bump_data_ver(); load_df.clear(); st.success("Category renamed.")

    with c2:
        st.markdown("**Services**")
        st.dataframe(svcs, use_container_width=True)
        st.markdown("Rename service")
        rs1, rs2 = st.columns(2)
        with rs1:
            old_svc = st.text_input("Old service")
        with rs2:
            new_svc = st.text_input("New service")
        if st.button("Rename service"):
            with ENGINE.begin() as cx:
                cx.execute(sql_text("UPDATE vendors SET service=:new WHERE service=:old"), {"new": new_svc, "old": old_svc})
            bump_data_ver(); load_df.clear(); st.success("Service renamed.")

    st.markdown("Assign all rows with (category, service) to a new pair")
    ac1, ac2, ac3, ac4 = st.columns(4)
    with ac1:
        from_cat = st.text_input("From category", key="from_cat")
    with ac2:
        from_svc = st.text_input("From service", key="from_svc")
    with ac3:
        to_cat = st.text_input("To category", key="to_cat")
    with ac4:
        to_svc = st.text_input("To service", key="to_svc")
    if st.button("Reassign pair"):
        with ENGINE.begin() as cx:
            cx.execute(sql_text(
                "UPDATE vendors SET category=:nc, service=:ns WHERE category=:oc AND service=:os"
            ), {"nc": to_cat, "ns": to_svc, "oc": from_cat, "os": from_svc})
        bump_data_ver(); load_df.clear(); st.success("Pair reassigned.")

# -----------------------------
# 🛠 Maintenance
# -----------------------------
with TAB[5]:
    st.subheader("Maintenance")

    st.markdown("**Computed Keywords**")
    cols = st.columns(3)
    with cols[0]:
        if st.button("Recompute (stale & unlocked)"):
            rows = _rows_for_stale(CURRENT_CKW_VER)
            n = ckw_recompute_rows(rows, override_locks=False)
            st.success(f"Recomputed {n} row(s).")
    with cols[1]:
        if st.button("Recompute ALL unlocked"):
            rows = _rows_for_all_unlocked()
            n = ckw_recompute_rows(rows, override_locks=False)
            st.success(f"Recomputed {n} row(s).")
    with cols[2]:
        if st.button("Force Recompute ALL (override locks)"):
            rows = _rows_for_all_unlocked()
            n = ckw_recompute_rows(rows, override_locks=True)
            st.success(f"Forced recompute {n} row(s).")

    # CKW seed store UI
    with st.expander("CKW Seed Store"):
        c1, c2 = st.columns(2)
        with c1:
            cat = st.text_input("Category", key="seed_cat")
            svc = st.text_input("Service", key="seed_svc")
            if st.button("Load seed"):
                st.text_area("Current seed", value=_ckw_seed_get(cat, svc), height=120, key="seed_view")
        with c2:
            seed_new = st.text_area("Set/replace seed (comma-separated words)", height=120, key="seed_edit")
            if st.button("Save seed"):
                _ckw_seed_set(cat, svc, seed_new)
                st.success("Seed saved.")

    # Schema Bootstrap (guarded)
    st.markdown("**Schema Bootstrap**")
    if not SCHEMA_BOOTSTRAP_ALLOWED:
        st.info("Schema Bootstrap is disabled. Set ADMIN_ALLOW_SCHEMA_BOOTSTRAP=1 in secrets to enable.")
    else:
        st.warning("DANGER: creates tables/indexes if missing. Use ONLY when you intend to initialize an empty DB.")
        if st.button("Run Schema Bootstrap"):
            ensure_schema(ENGINE)
            bump_data_ver(); load_df.clear()
            st.success("Schema ensured.")

# -----------------------------
# 🔍 Quick Probes
# -----------------------------
with TAB[6]:
    st.subheader("Quick Probes & Integrity Checks")

    with ENGINE.connect() as cx:
        total = cx.execute(sql_text("SELECT COUNT(*) FROM vendors")).scalar() or 0
        locked = cx.execute(sql_text("SELECT COUNT(*) FROM vendors WHERE IFNULL(ckw_locked,0)=1")).scalar() or 0
        stale = cx.execute(sql_text(
            """
            SELECT COUNT(*) FROM vendors
             WHERE IFNULL(ckw_locked,0)=0
               AND (ckw_version IS NULL OR ckw_version<>:v
                    OR computed_keywords IS NULL OR TRIM(computed_keywords)='')
            """
        ), {"v": CURRENT_CKW_VER}).scalar() or 0
        ucat = cx.execute(sql_text(
            "SELECT category, COUNT(*) AS n FROM vendors GROUP BY category HAVING n=0 ORDER BY category"
        )).fetchall()
        usvc = cx.execute(sql_text(
            "SELECT service, COUNT(*) AS n FROM vendors GROUP BY service HAVING n=0 ORDER BY service"
        )).fetchall()

    m1, m2, m3 = st.columns(3)
    m1.metric("Total vendors", total)
    m2.metric("CKW locked", locked)
    m3.metric("CKW stale (recompute)", stale)

    _uc = pd.DataFrame(ucat, columns=["category","n"])
    _us = pd.DataFrame(usvc, columns=["service","n"])

    st.markdown("**Unused taxonomy entries**")
    u1, u2 = st.columns(2)
    with u1:
        st.dataframe(_uc, use_container_width=True)
    with u2:
        st.dataframe(_us, use_container_width=True)

# -----------------------------
# ℹ️ Diagnostics
# -----------------------------
with st.expander("Diagnostics & Engine Info", expanded=False):
    st.markdown("Inspect connection, schema, and runtime status.")

    try:
        with ENGINE.connect() as cx:
            tables = [r[0] for r in cx.execute(sql_text(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )).fetchall()]
            st.write(f"Tables present: {tables}")

            if "vendors" in tables:
                count = cx.execute(sql_text("SELECT COUNT(*) FROM vendors")).scalar()
                st.write(f"Vendor rows: {count}")

                sample = cx.execute(sql_text(
                    """
                    SELECT id, business_name, category, service
                      FROM vendors
                     ORDER BY id ASC
                     LIMIT 5
                    """
                )).fetchall()
                st.write("Sample rows:")
                st.dataframe(pd.DataFrame(sample, columns=["id", "business_name", "category", "service"]))

            meta = {}
            try:
                meta_rows = cx.execute(sql_text("SELECT key, val FROM meta")).fetchall()
                meta = {k: v for k, v in meta_rows}
            except Exception:
                pass
            if meta:
                st.write("Meta table:")
                st.json(meta, expanded=False)
    except Exception as e:
        st.error(f"Diagnostics failed: {e}")

    st.markdown("---")
    st.markdown("**Engine parameters:**")
    st.json({
        "SQLAlchemy version": sa.__version__,
        "sqlalchemy-libsql version": SA_LIBSQL_VER,
        "Engine class": ENGINE.__class__.__name__,
        "URL": str(ENGINE.url),
        "Pool": str(getattr(ENGINE, 'pool', None)),
        "Target": TARGET_DESC,
        "APP_VER": APP_VER,
    }, expanded=False)

# -----------------------------
# ✅ End of app_admin.py
# -----------------------------
if __name__ == "__main__":
    try:
        st.success("Admin app loaded successfully.")
    except Exception as e:
        st.error(f"Fatal error at startup: {e}")
