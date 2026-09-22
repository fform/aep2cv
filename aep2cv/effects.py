"""Map built-in After Effects effects onto Cavalry's native filters.

Cavalry has no plugin API that could host an AE effect - its third-party
plugins are SkSL shaders - but it ships filters that cover most of what people
actually apply: blurs, drop shadows, fills, colour correction and wipes. Those
convert directly and stay editable.

Effects with no equivalent are named in the run report rather than dropped
silently. Third-party plugins are always reported, never guessed at.
"""

from __future__ import annotations

import math

from . import cvdoc


def _param(effect, *names):
    """Find a parameter by match name or display name.

    AE indexes parameter match names ("ADBE Drop Shadow-0002"), which differ
    per effect, so display names are the more portable key of the two.
    """
    wanted = {n.lower() for n in names}
    for prop in effect:
        if (getattr(prop, "match_name", "") or "").lower() in wanted:
            return prop
        if (getattr(prop, "name", "") or "").lower() in wanted:
            return prop
    return None


def _value(effect, *names, default=None):
    prop = _param(effect, *names)
    if prop is None:
        return default
    try:
        return prop.value
    except Exception:
        return default


def _colour(rgba, alpha=None):
    a = 255 if alpha is None else alpha
    return cvdoc.color(rgba[0] * 255, rgba[1] * 255, rgba[2] * 255, a)


def _opacity_fraction(value):
    """AE stores some opacities 0-1 and others 0-100; normalise to 0-1."""
    if value is None:
        return 1.0
    return value / 100.0 if value > 1.0 else float(value)


class EffectBuilder:
    """Attaches Cavalry filters to a converted layer."""

    # Effects that map straight onto a filter, as
    # AE match name -> (Cavalry node, [(cavalry attr, AE names, scale)])
    SIMPLE = {
        "ADBE Gaussian Blur 2": ("gaussianBlurFilter", [
            ("amount", ("Blurriness",), 1.0, "double2")]),
        "ADBE Camera Lens Blur": ("gaussianBlurFilter", [
            ("amount", ("Blur Radius",), 1.0, "double2")]),
        "ADBE Box Blur2": ("blurFilter", [
            ("amount", ("Blur Radius",), 1.0, "double2")]),
        "ADBE Fast Blur": ("blurFilter", [
            ("amount", ("Blurriness",), 1.0, "double2")]),
        "ADBE Sharpen": ("sharpenFilter", [
            ("intensity", ("Sharpen Amount",), 1.0, "double")]),
        "ADBE Posterize": ("posterizeFilter", [
            ("levels", ("Level",), 1.0, "int")]),
        "ADBE Threshold2": ("thresholdFilter", [
            ("threshold", ("Level",), 1 / 255.0, "double")]),
        "ADBE Brightness & Contrast 2": ("brightnessAndContrast", [
            ("brightness", ("Brightness",), 1.0, "double"),
            ("contrast", ("Contrast",), 1.0, "double")]),
        "ADBE Easy Levels2": ("levels", [
            ("inBlack", ("Input Black",), 1.0, "double"),
            ("inWhite", ("Input White",), 1.0, "double"),
            ("outBlack", ("Output Black",), 1.0, "double"),
            ("outWhite", ("Output White",), 1.0, "double"),
            ("gamma", ("Gamma",), 1.0, "double")]),
        "ADBE Pro Levels2": ("levels", [
            ("inBlack", ("Input Black",), 1.0, "double"),
            ("inWhite", ("Input White",), 1.0, "double"),
            ("outBlack", ("Output Black",), 1.0, "double"),
            ("outWhite", ("Output White",), 1.0, "double"),
            ("gamma", ("Gamma",), 1.0, "double")]),
        "ADBE Linear Wipe": ("linearWipe", [
            ("completion", ("Transition Completion",), 1.0, "double"),
            ("direction", ("Wipe Angle",), 1.0, "double"),
            ("feather", ("Feather",), 1.0, "double")]),
        "ADBE Venetian Blinds": ("venetianBlinds", [
            ("completion", ("Transition Completion",), 1.0, "double"),
            ("direction", ("Direction",), 1.0, "double"),
            ("width", ("Width",), 1.0, "double"),
            ("feather", ("Feather",), 1.0, "double")]),
        "ADBE Radial Wipe": ("radialWipe", [
            ("completion", ("Transition Completion",), 1.0, "double"),
            ("startAngle", ("Start Angle",), 1.0, "double"),
            ("feather", ("Feather",), 1.0, "double")]),
    }

    # Recognised, but nothing in Cavalry reproduces them.
    UNSUPPORTED = {
        "ADBE Ramp": "Gradient Ramp",
        "ADBE Roughen Edges": "Roughen Edges",
        "ADBE Turbulent Displace": "Turbulent Displace",
        "ADBE CurvesCustom": "Curves",
        "ADBE Colorama": "Colorama",
        "ADBE Echo": "Echo",
        "ADBE Displacement Map": "Displacement Map",
        "ADBE Corner Pin": "Corner Pin",
        "ADBE Transform": "Transform (effect)",
    }

    def __init__(self, converter, comp, comp_node):
        self.conv = converter
        self.doc = converter.doc
        self.comp = comp
        self.comp_node = comp_node

    def apply(self, layer, node, layer_kind):
        """Attach every convertible effect on this layer."""
        if layer_kind == "null":
            return  # a null draws nothing, so filters have nothing to act on
        try:
            parade = layer.property("ADBE Effect Parade")
        except Exception:
            return
        if not parade:
            return

        for effect in parade:
            match_name = getattr(effect, "match_name", "") or ""
            filter_node = self.build(effect, match_name)
            if filter_node is None:
                self.report_unconverted(effect, match_name)
                continue
            self.attach(node, filter_node)

    def build(self, effect, match_name):
        if match_name == "ADBE Drop Shadow":
            return self.drop_shadow(effect)
        if match_name == "ADBE Fill":
            return self.fill(effect)
        if match_name == "ADBE HUE SATURATION":
            return self.hue_saturation(effect)
        if match_name == "ADBE Invert":
            return self.doc.create("invert", "Invert")
        if match_name == "ADBE Black&White":
            return self.doc.create("blackAndWhite", "Black and White")
        if match_name == "ADBE Tritone":
            return self.tritone(effect)
        if match_name == "ADBE Glo2":
            return self.glow(effect)
        if match_name in self.SIMPLE:
            return self.simple(effect, match_name)
        return None

    def simple(self, effect, match_name):
        node_type, params = self.SIMPLE[match_name]
        node = self.doc.create(node_type, getattr(effect, "name", None) or node_type)
        for attr, names, scale, kind in params:
            prop = _param(effect, *names)
            if prop is None:
                continue
            if kind == "double2":
                # One AE radius drives both Cavalry axes.
                try:
                    v = float(prop.value) * scale
                except (TypeError, ValueError):
                    continue
                self.doc.set_raw(node, attr, cvdoc.double2(v, v))
            elif kind == "int":
                try:
                    self.doc.set_raw(node, attr, cvdoc.integer(int(prop.value)))
                except (TypeError, ValueError):
                    continue
            else:
                self.conv.set_or_animate(self.comp, self.comp_node, node, attr,
                                         prop, scale)
        return node

    def drop_shadow(self, effect):
        """AE gives a direction and distance; Cavalry wants an offset vector."""
        node = self.doc.create("dropShadowFilter", "Drop Shadow")
        colour = _value(effect, "Shadow Color", default=(0.0, 0.0, 0.0, 1.0))
        opacity = _value(effect, "Opacity", default=100.0)
        self.doc.set(node, shadowColor=_colour(
            colour, _opacity_fraction(opacity) * 255))

        direction = float(_value(effect, "Direction", default=135.0) or 0.0)
        distance = float(_value(effect, "Distance", default=0.0) or 0.0)
        # AE measures clockwise from straight up, in a Y-down space.
        angle = math.radians(direction)
        dx = math.sin(angle) * distance
        dy = -math.cos(angle) * distance
        if self.conv.flip_y:
            dy = -dy
        self.doc.set(node, offset=cvdoc.double2(dx, dy))

        softness = float(_value(effect, "Softness", default=0.0) or 0.0)
        # AE softness spans the whole blur; Cavalry's amount is a radius.
        radius = softness / 2.0
        self.doc.set(node, amount=cvdoc.double2(radius, radius))
        return node

    def fill(self, effect):
        node = self.doc.create("fill", "Fill")
        colour = _value(effect, "Color", default=(0.0, 0.0, 0.0, 1.0))
        opacity = _opacity_fraction(_value(effect, "Opacity", default=1.0))
        self.doc.set(node, fillColor=_colour(colour, opacity * 255))
        return node

    def hue_saturation(self, effect):
        node = self.doc.create("hueSaturationLightness", "Hue/Saturation")
        # AE's master hue is -180..180; Cavalry's is 0..360.
        hue = float(_value(effect, "Master Hue", default=0.0) or 0.0)
        self.doc.set(node, hue=cvdoc.double(hue % 360.0))
        for attr, name in (("saturation", "Master Saturation"),
                           ("lightness", "Master Lightness")):
            self.conv.set_or_animate(self.comp, self.comp_node, node, attr,
                                     _param(effect, name))
        return node

    def tritone(self, effect):
        node = self.doc.create("triToneFilter", "Tritone")
        for attr, name in (("highlightColor", "Highlights"),
                           ("midTonesColor", "Midtones"),
                           ("shadowColor", "Shadows")):
            colour = _value(effect, name)
            if colour:
                self.doc.set_raw(node, attr, _colour(colour))
        return node

    def glow(self, effect):
        node = self.doc.create("glowFilter", "Glow")
        radius = _value(effect, "Glow Radius")
        if radius:
            self.doc.set(node, blur=cvdoc.int2(int(radius), int(radius)))
        intensity = _value(effect, "Glow Intensity")
        if intensity:
            self.doc.set(node, intensity=cvdoc.double(intensity))
        return node

    def attach(self, layer_node, filter_node):
        index = self.doc.list_length(layer_node, "filters")
        self.doc.set_raw(layer_node, "filters", cvdoc.node_id_list(index + 1))
        self.doc.connect(f"{filter_node}.id", f"{layer_node}.filters.{index}")
        self.doc.add_child(layer_node, filter_node)

    def report_unconverted(self, effect, match_name):
        name = getattr(effect, "name", None) or match_name
        if match_name in self.UNSUPPORTED:
            self.conv.report.note(
                f"Effect not converted: {self.UNSUPPORTED[match_name]}.")
        elif match_name.startswith("ADBE "):
            self.conv.report.note(f"Effect not converted: {name}.")
        else:
            # Not an Adobe match name, so a third-party plugin.
            self.conv.report.note(
                f"Third-party plugin effect not converted: {name} ({match_name}).")
