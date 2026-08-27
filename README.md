# Fuel Route API

A Django API that plans the **cheapest way to fuel a 500-mile-range, 10 MPG
vehicle** on any drive within the USA. Give it a start and a finish; it returns
the route, a map, the fuel stops to make, and what the trip costs in fuel.

Prices come from the supplied OPIS truck-stop CSV (8,151 rows → **6,626 US
truck stops**). Routing uses **OSRM**, which is free and needs no API key.

```
GET /api/v1/route-plan/?start=Dallas,%20TX&finish=Chicago,%20IL
```

```jsonc
{
  "route": { "total_distance_miles": 966.3, "estimated_duration_hours": 17.09,
             "geometry": { "type": "LineString", "coordinates": [ ... ] } },
  "fuel_plan": {
    "total_cost_usd": 276.89,
    "total_gallons": 96.63,
    "average_price_per_gallon": 2.865,
    "fuel_stop_count": 3,
    "stops": [ { "sequence": 1, "station": { "name": "One9 #1248", "city": "Wilmer", "state": "TX" },
                 "price_per_gallon": 2.756, "distance_along_route_miles": 7.0,
                 "gallons_purchased": 48.02, "cost_usd": 132.33 }, ... ]
  },
  "meta": { "external_api_calls": 1, "timings_ms": { "routing_api": 1634.5,
            "station_matching": 10.0, "optimisation": 1.0 } }
}
```

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py load_fuel_prices      # loads 6,626 stations from data/
python manage.py runserver
```

Then open:

| URL | What it is |
|---|---|
| <http://127.0.0.1:8000/> | API reference page |
| <http://127.0.0.1:8000/map/?start=Dallas,+TX&finish=Chicago,+IL> | **Interactive map** of the route and its fuel stops |
| <http://127.0.0.1:8000/api/v1/route-plan/?start=Dallas,+TX&finish=Chicago,+IL> | The JSON above |

Requires Python 3.12+ (developed on 3.14). Coordinates ship with the repo, so
there is **no build step and no API key** — `migrate`, `load_fuel_prices`, go.

### Postman

Import `postman/collections/Fuel Route API.postman_collection.json` — 12
requests covering the happy paths, caching, GeoJSON output and every error mode,
with 37 assertions. It passes clean:

```bash
npx newman run "postman/collections/Fuel Route API.postman_collection.json"
#  assertions  37 executed  0 failed
```

---

## API

### `GET|POST /api/v1/route-plan/`

| Parameter | Required | Description |
|---|---|---|
| `start` | yes | `"City, ST"`, `"City, State Name"`, or `"lat,lon"` |
| `finish` | yes | same |
| `corridor_miles` | no | How far off-route a station may sit. Default `15`, max `50`. |
| `geometry` | no | `simplified` (default), `full`, `none` |
| `format` | no | `json` (default) or `geojson` |

Returns `200` with a plan, `400` for bad input, `404` for an unknown place,
`422` when no legal plan exists (out of range, no drivable route), `502/504`
if the routing provider is unreachable. Errors are always structured JSON:

```jsonc
{ "error": { "code": "range_gap",
             "message": "No fuel station within 500 miles between the origin and mile 2621 (gap of 2621 miles).",
             "detail": { "gap_miles": 2621.4, "gap_starts_at_mile": 0.0 },
             "hint": "Try increasing 'corridor_miles' ..." } }
```

Other endpoints: `GET /api/v1/stations/?state=TX&limit=25` (cheapest stations)
and `GET /api/v1/health/`.

---

## How it works

    start/finish text
        │  1. geocode          in-process gazetteer, 0 network calls
        ▼
    coordinates
        │  2. route            ── the ONE external call ──▶ OSRM
        ▼
    polyline (34k vertices) + road distance
        │  3. locate stations  one batched KD-tree query
        ▼
    stations along the corridor, each with a mile-marker + price
        │  4. optimise         dynamic program
        ▼
    fuel stops, gallons, total cost

### 1. The data problem: the CSV has no coordinates

The price file identifies each truck stop only as `"I-44, EXIT 283 & US-69",
Big Cabin, OK`. You cannot match that to a route without coordinates, and
geocoding 6,626 stations per request is obviously off the table.

So it happens **once, offline**, in `scripts/build_station_coordinates.py`:
every distinct city/state is resolved against the [GeoNames](https://www.geonames.org/)
US gazetteer (a free CC-BY dump), and the results are committed to
`data/city_coordinates.csv`. Matching runs in three passes:

1. exact match on a normalised name (`"St. Louis"` → `ST LOUIS`), searching
   GeoNames primary *and* alternate names — **3,790** of 3,808
2. punctuation-insensitive match (`"Mc Lean"` → `MCLEAN`) — **14** more
3. the last **4** (`Corinth ME`, `Crescent PA`, `Pueblo Of Acoma NM`,
   `Willow Beach AZ`) are hand-pinned in `data/manual_coordinates.csv`, having
   been resolved once via Nominatim

So the build is **reproducible with no network access and resolves 3,808/3,808
cities — 100%**, which is why no station is dropped for want of a location.

Two other things the raw file needs:

- **620 rows are Canadian** (ON, AB, BC, …). The brief says both endpoints are
  in the USA, so those are dropped at load time.
- **678 OPIS IDs repeat** — the same truck stop with several price observations,
  not separate stations. They are collapsed to one station carrying the mean
  observed price (`price_observations` records how many).

Stations are placed at their city centroid, which is the honest limit of this
data: the exact interchange is unknown, so a stop can sit a few miles from where
the sign actually is. `detour_from_route_miles` reports the offset for each stop
so it is visible rather than hidden.

### 2. Keeping it to one external call

The brief asks for one routing call; the API makes **exactly one**, and often
zero:

- **Geocoding is local.** `data/us_places.csv` bundles 68,854 US place names
  (including GeoNames alternates, so `"New York, NY"` resolves — GeoNames files
  it as *New York City*). A `"City, ST"` input costs **0.4 ms and no network**.
  Nominatim is only ever touched for something the gazetteer cannot resolve, and
  `lat,lon` input skips geocoding entirely.
- **One OSRM call** returns the geometry *and* the road distance together.
- **Results are cached** (6 h), so a repeated route makes **zero** calls and
  returns in ~9 ms.

One further win: OSRM is asked for `geometries=polyline6` rather than GeoJSON.
It is the same 34,540 vertices either way, but the payload drops from 276 KB to
118 KB and the call runs **3–4× faster** end to end. `services/polyline.py`
decodes it with NumPy in ~6 ms (verified exact against the GeoJSON form).

### 3. Finding stations along the route

Lat/lon are projected onto the 3D unit sphere, so a Euclidean KD-tree gives true
great-circle nearest neighbours. The route's own vertices (subsampled to ~0.5 mi)
go into the tree, then **all 6,626 stations are located in a single batched
query** — which yields, for each station, both its perpendicular offset from the
route and its mile-marker along it. Bounding-box prefiltering trims the set
first. Coast to coast this takes **~10 ms**.

### 4. Choosing the stops (the actual optimisation)

Cheapest-station-first greedy is *not* optimal — the right move is often to buy
a few gallons at a pricey stop purely to reach a cheaper one. So this is an
exact dynamic program (`services/optimizer.py`):

```
dp[j] = cheapest way to have paid for all fuel burned over [0, d[j]],
        arriving at station j with an empty tank

dp[j] = p[j]·d[j]/mpg                                   ← j is the first stop
dp[j] = min  dp[i] + p[i]·(d[j] − d[i])/mpg             ← drove in from stop i
       i<j, d[j]−d[i] ≤ range

answer = min  dp[j] + p[j]·(D − d[j])/mpg
        D−d[j] ≤ range
```

The transition rearranges to `(dp[i] − p[i]·d[i]/mpg) + (p[i]/mpg)·d[j]` — a
minimum over a sliding window of straight lines — so each step is one vectorised
NumPy reduction over a short slice. Coast to coast it runs in **~1 ms**.

It is verified against a brute-force oracle over every legal combination of stops
on 150 randomised instances per run, plus the invariant that
`total_gallons == total_distance / mpg` always holds.

**Cost model.** The driver pays for **every gallon burned**, so total gallons is
always distance ÷ 10. Fuel bought at a stop covers the leg to the next one; the
last stop covers the run to the destination. The short leg from the origin to
the *first* stop is priced at that first stop's price — the driver tops up
before leaving at the price they are about to pay anyway — and that leg is
capped at 50 miles so a plan can never "pre-buy" a whole tank at a bargain
hundreds of miles away. It is reported separately as `origin_fill`.

### Performance

Measured on New York → Los Angeles (2,794 mi, 34,499 route vertices, 537
stations in the corridor, 11 stops):

| Stage | Time |
|---|---|
| Geocoding (both endpoints, local) | **0.4 ms** |
| OSRM routing call *(network)* | 1,634 ms |
| Locating 6,626 stations on the route | **10.0 ms** |
| Refuelling optimisation | **1.0 ms** |
| **Total** | **1,666 ms** |
| **Same request, cached** | **~9 ms**, 0 external calls |

Own compute is **~11 ms**; the wall clock is the free OSRM demo server. Pointing
`OSRM_BASE_URL` at a self-hosted instance removes essentially all of it.
The gazetteer and station index are warmed on start-up, so request #1 is not
the slow one.

---

## Tests

```bash
python manage.py test          # 78 tests, ~0.2s, no network required
```

Routing is mocked throughout. Coverage includes the DP against a brute-force
oracle, the polyline decoder against the canonical reference vector, geometry
maths, location parsing (including rejecting `"Toronto, ON"` while still
accepting `"Vancouver, WA"`), caching behaviour, every validation path, and
every failure mode.

## Configuration

All optional, via environment variables: `OSRM_BASE_URL`,
`VEHICLE_MAX_RANGE_MILES`, `VEHICLE_MPG`, `DEFAULT_CORRIDOR_MILES`,
`ROUTE_CACHE_SECONDS`, `ENABLE_NOMINATIM_FALLBACK`, `DJANGO_DEBUG`,
`DJANGO_SECRET_KEY`.

## Layout

```
config/                     Django project (settings, urls, wsgi/asgi)
fuelroute/
  models.py                 FuelStation
  views.py  urls.py         HTTP layer — parsing and error mapping only
  services/
    geocoding.py            text -> coordinates (local gazetteer first)
    routing.py              the single OSRM call
    polyline.py             vectorised polyline6 decoder
    geo.py                  distances, measurement, simplification, KD-tree
    station_index.py        in-memory station snapshot + corridor search
    optimizer.py            the refuelling dynamic program
    planner.py              orchestration + caching
  management/commands/
    load_fuel_prices.py     CSV -> database
  templates/fuelroute/      landing page + Leaflet map
scripts/
  build_station_coordinates.py   one-time offline geocoding
data/                       price CSV + generated coordinates + gazetteer
postman/collections/        Postman collection (37 assertions)
```

## Notes and limitations

- Stations sit at **city centroids**, not exact interchanges (the CSV has no
  street coordinates). `detour_from_route_miles` surfaces the error per stop.
- The CSV carries **no observation dates**, so repeated prices for one station
  are averaged rather than taking "the latest".
- The public OSRM demo server is rate-limited and occasionally slow; it is fine
  for a demo, and `OSRM_BASE_URL` swaps it out for production.
- `DEBUG` defaults to on and the dev `SECRET_KEY` is a placeholder — set
  `DJANGO_DEBUG=0` and a real `DJANGO_SECRET_KEY` before deploying anywhere.

## Attribution

- Routing: [OSRM](http://project-osrm.org/) / [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL)
- Geocoding: [GeoNames](https://www.geonames.org/) (CC BY 4.0), [Nominatim](https://nominatim.org/)
- Map tiles: OpenStreetMap · [Leaflet](https://leafletjs.com/)
