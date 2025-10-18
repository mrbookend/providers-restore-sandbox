# app_readonly.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import io
import re
import html
import textwrap
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.engine import Engine

# Ensure the libsql dialect is registered (sqlite+libsql)
try:
    import sqlalchemy_libsql  # noqa: F401
except Exception:
    pass


# =============================
# Utilities
# =============================
def _get_secret(name: str, default: Optional[str | int | dict | bool] = None):
    """Prefer Streamlit secrets; tolerate env fallback only for simple strings."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    if isinstance(default, (str, type(None))):
        return os.getenv(name, default)  # env fallback for strings
    return default


def _as_bool(v: Optional[str | bool], default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _to_int(v, default: int) -> int:
    """Coerce to int; tolerate '200px', ' 180 ', floats, etc."""
    try:
        if isinstance(v, bool):
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        s = str(v).strip()
        if not s:
            return default
        m = re.search(r"-?\d+", s)
        if not m:
            return default
        n = int(m.group(0))
        return n if n > 0 else default
    except Exception:
        return default


def _css_len_from_secret(v: str | int | None, fallback_px: int) -> str:
    """
    Accepts e.g. "0", "20", "18px", "1rem", "0.5em".
    Returns a safe CSS length string; defaults to '{fallback_px}px' on bad input.
    """
    if v is None:
        return f"{fallback_px}px"
    s = str(v).strip()
    if not s:
        return f"{fallback_px}px"
    # numeric with optional unit
    if re.fullmatch(r"\d+(\.\d+)?(px|rem|em)?", s):
        if s.endswith(("px", "rem", "em")):
            return s
        return f"{s}px"
    return f"{fallback_px}px"


def _get_help_md() -> str:
    """Prefer top-level HELP_MD; if missing, also look under widths; else fallback."""
    # 1) top-level
    try:
        top = (st.secrets.get("HELP_MD", "") or "").strip()
        if top:
            return top
    except Exception:
        pass
    # 2) nested in [COLUMN_WIDTHS_PX_READONLY] (common paste mistake)
    try:
        nested = st.secrets.get("COLUMN_WIDTHS_PX_READONLY", {})
        if isinstance(nested, dict):
            cand = (nested.get("HELP_MD", "") or "").strip()
            if cand:
                return cand
    except Exception:
        pass
    # 3) built-in fallback
    return textwrap.dedent(
        """
        <style>
          .help-body p, .help-body li { font-size: 1rem; line-height: 1.45; }
          .help-body h1 { font-size: 1.4rem; margin: 0 0 6px; }
          .help-body h2 { font-size: 1.2rem; margin: 12px 0 6px; }
          .help-body h3 { font-size: 1.05rem; margin: 10px 0 6px; }
        </style>
        <div class="help-body">
          <h1>How to Use This List</h1>
          <p>Type in the search box, choose Sort, and download CSV/XLSX of your current view.</p>
        </div>
        """
    ).strip()


# =============================
# Page config
# =============================
PAGE_TITLE = _get_secret("page_title", "HCR Providers — Read-Only")
PAGE_MAX_WIDTH_PX = _to_int(_get_secret("page_max_width_px", 2300), 2300)
SIDEBAR_STATE = _get_secret("sidebar_state", "collapsed")
SHOW_DIAGS = _as_bool(_get_secret("READONLY_SHOW_DIAGS", False), False)
SHOW_STATUS = _as_bool(_get_secret("READONLY_SHOW_STATUS", False), False)

# secrets-driven padding (matches admin app behavior)
PAD_LEFT_CSS = _css_len_from_secret(_get_secret("page_left_padding_px", "12"), 12)

# secrets-driven viewport rows (10–40 clamp)
def _viewport_rows() -> int:
    n = _to_int(_get_secret("READONLY_VIEWPORT_ROWS", 15), 15)
    if n < 10:
        return 10
    if n > 40:
        return 40
    return n

VIEWPORT_ROWS = _viewport_rows()
ROW_PX = 32  # approximate row height
HEADER_PX = 44
SCROLL_MAX_HEIGHT = HEADER_PX + ROW_PX * VIEWPORT_ROWS  # pixels

st.set_page_config(page_title=PAGE_TITLE, layout="wide", initial_sidebar_state=SIDEBAR_STATE)

# ---- Help/Tips expander state (for Close button) ----
st.session_state.setdefault("help_open", False)

st.markdown(
    f"""
    <style>
      .block-container {{
        max-width: {PAGE_MAX_WIDTH_PX}px;
        padding-left: {PAD_LEFT_CSS};
      }}
      .prov-table td, .prov-table th {{
        white-space: normal;
        word-break: break-word;
        vertical-align: top;
        padding: 6px 8px;
        border-bottom: 1px solid #eee;
      }}
      .prov-table thead th {{
        position: sticky;
        top: 0;
        background: #fff;
        z-index: 2;
        border-bottom: 2px solid #ddd;
      }}
      .prov-wrap {{ overflow-x: auto; }}
      /* vertical scroll viewport (mouse-wheel scroll to end) */
      .prov-scroll {{
        max-height: {SCROLL_MAX_HEIGHT}px;
        overflow-y: auto;
      }}
      .help-row {{ display: flex; align-items: center; gap: 12px; }}
    </style>
    """,
    unsafe_allow_html=True,
)

if SHOW_DIAGS:
    st.caption(f"HELP_MD present: {'HELP_MD' in getattr(st, 'secrets', {})}")
    st.caption(
        "Turso secrets — URL: %s | TOKEN: %s"
        % (bool(_get_secret("TURSO_DATABASE_URL", "")), bool(_get_secret("TURSO_AUTH_TOKEN", "")))
    )
    st.caption(f"Viewport rows: {VIEWPORT_ROWS} (height ≈ {SCROLL_MAX_HEIGHT}px)")


# =============================
# Column labels & widths (SAFE)
# =============================
_readonly_labels_raw = _get_secret("READONLY_COLUMN_LABELS", {}) or {}
_col_widths_raw = _get_secret("COLUMN_WIDTHS_PX_READONLY", {}) or {}

READONLY_COLUMN_LABELS: Dict[str, str] = dict(_readonly_labels_raw)

_DEFAULT_WIDTHS = {
    "id": 40,
    "business_name": 180,
    "category": 175,
    "service": 120,
    "contact_name": 110,
    "phone": 120,
    "address": 220,
    "website": 160,
    "notes": 220,
    "keywords": 90,
}

# Only accept known width keys; ignore accidental extras (e.g., HELP_MD)
_user_widths: Dict[str, int] = {}
for k, v in dict(_col_widths_raw).items():
    sk = str(k)
    if sk not in _DEFAULT_WIDTHS:
        continue
    _user_widths[sk] = _to_int(v, _DEFAULT_WIDTHS.get(sk, 140))

COLUMN_WIDTHS_PX_READONLY: Dict[str, int] = {**_DEFAULT_WIDTHS, **_user_widths}

# Clamp invalids
for k, v in list(COLUMN_WIDTHS_PX_READONLY.items()):
    if not isinstance(v, int) or v <= 0:
        COLUMN_WIDTHS_PX_READONLY[k] = _DEFAULT_WIDTHS.get(k, 120)

# Do NOT pin first column (per request)
STICKY_FIRST_COL: bool = False

# Hide these in display; still searchable
HIDE_IN_DISPLAY = {"id", "keywords", "created_at", "updated_at", "updated_by"}


# =============================
# Database engine
# =============================
def build_engine() -> Tuple[Engine, Dict[str, str]]:
    info: Dict[str, str] = {}

    raw_url = _get_secret("TURSO_DATABASE_URL", "") or ""
    token   = _get_secret("TURSO_AUTH_TOKEN", "") or ""
    embedded_path = os.path.abspath(_get_secret("EMBEDDED_DB_PATH", "vendors-embedded.db"))

    if raw_url and token:
        # sync_url must be libsql://... (strip sqlite+libsql:// and query if present)
        if raw_url.startswith("sqlite+libsql://"):
            host = raw_url.split("://", 1)[1].split("?", 1)[0]
            sync_url = f"libsql://{host}"
        elif raw_url.startswith("libsql://"):
            sync_url = raw_url.split("?", 1)[0]
        else:
            host = raw_url.split("://", 1)[-1].split("?", 1)[0]
            sync_url = f"libsql://{host}"

        sqlalchemy_url = f"sqlite+libsql:///{embedded_path}"

        engine = create_engine(
            sqlalchemy_url,
            connect_args={
                "auth_token": token,
                "sync_url": sync_url,
            },
            pool_pre_ping=True,
            pool_recycle=300,
            pool_reset_on_return="commit",
        )
        # probe
        with engine.connect() as c:
            c.exec_driver_sql("select 1;")

        info.update(
            {
                "using_remote": "true",
                "strategy": "embedded_replica",
                "sqlalchemy_url": sqlalchemy_url,
                "dialect": "sqlite",
                "driver": getattr(engine.dialect, "driver", ""),
                "sync_url": sync_url,
            }
        )
        return engine, info

    # Fallback (local dev only)
    local_path = os.path.abspath("./vendors.db")
    local_url = f"sqlite:///{local_path}"
    engine = create_engine(local_url, pool_pre_ping=True)
    info.update(
        {
            "using_remote": "false",
            "strategy": "local_fallback",
            "sqlalchemy_url": local_url,
            "dialect": "sqlite",
            "driver": getattr(engine.dialect, "driver", ""),
        }
    )
    return engine, info


# =============================
# Data access
# =============================
VENDOR_COLS = [
    "id", "category", "service", "business_name", "contact_name", "phone",
    "address", "website", "notes", "keywords", "created_at", "updated_at", "updated_by",
]


def fetch_vendors(engine: Engine) -> pd.DataFrame:
    sql = "SELECT " + ", ".join(VENDOR_COLS) + " FROM vendors ORDER BY lower(business_name)"
    with engine.begin() as conn:
        df = pd.read_sql(sql_text(sql), conn)

    def _mk_anchor(v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            return ""
        u = v.strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            u = "https://" + u
        # compact label for display
        href = html.escape(u, quote=True)
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer">Website</a>'

    if "website" in df.columns:
        df["website"] = df["website"].fillna("").astype(str).map(_mk_anchor)

    for c in df.columns:
        if c not in ("id", "created_at", "updated_at"):
            df[c] = df[c].fillna("").astype(str)
    return df


# =============================
# Filtering
# =============================
def apply_global_search(df: pd.DataFrame, query: str) -> pd.DataFrame:
    q = (query or "").strip().lower()
    if not q:
        return df
    mask = pd.Series(False, index=df.index)
    for c in df.columns:
        # Coerce to string safely, lower, then literal substring search (no regex)
        s = df[c].astype(str).str.lower()
        mask |= s.str.contains(q, regex=False, na=False)
    return df[mask]


# =============================
# Rendering
# =============================
def _rename_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, str]]:
    mapping = {k: READONLY_COLUMN_LABELS.get(k, k.replace("_", " ").title()) for k in df.columns}
    df2 = df.rename(columns={k: mapping[k] for k in df.columns})
    rev = {v: k for k, v in mapping.items()}
    return df2, rev


def _build_table_html(df: pd.DataFrame, sticky_first: bool) -> str:
    df_disp, rev = _rename_columns(df)
    headers = []
    for col in df_disp.columns:
        orig = rev[col]
        width = COLUMN_WIDTHS_PX_READONLY.get(orig, 140)
        headers.append(
            f'<th style="min-width:{width}px;max-width:{width}px;">{html.escape(col)}</th>'
        )
    thead = "<thead><tr>" + "".join(headers) + "</tr></thead>"

    rows_html: List[str] = []
    for _, row in df_disp.iterrows():
        tds: List[str] = []
        for col in df_disp.columns:
            orig = rev[col]
            val = row[col]
            width = COLUMN_WIDTHS_PX_READONLY.get(orig, 140)
            if orig == "website":
                cell_html = val
            else:
                cell_html = html.escape(str(val) if val is not None else "")
            tds.append(f'<td style="min-width:{width}px;max-width:{width}px;">{cell_html}</td>')
        rows_html.append("<tr>" + "".join(tds) + "</tr>")
    tbody = "<tbody>" + "".join(rows_html) + "</tbody>"
    # prov-scroll enables the vertical scroll viewport sized by secrets
    return f'<div class="prov-wrap prov-scroll"><table class="prov-table">{thead}{tbody}</table></div>'


# =============================
# Main UI
# =============================
def main():
    engine, info = build_engine()
    if info.get("strategy") == "local_fallback":
        st.warning("Turso credentials not found. Running on local SQLite fallback (`./vendors.db`).")

    try:
        df_full = fetch_vendors(engine)
    except Exception as e:
        st.error(f"Failed to load vendors: {e}")
        return

    # Search runs on the full frame (so 'keywords' works), but display hides selected cols
    st.text_input(
        "",
        key="q",
        label_visibility="collapsed",
        placeholder="Search e.g., plumb, roofing, 'Inverness', phone digits, etc.",
        help="Case-insensitive, matches partial words across all columns. Tip text is also in Help / Tips.",
    )
    q = st.session_state.get("q", "")
    filtered_full = apply_global_search(df_full, q)

    # Columns we show (hide internal columns)
    disp_cols = [c for c in filtered_full.columns if c not in HIDE_IN_DISPLAY]
    df_disp_all = filtered_full[disp_cols]

    # ----- Controls Row: Help (left) + Downloads/Sort (right) -----
    def _label_for(col_key: str) -> str:
        return READONLY_COLUMN_LABELS.get(col_key, col_key.replace("_", " ").title())

    # Exclude 'website' from sort choices (HTML anchors)
    sortable_cols = [c for c in disp_cols if c != "website"]
    sort_labels = [_label_for(c) for c in sortable_cols]

    c_help, c_spacer, c_csv, c_xlsx, c_sort, c_order = st.columns([2, 6, 2, 2, 2, 2])

    with c_help:
        open_help = st.button("Help / Tips", type="primary", use_container_width=True)
    if open_help:
        st.session_state["help_open"] = True

    # Safe defaults when no sortable cols (highly unlikely, but defensive)
    if len(sortable_cols) == 0:
        chosen_label = "Business Name"
        order = "Ascending"
        sort_col = None
        ascending = True
    else:
        default_sort_col = "business_name" if "business_name" in sortable_cols else sortable_cols[0]
        default_label = _label_for(default_sort_col)

        with c_sort:
            chosen_label = st.selectbox(
                "Sort by",
                options=sort_labels,
                index=max(0, sort_labels.index(default_label)) if default_label in sort_labels else 0,
                key="sort_by_label",
            )
        with c_order:
            order = st.selectbox(
                "Order",
                options=["Ascending", "Descending"],
                index=0,
                key="sort_order",
            )

        sort_col = sortable_cols[sort_labels.index(chosen_label)] if chosen_label in sort_labels else sortable_cols[0]
        ascending = (order == "Ascending")

    # Case-insensitive sort for text columns; stable sort keeps ties predictable
    if sort_col is not None and sort_col in df_disp_all.columns and not df_disp_all.empty:
        keyfunc = (lambda s: s.str.lower()) if pd.api.types.is_string_dtype(df_disp_all[sort_col]) else None
        df_disp_sorted = df_disp_all.sort_values(
            by=sort_col,
            ascending=ascending,
            kind="mergesort",
            key=keyfunc
        )
    else:
        df_disp_sorted = df_disp_all.copy()

    # Downloads (use sorted view) — guard empty frames
    csv_buf = io.StringIO()
    if not df_disp_sorted.empty:
        df_disp_sorted.to_csv(csv_buf, index=False)
    with c_csv:
        st.download_button(
            "Download CSV",
            data=csv_buf.getvalue().encode("utf-8"),
            file_name="providers.csv",
            mime="text/csv",
            type="secondary",
            use_container_width=True,
            disabled=df_disp_sorted.empty,
        )

    excel_df = df_disp_sorted.copy()
    if "website" in excel_df.columns and not excel_df.empty:
        # Convert anchor to plain URL for Excel export
        excel_df["website"] = excel_df["website"].str.replace(r'.*href="([^"]+)".*', r"\1", regex=True)

    xlsx_buf = io.BytesIO()
    if not excel_df.empty:
        with pd.ExcelWriter(xlsx_buf, engine="xlsxwriter") as writer:
            excel_df.to_excel(writer, sheet_name="providers", index=False)
        xlsx_data = xlsx_buf.getvalue()
    else:
        xlsx_data = b""
    with c_xlsx:
        st.download_button(
            "Download XLSX",
            data=xlsx_data,
            file_name="providers.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="secondary",
            use_container_width=True,
            disabled=excel_df.empty,
        )

    # Optional status line (toggle via Secrets)
    if SHOW_STATUS:
        st.caption(f"{len(df_disp_sorted)} matching provider(s). Viewport rows: {VIEWPORT_ROWS}")

    # -------- Help / Tips expander (controlled by session_state + Close button) --------
    def _close_help():
        st.session_state["help_open"] = False

    with st.expander("Provider Help / Tips", expanded=st.session_state.get("help_open", False)):
        st.markdown(_get_help_md(), unsafe_allow_html=True)
        st.divider()
        st.button("Close", type="secondary", on_click=_close_help)

    # ---------------- Scrollable full table (admin-style viewport) ----------------
    if df_disp_sorted.empty:
        st.info("No matching providers.")
    else:
        st.markdown(_build_table_html(df_disp_sorted, sticky_first=STICKY_FIRST_COL), unsafe_allow_html=True)

    # Debug
    st.divider()
    if st.button("Debug (status & secrets keys)", type="secondary"):
        dbg = {
            "DB (resolved)": info,
            "Secrets keys": sorted(list(getattr(st, "secrets", {}).keys())) if hasattr(st, "secrets") else [],
            "Widths (effective)": COLUMN_WIDTHS_PX_READONLY,
            "Viewport rows": VIEWPORT_ROWS,
            "Scroll height px": SCROLL_MAX_HEIGHT,
        }
        st.write(dbg)
        st.caption(f"HELP_MD present (top-level): {'HELP_MD' in getattr(st, 'secrets', {})}")
        try:
            nested = st.secrets.get("COLUMN_WIDTHS_PX_READONLY", {})
            has_nested_help = isinstance(nested, dict) and bool((nested.get("HELP_MD", "") or "").strip())
        except Exception:
            has_nested_help = False
        st.caption(f"HELP_MD present (nested in widths): {has_nested_help}")

        try:
            with engine.begin() as conn:
                cols_v = pd.read_sql(sql_text("PRAGMA table_info(vendors)"), conn)
                cols_c = pd.read_sql(sql_text("PRAGMA table_info(categories)"), conn)
                cols_s = pd.read_sql(sql_text("PRAGMA table_info(services)"), conn)
                counts = {}
                for t in ("vendors", "categories", "services"):
                    c = pd.read_sql(sql_text(f"SELECT COUNT(*) AS n FROM {t}"), conn)["n"].iloc[0]
                    counts[t] = int(c)
            probe = {
                "vendors_columns": list(cols_v["name"]) if "name" in cols_v else [],
                "categories_columns": list(cols_c["name"]) if "name" in cols_c else [],
                "services_columns": list(cols_s["name"]) if "name" in cols_s else [],
                "counts": counts,
            }
            st.write(probe)
        except Exception as e:
            st.write({"db_probe_error": str(e)})


if __name__ == "__main__":
    main()
