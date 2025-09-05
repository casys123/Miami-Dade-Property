# app.py
# Miami-Dade Property & Market Insights Dashboard (Tabbed UI)
# - Folio lookup (MapServer first, FeatureServer fallback)
# - Zoning mix by municipality
# - Recent sales inside selected area
# - CSV exports + bulk folio lookup
# - Cleaner UI with tabs and improved errors / retries

import json
import time
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
import folium

try:
    from streamlit_folium import st_folium
except ImportError:
    st_folium = None

# ---------------------------
# Streamlit page config
# ---------------------------
st.set_page_config(
    page_title="Miami-Dade Property & Market Insights",
    page_icon="🏝️",
    layout="wide"
)

st.title("🏝️ Miami-Dade Property & Market Insights")
st.caption("Powered by Miami-Dade County Open Data & official portals.")

# ---------------------------
# Endpoints & Layers
# ---------------------------
MD_ZONING_FEATURESERVER = "https://services.arcgis.com/LBbVDC0hKPAnLRpO/ArcGIS/rest/services/Miami_Dade_Zoning_Phillips/FeatureServer"
LAYER_MUNICIPAL_BOUNDARY = 4
LAYER_ZONING = 12

PA_MAPSERVER = "https://gisweb.miamidade.gov/arcgis/rest/services/MD_Emaps/MapServer"
LAYER_PROPERTY_RECORDS = 70

PA_GISVIEW_FEATURESERVER = "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/PaGISView_gdb/FeatureServer"
LAYER_PROPERTY_POINT_VIEW = 0

LINK_PROPERTY_APPRAISER = "https://www.miamidade.gov/Apps/PA/propertysearch/"
LINK_PROPERTY_APPRAISER_HELP = "https://www.miamidadepa.gov/pa/property-search-help.asp"
LINK_CLERK_OFFICIAL_RECORDS = "https://onlineservices.miamidadeclerk.gov/officialrecords"
LINK_GIS_HUB = "https://gis-mdc.opendata.arcgis.com/"
LINK_PLANNING_RESEARCH = "https://www.miamidade.gov/global/economy/planning/research-reports.page"
LINK_ECONOMIC_DASH = "https://www.miamidade.gov/global/economy/innovation-and-economic-development/economic-metrics.page"

# ---------------------------
# Networking (session + retries)
# ---------------------------

@st.cache_resource(show_spinner=False)
def get_http() -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    retry = Retry(
        total=4,
        read=4,
        connect=4,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"])
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        # Nominatim requires identifying UA. Keep it generic, no secrets here.
        "User-Agent": "mdc-insights/1.4 (+info@miamimasterflooring.com)"
    })
    return s

HTTP = get_http()

# ---------------------------
# Helpers
# ---------------------------

def format_md_folio(input_str: str) -> Tuple[str, str]:
    """Return (hyphenated, digits) for a Miami-Dade folio input."""
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
def arcgis_query(service_url: str, layer: int, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Generic ArcGIS FeatureServer/MapServer query with retries + error handling."""
    base = f"{service_url}/{layer}/query"
    defaults = {"f": "json", "where": "1=1", "outFields": "*", "returnGeometry": False}
    q = {**defaults, **(params or {})}
    if q.get("returnGeometry"):
        q.setdefault("outSR", 4326)

    try:
        payload_str = json.dumps(q, separators=(",", ":"), ensure_ascii=False)
        use_post = ("geometry" in q) or (len(base) + len(payload_str) > 1800)

        if use_post:
            r = HTTP.post(
                base,
                data=q,
                timeout=30,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
        else:
            r = HTTP.get(base, params=q, timeout=25)

        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            # Normalize ArcGIS error into a single message
            err = data["error"]
            msg = err.get("message") or err
            st.info(f"ArcGIS error (layer {layer}): {msg}")
            return None
        return data
    except Exception as e:
        st.info(f"ArcGIS query unavailable (layer {layer}). Details: {e}")
        return None

# Geometry simplification (Douglas–Peucker)
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
        if not ring:
            continue
        closed = ring[0] == ring[-1]
        core = ring[:-1] if closed and len(ring) > 1 else ring
        simp = _dp(core, eps_deg)
        if closed and simp:
            # ensure closure stays intact
            if simp[0] != simp[-1]:
                simp.append(simp[0])
        out.append(simp)
    return out

# ---------------------------
# Data accessors
# ---------------------------

@st.cache_data(show_spinner=False, ttl=60*60)
def normalize_folios(text: str) -> List[str]:
    """Parse pasted text and return unique, normalized 13-digit folios (hyphenated)."""
    if not text:
        return []
    text = (
        text.replace("\r", " ")
            .replace("\n", " ")
            .replace(",", " ")
            .replace(";", " ")
            .replace("\t", " ")
    )
    digits_list = re.findall(r"\d{13}", text)
    out, seen = [], set()
    for d in digits_list:
        hyph = f"{d[0:2]}-{d[2:6]}-{d[6:9]}-{d[9:13]}"
        if d not in seen:
            out.append(hyph)
            seen.add(d)
    return out

@st.cache_data(show_spinner=False, ttl=60*60)
def fetch_municipalities():
    data = arcgis_query(MD_ZONING_FEATURESERVER, LAYER_MUNICIPAL_BOUNDARY, {"outFields":"NAME","returnGeometry":True})
    items = []
    if data and data.get("features"):
        for f in data["features"]:
            a = f.get("attributes", {}) or {}
            g = f.get("geometry", {}) or {}
            name = a.get("NAME") or a.get("Municipality") or a.get("municipality")
            rings = (g or {}).get("rings") or []
            # Keep first exterior ring for robustness
            if name and rings and rings[0]:
                items.append({"name": name, "rings": [rings[0]]})
    return sorted(items, key=lambda x: x["name"]) if items else []

@st.cache_data(show_spinner=False, ttl=60*60)
def geocode_address(addr: str):
    if not addr:
        return None
    try:
        r = HTTP.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": addr, "format": "json", "limit": 1},
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
        r = HTTP.get(f"{service_url}/{layer}", params={"f":"json"}, timeout=20)
        r.raise_for_status()
        meta = r.json()
        fields = meta.get("fields", []) if isinstance(meta, dict) else []
        return {(f.get("name") or "").lower(): f for f in fields}
    except Exception:
        return {}

@st.cache_data(show_spinner=False, ttl=60*60)
def get_property_by_folio(folio: str, prefer_mapserver: bool = True):
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
            # LIKE as a last resort
            wheres.append(f"FOLIO LIKE '%{esc(digits)}%'")
        for w in wheres:
            data = arcgis_query(PA_MAPSERVER, LAYER_PROPERTY_RECORDS, {
                "where": w, "outFields": out_fields, "returnGeometry": False, "resultRecordCount": 5
            })
            feats = (data or {}).get("features", [])
            if feats:
                a = feats[0].get("attributes", {}) or {}
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
                a = feats[0].get("attributes", {}) or {}
                return {"attributes": a, "source": "FeatureServer/0", "where_used": w}
        return None

    first = _query_mapserver70 if prefer_mapserver else _query_featureserver0
    second = _query_featureserver0 if prefer_mapserver else _query_mapserver70
    return first() or second()

@st.cache_data(show_spinner=False, ttl=30*60)
def bulk_properties_by_folios(folio_list, prefer_mapserver: bool = True, sleep_sec: float = 0.15):
    rows = []
    for fol in folio_list:
        hyph, digits = format_md_folio(fol)
        key = hyph or digits or str(fol)
        try:
            res = get_property_by_folio(key, prefer_mapserver=prefer_mapserver)
            if res and res.get("attributes"):
                a = res["attributes"]
                rows.append({
                    "Folio": a.get('folio') or key,
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
                    "PA Folio URL": pa_folio_url(a.get('folio') or key),
                    "Source": res.get("source"),
                    "Query": res.get("where_used"),
                    "Status": "OK",
                })
            else:
                rows.append({"Folio": key, "Status": "NOT FOUND"})
        except Exception as e:
            rows.append({"Folio": key, "Status": f"ERROR: {e}"})
        time.sleep(max(0.0, sleep_sec))
    df = pd.DataFrame(rows)
    # De-dupe by Folio if present
    if not df.empty and "Folio" in df.columns:
        df = df.drop_duplicates(subset=["Folio"], keep="first")
    return df

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
    if not rings:
        return pd.DataFrame(columns=["ZONE","ZONE_DESC"])
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
        a = f.get("attributes", {}) or {}
        rows.append({"ZONE": a.get("ZONE"), "ZONE_DESC": a.get("ZONE_DESC")})
    df = pd.DataFrame(rows).dropna().drop_duplicates()
    if df.empty:
        return pd.DataFrame(columns=["ZONE","ZONE_DESC"])
    return df.sort_values(by=["ZONE","ZONE_DESC"]).reset_index(drop=True)

@st.cache_data(show_spinner=False, ttl=30*60)
def get_recent_sales_in_polygon(rings, days: int = 90, max_rows: int = 5000):
    if not rings:
        return pd.DataFrame(columns=["folio","true_site_addr","true_site_city","true_site_zip_code","true_owner1","dateofsale_utc","price_1","dor_desc","subdivision","year_built","lot_size","building_heated_area"])

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
    # Robust date handling
    candidate_cols = ["dateofsale_utc","dateofsale","sale_date","last_sale_date","date_of_sale","saledate"]
    date_col = next((c for c in candidate_cols if c in df.columns), None)
    if date_col:
        s = df[date_col]
        if pd.api.types.is_numeric_dtype(s):
            df[date_col] = pd.to_datetime(s, unit="ms", utc=True, errors="coerce")
        else:
            df[date_col] = pd.to_datetime(s, utc=True, errors="coerce")
        cutoff = pd.Timestamp.utcnow().tz_localize("UTC") - pd.Timedelta(days=int(days))
        df = df[df[date_col] >= cutoff]
        if date_col != "dateofsale_utc":
            df.rename(columns={date_col: "dateofsale_utc"}, inplace=True)

    if "price_1" in df.columns:
        df["price_1"] = pd.to_numeric(df["price_1"], errors="coerce")

    if "dateofsale_utc" in df.columns:
        try:
            # Normalize to naive UTC for display/sorting consistency
            df["dateofsale_utc"] = pd.to_datetime(df["dateofsale_utc"], utc=True, errors="coerce").dt.tz_convert("UTC").dt.tz_localize(None)
        except Exception:
            pass
        df = df.sort_values("dateofsale_utc", ascending=False)

    return df.reset_index(drop=True)

# ---------------------------
# UI – Sidebar
# ---------------------------

with st.sidebar:
    st.header("Filters")
    muni_items = fetch_municipalities()
    muni_names = [it["name"] for it in muni_items] if muni_items else []
    selected_muni = st.selectbox("Select Municipality / Area", options=["(none)"] + muni_names, index=0)

    st.markdown("**Look up specific properties** (opens official sites in a new tab):")
    addr = st.text_input("Address (for map & Property Appraiser link)")
    owner = st.text_input("Owner Name (for Property Appraiser & Clerk search)")  # reserved for future external links
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

# ---------------------------
# Map (always visible)
# ---------------------------

def _fit_map_to_ring(m: folium.Map, ring_xy: List[Tuple[float, float]]):
    if not ring_xy:
        return
    try:
        lats = [lat for lon, lat in ring_xy]
        lons = [lon for lon, lat in ring_xy]
        if lats and lons and (min(lats) != max(lats)) and (min(lons) != max(lons)):
            m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])
    except Exception:
        pass

map_col = st.container()
with map_col:
    m = folium.Map(location=[25.774, -80.193], zoom_start=10, control_scale=True)
    selected_poly = None

    if selected_muni != "(none)" and muni_items:
        match = next((it for it in muni_items if it["name"] == selected_muni), None)
        if match:
            selected_poly = match["rings"]
            # ArcGIS rings are [x, y] => [lon, lat]
            ring_xy = match["rings"][0]
            folium.Polygon(
                locations=[(lat, lon) for lon, lat in ring_xy],
                tooltip=selected_muni, weight=2, fill=False
            ).add_to(m)
            _fit_map_to_ring(m, ring_xy)

    pt_latlon = None
    if addr:
        loc = geocode_address(addr)
        if loc:
            pt_latlon = loc
            folium.Marker(location=[loc[0], loc[1]], tooltip=addr).add_to(m)
            if not selected_poly:
                # Center if no polygon selected
                m.location = [loc[0], loc[1]]
                m.zoom_start = 15

    if st_folium:
        st_folium(m, width=None, height=480)
    else:
        st.warning("streamlit-folium not installed. Map preview disabled. Add `streamlit-folium` to requirements.txt.")

# ---------------------------
# Tabs
# ---------------------------
tab_prop, tab_bulk, tab_area = st.tabs(["🏠 Property Info", "📦 Bulk Search", "🗺️ Area Insights"])

with tab_prop:
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
            a = result["attributes"] or {}
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

            # Use & zoning labels
            pa_zone = a.get('pa_primary_zone')
            use_desc = a.get('primarylanduse_desc') or a.get('dor_desc')
            subdiv = a.get('subdivision')
            parts = []
            if pa_zone:  parts.append(f"**PA Primary Zone:** {pa_zone}")
            if use_desc: parts.append(f"**Primary Land Use:** {use_desc}")
            if subdiv:   parts.append(f"**Subdivision:** {subdiv}")
            if parts:    st.markdown("  • ".join(parts))

            # Building details
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

            # Links
            st.link_button("Open in Property Appraiser (Folio tab)", pa_folio_url(a.get('folio') or folio))

            # Diagnostics
            with st.expander("Folio lookup diagnostics"):
                st.write({"source": result.get("source"), "where_used": result.get("where_used")})

            # CSV export (single)
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
            st.download_button(
                "⬇️ Download this property (CSV)",
                data=df_prop.to_csv(index=False).encode('utf-8'),
                file_name=f"property_{''.join(ch for ch in (a.get('folio') or folio) if ch.isdigit())}.csv",
                mime="text/csv",
            )
        else:
            st.error("No property found for that folio via the selected source(s).")
            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("Retry with other source"):
                    prefer_mapserver = not prefer_mapserver
                    st.rerun()
            with cc2:
                st.link_button("Open Property Appraiser (Folio tab)", pa_folio_url(folio))
            st.caption("Tips: ensure 13 digits; try both formats (xx-xxxx-xxx-xxxx and 13 digits).")
    else:
        st.info("Enter a 13-digit folio in the sidebar to see property details here.")

with tab_bulk:
    st.subheader("Bulk folio lookup & CSV export")
    src_choice_bulk = st.radio(
        "Data source order",
        ["MapServer first (recommended)", "FeatureServer first (fallback)"],
        horizontal=True,
        index=0,
        key="bulk_source_choice",
    )
    prefer_mapserver_bulk = (src_choice_bulk == "MapServer first (recommended)")

    st.caption("Paste multiple folios (digits or hyphenated), separated by newlines/commas/semicolons/tabs — or upload a CSV with a column named 'folio'.")
    bulk_text = st.text_area(
        "Paste folios here",
        height=140,
        placeholder="3530070191100\n0131234567890\n01-2345-678-9012, 1133260050000",
    )
    upload = st.file_uploader("…or upload a CSV with a 'folio' column", type=["csv"])

    input_folios: List[str] = []
    if bulk_text:
        input_folios.extend(normalize_folios(bulk_text))
    if upload is not None:
        try:
            df_up = pd.read_csv(upload)
            if 'folio' in df_up.columns:
                for v in df_up['folio'].astype(str).tolist():
                    h, d = format_md_folio(v)
                    input_folios.append(h or d)
            else:
                st.warning("No 'folio' column found in uploaded CSV.")
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    # de-dupe while preserving order
    input_folios = list(dict.fromkeys([f for f in input_folios if f]))

    if input_folios:
        st.write(f"Detected **{len(input_folios)}** folio(s).")
    else:
        st.caption("Provide folios above to enable bulk lookup.")

    if input_folios and st.button("Run bulk lookup"):
        with st.spinner("Fetching properties…"):
            df_bulk = bulk_properties_by_folios(input_folios, prefer_mapserver=prefer_mapserver_bulk)
        if not df_bulk.empty:
            st.dataframe(df_bulk, use_container_width=True)
            st.download_button(
                "⬇️ Download bulk results (CSV)",
                data=df_bulk.to_csv(index=False).encode('utf-8'),
                file_name="mdc_properties_bulk.csv",
                mime="text/csv"
            )
        else:
            st.info("No results returned for the provided folios.")

with tab_area:
    st.subheader("Area insights (zoning & sales)")
    if selected_muni and selected_muni != "(none)":
        # Zoning
        st.markdown(f"**Zoning mix in {selected_muni}**")
        df_z = get_zones_in_polygon(selected_poly) if selected_poly else pd.DataFrame()
        if not df_z.empty:
            st.dataframe(df_z, use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Download zoning table (CSV)",
                data=df_z.to_csv(index=False).encode("utf-8"),
                file_name=f"{selected_muni}_zoning.csv",
                mime="text/csv"
            )
        else:
            st.info("Zoning summary not available right now.")

        # Sales
        st.markdown(f"**Recent sales in {selected_muni}** (last {int(sales_window)} days)")
        with st.expander("Diagnostics (service + query)"):
            st.caption("If results look empty, check the counts below to confirm live data.")
            st.write({
                "rings_vertices": sum(len(r) for r in selected_poly) if selected_poly else 0,
                "sales_window_days": int(sales_window)
            })

        df_sales = get_recent_sales_in_polygon(selected_poly, days=sales_window) if selected_poly else pd.DataFrame()
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
            st.download_button(
                "⬇️ Download recent sales (CSV)",
                data=df_show.to_csv(index=False).encode("utf-8"),
                file_name=f"{selected_muni}_recent_sales_{int(sales_window)}d.csv",
                mime="text/csv"
            )
            st.caption("Source: Miami-Dade Property Point View (PaGISView_gdb)")
        else:
            st.warning("No recent sales returned. Try increasing days or selecting a different municipality.")
    else:
        st.info("Pick a municipality in the left sidebar to see its zoning mix and recent sales.")

with st.expander("📊 Planning, Research & Economic Analysis – quick links"):
    st.write("Use these official dashboards and PDFs for countywide labor market, GDP, and office market context.")
    st.link_button("Open Economic Metrics Dashboard", LINK_ECONOMIC_DASH)
    st.link_button("Planning & Research Reports", LINK_PLANNING_RESEARCH)

st.caption("Data & sources: Miami-Dade Property Appraiser • Miami-Dade GIS Open Data Hub • Miami-Dade Clerk of Courts • Planning, Research & Economic Analysis. Unofficial convenience tool.")
