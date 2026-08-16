"""
Thin FastAPI wrapper around the running GraphHopper instance: takes an
origin, destination, and preference (bike/shade/default -- NOT
traffic; no traffic dataset exists anywhere in this repo, see README),
builds the matching custom_model payload via
lib/custom_model_areas.py, and forwards it to GraphHopper's /route.

This is deliberately the last step, not the first: scripts/05 already
proved each custom_model payload produces a sane, distinct route by
curling GraphHopper directly. This module doesn't add any new routing
logic, just a request/response shape on top of what's already been
verified to work.

Run (with `docker compose up` already running in graphhopper/):
  source .venv/bin/activate
  uvicorn backend.main:app --reload --port 8000

Then:
  curl -X POST localhost:8000/route -H 'Content-Type: application/json' -d '{
    "origin": [40.783, -73.9771], "destination": [40.795, -73.9688],
    "preference": "bike"
  }'
"""

import calendar
import json
import os
import threading
import time
from datetime import datetime
from typing import Literal, Optional, Tuple

import anthropic
import geopandas as gpd
import httpx
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lib.custom_model_areas import (
    custom_model_default,
    custom_model_for_bike,
    custom_model_for_shade,
)

load_dotenv()  # ANTHROPIC_API_KEY -- local dev convenience; never read client-side

GRAPHHOPPER_URL = os.environ.get("GRAPHHOPPER_URL", "http://localhost:8989")
BLOCKS_PATH = "data/processed/bike_scored_blocks.geojson"
SHADE_PATH = "data/processed/shade_index.parquet"

# Matches the project's existing "representative snapshot" convention
# (data/processed/shade_scored_blocks_jun_2pm.geojson) -- used when a
# shade request doesn't specify month/hour.
DEFAULT_SHADE_MONTH, DEFAULT_SHADE_HOUR = 6, 14

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy (operations.osmfoundation.org/policies/nominatim)
# requires a descriptive User-Agent identifying the application/contact --
# generic library UAs get blocked.
NOMINATIM_USER_AGENT = "nyc-routing/0.1 (mschwar7z@gmail.com)"
# Same policy: max 1 request/second. Loose bias box, not a hard bound, so a
# match just outside the five boroughs (e.g. across a river) still resolves.
NYC_VIEWBOX = "-74.2557,40.9153,-73.7002,40.4990"

app = FastAPI(title="nyc-routing")

# Local dev only: web/index_route.html calls this API straight from a
# file:// page (no origin at all) or a local http.server on a
# different port, either of which the default same-origin policy
# would block without this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RouteQueryError(Exception):
    """Raised at whichever step of /api/route-query fails, so the handler
    below can tell the frontend which step failed (LLM parse vs geocoding
    vs an unsupported preference vs GraphHopper/custom_model) instead of a
    generic 500."""

    def __init__(self, step: str, message: str):
        self.step = step
        self.message = message
        super().__init__(message)


@app.exception_handler(RouteQueryError)
def _route_query_error_handler(request: Request, exc: RouteQueryError):
    return JSONResponse(status_code=422, content={"step": exc.step, "message": exc.message})


blocks_gdf = gpd.read_file(BLOCKS_PATH)
shade_df = pd.read_parquet(SHADE_PATH)
VALID_SHADE_SNAPSHOTS = set(zip(shade_df["month"], shade_df["hour"]))


class RouteRequest(BaseModel):
    origin: Tuple[float, float]  # (lat, lon)
    destination: Tuple[float, float]  # (lat, lon)
    preference: Literal["bike", "shade", "default"]
    month: Optional[int] = None  # shade only; defaults to DEFAULT_SHADE_MONTH
    hour: Optional[int] = None  # shade only; defaults to DEFAULT_SHADE_HOUR
    seek_shade: bool = True  # shade only; False = avoid shade instead


class RouteResponse(BaseModel):
    preference: str
    distance_m: float
    time_s: float
    weight: float
    geometry: dict  # GeoJSON LineString, WGS84
    areas: dict  # GeoJSON FeatureCollection, the custom_model areas actually sent to GraphHopper


def _build_custom_model(req: RouteRequest) -> dict:
    if req.preference == "default":
        return custom_model_default()
    if req.preference == "bike":
        return custom_model_for_bike(blocks_gdf)

    # preference == "shade"
    month = req.month if req.month is not None else DEFAULT_SHADE_MONTH
    hour = req.hour if req.hour is not None else DEFAULT_SHADE_HOUR
    if (month, hour) not in VALID_SHADE_SNAPSHOTS:
        valid = sorted(VALID_SHADE_SNAPSHOTS)
        raise HTTPException(
            status_code=400,
            detail=f"No shade data for month={month}, hour={hour}. Valid (month, hour): {valid}",
        )
    return custom_model_for_shade(blocks_gdf, shade_df, month, hour, seek_shade=req.seek_shade)


def _call_graphhopper(req: RouteRequest) -> Tuple[dict, dict]:
    """Builds the custom_model and calls GraphHopper's /route -- the one
    place that happens, shared by /route and /api/route-query so the
    latter forwards to the exact same verified wrapper logic rather than
    duplicating (or re-deriving) the request shape.

    Returns (path, custom_model) -- path is GraphHopper's raw paths[0]
    dict (distance/time/weight/points/instructions), custom_model is
    what _build_custom_model produced.
    """
    custom_model = _build_custom_model(req)

    gh_body = {
        "points": [
            [req.origin[1], req.origin[0]],
            [req.destination[1], req.destination[0]],
        ],
        "profile": "bike",
        "points_encoded": False,
        "custom_model": custom_model,
    }
    try:
        resp = httpx.post(f"{GRAPHHOPPER_URL}/route", json=gh_body, timeout=30.0)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"Can't reach GraphHopper at {GRAPHHOPPER_URL}: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GraphHopper error: {resp.text}")

    return resp.json()["paths"][0], custom_model


@app.post("/route", response_model=RouteResponse)
def route(req: RouteRequest):
    path, custom_model = _call_graphhopper(req)
    return RouteResponse(
        preference=req.preference,
        distance_m=path["distance"],
        time_s=path["time"] / 1000,
        weight=path["weight"],
        geometry=path["points"],
        areas=custom_model["areas"],
    )


ANTHROPIC_MODEL = "claude-sonnet-5"

# What the LLM is allowed to extract -- nothing else. "traffic" is a valid
# *extraction* even though the backend has no traffic dataset (see README),
# so that case gets its own distinct "unsupported_preference" error below
# instead of a confusing geocode/routing failure.
_LOCATION_TEXT_GUIDANCE = (
    "Rewrite it into a description a geocoder can resolve, not a verbatim copy: "
    "for an intersection or cross-street ('the cross street of X and Y', 'X and Y', "
    "'X & Y'), identify which of the two is the named avenue/road and which is the "
    "numbered cross-street, and format it AVENUE FIRST as '<Avenue> & <Numbered "
    "Street>, Manhattan, NY' (e.g. 'Amsterdam Ave & W 79th St, Manhattan, NY') -- "
    "the reverse order (street first) frequently geocodes to the wrong intersection. "
    "For a landmark, building, or place name -- including informal or abbreviated "
    "ones (e.g. a university campus nickname) -- expand it to its full, "
    "commonly-used name if you're confident what it refers to, and append "
    "'New York, NY' if a city/borough isn't already implied."
)

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "origin_text": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "The origin location as described in the query, e.g. 'Grand Central Station'. "
                + _LOCATION_TEXT_GUIDANCE
                + " Null if the query means the user's current location ('my location', "
                "'here', 'from where I am'), or doesn't mention an origin at all."
            ),
        },
        "destination_text": {
            "type": "string",
            "description": "The destination location as described in the query. " + _LOCATION_TEXT_GUIDANCE,
        },
        "preference": {
            "type": "string",
            "enum": ["shade", "bike", "traffic", "default"],
            "description": (
                "'shade' for requests about sun/shade -- including 'shadiest', 'most shade', "
                "'coolest', 'avoid the sun'; 'bike' for requests about protected/painted bike "
                "paths or bike lanes; 'traffic' for requests about avoiding traffic or "
                "congestion; 'default' if no routing preference is stated."
            ),
        },
        "time": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "24-hour HH:MM if the query names a specific clock time (e.g. '4pm' -> "
                "'16:00'), else null. Null also for relative-to-now phrasing ('right now', "
                "'currently', or no time mentioned at all) -- the server fills in the actual "
                "current time, don't guess it."
            ),
        },
        "date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "YYYY-MM-DD if the query names a specific calendar date, else null. Null for "
                "'today'/'now'/unstated -- the server fills in the actual current date, "
                "don't guess it."
            ),
        },
    },
    "required": ["origin_text", "destination_text", "preference", "time", "date"],
    "additionalProperties": False,
}

_anthropic_client: Optional["anthropic.Anthropic"] = None


def _get_anthropic_client() -> "anthropic.Anthropic":
    global _anthropic_client
    if _anthropic_client is None:
        try:
            _anthropic_client = anthropic.Anthropic()
        except anthropic.AnthropicError as e:
            raise RouteQueryError("llm_parse", f"Anthropic client not configured: {e}")
    return _anthropic_client


def _parse_query_with_llm(query: str, has_origin_coords: bool) -> dict:
    """LLM concern only: query text -> structured fields. Never geocodes,
    never touches GraphHopper, never invents coordinates."""
    client = _get_anthropic_client()
    origin_note = (
        "The frontend already resolved a current-location reference to coordinates -- "
        "leave origin_text null in that case."
        if has_origin_coords
        else "No origin coordinates were supplied by the frontend -- extract the origin as text."
    )
    system = (
        "Extract structured fields from a routing query for a NYC routing app. "
        "Output only the fields in the schema. Do not compute or estimate a route, "
        "do not invent coordinates, do not answer anything else. " + origin_note
    )
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            # Comfortably covers the JSON payload even with expanded location
            # names -- 40 was too tight and truncated longer destinations
            # mid-string, which is what was actually causing the parse failures.
            max_tokens=300,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": QUERY_SCHEMA},
            },
            system=system,
            messages=[{"role": "user", "content": query}],
        )
    except TypeError as e:
        # The SDK raises a bare TypeError (not AnthropicError) when it can't
        # resolve credentials at request-build time -- ANTHROPIC_API_KEY unset.
        raise RouteQueryError("llm_parse", f"Anthropic client not configured: {e}")
    except anthropic.APIError as e:
        raise RouteQueryError("llm_parse", f"Anthropic API error: {e}")

    if response.stop_reason == "refusal":
        raise RouteQueryError("llm_parse", "The model declined to process this query.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RouteQueryError("llm_parse", "No text content in the model's response.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RouteQueryError("llm_parse", f"Model returned unparseable JSON: {e}")


_nominatim_lock = threading.Lock()
_last_nominatim_call = 0.0


def _geocode(text: str) -> Optional[Tuple[float, float]]:
    """Non-LLM: text -> (lat, lon) via Nominatim, or None if no match.
    Serializes calls behind a lock so concurrent requests still respect
    Nominatim's 1 request/second usage policy."""
    global _last_nominatim_call
    with _nominatim_lock:
        wait = 1.0 - (time.monotonic() - _last_nominatim_call)
        if wait > 0:
            time.sleep(wait)
        resp = httpx.get(
            NOMINATIM_URL,
            params={"q": text, "format": "json", "limit": 1, "viewbox": NYC_VIEWBOX, "bounded": 0},
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=10.0,
        )
        _last_nominatim_call = time.monotonic()
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


def _month_hour_from_parsed(parsed: dict) -> Tuple[Optional[int], Optional[int]]:
    """time "HH:MM" / date "YYYY-MM-DD" -> (month, hour). Either half is
    None when the query didn't state it -- _resolve_shade_snapshot below
    fills an unstated half in with the real current time."""
    hour = None
    if parsed.get("time"):
        try:
            hour = int(parsed["time"].split(":")[0])
        except (ValueError, IndexError):
            raise RouteQueryError("llm_parse", f"Could not parse time {parsed['time']!r} as HH:MM")
    month = None
    if parsed.get("date"):
        try:
            month = int(parsed["date"].split("-")[1])
        except (ValueError, IndexError):
            raise RouteQueryError("llm_parse", f"Could not parse date {parsed['date']!r} as YYYY-MM-DD")
    return month, hour


def _resolve_shade_snapshot(month: Optional[int], hour: Optional[int]) -> Tuple[int, int]:
    """Fills an unstated month/hour in with the real current time ("right
    now" / no time mentioned), then snaps to the nearest (month, hour)
    scripts/02 actually computed -- only Jan/Jun/Oct x 7am-7pm exist (see
    README's "First pass covers 3 months..." note), so most of the
    calendar year would otherwise have no snapshot at all. Circular month
    distance (Dec is 1 month from Jan, not 11) with hour as the tiebreaker
    within the nearest month(s)."""
    now = datetime.now()
    target_month = month if month is not None else now.month
    target_hour = hour if hour is not None else now.hour

    def month_dist(m):
        d = abs(m - target_month) % 12
        return min(d, 12 - d)

    return min(VALID_SHADE_SNAPSHOTS, key=lambda mh: (month_dist(mh[0]), abs(mh[1] - target_hour)))


PREFERENCE_SUMMARY = {
    "shade": "mostly shaded route",
    "bike": "route using protected bike infrastructure",
    "default": "route",
}


def _build_summary(preference: str, path: dict, shade_snapshot: Optional[Tuple[int, int]] = None) -> str:
    miles = path["distance"] * 0.000621371
    street_totals: dict = {}
    for instr in path.get("instructions") or []:
        name = instr.get("street_name")
        if name:
            street_totals[name] = street_totals.get(name, 0) + instr.get("distance", 0)
    dominant_street = max(street_totals, key=street_totals.get) if street_totals else None
    desc = PREFERENCE_SUMMARY.get(preference, "route")
    if shade_snapshot is not None:
        month, hour = shade_snapshot
        hour_12 = hour % 12 or 12
        am_pm = "am" if hour < 12 else "pm"
        desc = f"{desc} (based on {calendar.month_name[month]} {hour_12}{am_pm} shade data)"
    if dominant_street:
        return f"{miles:.1f} miles, {desc} via {dominant_street}"
    return f"{miles:.1f} miles, {desc}"


class RouteQueryRequest(BaseModel):
    query: str
    # (lat, lng), attached by the frontend via browser geolocation when the
    # query implies current location -- the LLM never guesses at this.
    origin_coords: Optional[Tuple[float, float]] = None


class Direction(BaseModel):
    text: str  # GraphHopper's own turn-by-turn instruction, e.g. "Turn right onto Amsterdam Avenue"
    distance_m: float
    time_s: float


class RouteQueryResponse(BaseModel):
    preference: str
    distance_m: float
    time_s: float
    geometry: dict  # GeoJSON LineString, WGS84
    summary: str
    directions: list[Direction]
    origin: Tuple[float, float]
    destination: Tuple[float, float]


@app.post("/api/route-query", response_model=RouteQueryResponse)
def route_query(req: RouteQueryRequest):
    parsed = _parse_query_with_llm(req.query, has_origin_coords=req.origin_coords is not None)

    preference = parsed.get("preference")
    if preference == "traffic":
        raise RouteQueryError(
            "unsupported_preference",
            "This query asks to avoid traffic, but no traffic dataset exists in this "
            "project (see README) -- only shade, bike, and default preferences are supported.",
        )
    if preference not in ("shade", "bike", "default"):
        raise RouteQueryError("llm_parse", f"Model returned an unrecognized preference: {preference!r}")

    if req.origin_coords is not None:
        origin = req.origin_coords
    elif parsed.get("origin_text"):
        try:
            origin = _geocode(parsed["origin_text"])
        except httpx.HTTPError as e:
            raise RouteQueryError("geocode_origin", f"Geocoding request failed: {e}")
        if origin is None:
            raise RouteQueryError("geocode_origin", f"No location found for {parsed['origin_text']!r}")
    else:
        raise RouteQueryError(
            "missing_origin",
            "The query doesn't name an origin, and no current-location coordinates were provided.",
        )

    destination_text = parsed.get("destination_text")
    if not destination_text:
        raise RouteQueryError("llm_parse", "Model did not extract a destination from the query.")
    try:
        destination = _geocode(destination_text)
    except httpx.HTTPError as e:
        raise RouteQueryError("geocode_destination", f"Geocoding request failed: {e}")
    if destination is None:
        raise RouteQueryError("geocode_destination", f"No location found for {destination_text!r}")

    month, hour = _month_hour_from_parsed(parsed)
    shade_snapshot = _resolve_shade_snapshot(month, hour) if preference == "shade" else None
    route_req = RouteRequest(
        origin=origin,
        destination=destination,
        preference=preference,
        month=shade_snapshot[0] if shade_snapshot else None,
        hour=shade_snapshot[1] if shade_snapshot else None,
    )
    try:
        path, _custom_model = _call_graphhopper(route_req)
    except HTTPException as e:
        raise RouteQueryError("routing", str(e.detail))

    directions = [
        Direction(text=instr["text"], distance_m=instr["distance"], time_s=instr["time"] / 1000)
        for instr in path.get("instructions") or []
    ]

    return RouteQueryResponse(
        preference=preference,
        distance_m=path["distance"],
        time_s=path["time"] / 1000,
        geometry=path["points"],
        summary=_build_summary(preference, path, shade_snapshot),
        directions=directions,
        origin=origin,
        destination=destination,
    )


@app.get("/shade_snapshots")
def shade_snapshots():
    """(month, hour) pairs scripts/02 actually computed -- lets callers
    build a valid picker instead of hardcoding a guess at what's there."""
    return sorted(VALID_SHADE_SNAPSHOTS)


@app.get("/health")
def health():
    return {"status": "ok", "blocks_loaded": len(blocks_gdf), "shade_rows_loaded": len(shade_df)}
