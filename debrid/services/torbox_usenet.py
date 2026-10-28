"""Conservative Usenet acquisition; TorBox owns durable job state, not RAM."""
import hashlib
import re
import math
import copy
from types import SimpleNamespace
from urllib.parse import urlsplit

import debrid
from debrid.services import torbox


def defer(reason):
    torbox.ui_print('[torbox] Usenet deferred: ' + reason + '; job retained, no fallback submitted')
    raise debrid.Deferred(reason)


def preflight(element):
    """Prevent a torrent fallback racing unresolved owned Usenet work."""
    prefix = request_prefix(element)
    for row in inventory():
        if not row.name.startswith(prefix):
            continue
        fresh = detail(row.id)
        if getattr(fresh, 'name', None) != row.name:
            defer('owned job identity changed')
        if getattr(fresh, 'download_state', '') in ('failed', 'error', 'cancelled', 'canceled'):
            continue
        if (getattr(fresh, 'download_finished', False) is not True
                or getattr(fresh, 'download_present', False) is not True):
            defer('owned Usenet work still pending')
        if files_for(fresh, element.files()) is None:
            defer('owned Usenet file coverage unavailable')


def request_prefix(element):
    ids = (getattr(element, 'grandparentEID', None) or getattr(element, 'parentEID', None)
           or getattr(element, 'EID', []))
    imdb = getattr(element, '_requested_imdb', None)
    identity = ('imdb://' + imdb if imdb else next((x for x in ids if x.startswith('imdb://')), None))
    identity = identity or (sorted(ids)[0] if ids else
                           element.query() if callable(getattr(element, 'query', None)) else getattr(element, 'title', ''))
    identity = str(getattr(element, 'type', '')) + ':' + identity
    if getattr(element, 'type', '') == 'episode':
        identity += ':' + str(element.parentIndex) + ':' + str(element.index)
    elif getattr(element, 'type', '') == 'season':
        identity += ':' + str(element.index)
    return 'pdusenet-' + hashlib.sha256(identity.encode()).hexdigest()[:16] + '-'


def marker(release, element):
    return request_prefix(element) + hashlib.sha256(release.title.encode()).hexdigest()[:16] + ' ' + release.title


def provider_get(path):
    try:
        response = torbox.session.get(torbox.base_url + path,
            headers={'Authorization': 'Bearer ' + torbox.api_key}, timeout=20)
        if response.status_code == 200:
            data = response.json()
            if data.get('success') is True:
                return data.get('data')
    except Exception:
        pass
    defer('provider response unavailable')


def inventory():
    rows = []
    for offset in range(0, 10000, 1000):
        data = provider_get('/usenet/mylist?bypass_cache=true&limit=1000&offset=' + str(offset))
        if not isinstance(data, list):
            defer('inventory unavailable')
        if any(not isinstance(r, dict) or not str(r.get('id', '')).isdigit()
               or not isinstance(r.get('name'), str) for r in data):
            defer('malformed provider inventory')
        rows.extend(SimpleNamespace(**r) for r in data)
        if len(data) < 1000:
            if len({getattr(r, 'id', None) for r in rows}) != len(rows):
                defer('ambiguous provider inventory')
            return rows
    defer('inventory exceeds safety bound')


def detail(number):
    data = provider_get('/usenet/mylist?id=' + str(number) + '&bypass_cache=true')
    rows = data if isinstance(data, list) else [data]
    matches = [r for r in rows if isinstance(r, dict) and str(r.get('id')) == str(number)]
    if len(matches) != 1:
        defer('job identity unavailable')
    row = SimpleNamespace(**matches[0])
    files = getattr(row, 'files', None)
    if isinstance(files, list):
        row.files = [SimpleNamespace(**f) for f in files if isinstance(f, dict)]
        if len(row.files) != len(files):
            row.files = None
    return row


def request_link(usenet_id, file_id):
    try:
        response = torbox.session.get(torbox.base_url + '/usenet/requestdl',
            params={'token': torbox.api_key, 'usenet_id': usenet_id, 'file_id': file_id}, timeout=20)
        data = response.json() if response.status_code == 200 else {}
        link = data.get('data')
        if data.get('success') is True and isinstance(link, str) and urlsplit(link).scheme in ('http', 'https') and urlsplit(link).netloc:
            return link
        torbox.ui_print('[torbox] Usenet link unavailable: HTTP ' + str(response.status_code))
    except Exception:
        # Request exceptions may embed the credential-bearing URL.
        torbox.ui_print('[torbox] Usenet link request unavailable')
    return None


def files_for(row, wanted):
    """None means uncertain; [] means a verified file list has no wanted video."""
    files = getattr(row, 'files', None)
    if not wanted or getattr(row, 'infected', False) or not isinstance(files, list) or not files:
        return None
    if len({str(getattr(f, 'id', None)) for f in files}) != len(files):
        return None
    selected = []
    for file in files:
        if (not isinstance(getattr(file, 'name', None), str) or not file.name
                or not str(getattr(file, 'id', '')).isdigit()
                or not isinstance(getattr(file, 'size', None), (int, float))
                or not math.isfinite(file.size) or file.size <= 0):
            return None
        if not re.search(r'\.(mkv|mp4|m4v|avi|ts|m2ts|webm)$', file.name, re.I):
            continue
        if getattr(file, 'infected', False) or getattr(file, 'partial', False):
            return None
        if re.search(r'(^|[. /_\\-])sample([. /_\\-]|$)', file.name, re.I) or getattr(file, 'infected', False):
            continue
        basename = re.split(r'[/\\]', file.name)[-1]
        if re.search(r'(^|[. _-])(partial|incomplete|downloading)([. _-]|$)', basename, re.I):
            return None
        matching = [pattern for pattern in wanted if re.search('(?:' + pattern + r')(?!\d)', basename, re.I)]
        if len(matching) > 1:
            return None
        if matching:
            selected.append(file)
    if selected and not all(any(re.search('(?:' + pattern + r')(?!\d)', re.split(r'[/\\]', file.name)[-1], re.I)
                               for file in selected) for pattern in wanted):
        return None
    return selected


def download(release, element, wanted):
    owned_name = marker(release, element)
    token = owned_name.split(' ', 1)[0] + ' '
    rows = inventory()
    prefix = request_prefix(element)
    for candidate in rows:
        name = getattr(candidate, 'name', '')
        if not name.startswith(prefix) or name.startswith(token):
            continue
        # Resolve earlier work for this request even if its indexer result is
        # gone or a different release now sorts first. Completed lower-quality
        # copies do not prevent a genuine upgrade.
        earlier = detail(candidate.id)
        if getattr(earlier, 'name', '') != name:
            defer('owned job identity changed')
        if getattr(earlier, 'download_state', '') in ('failed', 'error', 'cancelled', 'canceled'):
            continue
        if (getattr(earlier, 'download_finished', False) is not True
                or getattr(earlier, 'download_present', False) is not True):
            defer('earlier job for this request is still pending')
        selected = files_for(earlier, wanted)
        if selected is None:
            defer('earlier job file coverage uncertain')
        if selected:
            original = name[len(prefix) + 17:]
            recovered = torbox.releases.release('', 'usenet', original, [], 0, [])
            recovered.cached = ['TB']
            recovered.bitrate = 0
            if not original or marker(recovered, element) != name:
                defer('provider changed stored release identity')
            if finish(recovered, earlier, selected, wanted, element):
                release.__dict__.update(recovered.__dict__)
                return True
    matches = [r for r in rows if getattr(r, 'name', '').startswith(token) or getattr(r, 'name', '') == release.title]
    if len(matches) > 1:
        defer('multiple copies of the same release')
    if matches:
        row = matches[0]
    else:
        try:
            nzb = torbox.session.get(str(release.download[0]), timeout=20)
            if nzb.status_code != 200 or len(nzb.content) < 100:
                torbox.ui_print('[torbox] Usenet NZB unavailable for ' + release.title)
                return False
            response = torbox.requests.post(torbox.base_url + '/usenet/createusenetdownload',
                headers={'Authorization': 'Bearer ' + torbox.api_key},
                files={'file': (hashlib.sha256(nzb.content).hexdigest() + '.nzb', nzb.content, 'application/x-nzb')},
                data={'name': owned_name}, timeout=20)
            if response.status_code != 200:
                defer('submission response uncertain')
            data = response.json()
            if data.get('success') is not True:
                defer('submission not acknowledged')
            number = data.get('data', {}).get('usenetdownload_id')
            if not str(number).isdigit():
                defer('submission identity unavailable')
            row = SimpleNamespace(id=int(number))
        except debrid.Deferred:
            inventory()
            raise
        except Exception:
            # A timed-out submission can have committed; never blindly retry.
            inventory()
            defer('submission unavailable; next attempt will reconcile provider inventory')
    expected_id = row.id
    row = detail(expected_id)
    if (str(getattr(row, 'id', '')) != str(expected_id)
            or not (getattr(row, 'name', '').startswith(token) or getattr(row, 'name', '') == release.title)):
        defer('job identity unavailable')
    if getattr(row, 'download_state', '') in ('failed', 'error', 'cancelled', 'canceled'):
        # Keep provider failure records as restart-safe tombstones rather than
        # deleting and submitting the same broken NZB every poll.
        torbox.ui_print('[torbox] skipping failed Usenet release: ' + release.title)
        return False
    if getattr(row, 'download_finished', False) is not True or getattr(row, 'download_present', False) is not True:
        defer('job pending or not accessible')
    selected = files_for(row, wanted)
    if selected is None:
        defer('file inventory incomplete or partial episode coverage')
    if not selected:
        torbox.ui_print('[torbox] no matching video in existing Usenet job; retained for inspection')
        return False
    return finish(release, row, selected, wanted, element)


def finish(release, row, selected, wanted, element):
    staged = copy.deepcopy(release)
    patterns = [(pattern, re.compile('(?:' + pattern + r')(?!\d)', re.I)) for pattern in wanted]
    staged.files = [torbox.file(f.id, f.name, f.size, patterns, []) for f in selected]
    for record, original in zip(staged.files, selected):
        basename = re.split(r'[/\\]', original.name)[-1]
        record.match = next(pattern for pattern, compiled in patterns if compiled.search(basename))
    staged.wanted = len(staged.files)
    staged.size = sum(f.size for f in staged.files)
    from content import quality
    if hasattr(element, 'version') and not quality.matches(staged, element.version):
        torbox.ui_print('[torbox] existing Usenet files do not meet this version; retained')
        return False
    if getattr(element, '_monitor_managed', False):
        from content.classes import media
        from content.services.plex import current_library
        comparison = copy.deepcopy(element.version)
        comparison.rules = [r for r in comparison.rules if r[0] != 'cache status']
        existing = [r for r in quality.existing(element, current_library, media.session_releases)
                    if quality.matches(r, comparison)]
        ranked = copy.deepcopy(staged)
        ranked.wanted = ranked.unwanted = 0
        comparisons = copy.deepcopy(existing)
        for old in comparisons:
            old.wanted = old.unwanted = 0
        if comparisons and not all(quality.strictly_better(ranked, old, comparison) for old in comparisons):
            return False
    links = [request_link(row.id, file.id) for file in selected]
    if not all(links):
        defer('wanted video link unavailable')
    # Keep actual per-file facts for session coverage and duplicate suppression.
    staged.download = links
    release.__dict__.update(staged.__dict__)
    torbox.ui_print('[torbox] ready Usenet release: ' + release.title + ' (job ' + str(row.id) + ')')
    return True