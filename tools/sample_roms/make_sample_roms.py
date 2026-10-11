#!/usr/bin/env python3
"""Compile the per-core sample ROMs shipped with each release zip.

Every sample is an original test pattern (no game content): a minimal
program for each system that boots in the matching Libretro core. Each
core gets exactly one ROM, named `sample.<extension>`.

The per-system byte-level builders live in `builders/<core>.py`, one
module each, and are deliberately isolated so iterating on one system
never touches another.  Each exposes `build() -> bytes`.

Usage: make_sample_roms.py OUTPUT_DIR
"""

import hashlib
import sys
from pathlib import Path

# Allow running as a plain script (no `pip install -e`) by putting our own
# directory on sys.path so `import builders.nes` etc. resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from builders import gba, gbc, n64, nes, snes


BUILDERS = {
	"sample.nes": nes.build,
	"sample.sfc": snes.build,
	"sample.v64": n64.build,
	"sample.gbc": gbc.build,
	"sample.gba": gba.build,
}


def main() -> None:
	if len(sys.argv) != 2:
		raise SystemExit(f"Usage: {sys.argv[0]} OUTPUT_DIR")
	output_dir = Path(sys.argv[1])
	output_dir.mkdir(parents=True, exist_ok=True)
	for filename in BUILDERS:
		rom = BUILDERS[filename]()
		path = output_dir / filename
		path.write_bytes(rom)
		print(f"Wrote sample ROM {path} ({len(rom)} bytes, sha1 {hashlib.sha1(rom).hexdigest()})")


if __name__ == "__main__":
	main()
