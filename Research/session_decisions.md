I used a coding agent to take Humanpath — a walkability-aware pedestrian router
for Boston — from scoring changes to a deployed web app. What I'm proud of is the
decision-making: I set the priorities (sensible routes and a clean UI over perfect
metrics), made the architectural calls (a worst-segment-aware route score; not
penalizing forced customers-only endpoints like a zoo entrance), and drove a
research-backed performance fix — when long routes were slow, I had the agent
profile it, research how Google/Apple route, and replace Yen's k-shortest-paths
with A* + penalty-method alternatives using an admissible heuristic, verified exact
against Dijkstra (5 km: 2.4 s → 0.5 s). I iterated hard on the UI, cut features that
didn't earn their place, kept an invariant test suite green, and shipped to a public
repo and live deploy.

# Session decisions & pushbacks

A log of the **decisions and course-corrections I (the project owner) made** during the
build-out of the scoring/routing changes, the "Humanpath" UI, and deployment — focused
on the choices and pushbacks rather than implementation detail.

## Scoring & routing intent

- **Worst-segment penalty — reported score only.** A route's overall walkability should
  be dragged down by its worst block, but I decided this should change only the
  *reported* score, **not** the routing/cost (kept routing as-is). → length-weighted
  power mean.
- **Don't penalize "forced" sections.** A customers-only path you'd legitimately use at
  your own destination (the "zoo entrance") shouldn't count against the route. I scoped
  this to **terminal edges only** for now, deferring broader "forced vs changeable"
  detection.
- **Priority call:** I explicitly wanted *every route to make sense with reasonable
  scores* and a **presentable UI first**, over perfecting the metrics.
- **Sequencing:** I chose **scoring fixes first, then the UI** (so the demo shows the
  improved numbers).

## Routing performance — Yen's → A\*

- I noticed long routes were slow (~5 km took >5 s vs <2 s for ~1.3 km) and suspected the
  routing engine, not the address lookup.
- I asked to **research how Google/Apple Maps route** before choosing a fix (→ contraction
  hierarchies / customizable route planning; A\* + alternatives as the pragmatic tier).
- I asked for a **conceptual explanation of A\*** before approving it.
- **Decision:** replace **Yen's k-shortest-paths with A\* + penalty-method alternatives**
  (Tier 1). Documented Customizable Contraction Hierarchies as the long-term Tier 3 TODO.

## UI / "Humanpath" app

- **Provided my own design direction** (`app/Footpath Atlas.html`) — warm editorial look.
- **Clean & legible over features.** I asked to favor clarity, **dropped the click-on-map
  and lat/lon inputs** (address-only), and wanted **route specifics hidden by default**
  (revealed in a "Details" expander).
- **Map clicking saga (several pushbacks):**
  - Click-on-map felt clunky → I told you to **forget click input and focus on addresses**.
  - "Show on map" should display the route directly, not require opening Details.
  - Camera behavior: rejected the **whole-screen reset/flash**; first said I preferred the
    camera *lunging to the click* over a flash, then reversed to wanting **no camera move
    except on Find/Update**, and finally the camera should **fit the focused route** (never
    snap to the city default).
  - **Fine-tune sliders must not redraw the map** until *Update routes* is clicked.
- **Zoom (several pushbacks):** too slow → too fast → "clunky 4-step" → too slow again. I
  asked to research the traditional approach; the **Leaflet smooth-zoom plugin didn't work
  in the iframe** (broke trackpad zoom entirely) → reverted to tuned native zoom.
- **Segments:** the displayed path should be a **single smooth line by default**, only
  breaking into colored blocks **after "Show segments"** is clicked.
- **Sidebar:** make it **fixed-width and non-collapsible** (the collapse arrow wasn't
  reappearing) and remove the resize grip; later, **trim the empty space** above the title.
- **Distance units:** default to **miles (US)** with a **km** option; right-align the
  toggle to the rail edge.
- **Weakest stretch:** show **how far it is from the start**, not the street name.
- **Geocoding:** chose **cache + Boston-bounded Nominatim** for speed.
- **Branding:** renamed **Footpath → Humanpath**; supplied a two-dot/connector **logo** to
  replace the walking-person icon, with the title simply "Humanpath".

## Deployment & git

- **Published to GitHub** (created account `jlee0229`, **public** repo) via `gh`.
- **PR workflow:** opened PR #1 scoped to just the rename; kept later UI work on a separate
  branch so that PR stayed clean.
- **Download-on-startup:** since the graph data is too big for git, chose to **host it on a
  GitHub Release and fetch on first run** so a deployed instance works.
- **requirements.txt** added for Streamlit Cloud installs.
- **Consolidated to `main`:** decided to **merge everything to `main`** ("project isn't too
  complicated/large"), then deleted the merged feature branches.

## Notable deferrals (my call to leave for later)

- Broader "forced vs changeable" segment detection (kept terminal-only).
- Tier 2/3 routing speedups (projection caching; Customizable Contraction Hierarchies).
- Promoting the "Map area" region selector to a first-class control once more areas exist.
- Further scoring-value tuning (e.g., environment/arterial overrating).

Example Response: 
Good diagnosis instinct — let me both profile our router to find where the 5km time actually goes, and research the production techniques. Let me do both in parallel.

Profile is decisive: **Yen's k-shortest-paths (`nx.shortest_simple_paths`) is the bottleneck** — it jumps from 80 ms (1.3 km) to ~2.3 s (5 km), while snapping, clipping, and projection stay small. Let me pull the production-routing research to frame the fix.

Here's what I found, combined with our profile.

## Where our time actually goes

| Route | Clip size | Snap | Project | **Yen's k-paths** | Total |
|---|---|---|---|---|---|
| 1.3 km | 733 nodes | 0.4s* | 15 ms | **80 ms** | 0.21 s |
| 5 km | 7,898 nodes | 0.1s | 162 ms | **2,286 ms** | 2.41 s |
| 6 km | 4,378 nodes | 0.1s | 90 ms | **2,442 ms** | 2.59 s |

The culprit is **Yen's k-shortest-paths** (`nx.shortest_simple_paths`), which is ~95% of the long-route time. It scales badly: each of the k routes needs many Dijkstra runs with edge removals, in pure-Python NetworkX, over a clip that grows with O–D distance. Snapping/clipping/projection are negligible. (*The 0.4s snap on the first call is one-time cache warmup.)

## How Google/Apple do it

Production navigation does **almost no graph search at query time**. They shift the work into **preprocessing**:

- **Contraction Hierarchies (CH)** — preprocess once: rank nodes by "importance" and add *shortcut* edges that skip over unimportant nodes. Queries become a tiny bidirectional search that only climbs the hierarchy → **millisecond** queries on continental graphs (this powers OSRM and is believed to be behind Bing/Google).
- **Customizable Route Planning (CRP)** — the technique built for exactly our problem: edge weights change. It splits work into three phases: **(1) metric-independent preprocessing** of the road topology (slow, rare), **(2) metric customization** when the cost function changes (fast), **(3) query** (real-time). It's robust to changing the "metric" and handles turn costs/restrictions cleanly.
- They also **don't use Yen's** for alternatives — they use cheap methods (penalty/plateau/via-node) to produce 2–3 alternates.

The key relevance: **our `alpha` slider and factor-weight sliders change the cost metric on every query.** That's precisely the "metric" CRP/CCH are designed to re-customize quickly — and it's why a plain static CH would *not* work for us (its shortcuts bake in one fixed weighting).

## What I'd actually do (tailored to us)

**Tier 1 — replace Yen's (biggest win, modest effort, no preprocessing).** This kills the 2.3 s directly:
- Find the single best route with **A\*** instead of Yen's. A straight-line (haversine) heuristic is *admissible* for our cost because `cost = length·(1+α(1−walk)) ≥ length ≥ straight-line` for any α/weights — so A* stays correct under the sliders while exploring a fraction of the nodes. Optionally **bidirectional A\*** to roughly halve it again.
- Generate the handful of alternatives via the **penalty method** (take the best route, inflate its edges' weights ~1.3×, re-run A*, repeat) — a few A* runs instead of Yen's dozens of Dijkstras. Everything else (terminal exemption, confidence re-rank, clip widen) stays.
- Expected: long routes drop from ~2.5 s to a few hundred ms.

**Tier 2 — smaller tweaks:** lower `k`/confidence-expansion pulls, cache the projection so widen-retries don't rebuild it.

**Tier 3 — the "real Google answer": Customizable Contraction Hierarchies.** Preprocess Boston's topology once, do a fast customization whenever the weights change, near-instant queries. This is the correct long-term fix for dynamic weights but a large implementation effort — probably overkill before your deadline.

My recommendation: do **Tier 1** now — it removes the bottleneck you're feeling with a contained change to `router.py`, and keeps the sliders working. Want me to implement it?

Sources:
- [Customizable Route Planning in Road Networks (Microsoft Research)](https://www.microsoft.com/en-us/research/wp-content/uploads/2013/01/crp_web_130724.pdf)
- [Customizable Contraction Hierarchies (arXiv)](https://arxiv.org/pdf/1402.0402)
- [Contraction hierarchies (Wikipedia)](https://en.wikipedia.org/wiki/Contraction_hierarchies)
- [Google Maps Route Planning in 2026 (Scrap.io)](https://scrap.io/google-maps-route-planning-technology-20-years)