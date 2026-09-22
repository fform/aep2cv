"""Map a parsed After Effects project onto a Cavalry scene.

What survives the trip, and what does not, is documented in README.md. The
short version: composition settings, the layer tree (types, order, parenting),
layer in/out points, transforms and transform keyframes. Effects, masks and
shape-layer contents do not - they have no faithful Cavalry equivalent.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import py_aep

from . import cvdoc, fonts, vector
from .shapes import ShapeBuilder
from .cvdoc import CvDoc

# AE transform match names -> the Cavalry attribute channels they feed.
POSITION = "ADBE Position"
POSITION_SEPARATED = ("ADBE Position_0", "ADBE Position_1", "ADBE Position_2")
ANCHOR = "ADBE Anchor Point"
SCALE = "ADBE Scale"
ROTATION_Z = "ADBE Rotate Z"
ROTATION_X = "ADBE Rotate X"
ROTATION_Y = "ADBE Rotate Y"
OPACITY = "ADBE Opacity"
TRANSFORM_GROUP = "ADBE Transform Group"



@dataclass
class Report:
    """What the conversion did, and what it had to drop."""

    comps: int = 0
    layers: int = 0
    keyframes: int = 0
    skipped: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    gathered: list = field(default_factory=list)
    unresolved_fonts: list = field(default_factory=list)
    vectorised: list = field(default_factory=list)
    unconverted_vectors: list = field(default_factory=list)

    def skip(self, comp, layer, why):
        self.skipped.append(f"{comp} / {layer}: {why}")

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)


class Converter:
    def __init__(self, project, flip_y=True, vector_dir=None):
        self.project = project
        self.flip_y = flip_y
        self.vector_dir = vector_dir
        self.doc = CvDoc()
        self.report = Report()
        self.comp_nodes = {}          # AE CompItem id -> Cavalry compNode id
        self.assets = {}              # file path -> Cavalry asset id
        self.missing = set()          # referenced files not present on disk
        self.shader_of = {}           # footageShape id -> its imageShader id

    # -- coordinates ------------------------------------------------------
    def to_cv_point(self, x, y, comp, parented):
        """AE uses a top-left origin with Y pointing down; Cavalry's origin is
        the centre of the composition with Y pointing up.

        A parented layer's position is already expressed relative to its
        parent in both applications, so it needs no recentring - but it still
        needs the axis flip.
        """
        if parented:
            cx, cy = float(x), float(y)
        else:
            cx = float(x) - comp.width / 2.0
            cy = float(y) - comp.height / 2.0
        if self.flip_y:
            cy = -cy
        return cx, cy

    @staticmethod
    def _anchor_of(layer):
        try:
            a = layer.property(TRANSFORM_GROUP).property(ANCHOR).value
            return float(a[0]), float(a[1])
        except Exception:
            return 0.0, 0.0

    def to_cv_angle(self, degrees):
        """Flipping the Y axis reverses the direction of rotation."""
        return -float(degrees) if self.flip_y else float(degrees)

    # -- entry point ------------------------------------------------------
    def run(self):
        doc = self.doc
        doc.add_scene_scaffolding()

        comps = list(self.project.compositions)
        # Compositions first, so precomp layers have something to point at.
        for comp in comps:
            node = self.make_comp(comp)
            self.comp_nodes[comp.id] = node
            doc.add_child(doc.root_asset, node)
        for comp in comps:
            self.populate_comp(comp)

        if comps:
            doc.active_comp = self.comp_nodes[comps[0].id]
        self.report.missing = sorted(self.missing)
        return doc, self.report

    def make_comp(self, comp):
        bg = comp.bg_color or [0.0, 0.0, 0.0]
        # Composition length is startFrame/endFrame, with playbackStart/End as
        # the range the timeline plays - both, or the comp keeps its default
        # length. (Cavalry's own scripts set endFrame and playbackEnd together.)
        frames = int(round(comp.duration * comp.frame_rate))
        attrs = {
            "resolution": cvdoc.int2(comp.width, comp.height),
            "fps": cvdoc.double(comp.frame_rate),
            "startFrame": cvdoc.double(0),
            "endFrame": cvdoc.double(frames),
            "playbackStart": cvdoc.double(0),
            "playbackEnd": cvdoc.double(frames),
            "backgroundColor": cvdoc.color(bg[0] * 255, bg[1] * 255, bg[2] * 255, 255),
        }
        node = self.doc.create("compNode", comp.name, attrs)
        self.report.comps += 1
        return node

    # -- layers -----------------------------------------------------------
    def populate_comp(self, comp):
        comp_node = self.comp_nodes[comp.id]
        made = {}   # AE layer index -> Cavalry node id

        for layer in comp.layers:
            node = self.make_layer(comp, comp_node, layer)
            if node is None:
                continue
            made[layer.index] = node
            self.report.layers += 1

        # Hierarchy: a parented layer nests under its parent, everything else
        # hangs off the composition. Done in a second pass so forward
        # references to a parent defined later in the stack still resolve.
        for layer in comp.layers:
            node = made.get(layer.index)
            if node is None:
                continue
            parent = layer.parent
            parent_node = made.get(parent.index) if parent is not None else None
            if parent_node:
                # Nesting in nodeMeta is the parenting mechanism; hierarchy
                # already defaults to true, so leave it alone.
                self.doc.add_child(parent_node, node)
            else:
                self.doc.add_child(comp_node, node)
                self.doc.set(node, hierarchy=cvdoc.boolean(False))

    def make_layer(self, comp, comp_node, layer):
        kind = type(layer).__name__
        source = getattr(layer, "source", None)
        name = layer.name or kind

        node = None
        source_size = None
        origin_offset = (0.0, 0.0)

        if kind in ("CameraLayer", "LightLayer"):
            self.report.skip(comp.name, name, f"{kind} has no Cavalry equivalent")
            return None

        if kind == "TextLayer":
            node = self.doc.create("textShape", name)
            origin_offset = self.apply_text(node, layer, name)

        elif kind == "ShapeLayer":
            node = ShapeBuilder(self, comp, comp_node).build(layer, name)

        elif getattr(layer, "null_layer", False):
            node = self.doc.create("null", name)
            source_size = (source.width, source.height) if source else None

        elif isinstance(source, py_aep.CompItem):
            node = self.doc.create("compositionReference", name)
            target = self.comp_nodes.get(source.id)
            if target:
                self.doc.connect(f"{target}.id", f"{node}.composition")
            self.doc.connect(f"{comp_node}.time", f"{node}.time")
            # Cavalry tags a composition reference with the comp it draws in.
            self.doc.context_filter(node, [int(comp_node.split("#")[1])])
            source_size = (source.width, source.height)

        elif isinstance(source, py_aep.FootageItem):
            main = getattr(source, "main_source", None)
            source_size = (source.width, source.height)
            if isinstance(main, py_aep.SolidSource):
                node = self.doc.create(
                    "rectangleShape",
                    name,
                    {"dimensions": cvdoc.double2(source.width, source.height)},
                )
                col = main.color or [1.0, 1.0, 1.0]
                mat = self.doc.create(
                    "colorMaterial",
                    f"{name} Color",
                    {
                        "materialColor": cvdoc.color(
                            col[0] * 255, col[1] * 255, col[2] * 255
                        ),
                        "showInProjectWindow": cvdoc.boolean(False),
                    },
                )
                self.doc.connect(f"{mat}.id", f"{node}.material")
                self.doc.add_child(node, mat)
            elif isinstance(main, py_aep.FileSource) and main.file:
                node = self.make_vector_layer(name, main) or self.make_footage_layer(
                    name, source, main, comp, comp_node, layer)
            else:
                node = self.doc.create("null", name)
                self.report.skip(
                    comp.name, name, "unsupported footage source - placed as a null"
                )
        else:
            node = self.doc.create("null", name)
            self.report.skip(comp.name, name, f"unhandled layer kind {kind} - placed as a null")

        self.apply_transform(comp, layer, node, source_size, origin_offset)
        self.apply_timing(comp, comp_node, layer, node)
        return node

    def make_vector_layer(self, name, file_source):
        """Vector artwork becomes real geometry via Cavalry's SVG shape.

        Returns None when the file is not vector art, or when it needs
        converting and no converter is installed - the caller then falls back
        to treating it as footage.
        """
        path = str(file_source.file)
        if not (vector.is_svg(path) or vector.is_convertible(path)):
            return None
        if not os.path.exists(path):
            self.missing.add(path)
            return None

        if vector.is_convertible(path):
            if self.vector_dir is None:
                return None
            converted = vector.to_svg(path, self.vector_dir)
            if converted is None:
                if path not in self.report.unconverted_vectors:
                    self.report.unconverted_vectors.append(path)
                return None
            self.report.vectorised.append(f"{os.path.basename(path)} -> SVG")
            path = converted

        asset = self.assets.get(path)
        if asset is None:
            asset = self.doc.add_asset(path, os.path.basename(path))
            self.assets[path] = asset

        node = self.doc.create("svgShape", name)
        self.doc.connect(f"{asset}.id", f"{node}.input")
        return node

    def make_footage_layer(self, name, source, file_source, comp, comp_node, layer):
        """footageShape <- colorMaterial <- imageShader <- asset.

        The material bridge is what actually draws the pixels: an image shader
        wired straight to the shape renders nothing.

        Time is the subtle part. Cavalry pins footage to composition time, so
        a clip shows source frame N at composition frame N. After Effects
        instead offsets the source by the layer's start time, which is how a
        single clip gets cut into many pieces at different points in the
        source. We reproduce that by ramping the shader's time attribute
        rather than wiring composition time straight into it.
        """
        path = str(file_source.file)
        if not os.path.exists(path):
            self.missing.add(path)

        asset = self.assets.get(path)
        if asset is None:
            asset = self.doc.add_asset(path, os.path.basename(path))
            self.assets[path] = asset

        node = self.doc.create("footageShape", name)

        shader = self.doc.create(
            "imageShader", f"{name} Image Shader", {"colorId": cvdoc.integer(14)}
        )
        self.doc.connect(f"{asset}.id", f"{shader}.image")
        self.doc.connect(f"{comp_node}.fps", f"{shader}.fps")
        self.doc.connect(f"{comp_node}.fps", f"{shader}.inFps")
        self.doc.connect(f"{comp_node}.resolution", f"{shader}.resolution")

        fps = comp.frame_rate
        start_frame = int(round(layer.start_time * fps))
        if start_frame:
            self.trim_shader_time(comp, comp_node, shader, layer, start_frame)
        else:
            self.doc.connect(f"{comp_node}.time", f"{shader}.time")

        material = self.doc.create(
            "colorMaterial",
            f"{name} Material",
            {
                "colorShaders": cvdoc.slot_list(1),
                # The material's own colour defaults to opaque mid-grey, which
                # would matte anything with an alpha channel. Zero alpha lets
                # the image shader supply the pixels, transparency included -
                # this is what Cavalry itself writes for a footage material.
                "materialColor": cvdoc.color(100, 100, 100, 0),
                "showInProjectWindow": cvdoc.boolean(False),
            },
        )
        self.doc.connect(f"{shader}.id", f"{material}.colorShaders.0.shader")
        self.doc.connect(f"{material}.id", f"{node}.material")
        self.doc.connect(f"{shader}.id", f"{node}.footageShader")

        self.doc.add_child(node, shader)
        self.doc.add_child(node, material)
        self.shader_of[node] = shader
        return node

    def trim_shader_time(self, comp, comp_node, shader, layer, start_frame):
        """Ramp the shader's time so the clip plays its own slice of the source.

        Two keyframes, one frame of source per frame of composition, which is
        AE's ``source = composition - start`` offset expressed as a curve.
        """
        fps = comp.frame_rate
        in_frame = int(round(layer.in_point * fps))
        out_frame = int(round(layer.out_point * fps))
        if out_frame <= in_frame:
            out_frame = in_frame + 1

        curve = self.doc.create("animationCurve", None, with_uuid=False)
        self.doc.set_raw(curve, "keyframes", cvdoc.node_id_list(2))
        span = out_frame - in_frame
        handle = span / 3.0

        for i, frame in enumerate((in_frame, out_frame)):
            data = {
                "numValue": float(frame - start_frame),
                "interpolation": 0,
                "locked": True,
                "weightLocked": True,
                # Handles laid along the line keep playback at 1x under
                # Cavalry's bezier interpolation.
                "leftBez": {"x": -handle, "y": -handle},
                "rightBez": {"x": handle, "y": handle},
                "leftInfluence": 1 / 3.0,
                "rightInfluence": 1 / 3.0,
                "leftSpeed": float(fps),
                "rightSpeed": float(fps),
            }
            key = self.make_keyframe(frame, data)
            self.doc.connect(f"{key}.id", f"{curve}.keyframes.{i}")
            self.report.keyframes += 1

        self.doc.connect(f"{curve}.out", f"{shader}.time")
        self.doc.add_child(shader, curve)
        self.wire_curve_time(comp_node, curve)

    # AE justification enum names -> Cavalry's alignment index.
    ALIGNMENT = {"LEFT": 0, "CENTER": 1, "CENTRE": 1, "RIGHT": 2}

    def apply_text(self, node, layer, name):
        """Carry the string and formatting; return the text origin offset.

        The two applications anchor text differently. AE puts a point text
        layer's origin on the first baseline, and centres a box text layer on
        its origin. Cavalry positions from the top-left corner of the block.
        The offset we return converts one to the other.
        """
        doc = self._text_document(layer)
        if doc is None:
            self.report.note("A text layer's contents could not be read.")
            return (0.0, 0.0)

        text = getattr(doc, "text", None)
        if isinstance(text, str):
            self.doc.set(node, text=cvdoc.string(text))

        ae_font = getattr(doc, "font", None)
        size = getattr(doc, "font_size", None)
        if size:
            self.doc.set(node, fontSize=cvdoc.double(size))

        if getattr(doc, "box_text", False):
            box = getattr(doc, "box_text_size", None)
            if box:
                self.doc.set(node,
                             textBoxSize=cvdoc.double2(box[0], box[1]),
                             autoWidth=cvdoc.boolean(False),
                             autoHeight=cvdoc.boolean(False))
        else:
            self.doc.set(node, autoWidth=cvdoc.boolean(True))

        # AE records a PostScript name ("IBMPlexMono"); Cavalry wants the
        # family display name and style ("IBM Plex Mono" + "Regular") and will
        # not load the font from the PostScript name.
        if isinstance(ae_font, str) and ae_font:
            family, style, exact = fonts.resolve(ae_font)
            if family:
                self.doc.set_raw(node, "font", cvdoc.font(family, style))
            if not exact and ae_font not in self.report.unresolved_fonts:
                self.report.unresolved_fonts.append(ae_font)

        # AE leading is an absolute line height; Cavalry's lineSpacing is extra
        # space on top of the font's natural line height. AE's auto leading is
        # 1.2x the font size, so that is the baseline we subtract.
        leading = getattr(doc, "leading", None)
        if leading and size and not getattr(doc, "auto_leading", False):
            extra = leading - 1.2 * size
            if abs(extra) > 1e-6:
                self.doc.set(node, lineSpacing=cvdoc.double(extra))

        para = (getattr(doc, "space_before", 0) or 0) + (getattr(doc, "space_after", 0) or 0)
        if para:
            self.doc.set(node, paragraphSpacing=cvdoc.double(para))

        # AE tracking is thousandths of an em; Cavalry spaces in units.
        tracking = getattr(doc, "tracking", None)
        if tracking and size:
            self.doc.set(node, letterSpacing=cvdoc.double(tracking / 1000.0 * size))

        just = getattr(doc, "justification", None)
        if just is not None:
            key = str(getattr(just, "name", just)).upper()
            for word, index in self.ALIGNMENT.items():
                if word in key:
                    self.doc.set(node, horizontalAlignment=cvdoc.enum(index))
                    break

        # Colour rides on a material, the same way it does for shapes.
        fill = getattr(doc, "fill_color", None)
        if fill and getattr(doc, "apply_fill", True):
            mat = self.doc.create(
                "colorMaterial",
                f"{name} Fill",
                {
                    "materialColor": cvdoc.color(
                        fill[0] * 255, fill[1] * 255, fill[2] * 255
                    ),
                    "showInProjectWindow": cvdoc.boolean(False),
                },
            )
            self.doc.connect(f"{mat}.id", f"{node}.material")
            self.doc.add_child(node, mat)

        self.report.note(
            "Text carries string, font, size, leading, letter spacing, alignment "
            "and fill colour; per-character styling does not."
        )
        return self.text_origin_offset(doc, size, ae_font)

    @staticmethod
    def text_origin_offset(doc, size, ae_font=None):
        """AE's text origin -> the block's top-left corner, in AE units."""
        if getattr(doc, "box_text", False):
            box = getattr(doc, "box_text_pos", None)
            if box:
                # AE centres the box on the origin and hands us the corner.
                return float(box[0]), float(box[1])
        # Point text sits on its first baseline, so the top is one ascent up.
        return 0.0, -fonts.ascent_ratio(ae_font) * float(size or 0)

    @staticmethod
    def _text_document(layer):
        for getter in (
            lambda: layer.property("ADBE Text Properties").property("ADBE Text Document").value,
            lambda: layer.source_text.value,
        ):
            try:
                value = getter()
                if value is not None and hasattr(value, "text"):
                    return value
            except Exception:
                continue
        return None

    # -- timing -----------------------------------------------------------
    def apply_timing(self, comp, comp_node, layer, node):
        """AE in/out points become a Cavalry visibility curve.

        A visibility curve is two keyframes - 1.0 where the layer switches on,
        -1.0 where it switches off - driving the layer's ``on`` attribute.
        """
        if not layer.enabled:
            self.doc.set(node, on=cvdoc.boolean(False))
            return

        fps = comp.frame_rate
        in_frame = int(round(layer.in_point * fps))
        out_frame = int(round(layer.out_point * fps))
        if in_frame <= 0 and out_frame >= int(round(comp.duration * fps)):
            return  # spans the whole comp: leave it always on

        curve = self.doc.create("visibilityCurve", None, with_uuid=False)
        self.doc.set_raw(curve, "keyframes", cvdoc.node_id_list(2))
        for i, (frame, value) in enumerate(((in_frame, 1.0), (out_frame, -1.0))):
            key = self.make_keyframe(frame, {"numValue": value})
            self.doc.connect(f"{key}.id", f"{curve}.keyframes.{i}")
        self.doc.connect(f"{curve}.out", f"{node}.on")
        shader = self.shader_of.get(node)
        if shader:
            self.doc.connect(f"{curve}.out", f"{shader}.on")
        self.doc.add_child(node, curve)
        self.wire_curve_time(comp_node, curve)

    def wire_curve_time(self, comp_node, curve):
        self.doc.connect(f"{comp_node}.outActiveAnimationLayer", f"{curve}.activeAnimationLayer")
        self.doc.connect(f"{comp_node}.time", f"{curve}.time")

    def make_keyframe(self, frame, data):
        attrs = {"data": {"value": data, "varType": "keyData"}}
        if frame:
            attrs["timeOffset"] = cvdoc.double(frame)
        return self.doc.create("keyframe", None, attrs)

    # -- transforms -------------------------------------------------------
    def apply_transform(self, comp, layer, node, source_size, origin_offset=(0.0, 0.0)):
        group = layer.property(TRANSFORM_GROUP)
        if group is None:
            return
        parented = layer.parent is not None
        comp_node = self.comp_nodes[comp.id]

        def prop(match_name):
            try:
                return group.property(match_name)
            except Exception:
                return None

        pos = prop(POSITION)
        scale = prop(SCALE)
        rot = prop(ROTATION_Z)
        opacity = prop(OPACITY)
        anchor = prop(ANCHOR)

        # Static values.
        ox, oy = origin_offset
        if parented:
            # AE gives a child's position in the parent's LAYER space, whose
            # origin is the top-left of the parent's source. Cavalry measures
            # from the parent's pivot, which corresponds to its anchor point.
            pax, pay = self._anchor_of(layer.parent)
            ox -= pax
            oy -= pay
        if pos is not None and not pos.is_time_varying:
            v = pos.value
            x, y = self.to_cv_point(v[0] + ox, v[1] + oy, comp, parented)
            z = v[2] if len(v) > 2 else 0.0
            self.doc.set(node, position=cvdoc.double3(x, y, z))
        if scale is not None and not scale.is_time_varying:
            v = scale.value
            self.doc.set(node, scale=cvdoc.scale2(v[0] / 100.0, v[1] / 100.0))
        if rot is not None and not rot.is_time_varying:
            self.doc.set(node, rotation=cvdoc.double3(0.0, 0.0, self.to_cv_angle(rot.value)))
        # After Effects stores null layers at opacity 0 - a null never renders,
        # so the value is meaningless there and cannot reach its children. A
        # Cavalry null IS drawable and its opacity cascades, so copying the 0
        # would hide everything parented to it.
        is_null = bool(getattr(layer, "null_layer", False))
        if opacity is not None and not opacity.is_time_varying and not is_null:
            if abs(opacity.value - 100.0) > 1e-6:
                self.doc.set(node, opacity=cvdoc.double(opacity.value))
        if anchor is not None:
            a = anchor.value
            if source_size:
                # Footage and solids are drawn from their centre.
                sw, sh = source_size
                px, py = a[0] - sw / 2.0, a[1] - sh / 2.0
            else:
                # Text and shapes are drawn from their own local origin.
                px, py = a[0], a[1]
            if abs(px) > 1e-6 or abs(py) > 1e-6:
                self.doc.set(node, pivot=cvdoc.double2(px, -py if self.flip_y else py))

        # Animated channels.
        if pos is not None and pos.is_time_varying:
            self.animate(comp, comp_node, node, pos, [
                ("position.x", lambda v: self.to_cv_point(v[0] + ox, 0, comp, parented)[0]),
                ("position.y", lambda v: self.to_cv_point(0, v[1] + oy, comp, parented)[1]),
            ])
        if scale is not None and scale.is_time_varying:
            self.animate(comp, comp_node, node, scale, [
                ("scale.x", lambda v: v[0] / 100.0),
                ("scale.y", lambda v: v[1] / 100.0),
            ])
        if rot is not None and rot.is_time_varying:
            self.animate(comp, comp_node, node, rot,
                         [("rotation.z", lambda v: self.to_cv_angle(v))])
        if opacity is not None and opacity.is_time_varying and not is_null:
            self.animate(comp, comp_node, node, opacity, [("opacity", lambda v: v)])

    def set_or_animate(self, comp, comp_node, node, attr, prop, scale=1.0):
        """Write one scalar AE property to a Cavalry attribute.

        Static values are written directly; animated ones become an
        animationCurve, so shape modifiers animate like everything else.
        """
        if prop is None:
            return
        if prop.is_time_varying:
            self.animate(comp, comp_node, node, prop,
                         [(attr, lambda v: float(v) * scale)])
            return
        try:
            value = float(prop.value) * scale
        except (TypeError, ValueError):
            return
        self.doc.set_raw(node, attr, cvdoc.double(value))

    def animate(self, comp, comp_node, node, prop, channels):
        """One Cavalry animationCurve per scalar channel."""
        keys = list(prop.keyframes)
        if not keys:
            return
        fps = comp.frame_rate

        for dim, (attr, pick) in enumerate(channels):
            curve = self.doc.create("animationCurve", None, with_uuid=False)
            self.doc.set_raw(curve, "keyframes", cvdoc.node_id_list(len(keys)))

            for i, kf in enumerate(keys):
                frame = int(round(kf.time * fps))
                try:
                    value = float(pick(kf.value))
                except (TypeError, IndexError):
                    value = 0.0
                data = {"numValue": value, "interpolation": 0,
                        "locked": True, "weightLocked": True}
                data.update(self._ease(keys, i, dim, fps))
                key = self.make_keyframe(frame, data)
                self.doc.connect(f"{key}.id", f"{curve}.keyframes.{i}")
                self.report.keyframes += 1

            self.doc.connect(f"{curve}.out", f"{node}.{attr}")
            self.doc.add_child(node, curve)
            self.wire_curve_time(comp_node, curve)

    @staticmethod
    def _ease(keys, i, dim, fps):
        """AE temporal eases map almost directly onto Cavalry's key data.

        Both store an influence (a fraction of the segment) and a speed (value
        units per second); AE expresses influence as a percentage.
        """
        kf = keys[i]

        def ease_at(eases):
            if not eases:
                return None
            return eases[min(dim, len(eases) - 1)]

        out = {}
        e_in = ease_at(getattr(kf, "in_temporal_ease", None))
        e_out = ease_at(getattr(kf, "out_temporal_ease", None))

        prev_dt = (kf.time - keys[i - 1].time) if i > 0 else 0.0
        next_dt = (keys[i + 1].time - kf.time) if i < len(keys) - 1 else 0.0

        if e_in is not None:
            infl = float(getattr(e_in, "influence", 33.3)) / 100.0
            speed = float(getattr(e_in, "speed", 0.0))
            out["leftInfluence"] = infl
            out["leftSpeed"] = speed
            dx = infl * prev_dt * fps
            out["leftBez"] = {"x": -dx, "y": -speed * infl * prev_dt}
        if e_out is not None:
            infl = float(getattr(e_out, "influence", 33.3)) / 100.0
            speed = float(getattr(e_out, "speed", 0.0))
            out["rightInfluence"] = infl
            out["rightSpeed"] = speed
            dx = infl * next_dt * fps
            out["rightBez"] = {"x": dx, "y": speed * infl * next_dt}
        return out


def gather_media(doc, dest_dir, mode="link"):
    """Collect referenced media next to the scene and repoint the assets at it.

    Cavalry fails silently on media it cannot find, so keeping the footage
    beside the .cv makes the scene portable. ``link`` symlinks (instant, and
    screen recordings run to gigabytes); ``copy`` duplicates the bytes.
    """
    import shutil

    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    gathered, missing = [], []

    for node_id, path in doc.asset_paths():
        if not os.path.exists(path):
            missing.append(path)
            continue
        target = os.path.join(dest_dir, os.path.basename(path))
        if os.path.abspath(path) != target and not os.path.exists(target):
            if mode == "copy":
                shutil.copy2(path, target)
            else:
                os.symlink(os.path.abspath(path), target)
        doc.set_asset_path(node_id, target)
        gathered.append(target)
    return gathered, missing


def convert_file(aep_path, cv_path, flip_y=True, gather=None, gather_mode="link"):
    app = py_aep.parse(aep_path)
    cv_path = os.fspath(cv_path)
    stem = os.path.splitext(os.path.basename(cv_path))[0]
    vector_dir = os.path.join(os.path.dirname(os.path.abspath(cv_path)),
                              f"{stem}-vector")
    conv = Converter(app.project, flip_y=flip_y, vector_dir=vector_dir)
    doc, report = conv.run()

    if gather:
        gathered, missing = gather_media(doc, gather, gather_mode)
        report.gathered = gathered
        for m in missing:
            if m not in report.missing:
                report.missing.append(m)

    doc.write(cv_path)
    return report
