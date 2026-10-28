"""Per-file version coverage and conservative, side-effect-free cleanup planning."""
import copy
import ntpath
import re
import math
import time

import releases


def active_versions(item, collected=False):
    result = []
    primary = next((v for v in releases.sort.versions if '\u0336' not in v[0]), None)
    for name, triggers, lang, rules in releases.sort.versions:
        if '\u0336' in name:
            continue
        version = releases.sort.version(name, copy.deepcopy(triggers), lang, copy.deepcopy(rules))
        if primary is not None and name != primary[0]:
            # Optional presentations share intrinsic quality preferences without
            # weakening their own admission requirements or custom rule order.
            inherited = [copy.deepcopy(r) for r in primary[3] if r[1] in ('preference', 'upgrade')
                         and r[0] in ('title', 'resolution', 'bitrate')
                         and r not in version.rules]
            index = next((i for i, r in enumerate(version.rules) if r[0] in ('cache status', 'size')),
                         len(version.rules))
            version.rules[index:index] = inherited
        # Retry limits govern initial acquisition, not already-collected upgrades.
        if collected and isinstance(version.triggers, list):
            version.triggers = [t for t in version.triggers
                                if not (t[0] == 'retries' and t[1] == '<=')]
        if version.applies(item):
            result.append(version)
    return result


def stereo_required(version):
    return any(r[0] == 'title' and r[1] == 'requirement' and r[2] == 'include'
               and re.search(r'3D|SBS|FRAME.?PACK|HOU', str(r[3]), re.I) for r in version.rules)


def stereo(candidate):
    return bool(re.search(r'(?<![A-Z0-9])(?:3D(?:H?SBS|H?OU)?|(?:FULL|HALF)[ ._-]?(?:SBS|OU)|[HF]?SBS|HOU|FRAME[ ._-]?PACK)(?![A-Z0-9])',
                          candidate.title, re.I))


def matches(candidate, version, target=False):
    """Every required (and optionally upgrade) rule must match ONE file."""
    # A stereoscopic copy cannot stand in for an ordinary playable main copy.
    if stereo(candidate) and not stereo_required(version):
        return False
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


def predicate(candidate, rule, version):
    selected = copy.deepcopy(rule);selected[1] = 'upgrade'
    # Retain bucket admission while testing one quality feature, especially 3D.
    requirements = [r for r in version.rules if r[1] == 'requirement']
    single = releases.sort.version('feature', [], version.lang, requirements + [selected])
    return matches(candidate, single, True)


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
        # Plex stream facts can supply tags absent from generic filenames.
        # This is existing metadata, not an extra probe or optimistic guess.
        for stream in getattr(part, 'Stream', []) or []:
            description = ' '.join(str(getattr(stream, key, '') or '')
                                   for key in ('displayTitle', 'extendedDisplayTitle'))
            if re.search(r'\bAtmos\b', description, re.I):
                title += '.ATMOS'
            if str(getattr(stream, 'DOVIPresent', '')).lower() in ('1', 'true'):
                title += '.DV'
            if str(getattr(stream, 'colorTrc', '')).lower() in ('smpte2084', 'arib-std-b67'):
                title += '.HDR'
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
        acknowledged = all(any(matches(c, v) for v in versions)
                           and all(any(matches(p, v) and _covers(p, c, v) for p in result)
                                   for v in versions if matches(c, v)) for c in pending)
        if acknowledged:
            session.pop(key, None)
            pending = []
    return result + [copy.deepcopy(r) for r in pending]


def _covers(candidate, pending, version):
    """A scanned fallback must not acknowledge a better unscanned grab."""
    previous, current = presentation(pending.title), presentation(candidate.title)
    if (any(a and not b for a, b in zip(previous[:-1], current[:-1]))
            or not set(previous[-1]).issubset(current[-1])):
        return False
    comparison = copy.deepcopy(version)
    comparison.rules = [r for r in comparison.rules if r[0] not in ('cache status', 'source', 'seeders')]
    for rule in comparison.rules:
        if rule[1] != 'upgrade' and not (rule[0] == 'title' and rule[1] == 'preference'):
            continue
        if predicate(pending, rule, comparison) and not predicate(candidate, rule, comparison):
            return False
    try:
        return (int(candidate.resolution) >= int(pending.resolution)
                and not improves(pending, candidate, comparison))
    except (TypeError, ValueError, AttributeError):
        return False


def missing_versions(item, library, downloaded, session):
    candidates = existing(item, library, session)
    collected = library_match(item, library) is not None or bool(candidates)
    versions = active_versions(item, collected=collected)
    has_imax = any(re.search(r'IMAX', c.title, re.I) and not stereo(c) for c in candidates)
    result = []
    for version in versions:
        # IMAX wins expanded-ratio exclusivity, including after a restart.
        if 'matte' in version.name.lower() and has_imax:
            continue
        reached = any(matches(c, version, target=True) for c in candidates)
        if reached and not getattr(item, '_monitor_managed', False):
            continue
        found = library_match(item, library)
        if found is not None and not candidates and getattr(found, 'Media', []):
            # Multipart or incomplete Plex facts cannot prove a replacement is
            # needed. Keep them rather than repeatedly reacquire after restart.
            continue
        if any(matches(c, version) for c in candidates):
            # Targets describe the destination, not an all-or-nothing gate.
            version = releases.sort.version(version.name + ' upgrade', version.triggers,
                                            version.lang, copy.deepcopy(version.rules))
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


def improves(candidate, old, version):
    """Monotonic per-file progress within a coverage bucket, not target lock-in."""
    comparison = copy.deepcopy(version)
    comparison.rules = [r for r in comparison.rules if not (
        r[0] in ('cache status', 'source', 'seeders') and r[1] == 'preference')]
    if not matches(candidate, comparison) or not matches(old, comparison):
        return False
    try:
        if int(candidate.resolution) < int(old.resolution):
            return False
    except (AttributeError, ValueError, TypeError):
        return False
    # Do not sacrifice another independently valuable presentation/cut/language.
    a, b = presentation(old.title), presentation(candidate.title)
    if any(x and not y for x, y in zip(a[:-1], b[:-1])) or not set(a[-1]).issubset(b[-1]):
        return False
    gain = False
    for rule in comparison.rules:
        if rule[1] != 'upgrade' and not (rule[0] == 'title' and rule[1] == 'preference'):
            continue
        had, has = predicate(old, rule, comparison), predicate(candidate, rule, comparison)
        if had and not has:
            return False
        gain |= rule[1] == 'upgrade' and has and not had
    if gain:
        return True
    ranked, baseline = copy.deepcopy(candidate), copy.deepcopy(old)
    # Acquisition file counts and cache availability are not intrinsic quality.
    ranked.wanted = ranked.unwanted = baseline.wanted = baseline.unwanted = 0
    return strictly_better(ranked, baseline, comparison)


def accepts(candidate, existing, version):
    relevant = [r for r in existing if matches(r, version)]
    if not relevant:
        return matches(candidate, version)
    comparison = copy.deepcopy(version)
    def aligned(value):
        value = copy.deepcopy(value)
        if not any(r[0] == 'title' and r[1] == 'requirement' and r[2] == 'include'
                   and re.search(r'IMAX|MATTE|3D', str(r[3]), re.I) for r in version.rules):
            # Main-quality comparison may improve alongside a protected IMAX/
            # matte copy; this never grants cleanup permission for that copy.
            value.title = re.sub(r'IMAX|OPEN[ ._-]?MATTE|\bMATTE\b', '', value.title, flags=re.I)
        return value
    comparison.rules = [r for r in comparison.rules if not (
        r[0] == 'title' and r[1] == 'preference' and re.search(r'IMAX|MATTE', str(r[3]), re.I))]
    proposed = aligned(candidate)
    baselines = [aligned(old) for old in relevant]
    if any(_covers(old, proposed, comparison) for old in baselines):
        return False
    return (any(improves(proposed, old, comparison) for old in baselines)
            and not any(improves(old, proposed, comparison) for old in baselines))


def confirmed(item, candidate):
    """Recheck provider file facts after discovery, before recording success."""
    for file in getattr(candidate, 'files', []):
        name = ntpath.basename(getattr(file, 'name', ''))
        resolutions = re.findall(r'(?<!\d)(2160|1080|720|480)p(?!\d)', name, re.I)
        if resolutions and any(str(r) != str(candidate.resolution) for r in resolutions):
            return False
        if re.search(r'(?<![A-Z0-9])SDR(?![A-Z0-9])', name, re.I) and re.search(r'HDR|DOVI|\bDV\b', candidate.title, re.I):
            return False
    if not hasattr(item, 'version'):
        return True
    if not matches(candidate, item.version):
        return False
    if not getattr(item, '_monitor_managed', False):
        return True
    from content.classes import media
    from content.services.plex import current_library
    comparison = copy.deepcopy(item.version)
    comparison.rules = [r for r in comparison.rules if r[0] != 'cache status' or r[1] != 'preference']
    return accepts(candidate, existing(item, current_library, media.session_releases), comparison)


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
            if not all(improves(b, a, v)
                       for v in admitted):
                continue
            result.append((old, keeper))
            protected.add(keeper.id)
            removed.add(old.id)
            break
    return result