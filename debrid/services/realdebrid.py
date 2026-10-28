#import modules
from base import *
from ui.ui_print import *
import releases
import math

# (required) Name of the Debrid service
name = "Real Debrid"
short = "RD"
# (required) Authentification of the Debrid service, can be oauth aswell. Create a setting for the required variables in the ui.settings_list. For an oauth example check the trakt authentification.
api_key = ""
# Define Variables
session = requests.Session()
request_timeout = (10, 30)
errors = [
    [202," action already done"],
    [400," bad Request (see error message)"],
    [403," permission denied (infringing torrent or account locked or not premium)"],
    [503," service unavailable (see error message)"],
    [404," wrong parameter (invalid file id(s)) / unknown ressource (invalid id)"],
    ]
def setup(cls, new=False):
    from debrid.services import setup
    setup(cls,new)

# Error Log
def logerror(response):
    if not response.status_code in [200,201,204]:
        desc = ""
        for error in errors:
            if response.status_code == error[0]:
                desc = error[1]
        ui_print("[realdebrid] error: (" + str(response.status_code) + desc + ") " + str(response.content), debug=ui_settings.debug)
    if response.status_code == 401:
        ui_print("[realdebrid] error: (401 unauthorized): realdebrid api key does not seem to work. check your realdebrid settings.")
    if response.status_code == 403:
        if b'disabled_endpoint' in response.content:
            ui_print("[realdebrid] note: (403): real-debrid has disabled this endpoint (error_code 37) - skipping.")
        else:
            ui_print("[realdebrid] error: (403 unauthorized): You may have attempted to add an infringing torrent or your realdebrid account is locked or you dont have premium.")

# Get Function
def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36','authorization': 'Bearer ' + api_key}
    response = None
    try:
        response = session.get(url, headers=headers, timeout=request_timeout)
        logerror(response)
        if response.status_code >= 500 or response.status_code == 429:
            return None
        if response.status_code >= 400:
            try:
                data = json.loads(response.content)
            except (ValueError, TypeError):
                data = {}
            return SimpleNamespace(error=data.get('error') or 'http_refusal',
                                   error_code=data.get('error_code') or response.status_code)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        ui_print("[realdebrid] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response

# Post Function
def post(url, data):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36','authorization': 'Bearer ' + api_key}
    response = None
    try:
        response = session.post(url, headers=headers, data=data, timeout=request_timeout)
        logerror(response)
        if response.status_code >= 500 or response.status_code == 429:
            return None
        if response.status_code >= 400:
            try:
                data = json.loads(response.content)
            except (ValueError, TypeError):
                data = {}
            return SimpleNamespace(error=data.get('error') or 'http_refusal',
                                   error_code=data.get('error_code') or response.status_code)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        if hasattr(response,"status_code"):
            if response.status_code >= 300:
                ui_print("[realdebrid] error: (json exception): " + str(e), debug=ui_settings.debug)
        else:
            ui_print("[realdebrid] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response

# Delete Function
def delete(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36','authorization': 'Bearer ' + api_key}
    try:
        response = requests.delete(url, headers=headers, timeout=request_timeout)
        return response.status_code in (200, 202, 204, 404)
        # time.sleep(1)
    except Exception as e:
        ui_print("[realdebrid] error: (delete exception): " + str(e), debug=ui_settings.debug)
    return False

# Object classes
class file:
    def __init__(self, id, name, size, wanted_list, unwanted_list):
        self.id = id
        self.name = name
        self.size = size / 1000000000
        self.match = ''
        wanted = False
        unwanted = False
        for key, wanted_pattern in wanted_list:
            if wanted_pattern.search(self.name):
                wanted = True
                self.match = key
                break

        if not wanted:
            for key, unwanted_pattern in unwanted_list:
                if unwanted_pattern.search(self.name) or self.name.endswith('.exe') or self.name.endswith('.txt'):
                    unwanted = True
                    break

        self.wanted = wanted
        self.unwanted = unwanted

    def __eq__(self, other):
        return self.id == other.id

class version:
    def __init__(self, files):
        self.files = files
        self.needed = 0
        self.wanted = 0
        self.unwanted = 0
        self.size = 0
        for file in self.files:
            self.size += file.size
            if file.wanted:
                self.wanted += 1
            if file.unwanted:
                self.unwanted += 1

def _defer(reason):
    from debrid import Deferred
    raise Deferred('Real Debrid: ' + reason)


def _refused(response):
    return bool(getattr(response, 'error', None) or getattr(response, 'error_code', None))


def _inventory():
    """Bounded fresh inventory; absence must be proved before creating a job."""
    inventory = []
    for page in range(1, 11):
        rows = get('https://api.real-debrid.com/rest/1.0/torrents?limit=100&page=' + str(page))
        if _refused(rows):
            return False
        if not isinstance(rows, list):
            _defer('torrent inventory unavailable')
        if any(not getattr(row, 'id', None) or not isinstance(getattr(row, 'hash', None), str) for row in rows):
            _defer('torrent inventory malformed')
        inventory.extend(rows)
        if len(rows) < 100:
            return inventory
    _defer('torrent inventory exceeds safety bound')


def _existing(element, release):
    from debrid import provider_inventory
    rows = provider_inventory(element, short, _inventory)
    if rows is False:
        return False
    matches = [row for row in rows if row.hash.lower() == release.hash.lower()]
    if not matches:
        return None
    # Prefer an already complete copy, never change/delete an old job.
    matches.sort(key=lambda row: getattr(row, 'status', '') != 'downloaded')
    return str(matches[0].id)


def _cleanup(torrent_id):
    if not delete('https://api.real-debrid.com/rest/1.0/torrents/delete/' + torrent_id):
        _defer('cached probe cleanup uncertain')


def _link(value):
    return isinstance(value, str) and value.startswith(('https://', 'http://'))


def _complete(element, release, response, wanted, force):
    """True/False/None = playable / no wanted files / incomplete metadata or links."""
    raw_files = getattr(response, 'files', None)
    links = getattr(response, 'links', None)
    if not isinstance(raw_files, list) or not raw_files or not isinstance(links, list):
        return None
    wanted_patterns = [(key, regex.compile('(' + key + ')', regex.I)) for key in wanted]
    unwanted_patterns = [(key, regex.compile('(' + key + ')', regex.I)) for key in releases.sort.unwanted]
    selected = []
    try:
        for raw in raw_files:
            if (not isinstance(getattr(raw, 'path', None), str) or not raw.path
                    or not isinstance(getattr(raw, 'bytes', None), (int, float))
                    or not math.isfinite(raw.bytes) or raw.bytes <= 0
                    or getattr(raw, 'id', None) is None):
                return None
            if getattr(raw, 'selected', 0) == 1:
                selected.append(file(str(raw.id), raw.path, raw.bytes, wanted_patterns, unwanted_patterns))
    except (AttributeError, TypeError, ValueError):
        return None
    if (not selected or len({f.id for f in selected}) != len(selected)
            or len(selected) != len(links) or not all(_link(link) for link in links)):
        return None
    safe_ids = {f.id for f in selected}
    if not force:
        # Share the strict actual-video validator, not the Usenet lifecycle.
        # Normalize raw bytes before conversion to the legacy GB file objects.
        from debrid.services.torbox_usenet import files_for
        normalized = [SimpleNamespace(id=raw.id, name=raw.path, size=raw.bytes,
                                      infected=getattr(raw, 'infected', False),
                                      partial=getattr(raw, 'partial', False))
                      for raw in raw_files if getattr(raw, 'selected', 0) == 1]
        safe = files_for(SimpleNamespace(files=normalized, infected=getattr(response, 'infected', False)), wanted)
        if safe is None:
            return None
        safe_ids = {str(f.id) for f in safe}
        for f in selected:
            if f.id in safe_ids:
                basename = regex.split(r'[/\\]', f.name)[-1]
                f.match = next(key for key in wanted if regex.search('(?:' + key + r')(?!\d)', basename, regex.I))
                f.wanted = True
    needed = [(f, link) for f, link in zip(selected, links) if f.id in safe_ids]
    if not needed:
        return False
    staged = copy.deepcopy(release)
    staged.files = [f for f, _ in needed]
    staged.size = sum(f.size for f in staged.files)
    staged.wanted = sum(f.wanted for f in staged.files)
    staged.unwanted = sum(f.unwanted for f in staged.files)
    # Actual ready state is evidence; routing itself never adds cached flags.
    if 'RD' not in staged.cached:
        staged.cached.append('RD')
    from content import quality
    if not quality.confirmed(element, staged):
        return False
    direct = []
    for f, link in needed:
        result = post('https://api.real-debrid.com/rest/1.0/unrestrict/link', {'link': link})
        if _refused(result):
            return 'refused'
        if not _link(getattr(result, 'download', None)):
            return None
        direct.append(result.download)
    staged.download = direct
    # Retain discovery title/quality provenance. Renamed provider containers
    # are not evidence of encoding, resolution, presentation or source tags.
    release.__dict__.update(staged.__dict__)
    return True


# (required) Download Function.
def download(element, stream=True, query='', force=False):
    query = query or element.deviation()
    wanted = [query] if isinstance(element, releases.release) else element.files()
    for release in element.Releases[:]:
        if release.type == 'usenet' or not (force or regex.match(query, release.title, regex.I)):
            continue
        if not regex.fullmatch(r'[a-fA-F0-9]{40}', release.hash):
            continue
        torrent_id = _existing(element, release)
        if torrent_id is False:
            continue
        created = False
        if torrent_id is None:
            # Disabled instantAvailability does not justify an add/select probe:
            # that starts an uncached download. Unknown is inventory-only here.
            if stream and 'RD' not in release.cached:
                continue
            if not release.download:
                continue
            from debrid import invalidate_inventory
            invalidate_inventory(element, short)
            added = post('https://api.real-debrid.com/rest/1.0/torrents/addMagnet', {'magnet': str(release.download[0])})
            if _refused(added):
                continue  # Explicit refusal must permit TorBox fallback.
            if not getattr(added, 'id', None):
                _defer('submission outcome uncertain')
            torrent_id = str(added.id)
            created = True
            cached_ids = 'all'
            for cached_version in release.files:
                if getattr(cached_version, 'files', None):
                    cached_ids = ','.join(str(f.id) for f in cached_version.files)
                    break
            selected = post('https://api.real-debrid.com/rest/1.0/torrents/selectFiles/' + torrent_id,
                            {'files': cached_ids if stream else 'all'})
            if _refused(selected):
                # No fresh identity/state proof after a refusal. In particular,
                # create APIs may deduplicate; never delete an older ready job.
                continue
        response = get('https://api.real-debrid.com/rest/1.0/torrents/info/' + torrent_id)
        if response is not None and not _refused(response):
            if (str(getattr(response, 'id', '')) != torrent_id
                    or str(getattr(response, 'hash', '')).lower() != release.hash.lower()):
                _defer('torrent identity changed')
        status = getattr(response, 'status', '')
        if _refused(response):
            continue
        if status in ('error', 'magnet_error', 'virus', 'dead', 'failed'):
            if created:
                _cleanup(torrent_id)
            continue
        if status != 'downloaded':
            if created and stream and status in ('queued', 'magnet_conversion', 'magnet_convesion',
                                                  'waiting_files_selection', 'downloading', 'compressing', 'uploading'):
                _cleanup(torrent_id)
                continue
            _defer('torrent pending or status unavailable')
        completed = _complete(element, release, response, wanted, force)
        if completed is True:
            ui_print('[realdebrid] adding cached release: ' + release.title)
            return True
        if completed is None:
            # Ready wanted jobs are not bad cache probes just because the link
            # API is temporarily unavailable. Keep them for the next retry.
            _defer('torrent files or links unavailable')
        if created and completed is False:
            _cleanup(torrent_id)
    return False

# (required) Check Function
def check(element, force=False):
    if force:
        wanted = ['.*']
    else:
        wanted = element.files()
    unwanted = releases.sort.unwanted
    wanted_patterns = list(zip(wanted, [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in wanted]))
    unwanted_patterns = list(zip(unwanted, [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in unwanted]))

    hashes = []
    for release in element.Releases[:]:
        if release.type == 'usenet':
            # usenet releases are nzb files which real debrid cannot download - leave them for other services
            continue
        if len(release.hash) == 40:
            hashes += [release.hash]
            if not hasattr(release, 'provider_cache'):
                release.provider_cache = {}
            release.provider_cache['RD'] = 'unknown'
            # Previous positives are not evidence after a failed/disabled check.
            release.cached = [provider for provider in release.cached if provider != 'RD']
        else:
            ui_print("[realdebrid] error (missing torrent hash): ignoring release '" + release.title + "' ",ui_settings.debug)
            element.Releases.remove(release)
    if len(hashes) > 0:
        response = get('https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/' + '/'.join(hashes))
        ui_print("[realdebrid] checking and sorting all release files ...", ui_settings.debug)
        for release in element.Releases:
            if release.type == 'usenet':
                continue
            release_hash = release.hash.lower()
            if hasattr(response, release_hash):
                response_attr = getattr(response, release_hash)
                if hasattr(response_attr, 'rd'):
                    rd_attr = response_attr.rd
                    if not isinstance(rd_attr, list):
                        continue
                    release.provider_cache['RD'] = 'uncached'
                    if len(rd_attr) > 0:
                        versions = []
                        for cashed_version in rd_attr:
                            version_files = []
                            for file_ in cashed_version.__dict__:
                                file_attr = getattr(cashed_version, file_)
                                debrid_file = file(file_, file_attr.filename, file_attr.filesize, wanted_patterns, unwanted_patterns)
                                version_files.append(debrid_file)
                            versions += [version(version_files), ]
                        release.files = versions
                        # select cached version that has the most needed, most wanted, least unwanted files and most files overall
                        release.files.sort(key=lambda x: len(x.files), reverse=True)
                        release.files.sort(key=lambda x: x.wanted, reverse=True)
                        release.files.sort(key=lambda x: x.unwanted, reverse=False)
                        release.wanted = release.files[0].wanted
                        release.unwanted = release.files[0].unwanted
                        release.size = release.files[0].size
                        release.cached += ['RD']
                        release.provider_cache['RD'] = 'cached'
                        continue
        ui_print("done",ui_settings.debug)
