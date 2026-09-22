"""Convert After Effects shape layers into Cavalry shape graphs.

An AE shape layer is a tree. `Contents` holds groups, and each group holds
paths, fills, strokes and modifiers, plus its own transform - and groups nest.
Adobe's display names are unstable, so everything here keys off match names.

Cavalry models the same ideas differently:

* geometry is a `basicShape` plus a *generator* (rectangle/ellipse/star), or an
  `editableShape` carrying a bezier path
* paint is a `colorMaterial` (fill) and a `strokeMaterial` (stroke) attached to
  a shape, rather than entries in a list
* modifiers are *deformers* attached to the shape they modify
* grouping is a `group` node, which is itself a shape and can be transformed

So the conversion mirrors AE's tree with Cavalry groups, and turns each AE
modifier into the corresponding deformer or material setting.
"""

from __future__ import annotations

import math

from . import cvdoc, expressions

# -- AE match names --------------------------------------------------------
GROUP = "ADBE Vector Group"
CONTENTS = "ADBE Vectors Group"
XFORM = "ADBE Vector Transform Group"

PATH = "ADBE Vector Shape - Group"
RECT = "ADBE Vector Shape - Rect"
ELLIPSE = "ADBE Vector Shape - Ellipse"
STAR = "ADBE Vector Shape - Star"

FILL = "ADBE Vector Graphic - Fill"
GRAD_FILL = "ADBE Vector Graphic - G-Fill"
STROKE = "ADBE Vector Graphic - Stroke"
GRAD_STROKE = "ADBE Vector Graphic - G-Stroke"

MERGE = "ADBE Vector Filter - Merge"
TRIM = "ADBE Vector Filter - Trim"
ROUND_CORNERS = "ADBE Vector Filter - RC"
OFFSET_PATHS = "ADBE Vector Filter - Offset"
REPEATER = "ADBE Vector Filter - Repeater"

GENERATORS = {RECT: "rectangleShape", ELLIPSE: "ellipseShape", STAR: "starShape"}
GEOMETRY = set(GENERATORS) | {PATH}

# Modifiers we knowingly drop, so they can be reported rather than ignored.
UNSUPPORTED = {
    "ADBE Vector Filter - Twist": "Twist",
    "ADBE Vector Filter - PB": "Pucker & Bloat",
    "ADBE Vector Filter - Roughen": "Roughen Edges",
    "ADBE Vector Filter - Wiggler": "Wiggle Paths",
    "ADBE Vector Filter - Zigzag": "Zig Zag",
    "ADBE Vector Filter - RD": "Wiggle Transform",
    "ADBE Vector Stroke Taper": "stroke taper",
    "ADBE Vector Stroke Wave": "stroke wave",
}

# AE merge modes (1-5) -> Cavalry boolean clipping modes.
MERGE_MODES = {1: 0, 2: 0, 3: 1, 4: 2, 5: 3}
# AE line caps/joins are 1-based; Cavalry's are 0-based in the same order.
CAP_JOIN = {1: 0, 2: 1, 3: 2}


def _val(group, match_name):
    try:
        return expressions.effective(group.property(match_name)).value
    except Exception:
        return None


def _prop(group, match_name):
    try:
        return expressions.effective(group.property(match_name))
    except Exception:
        return None


class ShapeBuilder:
    """Builds the Cavalry node graph for one AE shape layer."""

    def __init__(self, converter, comp, comp_node):
        self.conv = converter
        self.doc = converter.doc
        self.comp = comp
        self.comp_node = comp_node
        self.flip = -1.0 if converter.flip_y else 1.0

    # -- entry point ------------------------------------------------------
    def build(self, layer, name):
        """The layer becomes a Cavalry group carrying the layer transform."""
        container = self.doc.create("group", name)
        try:
            root = layer.property("ADBE Root Vectors Group")
        except Exception:
            return container
        made = self.build_contents(root, container, name)
        if not self.doc.list_children(container):
            self.conv.report.skip(self.comp.name, name,
                                  "shape layer had no convertible contents")
        return container

    def build_contents(self, contents, container, name):
        """Walk one level of AE's Contents; return shapes still awaiting paint.

        AE applies a fill or stroke to every path in its group, including
        paths inside nested groups that carry no paint of their own. Cavalry
        attaches paint per shape, so unpainted shapes bubble up from nested
        groups and the nearest enclosing fill claims them.
        """
        groups, geometry, fills, strokes, modifiers = [], [], [], [], []
        for item in contents:
            mn = getattr(item, "match_name", "") or ""
            if mn == GROUP:
                groups.append(item)
            elif mn in GEOMETRY:
                geometry.append(item)
            elif mn in (FILL, GRAD_FILL):
                fills.append(item)
            elif mn in (STROKE, GRAD_STROKE):
                strokes.append(item)
            elif mn in (MERGE, TRIM, ROUND_CORNERS, OFFSET_PATHS, REPEATER):
                modifiers.append(item)
            elif mn in UNSUPPORTED:
                self.conv.report.note(
                    f"Shape modifier not converted: {UNSUPPORTED[mn]}.")

        shapes = []
        for geo in geometry:
            node = self.build_geometry(geo, name)
            if node is None:
                continue
            shapes.append(node)
            self.doc.add_child(container, node)

        # Nested groups first, so their unpainted shapes can be claimed here.
        for grp in groups:
            gname = getattr(grp, "name", None) or name
            inner = _prop(grp, CONTENTS)
            if inner is None:
                continue
            sub = self.doc.create("group", gname)
            unpainted = self.build_contents(inner, sub, gname)
            self.apply_group_transform(sub, grp)
            self.doc.add_child(container, sub)
            shapes.extend(unpainted)

        if not shapes:
            return []

        fill_node = self.build_fill(fills[0], name) if fills else None
        stroke_node = self.build_stroke(strokes[0], name) if strokes else None
        if len(fills) > 1 or len(strokes) > 1:
            self.conv.report.note(
                "A shape group had multiple fills or strokes; only the first "
                "of each is converted.")
        for node in shapes:
            if fill_node:
                self.doc.connect(f"{fill_node}.id", f"{node}.material")
            if stroke_node:
                self.doc.connect(f"{stroke_node}.id", f"{node}.stroke")
        if fill_node:
            self.doc.add_child(container, fill_node)
        if stroke_node:
            self.doc.add_child(container, stroke_node)

        for mod in modifiers:
            self.apply_modifier(mod, shapes, container, stroke_node, name)

        # Painted here, so nothing bubbles further up.
        return [] if (fill_node or stroke_node) else shapes

    # -- geometry ---------------------------------------------------------
    def build_geometry(self, geo, name):
        mn = geo.match_name
        if mn == PATH:
            return self.build_path(geo, name)

        node = self.doc.create("basicShape", getattr(geo, "name", None) or name)
        gen_type = GENERATORS[mn]
        gen = self.doc.create(gen_type, f"{name} {gen_type}")
        self.doc.connect(f"{gen}.id", f"{node}.generator")
        self.doc.add_child(node, gen)

        if mn == RECT:
            size = _val(geo, "ADBE Vector Rect Size")
            if size:
                self.doc.set(gen, dimensions=cvdoc.double2(size[0], size[1]))
            r = _val(geo, "ADBE Vector Rect Roundness")
            if r:
                self.doc.set(gen, cornerRadius=cvdoc.double(r))
            offset = _val(geo, "ADBE Vector Rect Position")
        elif mn == ELLIPSE:
            size = _val(geo, "ADBE Vector Ellipse Size")
            if size:
                # AE gives diameters, Cavalry wants radii.
                self.doc.set(gen, radius=cvdoc.double2(size[0] / 2.0, size[1] / 2.0))
            offset = _val(geo, "ADBE Vector Ellipse Position")
        else:
            outer = _val(geo, "ADBE Vector Star Outer Radius")
            inner = _val(geo, "ADBE Vector Star Inner Radius")
            points = _val(geo, "ADBE Vector Star Points")
            star_type = _val(geo, "ADBE Vector Star Type")
            if outer:
                self.doc.set(gen, radius=cvdoc.double2(outer, outer))
            if points:
                self.doc.set(gen, sides=cvdoc.integer(int(points)))
            # AE type 1 is a star, 2 a plain polygon with no inner radius.
            if star_type == 1 and inner:
                self.doc.set(gen,
                             useInnerRadius=cvdoc.boolean(True),
                             innerRadius=cvdoc.double(inner))
            rot = _val(geo, "ADBE Vector Star Rotation")
            if rot:
                self.doc.set(node, rotation=cvdoc.double3(0, 0, self.conv.to_cv_angle(rot)))
            offset = _val(geo, "ADBE Vector Star Position")

        if offset and (abs(offset[0]) > 1e-6 or abs(offset[1]) > 1e-6):
            self.doc.set(node, position=cvdoc.double3(offset[0], offset[1] * self.flip))
        return node

    def build_path(self, geo, name):
        """AE bezier path -> Cavalry's editablePath2.

        Cavalry stores [in handle, out handle, vertex] in absolute
        coordinates; AE stores tangents relative to their vertex.
        """
        shape = _val(geo, "ADBE Vector Shape")
        verts = list(getattr(shape, "vertices", None) or []) if shape else []
        if not verts:
            return None
        ins = list(getattr(shape, "in_tangents", None) or [])
        outs = list(getattr(shape, "out_tangents", None) or [])

        points = []
        for i, v in enumerate(verts):
            vx, vy = float(v[0]), float(v[1]) * self.flip
            it = ins[i] if i < len(ins) else (0.0, 0.0)
            ot = outs[i] if i < len(outs) else (0.0, 0.0)
            points.append({
                "gradientLocked": True,
                "weightLocked": False,
                "points": [
                    {"x": vx + float(it[0]), "y": vy + float(it[1]) * self.flip},
                    {"x": vx + float(ot[0]), "y": vy + float(ot[1]) * self.flip},
                    {"x": vx, "y": vy},
                ],
            })

        node = self.doc.create("editableShape", getattr(geo, "name", None) or name)
        self.doc.set_raw(node, "inputPath", {
            "value": {"contours": [{"closed": bool(getattr(shape, "closed", True)),
                                    "points": points}]},
            "varType": "editablePath2",
        })
        return node

    # -- paint ------------------------------------------------------------
    def build_fill(self, fill, name):
        node = self.doc.create("colorMaterial", f"{name} Fill", {
            "showInProjectWindow": cvdoc.boolean(False)})
        if fill.match_name == GRAD_FILL:
            self.build_gradient(node, fill, name)
            return node
        col = _val(fill, "ADBE Vector Fill Color")
        opacity = _val(fill, "ADBE Vector Fill Opacity")
        if col:
            alpha = 255 * (opacity / 100.0 if opacity is not None else 1.0)
            self.doc.set(node, materialColor=cvdoc.color(
                col[0] * 255, col[1] * 255, col[2] * 255, alpha))
        return node

    def build_gradient(self, material, fill, name):
        """colorMaterial <- gradientShader <- linear/radialGradientShader."""
        kind = _val(fill, "ADBE Vector Grad Type")
        gen_type = "radialGradientShader" if kind == 2 else "linearGradientShader"
        shader = self.doc.create("gradientShader", f"{name} Gradient")
        generator = self.doc.create(gen_type, f"{name} Gradient Ramp")
        stops = self.gradient_stops(fill)
        if stops:
            self.doc.set_raw(generator, "gradient", {"list": stops})
        self.gradient_placement(generator, fill)
        self.doc.connect(f"{generator}.id", f"{shader}.generator")
        self.doc.connect(f"{shader}.id", f"{material}.colorShaders.0.shader")
        self.doc.set_raw(material, "colorShaders", cvdoc.slot_list(1))
        self.doc.add_child(material, shader)
        self.doc.add_child(shader, generator)

    @staticmethod
    def gradient_stops(fill):
        """AE keeps colour and alpha on separate ramps; Cavalry wants RGBA."""
        grad = _val(fill, "ADBE Vector Grad Colors")
        colors = list(getattr(grad, "color_stops", None) or ())
        alphas = sorted((a.offset, a.alpha) for a in getattr(grad, "alpha_stops", None) or ())

        def alpha_at(offset):
            if not alphas:
                return 1.0
            if offset <= alphas[0][0]:
                return alphas[0][1]
            if offset >= alphas[-1][0]:
                return alphas[-1][1]
            for (o0, a0), (o1, a1) in zip(alphas, alphas[1:]):
                if o0 <= offset <= o1:
                    t = 0.0 if o1 == o0 else (offset - o0) / (o1 - o0)
                    return a0 + (a1 - a0) * t
            return 1.0

        stops = []
        for stop in colors:
            r, g, b = stop.color[:3]
            entry = {"color": cvdoc.color(r * 255, g * 255, b * 255,
                                          alpha_at(stop.offset) * 255)}
            if stop.offset:
                entry["position"] = cvdoc.double(stop.offset)
            stops.append({"compound": entry})
        return stops

    def gradient_placement(self, generator, fill):
        """Match AE's start/end points with a centre, angle and length."""
        start = _val(fill, "ADBE Vector Grad Start Pt")
        end = _val(fill, "ADBE Vector Grad End Pt")
        if not start or not end:
            return
        dx, dy = float(end[0]) - float(start[0]), float(end[1]) - float(start[1])
        cx, cy = (float(start[0]) + float(end[0])) / 2.0, (float(start[1]) + float(end[1])) / 2.0
        self.doc.set(generator, offset=cvdoc.double2(cx, cy * self.flip))
        angle = math.degrees(math.atan2(dy * self.flip, dx))
        if angle:
            self.doc.set(generator, rotation=cvdoc.double(angle))
        length = math.hypot(dx, dy)
        if length:
            self.doc.set(generator, scale=cvdoc.double2(length, length))

    def build_stroke(self, stroke, name):
        node = self.doc.create("strokeMaterial", f"{name} Stroke", {
            "showInProjectWindow": cvdoc.boolean(False)})
        col = _val(stroke, "ADBE Vector Stroke Color")
        opacity = _val(stroke, "ADBE Vector Stroke Opacity")
        if col:
            alpha = 255 * (opacity / 100.0 if opacity is not None else 1.0)
            self.doc.set(node, strokeColor=cvdoc.color(
                col[0] * 255, col[1] * 255, col[2] * 255, alpha))

        self.conv.set_or_animate(self.comp, self.comp_node, node, "width",
                                 _prop(stroke, "ADBE Vector Stroke Width"))

        cap = _val(stroke, "ADBE Vector Stroke Line Cap")
        if cap in CAP_JOIN:
            self.doc.set(node, capStyle=cvdoc.enum(CAP_JOIN[cap]))
        join = _val(stroke, "ADBE Vector Stroke Line Join")
        if join in CAP_JOIN:
            self.doc.set(node, joinStyle=cvdoc.enum(CAP_JOIN[join]))
        miter = _val(stroke, "ADBE Vector Stroke Miter Limit")
        if miter:
            self.doc.set(node, miter=cvdoc.double(miter))

        # AE lists dashes as separate Dash/Gap properties; Cavalry takes a
        # single "dash, gap" string.
        pattern = [v for v in (
            _val(stroke, "ADBE Vector Stroke Dash 1"),
            _val(stroke, "ADBE Vector Stroke Gap 1"),
            _val(stroke, "ADBE Vector Stroke Dash 2"),
            _val(stroke, "ADBE Vector Stroke Gap 2"),
            _val(stroke, "ADBE Vector Stroke Dash 3"),
            _val(stroke, "ADBE Vector Stroke Gap 3"),
        ) if v]
        if pattern:
            self.doc.set(node, dashPattern=cvdoc.string(
                ", ".join(f"{v:g}" for v in pattern)))
            offset = _val(stroke, "ADBE Vector Stroke Offset")
            if offset:
                self.doc.set(node, dashOffset=cvdoc.double(offset))
        return node

    # -- modifiers --------------------------------------------------------
    def apply_modifier(self, mod, shapes, container, stroke_node, name):
        mn = mod.match_name
        if mn == MERGE and len(shapes) > 1:
            self.apply_merge(mod, shapes, container, name)
        elif mn == TRIM:
            self.apply_trim(mod, stroke_node, name)
        elif mn == ROUND_CORNERS:
            self.apply_round(mod, shapes, name)
        elif mn == OFFSET_PATHS:
            self.apply_offset(mod, shapes, name)
        elif mn == REPEATER:
            self.apply_repeater(mod, shapes, container, name)

    def apply_merge(self, mod, shapes, container, name):
        """AE Merge Paths -> a Cavalry boolean deformer on the first shape."""
        mode = MERGE_MODES.get(_val(mod, "ADBE Vector Merge Type"), 0)
        node = self.doc.create("boolean", f"{name} Merge")
        others = shapes[1:]
        self.doc.set_raw(node, "clippingShapes", {
            "list": [{"compound": {"mode": cvdoc.integer(mode)}} for _ in others]})
        for i, other in enumerate(others):
            self.doc.connect(f"{other}.id", f"{node}.clippingShapes.{i}.id")
        self.attach_deformer(shapes[0], node)
        self.doc.add_child(shapes[0], node)
        self.conv.report.note(
            "Merge Paths is converted to Cavalry's boolean deformer; complex "
            "combinations may need adjusting.")

    def apply_trim(self, mod, stroke_node, name):
        """AE Trim Paths -> Cavalry's stroke trim (stroke only)."""
        if stroke_node is None:
            self.conv.report.note(
                "Trim Paths on a group with no stroke is not converted - "
                "Cavalry trims strokes, not fills.")
            return
        self.doc.set(stroke_node, trim=cvdoc.boolean(True))
        for attr, mn in (("trimStart", "ADBE Vector Trim Start"),
                         ("trimEnd", "ADBE Vector Trim End"),
                         ("trimTravel", "ADBE Vector Trim Offset")):
            self.conv.set_or_animate(self.comp, self.comp_node, stroke_node,
                                     attr, _prop(mod, mn))

    def apply_round(self, mod, shapes, name):
        radius = _prop(mod, "ADBE Vector RoundCorner Radius")
        for shape in shapes:
            node = self.doc.create("round", f"{name} Round Corners")
            self.conv.set_or_animate(self.comp, self.comp_node, node,
                                     "value", radius)
            self.attach_deformer(shape, node)
            self.doc.add_child(shape, node)

    def apply_offset(self, mod, shapes, name):
        amount = _prop(mod, "ADBE Vector Offset Amount")
        for shape in shapes:
            node = self.doc.create("pathOffsetBehaviour", f"{name} Offset Path")
            self.conv.set_or_animate(self.comp, self.comp_node, node,
                                     "offset", amount)
            self.attach_deformer(shape, node)
            self.doc.add_child(shape, node)

    def apply_repeater(self, mod, shapes, container, name):
        """AE Repeater -> a Cavalry duplicator wrapping the group's shapes."""
        copies = _val(mod, "ADBE Vector Repeater Copies")
        node = self.doc.create("duplicator", f"{name} Repeater")
        self.doc.set_raw(node, "shapes", cvdoc.node_id_list(len(shapes)))
        for i, shape in enumerate(shapes):
            self.doc.connect(f"{shape}.id", f"{node}.shapes.{i}")
        if copies:
            # A zero-size line keeps every copy in place; the per-copy
            # transform below is what spaces them out, as AE does.
            gen = self.doc.create("linearDistribution", f"{name} Repeater Count",
                                  {"count": cvdoc.integer(int(copies)),
                                   "size": cvdoc.double(0)})
            self.doc.connect(f"{gen}.id", f"{node}.generator")
            self.doc.add_child(node, gen)

        try:
            tg = mod.property("ADBE Vector Repeater Transform")
        except Exception:
            tg = None
        if tg is not None:
            pos = _val(tg, "ADBE Vector Repeater Position")
            if pos:
                self.doc.set(node, shapePosition=cvdoc.double2(
                    pos[0], pos[1] * self.flip))
            rot = _val(tg, "ADBE Vector Repeater Rotation")
            if rot:
                self.doc.set(node, shapeRotation=cvdoc.double(
                    self.conv.to_cv_angle(rot)))
            scale = _val(tg, "ADBE Vector Repeater Scale")
            if scale:
                self.doc.set(node, shapeScale=cvdoc.double2(
                    scale[0] / 100.0, scale[1] / 100.0))
        self.doc.add_child(container, node)

    def attach_deformer(self, shape, deformer):
        """Append to the shape's deformers list, growing it as needed."""
        index = self.doc.list_length(shape, "deformers")
        self.doc.set_raw(shape, "deformers", cvdoc.node_id_list(index + 1))
        self.doc.connect(f"{deformer}.id", f"{shape}.deformers.{index}")

    # -- group transform --------------------------------------------------
    def apply_group_transform(self, node, group):
        """AE's per-group transform, which sits inside the shape layer."""
        tg = _prop(group, XFORM)
        if tg is None:
            return
        pos = _val(tg, "ADBE Vector Position") or (0.0, 0.0)
        anchor = _val(tg, "ADBE Vector Anchor")
        scale = _val(tg, "ADBE Vector Scale")
        rot = _val(tg, "ADBE Vector Rotation")
        opacity = _val(tg, "ADBE Vector Group Opacity")
        skew = _val(tg, "ADBE Vector Skew")

        px, py = float(pos[0]), float(pos[1])
        if abs(px) > 1e-6 or abs(py) > 1e-6:
            self.doc.set(node, position=cvdoc.double3(px, py * self.flip))
        if anchor and (abs(anchor[0]) > 1e-6 or abs(anchor[1]) > 1e-6):
            self.doc.set(node, pivot=cvdoc.double2(anchor[0], anchor[1] * self.flip))
        if scale and (abs(scale[0] - 100) > 1e-6 or abs(scale[1] - 100) > 1e-6):
            self.doc.set(node, scale=cvdoc.scale2(scale[0] / 100.0, scale[1] / 100.0))
        if rot:
            self.doc.set(node, rotation=cvdoc.double3(0, 0, self.conv.to_cv_angle(rot)))
        if opacity is not None and abs(opacity - 100.0) > 1e-6:
            self.doc.set(node, opacity=cvdoc.double(opacity))
        if skew:
            self.doc.set(node, skew=cvdoc.double2(skew, 0.0))
