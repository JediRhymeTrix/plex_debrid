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

## Newznab discovery

`test_newznab.py` verifies canonical and historical source-name casing,
default/explicit source selection, shared setup, and episode title queries
whose IMDb ID is carried in the companion matching expression. It also
checks that torrent and Usenet results are combined before ranking, a
2160p DV/HDR/Atmos candidate outranks a cached 1080p fallback, and missing
Usenet results/identity do not discard torrent results. All indexer requests
are mocked; no NZBs are downloaded and no provider jobs are submitted.

Configure `newznab` in `Sources`, plus `Newznab Base URL` and `Newznab API Key`.
The historical `Newznab` spelling remains accepted. Default source selection
queries enabled adapters together; quality ranking—not protocol ordering—
chooses among their combined results. Upgrade targets still permit a playable
fallback when no preferred copy is discovered. This does not guarantee that
an indexer lists a release, or that a listed NZB can be completed by TorBox.

## Queue retirement

`test_retirement.py` covers `off`, recommended-default `target-met` (90-day
grace), and the optional `playable-after-grace` fallback. Cases include re-add
timestamps, release/acquisition/finale dates, uncertain identities and APIs,
ongoing/hiatus and incomplete ended shows, unreadable files, single-file target
coverage, exact-list ownership, and single-attempt removal without dangerous
POST retries. All list and media operations are mocked; tests never remove
real entries or files. `Monitoring retirement policy = off` disables retirement
without changing acquisition, quality upgrades or superseded cleanup.

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

## Movie identity correction

Successful movie grabs may queue a post-scan IMDb check in the gitignored
`plex_download_identity.json` under the configured settings directory. Records
contain only the original requested IMDb ID, selected torrent-relative video
path, byte size, and retry timestamps. They survive restart and expire after
30 days. No credentials or direct download links are stored.

Correction requires one uniquely linked Plex movie/file, an exact IMDb search,
and independent verification that the returned Plex candidate carries the same
IMDb ID. Multi-video downloads and merged/multipart items are not guessed.
Metadata-only rematching never deletes or renames files.

This is advisory and fail-open: no ID, no verified match, an ambiguous link, or
an API/persistence error leaves acquisition behavior unchanged. Existing title,
quality and cleanup rules are not relaxed. No match-dependent download rejection
is introduced.