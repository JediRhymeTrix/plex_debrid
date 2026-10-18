"""Lightweight Trakt monitoring; all service requests are mocked."""
import datetime
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import ui
from content import monitor
from content.classes import media
from content.services import trakt


NOW = datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc).timestamp()
USER = ['test', 'not-a-token']
INBOX = "test's private list: Queue"
MONITORED = "test's private list: Monitored"


def row(number=1, kind='movie', listed='2026-10-18T00:00:00.000Z', released='1980-01-01'):
    obj = NS(title='Example', year=1980, ids=NS(trakt=number, imdb='tt1234'), released=released)
    if kind == 'show':
        del obj.released
        obj.first_aired = released + 'T20:00:00.000Z'
        obj.status = 'returning series'
    return NS(type=kind, listed_at=listed, **{kind: obj})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        settings = dict(list=MONITORED, inbox=INBOX, poll_minutes='10', budget='10',
                        schedule='1,24,168,720')
        self.enterContext(patch.multiple(monitor, **settings))
        self.enterContext(patch.multiple(trakt, users=[USER], lists=[INBOX]))

    def test_empty_monitor_uses_legacy_loop(self):
        with patch.object(monitor, 'list', ''), patch.object(monitor, 'run') as run, \
                patch('ui.ui_cls'), patch.object(ui.content.classes, 'library', side_effect=RuntimeError('legacy reached')):
            with self.assertRaisesRegex(RuntimeError, 'legacy reached'):
                ui.threaded(lambda: True)
            run.assert_not_called()

    def test_configured_monitor_routes_without_constructing_legacy_watchlists(self):
        with patch.object(monitor, 'run') as run, patch('ui.ui_cls'), \
                patch.object(trakt.watchlist, '__init__', side_effect=AssertionError('legacy')):
            ui.threaded(lambda: True)
            run.assert_called_once()

    def test_configuration_requires_owned_main_and_distinct_optional_inboxes(self):
        self.assertEqual(monitor.configuration()[0], USER)
        for bad in (INBOX, "someone's private list: Monitored", '/users/test/lists/public'):
            with self.subTest(bad=bad), patch.object(monitor, 'list', bad):
                with self.assertRaises(ValueError): monitor.configuration()

    def test_main_queue_can_also_be_configured_as_a_legacy_trakt_source(self):
        with patch.object(monitor, 'list', INBOX), patch.object(monitor, 'inbox', ''):
            owner, sources, *_ = monitor.configuration()
            self.assertEqual(owner, USER)
            self.assertEqual(sources, [])

    def test_single_queue_snapshot_never_enrolls_removes_or_calls_inbox_endpoints(self):
        with patch.object(monitor, 'list', INBOX), patch.object(monitor, 'inbox', ''):
            worker = monitor.Worker()
            owned = [NS(name='Queue', privacy='private', ids=NS(trakt=11))]
            with patch.object(monitor, 'pages', side_effect=[owned, [row()]]) as reads, \
                    patch.object(monitor, 'write') as writes, patch.object(monitor, 'enroll') as enroll:
                self.assertTrue(worker.snapshot(NOW))
                self.assertEqual(reads.call_count, 2)
                writes.assert_not_called();enroll.assert_not_called()
            self.assertEqual(len(worker.rows), 1)
            self.assertIn(('movie', 1), worker.wake_since)

    def test_main_queue_readd_with_new_timestamp_wakes_and_invalidates_cached_metadata(self):
        with patch.object(monitor, 'list', INBOX), patch.object(monitor, 'inbox', ''):
            worker = monitor.Worker()
            worker.rows = [row(listed='2025-01-01T00:00:00Z')]
            worker.keep(('movie', 1), NS(type='movie'), NOW)
            worker.next_metadata[('movie', 1)] = NOW + 100000
            owned = [NS(name='Queue', privacy='private', ids=NS(trakt=11))]
            with patch.object(monitor, 'pages', side_effect=[owned, [row()]]), patch.object(monitor, 'write') as writes:
                self.assertTrue(worker.snapshot(NOW));writes.assert_not_called()
            self.assertEqual(worker.wake_since[('movie', 1)], NOW)
            self.assertNotIn(('movie', 1), worker.cache)
            self.assertNotIn(('movie', 1), worker.next_metadata)

    def test_old_timestamp_added_after_empty_queue_refresh_still_wakes(self):
        with patch.object(monitor, 'list', INBOX), patch.object(monitor, 'inbox', ''):
            worker = monitor.Worker()
            owned = [NS(name='Queue', privacy='private', ids=NS(trakt=11))]
            with patch.object(monitor, 'pages', side_effect=[owned, [], owned, [row(listed='2020-01-01T00:00:00Z')]]):
                self.assertTrue(worker.snapshot(NOW))
                self.assertTrue(worker.snapshot(NOW + 600))
            self.assertIn(('movie', 1), worker.wake_since)

    def test_single_queue_members_are_not_passed_to_legacy_autoremove(self):
        from content.services import plex, overseerr
        with patch.object(monitor, 'list', INBOX), patch.object(monitor, 'inbox', ''):
            worker = monitor.Worker();worker.library = [NS(type='movie')]
            original = [INBOX, "test's watchlist"]
            observed = []
            entry = NS(download=lambda **kw: observed.extend(trakt.lists))
            with patch.object(trakt, 'lists', original), patch.object(plex, 'watchlist', return_value=[]), \
                    patch.object(trakt, 'watchlist', return_value=[entry]), patch.object(overseerr, 'requests', return_value=[]):
                worker.legacy_cycle(NOW)
                self.assertEqual(trakt.lists, original)
            self.assertEqual(observed, ["test's watchlist"])

    def test_invalid_schedule_and_budget_rejected(self):
        for name, value in (('schedule', '1,0,2,3'), ('schedule', 'garbage'), ('budget', '0'), ('poll_minutes', 'nan')):
            with patch.object(monitor, name, value), self.assertRaises(ValueError): monitor.configuration()

    def test_dates_are_utc_independent_of_host_timezone(self):
        self.assertEqual(monitor.timestamp('2026-10-18T00:00:00.000Z'), NOW)
        self.assertEqual(monitor.timestamp('2026-10-17T18:00:00-06:00'), NOW)
        self.assertEqual(monitor.timestamp(None), 0)

    def test_partial_pagination_is_not_an_empty_list(self):
        with patch.object(trakt, 'get', side_effect=[([row()], {'X-Pagination-Page-Count': '2'}), (None, None)]):
            self.assertIsNone(monitor.pages('/users/me/lists/1/items', USER))

    def test_pagination_reads_every_page_and_restores_auth_context(self):
        previous = trakt.current_user
        with patch.object(trakt, 'get', side_effect=[([row()], {'X-Pagination-Page-Count': '2'}), ([row(2)], {'X-Pagination-Page-Count': '2'})]):
            self.assertEqual(len(monitor.pages('/users/me/lists/1/items', USER)), 2)
        self.assertEqual(trakt.current_user, previous)

    def test_transfer_adds_verifies_then_removes_only_exact_inbox(self):
        events = []
        def read(path, user):
            events.append(('read', path)); return [row()]
        def post(path, data, user):
            events.append(('post', path))
            return NS(deleted=NS(movies=1, shows=0))
        with patch.object(monitor, 'pages', side_effect=read), patch.object(monitor, 'write', side_effect=post):
            result, wake = monitor.enroll([row()], [], USER, 11, 22)
        self.assertEqual(events[0], ('post', '/users/me/lists/22/items'))
        self.assertEqual(events[-1], ('post', '/users/me/lists/11/items/remove'))
        self.assertIn(('movie', 1), wake)
        self.assertEqual(len(result), 1)

    def test_transfer_never_removes_without_verified_membership(self):
        for result in (None, []):
            with patch.object(monitor, 'pages', return_value=result), patch.object(monitor, 'write') as write:
                monitor.enroll([row()], [], USER, 11, 22)
                self.assertFalse(any('/remove' in call.args[0] for call in write.call_args_list))

    def test_readd_existing_member_wakes_without_duplicate_enrollment(self):
        with patch.object(monitor, 'pages', return_value=[row()]), patch.object(monitor, 'write') as write:
            _, wake = monitor.enroll([row()], [row()], USER, 11, 22)
            self.assertIn(('movie', 1), wake)
            self.assertFalse(any(call.args[0].endswith('/items') for call in write.call_args_list))

    def test_future_media_is_parked_and_old_media_readd_is_immediate(self):
        worker = monitor.Worker()
        future = row(released='2027-01-01')
        self.assertFalse(worker.due(('movie', 1), future.movie, NOW, False))
        self.assertTrue(worker.due(('movie', 1), row().movie, NOW, True))

    def test_misses_do_not_disable_catalogue_forever(self):
        worker = monitor.Worker()
        obj = row().movie
        worker.checked[('movie', 1)] = NOW
        self.assertFalse(worker.due(('movie', 1), obj, NOW + 10, False))
        self.assertTrue(worker.due(('movie', 1), obj, NOW + 721 * 3600, False))

    def test_budget_and_metadata_cache_are_bounded(self):
        worker = monitor.Worker()
        self.assertLessEqual(worker.remaining, 10)
        for i in range(monitor.CACHE_LEAVES + 1):
            worker.keep(('movie', i), NS(type='movie'), NOW)
        self.assertLessEqual(sum(x[2] for x in worker.cache.values()), monitor.CACHE_LEAVES)

    def test_managed_copy_never_permanently_ignores_or_autoremoves(self):
        obj = media(NS(type='movie', _monitor_managed=True))
        with patch('content.classes.ignore.add') as ignore:
            obj.watch(); ignore.assert_not_called()
        self.assertTrue(obj.upgrade_due())

    def test_empty_zero_page_list_is_valid(self):
        with patch.object(trakt, 'get', return_value=([], {'X-Pagination-Page-Count': '0'})):
            self.assertEqual(monitor.pages('/empty', USER), [])

    def test_show_budget_continues_on_next_pass_without_repeating_first_episode(self):
        worker = monitor.Worker(); worker.remaining = 1
        identity = ('show', 1); worker.wake.add(identity); worker.wake_since[identity] = NOW - 1
        def episode(index):
            return NS(type='episode', parentIndex=1, index=index, first_aired='2026-10-17T00:00:00Z',
                      versions=lambda: [NS(name='Main')], watched=lambda: False, downloading=lambda: False,
                      query=lambda: 'example.S01E%02d.' % index)
        show = NS(type='show', Seasons=[NS(index=1, Episodes=[episode(1), episode(2)])])
        working, ids = worker.selected(show, identity, NOW, True)
        self.assertEqual(ids, [('episode', 1, 1, 1)])
        working.Seasons[0].Episodes.clear()
        self.assertEqual(len(show.Seasons[0].Episodes), 2)
        worker.mark(ids[0], NOW);worker.remaining = 1
        working, ids = worker.selected(show, identity, NOW + 60, True)
        self.assertEqual(ids, [('episode', 1, 1, 2)])

    def test_removal_from_monitoring_prunes_work_not_library_files(self):
        worker = monitor.Worker();worker.rows = [row()]
        worker.keep(('movie', 1), NS(type='movie'), NOW)
        owned = [NS(name='Queue', ids=NS(trakt=11)), NS(name='Monitored', ids=NS(trakt=22))]
        with patch.object(monitor, 'pages', side_effect=[owned, [], []]), patch.object(monitor, 'write') as write:
            self.assertTrue(worker.snapshot(NOW));write.assert_not_called()
        self.assertEqual(worker.rows, []);self.assertEqual(len(worker.cache), 0)

    def test_source_failure_preserves_prior_snapshot_without_writes(self):
        worker = monitor.Worker();worker.rows = [row()]
        with patch.object(monitor, 'pages', return_value=None), patch.object(monitor, 'write') as write:
            self.assertFalse(worker.snapshot(NOW));write.assert_not_called()
        self.assertEqual(len(worker.rows), 1)

    def test_failed_enrollment_verification_retries_immediate_wake(self):
        worker = monitor.Worker();worker.rows = [row()]
        owned = [NS(name='Queue', ids=NS(trakt=11)), NS(name='Monitored', ids=NS(trakt=22))]
        with patch.object(monitor, 'pages', side_effect=[owned, [row()], [row()], None]):
            self.assertTrue(worker.snapshot(NOW))
        self.assertEqual(worker.seen_inbox, set())
        with patch.object(monitor, 'pages', side_effect=[owned, [row()], [row()], [row()], [row()]]), patch.object(monitor, 'write'):
            self.assertTrue(worker.snapshot(NOW + 600))
        self.assertIn(('movie', 1), worker.wake)

    def test_cycle_dispatches_only_budgeted_selected_work(self):
        worker = monitor.Worker();worker.limit = 1;worker.rows = [row(1), row(2)]
        worker.next_sources = worker.next_library = worker.next_legacy = NOW + 10000
        worker.library = [NS(type='movie')];worker.wake = {('movie', 1), ('movie', 2)}
        def hydrate(r, now):
            return NS(type='movie', released='1980-01-01', versions=lambda: [NS(name='Main')])
        with patch.object(worker, 'hydrate', side_effect=hydrate), patch.object(worker, 'dispatch', wraps=worker.dispatch), \
                patch.object(worker, 'selected', side_effect=lambda item, ident, now, force: (NS(type='movie', download=lambda **k: None), [ident])) as select:
            worker.cycle(NOW)
        self.assertEqual(select.call_count, 1)
        self.assertEqual(worker.remaining, 0)
        self.assertEqual(len(worker.wake), 1)

    def test_legacy_removal_cannot_touch_consumable_inboxes(self):
        from content.services import plex, overseerr
        worker = monitor.Worker();worker.library = [NS(type='movie')]
        original = [INBOX, "test's watchlist"]
        observed = []
        entry = NS(download=lambda **kw: observed.extend(trakt.lists))
        with patch.object(trakt, 'lists', original), patch.object(plex, 'watchlist', return_value=[]), \
                patch.object(trakt, 'watchlist', return_value=[entry]), patch.object(overseerr, 'requests', return_value=[]):
            worker.legacy_cycle(NOW)
            self.assertEqual(trakt.lists, original)
        self.assertEqual(observed, ["test's watchlist"])

    def test_unchecked_show_tail_precedes_repeated_prefix(self):
        worker = monitor.Worker();worker.remaining = 1
        def ep(index, season=1):
            return NS(type='episode', parentIndex=season, index=index,
                      first_aired='2026-10-17T00:00:00Z', versions=lambda: [NS(name='Main')],
                      watched=lambda: False, downloading=lambda: False, query=lambda: 'example.S%02dE%02d' % (season, index))
        show = NS(type='show', Seasons=[NS(index=1, Episodes=[ep(1), ep(2)]), NS(index=2, Episodes=[ep(1, 2)])])
        for identity in (('episode', 1, 1, 1), ('episode', 1, 1, 2)):
            worker.mark(identity, NOW - 86400)
        import releases
        with patch.object(releases.sort, 'versions', [['Main', [], 'en', []]]):
            working, identities = worker.selected(show, ('show', 1), NOW, False)
            self.assertEqual(identities, [('episode', 1, 2, 1)])

    def test_recent_missing_episode_keeps_hourly_schedule(self):
        worker = monitor.Worker()
        ep = NS(type='episode', first_aired='2026-10-17T00:00:00Z')
        self.assertEqual(worker.interval(('episode', 1, 1, 1), ep, NOW), 3600)

    def test_old_item_missing_baseline_does_not_wait_a_month(self):
        worker = monitor.Worker()
        movie = NS(type='movie', originallyAvailableAt='1980-01-01')
        self.assertEqual(worker.interval(('movie', 1), movie, NOW), 86400)

    def test_inbox_remove_readd_race_is_not_consumed(self):
        newer = row(listed='2026-10-18T01:00:00Z')
        with patch.object(monitor, 'pages', side_effect=[[row()], [newer]]), patch.object(monitor, 'write') as write:
            monitor.enroll([row()], [row()], USER, 11, 22)
            self.assertFalse(any('/remove' in c.args[0] for c in write.call_args_list))

    def test_swallowed_indexer_timeout_retries_before_catalogue_interval(self):
        import scraper
        from scraper.services import prowlarr
        worker = monitor.Worker()
        def download(**kw):
            prowlarr.scrape('Example', '(.*)')
        item = NS(type='movie', download=download)
        with patch.object(scraper.services, 'active', ['prowlarr']), \
                patch.object(prowlarr.session, 'get', side_effect=prowlarr.requests.exceptions.Timeout):
            worker.dispatch(item, [('movie', 1)], NOW)
        self.assertLess(worker.checked[('movie', 1)], NOW - 700 * 3600)

    def test_healthy_show_metadata_not_starved_by_failing_prefix(self):
        worker = monitor.Worker();worker.limit = 1
        worker.rows = [row(1, 'show'), row(2, 'show')]
        worker.next_sources = worker.next_library = worker.next_legacy = NOW + 100000
        worker.library = [NS(type='movie')]
        calls = []
        def hydrate(r, now):
            calls.append(key := monitor.key(r))
            if key[1] == 1: raise RuntimeError('offline')
            return NS(type='show', Seasons=[])
        with patch.object(worker, 'hydrate', side_effect=hydrate):
            worker.cycle(NOW);worker.cycle(NOW + 600)
        self.assertIn(('show', 2), calls)

    def test_failed_priority_wake_does_not_starve_normal_work(self):
        worker = monitor.Worker();worker.limit = 1;worker.poll = 60
        worker.rows = [row(1, 'show'), row(2, 'show')]
        worker.wake.add(('show', 1));worker.wake_since[('show', 1)] = NOW
        worker.next_sources = worker.next_library = worker.next_legacy = NOW + 100000
        worker.library = [NS(type='movie')];calls = []
        def hydrate(r, now):
            calls.append(monitor.key(r))
            if monitor.key(r)[1] == 1:raise RuntimeError('offline')
            return NS(type='show', Seasons=[])
        with patch.object(worker, 'hydrate', side_effect=hydrate):
            for n in range(3):worker.cycle(NOW + n * 60)
        self.assertIn(('show', 2), calls)
        self.assertIn(('show', 1), worker.wake_since)

    def test_failed_readd_retains_force_intent_across_source_refresh(self):
        worker = monitor.Worker()
        worker.rows = [row(1, 'show', listed='2025-01-01T00:00:00Z')]
        worker.wake_since[('show', 1)] = NOW
        # Failed metadata demotes priority, not the original immediate request.
        self.assertNotIn(('show', 1), worker.wake)
        owned = [NS(name='Queue', ids=NS(trakt=11)), NS(name='Monitored', ids=NS(trakt=22))]
        with patch.object(monitor, 'pages', side_effect=[owned, worker.rows, []]):
            self.assertTrue(worker.snapshot(NOW + 600))
        self.assertEqual(worker.wake_since[('show', 1)], NOW)

    def test_successful_trakt_create_is_not_logged_as_error(self):
        with patch.object(trakt, 'ui_print') as log:
            trakt.logerror(NS(status_code=201, content=b'created'))
            log.assert_not_called()

    def test_hydration_numbers_full_anime_before_selecting_subset(self):
        worker = monitor.Worker()
        full = media(NS(type='show', genres=['anime'], Seasons=[
            NS(index=1, Episodes=[NS(index=1), NS(index=2)]),
            NS(index=2, Episodes=[NS(index=1)])]))
        with patch.object(trakt, 'show', return_value=full):
            item = worker.hydrate(row(1, 'show'), NOW)
        self.assertEqual(item.Seasons[1].Episodes[0].anime_count, '3')
        working = media(NS(**dict(item.__dict__, Seasons=item.Seasons[1:])))
        working.isanime()
        self.assertEqual(working.Seasons[0].Episodes[0].anime_count, '3')


if __name__ == '__main__':
    unittest.main()