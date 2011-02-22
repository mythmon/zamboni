import collections
from datetime import datetime, timedelta
import uuid

from django.core.cache import cache
from django.conf import settings
from django.db.models import Q, signals as db_signals

import jingo
import redisutils

from amo.utils import sorted_groupby
from versions.compare import version_int
from .models import BlocklistItem, BlocklistPlugin, BlocklistGfx, BlocklistApp


App = collections.namedtuple('App', 'guid min max')


def blocklist(request, apiver, app, appver):
    key = 'blocklist:%s:%s:%s'%  (apiver, app, appver)
    response = cache.get(key)
    if response is None:
        response = _blocklist(request, apiver, app, appver)
        cache.set(key, response, 60 * 60)
        # This gets cleared with the clear_blocklist signal handler.
        redisutils.connections['master'].sadd('blocklist:keys', key)
    if settings.BLOCKLIST_COOKIE not in request.COOKIES:
        response.set_cookie(settings.BLOCKLIST_COOKIE, uuid.uuid4(),
                            expires=datetime.now() + timedelta(days=5 * 365),
                            path='/blocklist/', secure=True)
    return response


def _blocklist(request, apiver, app, appver):
    apiver = int(apiver)
    items = get_items(apiver, app, appver)
    plugins = get_plugins(apiver, app, appver)
    gfxs = BlocklistGfx.objects.filter(Q(guid__isnull=True) | Q(guid=app))
    return jingo.render(request, 'blocklist/blocklist.xml',
                            dict(items=items, plugins=plugins, gfxs=gfxs,
                                 apiver=apiver, appguid=app, appver=appver))


def clear_blocklist(*args, **kw):
    # Something in the blocklist changed; invalidate all responses.
    keys = redisutils.connections['master'].smembers('blocklist:keys')
    cache.delete_many(keys)


for m in BlocklistItem, BlocklistPlugin, BlocklistGfx, BlocklistApp:
    db_signals.post_save.connect(clear_blocklist, sender=m,
                                 dispatch_uid='save_%s' % m)
    db_signals.post_delete.connect(clear_blocklist, sender=m,
                                   dispatch_uid='delete_%s' % m)


def get_items(apiver, app, appver):
    # Collapse multiple blocklist items (different version ranges) into one
    # item and collapse each item's apps.
    addons = (BlocklistItem.uncached
              .filter(Q(app__guid__isnull=True) | Q(app__guid=app))
              .extra(select={'app_guid': 'blapps.guid',
                             'app_min': 'blapps.min',
                             'app_max': 'blapps.max'}))
    items = {}
    for guid, rows in sorted_groupby(addons, 'guid'):
        rr = []
        for id, rs in sorted_groupby(list(rows), 'id'):
            rs = list(rs)
            rr.append(rs[0])
            rs[0].apps = [App(r.app_guid, r.app_min, r.app_max)
                           for r in rs if r.app_guid]
        os = [r.os for r in rr if r.os]
        items[guid] = {'rows': rr, 'os': os and os[0] or None}
    return items


def get_plugins(apiver, app, appver):
    # API versions < 3 ignore targetApplication entries for plugins so only
    # block the plugin if the appver is within the block range.
    plugins = BlocklistPlugin.uncached.filter(
        Q(guid__isnull=True) | Q(guid=app))
    if apiver < 3:
        def between(ver, min, max):
            if not (min and max):
                return True
            return version_int(min) < ver < version_int(max)
        app_version = version_int(appver)
        plugins = [p for p in plugins if between(app_version, p.min, p.max)]
    return plugins
