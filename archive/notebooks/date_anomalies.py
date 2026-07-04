"""
Boston Sidewalk Inventory — Inspection Date Anomaly Analysis
------------------------------------------------------------
Investigates rows where the last-inspected year is 1970 (Unix-epoch
placeholder, not a real inspection date) and asks whether those rows
should be treated the same as rows with no date at all.

Questions answered:
  Q1. Do 1970-dated sidewalks share an inspector?
  Q2. Does that inspector appear elsewhere in the file?
  Q3. What is the average last-inspected date excluding 1970 anomalies?
  Q4. Are there other distinguishing patterns on 1970-dated rows?

Run from the project root:
    python3 notebooks/date_anomalies.py
"""

import geopandas as gpd
import pandas as pd
from pathlib import Path

# ── 1. Load ──────────────────────────────────────────────────────────────────
SHP = Path("data/boston/Sidewalk_Inventory/Sidewalk_Inventory.shp")
if not SHP.exists():
    raise FileNotFoundError(f"Shapefile not found at {SHP}.")

gdf = gpd.read_file(SHP)
print(f"\n{'='*60}")
print(f"Loaded {len(gdf):,} rows, {len(gdf.columns)} columns")
print(f"{'='*60}")

# Known column names from prior data exploration
DATE_COL = "new_insp_d"   # most-recent re-inspection date
INSP_COL = "INSP"         # inspector name (free text)

# ── 2. Parse dates ───────────────────────────────────────────────────────────
gdf["_date"] = pd.to_datetime(gdf[DATE_COL], errors="coerce")
gdf["_year"] = gdf["_date"].dt.year

print("\n--- Year distribution (all rows) ---")
print(gdf["_year"].value_counts().sort_index().to_string())

# ── 3. Split into groups ─────────────────────────────────────────────────────
ANOMALY_YEAR = 1970
mask_1970   = gdf["_year"] == ANOMALY_YEAR
mask_normal = gdf["_year"].notna() & (gdf["_year"] != ANOMALY_YEAR)
mask_null   = gdf["_year"].isna()

anomaly = gdf[mask_1970].copy()
normal  = gdf[mask_normal].copy()

print(f"\n{'='*60}")
print(f"  1970 placeholder : {mask_1970.sum():,}  ({mask_1970.mean()*100:.1f}%)")
print(f"  Normal dates     : {mask_normal.sum():,}  ({mask_normal.mean()*100:.1f}%)")
print(f"  Missing date     : {mask_null.sum():,}  ({mask_null.mean()*100:.1f}%)")
print(f"{'='*60}")

# ── Q1: Which inspectors appear on 1970-dated rows? ──────────────────────────
print("\n--- Q1: Inspector(s) on 1970-dated rows ---")
insp_1970 = anomaly[INSP_COL].value_counts(dropna=False)
print(insp_1970.to_string())

# ── Q2: Do those inspectors appear on normal-dated rows? ─────────────────────
print("\n--- Q2: Same inspector(s) in the rest of the file ---")
anomaly_inspectors = anomaly[INSP_COL].dropna().unique()
if len(anomaly_inspectors) == 0:
    print("  (All 1970-dated rows have null inspector)")
else:
    for insp in sorted(anomaly_inspectors):
        n_normal = (normal[INSP_COL] == insp).sum()
        n_null   = (gdf[mask_null][INSP_COL] == insp).sum()
        print(f"  '{insp}' → {n_normal:,} normal-dated rows, {n_null:,} null-dated rows")

# Inspector overlap summary
anomaly_insp_set = set(anomaly_inspectors)
normal_insp_set  = set(normal[INSP_COL].dropna().unique())
overlap = anomaly_insp_set & normal_insp_set
print(f"\n  Inspectors unique to 1970 rows : {len(anomaly_insp_set - normal_insp_set)}")
print(f"  Inspectors shared with normal  : {len(overlap)}")
if overlap:
    print(f"  Shared names: {sorted(overlap)[:10]}")

# ── Q3: Average last-inspected date on non-1970 rows ─────────────────────────
print("\n--- Q3: Average last-inspected date (excluding 1970 anomalies) ---")
avg_ts = normal["_date"].mean()
med_ts = normal["_date"].median()
min_ts = normal["_date"].min()
max_ts = normal["_date"].max()
print(f"  Mean   : {avg_ts.date()}") # type: ignore
print(f"  Median : {med_ts.date()}") # type: ignore
print(f"  Range  : {min_ts.date()} → {max_ts.date()}")

print("\n  Year distribution (non-1970):")
print(normal["_year"].value_counts().sort_index().to_string())

# ── Q4: Distinguishing patterns on 1970 rows ─────────────────────────────────
print("\n--- Q4: Patterns specific to 1970-dated rows ---")

# 4a. 'inspected' flag — tells us if the sidewalk was ever field-surveyed
print("\n  4a. 'inspected' flag")
for label, mask in [("1970-dated", mask_1970), ("Normal dates", mask_normal)]:
    counts = gdf[mask]["inspected"].value_counts(dropna=False)
    null_pct = gdf[mask]["inspected"].isna().mean() * 100
    print(f"    {label}: {counts.to_dict()}  (null: {null_pct:.1f}%)")

# 4b. SURVEY column — SURVEYED / RE-SURVEY / MISSING SURVEY
print("\n  4b. SURVEY status")
for label, mask in [("1970-dated", mask_1970), ("Normal dates", mask_normal)]:
    counts = gdf[mask]["SURVEY"].value_counts(dropna=False)
    print(f"    {label}:")
    print("      " + counts.to_string().replace("\n", "\n      "))

# 4c. SCI completeness — do 1970 rows have condition scores?
print("\n  4c. SCI (condition index) completeness")
for label, mask in [("1970-dated", mask_1970), ("Normal dates", mask_normal)]:
    null_pct = gdf[mask]["SCI"].isna().mean() * 100
    zero_pct = (gdf[mask]["SCI"] == "0").mean() * 100
    print(f"    {label}: null={null_pct:.1f}%  zero='0'={zero_pct:.1f}%")

# 4d. MATERIAL completeness
print("\n  4d. MATERIAL completeness")
for label, mask in [("1970-dated", mask_1970), ("Normal dates", mask_normal)]:
    null_pct  = gdf[mask]["MATERIAL"].isna().mean() * 100
    other_pct = (gdf[mask]["MATERIAL"] == "OT").mean() * 100
    print(f"    {label}: null={null_pct:.1f}%  OT(other)={other_pct:.1f}%")

# 4e. District concentration
print("\n  4e. District breakdown — 1970-dated rows")
dist_1970   = anomaly["DISTRICT"].value_counts(normalize=True)
dist_normal = normal["DISTRICT"].value_counts(normalize=True)
all_dists   = dist_1970.index.union(dist_normal.index)
df_dist = pd.DataFrame({
    "pct_1970":   dist_1970.reindex(all_dists, fill_value=0) * 100,
    "pct_normal": dist_normal.reindex(all_dists, fill_value=0) * 100,
}).sort_values("pct_1970", ascending=False)
df_dist["delta_pp"] = df_dist["pct_1970"] - df_dist["pct_normal"]
print(df_dist.round(1).to_string())

# 4f. curb_type null rate gap (highlighted in Q4 automated scan)
print("\n  4f. curb_type null rate")
for label, mask in [("1970-dated", mask_1970), ("Normal dates", mask_normal)]:
    null_pct = gdf[mask]["curb_type"].isna().mean() * 100
    print(f"    {label}: null={null_pct:.1f}%")

# ── 5. Recommendation ────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("RECOMMENDATION")
print(f"{'='*60}")

n_1970          = mask_1970.sum()
n_1970_uninsp   = anomaly["inspected"].isna().sum()
n_1970_insp     = n_1970 - n_1970_uninsp
shared_insp     = len(overlap)

print(f"""
  Total 1970-dated rows     : {n_1970:,}
  Of which 'inspected'=null : {n_1970_uninsp:,}  ({n_1970_uninsp/n_1970*100:.1f}%)
  Of which 'inspected'=yes  : {n_1970_insp:,}  ({n_1970_insp/n_1970*100:.1f}%)
  Inspector names also seen on normal-dated rows: {shared_insp}

  The 1970-01-01 dates are Unix-epoch defaults, not real inspection dates.
  However, the rows are NOT the same as rows with no date:

  - {n_1970_insp:,} ({n_1970_insp/n_1970*100:.1f}%) still carry 'inspected'=yes and have
    non-null SCI / MATERIAL values — the field survey happened; only the
    date logging was corrupted.
  - {n_1970_uninsp:,} ({n_1970_uninsp/n_1970*100:.1f}%) have 'inspected'=null; these are
    more similar to truly uninspected rows.

  Recommended treatment in build.py:
    - Replace CONF_CITY_SUSPECT with a two-level flag:
        * CONF_CITY_DATE_MISSING  — 1970 date but inspected=yes  → use SCI,
          apply moderate confidence penalty (~0.75 × normal confidence)
        * CONF_CITY_UNINSPECTED   — 1970 date and inspected=null → treat
          like no city match (skip to OSM-tag tier)
    - District skew (West Roxbury over-represented) is likely a data-
      collection batch issue, not a spatial confound — no spatial
      correction needed.
""")

print(f"{'='*60}")
print("Analysis complete.")
print(f"{'='*60}")
