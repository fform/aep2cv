"""Read Cavalry's shipped node schema and audit a generated .cv against it.

Cavalry ships the authoritative definition of every node type and attribute in
`Cavalry.app/Contents/assets/Definitions/nodeDefinitions.json`. This resolves
the superType chain so we can check that every attribute we emit is real.
"""
import json, sys, os

DEFS = ("/Applications/Cavalry.app/Contents/assets/Definitions/nodeDefinitions.json")


def load(path=DEFS):
    entries = json.load(open(path))
    return {e["nodeType"]: e for e in entries if isinstance(e, dict) and "nodeType" in e}


def resolve(defs, node_type, _seen=None):
    """Full attribute dict for a node type, walking its superType chain."""
    _seen = _seen or set()
    if node_type in _seen or node_type not in defs:
        return {}
    _seen.add(node_type)
    e = defs[node_type]
    attrs = {}
    sup = e.get("superType")
    if sup:
        attrs.update(resolve(defs, sup, _seen))
    attrs.update(e.get("attributes") or {})
    return attrs


def main():
    defs = load()
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        nt = sys.argv[2]
        attrs = resolve(defs, nt)
        chain, cur = [], nt
        while cur:
            chain.append(cur)
            cur = defs.get(cur, {}).get("superType")
        print(f"{nt}  (chain: {' <- '.join(chain)})  {len(attrs)} attributes")
        want = sys.argv[3:] 
        for k in sorted(attrs):
            if want and not any(w.lower() in k.lower() for w in want):
                continue
            print(f"  {k}: {json.dumps(attrs[k])[:160]}")
        return

    # audit mode: check a generated .cv
    scene = json.load(open(sys.argv[1]))
    bad = {}
    for n in scene["nodes"]:
        nt = n["nodeType"]
        if nt not in defs:
            bad.setdefault("UNKNOWN NODE TYPE", set()).add(nt)
            continue
        attrs = resolve(defs, nt)
        for name in n.get("attributes", {}):
            if name not in attrs:
                bad.setdefault(nt, set()).add(name)
    # and connection endpoints
    for c in scene["connections"]:
        for side in ("from", "to"):
            nid, _, attr = c[side].partition(".")
            nt = next((x["nodeType"] for x in scene["nodes"] if x["nodeId"] == nid), None)
            if not nt or nt not in defs:
                continue
            root = attr.split(".")[0]
            if root and root not in resolve(defs, nt):
                bad.setdefault(f"{nt} (connection)", set()).add(root)
    if not bad:
        print("all emitted attributes exist in the Cavalry schema")
    for k in sorted(bad):
        print(f"{k}: {sorted(bad[k])}")


main()
