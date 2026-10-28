# Lightweight Trakt subscriptions

Use **Plex Download Queue** as one authoritative private list of movies and
shows to acquire or improve. Entries are not consumed after a pass; they stay
until manually removed or eligible under the configured retirement policy.
A second list is not required. Trakt
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
| `Monitoring schedule hours` | `1,24,168,720` | New <=7-day release, recent movie, mature, catalogue/missing-alternate cadence |
| `Monitoring retirement policy` | `target-met` | `off`, `target-met`, or `playable-after-grace` |
| `Monitoring retirement grace days` | `90` | Positive grace from the latest required factual date |

New movies and episodes <=7 days old use the first (default hourly) cadence;
movies <=90 days use daily cadence, mature releases <=365 days use weekly.
Unknown/older release dates use catalogue cadence. Missing baseline copies get
at least daily opportunities; newly released items retain the faster cadence.
Observed additions/re-adds to the main queue bypass normal
cadence, but never release gating, watched exclusions or hard version rules.
Recent Plex additions get a fresh upgrade window. Existing main/presentation
buckets keep receiving age-paced, strictly improving quality checks even after
their nominal target is met. Missing rare alternate presentations use catalogue
cadence unless main work already makes the item due. Nothing expires solely because it is old or searches
return no results; retirement additionally requires verified playable coverage.

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
- Removing or retiring a title stops its background monitoring without deleting media.
- An observed remove/re-add, or a changed Trakt entry timestamp, wakes a title
  again without duplicating an existing copy. Re-adds invalidate its cached
  metadata. A change entirely between polls can be invisible if Trakt does not
  change the entry timestamp.
- Successful refreshes with unchanged membership do not keep forcing searches.
  Failed or partial reads preserve the last successful in-memory snapshot.

The list records **monitoring intent**, not a count of currently actionable
downloads. Scheduling and file coverage determine what work is actually due.

## Progressive quality and acquisition decisions

- Upgrade targets describe the destination, not an all-at-once admission gate:
  a valid `2160p SDR -> 2160p HDR -> 2160p DV+HDR` progression stays actionable.
  Already met targets, resolution, admitted presentation/cut/language features
  and positive title preferences are protected from regression. Hard requirements
  remain hard. Preference-ranked ties are never reacquired just for a provider.
- 3D remains independent and cannot masquerade as the ordinary main copy. IMAX
  takes precedence over Open Matte for expanded framing, not over the 3D bucket.
  Protected alternate copies do not veto a better main copy, nor does a better
  main copy authorize deleting them. Existing optional buckets remain upgradeable
  and inherit missing primary intrinsic quality preferences/soft targets, preserving their own
  admission requirements and configured rule order.
- Superseded planning uses the same monotonic per-file quality comparison, so a
  proven intermediate improvement can supersede an SDR fallback. All actual
  deletion safeguards remain: only isolated, verified single-file TorBox torrents,
  fresh readable keepers, no shared/ambiguous references, no protected 3D/IMAX/
  Open Matte deletion, no automatic Usenet acquisition deletion.
- Enabled Newznab discovery runs once even when explicit torrent scraper groups
  return early. Usenet results do not short-circuit later torrent fallback groups.
- Acquisition tries cached torrent candidates first, then Usenet, then permitted
  uncached torrents. Quality ranking applies within each wave. RD is preferred
  when both providers can serve a torrent; TB is preferred when only TB is known
  cached. Unknown cache status is not success and not a known negative: actual
  provider status and usable wanted links must verify readiness. An explicit RD
  refusal can fall through to TB. Uncertain accepted/pending jobs defer rather
  than spawning a second download elsewhere.

"Best" follows the configured intrinsic preferences and known filename/file
metadata (including available Plex HDR/DV/Atmos stream facts), not new bitstream
analysis or a promise of a future release. Provider cache
APIs, release availability and exact completion can be uncertain. The scheduler
remains bounded and restart-reconstructible; no permanent monitoring database,
new dependencies, service or worker was added.

## Configurable queue retirement

The recommended default is **`target-met` with 90 days**. Set `off` to preserve
all subscriptions until manual removal. `playable-after-grace` instead permits
a verified playable fallback meeting the main version's hard requirements even
when its upgrade target is unmet; consider setting grace to **180 days** when
using that more aggressive mode. Both modes use the same configurable grace.

Retirement requires the grace to have elapsed since the latest required date:
queue addition/re-addition, Plex acquisition, and home-release availability
(movies) or final normal episode airdate (shows). A first applicable known
digital/physical/TV release establishes movie home availability; theatrical-only
metadata does not. Missing/invalid dates, unavailable APIs, identity conflicts,
multipart/unknown files, or unreadable replacements retain the subscription.

- Movies must be released and have a freshly verified usable library file.
- Returning, upcoming, in-production and hiatus shows never retire.
- Ended/cancelled shows need complete normal-season episode metadata, no known
  future/unaired episodes, and every expected episode available at the selected
  policy's quality. Specials remain excluded, matching the existing show scope.
- `target-met` requires all main upgrade predicates on one file per movie or
  episode. Optional IMAX/3D/Open Matte versions do not block retirement; an
  explicit hard requirement in the primary version does.
- Watched status and unsuccessful searches are not retirement evidence.

At most **one aged candidate per successful membership poll** is checked, using
round-robin selection. Young entries cause no extra retirement API requests.
Eligibility uses fresh Trakt metadata, Plex `checkFiles` and a bounded one-byte
read. File verification stops starting new reads after a 30-second candidate
deadline (an in-flight request still uses its normal timeout). Large/slow shows
that cannot be completely verified within a pass are conservatively retained;
there is no persisted verification cursor. Before removal the exact main-list entry timestamp is checked again;
the service verifies absence afterward. It never deletes media, an entire list,
or entries from unrelated lists. Trakt has no cross-request atomic removal;
changes racing between the last read and removal cannot be eliminated entirely.

**Retirement stops future upgrades through this queue.** It does not establish
that no later REMUX, remaster or alternate presentation will appear. Re-add a
retired title to request monitoring again. The grace is not "time since last
improvement": Plex's item-added timestamp may predate its latest replacement.
No history database or archive list is introduced.

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

- Seven policy/scheduling settings, no new dependencies or persistent job state.
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