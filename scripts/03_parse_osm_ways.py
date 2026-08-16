"""
Step 3 (routing engine): parse the Manhattan OSM extract into a
GeoDataFrame of way geometries, so later steps can check our block
geometry against real street geometry without re-parsing 75MB of XML
every time.

Run:
  python3 scripts/03_parse_osm_ways.py

Input:
  data/raw/osm/manhattan.osm  Manhattan `highway=*` ways, pulled from
                               the Overpass API (see README -- Geofabrik
                               only offers a whole-state PBF, and a
                               570k-way NYC-bbox pull risked Overpass's
                               public-instance limits; a Manhattan-sized
                               pull (~121k ways) completed cleanly in
                               ~1 minute, confirmed empirically before
                               committing to it).

Output:
  data/processed/manhattan_osm_ways.geojson -- `way_id, highway,
  geometry` for every `highway=*` way, WGS84. This is NOT what
  GraphHopper imports (GraphHopper reads data/raw/osm/manhattan.osm
  directly) -- it's a side artifact so scripts 04+ can do geometric
  checks against real street geometry without re-parsing the raw file.

Uses stdlib xml.etree.ElementTree.iterparse rather than a full DOM
load or an extra OSM-parsing dependency (osmium/pyrosm aren't in this
project's venv) -- streaming is enough since Overpass's
`(._;>;); out body;` recursion guarantees every <node> a <way> refers
to appears earlier in the file, so a single forward pass can resolve
node refs as it goes.
"""

import json
import sys
import xml.etree.ElementTree as ET

input_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/osm/manhattan.osm"
output_path = sys.argv[2] if len(sys.argv) > 2 else "data/processed/manhattan_osm_ways.geojson"

nodes = {}
features = []
current_way_id = None
current_refs = None
current_tags = None

for event, elem in ET.iterparse(input_path, events=("start", "end")):
    if event == "start":
        if elem.tag == "node":
            nodes[elem.attrib["id"]] = (float(elem.attrib["lon"]), float(elem.attrib["lat"]))
        elif elem.tag == "way":
            current_way_id = elem.attrib["id"]
            current_refs = []
            current_tags = {}
    elif event == "end":
        if elem.tag == "nd" and current_refs is not None:
            current_refs.append(elem.attrib["ref"])
        elif elem.tag == "tag" and current_tags is not None:
            current_tags[elem.attrib["k"]] = elem.attrib["v"]
        elif elem.tag == "way":
            highway = current_tags.get("highway")
            if highway is not None:
                coords = [nodes[ref] for ref in current_refs if ref in nodes]
                if len(coords) >= 2:
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": coords},
                        "properties": {"way_id": current_way_id, "highway": highway},
                    })
            current_way_id = None
            current_refs = None
            current_tags = None
            elem.clear()  # keep peak memory down over 120k ways

print(f"Parsed {len(features)} highway ways from {input_path}")

geojson_out = {"type": "FeatureCollection", "features": features}
with open(output_path, "w") as f:
    json.dump(geojson_out, f)
print(f"Wrote {len(features)} ways to {output_path}")
