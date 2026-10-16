"""Best-effort IMDb correction for positively linked, newly downloaded movies.

This is NOT an acquisition filter. Missing IDs, files, matches, or unavailable
APIs never reject a download. Uncertain library items are left untouched.
"""
import json
import math
import ntpath
import re
import time
from pathlib import Path
from types import SimpleNamespace


def _log(message):
    from ui.ui_print import ui_print
    ui_print('[plex identity] ' + message)


def _state_path():
    from ui.ui_print import config_dir
    return Path(config_dir or '.') / 'plex_download_identity.json'


def _load():
    try:
        data = json.loads(_state_path().read_text(encoding='utf-8'))
        if not isinstance(data, list):
            return []
        return [r for r in data if isinstance(r, dict)
                and re.fullmatch(r'tt\d+', str(r.get('imdb', '')))
                and isinstance(r.get('file'), str) and r['file']
                and isinstance(r.get('size'), int) and r['size'] > 0
                and isinstance(r.get('created'), (int, float))
                and time.time() - r['created'] < 30 * 86400]
    except (OSError, ValueError, TypeError):
        return []


def _save(records):
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        _log('could not persist pending check; download remains allowed')
        return False


def _imdb(item):
    values = list(getattr(item, 'EID', []) or [])
    values += [getattr(x, 'id', '') for x in getattr(item, 'Guid', []) or []]
    ids = {str(v).split('://', 1)[1] for v in values
           if re.fullmatch(r'imdb://tt\d+', str(v))}
    return next(iter(ids)) if len(ids) == 1 else None


def capture(item):
    """Keep the request's ID before title-based metadata enrichment can alter it."""
    if getattr(item, 'type', None) == 'movie' and not hasattr(item, '_requested_imdb'):
        item._requested_imdb = _imdb(item)


def _relative(name):
    if not isinstance(name, str):
        return None
    # Providers return torrent-relative paths, sometimes prefixed with '/'.
    name = name.lstrip('/\\')
    if ntpath.splitdrive(name)[0] or '..' in name.replace('\\', '/').split('/'):
        return None
    name = ntpath.normcase(ntpath.normpath(name))
    if not ntpath.dirname(name) or not name.lower().endswith(('.mkv', '.mp4', '.avi', '.m4v', '.ts')):
        return None
    return name


def remember(item, release):
    """Persist only requested ID + selected relative file/size; no credentials."""
    if getattr(item, 'type', None) != 'movie':
        return False
    imdb = getattr(item, '_requested_imdb', _imdb(item))
    if imdb is None:
        _log('no unique IMDb ID; download remains allowed without rematching')
        return False
    records = _load()
    selected = [f for f in getattr(release, 'files', []) or []
                if getattr(f, 'wanted', False) is True
                and str(getattr(f, 'name', '')).lower().endswith(('.mkv', '.mp4', '.avi', '.m4v', '.ts'))]
    # A movie acquisition can contain bonus videos or several films. Never
    # apply one requested ID to every wanted video in a pack.
    if len(selected) != 1:
        return False
    changed = False
    for file in selected:
        name = _relative(getattr(file, 'name', None))
        try:
            size = float(file.size) * 1_000_000_000
            if not math.isfinite(size) or size <= 0:
                continue
        except (AttributeError, TypeError, ValueError):
            continue
        if name is None:
            continue
        size = round(size)
        if not any(r['imdb'] == imdb and r['file'] == name and r['size'] == size for r in records):
            records.append(dict(imdb=imdb, file=name, size=size, created=time.time()))
            changed = True
    return _save(records) if changed else False


def linked(item, record):
    # Matching an entire Plex item must not alter another file/edition merged
    # into it. Start conservatively with one Media and one Part only.
    media = getattr(item, 'Media', []) or []
    if getattr(item, 'type', None) != 'movie' or len(media) != 1:
        return False
    parts = getattr(media[0], 'Part', []) or []
    if len(parts) != 1:
        return False
    part = parts[0]
    try:
        path = ntpath.normcase(ntpath.normpath(part.file))
        relative = record['file']
        return (path.endswith('\\' + relative)
                and int(part.size) == record['size'])
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _get(url, params=None):
    from content.services import plex
    try:
        if not plex.users:
            return None
        response = plex.session.get(
            url, params=params, headers={'Accept': 'application/json',
                                         'X-Plex-Token': plex.users[0][1]}, timeout=20)
        if response.status_code != 200:
            return None
        return json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception:
        return None


def _local(key):
    from content.services import plex
    data = _get(plex.library.url + '/library/metadata/' + str(key), {'includeGuids': 1})
    values = getattr(getattr(data, 'MediaContainer', None), 'Metadata', [])
    if len(values) != 1 or str(getattr(values[0], 'ratingKey', '')) != str(key):
        return None
    return values[0]


def exact_match(key, imdb):
    """Never choose a first title result; verify the candidate's external ID."""
    from content.services import plex
    if not re.fullmatch(r'\d+', str(key)) or not re.fullmatch(r'tt\d+', str(imdb)):
        return None
    data = _get(plex.library.url + '/library/metadata/' + str(key) + '/matches',
                {'manual': 1, 'title': imdb, 'agent': 'tv.plex.agents.movie', 'language': 'en-US'})
    candidates = getattr(getattr(data, 'MediaContainer', None), 'SearchResult', [])
    verified = {}
    for match in candidates:
        guid = str(getattr(match, 'guid', ''))
        if not re.fullmatch(r'plex://movie/[a-fA-F0-9]{24}', guid):
            continue
        details = _get('https://discover.provider.plex.tv/library/metadata/' + guid.rsplit('/', 1)[1],
                       {'includeGuids': 1})
        metadata = getattr(getattr(details, 'MediaContainer', None), 'Metadata', [])
        if len(metadata) == 1 and _imdb(metadata[0]) == imdb:
            verified[guid] = match
    return next(iter(verified.values())) if len(verified) == 1 else None


def _repair(item, matching, imdb):
    from content.services import plex
    key = str(getattr(item, 'ratingKey', ''))
    if not key.isdigit():
        return None
    current = _local(key)
    if current is None or not all(linked(current, r) for r in matching):
        return None
    if _imdb(current) == imdb:
        return current
    match = exact_match(key, imdb)
    if match is None:
        _log('no verified match for ' + imdb + '; keeping metadata, download remains allowed')
        return None
    latest = _local(key)
    if latest is None or not all(linked(latest, r) for r in matching):
        return None
    response = plex.session.put(
        plex.library.url + '/library/metadata/' + key + '/match',
        params={'guid': match.guid, 'name': getattr(match, 'name', '')},
        headers={'X-Plex-Token': plex.users[0][1]}, timeout=30)
    if response.status_code not in (200, 201, 204):
        _log('metadata correction refused; download remains allowed')
        return None
    verified = _local(key)
    if (verified is None or _imdb(verified) != imdb
            or not all(linked(verified, r) for r in matching)):
        # PMS may update asynchronously; never acknowledge a different file.
        return None
    _log('corrected movie ' + key + ' to ' + imdb + ' (' + verified.title + ')')
    return verified


def reconcile(items):
    """Called after a scan's library read, before quality evaluation/cleanup."""
    from content.services import plex
    try:
        records = _load()
        if not records:
            return
        remaining = list(records)
        for item in items:
            if getattr(item, 'type', None) != 'movie':
                continue
            matching = [r for r in records if linked(item, r)]
            if not matching:
                continue
            ids = {r['imdb'] for r in matching}
            if len(ids) != 1 or sum(any(linked(x, r) for r in matching) for x in items) != 1:
                _log('ambiguous file association; keeping existing match and allowing downloads')
                continue
            if any(float(r.get('next_check', 0)) > time.time() for r in matching):
                continue
            for record in matching:
                record['next_check'] = time.time() + 600
            imdb = next(iter(ids))
            try:
                verified = _repair(item, matching, imdb)
                if verified is not None:
                    item.__dict__.update(verified.__dict__)
                    item.EID = [g.id for g in getattr(verified, 'Guid', [])]
                    remaining = [r for r in remaining if r not in matching]
            except Exception as exc:
                _log('check skipped (' + type(exc).__name__ + '); download remains allowed')
        _save(remaining)
    except Exception as exc:
        # No request URLs, tokens, rejection, deletion or exception propagation.
        _log('check skipped (' + type(exc).__name__ + '); download remains allowed')