"""Structural checks on a generated .cv against a reference scene."""
import json, sys, collections

gen = json.load(open(sys.argv[1]))
ref = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else None

errs, warns = [], []
if ref and set(gen) != set(ref):
    errs.append(f"top-level keys differ: missing={set(ref)-set(gen)} extra={set(gen)-set(ref)}")

ids = {n["nodeId"] for n in gen["nodes"]}
dupes = [k for k, v in collections.Counter(n["nodeId"] for n in gen["nodes"]).items() if v > 1]
if dupes: errs.append(f"duplicate nodeIds: {dupes[:5]}")

for n in gen["nodes"]:
    if set(n) - {"attributes", "nodeId", "nodeType", "contextFilter"}:
        errs.append(f"unexpected node keys on {n['nodeId']}: {set(n)}")
    if n["nodeId"].split("#")[0] != n["nodeType"]:
        errs.append(f"nodeId/nodeType mismatch: {n['nodeId']} vs {n['nodeType']}")

# referential integrity of the graph
for c in gen["connections"]:
    for side in ("from", "to"):
        nid = c[side].split(".")[0]
        if nid not in ids:
            errs.append(f"connection {side} references unknown node {nid}")

for m in gen["nodeMeta"]:
    if m["nodeId"] not in ids:
        errs.append(f"nodeMeta for unknown node {m['nodeId']}")
    for ch in m["children"]:
        if ch not in ids:
            errs.append(f"nodeMeta child unknown: {ch}")

# every node should be reachable: a child of something, or a root-ish singleton
kids = {c for m in gen["nodeMeta"] for c in m["children"]}
ROOTS = {"compNode", "renderQueue", "dynamicIndexManager", "paletteContainer", "keyframe", "asset"}
orphans = [n["nodeId"] for n in gen["nodes"]
           if n["nodeId"] not in kids and n["nodeType"] not in ROOTS]
if orphans: warns.append(f"{len(orphans)} orphan nodes, e.g. {orphans[:5]}")

for a in gen["assets"]:
    if a["nodeId"] not in ids: errs.append(f"asset node missing from nodes: {a['nodeId']}")

# Reference-slot lists must be filled by connections. Lists whose entries
# carry inline values (gradient stops, say) are data, not references.
def is_reference_slot(entry):
    return entry == {} or entry.get("varType") == "nodeId" or (
        "compound" in entry and "mode" in entry.get("compound", {}))

for n in gen["nodes"]:
    for name, at in n["attributes"].items():
        if not (isinstance(at, dict) and "list" in at):
            continue
        entries = at["list"]
        got = sum(1 for c in gen["connections"]
                  if c["to"].startswith(f"{n['nodeId']}.{name}."))
        if got > len(entries):
            errs.append(f"{n['nodeId']}.{name}: {got} connections but only "
                        f"{len(entries)} slots")
        elif got < len(entries) and all(is_reference_slot(e) for e in entries):
            errs.append(f"{n['nodeId']}.{name}: {len(entries)} reference slots "
                        f"but {got} connections")

kinds = collections.Counter(n["nodeType"] for n in gen["nodes"])
print("node types:", dict(kinds))
print(f"nodes={len(gen['nodes'])} connections={len(gen['connections'])} "
      f"meta={len(gen['nodeMeta'])} assets={len(gen['assets'])}")
if ref:
    unknown = set(kinds) - {n["nodeType"] for n in ref["nodes"]}
    if unknown: print("node types not present in reference:", unknown)
for w in warns: print("WARN:", w)
for e in errs[:20]: print("ERROR:", e)
print("OK" if not errs else f"{len(errs)} ERRORS")
