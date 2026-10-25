# Repository guidance

- Everything authored in this project is in English: source code,
  comments, tests, docs, plans, and user-facing strings.
- Do not write trademarks into the repo: no game, character, company,
  or console brand names in code, comments, tests, docs, or plans.
  Name things by core (FCEUmm, bsnes, Mupen64Plus-Next, Gambatte,
  mGBA), not by platform; platform abbreviations appear only where a
  ROM format requires them at byte level or in generic user-facing ROM
  descriptions. Describe ROMs generically (e.g. "a FCEUmm platformer
  ROM") and identify a specific one by its public catalog hash (e.g.
  a No-Intro SHA-1), never its title or filename — including fake ROM
  filenames in tests.
- Upstream submodule contents are exempt from the rules above: they
  are pinned third-party trees, not ours, as is anything generated
  from them under `build/` or `dist/`.
- Commit messages use 50/72 formatting: first line 50 characters or
  less, a blank line, then the body wrapped at 72 characters.
- Avoid enumeration phrases (counting cores or tallying licenses,
  e.g. "all eight", "the seven cores", "five are GPLv2"): they
  duplicate the table or list that already says it, go stale, and
  must be re-synced on every change. Prefer describing without
  counting.
