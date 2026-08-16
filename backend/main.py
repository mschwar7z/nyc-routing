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

import os
from typing import Literal, Optional, Tuple

import geopandas as gpd
import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from lib.custom_model_areas import (
    custom_model_default,
    custom_model_for_bike,
    custom_model_for_shade,
)

GRAPHHOPPER_URL = os.environ.get("GRAPHHOPPER_URL", "http://localhost:8989")
BLOCKS_PATH = "data/processed/bike_scored_blocks.geojson"
SHADE_PATH = "data/processed/shade_index.parquet"

# Matches the project's existing "representative snapshot" convention
# (data/processed/shade_scored_blocks_jun_2pm.geojson) -- used when a
# shade request doesn't specify month/hour.
DEFAULT_SHADE_MONTH, DEFAULT_SHADE_HOUR = 6, 14

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


@app.post("/route", response_model=RouteResponse)
def route(req: RouteRequest):
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

    path = resp.json()["paths"][0]
    return RouteResponse(
        preference=req.preference,
        distance_m=path["distance"],
        time_s=path["time"] / 1000,
        weight=path["weight"],
        geometry=path["points"],
        areas=custom_model["areas"],
    )


@app.get("/shade_snapshots")
def shade_snapshots():
    """(month, hour) pairs scripts/02 actually computed -- lets callers
    build a valid picker instead of hardcoding a guess at what's there."""
    return sorted(VALID_SHADE_SNAPSHOTS)


@app.get("/health")
def health():
    return {"status": "ok", "blocks_loaded": len(blocks_gdf), "shade_rows_loaded": len(shade_df)}
