# app.py
# Miami-Dade Property & Market Insights Dashboard
# - Folio lookup (MapServer first, FeatureServer fallback)
# - Zoning mix by municipality
# - Recent sales inside selected area
# - CSV exports

import json
import requests
import pandas as pd
import streamlit as st
import folium
import time

# Safe import: streamlit-folium is optional
try:
    from streamlit_folium import st_folium
except ImportError:
    st_folium = None

st.set_page_config(page_title="Miami-Dade Property & Market Insights", page_icon="🏝️", layout="wide")

# ---------------------------
# Endpoints & Layers
# ---------------------------
MD_ZONING_FEATURESERVER = "https://services.arcgis.com/LBbVDC0hKPAnLRpO/ArcGIS/rest/services/Miami_Dade_Zoning_Phillips/FeatureServer"
LAYER_MUNICIPAL_BOUNDARY = 4
LAYER_ZONING = 12

# County-run MapServer with explicit FOLIO schema (more reliable for folio lookups)
PA_MAPSERVER = "https://gisweb.miamidade.gov/arcgis/rest/services/MD_Emaps/MapServer"
LAYER_PROPERTY_RECORDS = 70  # has FOLIO + address/owner/areas/rooms

# Hosted FeatureServer used for property point/sales queries
PA_GISVIEW_FEATURESERVER = "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/PaGISView_gdb/FeatureServer"
LAYER_PROPERTY_POINT_VIEW = 0

LINK_PROPERTY_APPRAISER = "https://www.miamidade.gov/Apps/PA/propertysearch/"
LINK_PROPERTY_APPRAISER_HELP = "https://www.miamidadepa.gov/pa/property-search-help.asp"
LINK_CLERK_OFFICIAL_RECORDS = "https://onlineservices.miamidadeclerk.gov/officialrecords"
LINK_GIS_HUB = "https://gis-mdc.opendata.arcgis.com/"
LINK_PLANNING_RESEARCH = "https://www.miamidade.gov/global/economy/planning/research-reports.page"
LINK_ECONOMIC_DASH = "https://www.miamidade.gov/global/economy/innovation-and-economic-development/economic-metrics.page"

# ---------------------------
# Helpers
# ---------------------------

def format_md_folio(input_str: str):
    """Return (hyphenated, digits) for a Miami‑Dade folio input.
    If the input has 13 digits, we format as xx-xxxx-xxx-xxxx.
    """
    s = (input_str or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 13:
        hyph = f"{digits[0:2]}-{digits[2:6]}-{digits[6:9]}-{digits[9:13]}"
        return hyph, digits
    return s, digits


def pa_folio_url(folio: str) -> str:
    digits = "".join(ch for ch in (folio or "") if ch.isdigit())
    return f"https://www.miamidade.gov/Apps/PA/propertysearch/#/folio/{digits}" if digits else LINK_PROPERTY_APPRAISER

@st.cache_data(show_spinner=False, ttl=60*60)
def arcgis_query(service_url: str, layer: int, params: dict):
    """Generic ArcGIS FeatureServer/MapServer query.
    - Uses POST if geometry present or request is long (avoid 413)
    - Only sets outSR when returnGeometry=True
    - Returns parsed JSON or None and shows a friendly message
    """
    base = f"{service_url}/{layer}/query"
    defaults = {
        "f": "json",
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": False,
    }
    q = {**defaults, **(params or {})}
    if q.get("returnGeometry"):
        q.setdefault("outSR", 4326)
    try:
        use_post = ("geometry" in q) or (len(base) + len(str(q)) > 1800)
        if use_post:
            r = requests.post(base, data=q, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"})
        else:
            r = requests.get(base, params=q, timeout=25)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data.get("error"))
        return data
    except Exception as e:
        st.info(f"ArcGIS query unavailable (layer {layer}). Details: {e}")
        return None

# Geometry simplification (Douglas–Peucker) to keep payloads small

def _perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    if (x1, y1) == (x2, y2):
        return ((x-x1)**2 + (y-y1)**2) ** 0.5
    t = ((x-x1)*(x2-x1) + (y-y1)*(y2-y1)) / ((x2-x1)**2 + (y2-y1)**2)
    t = max(0.0, min(1.0, t))
    proj = (x1 + t*(x2-x1), y1 + t*(y2-y1))
    return ((x-proj[0])**2 + (y-proj[1])**2) ** 0.5

def _dp(pts, eps):
    if len(pts) <= 2:
        return pts
    a, b = pts[0], pts[-1]
    dmax, idx = 0.0, -1
    for i in range(1, len(pts)-1):
        d = _perp(pts[i], a, b)
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps and idx != -1:
        left = _dp(pts[:idx+1], eps)
        right = _dp(pts[idx:], eps)
        return left[:-1] + right
    return [a, b]

def simplify_rings(rings, tolerance_meters=25):
    eps_deg = max(1e-6, tolerance_meters / 111_320.0)
    out = []
    for ring in rings:
        closed = ring[0] == ring[-1]
        core = ring[:-1] if closed else ring
        simp = _dp(core, eps_deg)
        if closed:
            simp.append(simp[0])
        out.append(simp)
    return out

# ---------------------------
# Data accessors
# ---------------------------

@st.cache_data(show_spinner=False, ttl=60*60)
def normalize_folios(text: str):
    """Parse newline/CSV/space-separated folios; return list of (hyphenated, digits)."""
    if not text:
        return []
    raw_tokens = [tok.strip() for tok in (
        text.replace("
", "
").replace(",", "
").split("
")
    ) if tok.strip()]
    seen = set(); out = []
    for tok in raw_tokens:
        hyph, digits = format_md_folio(tok)
        key = digits or hyph
        if key and key not in seen:
            seen.add(key)
            out.append((hyph, digits))
    return out


@st.cache_data(show_spinner=False, ttl=60*60)
def fetch_municipalities():
    data = arcgis_query(MD_ZONING_FEATURESERVER, LAYER_MUNICIPAL_BOUNDARY, {
        "outFields": "NAME", "returnGeometry": True
    })
    items = []
    if data and data.get("features"):
        for f in data["features"]:
            a = f.get("attributes", {})
            g = f.get("geometry", {})
            name = a.get("NAME") or a.get("Municipality") or a.get("municipality")
            rings = (g or {}).get("rings")
            if name and rings:
                items.append({"name": name, "rings": [rings[0]]})
    return sorted(items, key=lambda x: x["name"]) if items else []

@st.cache_data(show_spinner=False, ttl=60*60)
def geocode_address(addr: str):
    if not addr:
        return None
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": addr, "format": "json", "limit": 1},
            headers={"User-Agent": "mdc-dashboard/1.3 (Streamlit)"},
            timeout=20,
        )
        r.raise_for_status()
        res = r.json() or []
        if res:
            return float(res[0]["lat"]), float(res[0]["lon"])
    except Exception:
        pass
    return None

@st.cache_data(show_spinner=False, ttl=60*60)
def get_layer_fields(service_url: str, layer: int):
    try:
        meta = requests.get(f"{service_url}/{layer}", params={"f": "json"}, timeout=20).json()
        fields = meta.get("fields", []) if isinstance(meta, dict) else []
        return {(f.get("name") or "").lower(): f for f in fields}
    except Exception:
        return {}

@st.cache_data(show_spinner=False, ttl=60*60)
def get_property_by_folio(folio: str, prefer_mapserver: bool = True):
    """Look up a property by folio.
    A) Try county MapServer/70 (FOLIO) first; B) fall back to hosted FeatureServer/0.
    Returns {"attributes": normalized_dict, "source": str, "where_used": str} or None.
    """
    if not folio:
        return None

    raw = str(folio).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())

    def hyph(d: str) -> str:
        return f"{d[0:2]}-{d[2:6]}-{d[6:9]}-{d[9:13]}" if len(d) == 13 else d

    hyphenated = hyph(digits) if digits else raw

    def esc(s: str) -> str:
        return s.replace("'", "''")

    def _query_mapserver70():
        out_fields = ",".join([
            "FOLIO","TRUE_SITE_ADDR","TRUE_SITE_CITY","TRUE_SITE_ZIP_CODE",
            "TRUE_OWNER1","TRUE_OWNER2","DOR_DESC","SUBDIVISION","YEAR_BUILT","LOT_SIZE",
            "BUILDING_ACTUAL_AREA","BUILDING_EFFECTIVE_AREA","BUILDING_GROSS_AREA",
            "BEDROOM_COUNT","BATHROOM_COUNT","HALF_BATHROOM_COUNT","FLOOR_COUNT"
        ])
        wheres = []
        if hyphenated: wheres.append(f"FOLIO = '{esc(hyphenated)}'")
        if digits and len(digits) == 13:
            wheres.append(f"FOLIO = '{esc(digits)}'")
            wheres.append(f"FOLIO LIKE '%{esc(digits)}%'")
        for w in wheres:
            data = arcgis_query(PA_MAPSERVER, LAYER_PROPERTY_RECORDS, {
                "where": w, "outFields": out_fields, "returnGeometry": False, "resultRecordCount": 5
            })
            feats = (data or {}).get("features", [])
            if feats:
                a = feats[0].get("attributes", {})
                mapped = {
                    "folio": a.get("FOLIO"),
                    "true_site_addr": a.get("TRUE_SITE_ADDR"),
                    "true_site_city": a.get("TRUE_SITE_CITY"),
                    "true_site_zip_code": a.get("TRUE_SITE_ZIP_CODE"),
                    "true_owner1": a.get("TRUE_OWNER1"),
                    "true_owner2": a.get("TRUE_OWNER2"),
                    "dor_desc": a.get("DOR_DESC"),
                    "subdivision": a.get("SUBDIVISION"),
                    "year_built": a.get("YEAR_BUILT"),
                    "lot_size": a.get("LOT_SIZE"),
                    "building_heated_area": a.get("BUILDING_EFFECTIVE_AREA") or a.get("BUILDING_ACTUAL_AREA"),
                    "adjusted_area": a.get("BUILDING_EFFECTIVE_AREA"),
                    "actual_area": a.get("BUILDING_ACTUAL_AREA"),
                    "bedrooms": a.get("BEDROOM_COUNT"),
                    "bathrooms": a.get("BATHROOM_COUNT"),
                    "half_bathrooms": a.get("HALF_BATHROOM_COUNT"),
                    "no_stories": a.get("FLOOR_COUNT"),
                    "pa_primary_zone": None,
                    "primarylanduse_desc": a.get("DOR_DESC"),
                }
                return {"attributes": mapped, "source": "MapServer/70", "where_used": w}
        return None

    def _query_featureserver0():
        fields_meta = get_layer_fields(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW)
        # discover folio field name
        folio_field = None
        for cand in ["folio","folio_num","folio_nbr","folioid","folio_number","md_folio"]:
            if cand in fields_meta:
                folio_field = fields_meta[cand]["name"]; break
        if folio_field is None:
            for k, v in fields_meta.items():
                if "folio" in k:
                    folio_field = v.get("name"); break
        folio_field = folio_field or "folio"

        out_fields = ",".join([
            "folio","true_site_addr","true_site_city","true_site_zip_code",
            "true_owner1","true_owner2","dor_desc","subdivision","year_built","lot_size",
            "building_heated_area","adjusted_area","actual_area","living_units","bedrooms","bathrooms","half_bathrooms","no_stories",
            "pa_primary_zone","primarylanduse_desc","mailing_address1","mailing_address2","mailing_city","mailing_state","mailing_zip"
        ])
        wheres = []
        if hyphenated: wheres.append(f"{folio_field} = '{esc(hyphenated)}'")
        if digits and len(digits) == 13:
            wheres.append(f"{folio_field} = '{esc(digits)}'")
            wheres.append(f"{folio_field} LIKE '%{esc(digits)}%'")
        for w in wheres:
            data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, {
                "where": w, "outFields": out_fields, "returnGeometry": False, "resultRecordCount": 5
            })
            feats = (data or {}).get("features", [])
            if feats:
                a = feats[0].get("attributes", {})
                return {"attributes": a, "source": "FeatureServer/0", "where_used": w}
        return None

    first = _query_mapserver70 if prefer_mapserver else _query_featureserver0
    second = _query_featureserver0 if prefer_mapserver else _query_mapserver70
        return first() or second()

@st.cache_data(show_spinner=False, ttl=30*60)
def bulk_properties_by_folios(folio_list, prefer_mapserver: bool = True, sleep_sec: float = 0.15):
    """Lookup many folios and return a DataFrame of normalized export rows with status.
    folio_list: iterable of folio strings (hyphenated or digits)."""
    rows = []
    for idx, fol in enumerate(folio_list, start=1):
        hyph, digits = format_md_folio(fol)
        try:
            res = get_property_by_folio(hyph or digits, prefer_mapserver=prefer_mapserver)
            if res and res.get("attributes"):
                a = res["attributes"]
                rows.append({
                    "Folio": a.get('folio') or hyph or digits,
                    "Property Address": a.get('true_site_addr'),
                    "City": a.get('true_site_city'),
                    "ZIP": a.get('true_site_zip_code'),
                    "Owner 1": a.get('true_owner1'),
                    "Owner 2": a.get('true_owner2'),
                    "Subdivision": a.get('subdivision'),
                    "Primary Land Use": a.get('primarylanduse_desc') or a.get('dor_desc'),
                    "PA Primary Zone": a.get('pa_primary_zone'),
                    "Beds": a.get('bedrooms'),
                    "Baths": a.get('bathrooms'),
                    "Half Baths": a.get('half_bathrooms'),
                    "Floors": a.get('no_stories'),
                    "Living Units": a.get('living_units'),
                    "Actual Area (SqFt)": a.get('actual_area'),
                    "Living Area (SqFt)": a.get('building_heated_area'),
                    "Adjusted Area (SqFt)": a.get('adjusted_area'),
                    "Lot Size (SqFt)": a.get('lot_size'),
                    "Year Built": a.get('year_built'),
                    "PA Folio URL": pa_folio_url(a.get('folio') or hyph or digits),
                    "Source": res.get("source"),
                    "Query": res.get("where_used"),
                    "Status": "OK",
                })
            else:
                rows.append({"Folio": hyph or digits, "Status": "NOT FOUND"})
        except Exception as e:
            rows.append({"Folio": hyph or digits, "Status": f"ERROR: {e}"})
        time.sleep(max(0.0, sleep_sec))
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False, ttl=60*60)
def get_zoning_at_point(lon: float, lat: float):
    geom = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}
    data = arcgis_query(MD_ZONING_FEATURESERVER, LAYER_ZONING, {
        "geometry": json.dumps(geom),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ZONE,ZONE_DESC,OVLY,ZONEMUNC",
        "returnGeometry": False,
    })
    if data and data.get("features"):
        return data["features"][0].get("attributes", {})
    return None

@st.cache_data(show_spinner=False, ttl=60*60)
def get_zones_in_polygon(rings):
    use_rings = rings
    if sum(len(r) for r in rings) > 1500:
        use_rings = simplify_rings(rings, tolerance_meters=25)
    poly = {"rings": use_rings, "spatialReference": {"wkid": 4326}}
    data = arcgis_query(MD_ZONING_FEATURESERVER, LAYER_ZONING, {
        "geometry": json.dumps(poly),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ZONE,ZONE_DESC",
        "returnDistinctValues": True,
        "returnGeometry": False,
    })
    if not data or not data.get("features"):
        return pd.DataFrame(columns=["ZONE","ZONE_DESC"])
    rows = []
    for f in data["features"]:
        a = f.get("attributes", {})
        rows.append({"ZONE": a.get("ZONE"), "ZONE_DESC": a.get("ZONE_DESC")})
    return pd.DataFrame(rows).dropna().drop_duplicates().sort_values(by=["ZONE","ZONE_DESC"]).reset_index(drop=True)

@st.cache_data(show_spinner=False, ttl=30*60)
def get_recent_sales_in_polygon(rings, days: int = 90, max_rows: int = 5000):
    """Recent sales inside polygon using FeatureServer/0; paginate + client-side date filter."""
    def _paged(base_params, step=2000, cap=max_rows):
        rows, offset = [], 0
        while len(rows) < cap:
            params = dict(base_params)
            params["resultRecordCount"] = min(step, cap - len(rows))
            params["resultOffset"] = offset
            data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params)
            feats = (data or {}).get("features", [])
            if not feats:
                break
            rows.extend([f.get("attributes", {}) for f in feats])
            if len(feats) < params["resultRecordCount"]:
                break
            offset += params["resultRecordCount"]
        return rows

    use_rings = rings
    if sum(len(r) for r in rings) > 1500:
        use_rings = simplify_rings(rings, tolerance_meters=25)
    poly = {"rings": use_rings, "spatialReference": {"wkid": 4326}}

    base = {
        "geometry": json.dumps(poly),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join([
            "folio","true_site_addr","true_site_city","true_site_zip_code",
            "true_owner1","dateofsale_utc","price_1","dor_desc","subdivision","year_built","lot_size","building_heated_area"
        ]),
        "returnGeometry": False,
        "geometryPrecision": 6,
    }

    attrs = _paged(base)
    if not attrs:
        return pd.DataFrame(columns=["folio","true_site_addr","true_site_city","true_site_zip_code","true_owner1","dateofsale_utc","price_1","dor_desc","subdivision","year_built","lot_size","building_heated_area"]) 

    df = pd.DataFrame(attrs)

    # Detect & parse sale date
    candidate_cols = ["dateofsale_utc","dateofsale","sale_date","last_sale_date","date_of_sale","saledate"]
    date_col = next((c for c in candidate_cols if c in df.columns), None)
    if date_col:
        s = df[date_col]
        if pd.api.types.is_numeric_dtype(s):
            df[date_col] = pd.to_datetime(s, unit="ms", utc=True, errors="coerce")
        else:
            df[date_col] = pd.to_datetime(s, utc=True, errors="coerce")
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(days))
        df = df[df[date_col] >= cutoff]
        if date_col != "dateofsale_utc":
            df.rename(columns={date_col: "dateofsale_utc"}, inplace=True)

    if "price_1" in df.columns:
        df["price_1"] = pd.to_numeric(df["price_1"], errors="coerce")

    if "dateofsale_utc" in df.columns:
        try:
            df["dateofsale_utc"] = pd.to_datetime(df["dateofsale_utc"], utc=True, errors="coerce").dt.tz_convert("UTC").dt.tz_localize(None)
        except Exception:
            pass
        df = df.sort_values("dateofsale_utc", ascending=False)

    return df.reset_index(drop=True)

# ---------------------------
# UI
# ---------------------------

st.title("🏝️ Miami-Dade Property & Market Insights")
st.caption("Powered by Miami-Dade County Open Data & official portals.")

with st.sidebar:
    st.header("Filters")
    muni_items = fetch_municipalities()
    muni_names = [it["name"] for it in muni_items] if muni_items else []
    selected_muni = st.selectbox("Select Municipality / Area", options=["(none)"] + muni_names, index=0)

    st.markdown("**Look up specific properties** (opens official sites in a new tab):")
    addr = st.text_input("Address (for map & Property Appraiser link)")
    owner = st.text_input("Owner Name (for Property Appraiser & Clerk search)")
    folio_input = st.text_input("Folio Number (13 digits)")
    folio_hyph, folio_digits = format_md_folio(folio_input)
    if folio_digits and len(folio_digits) == 13 and folio_input != folio_hyph:
        st.caption(f"Using normalized folio: **{folio_hyph}**")
    folio = folio_hyph or folio_input

    st.markdown("**Recent Sales Window**")
    sales_window = st.slider("Days back", min_value=7, max_value=365, value=90, step=7)

    st.markdown("---")
    st.markdown(f"- 📍 Property Appraiser: [Search app]({LINK_PROPERTY_APPRAISER})  ")
    if folio:
        st.markdown(f"  • Quick link for folio **{folio}** → [open]({pa_folio_url(folio)})  ")
    st.markdown(f"- 🗺️ GIS Hub: [Open Data]({LINK_GIS_HUB})  ")
    st.markdown(f"- 📊 Econ & Planning: [Research Reports]({LINK_PLANNING_RESEARCH}) · [Metrics Dashboard]({LINK_ECONOMIC_DASH})  ")

col_map, col_info = st.columns([1.2, 0.8])

# --- Map column ---
with col_map:
    m = folium.Map(location=[25.774, -80.193], zoom_start=10, control_scale=True)

    selected_poly = None
    if selected_muni != "(none)" and muni_items:
        match = next((it for it in muni_items if it["name"] == selected_muni), None)
        if match:
            selected_poly = match["rings"]
            folium.Polygon(locations=[(lat, lon) for lon, lat in match["rings"][0]], tooltip=selected_muni, weight=2, fill=False).add_to(m)
            lats = [lat for lon, lat in match["rings"][0]]
            lons = [lon for lon, lat in match["rings"][0]]
            m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    pt_latlon = None
    if addr:
        loc = geocode_address(addr)
        if loc:
            pt_latlon = loc
            folium.Marker(location=[loc[0], loc[1]], tooltip=addr).add_to(m)
            if not selected_poly:
                m.location = [loc[0], loc[1]]
                m.zoom_start = 15

    if st_folium:
        st_folium(m, width=None, height=600)
    else:
        st.warning("streamlit-folium not installed. Map preview disabled. Please add 'streamlit-folium' to requirements.txt.")

# --- Info column ---
with col_info:
    st.subheader("Property by Folio")

    src_choice = st.radio(
        "Data source for folio lookup",
        ["MapServer first (recommended)", "FeatureServer first (fallback)"],
        horizontal=True,
        index=0,
        help="If you still see 400 errors, switch and compare diagnostics below.",
    )
    prefer_mapserver = (src_choice == "MapServer first (recommended)")

    if folio:
        result = get_property_by_folio(folio, prefer_mapserver=prefer_mapserver)
        if result:
            a = result["attributes"]
            st.success(f"Folio: {a.get('folio') or ''}")
            # Address & owners
            addr_line = a.get('true_site_addr') or ''
            city_zip = " ".join([str(a.get('true_site_city') or ''), str(a.get('true_site_zip_code') or '')]).strip()
            owner1 = a.get('true_owner1') or ''
            owner2 = a.get('true_owner2') or ''
            c1, c2 = st.columns(2)
            with c1:
                if addr_line: st.markdown(f"**Property Address:** {addr_line}")
                if city_zip:  st.markdown(city_zip)
            with c2:
                owners = "<br/>".join([x for x in [owner1, owner2] if x])
                if owners:
                    st.markdown("**Owner(s):**  ")
                    st.markdown(owners, unsafe_allow_html=True)
            # Use & zoning labels (if available)
            pa_zone = a.get('pa_primary_zone')
            use_desc = a.get('primarylanduse_desc') or a.get('dor_desc')
            subdiv = a.get('subdivision')
            parts = []
            if pa_zone:  parts.append(f"**PA Primary Zone:** {pa_zone}")
            if use_desc: parts.append(f"**Primary Land Use:** {use_desc}")
            if subdiv:   parts.append(f"**Subdivision:** {subdiv}")
            if parts:    st.markdown("  • ".join(parts))
            # Building details table
            kmap = {
                "bedrooms": "Beds",
                "bathrooms": "Baths",
                "half_bathrooms": "Half Baths",
                "no_stories": "Floors",
                "living_units": "Living Units",
                "actual_area": "Actual Area (SqFt)",
                "building_heated_area": "Living Area (SqFt)",
                "adjusted_area": "Adjusted Area (SqFt)",
                "lot_size": "Lot Size (SqFt)",
                "year_built": "Year Built",
            }
            disp = {v: a.get(k) for k, v in kmap.items() if a.get(k) is not None}
            if disp:
                df_disp = pd.DataFrame([disp]).T.reset_index()
                df_disp.columns = ["Attribute", "Value"]
                st.dataframe(df_disp, use_container_width=True, hide_index=True)

            st.link_button("Open in Property Appraiser (Folio tab)", pa_folio_url(a.get('folio') or folio))

            with st.expander("Folio lookup diagnostics"):
                st.write({"source": result.get("source"), "where_used": result.get("where_used")})

            # --- CSV export for this property ---
            export = {
                "Folio": a.get('folio') or folio,
                "Property Address": a.get('true_site_addr'),
                "City": a.get('true_site_city'),
                "ZIP": a.get('true_site_zip_code'),
                "Owner 1": a.get('true_owner1'),
                "Owner 2": a.get('true_owner2'),
                "Subdivision": a.get('subdivision'),
                "Primary Land Use": a.get('primarylanduse_desc') or a.get('dor_desc'),
                "PA Primary Zone": a.get('pa_primary_zone'),
                "Beds": a.get('bedrooms'),
                "Baths": a.get('bathrooms'),
                "Half Baths": a.get('half_bathrooms'),
                "Floors": a.get('no_stories'),
                "Living Units": a.get('living_units'),
                "Actual Area (SqFt)": a.get('actual_area'),
                "Living Area (SqFt)": a.get('building_heated_area'),
                "Adjusted Area (SqFt)": a.get('adjusted_area'),
                "Lot Size (SqFt)": a.get('lot_size'),
                "Year Built": a.get('year_built'),
                "PA Folio URL": pa_folio_url(a.get('folio') or folio),
            }
            df_prop = pd.DataFrame([export])
            csv_prop = df_prop.to_csv(index=False).encode('utf-8')
            st.download_button(
                "⬇️ Download this property (CSV)",
                data=csv_prop,
                file_name=f"property_{''.join(ch for ch in (a.get('folio') or folio) if ch.isdigit())}.csv",
                mime="text/csv",
            )

            # --- Bulk export UI ---
            st.markdown("---")
            st.markdown("### Bulk folio lookup & export")
            st.caption("Paste multiple folios (digits or hyphenated), one-per-line or separated by commas. Or upload a CSV with a column named 'folio'.")
            bulk_text = st.text_area("Paste folios here", height=120, placeholder="3530070191100
0131234567890
01-2345-678-9012, 1133260050000")
            upload = st.file_uploader("…or upload a CSV with a 'folio' column", type=["csv"]) 

            input_folios = []
            if bulk_text:
                input_folios.extend([x[0] or x[1] for x in normalize_folios(bulk_text)])
            if upload is not None:
                try:
                    df_up = pd.read_csv(upload)
                    if 'folio' in df_up.columns:
                        for v in df_up['folio'].astype(str).tolist():
                            h,d = format_md_folio(v)
                            input_folios.append(h or d)
                except Exception as e:
                    st.error(f"Could not read CSV: {e}")

            input_folios = [f for f in input_folios if f]
            if input_folios:
                st.write(f"Detected **{len(input_folios)}** folio(s). Duplicates will be ignored in results.")
                if st.button("Run bulk lookup"):
                    with st.spinner("Fetching properties…"):
                        df_bulk = bulk_properties_by_folios(input_folios, prefer_mapserver=prefer_mapserver)
                    if not df_bulk.empty:
                        # drop duplicate rows by Folio keeping first OK
                        if 'Folio' in df_bulk.columns:
                            df_bulk = df_bulk.drop_duplicates(subset=['Folio'], keep='first')
                        st.dataframe(df_bulk, use_container_width=True)
                        csv_bulk = df_bulk.to_csv(index=False).encode('utf-8')
                        st.download_button("⬇️ Download bulk results (CSV)", data=csv_bulk, file_name="mdc_properties_bulk.csv", mime="text/csv")
                    else:
                        st.info("No results returned for the provided folios.")
            else:
                st.caption("Provide folios above to enable bulk lookup.")

        else:
            st.info("No property found for that folio via the selected source(s). Try the other source above, or open the Property Appraiser link.")
                "⬇️ Download this property (CSV)",
                data=csv_prop,
                file_name=f"property_{''.join(ch for ch in (a.get('folio') or folio) if ch.isdigit())}.csv",
                mime="text/csv",
            )
        else:
            st.info("No property found for that folio via the selected source(s). Try the other source above, or open the Property Appraiser link.")
    else:
        st.caption("Enter a 13-digit folio in the sidebar to see property details here.")

    st.subheader("Property & Zoning at Location")
    if pt_latlon:
        z = get_zoning_at_point(lon=pt_latlon[1], lat=pt_latlon[0])
        if z:
            st.success(
                f"**Zoning:** {z.get('ZONE')}  " +
                (f"*{z.get('ZONE_DESC')}*  " if z.get('ZONE_DESC') else "")+
                (f"**Overlay:** {z.get('OVLY')}  " if z.get('OVLY') else "")+
                (f"**Jurisdiction:** {z.get('ZONEMUNC')}" if z.get('ZONEMUNC') else "")
            )
        else:
            st.info("No zoning polygon found at this point (or service busy). Try moving the point or selecting a municipality.")
    else:
        st.caption("Enter an address in the sidebar to see zoning at that point.")

    st.subheader("Official Lookups")
    st.markdown(
        f"**Property Appraiser:** <a href='{LINK_PROPERTY_APPRAISER}' target='_blank'>Open search</a><br/>"
        f"<small>Use tabs to search by Address, Owner, or Folio. See <a href='{LINK_PROPERTY_APPRAISER_HELP}' target='_blank'>help</a>.</small>",
        unsafe_allow_html=True,
    )
    st.markdown(f"**Clerk of Courts (Official Records):** <a href='{LINK_CLERK_OFFICIAL_RECORDS}' target='_blank'>Open search</a>", unsafe_allow_html=True)

# --- Area summaries ---
if selected_poly:
    st.markdown("---")
    st.subheader(f"Zoning mix in **{selected_muni}**")
    df_z = get_zones_in_polygon(selected_poly)
    if not df_z.empty:
        st.dataframe(df_z, use_container_width=True, hide_index=True)
        csv_z = df_z.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download zoning table (CSV)", data=csv_z, file_name=f"{selected_muni}_zoning.csv", mime="text/csv")
    else:
        st.info("Zoning summary not available right now.")

    st.subheader(f"Recent sales in **{selected_muni}** (last {sales_window} days)")
    with st.expander("Diagnostics (service + query)"):
        st.caption("If results look empty, check the counts below to confirm live data.")
        st.write({"rings_vertices": sum(len(r) for r in selected_poly), "sales_window_days": int(sales_window)})
    df_sales = get_recent_sales_in_polygon(selected_poly, days=sales_window)
    if not df_sales.empty:
        show_cols = {
            "dateofsale_utc": "Sale Date",
            "price_1": "Price",
            "true_site_addr": "Address",
            "true_site_city": "City",
            "true_site_zip_code": "ZIP",
            "dor_desc": "Land Use",
            "subdivision": "Subdivision",
            "year_built": "Year Built",
            "lot_size": "Lot SqFt",
            "building_heated_area": "Heated SqFt",
            "true_owner1": "Owner",
            "folio": "Folio",
        }
        df_show = df_sales.rename(columns=show_cols)
        st.dataframe(df_show, use_container_width=True)
        csv_sales = df_show.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download recent sales (CSV)", data=csv_sales, file_name=f"{selected_muni}_recent_sales_{sales_window}d.csv", mime="text/csv")
        st.caption("Source: Miami-Dade Property Point View (PaGISView_gdb)")
    else:
        st.warning("No recent sales returned. This can happen if the service is caching, date math differs, or the area/time window has few records. Try increasing days or changing the municipality.")

with st.expander("📊 Planning, Research & Economic Analysis – quick links"):
    st.write("Use these official dashboards and PDFs for countywide labor market, GDP, and office market context.")
    st.link_button("Open Economic Metrics Dashboard", LINK_ECONOMIC_DASH)
    st.link_button("Planning & Research Reports", LINK_PLANNING_RESEARCH)

st.caption("Data & sources: Miami-Dade Property Appraiser • Miami-Dade GIS Open Data Hub • Miami-Dade Clerk of Courts • Planning, Research & Economic Analysis. Unofficial convenience tool.")
