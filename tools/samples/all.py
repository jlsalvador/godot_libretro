#!/usr/bin/env python3
"""Compile the per-core sample ROMs shipped with each release zip.

Every sample is an original test pattern (no game content): a minimal
program for each system that boots in the matching Libretro core. Each
core gets exactly one ROM, named `sample.<extension>` (the picodrive and genesis_plus_gx cores share `sample.bin`).

The per-system byte-level builders live next to this script, one
module per core, and are deliberately isolated so iterating on one system
never touches another.  Each exposes `build() -> bytes`.

Usage: all.py OUTPUT_DIR
"""

import hashlib
import sys
from pathlib import Path

# Allow running as a plain script (no `pip install -e`) by putting our own
# directory on sys.path so `import fceumm` etc. resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bsnes
import fceumm
import gambatte
import mgba
import mupen64plus_next
import pcsx_rearmed
import picodrive


BUILDERS = {
	"sample.nes": fceumm.build,
	"sample.sfc": bsnes.build,
	"sample.z64": mupen64plus_next.build,
	"sample.gbc": gambatte.build,
	"sample.gba": mgba.build,
	"sample.exe": pcsx_rearmed.build,
	"sample.bin": picodrive.build,
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
