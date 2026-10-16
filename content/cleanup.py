"""Delete isolated single-file TorBox downloads after fail-closed checks.

Never unlink mount paths or use Plex's filesystem delete endpoint. These can
delete a whole backing season pack on debrid mounts. Only a verified TorBox
ID with exactly ONE file can be removed. Unknown mappings and RD are skipped.
"""
import copy
import ntpath
import re

from content import quality


def _true(value):
    return str(value).lower() in ('true', '1')


def _playing(plex, key):
    response = plex.get(plex.library.url + '/status/sessions?X-Plex-Token=' + plex.users[0][1])
    if response is None or not hasattr(response, 'MediaContainer'):
        return True
    container = response.MediaContainer
    if not hasattr(container, 'size'):
        return True
    entries = getattr(container, 'Metadata', [])
    if int(container.size) != len(entries) or int(getattr(container, 'totalSize', len(entries))) != len(entries):
        return True
    return any(str(getattr(x, 'ratingKey', '')) == key for x in entries)


def _fresh(plex, item):
    key = str(getattr(item, 'ratingKey', ''))
    if not key.isdigit():
        return None
    response = plex.get(plex.library.url + '/library/metadata/' + key
                        + '?checkFiles=1&includeMedia=1&X-Plex-Token=' + plex.users[0][1])
    if response is None or not hasattr(response, 'MediaContainer'):
        return None
    entries = getattr(response.MediaContainer, 'Metadata', [])
    if len(entries) != 1 or str(getattr(entries[0], 'ratingKey', '')) != key:
        return None
    fresh = copy.copy(item)
    if getattr(entries[0], 'type', None) != item.type:
        return None
    fresh.__dict__.update(entries[0].__dict__)
    fresh.Media = getattr(entries[0], 'Media', [])
    return fresh


def _shared(plex, path, item):
    """A multi-episode file must never be removed on behalf of one episode."""
    path = ntpath.normcase(ntpath.normpath(path))
    count = 0
    for entry in plex.current_library:
        leaves = ([entry] if entry.type == 'movie' else
                  [e for s in getattr(entry, 'Seasons', []) for e in s.Episodes])
        for leaf in leaves:
            for media in getattr(leaf, 'Media', []):
                count += sum(ntpath.normcase(ntpath.normpath(p.file)) == path
                             for p in getattr(media, 'Part', []) if getattr(p, 'file', None))
    if count > 1:
        return True
    if item.type == 'episode':
        name = ntpath.basename(path)
        episodes = re.findall(r's\d+e\d+', name, re.I)
        if len(episodes) != 1 or re.search(r's\d+e\d+[ ._-]*(?:e\d+|-\d+)', name, re.I):
            return True
    return False


def _readable(plex, media):
    part = media.Part[0]
    key = getattr(part, 'key', '')
    if not re.fullmatch(r'/library/parts/\d+/\d+/file\.[a-zA-Z0-9]+', key):
        return False
    response = plex.session.get(plex.library.url + key,
                                headers={'X-Plex-Token': plex.users[0][1], 'Range': 'bytes=0-0'},
                                stream=True, timeout=15)
    try:
        if response.status_code not in (200, 206):
            return False
        if response.status_code == 206:
            total = response.headers.get('Content-Range', '').rsplit('/', 1)[-1]
        else:
            total = response.headers.get('Content-Length', '')
        if not total.isdigit() or int(total) != int(part.size):
            return False
        return bool(next(response.iter_content(chunk_size=1), b''))
    finally:
        response.close()


def _torbox_inventory():
    from debrid.services import torbox
    response = torbox.session.get(
        torbox.base_url + '/torrents/mylist?bypass_cache=true',
        headers={'Authorization': 'Bearer ' + torbox.api_key,
                 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0'},
        timeout=30)
    if response.status_code != 200:
        return None
    data = response.json()
    if data.get('success') is not True or not isinstance(data.get('data'), list):
        return None
    return data['data']


def _backend_plan(old, keeper, inventory, root):
    """Prove distinct backend objects and exact mount-relative path/size matches."""
    if not root or not isinstance(inventory, list):
        return None
    root = ntpath.normcase(ntpath.normpath(root))
    if not ntpath.isabs(root):
        return None
    def locate(media):
        path = ntpath.normcase(ntpath.normpath(media.Part[0].file))
        try:
            if ntpath.commonpath([root, path]) != root:
                return None
        except ValueError:
            return None
        relative = ntpath.relpath(path, root)
        matches = []
        for torrent in inventory:
            if not isinstance(torrent, dict) or not isinstance(torrent.get('files'), list):
                return None
            for file in torrent['files']:
                if not isinstance(file, dict) or not file.get('name'):
                    return None
                if ntpath.normcase(ntpath.normpath(file['name'])) == relative:
                    matches.append((torrent, file))
        if len(matches) != 1:
            return None
        t, file = matches[0]
        if int(file.get('size', -1)) != int(media.Part[0].size):
            return None
        if (not _true(t.get('download_finished', False))
                or not _true(t.get('download_present', False))
                or not str(t.get('id', '')).isdigit()
                or not re.fullmatch(r'[a-fA-F0-9]{40}', t.get('hash', ''))):
            return None
        if sum(x.get('hash', '').lower() == t['hash'].lower() for x in inventory) != 1:
            return None
        return t
    a, b = locate(old), locate(keeper)
    if (a is None or b is None or a['id'] == b['id'] or a['hash'].lower() == b['hash'].lower()
            or len(a['files']) != 1 or int(a.get('size', -1)) != int(old.Part[0].size)):
        return None
    return a, b


def _delete_torbox(torrent_id):
    from debrid.services import torbox
    response = torbox.session.post(
        torbox.base_url + '/torrents/controltorrent',
        headers={'Authorization': 'Bearer ' + torbox.api_key},
        json={'operation': 'delete', 'torrent_id': int(torrent_id)}, timeout=30)
    return response.status_code == 200 and response.json().get('success') is True


def _independent_references(plex, item, old, keeper):
    """Fresh ALL-library inverse references, including other mount aliases.

    A same-name OR same-size file anywhere else is treated as a possible alias.
    This intentionally rejects harmless collisions rather than risk a shared ID.
    """
    auth = 'X-Plex-Token=' + plex.users[0][1]
    response = plex.get(plex.library.url + '/library/sections?' + auth)
    if response is None or not hasattr(response, 'MediaContainer'):
        return False
    sections = getattr(response.MediaContainer, 'Directory', [])
    if not sections:
        return False
    found = 0
    victim = old.Part[0]
    for section in sections:
        if section.type not in ('movie', 'show'):
            continue
        kind = '1' if section.type == 'movie' else '4'
        data = plex.get(plex.library.url + '/library/sections/' + str(section.key)
                        + '/all?type=' + kind + '&' + auth)
        if data is None or not hasattr(data, 'MediaContainer'):
            return False
        container = data.MediaContainer
        entries = getattr(container, 'Metadata', [])
        if not hasattr(container, 'size') or int(container.size) != len(entries):
            return False
        if int(getattr(container, 'totalSize', len(entries))) != len(entries):
            return False
        for entry in entries:
            if not getattr(entry, 'Media', []):
                return False
            for m in entry.Media:
                if not getattr(m, 'Part', []):
                    return False
                for p in m.Part:
                    if not getattr(p, 'file', None) or not getattr(p, 'size', None):
                        return False
                    same_name = ntpath.basename(p.file).lower() == ntpath.basename(victim.file).lower()
                    same_size = int(p.size) == int(victim.size)
                    if same_name or same_size:
                        if str(entry.ratingKey) != str(item.ratingKey) or str(m.id) != str(old.id):
                            return False
                        found += 1
    return found == 1


def clean(item, dry_run=False):
    from content.services import plex
    from content.classes import media
    from ui.ui_print import ui_print
    key = str(getattr(item, 'ratingKey', ''))
    done = []
    try:
        if item.downloading():
            return done
        fresh = _fresh(plex, item)
        if fresh is None or _playing(plex, key):
            return done
        # A successful delete is followed by a fresh metadata fetch before any
        # next deletion; stale plans or disappearing keepers are never trusted.
        plan = quality.superseded(fresh)
        for old, keeper in plan:
            if item.downloading():
                break
            fresh = _fresh(plex, item)
            if fresh is None or _playing(plex, key):
                break
            current = next(((a, b) for a, b in quality.superseded(fresh)
                            if str(a.id) == str(old.id) and str(b.id) == str(keeper.id)), None)
            if current is None:
                continue
            old, keeper = current
            if any(not _true(getattr(p, 'exists', False))
                   or not _true(getattr(p, 'accessible', False))
                   for m in (old, keeper) for p in m.Part):
                continue
            # The first release of cleanup deliberately opts out of all special
            # presentations, even when filenames suggest same-class dominance.
            if any(re.search(r'IMAX|3D|MATTE|H?SBS|H?OU|FRAME[ ._-]?PACK', m.Part[0].file, re.I)
                   or str(getattr(m, 'videoStereoMode', '')).lower() not in ('', '0', 'mono', '2d')
                   for m in (old, keeper)):
                continue
            if any(_shared(plex, m.Part[0].file, item) for m in (old, keeper)):
                continue
            backend = _backend_plan(old, keeper, _torbox_inventory(),
                                    media.upgrade_cleanup_torbox_root)
            if backend is None:
                continue
            if not _independent_references(plex, item, old, keeper):
                continue
            if dry_run:
                done.append((old, keeper))
                continue
            if not _readable(plex, keeper) or _playing(plex, key):
                continue
            # Provider inventory is refreshed immediately before mutation.
            confirmed = _backend_plan(old, keeper, _torbox_inventory(),
                                      media.upgrade_cleanup_torbox_root)
            def identity(records):
                return tuple((t['id'], t['hash'], tuple(sorted((f['name'], f['size']) for f in t['files'])))
                             for t in records)
            if confirmed is None or identity(confirmed) != identity(backend):
                continue
            latest = _fresh(plex, item)
            if latest is None or _playing(plex, key):
                continue
            valid = next(((a, b) for a, b in quality.superseded(latest)
                          if str(a.id) == str(old.id) and str(b.id) == str(keeper.id)
                          and a.Part[0].file == old.Part[0].file
                          and b.Part[0].file == keeper.Part[0].file), None)
            if valid is None or not _independent_references(plex, item, old, keeper):
                continue
            def media_identity(m):
                return (str(m.id), str(getattr(m, 'duration', '')),
                        str(getattr(m, 'videoResolution', '')),
                        str(getattr(m, 'videoStereoMode', '')),
                        tuple((str(p.id), p.file, int(p.size), str(getattr(p, 'duration', '')),
                               _true(getattr(p, 'exists', False)),
                               _true(getattr(p, 'accessible', False))) for p in m.Part))
            if (media_identity(valid[0]) != media_identity(old)
                    or media_identity(valid[1]) != media_identity(keeper)
                    or item.downloading() or _playing(plex, key)):
                continue
            if not _delete_torbox(confirmed[0]['id']):
                ui_print('[plex cleanup] TorBox deletion refused for media ' + str(old.id))
                break
            done.append((old, keeper))
            ui_print('[plex cleanup] removed superseded file: ' + old.Part[0].file
                     + ' | kept: ' + keeper.Part[0].file)
            # Do not let the library cache keep advertising a deleted copy.
            item.Media = [m for m in item.Media if str(m.id) != str(old.id)]
    except Exception as exc:
        # Do not log request exception URLs (they can contain credentials).
        ui_print('[plex cleanup] skipped item ' + key + ': ' + type(exc).__name__)
    return done