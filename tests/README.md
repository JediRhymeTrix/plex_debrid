# Quality and cleanup regression tests

Run from the repository root:

```text
C:\Python312\python.exe -B -W ignore -m unittest discover -s tests -v
```

Tests use the real version/rule classes with mocked Plex/debrid I/O. They do not
need settings, credentials, mounted drives or network access. Import `ui` first,
as `main.py` does, because the legacy packages contain circular imports.

Coverage includes independent version upgrades, restart reconstruction,
joint per-file targets, IMAX/Open Matte exclusivity, cross-version duplicate
suppression, pending-grab reconciliation/expiry, per-episode pack coverage,
retry limits, actual rule ordering/ties, missing keepers, playback, failed
requests, shared files, and isolated TorBox backend identification.

## Cleanup boundaries

Cleanup is opt-in; both `Library upgrade remove superseded files = true` and
an explicit `Library upgrade cleanup TorBox root` are needed. It only removes
a strictly dominated **single-file TorBox torrent** when another independently
identified TorBox download satisfies all the old copy's applicable targets.
Distinct cuts, languages, presentations, tied preferences, unsupported rules,
season packs, shared paths, unknown mappings and Real-Debrid downloads are
retained. IMAX, 3D and Open Matte are deliberately excluded from deletion.

Execution rechecks Plex metadata with `checkFiles`, playback, file accessibility,
replacement byte availability and fresh provider inventory. Deletion targets
one explicit TorBox ID; there is no local unlink, Plex filesystem deletion,
bulk operation or fallback on error. A failed/uncertain response stops cleanup.
Plex may retain the unavailable old entry until its next normal scan.

`content.cleanup.clean(item, dry_run=True)` performs observational checks only,
without media-byte reads or deletion. Do not use `plex.library()` to implement a
preview when cleanup is enabled: the library refresh hook runs cleanup.