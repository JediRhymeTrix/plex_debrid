"""Stateless queue retirement. Never deletes media or changes download rules."""
import math
import json
import time
from types import SimpleNamespace

from content import quality
from content.cleanup import _readable
from content.services import plex, trakt


POLICIES = ('off', 'target-met', 'playable-after-grace')


def old_enough(dates, now, days):
    try:
        return bool(dates) and all(d is not None and math.isfinite(float(d)) and float(d) > 0
                                   for d in dates) and now - max(dates) >= days * 86400
    except (TypeError, ValueError):
        return False


def _ids(obj):
    values = dict(getattr(getattr(obj, 'ids', None), '__dict__', {}))
    for value in list(getattr(obj, 'EID', []) or []) + [g.id for g in getattr(obj, 'Guid', []) or []]:
        if '://' in str(value):
            kind, number = value.split('://', 1)
            if kind in ('imdb', 'tmdb', 'tvdb'):
                if values.get(kind) and str(values[kind]) != number:
                    return {}
                values[kind] = number
    return {k: str(v) for k, v in values.items() if k in ('imdb', 'tmdb', 'tvdb') and v}


def _same(a, b):
    left, right = _ids(a), _ids(b)
    common = left.keys() & right.keys()
    return bool(common & {'imdb', 'tmdb'}) and all(left[k] == right[k] for k in common)


def _pending(item, now):
    from content.classes import media
    session = getattr(media, 'session_releases', {})
    return bool(session) and any(now - getattr(r, 'grabbed_at', now) < 7200
                                 for r in session.get(item.query(), []))


def _trakt(path, user):
    previous = trakt.current_user
    try:
        trakt.current_user = user
        return trakt.get('https://api.trakt.tv' + path)[0]
    finally:
        trakt.current_user = previous


def _fresh(item):
    from content.identity import _get
    key = str(getattr(item, 'ratingKey', ''))
    if not key.isdigit():
        return None
    response = _get(plex.library.url + '/library/metadata/' + key,
                    {'checkFiles': 1, 'includeMedia': 1, 'includeGuids': 1})
    entries = getattr(getattr(response, 'MediaContainer', None), 'Metadata', [])
    if len(entries) != 1 or str(getattr(entries[0], 'ratingKey', '')) != key:
        return None
    return entries[0] if getattr(entries[0], 'type', None) == item.type else None


def _remove(path, data, user):
    # A destructive timeout may already have committed. Do not use trakt.post's
    # automatic retry, which could remove a subsequent user re-add.
    response = trakt.session.post('https://api.trakt.tv' + path, json=data,
                                  headers={'trakt-api-key': trakt.client_id, 'trakt-api-version': '2',
                                           'Authorization': 'Bearer ' + user[1]}, timeout=30)
    if response.status_code != 200:
        return None
    return json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))


def covered(item, policy, deadline=None):
    """One independently readable file must meet all main objective predicates."""
    configs = [v for v in quality.releases.sort.versions if '\u0336' not in v[0]]
    if not configs or any(t[0] not in ('media type', 'retries') for t in configs[0][1]):
        return False
    version = next(iter(quality.active_versions(item, collected=True)), None)
    if version is None or version.name != configs[0][0]:
        return False
    weights = ('requirement', 'upgrade') if policy == 'target-met' else ('requirement',)
    for rule in version.rules:
        # A readable collected file proves availability, not original provenance.
        if rule[0] == 'cache status' and rule[2] == 'cached':
            continue
        if rule[1] in weights and (rule[0] not in ('title', 'resolution', 'size', 'bitrate')
                                   or rule[2] in ('highest', 'lowest')):
            return False
    for media in getattr(item, 'Media', []) or []:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        candidate = quality.file_release(media)
        parts = getattr(media, 'Part', [])
        duration = float(getattr(media, 'duration', 0) or 0)
        if (candidate is None or not parts or not math.isfinite(candidate.size) or candidate.size <= 0
                or not math.isfinite(duration) or duration <= 0):
            continue
        if any(r[0] == 'bitrate' and r[1] in weights for r in version.rules):
            bitrate = float(getattr(media, 'bitrate', 0) or 0)
            if not math.isfinite(bitrate) or bitrate <= 0:
                continue
        if any(str(getattr(p, 'exists', '')).lower() not in ('true', '1')
               or str(getattr(p, 'accessible', '')).lower() not in ('true', '1') for p in parts):
            continue
        if quality.matches(candidate, version, target=policy == 'target-met') and _readable(plex, media):
            return deadline is None or time.monotonic() < deadline
    return False


def eligible(row, library, user, policy, days, now):
    """Unknown facts retain subscriptions. Full shows mean normal seasons only."""
    from content import monitor
    deadline = time.monotonic() + 30
    identity = monitor.key(row)
    listed = monitor.timestamp(getattr(row, 'listed_at', None))
    if policy == 'off' or identity is None or not old_enough([listed], now, days):
        return False
    requested = getattr(row, identity[0])
    local = next((x for x in library if x.type == identity[0] and _same(requested, x)), None)
    if local is None or not old_enough([listed, float(getattr(local, 'addedAt', 0) or 0)], now, days):
        return False
    if _pending(local, now):
        return False
    summary = _trakt('/' + identity[0] + 's/' + str(identity[1]) + '?extended=full', user)
    if (summary is None or str(getattr(getattr(summary, 'ids', None), 'trakt', '')) != str(identity[1])
            or not _same(requested, summary)):
        return False
    if identity[0] == 'movie':
        if getattr(summary, 'status', '') != 'released':
            return False
        releases = _trakt('/movies/' + str(identity[1]) + '/releases', user)
        if not isinstance(releases, list):
            return False
        dates = [monitor.timestamp(getattr(r, 'release_date', None)) for r in releases
                 if getattr(r, 'release_type', '') in ('digital', 'physical', 'tv')]
        if not dates or any(not d for d in dates):
            return False
        release = monitor.timestamp(getattr(summary, 'released', None))
        if not old_enough([listed, release, min(dates)], now, days):
            return False
        fresh = _fresh(local)
        return bool(fresh is not None and _same(summary, fresh) and _same(requested, fresh)
                    and old_enough([listed, release,
                                    min(dates), float(getattr(fresh, 'addedAt', 0) or 0)], now, days)
                    and covered(fresh, policy, deadline))
    if getattr(summary, 'status', '') not in ('ended', 'canceled', 'cancelled'):
        return False
    seasons = _trakt('/shows/' + str(identity[1]) + '/seasons?extended=episodes,full', user)
    if not isinstance(seasons, list) or not seasons:
        return False
    expected = []
    for season in seasons:
        if getattr(season, 'number', -1) == 0:
            continue
        episodes = getattr(season, 'episodes', [])
        count = getattr(season, 'episode_count', None)
        if not episodes or count != len(episodes) or getattr(season, 'aired_episodes', None) != count:
            return False
        for episode in episodes:
            aired = monitor.timestamp(getattr(episode, 'first_aired', None))
            if not aired or aired > now:
                return False
            expected.append((season.number, episode.number, aired))
    if not expected or len(expected) != getattr(summary, 'aired_episodes', None):
        return False
    if len({(s, e) for s, e, date in expected}) != len(expected):
        return False
    if not old_enough([listed, max(x[2] for x in expected)], now, days):
        return False
    # Prove complete local scope before paying for any live media checks.
    local_episodes = {(e.parentIndex, e.index): e for s in getattr(local, 'Seasons', []) for e in s.Episodes}
    if any((s, n) not in local_episodes for s, n, date in expected):
        return False
    if any(_pending(local_episodes[(s, n)], now) for s, n, date in expected):
        return False
    if not old_enough([float(getattr(local_episodes[(s, n)], 'addedAt', 0) or 0) for s, n, date in expected], now, days):
        return False
    fresh_show = _fresh(local)
    if (fresh_show is None or not _same(summary, fresh_show) or not _same(requested, fresh_show)
            or not getattr(fresh_show, 'guid', None)):
        return False
    clocks = [listed, max(x[2] for x in expected), float(getattr(fresh_show, 'addedAt', 0) or 0)]
    from content.identity import _get
    response = _get(plex.library.url + '/library/metadata/' + str(local.ratingKey) + '/allLeaves',
                    {'checkFiles': 1, 'includeMedia': 1})
    container = getattr(response, 'MediaContainer', None)
    entries = getattr(container, 'Metadata', [])
    if (container is None or not hasattr(container, 'size') or int(container.size) != len(entries)
            or int(getattr(container, 'totalSize', len(entries))) != len(entries)):
        return False
    episodes = {(getattr(e, 'parentIndex', None), getattr(e, 'index', None)): e for e in entries}
    if len(episodes) != len(entries):
        return False
    for season, number, aired in expected:
        episode = episodes.get((season, number))
        if (episode is None or getattr(episode, 'type', None) != 'episode'
                or getattr(episode, 'grandparentGuid', None) != fresh_show.guid):
            return False
        clocks.append(float(getattr(episode, 'addedAt', 0) or 0))
    return (old_enough(clocks, now, days)
            and all(covered(episodes[(s, n)], policy, deadline) for s, n, date in expected))


def run(worker, now):
    """At most one aged candidate per successful membership poll, round-robin."""
    from content import monitor
    if monitor.retirement_policy == 'off' or not worker.rows or worker.main_list_id is None:
        return []
    days = float(monitor.retirement_grace_days)
    candidates = [r for r in worker.rows if old_enough(
        [monitor.timestamp(getattr(r, 'listed_at', None))], now, days)]
    if not candidates:
        return []
    row = candidates[worker.retirement_cursor % len(candidates)]
    worker.retirement_cursor += 1
    identity = monitor.key(row)
    try:
        if not eligible(row, worker.library, worker.user, monitor.retirement_policy, days, now):
            return []
        path = '/users/me/lists/' + str(worker.main_list_id) + '/items/movies,shows?extended=full'
        latest = monitor.pages(path, worker.user)
        if latest is None:
            return []
        current = [r for r in latest if monitor.key(r) == identity]
        if len(current) != 1 or current[0].listed_at != row.listed_at:
            return []
        response = _remove('/users/me/lists/' + str(worker.main_list_id) + '/items/remove',
                           monitor.payload([row]), worker.user)
        deleted = getattr(response, 'deleted', None)
        if deleted is None or getattr(deleted, identity[0] + 's', 0) != 1:
            return []
        confirmed = monitor.pages(path, worker.user)
        if confirmed is None or any(monitor.key(r) == identity for r in confirmed):
            return []
        worker.rows = [r for r in worker.rows if monitor.key(r) != identity]
        worker.wake.discard(identity)
        for cache in (worker.wake_since, worker.cache, worker.next_metadata):
            cache.pop(identity, None)
        monitor.ui_print('[monitor] retired ' + str(identity) + ' under ' + monitor.retirement_policy
                         + ' after ' + str(days) + '-day grace; media retained')
        return [identity]
    except Exception as exc:
        monitor.ui_print('[monitor] retirement deferred: ' + type(exc).__name__)
        return []