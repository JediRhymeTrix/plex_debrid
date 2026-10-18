"""One authoritative Trakt subscription list; an optional inbox is disabled by default.

No database, task journal, extra worker threads or third-party dependencies.
The original automation loop is unchanged when `list` is empty.
"""
import copy
import datetime
import hashlib
import json
import math
import time
from collections import OrderedDict

from content.services import trakt
from ui.ui_print import ui_print


list = ''
inbox = ''
poll_minutes = '10'
budget = '10'
# Hours: recent episodes, recent movies, mature releases, catalogue/alternates.
schedule = '1,24,168,720'
CACHE_LEAVES = 5000
COOLDOWNS = 20000


def configuration():
    prefix = "'s private list: "
    owner = next((u for u in trakt.users if list.startswith(u[0] + prefix)), None)
    sources = [s.strip() for s in inbox.split(';') if s.strip()]
    if (owner is None or not list[len(owner[0] + prefix):].strip()
            or any(not s.startswith(owner[0] + prefix) or s == list or s not in trakt.lists
                   for s in sources)):
        raise ValueError('main list must be owned/private; optional inboxes must be distinct owned private Trakt lists')
    hours = tuple(float(s) for s in schedule.split(','))
    poll, attempts = float(poll_minutes), int(budget)
    if (len(hours) != 4 or any(not math.isfinite(h) or h <= 0 for h in hours)
            or not math.isfinite(poll) or poll < 1 or attempts < 1 or attempts > 100):
        raise ValueError('use four positive schedule hours, poll >= 1 minute, budget 1..100')
    return owner, sources, poll * 60, attempts, hours


def timestamp(value):
    try:
        date = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        return date.timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0


def key(row):
    kind = getattr(row, 'type', '')
    obj = getattr(row, kind, None) if kind in ('movie', 'show') else None
    number = getattr(getattr(obj, 'ids', None), 'trakt', None)
    return (kind, int(number)) if str(number).isdigit() else None


def write(path, data, user):
    previous = trakt.current_user
    try:
        trakt.current_user = user
        return trakt.post('https://api.trakt.tv' + path, json.dumps(data))
    finally:
        trakt.current_user = previous


def pages(path, user):
    """Never return a partial snapshot as a successful empty membership set."""
    previous = trakt.current_user
    result = []
    try:
        trakt.current_user = user
        page, count = 1, 1
        while page <= count:
            data, headers = trakt.get('https://api.trakt.tv' + path
                                      + ('&' if '?' in path else '?') + 'limit=100&page=' + str(page))
            if not isinstance(data, type([])):
                return None
            fields = {k.lower(): v for k, v in (headers or {}).items()}
            count = int(fields.get('x-pagination-page-count', 1))
            if page == 1 and not data and count == 0:
                return []
            if count < page or count > 1000 or (page < count and not data):
                return None
            result.extend(data)
            page += 1
        return result
    except (TypeError, ValueError):
        return None
    finally:
        trakt.current_user = previous


def payload(rows):
    data = {'movies': [], 'shows': []}
    for row in rows:
        identity = key(row)
        if identity:
            data[identity[0] + 's'].append({'ids': {'trakt': identity[1]}})
    return data


def enroll(incoming, monitored, user, source_id, destination_id):
    """Idempotent add -> verify -> remove from this inbox only. No broad removal."""
    members = {key(r) for r in monitored}
    missing = [r for r in incoming if key(r) and key(r) not in members]
    if missing:
        write('/users/me/lists/' + str(destination_id) + '/items', payload(missing), user)
    verified = pages('/users/me/lists/' + str(destination_id) + '/items/movies,shows?extended=full', user)
    if verified is None:
        return monitored, set()
    confirmed = {key(r) for r in verified}
    accepted = [r for r in incoming if key(r) and key(r) in confirmed]
    wake = {key(r) for r in accepted}
    if accepted:
        # Do not consume a newer remove/re-add that raced our source snapshot.
        current = pages('/users/me/lists/' + str(source_id) + '/items/movies,shows?extended=full', user)
        if current is None:
            return verified, set()
        stamps = {(key(r), getattr(r, 'listed_at', '')) for r in current}
        accepted = [r for r in accepted if (key(r), getattr(r, 'listed_at', '')) in stamps]
        if not accepted:
            return verified, set()
        response = write('/users/me/lists/' + str(source_id) + '/items/remove', payload(accepted), user)
        deleted = getattr(response, 'deleted', None)
        if deleted is not None:
            ui_print('[monitor] enrolled ' + str(len(accepted)) + ' request(s); acknowledged inbox ' + str(source_id))
    return verified, wake


class Worker:
    def __init__(self):
        self.user, self.sources, self.poll, self.limit, self.hours = configuration()
        self.remaining = self.limit
        self.checked = OrderedDict()
        self.cache = OrderedDict()
        self.rows = []
        self.snapshot_loaded = False
        self.wake = set()
        self.wake_since = {}
        self.seen_inbox = set()
        self.library = []
        self.next_library = 0
        self.next_sources = 0
        self.next_metadata = OrderedDict()
        self.next_legacy = 0

    def keep(self, identity, item, now):
        leaves = (max(1, sum(len(s.Episodes) for s in item.Seasons)) if item.type == 'show' else 1)
        self.cache.pop(identity, None)
        if leaves <= CACHE_LEAVES:
            self.cache[identity] = (now, item, leaves)
        while sum(x[2] for x in self.cache.values()) > CACHE_LEAVES:
            self.cache.popitem(last=False)

    def interval(self, identity, obj, now):
        from content import quality
        date = timestamp(getattr(obj, 'first_aired', None) or getattr(obj, 'released', None)
                         or getattr(obj, 'originallyAvailableAt', None))
        age = (now - date) / 86400 if date else 366
        if identity[0] == 'episode' and age <= 7:
            return self.hours[0] * 3600
        found = quality.library_match(obj, self.library) if getattr(obj, 'type', None) in ('movie', 'episode') else None
        if found and getattr(found, 'addedAt', None):
            age = min(age, (now - float(found.addedAt)) / 86400)
        if getattr(obj, 'type', None) in ('movie', 'episode') and not found:
            # Old/unavailable acquisitions stay alive without monthly waits.
            return min(self.hours[1], 24) * 3600
        if age > 365:
            return self.hours[3] * 3600
        if identity[0] == 'movie' and age <= 90:
            return self.hours[1] * 3600
        return self.hours[2] * 3600

    def due(self, identity, obj, now, force=False, optional=False):
        date = timestamp(getattr(obj, 'first_aired', None) or getattr(obj, 'released', None)
                         or getattr(obj, 'originallyAvailableAt', None))
        if date > now:
            return False
        period = self.hours[3] * 3600 if optional else self.interval(identity, obj, now)
        if force:
            return True
        if identity not in self.checked:
            # Reconstruct a stable time bucket; only the current poll's slice
            # is eligible at startup. No whole-catalogue restart storm.
            offset = int.from_bytes(hashlib.sha256(repr(identity).encode()).digest()[:4], 'big') % max(1, int(period))
            if (now - offset) % period > min(period, max(self.poll, 86400)):
                return False
        return now - self.checked.get(identity, 0) >= period

    def mark(self, identity, now):
        self.checked.pop(identity, None)
        self.checked[identity] = now
        while len(self.checked) > COOLDOWNS:
            self.checked.popitem(last=False)

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        lists = pages('/users/me/lists', self.user)
        if lists is None:
            return False
        names = {self.user[0] + "'s private list: " + x.name: x.ids.trakt for x in lists
                 if getattr(x, 'privacy', 'private') == 'private'}
        if list not in names or any(s not in names for s in self.sources):
            ui_print('[monitor] configured list missing; preserving previous snapshot')
            return False
        destination = names[list]
        monitored = pages('/users/me/lists/' + str(destination) + '/items/movies,shows?extended=full', self.user)
        incoming = []
        if monitored is None:
            return False
        for source in self.sources:
            rows = pages('/users/me/lists/' + str(names[source]) + '/items/movies,shows?extended=full', self.user)
            if rows is None:
                return False
            incoming.append((names[source], rows))
        present = set()
        for source, rows in incoming:
            observed = {(source, key(r), getattr(r, 'listed_at', '')) for r in rows if key(r)}
            fresh = observed - self.seen_inbox
            monitored, wake = enroll(rows, monitored, self.user, source, destination) if rows else (monitored, set())
            for sid, identity, date in fresh:
                if identity in wake:
                    self.wake.add(identity)
                    self.wake_since[identity] = now
                    self.next_metadata.pop(identity, None)
            present.update(record for record in observed
                           if record in self.seen_inbox or record[1] in wake)
        self.seen_inbox = present
        # The main list is authoritative and is never consumed. Compare entry
        # timestamps as well as membership so an observed re-add wakes old media.
        previous = {key(r): getattr(r, 'listed_at', '') for r in self.rows}
        self.rows = [r for r in monitored if key(r)]
        members = {key(r) for r in self.rows}
        for row in self.rows:
            identity = key(row)
            changed = identity in previous and previous[identity] != getattr(row, 'listed_at', '')
            added = identity not in previous and (self.snapshot_loaded or now - timestamp(getattr(row, 'listed_at', None)) < 86400)
            if changed or added:
                self.wake.add(identity)
                self.wake_since[identity] = now
                self.next_metadata.pop(identity, None)
                self.cache.pop(identity, None)
        self.wake.intersection_update(members)
        self.wake_since = {k: v for k, v in self.wake_since.items() if k in members}
        for identity in tuple(self.cache):
            if identity not in members:
                self.cache.pop(identity)
        for identity in tuple(self.next_metadata):
            if identity not in members:
                self.next_metadata.pop(identity)
        self.snapshot_loaded = True
        return True

    def hydrate(self, row, now):
        identity = key(row)
        cached = self.cache.get(identity)
        ttl = (86400 if getattr(getattr(row, 'show', None), 'status', '') == 'ended'
               else 3600) if identity[0] == 'show' else 86400
        if cached and now - cached[0] < ttl:
            self.cache.move_to_end(identity)
            return cached[1]
        obj = copy.deepcopy(getattr(row, identity[0]))
        obj.type, obj.user, obj.guid = identity[0], self.user, identity[1]
        obj.watchlistedAt = timestamp(getattr(row, 'listed_at', None))
        previous = trakt.current_user
        try:
            trakt.current_user = self.user
            item = trakt.show(obj) if identity[0] == 'show' else trakt.movie(obj)
            if item.type == 'show':
                item.isanime()
        finally:
            trakt.current_user = previous
        self.keep(identity, item, now)
        item._monitor_managed = True
        if item.type == 'show':
            for season in item.Seasons:
                season._monitor_managed = True
                for episode in season.Episodes:
                    episode._monitor_managed = True
        return item

    def selected(self, item, identity, now, force):
        """Copy only selected show leaves; never mutate the cached metadata graph."""
        import releases
        primary = next((v[0] for v in releases.sort.versions if '\u0336' not in v[0]), '')
        from content import quality
        from content.classes import media
        def optional_only(obj, pending):
            primary_version = next(iter(quality.active_versions(obj, collected=True)), None)
            existing = quality.existing(obj, self.library, media.session_releases)
            reached = bool(primary_version and any(quality.matches(r, primary_version, True) for r in existing))
            return reached or (bool(pending) and not any(v.name in (primary, primary + ' upgrade') for v in pending))
        if item.type == 'movie':
            pending = item.versions()
            if not pending or item.watched() or item.downloading():
                return None, []
            if not self.due(identity, item, now, force, optional_only(item, pending)):
                return None, []
            return copy.deepcopy(item), [identity]
        candidates = []
        for season in item.Seasons:
            for episode in season.Episodes:
                leaf = ('episode', identity[1], episode.parentIndex, episode.index)
                if force and self.checked.get(leaf, 0) >= self.wake_since.get(identity, now):
                    continue
                pending = episode.versions()
                if not pending or episode.watched() or episode.downloading():
                    continue
                if self.due(leaf, episode, now, force, optional_only(episode, pending)):
                    candidates.append((leaf, season, episode))
        candidates.sort(key=lambda x: (self.checked.get(x[0], 0),
                                       -timestamp(getattr(x[2], 'first_aired', None)), x[0]))
        chosen = candidates[:self.remaining]
        result = copy.copy(item)
        result.Seasons = []
        groups = {}
        for leaf, season, episode in chosen:
            if season.index not in groups:
                target = copy.copy(season)
                target.Episodes = []
                groups[season.index] = target
                result.Seasons.append(target)
            groups[season.index].Episodes.append(copy.deepcopy(episode))
        selected = [x[0] for x in chosen]
        return (result, selected) if selected else (None, [])

    def dispatch(self, item, identities, now):
        from content.classes import media
        import scraper
        scraper.failed = False
        for identity in identities:
            self.mark(identity, now)
        self.remaining -= len(identities)
        try:
            item.download(library=self.library)
        except Exception as exc:
            ui_print('[monitor] attempt failed: ' + type(exc).__name__)
            scraper.failed = True
        finally:
            leaves = ([item] if item.type == 'movie' else
                      [e for s in item.Seasons for e in s.Episodes])
            if scraper.failed or any(getattr(e, '_monitor_retry', False) for e in leaves):
                for identity in identities:
                    self.checked[identity] = now - max(self.hours) * 3600 + self.poll
            for cache in (media.session_releases, media.upgrade_attempts):
                while len(cache) > COOLDOWNS:
                    cache.pop(next(iter(cache)))
            if len(media.downloaded_versions) > COOLDOWNS:
                del media.downloaded_versions[:-COOLDOWNS]
        # Keep existing session dedup bridges bounded to current monitoring work.

    def legacy_cycle(self, now):
        """Other sources keep their original behavior; never consume them as inboxes."""
        from content.services import plex, overseerr
        if now < self.next_legacy:
            return
        self.next_legacy = now + 1800
        original = trakt.lists
        try:
            trakt.lists = [s for s in original if s not in self.sources and s != list]
            sources = (plex.watchlist(), trakt.watchlist(), overseerr.requests())
            entries = [entry for source in sources for entry in source]
            from ui import unique
            for entry in unique(entries):
                try:
                    entry.download(library=self.library)
                except Exception as exc:
                    ui_print('[monitor] legacy source deferred: ' + type(exc).__name__)
        finally:
            trakt.lists = original

    def cycle(self, now=None):
        from content.classes import library
        now = time.time() if now is None else now
        self.remaining = self.limit
        if now >= self.next_sources:
            self.snapshot(now)
            self.next_sources = now + self.poll
        if not self.rows:
            if now < self.next_legacy:
                return
        if now >= self.next_library:
            current = library()[0]()
            if not current:
                self.library = []
                self.next_library = now + self.poll
                return
            self.library = current
            self.next_library = now + self.poll
        if not self.library:
            return
        # Wake requests first, then oldest attempts. Stable IDs break ties.
        rows = sorted(self.rows, key=lambda r: (key(r) not in self.wake,
                                               self.checked.get(key(r), 0), key(r)))
        hydrated = 0
        for row in rows:
            if self.remaining <= 0:
                break
            identity = key(row)
            force = identity in self.wake_since
            obj = getattr(row, identity[0])
            if timestamp(getattr(obj, 'first_aired', None) or getattr(obj, 'released', None)) > now:
                continue
            if identity[0] == 'movie':
                obj.type, obj.EID = 'movie', trakt.setEID(obj)
                # Movies hydrate locally (no metadata request). Apply cadence
                # only after determining actual coverage; never intersect two
                # independent startup buckets for the same title.
            if now < self.next_metadata.get(identity, 0):
                continue
            try:
                if identity[0] == 'show':
                    cached = self.cache.get(identity)
                    if not cached or now - cached[0] >= 3600:
                        if hydrated >= self.limit:
                            continue
                        hydrated += 1
                item = self.hydrate(row, now)
                working, identities = self.selected(item, identity, now, force)
                if working is not None:
                    self.dispatch(working, identities, now)
                    if item.type == 'movie' or self.remaining > 0:
                        self.wake.discard(identity)
                        self.wake_since.pop(identity, None)
                elif force:
                    self.wake.discard(identity)
                    self.wake_since.pop(identity, None)
                if identity[0] == 'show':
                    self.mark(identity, now)
                self.next_metadata[identity] = now + (60 if identity in self.wake else
                                                       86400 if getattr(item, 'status', '') == 'ended'
                                                       else 3600 if identity[0] == 'show' else self.poll)
            except Exception as exc:
                ui_print('[monitor] metadata unavailable for ' + str(identity) + ': ' + type(exc).__name__)
                self.mark(identity, now)
                self.wake.discard(identity)
                self.next_metadata[identity] = now + self.poll
            while len(self.next_metadata) > COOLDOWNS:
                self.next_metadata.popitem(last=False)
        ui_print('[monitor] subscriptions=' + str(len(self.rows)) + ' attempts=' + str(self.limit - self.remaining)
                 + '/' + str(self.limit) + ' metadata_leaves=' + str(sum(x[2] for x in self.cache.values())))
        self.legacy_cycle(now)


def run(stop):
    try:
        worker = Worker()
    except (TypeError, ValueError) as exc:
        ui_print('[monitor] invalid configuration: ' + str(exc) + '; no inbox entries changed')
        return
    ui_print('[monitor] stateless Trakt subscriptions enabled; no local monitoring database')
    while not stop():
        try:
            worker.cycle()
        except Exception as exc:
            ui_print('[monitor] cycle deferred: ' + type(exc).__name__)
        # Reuse the existing cooperative stop mechanism; no scheduler thread.
        for _ in range(12):
            if stop():
                return
            time.sleep(5)