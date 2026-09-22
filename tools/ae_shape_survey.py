"""Enumerate every shape-related property AE uses across a set of projects.

Adobe's match names are the stable identifiers; the display names are not.
This walks each shape layer's full property tree and tallies what appears, so
the converter can be built against what really occurs rather than guesswork.
"""
import sys, collections
import py_aep

counts = collections.Counter()
examples = {}
paths = sys.argv[1:]

def walk(group, depth, comp, layer, trail):
    for p in group:
        mn = getattr(p, "match_name", "") or ""
        counts[mn] += 1
        examples.setdefault(mn, (p.name, comp, layer, " > ".join(trail[-3:])))
        if depth < 9:
            try:
                iter(p)
            except TypeError:
                continue
            walk(p, depth + 1, comp, layer, trail + [mn])

for path in paths:
    try:
        app = py_aep.parse(path)
    except Exception as e:
        print(f"!! {path}: {e}")
        continue
    for comp in app.project.compositions:
        for layer in comp.layers:
            if type(layer).__name__ != "ShapeLayer":
                continue
            try:
                root = layer.property("ADBE Root Vectors Group")
            except Exception:
                continue
            walk(root, 0, comp.name, layer.name, [])

print(f"{'count':>6}  match name")
for mn, n in counts.most_common():
    name, comp, layer, trail = examples[mn]
    print(f"{n:>6}  {mn:<42} e.g. {name!r} in {layer!r}")
