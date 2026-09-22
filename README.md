# aep2cv

Convert Adobe After Effects projects (`.aep`) into Cavalry scenes (`.cv`).

**After Effects is not required.** The `.aep` is parsed directly from its
binary RIFX container, so this works on projects you can no longer open — an
expired subscription, a machine without AE, a build server.

## Install

```sh
pipx install aep2cv          # or: uv tool install aep2cv
```

Homebrew:

```sh
brew tap fform/aep2cv && brew install aep2cv
```

Pure Python, no compiled code of its own — the same install works on macOS,
Windows and Linux.

## Usage

```sh
aep2cv project.aep                        # writes project.cv alongside it
aep2cv project.aep -o scenes/project.cv
aep2cv project.aep --gather               # collect media beside the scene
```

| option | |
|---|---|
| `-o, --output` | destination `.cv` (default: alongside the source) |
| `--gather [DIR]` | collect referenced media next to the scene and repoint the assets at it (default: a `footage` folder beside the `.cv`) |
| `--gather-mode` | `link` (default) or `copy` |
| `--no-flip-y` | keep AE's Y-down axis; Cavalry is Y-up, so this mirrors the result |
| `-q, --quiet` | only report problems |

From Python:

```python
from aep2cv import convert_file

report = convert_file("project.aep", "project.cv")
print(report.comps, report.layers, report.keyframes)
```

## What converts

| After Effects | Cavalry |
|---|---|
| Composition | `compNode` — resolution, frame rate, length, background |
| Layer order and parenting | nested `nodeMeta` hierarchy |
| Layer in/out points | `visibilityCurve` gating the shape and its shader |
| Precomp layer | `compositionReference` |
| Footage (video/image) | `footageShape` + `imageShader` + `colorMaterial` |
| Footage trim | a ramp on the shader's time, so edits of one clip survive |
| SVG artwork | `svgShape` — real geometry, not a flat image |
| Illustrator / EPS / PDF | converted to SVG, then `svgShape` |
| Solid | `rectangleShape` + `colorMaterial` |
| Null | `null` |
| Text | `textShape` — string, font, size, leading, letter spacing, alignment, fill |
| Shape layers | nested `group` nodes mirroring AE's Contents tree |
| ↳ Rect / Ellipse / Star | `basicShape` + generator |
| ↳ Drawn path | `editableShape` |
| ↳ Fill / gradient fill | `colorMaterial`, optionally via a gradient shader |
| ↳ Stroke | `strokeMaterial` — colour, width, cap, join, miter, dashes |
| ↳ Merge Paths | `boolean` deformer |
| ↳ Trim Paths | stroke trim |
| ↳ Rounded Corners / Offset Paths | `round` / `pathOffsetBehaviour` deformers |
| ↳ Repeater | `duplicator` |
| Transforms and their keyframes | same attributes, with easing preserved |
| Built-in effects | Cavalry's native filters (see below) |

### Effects

Built-in AE effects map onto Cavalry's own filters, so they stay editable:

| After Effects | Cavalry |
|---|---|
| Gaussian Blur, Camera Lens Blur | `gaussianBlurFilter` |
| Fast Box Blur, Fast Blur | `blurFilter` |
| Drop Shadow | `dropShadowFilter` (direction + distance → offset vector) |
| Fill | `fill` |
| Hue/Saturation | `hueSaturationLightness` |
| Glow | `glowFilter` |
| Levels | `levels` |
| Brightness & Contrast | `brightnessAndContrast` |
| Invert, Black & White, Tritone | `invert`, `blackAndWhite`, `triToneFilter` |
| Posterize, Threshold, Sharpen | `posterizeFilter`, `thresholdFilter`, `sharpenFilter` |
| Linear Wipe, Radial Wipe, Venetian Blinds | `linearWipe`, `radialWipe`, `venetianBlinds` |

Anything else — including all third-party plugins — is reported by name and
left off. Cavalry cannot host AE effects: its own plugins are SkSL shaders,
and it has no OpenFX support.

Units are converted throughout: scale `0–100` → `0–1`, seconds → frames, and
AE's top-left Y-down origin → Cavalry's centre Y-up origin.

## What does not convert

The layer is still created, with its name, timing and transform intact:

- Effects, masks, track mattes and blend modes
- Expressions
- Cameras and lights
- Per-character text styling
- Shape modifiers with no Cavalry equivalent — Twist, Pucker & Bloat, Roughen
  Edges, Wiggle Paths, Wiggle Transform, Zig Zag, stroke taper and wave
- Multiple fills or strokes on one group (the first of each is used)
- Animated shape *contents* — paths and gradients convert at their current
  value. Scalar modifiers (trim, stroke width, corner radius) keep keyframes.

Every dropped item is named in the run report rather than skipped silently.
Treat the result as a scaffold to finish by hand, not a finished scene.

## Notes

- **Missing media.** Cavalry renders nothing, and says nothing, for footage it
  cannot find. Every reference is checked and missing files are reported.
  `--gather` keeps the media beside the scene so it travels with it; it
  symlinks by default, so use `--gather-mode copy` for anything in a temp
  directory.
- **Missing fonts.** A font that is not installed is reported, since Cavalry
  will substitute a fallback just as AE would.
- **`.ai` files** need a vector converter on `PATH` — `pdftocairo` (poppler),
  `mutool`, `pdf2svg` or Inkscape. Without one, the file is placed as footage
  and reported. Artwork already run through AE's *Create Shapes from Vector
  Layer* comes across as editable shapes and needs none of this.
- **Y axis.** If a scene comes out mirrored, re-run with `--no-flip-y`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m aep2cv project.aep -o project.cv
```

```
aep2cv/cvdoc.py        .cv document builder (nodes, hierarchy, connections)
aep2cv/convert.py      the AE → Cavalry mapping
aep2cv/shapes.py       shape layers: geometry, paint and modifiers
aep2cv/effects.py      built-in AE effects → Cavalry filters
aep2cv/fonts.py        resolves AE PostScript names to family + style
aep2cv/vector.py       SVG, and .ai/.eps/.pdf conversion
aep2cv/cli.py          command line entry point
tools/validate_cv.py   structural and referential checks on a generated .cv
tools/cv_schema.py     audits emitted attributes against Cavalry's own schema
packaging/             Homebrew formula and release notes
```

`.cv` is an undocumented format. `tools/cv_schema.py` reads Cavalry's own node
schema from `Cavalry.app/Contents/assets/Definitions/nodeDefinitions.json` and
checks every attribute the converter emits — a misspelled attribute is not an
error in Cavalry, it is silently ignored, so run it after any change:

```sh
python tools/cv_schema.py out/scene.cv        # audit a generated scene
python tools/cv_schema.py show textShape font # inspect a node type
```

## Licence

MIT
