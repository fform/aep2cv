"""Evaluate After Effects expressions that resolve to a fixed value.

A .cv scene cannot run AE expressions, and the .aep only stores each
property's pre-expression value. In template projects that value is often
meaningless: the real one comes from an expression linking the property to a
control on a "Controller" layer. For example:

    thisComp.layer("Controller").effect("Glow Radius")("ADBE Slider Control-0001")
    (effect("Glow")(1) == 1) ? 100 : 0
    [flipH ? -value[0] : value[0], flipV ? -value[1] : value[1]]

This module interprets the small subset of AE's JavaScript that such rigs use:
literals, arithmetic with AE's vector semantics, comparisons, ternaries,
local variables, ``Math``, and references to comps, layers, effects and
transform properties. Anything that depends on time (``time``, ``wiggle``,
references to keyframed properties) or needs more of the language is
reported as unresolved, and the property keeps its pre-expression value.

A property with keyframes whose expression reads ``value`` is evaluated once
per keyframe, so ``value * slider`` keeps its animation.

Every leaf property read in the converter goes through ``effective()``,
which also corrects two unit mismatches between the stored value and what AE
shows (and what expressions see): percentages stored on a 0-255 scale, and
effect points left at their default, which py_aep 0.16 reports 100x too
large.
"""

from __future__ import annotations

import math
import re

from py_aep.models.properties.property import Property


class Unresolved(Exception):
    """The expression cannot be reduced to a fixed value."""


# -- the active resolver ---------------------------------------------------
# Set for the duration of a conversion, so module-level helpers in shapes.py
# and effects.py can resolve without threading the converter through.
_active = None


def activate(resolver):
    global _active
    _active = resolver


def effective(prop):
    """The property as AE would evaluate it: see ``Resolver.resolve``."""
    if prop is None or _active is None or not isinstance(prop, Property):
        return prop
    return _active.resolve(prop)


# -- resolved property proxies ---------------------------------------------
class ResolvedKeyframe:
    def __init__(self, keyframe, value):
        self._kf = keyframe
        self.value = value

    def __getattr__(self, name):
        return getattr(self._kf, name)


class ResolvedProperty:
    """Reads like a py_aep Property, with the value after the expression."""

    def __init__(self, prop, value, keyframes):
        self._prop = prop
        self.value = value
        self.keyframes = keyframes
        self.is_time_varying = bool(keyframes)

    def __getattr__(self, name):
        return getattr(self._prop, name)

    def __iter__(self):
        return iter(self._prop)


# -- units -------------------------------------------------------------------
def _percent_scale(prop):
    """Stored value -> the percentage AE displays, or None if not a percent.

    AE stores some percentages as 0-255 (Drop Shadow opacity, Glow
    threshold) and others as 0-1 (Fill opacity); ``max_value`` tells which.
    """
    try:
        if prop.units_text != "percent" or not prop.has_max:
            return None
        top = float(prop.max_value)
    except Exception:
        return None
    if top > 1.5:
        return 100.0 / top
    return 100.0


def _map(value, fn):
    if isinstance(value, (list, tuple)):
        return [fn(v) for v in value]
    return fn(value)


def containing_layer(prop):
    node = prop
    while node is not None:
        if hasattr(node, "containing_comp"):
            return node
        node = getattr(node, "parent_property", None)
    return None


def _in_effect(prop):
    node = getattr(prop, "parent_property", None)
    while node is not None:
        if getattr(node, "match_name", "") == "ADBE Effect Parade":
            return True
        node = getattr(node, "parent_property", None)
    return False


def _fix_effect_point(prop, value):
    """py_aep 0.16 scales a *default* effect point by the layer size without
    first converting its percentage, so a centred point on a 1920x1080 layer
    reads [96000, 54000]. No real point sits 10x outside its layer."""
    if not (isinstance(value, (list, tuple)) and len(value) >= 2 and _in_effect(prop)):
        return value
    layer = containing_layer(prop)
    w = float(getattr(layer, "width", 0) or 0)
    h = float(getattr(layer, "height", 0) or 0)
    if w and h and (abs(value[0]) > 10 * w or abs(value[1]) > 10 * h):
        return [v / 100.0 for v in value]
    return value


# -- tokenizer -------------------------------------------------------------
_TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|/\*.*?\*/)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<name>[A-Za-z_$][\w$]*)
  | (?P<op>===|!==|==|!=|<=|>=|&&|\|\||[-+*/%<>!=?:.,;()\[\]{}])
""", re.X | re.S)


def tokenize(src):
    tokens, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise Unresolved(f"cannot read {src[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        text = m.group()
        if kind == "num":
            tokens.append(("num", float(text)))
        elif kind == "str":
            tokens.append(("str", bytes(text[1:-1], "utf-8").decode("unicode_escape")))
        else:
            tokens.append((kind, text))
    tokens.append(("end", None))
    return tokens


# -- parser ----------------------------------------------------------------
# AST nodes are tuples: (kind, ...).
_BINARY = [
    ("||",), ("&&",), ("==", "!=", "===", "!=="),
    ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%"),
]
_UNSUPPORTED_WORDS = {"if", "for", "while", "function", "return", "switch",
                      "try", "new", "do"}


class Parser:
    def __init__(self, src):
        self.tokens = tokenize(src)
        self.i = 0

    def peek(self, offset=0):
        return self.tokens[self.i + offset]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def accept(self, text):
        if self.peek()[1] == text and self.peek()[0] in ("op", "name"):
            self.i += 1
            return True
        return False

    def expect(self, text):
        if not self.accept(text):
            raise Unresolved(f"expected {text!r}")

    def program(self):
        body = []
        while self.peek()[0] != "end":
            if self.accept(";"):
                continue
            body.append(self.statement())
        return body

    def statement(self):
        kind, text = self.peek()
        if kind == "name" and text in _UNSUPPORTED_WORDS:
            raise Unresolved(f"'{text}' statements are not evaluated")
        if kind == "name" and text in ("var", "let", "const"):
            self.next()
            decls = []
            while True:
                name = self.next()
                self.expect("=")
                decls.append(("assign", name[1], self.expression()))
                if not self.accept(","):
                    break
            self.accept(";")
            return decls[0] if len(decls) == 1 else ("block", decls)
        elif kind == "name" and self.peek(1) == ("op", "="):
            self.next()
            self.next()
            node = ("assign", text, self.expression())
        else:
            node = ("expr", self.expression())
        self.accept(";")
        return node

    def expression(self):
        cond = self.binary(0)
        if self.accept("?"):
            a = self.expression()
            self.expect(":")
            b = self.expression()
            return ("ternary", cond, a, b)
        return cond

    def binary(self, level):
        if level == len(_BINARY):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek()[0] == "op" and self.peek()[1] in _BINARY[level]:
            op = self.next()[1]
            left = ("binary", op, left, self.binary(level + 1))
        return left

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("-", "+", "!"):
            op = self.next()[1]
            return ("unary", op, self.unary())
        return self.postfix(self.primary())

    def postfix(self, node):
        while True:
            if self.accept("."):
                name = self.next()
                if name[0] != "name":
                    raise Unresolved("expected a property name")
                node = ("member", node, name[1])
            elif self.accept("("):
                args = []
                if not self.accept(")"):
                    args.append(self.expression())
                    while self.accept(","):
                        args.append(self.expression())
                    self.expect(")")
                node = ("call", node, args)
            elif self.accept("["):
                index = self.expression()
                self.expect("]")
                node = ("index", node, index)
            else:
                return node

    def primary(self):
        kind, text = self.next()
        if kind in ("num", "str"):
            return ("lit", text)
        if kind == "name":
            if text == "true":
                return ("lit", True)
            if text == "false":
                return ("lit", False)
            if text in _UNSUPPORTED_WORDS:
                raise Unresolved(f"'{text}' is not evaluated")
            return ("name", text)
        if text == "(":
            node = self.expression()
            self.expect(")")
            return node
        if text == "[":
            items = []
            if not self.accept("]"):
                items.append(self.expression())
                while self.accept(","):
                    items.append(self.expression())
                self.expect("]")
            return ("array", items)
        raise Unresolved(f"unexpected {text!r}")


def uses_value(ast):
    """Whether an expression reads its own pre-expression ``value``."""
    if isinstance(ast, tuple):
        if ast == ("name", "value"):
            return True
        return any(uses_value(part) for part in ast[1:])
    if isinstance(ast, list):
        return any(uses_value(part) for part in ast)
    return False


# -- runtime objects -------------------------------------------------------
# Layer-level shorthands (thisLayer.position) and transform members.
_TRANSFORM_MEMBERS = {
    "anchorPoint": "ADBE Anchor Point",
    "position": "ADBE Position",
    "xPosition": "ADBE Position_0",
    "yPosition": "ADBE Position_1",
    "scale": "ADBE Scale",
    "rotation": "ADBE Rotate Z",
    "zRotation": "ADBE Rotate Z",
    "opacity": "ADBE Opacity",
}


class CompRef:
    def __init__(self, comp):
        self.comp = comp

    def member(self, name, ev):
        comp = self.comp
        if name == "layer":
            return Func(lambda key: LayerRef(self.find(key)))
        if name in ("width", "height", "duration", "name"):
            return getattr(comp, name)
        if name == "frameDuration":
            return 1.0 / comp.frame_rate
        if name == "numLayers":
            return float(len(comp.layers))
        raise Unresolved(f"thisComp.{name} is not evaluated")

    def find(self, key):
        layers = self.comp.layers
        if isinstance(key, str):
            for layer in layers:
                if layer.name == key:
                    return layer
            raise Unresolved(f'no layer "{key}"')
        index = int(key)  # AE layer indices are 1-based
        if 1 <= index <= len(layers):
            return layers[index - 1]
        raise Unresolved(f"no layer {index}")


class GroupRef:
    """A property group - a layer, its transform, or an effect."""

    def __init__(self, group):
        self.group = group

    def child(self, key):
        group = self.group
        if isinstance(key, (int, float)):
            kids = [p for p in group]
            index = int(key)
            if 1 <= index <= len(kids):
                return wrap(kids[index - 1])
            raise Unresolved(f"no property {index}")
        try:
            found = group.property(key)
        except Exception:
            found = None
        if found is None:
            for p in group:
                if (getattr(p, "name", None) or "") == key:
                    found = p
                    break
        if found is None:
            raise Unresolved(f'no property "{key}"')
        return wrap(found)

    def call(self, args, ev):
        if len(args) != 1:
            raise Unresolved("a property group takes one argument")
        return self.child(args[0])

    def member(self, name, ev):
        if name in _TRANSFORM_MEMBERS:
            try:
                return self.child(_TRANSFORM_MEMBERS[name])
            except Unresolved:
                pass
        if name == "name":
            return getattr(self.group, "name", "")
        if name == "content":
            return Func(lambda key: GroupRef(self._contents()).child(key))
        # AE exposes every property as a camelCase member: .copies, .size.
        wanted = name.lower()
        for p in self.group:
            if (getattr(p, "name", None) or "").replace(" ", "").lower() == wanted:
                return wrap(p)
        raise Unresolved(f".{name} is not evaluated")

    def _contents(self):
        """A shape group's Contents - what content("Name") searches."""
        for mn in ("ADBE Root Vectors Group", "ADBE Vectors Group"):
            try:
                found = self.group.property(mn)
            except Exception:
                found = None
            if found is not None:
                return found
        raise Unresolved("content() on something that has no contents")


class LayerRef(GroupRef):
    def __init__(self, layer):
        super().__init__(layer)
        self.layer = layer

    def member(self, name, ev):
        layer = self.layer
        if name == "effect":
            return Func(lambda key: GroupRef(self._effects()).child(key))
        if name == "transform":
            return GroupRef(layer.property("ADBE Transform Group"))
        if name in _TRANSFORM_MEMBERS:
            return GroupRef(layer.property("ADBE Transform Group")).member(name, ev)
        if name == "content":
            return Func(lambda key: GroupRef(self._contents()).child(key))
        if name in ("width", "height"):
            return float(getattr(layer, name))
        if name == "index":
            return float(layer.index)
        if name == "name":
            return layer.name
        raise Unresolved(f"layer.{name} is not evaluated")

    def _effects(self):
        parade = self.layer.property("ADBE Effect Parade")
        if parade is None:
            raise Unresolved(f'layer "{self.layer.name}" has no effects')
        return parade

    def call(self, args, ev):
        # thisComp.layer("X")("Transform") / ("Effects")
        key = args[0] if args else None
        if key in ("Transform", "ADBE Transform Group"):
            return GroupRef(self.layer.property("ADBE Transform Group"))
        if key in ("Effects", "ADBE Effect Parade"):
            return GroupRef(self._effects())
        return super().call(args, ev)


class PropRef:
    def __init__(self, prop):
        self.prop = prop

    def member(self, name, ev):
        if name == "value":
            return ev.value_of(self.prop)
        if name in ("numKeys",):
            return float(len(self.prop.keyframes))
        raise Unresolved(f"property.{name} is not evaluated")


class Func:
    def __init__(self, fn):
        self.fn = fn

    def call(self, args, ev):
        return self.fn(*args)


def wrap(obj):
    if isinstance(obj, Property):
        return PropRef(obj)
    return GroupRef(obj)


def _clamp(v, lo, hi):
    if isinstance(v, list):
        return [min(max(a, lo), hi) for a in v]
    return min(max(_num(v), lo), hi)


def _linear(t, *args):
    if len(args) == 2:
        t_min, t_max, v1, v2 = 0.0, 1.0, args[0], args[1]
    elif len(args) == 4:
        t_min, t_max, v1, v2 = args
    else:
        raise Unresolved("linear() takes 3 or 5 arguments")
    if t_max == t_min:
        u = 0.0 if t <= t_min else 1.0
    else:
        u = min(max((t - t_min) / (t_max - t_min), 0.0), 1.0)
    return _add(_mul(v1, 1.0 - u), _mul(v2, u))


class MathRef:
    FUNCS = {
        "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
        "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
        "abs": abs, "floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt,
        "pow": math.pow, "exp": math.exp, "log": math.log,
        "round": lambda x: math.floor(x + 0.5),
        "min": lambda *a: min(a), "max": lambda *a: max(a),
    }

    def member(self, name, ev):
        if name in self.FUNCS:
            fn = self.FUNCS[name]
            return Func(lambda *a: float(fn(*[_num(x) for x in a])))
        if name == "PI":
            return math.pi
        raise Unresolved(f"Math.{name} is not evaluated")


GLOBALS = {
    "Math": MathRef(),
    "degreesToRadians": Func(lambda d: math.radians(_num(d))),
    "radiansToDegrees": Func(lambda r: math.degrees(_num(r))),
    "clamp": Func(lambda v, lo, hi: _clamp(v, _num(lo), _num(hi))),
    "linear": Func(_linear),
}


# -- AE arithmetic ---------------------------------------------------------
def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    raise Unresolved(f"expected a number, got {type(v).__name__}")


def _vec2(a, b, op):
    """AE applies array arithmetic element-wise; the shorter array is padded
    with zeros."""
    n = max(len(a), len(b))
    a = list(a) + [0.0] * (n - len(a))
    b = list(b) + [0.0] * (n - len(b))
    return [op(x, y) for x, y in zip(a, b)]


def _add(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return f"{a}{b}"
    if isinstance(a, list) and isinstance(b, list):
        return _vec2(a, b, lambda x, y: x + y)
    if isinstance(a, list) or isinstance(b, list):
        raise Unresolved("array + number")
    return _num(a) + _num(b)


def _mul(a, b):
    if isinstance(a, list) and isinstance(b, list):
        raise Unresolved("array * array")
    if isinstance(a, list):
        return [x * _num(b) for x in a]
    if isinstance(b, list):
        return [_num(a) * x for x in b]
    return _num(a) * _num(b)


def _binary(op, a, b):
    if op == "+":
        return _add(a, b)
    if op == "-":
        if isinstance(a, list) and isinstance(b, list):
            return _vec2(a, b, lambda x, y: x - y)
        return _num(a) - _num(b)
    if op == "*":
        return _mul(a, b)
    if op == "/":
        if isinstance(a, list):
            d = _num(b)
            if d == 0:
                raise Unresolved("division by zero")
            return [x / d for x in a]
        d = _num(b)
        if d == 0:
            raise Unresolved("division by zero")
        return _num(a) / d
    if op == "%":
        return math.fmod(_num(a), _num(b))
    if op in ("==", "===", "!=", "!=="):
        if isinstance(a, list) or isinstance(b, list):
            same = a == b
        elif isinstance(a, str) or isinstance(b, str):
            same = a == b
        else:
            same = _num(a) == _num(b)
        return same if op in ("==", "===") else not same
    x, y = _num(a), _num(b)
    return {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


def _truthy(v):
    if isinstance(v, list):
        return True
    if isinstance(v, str):
        return bool(v)
    return _num(v) != 0.0


# -- evaluator -------------------------------------------------------------
class Evaluation:
    """One run of one expression."""

    def __init__(self, resolver, prop, value):
        self.resolver = resolver
        self.prop = prop
        layer = containing_layer(prop)
        if layer is None:
            raise Unresolved("property is not on a layer")
        self.layer = layer
        if isinstance(value, tuple):
            value = list(value)
        self.scope = {
            "value": value,
            "thisComp": CompRef(layer.containing_comp),
            "thisLayer": LayerRef(layer),
            "thisProperty": PropRef(prop),
            "effect": LayerRef(layer).member("effect", self),
            "content": Func(lambda key: GroupRef(LayerRef(layer)._contents()).child(key)),
            "comp": Func(self._comp),
        }

    def _comp(self, name):
        for comp in self.resolver.compositions:
            if comp.name == name:
                return CompRef(comp)
        raise Unresolved(f'no composition "{name}"')

    def run(self, program):
        result = None
        for stmt in self._flatten(program):
            if stmt[0] == "assign":
                self.scope[stmt[1]] = self.plain(self.eval(stmt[2]))
                result = self.scope[stmt[1]]
            else:
                result = self.plain(self.eval(stmt[1]))
        if result is None:
            raise Unresolved("expression has no result")
        return result

    @staticmethod
    def _flatten(program):
        for stmt in program:
            if stmt[0] == "block":
                yield from stmt[1]
            else:
                yield stmt

    def value_of(self, prop):
        return self.resolver.expression_value(prop)

    def plain(self, v):
        """Collapse a property reference to the value it holds."""
        if isinstance(v, PropRef):
            return self.value_of(v.prop)
        if isinstance(v, (GroupRef, CompRef, Func, MathRef)):
            raise Unresolved("expression result is not a value")
        return v

    def eval(self, node):
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "name":
            name = node[1]
            if name in self.scope:
                return self.scope[name]
            if name in GLOBALS:
                return GLOBALS[name]
            if name == "time":
                raise Unresolved("depends on time")
            raise Unresolved(f"'{name}' is not evaluated")
        if kind == "array":
            return [_num(self.plain(self.eval(x))) for x in node[1]]
        if kind == "unary":
            v = self.plain(self.eval(node[2]))
            if node[1] == "!":
                return not _truthy(v)
            if node[1] == "-":
                return [-x for x in v] if isinstance(v, list) else -_num(v)
            return v
        if kind == "binary":
            op = node[1]
            if op in ("&&", "||"):
                left = self.plain(self.eval(node[2]))
                if (op == "&&") != _truthy(left):
                    return left
                return self.plain(self.eval(node[3]))
            return _binary(op, self.plain(self.eval(node[2])),
                           self.plain(self.eval(node[3])))
        if kind == "ternary":
            cond = self.plain(self.eval(node[1]))
            return self.eval(node[2] if _truthy(cond) else node[3])
        if kind == "member":
            target = self.eval(node[1])
            if isinstance(target, (CompRef, GroupRef, PropRef, MathRef)):
                return target.member(node[2], self)
            if node[2] == "length" and isinstance(target, (list, str)):
                return float(len(target))
            raise Unresolved(f".{node[2]} is not evaluated")
        if kind == "call":
            target = self.eval(node[1])
            args = [self.plain(self.eval(a)) for a in node[2]]
            if isinstance(target, (Func, GroupRef)):
                return target.call(args, self)
            raise Unresolved("call to something that is not a function")
        if kind == "index":
            target = self.plain(self.eval(node[1]))
            i = int(_num(self.plain(self.eval(node[2]))))
            if isinstance(target, list) and -len(target) <= i < len(target):
                return target[i]
            raise Unresolved("index out of range")
        raise Unresolved(f"cannot evaluate {kind}")


class Resolver:
    """Resolves properties once each, recording what it could not."""

    def __init__(self, compositions=()):
        self.compositions = list(compositions)
        self._programs = {}
        self._resolved = {}
        self._active = set()
        self.resolved = 0
        self.unresolved = []   # (layer name, property path, reason)

    def resolve(self, prop):
        key = id(prop)
        cached = self._resolved.get(key)
        if cached is not None:
            return cached[1]
        result = self._resolve(prop)
        # Keep prop alive alongside its id so the id is never reused.
        self._resolved[key] = (prop, result)
        return result

    def _stored(self, prop):
        try:
            value = prop.value
        except Exception:
            value = None
        return _fix_effect_point(prop, value)

    def _resolve(self, prop):
        value = self._stored(prop)
        try:
            keyframes = list(prop.keyframes) if prop.is_time_varying else []
        except Exception:
            keyframes = []

        expr = self._expression(prop)
        if expr:
            try:
                return self._evaluate(prop, expr, value, keyframes)
            except Unresolved as err:
                self._fail(prop, str(err))
            except RecursionError:
                self._fail(prop, "expressions reference each other in a loop")
            except Exception as err:  # a malformed expression, or py_aep
                self._fail(prop, f"{type(err).__name__}: {err}")
        return ResolvedProperty(prop, value, keyframes)

    @staticmethod
    def _expression(prop):
        try:
            if prop.expression_enabled and (prop.expression or "").strip():
                return prop.expression
        except Exception:
            pass
        return None

    def _program(self, expr):
        program = self._programs.get(expr)
        if program is None:
            program = Parser(expr).program()
            self._programs[expr] = program
        return program

    def _evaluate(self, prop, expr, value, keyframes):
        key = id(prop)
        if key in self._active:
            raise Unresolved("expressions reference each other in a loop")
        self._active.add(key)
        try:
            program = self._program(expr)
            to_ui, from_ui = self._converters(prop)
            if keyframes and uses_value(program):
                results = [from_ui(Evaluation(self, prop, to_ui(k.value)).run(program))
                           for k in keyframes]
                self.resolved += 1
                if all(r == results[0] for r in results):
                    return ResolvedProperty(prop, results[0], [])
                return ResolvedProperty(prop, results[0], [
                    ResolvedKeyframe(k, r) for k, r in zip(keyframes, results)])
            result = from_ui(Evaluation(self, prop, to_ui(value)).run(program))
            self.resolved += 1
            return ResolvedProperty(prop, result, [])
        finally:
            self._active.discard(key)

    @staticmethod
    def _converters(prop):
        scale = _percent_scale(prop)
        if scale is None:
            return (lambda v: v), (lambda v: v)
        return (lambda v: _map(v, lambda x: x * scale),
                lambda v: _map(v, lambda x: _num(x) / scale))

    def expression_value(self, prop):
        """A referenced property's value, as its own expression sees it."""
        resolved = self.resolve(prop)
        if resolved.is_time_varying:
            raise Unresolved(f'"{prop.name}" is keyframed')
        if self._expression(prop) and self._failed(prop):
            raise Unresolved(f'"{prop.name}" has an unresolved expression')
        to_ui, _ = self._converters(prop)
        value = resolved.value
        if isinstance(value, tuple):
            value = list(value)
        return to_ui(value)

    def _failed(self, prop):
        return any(entry[3] is prop for entry in self.unresolved)

    def _fail(self, prop, reason):
        layer = containing_layer(prop)
        path = []
        node = prop
        while node is not None and node is not layer:
            name = getattr(node, "name", None) or getattr(node, "match_name", "")
            if name:
                path.append(name)
            node = getattr(node, "parent_property", None)
        comp = getattr(getattr(layer, "containing_comp", None), "name", "?")
        self.unresolved.append((comp, getattr(layer, "name", "?"),
                                " / ".join(reversed(path)), prop, reason))
