"""Resolve After Effects font names into Cavalry's {font, style} pair.

After Effects stores a PostScript name ("IBMPlexMono", "SourceSerifPro-Regular").
Cavalry wants the family display name and a separate style ("IBM Plex Mono" +
"Regular"), and quietly fails to load the font when handed the PostScript name.

The installed fonts are read with fontTools, which works the same on macOS,
Windows and Linux and is already a dependency. Reading the fonts directly also
gives us each face's real ascent, which is what positions point text.

Fonts that are not installed fall back to splitting and de-camel-casing the
PostScript name, which gets the common cases right.
"""

from __future__ import annotations

import logging
import os
import re
import sys

# fontTools chatters about odd timestamps in perfectly usable fonts.
logging.getLogger("fontTools").setLevel(logging.ERROR)

FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Style words that appear as a PostScript suffix, longest first so that
# "SemiBoldItalic" beats "Bold".
_STYLE_WORDS = (
    "ExtraLightItalic", "SemiBoldItalic", "ExtraBoldItalic", "MediumItalic",
    "LightItalic", "ThinItalic", "BlackItalic", "BoldItalic", "ExtraLight",
    "ExtraBold", "SemiBold", "Regular", "Medium", "Italic", "Light", "Black",
    "Thin", "Bold", "Book", "Roman",
)

# Fraction of the font size above the baseline, when the face is unknown.
DEFAULT_ASCENT = 0.8

_index = None


def font_directories():
    """Where each platform keeps its fonts."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts",
                os.path.join(home, "Library/Fonts")]
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        local = os.environ.get("LOCALAPPDATA", "")
        dirs = [os.path.join(windir, "Fonts")]
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        return dirs
    return ["/usr/share/fonts", "/usr/local/share/fonts",
            os.path.join(home, ".fonts"),
            os.path.join(home, ".local/share/fonts")]


def _faces(path):
    """Yield every face in a font file; collections hold several."""
    from fontTools.ttLib import TTFont, TTCollection

    if path.lower().endswith((".ttc", ".otc")):
        with TTCollection(path, lazy=True) as collection:
            for face in collection.fonts:
                yield face
    else:
        yield TTFont(path, fontNumber=0, lazy=True)


def _describe(face):
    """(postscript, family, style, ascent as a fraction of the em)."""
    names = {}
    for record in face["name"].names:
        if record.nameID in (1, 2, 6, 16, 17) and record.nameID not in names:
            try:
                names[record.nameID] = record.toUnicode()
            except Exception:
                continue
    postscript = names.get(6)
    if not postscript:
        return None
    # 16/17 are the typographic family and style, which is the split Cavalry
    # wants; 1/2 fold the weight into the family for anything but Regular.
    family = names.get(16) or names.get(1)
    style = names.get(17) or names.get(2) or "Regular"
    if not family:
        return None

    ascent = None
    try:
        upm = face["head"].unitsPerEm or 1000
        raw = getattr(face.get("hhea"), "ascent", None)
        if not raw:
            raw = getattr(face.get("OS/2"), "sTypoAscender", None)
        if raw:
            ascent = abs(raw) / float(upm)
    except Exception:
        ascent = None
    return postscript, family, style, ascent


def _build_index():
    """PostScript name -> (family, style, ascent). Cheap enough to do eagerly."""
    index = {}
    for directory in font_directories():
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for name in files:
                if not name.lower().endswith(FONT_EXTENSIONS):
                    continue
                path = os.path.join(root, name)
                try:
                    for face in _faces(path):
                        described = _describe(face)
                        if described:
                            postscript, family, style, ascent = described
                            index.setdefault(postscript, (family, style, ascent))
                        try:
                            face.close()
                        except Exception:
                            pass
                except Exception:
                    continue  # unreadable or unsupported font, skip it
    return index


def installed_fonts():
    global _index
    if _index is None:
        try:
            _index = _build_index()
        except Exception:
            _index = {}
    return _index


def _split_style(name):
    """Best-effort family/style split for a font we could not look up."""
    base, _, suffix = name.partition("-")
    if suffix:
        return base, _CAMEL.sub(" ", suffix)
    for word in _STYLE_WORDS:
        if name.endswith(word) and len(name) > len(word):
            return name[: -len(word)], _CAMEL.sub(" ", word)
    return name, "Regular"


def _lookup(ae_font):
    table = installed_fonts()
    hit = table.get(ae_font)
    if hit:
        return hit
    # AE sometimes records the family alone for a regular weight.
    base, want = _split_style(ae_font)
    want = want.replace(" ", "").lower()
    for postscript, entry in table.items():
        if postscript.split("-")[0] == base and entry[1].replace(" ", "").lower() == want:
            return entry
    return None


def resolve(ae_font):
    """(family, style, exact) for an After Effects font name."""
    if not ae_font:
        return None, None, False
    hit = _lookup(ae_font)
    if hit:
        return hit[0], hit[1], True
    base, style = _split_style(ae_font)
    return _CAMEL.sub(" ", base), style, False


def ascent_ratio(ae_font):
    """How far above the baseline the face reaches, as a fraction of its size."""
    hit = _lookup(ae_font) if ae_font else None
    if hit and hit[2]:
        return hit[2]
    return DEFAULT_ASCENT
