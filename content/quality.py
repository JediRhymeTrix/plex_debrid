"""Per-file version coverage and conservative, side-effect-free cleanup planning."""
import copy
import ntpath
import re
import math
import time

import releases


def active_versions(item, collected=False):
    result = []
    for name, triggers, lang, rules in releases.sort.versions:
        if '\u0336' in name:
            continue
        version = releases.sort.version(name, copy.deepcopy(triggers), lang, copy.deepcopy(rules))
        # Retry limits govern initial acquisition, not already-collected upgrades.
        if collected and isinstance(version.triggers, list):
            version.triggers = [t for t in version.triggers
                                if not (t[0] == 'retries' and t[1] == '<=')]
        if version.applies(item):
            result.append(version)
    return result


def matches(candidate, version, target=False):
    """Every required (and optionally upgrade) rule must match ONE file."""
    weights = ('requirement', 'upgrade') if target else ('requirement',)
    for rule in version.rules:
        if rule[1] not in weights:
            continue
        # Source and swarm availability are acquisition facts, not properties
        # retained by Plex. Never re-grab an existing file for unknown provenance.
        if getattr(candidate, 'in_library', False) and rule[0] in ('source', 'seeders', 'cache status'):
            continue
        cls = next((c for c in releases.sort.version.rule.__subclasses__()
                    if c.name == rule[0]), None)
        if cls is None:
            return False
        if rule[2] not in cls.operators:
            return False
        attribute = 'cached' if rule[0] == 'cache status' else rule[0]
        if rule[0] not in ('file names', 'file sizes') and not hasattr(candidate, attribute):
            return False
        if rule[0] == 'title' and rule[2] in ('include', 'exclude'):
            try:
                releases.regex.compile(rule[3])
            except Exception:
                return False
        if rule[0] in ('file names', 'file sizes') and not getattr(candidate, 'files', []):
            return False
        if rule[0] in ('size', 'resolution', 'bitrate', 'seeders') and rule[2] in ('==', '>=', '<='):
            try:
                actual, expected = float(getattr(candidate, attribute)), float(rule[3])
                if not math.isfinite(actual) or not math.isfinite(expected):
                    return False
                if not {'==': actual == expected, '>=': actual >= expected, '<=': actual <= expected}[rule[2]]:
                    return False
                continue
            except (TypeError, ValueError, AttributeError):
                return False
        if not cls(rule[0], 'requirement', rule[2], rule[3]).apply([copy.deepcopy(candidate)]):
            return False
    return True


def file_release(media):
    """Represent one Plex media version using the same release/rule classes."""
    if len(getattr(media, 'Part', [])) != 1:
        return None
    part = media.Part[0]
    if not getattr(part, 'file', None):
        return None
    if str(getattr(part, 'exists', True)).lower() in ('false', '0'):
        return None
    try:
        resolution = str(media.videoResolution)
        resolution = '2160' if resolution == '4k' else resolution
        # Release tags often live only in the torrent's parent folder.
        title = (ntpath.basename(ntpath.dirname(part.file)) + '.'
                 + ntpath.basename(part.file)).replace(' ', '.')
        result = releases.release('', 'torrent', '(' + resolution + 'p) ' + title,
                                  [], float(part.size) / 1_000_000_000, [])
        result.resolution = resolution
        result.bitrate = float(getattr(media, 'bitrate', 0) or 0) / 1000
        # Existing playable files are equally available; do not invent a provider.
        result.cached = ['library']
        result.in_library = True
        from types import SimpleNamespace
        result.files = [SimpleNamespace(name=part.file, size=float(part.size) / 1_000_000_000)]
        return result
    except (AttributeError, TypeError, ValueError):
        return None


def library_match(item, library):
    if item.type == 'movie':
        return next((x for x in library if x.type == 'movie' and item == x), None)
    if item.type == 'episode':
        for show in library:
            if show.type != 'show':
                continue
            for season in show.Seasons:
                for episode in season.Episodes:
                    if item == episode:
                        return episode
    return None


def existing(item, library, session):
    found = library_match(item, library)
    key = item.query()
    pending = session.get(key, [])
    pending = [r for r in pending if time.time() - getattr(r, 'grabbed_at', time.time()) < 7200]
    if pending:
        session[key] = pending
    else:
        session.pop(key, None)
    result = []
    if found is not None:
        for media in getattr(found, 'Media', []):
            candidate = file_release(media)
            if candidate is not None:
                result.append(candidate)
    if pending and result:
        # Once a scanned copy covers the targets of a pending grab, Plex owns
        # that record. Do not retain historical targets after manual removal.
        versions = active_versions(item, collected=True)
        acknowledged = all(any(matches(p, v, True) for p in result)
                           for c in pending for v in versions if matches(c, v, True))
        if acknowledged:
            session.pop(key, None)
            pending = []
    return result + [copy.deepcopy(r) for r in pending]


def missing_versions(item, library, downloaded, session):
    candidates = existing(item, library, session)
    collected = library_match(item, library) is not None or bool(candidates)
    versions = active_versions(item, collected=collected)
    has_imax = any(re.search(r'IMAX', c.title, re.I) for c in candidates)
    result = []
    for version in versions:
        # IMAX wins expanded-ratio exclusivity, including after a restart.
        if 'matte' in version.name.lower() and has_imax:
            continue
        if any(matches(c, version, target=True) for c in candidates):
            continue
        found = library_match(item, library)
        if found is not None and not candidates and getattr(found, 'Media', []):
            # Multipart or incomplete Plex facts cannot prove a replacement is
            # needed. Keep them rather than repeatedly reacquire after restart.
            continue
        if any(matches(c, version) for c in candidates):
            rules = copy.deepcopy(version.rules)
            for rule in rules:
                if rule[1] == 'upgrade':
                    rule[1] = 'requirement'
            version = releases.sort.version(version.name + ' upgrade', version.triggers,
                                            version.lang, rules)
        result.append(version)
    return result


def presentation(title):
    """Never automatically merge different presentations, edits or language tags."""
    patterns = (r'IMAX', r'\b3D\b', r'OPEN[ ._-]?MATTE|\bMATTE\b',
                r'EXTENDED', r'DIRECTOR.?S?[ ._-]?(?:CUT)?', r'THEATRICAL',
                r'UNRATED', r'UNCUT', r'REMASTER(?:ED)?', r'CRITERION',
                r'ANNIVERSARY', r'COLLECTOR.?S?', r'SPECIAL[ ._-]?EDITION',
                r'FINAL[ ._-]?CUT', r'ALTERNATE[ ._-]?(?:CUT|ENDING)',
                r'BLACK[ ._-]?(?:AND[ ._-]?)?WHITE|MONOCHROME',
                r'\bB[&.]W\b|COLORI[ZS]ED', r'\b(?:48|50|60|120)FPS\b|\bHFR\b',
                r'\b(?:UPSCALE|UPSCALED|AI|RIFE)\b',
                r'\b(?:DUAL|MULTI|DUBBED)\b')
    languages = tuple(sorted(set(re.findall(
        r'\b(?:ENG|ENGLISH|HIN|HINDI|GER|GERMAN|FRE|FRENCH|ITA|ITALIAN|RUS|RUSSIAN|'
        r'JPN|JAPANESE|UKR|UKRAINIAN|SPA|SPANISH|VF2|VFF|VFQ|VOSTFR|DUBBED)\b', title, re.I))))
    return tuple(bool(re.search(p, title, re.I)) for p in patterns) + (tuple(x.upper() for x in languages),)


def strictly_better(keeper, old, version):
    """Stable sorts hide ties: the keeper must win in BOTH input orders."""
    ranking = copy.deepcopy(version)
    # Eligibility is checked independently on each single candidate. Sorting
    # only preferences avoids legacy title-only equality in list.remove().
    ranking.rules = [r for r in ranking.rules if r[1] == 'preference']
    for order in ((old, keeper), (keeper, old)):
        pair = copy.deepcopy(list(order))
        expected = pair[1] if order[0] is old else pair[0]
        winner = releases.sort(pair, ranking, False)
        if not winner or winner[0] is not expected:
            return False
    return True


def superseded(item):
    """Return (old Media, keeper Media) pairs. No I/O and no deletion here.

    Fail closed on rules not reconstructible from Plex, multipart files, shared
    paths, unknown sizes/durations, ties, differing edits and rule conflicts.
    Upgrade targets protect a fulfilled target but do not prevent pruning two
    fallback copies when their ranking has a strict winner.
    """
    supported = {'title', 'resolution', 'size', 'bitrate', 'cache status'}
    for config in releases.sort.versions:
        if '\u0336' in config[0]:
            continue
        if any(t[0] not in ('media type', 'retries') for t in config[1]):
            return []
        for rule in config[3]:
            cls = next((c for c in releases.sort.version.rule.__subclasses__()
                        if c.name == rule[0]), None)
            if (rule[0] not in supported or cls is None or rule[2] not in cls.operators
                    or rule[1] not in ('requirement', 'preference', 'upgrade')):
                return []
            if rule[1] in ('requirement', 'upgrade') and rule[2] in ('highest', 'lowest'):
                return []
            if rule[0] == 'title' and rule[2] in ('include', 'exclude'):
                try:
                    re.compile(rule[3])
                except (TypeError, re.error):
                    return []
            if rule[0] in ('size', 'bitrate', 'resolution') and rule[2] in ('==', '>=', '<='):
                try:
                    if not math.isfinite(float(rule[3])):
                        return []
                except (TypeError, ValueError):
                    return []
    versions = active_versions(item, collected=True)
    if not versions:
        return []
    candidates = []
    paths = set()
    for media in getattr(item, 'Media', []):
        release = file_release(media)
        if release is None:
            return []
        path = ntpath.normcase(ntpath.normpath(media.Part[0].file))
        if path in paths or not str(getattr(media, 'id', '')).isdigit():
            return []
        paths.add(path)
        duration = float(getattr(media, 'duration', 0) or 0)
        if any(r[0] == 'bitrate' for v in versions for r in v.rules):
            bitrate = float(getattr(media, 'bitrate', 0) or 0)
            if not math.isfinite(bitrate) or bitrate <= 0:
                return []
        if (release.size <= 0 or duration <= 0 or not math.isfinite(duration)
                or not math.isfinite(release.size) or release.resolution not in ('480', '720', '1080', '2160')):
            return []
        signature = (presentation(release.title), str(getattr(item, 'editionTitle', '')),
                     str(getattr(media, 'videoStereoMode', '')))
        candidates.append((media, release, duration, signature))
    result = []
    # Keepers may not themselves be deleted in the same plan.
    protected = set()
    removed = set()
    for old, a, duration, signature in candidates:
        if old.id in protected:
            continue
        admitted = [v for v in versions if matches(a, v)]
        if not admitted:
            continue
        for keeper, b, new_duration, new_signature in candidates:
            if keeper.id == old.id or keeper.id in removed or signature != new_signature:
                continue
            if abs(new_duration - duration) > max(60000, duration * 0.02):
                continue
            if not all(matches(b, v, target=True) and strictly_better(b, a, v)
                       for v in admitted):
                continue
            result.append((old, keeper))
            protected.add(keeper.id)
            removed.add(old.id)
            break
    return result