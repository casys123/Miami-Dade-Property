# app.py
# Miami-Dade Property & Market Insights Dashboard (CSV export + Recent Sales)

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

def _rdp_simplify(points, eps):
    """Douglas–Peucker polyline simplification for a ring (list of (lon,lat)).
    eps is tolerance in degrees (~ meters/111_320)."""
    if len(points) < 3:
        return points
    # perpendicular distance from point p to line ab
    def _perp(p, a, b):
        (x, y), (x1, y1), (x2, y2) = p, a, b
        if (x1, y1) == (x2, y2):
            return ((x-x1)**2 + (y-y1)**2) ** 0.5
        t = ((x-x1)*(x2-x1) + (y-y1)*(y2-y1)) / ((x2-x1)**2 + (y2-y1)**2)
        t = max(0.0, min(1.0, t))
        proj = (x1 + t*(x2-x1), y1 + t*(y2-y1))
        return ((x-proj[0])**2 + (y-proj[1])**2) ** 0.5
    # recursive DP
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
        else:
            return [a, b]
    # Ensure closed ring (repeat first point at end)
    closed = points[0] == points[-1]
    core = points[:-1] if closed else points
    simp = _dp(core)
    if closed:
        simp.append(simp[0])
    return simp

def simplify_rings(rings, tolerance_meters=20):
    """Return simplified rings using a Douglas–Peucker tolerance in meters."""
    # crude deg per meter conversion near Miami (~ 1 deg lat ≈ 111_320 m)
    eps_deg = max(1e-6, tolerance_meters / 111_320.0)
    out = []
    for ring in rings:
        out.append(_rdp_simplify(ring, eps_deg))
    return out
@st.cache_data(show_spinner=False, ttl=60*60)
def arcgis_query(service_url: str, layer: int, params: dict):
    base = f"{service_url}/{layer}/query"
    defaults = {
        "f": "json",
        "outFields": "*",
        "where": "1=1",
        "returnGeometry": True,
        "outSR": 4326,
    }
    q = {**defaults, **(params or {})}
    try:
        # Use POST when geometry is present or URL would be too long to avoid 413 errors
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
            if not name:
                continue
            rings = geom.get("rings")
            if not rings:
                continue
            first_poly = rings[0]
            items.append({"name": name, "rings": [first_poly]})
    return sorted(items, key=lambda x: x["name"]) if items else []

@st.cache_data(show_spinner=False, ttl=60*60)
def geocode_address(addr: str):
    if not addr:
        return None
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": addr, "format": "json", "limit": 1},
            headers={"User-Agent": "mdc-dashboard/1.1 (Streamlit)"},
            timeout=20,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            return (lat, lon)
        return None
    except Exception:
        return None

@st.cache_data(show_spinner=False, ttl=60*60)
def get_property_by_folio(folio: str):
    """Return core attributes for a folio from the Property Appraiser GIS view."""
    if not folio:
        return pd.DataFrame()
    folio = folio.strip().replace("-", "")
    params = {
        "where": f"folio = '{folio}'",
        "outFields": ",".join([
            "folio","true_site_addr","true_site_city","true_site_zip_code",
            "true_owner1","true_owner2","dor_desc","subdivision","year_built","lot_size",
            "building_heated_area","adjusted_area","actual_area","living_units","bedrooms","bathrooms","half_bathrooms","no_stories",
            "pa_primary_zone","primarylanduse_desc"
        ]),
        "returnGeometry": False,
    }
    data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params)
    feats = (data or {}).get("features", [])
    if not feats:
        return pd.DataFrame()
    row = feats[0].get("attributes", {})
    row["beds"] = row.get("bedrooms")
    row["baths"] = row.get("bathrooms")
    row["half_baths"] = row.get("half_bathrooms")
    row["primary_land_use"] = row.get("primarylanduse_desc") or row.get("dor_desc")
    return pd.DataFrame([row])

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
    # Proactively simplify large polygons to avoid 413 on ArcGIS (URL/body too big)
    orig_len = sum(len(r) for r in rings)
    use_rings = rings
    if orig_len > 1500:
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
    rows = []
    for f in data["features"]:
        a = f.get("attributes", {})
        rows.append({"ZONE": a.get("ZONE"), "ZONE_DESC": a.get("ZONE_DESC")})
    return pd.DataFrame(rows).dropna().drop_duplicates().sort_values(by=["ZONE", "ZONE_DESC"]).reset_index(drop=True)

@st.cache_data(show_spinner=False, ttl=30*60)
def get_recent_sales_in_polygon(rings, days: int = 90, max_rows: int = 5000):
    """Recent sales inside polygon.
    Strategy:
      1) Spatial filter by polygon (POST)
      2) Pull up to `max_rows` with pagination (no fragile server-side date WHERE)
      3) Auto-detect a sale date field (prefers 'dateofsale_utc') and filter client-side
    """
    # --- Helper: paginate ArcGIS queries ---
    def _paged_query(base_params, step=2000, hard_cap=max_rows):
        rows = []
        offset = 0
        while True:
            params = dict(base_params)
            params["resultRecordCount"] = min(step, hard_cap - len(rows))
            params["resultOffset"] = offset
            data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params)
            feats = (data or {}).get("features", [])
            if not feats:
                break
            for f in feats:
                rows.append(f.get("attributes", {}))
            if len(rows) >= hard_cap or len(feats) < params["resultRecordCount"]:
                break
            offset += params["resultRecordCount"]
        return rows

    # --- Simplify polygon if huge to avoid 413s ---
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
        # Avoid relying on a specific field name for ordering
        # We'll sort locally after parsing dates
        "geometryPrecision": 6,
    }

    attrs = _paged_query(base_params)
    if not attrs:
        return pd.DataFrame(columns=["folio","true_site_addr","true_site_city","true_site_zip_code","true_owner1","dateofsale_utc","price_1","dor_desc","subdivision","year_built","lot_size","building_heated_area"]) 

    df = pd.DataFrame(attrs)

    # --- Pick a sale date column robustly ---
    candidate_cols = [
        "dateofsale_utc", "dateofsale", "sale_date", "last_sale_date", "date_of_sale", "saledate"
    ]
    date_col = next((c for c in candidate_cols if c in df.columns), None)
    if date_col is None:
        # fallback: heuristic - first column that contains both 'date' and 'sale'
        for c in df.columns:
            lc = c.lower()
            if "date" in lc and "sale" in lc:
                date_col = c
                break
    # Parse dates if we found any
    if date_col:
        s = df[date_col]
        if pd.api.types.is_numeric_dtype(s):
            df[date_col] = pd.to_datetime(s, unit="ms", utc=True, errors="coerce")
        else:
            df[date_col] = pd.to_datetime(s, utc=True, errors="coerce")
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(days))
        df = df[df[date_col] >= cutoff]
        # Rename to common name for UI
        if date_col != "dateofsale_utc":
            df.rename(columns={date_col: "dateofsale_utc"}, inplace=True)
    else:
        # No date field detected; skip date filtering
        pass

    # Normalize price
    if "price_1" in df.columns:
        df["price_1"] = pd.to_numeric(df["price_1"], errors="coerce")

    # Friendly display date (drop tz)
    if "dateofsale_utc" in df.columns:
        try:
            df["dateofsale_utc"] = pd.to_datetime(df["dateofsale_utc"], utc=True, errors="coerce").dt.tz_convert("UTC").dt.tz_localize(None)
        except Exception:
            pass

    # Sort newest first if we have the date
    if "dateofsale_utc" in df.columns:
        df = df.sort_values("dateofsale_utc", ascending=False)

    return df.reset_index(drop=True)

# ---------------------------
# Folio Lookup helpers
# ---------------------------

def _only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

@st.cache_data(show_spinner=False, ttl=30*60)
def get_property_by_folio(folio_str: str):
    folio_num = _only_digits(folio_str)
    if not folio_num:
        return None
    params = {
        "where": f"folio='{folio_num}'",
        "outFields": "*",
        "returnGeometry": True,
    }
    data = arcgis_query(PA_GISVIEW_FEATURESERVER, LAYER_PROPERTY_POINT_VIEW, params)
    if not data or not data.get("features"):
        return None
    feat = data["features"][0]
    attrs = feat.get("attributes", {})
    geom = feat.get("geometry") or {}
    # Try zoning at the property point if we have geometry
    zoning = None
    try:
        if geom and {k for k in geom.keys() if k in ("x","y")}:
            lon = geom.get("x"); lat = geom.get("y")
            if lon is not None and lat is not None:
                zoning = get_zoning_at_point(lon=float(lon), lat=float(lat))
    except Exception:
        pass
    return {"attributes": attrs, "geometry": geom, "zoning": zoning}

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

with col_info:
    st.subheader("Property by Folio")
    if 'folio' in locals() and folio:
        df_prop = get_property_by_folio(folio)
        if not df_prop.empty:
            r = df_prop.iloc[0].to_dict()
            st.success(f"**Folio:** {r.get('folio','')}")
            addr_line = r.get('true_site_addr') or ''
            city_zip = " ".join([str(r.get('true_site_city') or ''), str(r.get('true_site_zip_code') or '')]).strip()
            owner1 = r.get('true_owner1') or ''
            owner2 = r.get('true_owner2') or ''
            cols = st.columns(2)
            with cols[0]:
                if addr_line:
                    st.markdown(f"**Property Address**  
{addr_line}")
                if city_zip:
                    st.markdown(city_zip)
            with cols[1]:
                if owner1 or owner2:
                    st.markdown("**Owner**  ")
                    st.markdown("<br/>".join([x for x in [owner1, owner2] if x]), unsafe_allow_html=True)
            st.markdown(
                (f"**PA Primary Zone:** {r.get('pa_primary_zone')}  " if r.get('pa_primary_zone') else "")+
                (f"**Primary Land Use:** {r.get('primary_land_use')}  " if r.get('primary_land_use') else "")+
                (f"**Subdivision:** {r.get('subdivision') if r.get('subdivision') else ''}")
            )
            kmap = {
                "beds": "Beds",
                "baths": "Baths",
                "half_baths": "Half Baths",
                "no_stories": "Floors",
                "living_units": "Living Units",
                "actual_area": "Actual Area (SqFt)",
                "building_heated_area": "Living Area (SqFt)",
                "adjusted_area": "Adjusted Area (SqFt)",
                "lot_size": "Lot Size (SqFt)",
                "year_built": "Year Built",
            }
            disp = {v: r.get(k) for k,v in kmap.items() if r.get(k) is not None}
            if disp:
                df_disp = pd.DataFrame([disp]).T.reset_index()
                df_disp.columns = ["Attribute", "Value"]
                st.dataframe(df_disp, use_container_width=True, hide_index=True)
            st.link_button("Open in Property Appraiser", pa_folio_url(folio))
        else:
            st.info("No property found for that folio in the Open Data layer. Double-check the 13-digit folio or open the Property Appraiser search.")
    else:
        st.caption("Enter a 13-digit folio in the sidebar to see property details here.")

    
    st.subheader("Lookup by Folio")
    if folio:
        prop = get_property_by_folio(folio)
        if prop:
            a = prop.get("attributes", {})
            z = prop.get("zoning") or {}
            # Compose owner lines
            owners = ", ".join([x for x in [a.get("true_owner1"), a.get("true_owner2"), a.get("true_owner3"), a.get("true_owner4")] if x]) or "—"
            mailing_bits = [a.get("mailing_address1"), a.get("mailing_address2"), a.get("mailing_city"), a.get("mailing_state"), a.get("mailing_zip")]
            mailing = ", ".join([b for b in mailing_bits if b]) or "—"
            # Basic facts
            facts = {
                "Folio": a.get("folio"),
                "Subdivision": a.get("subdivision") or "—",
                "Property Address": a.get("true_site_addr") or "—",
                "City/ZIP": ", ".join([b for b in [a.get("true_site_city"), a.get("true_site_zip_code")] if b]) or "—",
                "Owner(s)": owners,
                "Mailing Address": mailing,
                "PA Primary Zone": (z.get("ZONE") if z else a.get("pa_primary_zone")) or "—",
                "Primary Land Use": a.get("dor_desc") or "—",
                "Beds / Baths / Half": " / ".join([
                    str(a.get("bedrooms")) if a.get("bedrooms") is not None else "-",
                    str(a.get("bathrooms")) if a.get("bathrooms") is not None else "-",
                    str(a.get("half_bath")) if a.get("half_bath") is not None else "-",
                ]),
                "Floors": a.get("stories") or a.get("floors") or "—",
                "Living Units": a.get("num_units") or a.get("living_units") or "—",
                "Actual Area": (f"{int(a.get('actual_area')):,} Sq.Ft" if a.get("actual_area") else "—"),
                "Living Area": (f"{int(a.get('building_heated_area')):,} Sq.Ft" if a.get("building_heated_area") else (f"{int(a.get('living_area')):,} Sq.Ft" if a.get("living_area") else "—")),
                "Adjusted Area": (f"{int(a.get('adjusted_area')):,} Sq.Ft" if a.get("adjusted_area") else "—"),
                "Lot Size": (f"{int(a.get('lot_size')):,} Sq.Ft" if a.get("lot_size") else "—"),
                "Year Built": a.get("year_built") or "—",
            }
            st.json(facts, expanded=False)
            # Useful links
            if a.get("folio"):
                folio_link = f"{LINK_PROPERTY_APPRAISER}?searchOption=folio&searchValue={a.get('folio')}"
                st.link_button("Open in Property Appraiser", folio_link)
                st.link_button("Search Official Records (Clerk)", LINK_CLERK_OFFICIAL_RECORDS)
        else:
            st.warning("No property found for that folio. Make sure it's 13 digits (numbers only).")
    else:
        st.caption("Enter a folio number in the sidebar to fetch property details.")

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
# Web app & viz
# streamlit==1.37.1
# streamlit-folium==0.20.0
# folium==0.17.0
#
# Data
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
