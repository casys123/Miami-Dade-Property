# app.py
# Miami-Dade Property & Market Insights Dashboard (CSV export + Recent Sales + Folio Lookup)

import json
import requests
import pandas as pd
import streamlit as st
import folium

# Safe import: streamlit-folium may not be installed in some environments
try:
    from streamlit_folium import st_folium
except ImportError:
    st_folium = None

st.set_page_config(page_title="Miami-Dade Property & Market Insights", page_icon="🏝️", layout="wide")

# ---------------------------
# Endpoints
# ---------------------------
MD_ZONING_FEATURESERVER = "https://services.arcgis.com/LBbVDC0hKPAnLRpO/ArcGIS/rest/services/Miami_Dade_Zoning_Phillips/FeatureServer"
LAYER_MUNICIPAL_BOUNDARY = 4
LAYER_ZONING = 12

PA_GISVIEW_FEATURESERVER = "https://services.arcgis.com/8Pc9XBTAsYuxx9Ny/arcgis/rest/services/PaGISView_gdb/FeatureServer"
LAYER_PROPERTY_POINT_VIEW = 0

LINK_PROPERTY_APPRAISER = "https://www.miamidade.gov/Apps/PA/propertysearch/"
LINK_PROPERTY_APPRAISER_HELP = "https://www.miamidadepa.gov/pa/property-search-help.asp"
LINK_CLERK_OFFICIAL_RECORDS = "https://onlineservices.miamidadeclerk.gov/officialrecords"
LINK_GIS_HUB = "https://gis-mdc.opendata.arcgis.com/"
LINK_PLANNING_RESEARCH = "https://www.miamidade.gov/global/economy/planning/research-reports.page"
LINK_ECONOMIC_DASH = "https://www.miamidade.gov/global/economy/innovation-and-economic-development/economic-metrics.page"

# Direct link helper to open PA search prefilled by folio

def pa_folio_url(folio: str) -> str:
    folio = (folio or "").strip().replace("-", "")
    return f"https://www.miamidade.gov/Apps/PA/propertysearch/#/folio/{folio}" if folio else LINK_PROPERTY_APPRAISER

# ---------------------------
# Utilities
# ---------------------------

@st.cache_data(show_spinner=False, ttl=60*60)
def arcgis_query(service_url: str, layer: int, params: dict):
    """Generic ArcGIS FeatureServer query with GET/POST auto-switch.
    - Uses POST when geometry or long params (prevents 413).
    - If returnGeometry is False, we omit outSR to avoid picky servers.
    """
    base = f"{service_url}/{layer}/query"
    defaults = {
        "f": "json",
        "outFields": "*",
        "where": "1=1",
        "returnGeometry": False,
    }
    q = {**defaults, **(params or {})}
    # Only include outSR if we're actually returning geometry
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
            raise RuntimeError(str(data["error"]))
        return data
    except Exception as e:
        st.info(f"ArcGIS query unavailable (layer {layer}). Details: {e}")
        return None

# --- Geometry helpers (simplify big polygons to keep payloads small) ---

def _rdp_simplify(points, eps):
    """Douglas–Peucker on a single ring: list[(lon,lat)] → simplified ring."""
    if len(points) < 3:
        return points

    def _perp(p, a, b):
        (x, y), (x1, y1), (x2, y2) = p, a, b
        if (x1, y1) == (x2, y2):
            return ((x-x1)**2 + (y-y1)**2) ** 0.5
        t = ((x-x1)*(x2-x1) + (y-y1)*(y2-y1)) / ((x2-x1)**2 + (y2-y1)**2)
        t = max(0.0, min(1.0, t))
        proj = (x1 + t*(x2-x1), y1 + t*(y2-y1))
        return ((x-proj[0])**2 + (y-proj[1])**2) ** 0.5

    def _dp(pts):
        if len(pts) <= 2:
            return pts
        a, b = pts[0], pts[-1]
        dmax, idx = 0.0, -1
        for i in range(1, len(pts)-1):
            d = _perp(pts[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps and idx != -1:
            left = _dp(pts[:idx+1])
            right = _dp(pts[idx:])
            return left[:-1] + right
        return [a, b]

    closed = points[0] == points[-1]
    core = points[:-1] if closed else points
    simp = _dp(core)
    if closed:
        simp.append(simp[0])
    return simp

def simplify_rings(rings, tolerance_meters=25):
    eps_deg = max(1e-6, tolerance_meters / 111_320.0)
    return [_rdp_simplify(r, eps_deg) for r in rings]

@st.cache_data(show_spinner=False, ttl=60*60)
def fetch_municipalities():
    data = arcgis_query(MD_ZONING_FEATURESERVER, LAYER_MUNICIPAL_BOUNDARY, {
        "outFields": "NAME",
        "returnGeometry": True,
    })
    items = []
    if data and "features" in data:
        for f in data["features"]:
            attrs = f.get("attributes", {})
            geom = f.get("geometry", {})
            name = attrs.get("NAME") or attrs.get("Municipality") or attrs.get("municipality")
            rings = (geom or {}).get("rings")
            if not name or not rings:
                continue
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
            headers={"User-Agent": "mdc-dashboard/1.2 (Streamlit)"},
            timeout=20,
        )
        r.raise_for_status()
        results = r.json() or []
        if results:
            return (float(results[0]["lat"]), float(results[0]["lon"]))
    except Exception:
        pass
    return None

# ---------------------------
# Data accessors
# ---------------------------

@st.cache_data(show_spinner=False, ttl=60*60)
def get_property_by_folio(folio: str):
    """Return a dict with attributes for a folio from PA GIS view (robust 'where' fallbacks)."""
    if not folio:
        return None
    folio = "".join(ch for ch in folio if ch.isdigit())
    if not folio:
        return None

    fields = [
        "folio","true_site_addr","true_site_city","true_site_zip_code",
        "true_owner1","true_owner2","dor_desc","subdivision","year_built","lot_size",
        "building_heated_area","adjusted_area","actual_area","living_units","bedrooms","bathrooms","half_bathrooms","no_stories",
        "pa_primary_zone","primarylanduse_desc","mailing_address1","mailing_address2","mailing_city","mailing_state","mailing_zip"
    ]

    wheres = [
        f"folio = '{folio}'",
        f"folio = {folio}",  # in case the service typed it numeric
        f"folio LIKE '{folio}%'",
        f"parent_folio = '{folio}'",
    ]

    for w in wheres:
        params = {
            "where": w,
            "outFields": ",".join(fields),
            "returnGeometry": False,
            "sqlFormat": "standard",
        }
        data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params)
        feats = (data or {}).get("features", [])
        if feats:
            # Prefer exact folio match
            for f in feats:
                a = f.get("attributes", {})
                if str(a.get("folio", "")).replace("-", "") == folio:
                    return {"attributes": a}
            # else return first
            return {"attributes": feats[0].get("attributes", {})}

    # Try objectIds route as last resort
    params_ids = {
        "where": f"folio LIKE '{folio}%'",
        "returnIdsOnly": True,
    }
    data_ids = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params_ids)
    if data_ids and data_ids.get("objectIds"):
        oid = data_ids["objectIds"][0]
        params_oid = {
            "objectIds": oid,
            "outFields": ",".join(fields),
            "returnGeometry": False,
        }
        data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params_oid)
        feats = (data or {}).get("features", [])
        if feats:
            return {"attributes": feats[0].get("attributes", {})}

    return None

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
    if not data or not data.get("features"):
        return None
    return data["features"][0].get("attributes", {})

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
        return pd.DataFrame(columns=["ZONE", "ZONE_DESC"])
    rows = [{"ZONE": f.get("attributes", {}).get("ZONE"),
             "ZONE_DESC": f.get("attributes", {}).get("ZONE_DESC")} for f in data["features"]]
    return pd.DataFrame(rows).dropna().drop_duplicates().sort_values(by=["ZONE", "ZONE_DESC"]).reset_index(drop=True)

@st.cache_data(show_spinner=False, ttl=30*60)
def get_recent_sales_in_polygon(rings, days: int = 90, max_rows: int = 5000):
    """Fetch recent sales by polygon with pagination; filter by date client-side."""
    def _paged_query(base_params, step=2000, hard_cap=max_rows):
        rows = []
        offset = 0
        while True:
            if len(rows) >= hard_cap:
                break
            params = dict(base_params)
            params["resultRecordCount"] = min(step, hard_cap - len(rows))
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

    base_params = {
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

    attrs = _paged_query(base_params)
    if not attrs:
        return pd.DataFrame(columns=["folio","true_site_addr","true_site_city","true_site_zip_code","true_owner1","dateofsale_utc","price_1","dor_desc","subdivision","year_built","lot_size","building_heated_area"]) 

    df = pd.DataFrame(attrs)

    # Detect and parse sale date
    candidate_cols = ["dateofsale_utc", "dateofsale", "sale_date", "last_sale_date", "date_of_sale", "saledate"]
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
    folio = st.text_input("Folio Number (13 digits)")

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
            folium.Polygon(locations=[(lat, lon) for lon, lat in match["rings"][0]],
                           tooltip=selected_muni, weight=2, fill=False).add_to(m)
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
    if folio:
        prop = get_property_by_folio(folio)
        if prop:
            a = prop.get("attributes", {})
            # Top line
            st.success(f"Folio: {a.get('folio','')}")
            # Address & owners
            addr_line = a.get('true_site_addr') or ''
            city_zip = " ".join([str(a.get('true_site_city') or ''), str(a.get('true_site_zip_code') or '')]).strip()
            owner1 = a.get('true_owner1') or ''
            owner2 = a.get('true_owner2') or ''
            c1, c2 = st.columns(2)
            with c1:
                if addr_line:
                    st.markdown(f"**Property Address:** {addr_line}")
                if city_zip:
                    st.markdown(city_zip)
            with c2:
                owners = "<br/>".join([x for x in [owner1, owner2] if x])
                if owners:
                    st.markdown("**Owner(s):**  ")
                    st.markdown(owners, unsafe_allow_html=True)
            # Zoning/Use
            pa_zone = a.get('pa_primary_zone')
            use_desc = a.get('primarylanduse_desc') or a.get('dor_desc')
            subdiv = a.get('subdivision')
            parts = []
            if pa_zone: parts.append(f"**PA Primary Zone:** {pa_zone}")
            if use_desc: parts.append(f"**Primary Land Use:** {use_desc}")
            if subdiv: parts.append(f"**Subdivision:** {subdiv}")
            if parts:
                st.markdown("  • ".join(parts))
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
            st.link_button("Open in Property Appraiser", pa_folio_url(folio))
        else:
            st.info("No property found for that folio in the Open Data layer. Double-check the 13-digit folio or open the Property Appraiser search.")
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
        st.write({
            "rings_vertices": sum(len(r) for r in selected_poly),
            "sales_window_days": int(sales_window),
        })
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
        st.warning("No recent sales returned. This can happen if the service is caching, date math differs, or the area/time window has few records. Try increasing days or toggling the municipality.")

with st.expander("📊 Planning, Research & Economic Analysis – quick links"):
    st.write("Use these official dashboards and PDFs for countywide labor market, GDP, and office market context.")
    st.link_button("Open Economic Metrics Dashboard", LINK_ECONOMIC_DASH)
    st.link_button("Planning & Research Reports", LINK_PLANNING_RESEARCH)

st.caption("Data & sources: Miami-Dade Property Appraiser • Miami-Dade GIS Open Data Hub • Miami-Dade Clerk of Courts • Planning, Research & Economic Analysis. Unofficial convenience tool.")

# ---------------------------
# (Repo files below — copy each to its own file at repo root)
# ---------------------------

# requirements.txt (create this file at repo root)
# -----------------------------------------------
# streamlit==1.37.1
# streamlit-folium==0.20.0
# folium==0.17.0
# pandas==2.2.2
# requests==2.32.3

# runtime.txt (create this file at repo root)
# ------------------------------------------
# python-3.11.9

# .streamlit/config.toml (make a folder named .streamlit and put this inside)
# -----------------------------------------------------
# [server]
# headless = true
# port = 8501
# enableCORS = false
# enableXsrfProtection = true
#
# [browser]
# gatherUsageStats = false
#
# [theme]
# primaryColor = "#0066CC"
# backgroundColor = "#FFFFFF"
# secondaryBackgroundColor = "#F6F8FA"
# textColor = "#0F1419"
# font = "sans serif"
