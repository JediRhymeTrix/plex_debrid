from base import *
#import child modules
from debrid import services
from ui.ui_print import *

tracker = []
downloading = []
uncached = 'true'

class Deferred(Exception):
    """Provider work is pending/uncertain: retry later, not a successful grab."""


def provider_inventory(element, key, loader):
    """Reuse bounded inventory only within an explicitly scoped acquisition pass."""
    inventory = getattr(element, '_provider_inventory', None)
    if not isinstance(inventory, dict):
        return loader()
    if key not in inventory:
        inventory[key] = loader()
    return inventory[key]


def invalidate_inventory(element, key):
    inventory = getattr(element, '_provider_inventory', None)
    if isinstance(inventory, dict):
        inventory.pop(key, None)


def _providers(release, stream):
    """Routing is not cache evidence. Only reorder the RD/TB slots by default."""
    active = list(services.get())
    route = None
    for pattern, targets in tracker:
        if regex.search(pattern, release.source, regex.I):
            route = [targets] if isinstance(targets, str) else list(targets)
    if route is not None:
        active = [service for target in route for service in active if service.short == target]
    if release.type == 'usenet':
        return [service for service in active if service.short == 'TB']
    cached = getattr(release, 'cached', [])
    if stream:
        # RD's disabled cache endpoint means unknown, not unavailable. Both
        # adapters can safely probe existing jobs without starting uncached work.
        active = [service for service in active if service.short in ('RD', 'TB')
                  or service.short in cached or route is not None]
    elif cached and route is None:
        active = [service for service in active if service.short in ('RD', 'TB') or service.short in cached]
    if route is None:
        preferred = ['TB', 'RD'] if stream and 'TB' in cached and 'RD' not in cached else ['RD', 'TB']
        pair = sorted((s for s in active if s.short in preferred), key=lambda s: preferred.index(s.short))
        ordered = iter(pair)
        active = [next(ordered) if s.short in preferred else s for s in active]
    return active


# Download Method:
def download(element, stream=True, query='', force=False):
    scoped = not hasattr(element, '_provider_inventory')
    if scoped:
        element._provider_inventory = {}
    try:
        for release in copy.deepcopy(element.Releases):
            for service in _providers(release, stream):
                # A failed provider may mutate files/links. Never pass that
                # partial result on; Deferred deliberately stops fallback.
                element.Releases = [copy.deepcopy(release)]
                if service.download(element, stream=stream, query=query, force=force):
                    selected = element.Releases[0]
                    for attribute in ('existing_releases', 'downloaded_releases'):
                        if not hasattr(element, attribute):
                            setattr(element, attribute, [])
                        getattr(element, attribute).append(selected.title)
                    return True
            element.Releases = [release]
        return False
    finally:
        if scoped:
            del element._provider_inventory

# Check Method:
def check(element, force=False):
    if len(element.Releases) == 0:
        return
    checked = []
    for release in element.Releases[:]:
        if release.checked:
            checked += [release]
            element.Releases.remove(release)
    activeservices = services.active
    if len(element.Releases) > 0:
        ui_print("checking cache status for scraped releases on: [" + "],[".join(activeservices) + "] ...")
        for service in services.get():
            service.check(element, force=force)
        ui_print("done")
    for release in checked:
        element.Releases += [release]
    for release in element.Releases:
        release.checked = True
