#!/usr/bin/env python3
"""Render a read-only block-map PNG from a saved Minecraft Java world.

The world is only ever read (bounded, symlink-checked Anvil/level.dat reads); the
image path is the only file written.  Metadata (caption, method, bounds, chunk
counts, hashes, snapshot fingerprint) is printed to stdout as JSON so a caller
can cache on the world fingerprint and let the dashboard show the caption.

Examples
--------
    python3 scripts/render_world.py \
        --world infra/server-data/world \
        --output runs/_analysis/world_render.png
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _load_renderer():
    try:
        from minecraft_harness.world_render import WorldRenderError, render_world, world_fingerprint
    except ModuleNotFoundError:  # running from a checkout without installation
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        from minecraft_harness.world_render import WorldRenderError, render_world, world_fingerprint
    return WorldRenderError, render_world, world_fingerprint


def parse_size(value):
    match = value.lower().split('x')
    try:
        width, height = (int(part) for part in match)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('size must look like 1024x640') from None
    if len(match) != 2:
        raise argparse.ArgumentTypeError('size must look like 1024x640')
    return width, height


def main(argv=None):
    WorldRenderError, render_world, world_fingerprint = _load_renderer()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--world', type=Path, required=True,
                        help='saved-world directory containing level.dat and region/')
    parser.add_argument('--output', type=Path,
                        help='PNG path to create (parent directories are created); '
                             'required unless --fingerprint-only is used')
    parser.add_argument('--center', nargs=2, type=int, metavar=('X', 'Z'),
                        help='block centre; defaults to the level.dat spawn point')
    parser.add_argument('--width', type=int, default=64, help='sampled X extent in blocks (default 64)')
    parser.add_argument('--depth', type=int, default=64, help='sampled Z extent in blocks (default 64)')
    parser.add_argument('--style', choices=('top', 'iso', 'mesh'), default='top', help='render style (default: top)')
    parser.add_argument('--size', type=parse_size, default=(1024, 640), metavar='WxH',
                        help='output pixel size (default 1024x640)')
    parser.add_argument('--full-world', action='store_true',
                        help='mesh style only: render the complete saved overworld extent (LOD) '
                             'instead of a centered window; implies --width 0 --depth 0')
    parser.add_argument('--fingerprint-only', action='store_true',
                        help='print the world input fingerprint and exit without rendering')
    args = parser.parse_args(argv)
    if not args.fingerprint_only and args.output is None:
        parser.error('--output is required unless --fingerprint-only is used')
    try:
        if args.fingerprint_only:
            print(json.dumps(world_fingerprint(args.world), indent=2, sort_keys=True))
            return 0
        width = 0 if args.full_world else args.width
        depth = 0 if args.full_world else args.depth
        metadata = render_world(args.world, args.output, center=args.center, width=width,
                                depth=depth, style=args.style, size=args.size,
                                full_world=args.full_world)
    except WorldRenderError as error:
        print(json.dumps({'status': 'error', 'error': str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
