#import modules
from base import *
from ui.ui_print import *
import releases

name = "Newznab"
session = custom_session()
# indexers sit behind bot protection which rejects the default python-requests user agent
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'})

base_url = 'https://nzbplanet.net/api'
api_key = ''

def setup(cls, new=False):
    from settings import settings_list
    settings = []
    for category, allsettings in settings_list:
        for setting in allsettings:
            if setting.cls == cls:
                settings += [setting]
    if settings == []:
        if not cls.name in active:
            active += [cls.name]
    back = False
    if not new:
        while not back:
            print("0) Back")
            indices = []
            for index, setting in enumerate(settings):
                print(str(index + 1) + ') ' + setting.name)
                indices += [str(index + 1)]
            print()
            if settings == []:
                print("Nothing to edit!")
                print()
                time.sleep(3)
                return
            choice = input("Choose an action: ")
            if choice in indices:
                settings[int(choice) - 1].input()
                if not cls.name in active:
                    active += [cls.name]
                back = True
            elif choice == '0':
                back = True
    else:
        if not cls.name in active:
            active += [cls.name]

def get(url, params=None):
    params = params or {}
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=60)
            if response.status_code == 200:
                return response
        except Exception as e:
            ui_print('[newznab] error: (exception): ' + str(e), debug=ui_settings.debug)
        time.sleep(2)
    ui_print('[newznab] error: could not query indexer after 3 attempts', debug=ui_settings.debug)
    return None

def resolve_title(type, imdb_id):
    # resolve a title (and year) from an imdb id via cinemeta, the same way torrentio does lookups
    try:
        response = get('https://v3-cinemeta.strem.io/meta/' + ('series' if type == 'show' else 'movie') + '/' + imdb_id + '.json')
        if response is not None:
            meta = response.json().get('meta', {})
            title = meta.get('name', '')
            year = str(meta.get('releaseInfo', '') or '')[:4]
            if title != '':
                return title, year
    except Exception as e:
        ui_print('[newznab] error: (title lookup exception): ' + str(e), debug=ui_settings.debug)
    return None, None

def scrape(query, altquery):
    from scraper.services import active
    scraped_releases = []
    if not 'newznab' in active:
        return scraped_releases
    if api_key == '':
        ui_print('[newznab] error: no api key set', debug=ui_settings.debug)
        return scraped_releases
    type = ("show" if regex.search(r'(S[0-9]|complete|S\?[0-9])', altquery, regex.I) else "movie")
    imdb = regex.search(r'(tt[0-9]+)', str(query), regex.I)
    if imdb is None:
        ui_print('[newznab] error: no imdb id found for query: ' + str(query), debug=ui_settings.debug)
        return scraped_releases
    imdb_id = imdb.group()
    title, year = resolve_title(type, imdb_id)
    if title is None:
        ui_print('[newznab] error: could not resolve a title for imdb id: ' + imdb_id)
        return scraped_releases
    params = {'apikey': api_key, 'extended': '1'}
    if type == 'show':
        s = (regex.search(r'(?<=S)([0-9]+)', altquery, regex.I).group()
             if regex.search(r'(?<=S)([0-9]+)', altquery, regex.I) else None)
        e = (regex.search(r'(?<=E)([0-9]+)', altquery, regex.I).group()
             if regex.search(r'(?<=E)([0-9]+)', altquery, regex.I) else None)
        if s == None or int(s) == 0:
            s = 1
        if e == None or int(e) == 0:
            e = 1
        params['t'] = 'tvsearch'
        params['q'] = title + ' s' + '{:02d}'.format(int(s)) + 'e' + '{:02d}'.format(int(e))
    else:
        params['t'] = 'search'
        params['q'] = title + ' ' + year if year else title
    response = get(base_url, params)
    if response is None:
        return scraped_releases
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(response.content)
        for item in root.findall('.//item'):
            try:
                r_title = item.findtext('title') or ''
                r_link = item.findtext('link') or ''
                r_size = 0
                for attr in item.findall('attr'):
                    if attr.get('name') == 'size':
                        r_size = float(attr.get('value') or 0)
                if r_size == 0:
                    enclosure = item.find('enclosure')
                    if enclosure is not None:
                        r_size = float(enclosure.get('length') or 0)
                if r_title == '' or r_link == '':
                    continue
                scraped_releases += [
                    releases.release('[newznab]', 'usenet', r_title, [], r_size / 1000000000, [r_link], seeders=0)]
            except:
                continue
    except Exception as e:
        ui_print('[newznab] error: (parse exception): ' + str(e), debug=ui_settings.debug)
    return scraped_releases