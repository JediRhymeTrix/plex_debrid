# Lightweight Trakt subscriptions

Use **Plex Download Queue** as one authoritative private list of movies and
shows to acquire or improve. Entries stay until you remove them; the service
does not empty this list after a pass. A second list is not required. Trakt
stores intent, Plex stores observed files, and the existing downloader handles
releases. There is no local monitoring database, scheduler journal, dependency
or extra service/thread. A separate consumable inbox is optional and disabled
by default.

## Configuration

Select your existing private **Plex Download Queue** under a configured Trakt
user. Point `Trakt monitoring list` at it and leave `Trakt inbox lists` empty.
The main queue may also appear in `Trakt lists`: while scheduling is enabled,
it is excluded from legacy processing and auto-removal. The setting name
`Trakt monitoring list` is retained for configuration compatibility; it means
the main queue, not another required list.

| Setting | Default | Meaning |
| --- | --- | --- |
| `Trakt monitoring list` | empty | `<user>'s private list: Plex Download Queue`; empty retains the original loop |
| `Trakt inbox lists` | empty | Optional separate private inboxes; empty disables all transfer operations |
| `Monitoring poll minutes` | `10` | Trakt membership and Plex library refresh interval; minimum 1 |
| `Monitoring attempt budget` | `10` | Maximum monitored movie/episode attempts per pass; 1–100 |
| `Monitoring schedule hours` | `1,24,168,720` | Recent episode, recent movie, mature, catalogue/optional cadence |

Recent episodes are <=7 days old; recent movies <=90 days; mature releases are
<=365 days. Unknown/older release dates use catalogue cadence. Missing baseline
copies get at least daily opportunities; newly aired missing episodes retain
the faster episode cadence. Observed additions/re-adds to the main queue bypass normal
cadence, but never release gating, watched exclusions or hard version rules.
Recent Plex additions get a fresh upgrade window. After the main target is met,
strictly better preference-ranked copies and optional presentations are checked
at catalogue cadence. Nothing expires solely because it is old.

One pass runs approximately each minute, cooperatively in the existing service.
The budget counts movie/episode attempts, **not HTTP calls**: a normal attempt
may use scraper fallbacks. Managed work validates upgrades per episode rather
than inferring improvement from a season-pack title. Existing non-inbox Plex, Trakt and
Overseerr sources retain their original processing at approximately 30-minute
intervals; they are outside the new monitored-work budget.

## Main queue semantics

- Add an old, new or upcoming title directly to **Plex Download Queue**.
- Acquired titles remain subscribed to slow, budgeted upgrade checks.
- Optional presentations do not make every title eligible on every pass.
- Removing a title stops its background monitoring without deleting media.
- An observed remove/re-add, or a changed Trakt entry timestamp, wakes a title
  again without duplicating an existing copy. Re-adds invalidate its cached
  metadata. A change entirely between polls can be invisible if Trakt does not
  change the entry timestamp.
- Successful refreshes with unchanged membership do not keep forcing searches.
  Failed or partial reads preserve the last successful in-memory snapshot.

The list records **monitoring intent**, not a count of currently actionable
downloads. Scheduling and file coverage determine what work is actually due.

## Optional inbox (disabled by default)

Only enable this if you want a separate consumable search-request interface.
Configured inboxes must be distinct owned private lists in `Trakt lists`;
the main queue cannot be an inbox. With `Trakt inbox lists` empty, no inbox
reads, enrollment writes or source removals occur.

1. Batch-add absent inbox IDs to the main queue.
2. Read the destination back and confirm each subscription exists.
3. Recheck inbox membership/timestamp; remove only the confirmed original
   requests from that exact optional inbox, never the main queue.
4. Wake enrolled titles for evaluation, including old library titles.

Inbox removal means **accepted for ongoing monitoring**, not “all downloads
complete.” Unavailable/upcoming requests can leave the inbox because their
subscription remains in the main queue. Add them to the optional inbox again
to request another immediate evaluation. The default single-list workflow
does not use these transfers.

Cross-list changes are not atomic. The add/read-back/remove ordering makes
retries idempotent; failed/partial membership reads do not erase the previous
in-memory snapshot. Trakt remains authoritative after restart.

## Resource boundaries and compatibility

- Five settings, one monitor module, no new dependencies.
- Full list snapshots are lightweight raw rows. Show metadata is hydrated only
  when eligible, bounded per pass, refreshed hourly (ended shows daily), and
  cached under a 5,000-episode/leaf cap.
- Cooldown/metadata-deadline maps are capped at 20,000 entries. A restart loses
  cooldowns, not subscriptions; stable ID/time slices stagger catalogue work.
- Working shows copy only selected leaves; cached source graphs stay intact.
- One normal result pool evaluates all configured versions; no separate scraper
  loop per presentation.
- Monitor-managed copies do not write retry-ignore state or auto-remove source
  entries. Explicit watched exclusions and the existing quality/identity and
  cleanup safeguards remain in effect.
- Main quality may continue improving after its nominal target, but only a
  strictly better result is accepted. Equal copies and transport/cache-only
  changes are not quality upgrades. Unmanaged legacy behavior is unchanged.
- Empty `Trakt monitoring list` returns to the original automation loop.

Polling is best effort, not a guarantee of detecting short-lived releases.
Infrastructure failures can postpone attempts; no permanent “never available”
classification is inferred from a run of empty searches. Uncached provider jobs
still use the existing downloader's pending-transfer mechanism.

## Validation

Run from the repository root:

```text
C:\Python312\python.exe -B -W ignore -m unittest discover -s tests -v
```

All tests mock service operations. They cover legacy routing, private-list
ownership, complete pagination, verified transfers, read failures, re-add races,
UTC dates, pending/optional quality, bounded work and cache, fair episode
selection, and the existing identity/quality/cleanup regression suites.