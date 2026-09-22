"""Check that every generated shape ends up painted and reachable."""
import json, sys, collections

j = json.load(open(sys.argv[1]))
nodes = {n["nodeId"]: n for n in j["nodes"]}
SHAPES = {"editableShape", "basicShape", "footageShape", "textShape"}

painted = {c["to"].split(".")[0] for c in j["connections"] if c["to"].endswith(".material")}
stroked = {c["to"].split(".")[0] for c in j["connections"] if c["to"].endswith(".stroke")}
gen = {c["to"].split(".")[0] for c in j["connections"] if c["to"].endswith(".generator")}

shapes = [n for n in j["nodes"] if n["nodeType"] in SHAPES]
bare = [n["nodeId"] for n in shapes if n["nodeId"] not in painted and n["nodeId"] not in stroked
        and n["nodeType"] in ("editableShape", "basicShape")]
noGen = [n["nodeId"] for n in shapes if n["nodeType"] == "basicShape" and n["nodeId"] not in gen]

print(f"{len(shapes)} shapes: {len(painted)} filled, {len(stroked)} stroked")
print(f"  unpainted (no fill and no stroke): {len(bare)}" + (f" e.g. {bare[:4]}" if bare else ""))
print(f"  basicShape with no generator: {len(noGen)}" + (f" e.g. {noGen[:4]}" if noGen else ""))
print("  node types:", dict(collections.Counter(n["nodeType"] for n in j["nodes"])))
