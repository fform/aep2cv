"""Command line entry point: ``python -m aep2cv project.aep -o project.cv``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .convert import convert_file


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="aep2cv",
        description="Convert an After Effects .aep project into a Cavalry .cv scene. "
                    "After Effects does not need to be installed.",
    )
    ap.add_argument("aep", type=Path, help="source .aep project")
    ap.add_argument("-o", "--output", type=Path, help="destination .cv (default: alongside the source)")
    ap.add_argument("--no-flip-y", action="store_true",
                    help="keep AE's Y-down axis (Cavalry is Y-up, so this mirrors the result)")
    ap.add_argument("--gather", nargs="?", const="", metavar="DIR",
                    help="collect referenced media next to the scene and repoint the "
                         "asset paths at it (default: a 'footage' folder beside the .cv)")
    ap.add_argument("--gather-mode", choices=("link", "copy"), default="link",
                    help="symlink the gathered media (default) or copy the bytes")
    ap.add_argument("-q", "--quiet", action="store_true", help="only report problems")
    args = ap.parse_args(argv)

    if not args.aep.is_file():
        ap.error(f"no such file: {args.aep}")
    out = args.output or args.aep.with_suffix(".cv")

    gather = args.gather
    if gather == "":
        gather = str(out.parent / "footage")

    report = convert_file(args.aep, out, flip_y=not args.no_flip_y,
                          gather=gather, gather_mode=args.gather_mode)

    if not args.quiet:
        print(f"{args.aep.name} -> {out}")
        print(f"  {report.comps} compositions, {report.layers} layers, "
              f"{report.keyframes} keyframes")
        if report.vectorised:
            print(f"  converted {len(report.vectorised)} vector file(s) to SVG")
        if report.gathered:
            print(f"  gathered {len(report.gathered)} media file(s) into {gather}")
        if report.expressions_resolved:
            print(f"  evaluated {report.expressions_resolved} expression(s) to fixed values")
        for note in report.notes:
            print(f"  note: {note}")
    for skipped in report.skipped:
        print(f"  skipped: {skipped}", file=sys.stderr)
    if report.unresolved_fonts:
        # Cavalry cannot load a font that is not installed either.
        print(f"  WARNING: {len(report.unresolved_fonts)} font(s) are not installed "
              f"on this machine - the family name is a best guess and Cavalry will "
              f"substitute:", file=sys.stderr)
        for f in report.unresolved_fonts:
            print(f"    unresolved font: {f}", file=sys.stderr)
    if report.unconverted_vectors:
        from .vector import INSTALL_HINT
        print(f"  NOTE: {len(report.unconverted_vectors)} Illustrator/PDF file(s) "
              f"were placed as footage, which Cavalry cannot read - "
              f"{INSTALL_HINT}:", file=sys.stderr)
        for v in report.unconverted_vectors:
            print(f"    not vectorised: {v}", file=sys.stderr)
    if report.unresolved_expressions:
        # Grouped by reason: template rigs repeat the same expression per layer.
        by_reason = {}
        for where, reason in report.unresolved_expressions:
            by_reason.setdefault(reason, []).append(where)
        print(f"  NOTE: {len(report.unresolved_expressions)} expression(s) could not be "
              f"evaluated - those properties keep their pre-expression value:",
              file=sys.stderr)
        for reason, places in by_reason.items():
            more = f" (+{len(places) - 1} more)" if len(places) > 1 else ""
            print(f"    {reason}: {places[0]}{more}", file=sys.stderr)
    if report.missing:
        # Cavalry renders nothing for media it cannot find, and says nothing.
        print(f"  WARNING: {len(report.missing)} referenced file(s) not found on disk - "
              f"these layers will render empty:", file=sys.stderr)
        for m in report.missing:
            print(f"    missing: {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
