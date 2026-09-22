"""Bring vector artwork across as vectors rather than as a flat image.

Cavalry ships a real SVG parser and an `svgShape` node, so an SVG becomes
editable geometry with its own fills and strokes. It has no Illustrator
importer, but a modern `.ai` file is a PDF underneath, so `.ai`/`.eps`/`.pdf`
can be converted to SVG first by whichever vector tool is on the machine.

Conversion is best-effort: if nothing suitable is installed we say so and fall
back to treating the file as footage, rather than emitting a layer that would
silently render nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

SVG_EXTENSIONS = {".svg"}
CONVERTIBLE_EXTENSIONS = {".ai", ".eps", ".pdf"}

# Vector-preserving converters, best first. Each entry builds an argv given
# the source and destination paths.
CONVERTERS = (
    ("pdftocairo", lambda src, dst: ["pdftocairo", "-svg", src, dst]),
    ("mutool", lambda src, dst: ["mutool", "convert", "-F", "svg", "-o", dst, src]),
    ("pdf2svg", lambda src, dst: ["pdf2svg", src, dst]),
    ("inkscape", lambda src, dst: ["inkscape", "--export-type=svg",
                                   f"--export-filename={dst}", src]),
)

INSTALL_HINT = ("install poppler (pdftocairo), mupdf (mutool), pdf2svg or "
                "Inkscape to convert them")


def extension(path):
    return os.path.splitext(str(path))[1].lower()


def is_svg(path):
    return extension(path) in SVG_EXTENSIONS


def is_convertible(path):
    return extension(path) in CONVERTIBLE_EXTENSIONS


def find_converter():
    """The first installed vector converter, or None."""
    for name, build in CONVERTERS:
        if shutil.which(name):
            return name, build
    return None


def to_svg(src, dest_dir):
    """Convert a vector file to SVG. Returns the new path, or None.

    Raises nothing: a failed conversion is a fallback, not an error.
    """
    converter = find_converter()
    if converter is None:
        return None
    name, build = converter

    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(str(src)))[0]
    dst = os.path.join(dest_dir, f"{stem}.svg")
    if os.path.exists(dst):
        return dst

    try:
        result = subprocess.run(build(str(src), dst), capture_output=True,
                                timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not os.path.exists(dst):
        # Some converters number their output when a file has several pages.
        numbered = os.path.join(dest_dir, f"{stem}1.svg")
        if os.path.exists(numbered):
            os.replace(numbered, dst)
            return dst
        return None
    return dst
