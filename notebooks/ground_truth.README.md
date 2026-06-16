# Ground-truth log (all regions)

Fill in `ground_truth.csv` — one row per sidewalk segment you inspect **because
it's tied to a problem route**, in any region. Don't survey everything; record
only what a flagged/odd route makes you go look at. Delete the `EXAMPLE_*` row
once you've added real data.

Each column records reality so you can check it against what the scorer
believes (use `diagnostics.breakdown_route` / `inspect_route_map` for the model
side, and `audit_route` to find which routes are worth inspecting).

| Column | Allowed / format | Tests which model field |
|---|---|---|
| `region` | `beacon_hill` / `charlestown_sullivan` / `newmarket_massave` / `nubian_roxbury` / `other` | groups observations by area (`python -m walkability.graph.build --list-regions`) |
| `route_name` | a `PROBLEM_ROUTES` name from `problem_routes.py`, or blank | links the observation to the route that surfaced it |
| `segment_id` | short label you choose | — |
| `lat`, `lon` | decimal degrees, a point on the segment | locates it on the map |
| `street`, `side` | free text; side = `N`/`S`/`E`/`W`/`both` | — |
| `material_actual` | `concrete` / `brick` / `asphalt` / `granite` / `other` | `surface_material_score` (city `MATERIAL`; `other`→`None`) |
| `condition_actual` | `smooth` / `minor_cracks` / `heaved` / `broken` | `surface_score` (city `SCI`/100, may be stale) |
| `sidewalk_present` | `yes` / `no` / `partial` | coverage / `data_source` tier |
| `width_feel` | `narrow` / `normal` / `wide` | (not yet scored; `sidewalk_width_ft` exists raw) |
| `access_actual` | `public` / `private` / `gated` / `customers` | `foot_access` |
| `crossing_quality` | `signalized` / `marked` / `unmarked` / `none` / `na` | **nothing yet** — crossings are unused `highway=crossing` nodes |
| `subj_walkability` | `1`–`5` (5 = great) | calibration target for the composite `walk_score` |
| `notes` | free text | your reasoning / hypothesis |

## Regions
The less-walkable regions exist precisely so problems show up that Beacon Hill
never surfaces (arterials, restricted access, poor surfaces). Build/list them:

```bash
python -m walkability.graph.build --list-regions
python -m walkability.graph.build --dev --region nubian_roxbury
python notebooks/region_maps.py --region nubian_roxbury   # walk_score heatmap
```

## Workflow
1. `python notebooks/problem_routes.py --audit` → find the most-flagged routes (any region).
2. `python notebooks/problem_routes.py --inspect <route_name>` → open the inspector map; hover red edges.
3. Walk / Street-View the suspicious segment, add a row here with its `region` and `route_name`.
4. Compare your row to the model (`breakdown_route`):
   - factor is `None`/coarse tier → **data gap**
   - factor present but wrong value → **data-quality** issue
   - factor right but route still bad → **weight problem** (tune `FACTOR_WEIGHTS`/`alpha`)
5. If a row reveals a routing bug, ensure the route is in `PROBLEM_ROUTES` (tagged
   with its `region`) so it's tracked against regressions.
