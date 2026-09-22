"""Builder for Cavalry .cv scene files.

A .cv file is plain JSON with four parts that matter here:

* ``nodes``       - flat list of ``{nodeId, nodeType, attributes}``
* ``nodeMeta``    - the scene hierarchy, as ``{nodeId, children: [...]}``
* ``connections`` - the node graph, as ``{from: "node.attr", to: "node.attr"}``
* ``assets``      - external file references

Attributes are sparse: Cavalry only serialises non-default values, so we only
write what we actually know.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import OrderedDict

# Version stamp of the example scene this builder was modelled on.
CV_VERSION = "1.0.62"


def _v(value, var_type):
    return {"value": value, "varType": var_type}


def string(s):
    return _v(s, "string")


def double(x):
    return _v(float(x), "double")


def boolean(b):
    return _v(bool(b), "bool")


def integer(i):
    return _v(int(i), "int")


def enum(i):
    return _v(int(i), "enum")


def double2(x, y):
    return _v({"x": float(x), "y": float(y)}, "double2")


def double3(x, y, z=0.0):
    return _v({"x": float(x), "y": float(y), "z": float(z)}, "double3")


def int2(x, y):
    return _v({"x": int(x), "y": int(y)}, "int2")


def color(r, g, b, a=255):
    """Cavalry colours are 0-255 integer RGBA."""
    clamp = lambda n: max(0, min(255, int(round(n))))
    return _v({"r": clamp(r), "g": clamp(g), "b": clamp(b), "a": clamp(a)}, "color")


def font(family, style="Regular"):
    return _v({"font": str(family), "style": str(style)}, "font")


def scale2(x, y, aspect=1.0):
    d = double2(x, y)
    d["aspectRatio"] = float(aspect)
    return d


def node_id_list(n):
    """A list-valued attribute of node references; the actual ids come from
    ``connections`` entries targeting ``<attr>.<index>``."""
    return {"list": [{"varType": "nodeId"} for _ in range(n)]}


def slot_list(n):
    """A list-valued attribute whose entries are compound slots, filled by
    connections targeting ``<attr>.<index>.<field>`` (masks, colorShaders)."""
    return {"list": [{} for _ in range(n)]}


class CvDoc:
    """Accumulates nodes, hierarchy and connections, then serialises to .cv."""

    def __init__(self):
        self._counters = {}
        self._nodes = OrderedDict()
        self._children = {}
        self._connections = []
        self._assets = []
        self.active_comp = None
        self.root_asset = None

    # -- node creation ----------------------------------------------------
    def create(self, node_type, nice_name=None, attrs=None, with_uuid=True):
        n = self._counters.get(node_type, 0) + 1
        self._counters[node_type] = n
        node_id = f"{node_type}#{n}"

        attributes = {}
        if with_uuid:
            attributes["uuid"] = string(str(uuid.uuid4()))
        if nice_name is not None:
            attributes["niceName"] = string(nice_name)
        if attrs:
            attributes.update(attrs)

        self._nodes[node_id] = {
            "attributes": attributes,
            "nodeId": node_id,
            "nodeType": node_type,
        }
        return node_id

    def set(self, node_id, **attrs):
        self._nodes[node_id]["attributes"].update(attrs)

    def set_raw(self, node_id, key, value):
        self._nodes[node_id]["attributes"][key] = value

    def list_children(self, node_id):
        return self._children.get(node_id, [])

    def list_length(self, node_id, attr):
        """How many slots a list-valued attribute currently has."""
        value = self._nodes[node_id]["attributes"].get(attr)
        return len(value.get("list", [])) if isinstance(value, dict) else 0

    def context_filter(self, node_id, values):
        self._nodes[node_id]["contextFilter"] = list(values)

    # -- graph ------------------------------------------------------------
    def connect(self, frm, to):
        self._connections.append({"from": frm, "to": to})

    def add_child(self, parent_id, child_id):
        self._children.setdefault(parent_id, []).append(child_id)

    def add_asset(self, file_path, nice_name=None):
        node_id = self.create("asset", nice_name)
        if self.root_asset:
            self.add_child(self.root_asset, node_id)
        self._assets.append(
            {
                "data": {"filePath": str(file_path)},
                "nodeId": node_id,
                "type": "file",
            }
        )
        return node_id

    def asset_paths(self):
        """(nodeId, filePath) for every external file the scene references."""
        return [(a["nodeId"], a["data"]["filePath"]) for a in self._assets]

    def set_asset_path(self, node_id, file_path):
        for a in self._assets:
            if a["nodeId"] == node_id:
                a["data"]["filePath"] = str(file_path)
                return
        raise KeyError(node_id)

    # -- serialisation ----------------------------------------------------
    def to_dict(self):
        node_meta = [
            {"children": kids, "nodeId": nid}
            for nid, kids in self._children.items()
            if kids
        ]
        return {
            "activeComp": self.active_comp or "",
            "assets": self._assets,
            "connections": self._connections,
            "dependencyGraph": {
                "tabs": [
                    {"compId": nid, "name": "", "positions": []}
                    for nid, n in self._nodes.items()
                    if n["nodeType"] == "compNode"
                ]
            },
            "labelColors": "Pastel",
            "nodeMeta": node_meta,
            "nodes": list(self._nodes.values()),
            "other": {"viewport": {"backgroundMode": 0, "rulers": {}}},
            "referenceData": {},
            "resourceType": "cavalry.scenefile",
            "trackingData": {},
            "version": CV_VERSION,
        }

    def write(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1, sort_keys=True)
            fh.write("\n")

    def add_scene_scaffolding(self):
        """The housekeeping nodes every Cavalry scene carries.

        ``asset#1`` is the scene root: compositions and imported files hang
        off it, which is what populates Cavalry's project window.
        """
        self.root_asset = self.create("asset")
        self.create("renderQueue", None, with_uuid=False)
        self.create("dynamicIndexManager", "Render Manager")
        palette = self.create("paletteContainer", None, with_uuid=False)
        swatches = self.create("colorArray", "Default Scene Palette")
        self.add_child(palette, swatches)
