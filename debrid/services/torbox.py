#import modules
from base import *
from ui.ui_print import *
import releases
import math

# (required) Name of the Debrid service
name = "TorBox"
short = "TB"
# (required) Authentification of the Debrid service, can be oauth aswell. Create a setting for the required variables in the ui.settings_list. For an oauth example check the trakt authentification.
api_key = ""
# Define Variables
session = requests.Session()
base_url = 'https://api.torbox.app/v1/api'

def setup(cls, new=False):
    from debrid.services import setup
    setup(cls,new)

# Error Log
def logerror(response):
    if not response.status_code in [200,201]:
        ui_print("[torbox] error: (" + str(response.status_code) + ") " + str(response.content), debug=ui_settings.debug)
    if response.status_code == 401:
        ui_print("[torbox] error: (401 unauthorized): torbox api key does not seem to work. check your torbox settings.")

# Get Function
def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36',
        'Authorization': 'Bearer ' + api_key}
    response = None
    try:
        response = session.get(url, headers=headers, timeout=60)
        logerror(response)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        ui_print("[torbox] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response

# Post Function
def post(url, data):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36',
        'Authorization': 'Bearer ' + api_key}
    response = None
    try:
        response = session.post(url, headers=headers, data=data, timeout=60)
        logerror(response)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        if hasattr(response,"status_code"):
            if response.status_code >= 300:
                ui_print("[torbox] error: (json exception): " + str(e), debug=ui_settings.debug)
        else:
            ui_print("[torbox] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response

# Delete Function
def delete(torrent_id):
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36','Authorization': 'Bearer ' + api_key}
    try:
        response = session.post(base_url + '/torrents/controltorrent', headers=headers, json={'operation': 'delete', 'torrent_id': torrent_id}, timeout=60)
        return response.status_code == 200 and response.json().get('success') is True
    except Exception as e:
        ui_print("[torbox] error: (delete exception): " + str(e), debug=ui_settings.debug)
    return False

# get a direct download link for a file of a torrent
def requestdl(torrent_id, file_id):
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36','Authorization': 'Bearer ' + api_key}
    try:
        response = session.get(base_url + '/torrents/requestdl?torrent_id=' + str(torrent_id) + '&file_id=' + str(file_id) + '&token=' + api_key, headers=headers, timeout=60)
        if response.status_code == 200:
            data = json.loads(response.content)
            if data.get('success') and data.get('data'):
                return data['data']
    except Exception as e:
        ui_print("[torbox] error: (requestdl exception): " + str(e), debug=ui_settings.debug)
    return None

# get a torrent from the torbox torrent list by its id. returns None if it could not be found.
def get_torrent(torrent_id, retries=5, wait=2):
    for _ in range(retries):
        response = get(base_url + '/torrents/mylist?id=' + str(torrent_id))
        if response is not None and getattr(response, 'success', False) and response.data is not None:
            return response.data
        time.sleep(wait)
    return None

# add an nzb release to torbox as a usenet download. returns True if the release was added or downloaded.
def download_usenet(release, element, wanted):
    from debrid.services.torbox_usenet import download
    return download(release, element, wanted)

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
    raise Deferred('TorBox torrent: ' + reason)


def _inventory():
    """Reuse provider jobs, including pending ones; never delete pre-existing work."""
    inventory = []
    for offset in range(0, 10000, 1000):
        response = get(base_url + '/torrents/mylist?bypass_cache=true&limit=1000&offset=' + str(offset))
        if response is not None and getattr(response, 'success', None) is False:
            return False
        rows = getattr(response, 'data', None)
        if not isinstance(rows, list):
            _defer('inventory unavailable')
        if any(getattr(row, 'id', None) is None or not isinstance(getattr(row, 'hash', None), str) for row in rows):
            _defer('inventory malformed')
        inventory.extend(rows)
        if len(rows) < 1000:
            return inventory
    _defer('inventory exceeds safety bound')


def _existing(element, release):
    from debrid import provider_inventory
    rows = provider_inventory(element, short, _inventory)
    if rows is False:
        return False
    matches = [row for row in rows if row.hash.lower() == release.hash.lower()]
    if not matches:
        return None
    matches.sort(key=lambda row: getattr(row, 'download_state', '') not in ('cached', 'completed'))
    return matches[0].id


def _cleanup(torrent_id):
    if not delete(torrent_id):
        _defer('cached probe cleanup uncertain')


def _complete(element, release, torrent, wanted, force):
    raw_files = getattr(torrent, 'files', None)
    if not isinstance(raw_files, list):
        _defer('file metadata unavailable')
    wanted_patterns = [(key, regex.compile('(' + key + ')', regex.I)) for key in wanted]
    unwanted_patterns = [(key, regex.compile('(' + key + ')', regex.I)) for key in releases.sort.unwanted]
    if any(not isinstance(getattr(f, 'name', None), str) or not f.name
           or getattr(f, 'id', None) is None
           or not isinstance(getattr(f, 'size', None), (int, float))
           or not math.isfinite(f.size) or f.size <= 0 for f in raw_files):
        _defer('file metadata incomplete')
    if len({str(f.id) for f in raw_files}) != len(raw_files):
        _defer('file identity ambiguous')
    try:
        files = [file(f.id, f.name, f.size, wanted_patterns, unwanted_patterns) for f in raw_files]
    except (AttributeError, TypeError, ValueError):
        _defer('file metadata incomplete')
    safe_ids = {str(f.id) for f in files}
    if not force:
        from debrid.services.torbox_usenet import files_for
        safe = files_for(torrent, wanted)
        if safe is None:
            _defer('wanted video file metadata or coverage uncertain')
        safe_ids = {str(f.id) for f in safe}
        for f in files:
            if str(f.id) in safe_ids:
                basename = regex.split(r'[/\\]', f.name)[-1]
                f.match = next(key for key in wanted if regex.search('(?:' + key + r')(?!\d)', basename, regex.I))
                f.wanted = True
    selected = [f for f in files if str(f.id) in safe_ids]
    if not selected:
        return False
    staged = copy.deepcopy(release)
    staged.files = selected
    staged.size = sum(f.size for f in selected)
    staged.wanted = sum(f.wanted for f in selected)
    staged.unwanted = sum(f.unwanted for f in selected)
    if 'TB' not in staged.cached:
        staged.cached.append('TB')
    from content import quality
    if not quality.confirmed(element, staged):
        return False
    links = []
    for f in selected:
        link = requestdl(torrent.id, f.id)
        if not isinstance(link, str) or not link.startswith(('https://', 'http://')):
            # A link failure does not prove these files are unwanted. Retain
            # even a newly created needed job for a later retry, not deletion.
            _defer('wanted file link unavailable')
        links.append(link)
    staged.download = links
    release.__dict__.update(staged.__dict__)
    return True


# (required) Download Function.
def download(element, stream=True, query='', force=False):
    query = query or element.deviation()
    wanted = [query] if isinstance(element, releases.release) else element.files()
    for release in element.Releases[:]:
        if not (force or regex.match(query, release.title, regex.I)):
            continue
        if release.type == 'usenet':
            return download_usenet(release, element, wanted)
        if not regex.fullmatch(r'[a-fA-F0-9]{40}', release.hash):
            continue
        torrent_id = _existing(element, release)
        if torrent_id is False:
            continue
        created = False
        if torrent_id is None:
            if stream and 'TB' not in release.cached:
                continue  # Unknown cache: inventory-only, never blind uncached add.
            if not release.download:
                continue
            from debrid import invalidate_inventory
            invalidate_inventory(element, short)
            response = post(base_url + '/torrents/createtorrent',
                            {'magnet': str(release.download[0]), 'seed': 2, 'allow_zip': False, 'name': release.title})
            if response is not None and getattr(response, 'success', None) is False:
                continue
            torrent_id = getattr(getattr(response, 'data', None), 'torrent_id', None)
            if torrent_id is None:
                _defer('submission outcome uncertain')
            created = True
        torrent = get_torrent(torrent_id, retries=1)
        if torrent is None:
            _defer('torrent info unavailable')
        if (str(getattr(torrent, 'id', '')) != str(torrent_id)
                or str(getattr(torrent, 'hash', '')).lower() != release.hash.lower()):
            _defer('torrent identity changed')
        state = getattr(torrent, 'download_state', '')
        if state in ('failed', 'error', 'cancelled', 'canceled'):
            if created:
                _cleanup(torrent_id)
            continue
        if state not in ('cached', 'completed') or getattr(torrent, 'download_present', False) is not True:
            if created and stream and state in ('queued', 'downloading', 'uploading', 'metaDL', 'checking'):
                _cleanup(torrent_id)
                continue
            _defer('torrent still pending')
        if _complete(element, release, torrent, wanted, force):
            ui_print('[torbox] adding cached release: ' + release.title)
            return True
        if created:
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
            # usenet releases are nzb files which torbox can always fetch - mark them cached for torbox
            if not 'TB' in release.cached:
                release.cached += ['TB']
            continue
        if len(release.hash) == 40:
            hashes += [release.hash]
        else:
            ui_print("[torbox] error (missing torrent hash): ignoring release '" + release.title + "' ",ui_settings.debug)
            element.Releases.remove(release)
    if len(hashes) > 0:
        response = get(base_url + '/torrents/checkcached?hash=' + ','.join(hashes) + '&format=object')
        ui_print("[torbox] checking cache status for scraped releases ...", ui_settings.debug)
        if response is not None and getattr(response, 'success', False) and response.data is not None:
            for release in element.Releases:
                release_hash = release.hash.lower()
                if hasattr(response.data, release_hash):
                    release.cached += ['TB']
        ui_print("done",ui_settings.debug)
