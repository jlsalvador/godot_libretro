"""Per-core sample ROM builders.

Each module exposes `build() -> bytes` producing one sample ROM for one core.
They're deliberately isolated (no shared state) so iterating on one system
does not touch the others.
"""
