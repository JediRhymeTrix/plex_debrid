#import modules
from base import *
from ui.ui_print import *
import releases

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
        session.post(base_url + '/torrents/controltorrent', headers=headers, json={'operation': 'delete', 'torrent_id': torrent_id}, timeout=60)
    except Exception as e:
        ui_print("[torbox] error: (delete exception): " + str(e), debug=ui_settings.debug)
    return None

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

# get a usenet download from the torbox usenet list by its id. returns None if it could not be found.
def get_usenet(usenet_id, retries=5, wait=2):
    for _ in range(retries):
        response = get(base_url + '/usenet/mylist?id=' + str(usenet_id) + '&bypass_cache=true')
        if response is not None and getattr(response, 'success', False) and response.data is not None:
            data = response.data
            if isinstance(data, list):
                for row in data:
                    if int(getattr(row, 'id', 0)) == int(usenet_id):
                        return row
            else:
                return data
        time.sleep(wait)
    return None

# get a direct download link for a file of a usenet download
def requestdl_usenet(usenet_id, file_id):
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36','Authorization': 'Bearer ' + api_key}
    try:
        response = session.get(base_url + '/usenet/requestdl?usenet_id=' + str(usenet_id) + '&file_id=' + str(file_id), headers=headers, timeout=60)
        if response.status_code == 200:
            data = json.loads(response.content)
            if data.get('success') and data.get('data'):
                return data['data']
    except Exception as e:
        ui_print("[torbox] error: (usenet requestdl exception): " + str(e), debug=ui_settings.debug)
    return None

# delete a usenet download from the torbox usenet list
def delete_usenet(usenet_id):
    try:
        requests.post(base_url + '/usenet/controlusenetdownload', headers={'Authorization': 'Bearer ' + api_key, 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}, json={'operation': 'delete', 'usenet_id': usenet_id}, timeout=60)
    except Exception as e:
        ui_print("[torbox] error: (usenet delete exception): " + str(e), debug=ui_settings.debug)

# add an nzb release to torbox as a usenet download. returns True if the release was added or downloaded.
def download_usenet(release, element, wanted):
    import debrid as db
    usenet_id = None
    try:
        nzb_url = str(release.download[0])
        response = session.get(nzb_url, timeout=60)
        if response.status_code != 200 or len(response.content) < 100:
            ui_print('[torbox] error: could not retrieve nzb for release: ' + release.title, ui_settings.debug)
            return False
        files = {'file': (releases.rename(release.title) + '.nzb', response.content, 'application/x-nzb')}
        response = requests.post(base_url + '/usenet/createusenetdownload', headers={'Authorization': 'Bearer ' + api_key, 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36'}, files=files, data={'name': release.title}, timeout=60)
        if response.status_code != 200:
            ui_print('[torbox] error: could not add nzb for release: ' + release.title, ui_settings.debug)
            return False
        data = json.loads(response.content)
        if not data.get('success'):
            ui_print('[torbox] error: could not add nzb for release: ' + release.title + ' - ' + str(data.get('detail')), ui_settings.debug)
            return False
        usenet_id = data.get('data', {}).get('usenetdownload_id')
    except Exception as e:
        ui_print('[torbox] error: (usenet add exception): ' + str(e), ui_settings.debug)
        return False
    if usenet_id is None:
        return False
    # wait for torbox to fetch the release from usenet, then hand out direct links for the wanted files
    wanted_patterns = list(zip(wanted, [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in wanted]))
    unwanted_patterns = list(zip(releases.sort.unwanted, [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in releases.sort.unwanted]))
    for _ in range(12):
        time.sleep(5)
        usenet = get_usenet(usenet_id)
        if usenet is None or not bool(getattr(usenet, 'download_finished', False)):
            continue
        # the release is complete on torbox - collect links for the wanted files
        release.download = []
        for file_ in getattr(usenet, 'files', []):
            name = getattr(file_, 'name', '') or ''
            fid = getattr(file_, 'id', 0)
            wanted = False
            unwanted = False
            for key, wanted_pattern in wanted_patterns:
                if wanted_pattern.search(name):
                    wanted = True
                    break
            if not wanted:
                for key, unwanted_pattern in unwanted_patterns:
                    if unwanted_pattern.search(name) or name.endswith('.exe') or name.endswith('.txt'):
                        unwanted = True
                        break
            if wanted or not unwanted:
                link = requestdl_usenet(usenet_id, fid)
                if link is not None:
                    release.download += [link]
        if len(release.download) > 0:
            ui_print('[torbox] adding usenet release: ' + release.title)
            return True
        ui_print('[torbox] error: usenet release: "' + release.title + '" does not contain any of the wanted files.', ui_settings.debug)
        delete_usenet(usenet_id)
        return False
    # torbox is still fetching the release - treat it like an uncached torrent that continues downloading on torbox
    if hasattr(element, 'version'):
        db.downloading += [element.query() + ' [' + element.version.name + ']']
    ui_print('[torbox] adding uncached usenet release: ' + release.title)
    return True

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

# (required) Download Function.
def download(element, stream=True, query='', force=False):
    cached = element.Releases
    if query == '':
        query = element.deviation()
    wanted = [query]
    if not isinstance(element, releases.release):
        wanted = element.files()
    for release in cached[:]:
        # if release matches query
        if regex.match(query, release.title,regex.I) or force:
            if release.type == 'usenet':
                # nzb release: add it to torbox as a usenet download
                return download_usenet(release, element, wanted)
            # post magnet to torbox
            try:
                response = post(base_url + '/torrents/createtorrent', {'magnet': str(release.download[0]), 'seed': 2, 'allow_zip': False, 'name': release.title})
                torrent_id = response.data.torrent_id
            except:
                ui_print('[torbox] error: could not add magnet for release: ' + release.title, ui_settings.debug)
                continue
            if stream:
                torrent = get_torrent(torrent_id)
                if torrent is None:
                    ui_print('[torbox] error: could not get torrent info for release: ' + release.title, ui_settings.debug)
                    delete(torrent_id)
                    continue
                # if the torrent is cached on torbox, all of its files are instantly available for direct download
                if torrent.download_state in ["cached","completed"] and torrent.download_present:
                    # check if the release contains any of the wanted files
                    unwanted = releases.sort.unwanted
                    wanted_patterns = list(zip(wanted, [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in wanted]))
                    unwanted_patterns = list(zip(unwanted, [regex.compile(r'(' + key + ')', regex.IGNORECASE) for key in unwanted]))
                    version_files = []
                    for file_ in torrent.files or []:
                        version_files.append(file(file_.id, file_.name, file_.size, wanted_patterns, unwanted_patterns))
                    torrent_version = version(version_files)
                    if len(torrent_version.files) == 0:
                        ui_print('[torbox] error: no files could be selected for release: ' + release.title, ui_settings.debug)
                        delete(torrent_id)
                        continue
                    if torrent_version.wanted > 0 or force:
                        release.files = torrent_version.files
                        release.download = []
                        for file_ in torrent_version.files:
                            if force or file_.wanted:
                                link = requestdl(torrent_id, file_.id)
                                if link is not None:
                                    release.download += [link]
                        ui_print('[torbox] adding cached release: ' + release.title)
                        if not torrent.name == "":
                            release.title = torrent.name
                        return True
                    else:
                        ui_print('[torbox] error: release: "' + release.title + '" does not contain any of the wanted files.', ui_settings.debug)
                        delete(torrent_id)
                        continue
                else:
                    # torrent is not cached. if uncached downloads are allowed by the version rules, add it to torbox for downloading.
                    import debrid as db
                    debrid_uncached = False
                    if db.uncached == 'true':
                        debrid_uncached = True
                        if hasattr(element,"version"):
                            for i,rule in enumerate(element.version.rules):
                                if (rule[0] == "cache status") and (rule[1] == 'requirement' or rule[1] == 'preference') and (rule[2] == "cached"):
                                    debrid_uncached = False
                    if debrid_uncached:
                        db.downloading += [element.query() + ' [' + element.version.name + ']']
                        ui_print('[torbox] adding uncached release: ' + release.title)
                        return True
                    else:
                        ui_print('[torbox] error: release: "' + release.title + '" is not cached on torbox.', ui_settings.debug)
                        delete(torrent_id)
                        continue
            else:
                ui_print('[torbox] adding uncached release: ' + release.title)
                return True
        else:
            ui_print('[torbox] error: rejecting release: "' + release.title + '" because it doesnt match the allowed deviation', ui_settings.debug)
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
