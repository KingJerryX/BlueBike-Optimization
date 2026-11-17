# Project Context: Bluebikes Rebalancing Optimization

Final group project (team of 3) for an MIT optimization methods class. The assignment requires:
- a clearly identified decision-making problem
- optimization methods to solve it
- computational results
- evaluation of optimization against benchmarks
- recommendations communicated to a decision-maker

**Working title:** Optimizing Bike Rebalancing for Bluebikes in Cambridge
(alternative: *Optimizing Overnight and Peak-Hour Rebalancing for Bluebikes in Cambridge*)

---

## Decision problem

Bluebikes stations regularly run **empty** (riders can't rent) or **full** (riders can't return a bike). These are **stockouts**. The operator (Lyft, on behalf of the cities) decides:
1. How many bikes each station should start the day with (target inventory).
2. How rebalancing trucks should move bikes to reach those targets, overnight and possibly during peak hours.

**Objective:** minimize expected failed rentals + failed returns.

**Scope:** Cambridge only (~60–80 stations), to keep the routing MIP tractable.

### Key insight on the objective
Both failure types are penalized, so filling a station to capacity is costly: it blocks returns. The optimal target is interior and depends on the station's demand pattern:
- Morning-origin stations (residential, e.g. Central Square): lean full
- Morning-destination stations (Kendall, MIT): lean empty, to leave docks open
- Balanced stations: near the middle

---

## Data sources

| Source | Use | Link |
|---|---|---|
| Bluebikes trip history (quarterly) | Hourly pickups/drop-offs per station | https://bluebikes.com/system-data |
| Raw trip + station files | Bulk download | https://s3.amazonaws.com/hubway-data/index.html |
| GBFS discovery feed | Entry point for live data | https://gbfs.bluebikes.com/gbfs/gbfs.json |
| station_information | Station coordinates, dock capacity | https://gbfs.lyft.com/gbfs/1.1/bos/en/station_information.json |
| station_status | Live bike/dock counts per station | https://gbfs.lyft.com/gbfs/1.1/bos/en/station_status.json |
| NOAA weather | Demand covariate / scenarios | https://www.ncei.noaa.gov/cdo-web/ |

Trip data notes: it includes start/end station, timestamps, duration, and rider type (Member vs Casual). Staff service trips and trips under 60 s are already removed.

### Critical data caveat: censored demand
Trip data only records rentals that **happened**. When a station is empty, observed rentals drop to ~0, but true demand doesn't. GBFS only shows the *current* snapshot, with no history. **Plan:** run a logger that saves `station_status.json` every 5–10 minutes, then use the logged inventories to flag stockout periods and correct demand estimates. Treat stockout hours as "demand ≥ observed," not "low demand."

---

## Modeling plan

### 1. Demand estimation
Estimate hourly pickup and drop-off rates per station from the trip data, adjusted for stockouts using the logged station status.

### 2. First formulation: target inventory (LP/MIP)
- **Decision:** start-of-day bike count at each station (integer).
- **Objective:** minimize expected failed rentals + failed returns, using a piecewise-linear expected-shortage cost per station.
- **Constraints:**
  - 0 ≤ target ≤ dock capacity at each station
  - sum of targets ≤ total number of bikes available in the Cambridge study area (estimate from overnight station_status totals)
- The bike-total constraint is a ceiling and may not bind. Report whether the system is short on bikes or short on placement.

### 3. Second formulation: routing (MIP)
- Pickup-and-delivery vehicle routing problem: trucks of fixed capacity move bikes from surplus to deficit stations within an overnight time window.
- Binary arc variables (truck travels station i → j), plus load/flow variables.
- **Routes are the model's output, not an input.**

### 4. Extension: dynamic (peak-hour) rebalancing
- Multi-period / time-expanded MIP: split the day into periods (e.g. 1-hour blocks) and decide truck moves per period. Inventory carries over between periods.
- Motivation: e.g. Kendall stations fill by ~9 a.m.; moving bikes out mid-morning frees docks.
- Build overnight first, then compare **overnight only** vs **overnight + peak-hour**. The gap answers "is daytime rebalancing worth paying for?"

### 5. Extension: uncertainty
- **Stochastic:** demand scenarios with probabilities (e.g. sunny weekday / rainy weekday / weekend). Choose one set of inventories that minimizes failures *averaged across* scenarios.
- **Robust:** demand within an uncertainty set (e.g. ±20% of average). Minimize failures in the *worst case*.
- Compare deterministic vs stochastic vs robust plans in the simulator, especially on high-demand days.

---

## Assumptions (Lyft does not publish these)
- **Number of trucks:** unknown. Either (a) run a sensitivity analysis over 1–4 trucks, and/or (b) make it an integer decision variable with a per-truck nightly cost (driver + vehicle). Plan is to do both.
- **Truck capacity:** ~20 bikes (assumption; vary it).
- **Depot location:** choose a plausible spot, or let trucks start at any station.
- **Time window:** e.g. a 6-hour overnight shift.
- **Travel times:** from station coordinates, straight-line distance × ~1.3 detour factor (or a routing API).
- **Terminology:** "fleet size" is ambiguous. Use "total number of bikes" for bikes and "number of rebalancing trucks" for trucks.

## Evaluation
- Build a **simulator** that replays held-out real days of trips against a given starting inventory and rebalancing plan, counting failed rentals and returns.
- **Benchmarks:**
  1. No rebalancing
  2. Fill every station to half capacity
  3. Greedy nearest-surplus heuristic
- Report the headline improvement vs benchmarks, sensitivity to truck count, the value of daytime rebalancing, and the value of modeling uncertainty.

## Limitations to mention
- Bluebikes also rebalances through the **Bike Angels** program (riders earn points for moving bikes from full to empty stations); the model covers trucks only.
- Truck fleet and operating parameters are assumed.
- Demand correction for stockouts depends on how long the station_status logger has been running.

## Suggested team split
1. Data pipeline + demand estimation (including the GBFS logger and stockout correction)
2. Target-inventory model + uncertainty extensions
3. Routing / multi-period MIP + simulator and benchmarks

## Coding conventions
- Use **descriptive variable names** (e.g. `target_bikes_at_station`, `truck_capacity`, `failed_rentals`), never nondescript names like `C1`, `q1`, `x`.
- Likely stack: Python (pandas for data, Gurobi or another MIP solver via its Python API).
