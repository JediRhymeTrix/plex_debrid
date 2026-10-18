"""Queue retirement is metadata-only; every service operation is mocked."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import ui
import releases
from content import monitor, retirement
from content.classes import media
from content.services import plex, trakt
from test_monitor import NOW, USER, INBOX


OLD = '2025-01-01T00:00:00Z'
PRIMARY = ['Main', [], 'en', [
    ['resolution', 'upgrade', '==', '2160'],
    ['title', 'upgrade', 'include', r'\bDV\b'],
    ['title', 'upgrade', 'include', 'HDR'],
    ['size', 'requirement', '>=', '0.1']]]


def item(kind='movie', number=1, title='Example', **extras):
    return media(NS(type=kind, title=title, year=2000, ids=NS(trakt=number, imdb='tt1234'),
                    EID=['imdb://tt1234'], ratingKey=str(number),
                    Guid=[NS(id='imdb://tt1234')], addedAt=1000000000,
                    **extras))


def copy_media(name='Example.2160p.DV.HDR.mkv', resolution='4k'):
    return NS(id='10', videoResolution=resolution, duration=6000000, bitrate=10000,
              Part=[NS(id='20', file='T:\\' + name, size=3000000000, duration=6000000,
                       exists=True, accessible=True, key='/library/parts/20/1/file.mkv')])


def row(kind='movie', **extras):
    obj = NS(ids=NS(trakt=1, imdb='tt1234'), title='Example', year=2000,
             released='2000-01-01', status='released' if kind == 'movie' else 'ended', **extras)
    return NS(type=kind, listed_at=OLD, **{kind: obj})


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(releases.sort, 'versions', [copy.deepcopy(PRIMARY)]))
        self.enterContext(patch.multiple(monitor, list=INBOX, inbox='', retirement_policy='target-met',
                                        retirement_grace_days='90'))
        self.enterContext(patch.multiple(trakt, users=[USER], lists=[INBOX]))
        self.enterContext(patch.object(media, 'session_releases', {}, create=True))
        self.enterContext(patch.object(retirement, '_readable', return_value=True))
        self.movie = item(Media=[copy_media()])

    def test_recommended_defaults_and_validation(self):
        self.assertEqual(retirement.POLICIES, ('off', 'target-met', 'playable-after-grace'))
        for mode in retirement.POLICIES:
            with patch.object(monitor, 'retirement_policy', mode): monitor.configuration()
        for name, value in (('retirement_policy', 'anything'), ('retirement_grace_days', '0'),
                            ('retirement_grace_days', 'nan'), ('retirement_grace_days', '-1')):
            with patch.object(monitor, name, value), self.assertRaises(ValueError): monitor.configuration()

    def test_age_requires_list_release_and_acquisition_grace(self):
        self.assertTrue(retirement.old_enough([1000000000, 1100000000, 1200000000], NOW, 90))
        for dates in ([NOW - 86400, 1100000000, 1200000000], [0, 1100000000], [None], [NOW + 1], []):
            with self.subTest(dates=dates): self.assertFalse(retirement.old_enough(dates, NOW, 90))

    def test_main_target_on_single_playable_file(self):
        self.assertTrue(retirement.covered(self.movie, 'target-met'))
        self.movie.Media = [copy_media('Example.2160p.DV.mkv'), copy_media('Example.1080p.HDR.mkv', '1080')]
        self.assertFalse(retirement.covered(self.movie, 'target-met'))

    def test_below_target_fallback_only_allowed_in_playable_mode(self):
        self.movie.Media = [copy_media('Example.1080p.mkv', '1080')]
        self.assertFalse(retirement.covered(self.movie, 'target-met'))
        self.assertTrue(retirement.covered(self.movie, 'playable-after-grace'))

    def test_hard_requirements_still_apply_in_playable_mode(self):
        self.movie.Media[0].Part[0].size = 1
        self.assertFalse(retirement.covered(self.movie, 'playable-after-grace'))

    def test_unreadable_or_unknown_playability_never_retires(self):
        for field in ('exists', 'accessible'):
            for value in (False, None):
                with self.subTest(field=field, value=value):
                    candidate = copy.deepcopy(self.movie);setattr(candidate.Media[0].Part[0], field, value)
                    self.assertFalse(retirement.covered(candidate, 'target-met'))
        with patch.object(retirement, '_readable', return_value=False):
            self.assertFalse(retirement.covered(self.movie, 'target-met'))

    def test_unsupported_target_rules_fail_closed(self):
        releases.sort.versions[0][3].append(['source', 'requirement', 'include', 'torrentio'])
        self.assertFalse(retirement.covered(self.movie, 'target-met'))

    def test_cached_requirement_is_proven_by_readable_collected_file(self):
        releases.sort.versions[0][3].append(['cache status', 'requirement', 'cached', ''])
        self.assertTrue(retirement.covered(self.movie, 'target-met'))
        with patch.object(retirement, '_readable', return_value=False):
            self.assertFalse(retirement.covered(self.movie, 'target-met'))

    def test_verification_deadline_stops_additional_byte_reads(self):
        with patch.object(retirement.time, 'monotonic', return_value=31), patch.object(retirement, '_readable') as read:
            self.assertFalse(retirement.covered(self.movie, 'target-met', deadline=30))
            read.assert_not_called()

    def test_deadline_expiring_during_read_cannot_authorize_retirement(self):
        with patch.object(retirement.time, 'monotonic', side_effect=[0, 31]):
            self.assertFalse(retirement.covered(self.movie, 'target-met', deadline=30))

    def test_actual_readability_requires_http_bytes_and_matching_size(self):
        from content.cleanup import _readable
        for status, size, data, expected in ((206, '3000000000', b'x', True),
                                            (200, '3000000000', b'x', True),
                                            (206, '1', b'x', False), (206, '3000000000', b'', False),
                                            (404, '3000000000', b'x', False)):
            response = NS(status_code=status, headers={'Content-Range': 'bytes 0-0/' + size, 'Content-Length': size},
                          iter_content=lambda **kwargs: iter([data]), close=lambda: None)
            with patch.object(plex.session, 'get', return_value=response) as get, \
                    patch.object(plex, 'users', [['test', 'no-secret']]):
                self.assertEqual(_readable(plex, self.movie.Media[0]), expected)
                self.assertEqual(get.call_args.kwargs['headers']['Range'], 'bytes=0-0')

    def test_stock_quality_configuration_can_retire_readable_fulfilled_copy(self):
        stock = ['1080p SDR', [['retries', '<=', '48'], ['media type', 'all', '']], 'en', [
            ['cache status', 'requirement', 'cached', ''], ['resolution', 'requirement', '<=', '1080'],
            ['resolution', 'preference', 'highest', ''], ['size', 'requirement', '>=', '0.1']]]
        self.movie.Media = [copy_media('Example.1080p.mkv', '1080')]
        with patch.object(releases.sort, 'versions', [stock]):
            self.assertTrue(retirement.covered(self.movie, 'target-met'))

    def test_optional_presentations_do_not_block_main_target(self):
        releases.sort.versions.append(['3D', [], 'en', [['title', 'requirement', 'include', '3D']]])
        self.assertTrue(retirement.covered(self.movie, 'target-met'))

    def test_primary_explicit_presentation_requirement_is_not_optional(self):
        releases.sort.versions[0][3].append(['title', 'requirement', 'include', 'IMAX'])
        self.assertFalse(retirement.covered(self.movie, 'target-met'))

    def test_movie_requires_home_release_and_matching_identity(self):
        summary = NS(ids=NS(trakt=1, imdb='tt1234'), status='released', released='2000-01-01')
        homes = [NS(release_type='digital', release_date='2000-03-01')]
        with patch.object(retirement, '_trakt', side_effect=[summary, homes]), \
                patch.object(retirement, '_fresh', return_value=self.movie):
            self.assertTrue(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))
        with patch.object(retirement, '_trakt', side_effect=[summary, []]):
            self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))
        wrong = copy.deepcopy(self.movie);wrong.Guid = [NS(id='imdb://tt9999')];wrong.EID = ['imdb://tt9999']
        with patch.object(retirement, '_trakt', side_effect=[summary, homes]), patch.object(retirement, '_fresh', return_value=wrong):
            self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))

    def test_recent_acquisition_or_home_release_restarts_grace(self):
        summary = NS(ids=NS(trakt=1, imdb='tt1234'), status='released', released='2000-01-01')
        for kind in ('added', 'home'):
            candidate = copy.deepcopy(self.movie)
            homes = [NS(release_type='digital', release_date='2000-03-01')]
            if kind == 'added': candidate.addedAt = NOW - 86400
            if kind == 'home': homes[0].release_date = '2026-10-17'
            with patch.object(retirement, '_trakt', side_effect=[summary, homes]), patch.object(retirement, '_fresh', return_value=candidate):
                self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))

    def test_partial_summary_ids_do_not_hide_request_fresh_plex_conflict(self):
        request = row();request.movie.ids.tmdb = 10
        self.movie.ids.tmdb = 10
        summary = NS(ids=NS(trakt=1, tmdb=10), status='released', released='2000-01-01')
        fresh = copy.deepcopy(self.movie)
        fresh.ids = NS(tmdb=10, imdb='tt9999');fresh.EID = ['tmdb://10', 'imdb://tt9999']
        fresh.Guid = [NS(id='tmdb://10'), NS(id='imdb://tt9999')]
        with patch.object(retirement, '_trakt', side_effect=[summary, [NS(release_type='digital', release_date=OLD)]]), \
                patch.object(retirement, '_fresh', return_value=fresh):
            self.assertFalse(retirement.eligible(request, [self.movie], USER, 'target-met', 90, NOW))

    def test_recent_release_rejects_before_fresh_plex_io(self):
        summary = NS(ids=NS(trakt=1, imdb='tt1234'), status='released', released='2026-10-17')
        with patch.object(retirement, '_trakt', side_effect=[summary, [NS(release_type='digital', release_date='2026-10-17')]]), \
                patch.object(retirement, '_fresh') as fresh:
            self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))
            fresh.assert_not_called()

    def test_recent_acquisition_gate_avoids_extra_api_calls(self):
        self.movie.addedAt = NOW - 86400
        with patch.object(retirement, '_trakt') as call:
            self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'playable-after-grace', 90, NOW))
            call.assert_not_called()

    def test_pending_grab_retains_subscription_without_extra_metadata(self):
        self.movie.query = lambda: 'example.2000'
        media.session_releases['example.2000'] = [NS(grabbed_at=NOW - 60)]
        with patch.object(retirement, '_trakt') as call:
            self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))
            call.assert_not_called()

    def test_upcoming_unknown_or_recent_requests_are_not_eligible(self):
        for listed in (None, 'invalid', '2026-10-17T00:00:00Z', '2099-01-01'):
            candidate = row();candidate.listed_at = listed
            with patch.object(retirement, '_trakt') as request:
                self.assertFalse(retirement.eligible(candidate, [self.movie], USER, 'target-met', 90, NOW))
                request.assert_not_called()

    def show_data(self):
        ep = item('episode', parentIndex=1, index=1, Media=[copy_media()], first_aired=OLD, grandparentGuid='plex://show/example')
        ep2 = item('episode', number=2, parentIndex=1, index=2, Media=[copy_media()], first_aired=OLD, grandparentGuid='plex://show/example')
        show = item('show', Seasons=[NS(index=1, Episodes=[ep, ep2])], guid='plex://show/example')
        summary = NS(ids=NS(trakt=1, imdb='tt1234'), status='ended', aired_episodes=2)
        seasons = [NS(number=1, episode_count=2, aired_episodes=2,
                      episodes=[NS(number=1, first_aired=OLD), NS(number=2, first_aired=OLD)])]
        return show, summary, seasons

    def test_ended_show_requires_every_normal_episode(self):
        show, summary, seasons = self.show_data()
        with patch.object(retirement, '_trakt', side_effect=[summary, seasons]), \
                patch.object(retirement, '_fresh', side_effect=lambda x: x), \
                patch('content.identity._get', return_value=NS(MediaContainer=NS(size=2, Metadata=show.Seasons[0].Episodes))):
            self.assertTrue(retirement.eligible(row('show'), [show], USER, 'target-met', 90, NOW))
        show.Seasons[0].Episodes.pop()
        with patch.object(retirement, '_trakt', side_effect=[summary, seasons]):
            self.assertFalse(retirement.eligible(row('show'), [show], USER, 'target-met', 90, NOW))

    def test_ongoing_hiatus_future_unknown_and_partial_show_data_stay(self):
        for mode in ('returning series', 'in production', 'planned', 'hiatus', 'future', 'partial', 'unknown_date'):
            with self.subTest(mode=mode):
                show, summary, seasons = self.show_data()
                if mode in ('future', 'partial', 'unknown_date'):
                    if mode == 'future': seasons[0].episodes[1].first_aired = '2099-01-01'
                    if mode == 'partial': seasons[0].episodes.pop()
                    if mode == 'unknown_date': seasons[0].episodes[1].first_aired = None
                else: summary.status = mode
                with patch.object(retirement, '_trakt', side_effect=[summary, seasons]):
                    self.assertFalse(retirement.eligible(row('show'), [show], USER, 'target-met', 90, NOW))

    def test_show_wrong_parent_partial_batch_and_missing_copy_retain(self):
        for mode in ('wrong-parent', 'wrong-type', 'partial', 'missing', 'duplicate', 'recent-finale', 'recent-copy'):
            with self.subTest(mode=mode):
                show, summary, seasons = self.show_data()
                episodes = copy.deepcopy(show.Seasons[0].Episodes)
                if mode == 'wrong-parent': episodes[0].grandparentGuid = 'plex://show/wrong'
                if mode == 'wrong-type': episodes[0].type = 'clip'
                if mode == 'partial': episodes.pop()
                if mode == 'missing': episodes[0].Media = []
                if mode == 'duplicate': episodes[1] = copy.deepcopy(episodes[0])
                if mode == 'recent-finale': seasons[0].episodes[-1].first_aired = '2026-10-17T00:00:00Z'
                if mode == 'recent-copy': episodes[-1].addedAt = NOW - 86400
                response = NS(MediaContainer=NS(size=len(episodes), totalSize=2, Metadata=episodes))
                with patch.object(retirement, '_trakt', side_effect=[summary, seasons]), \
                        patch.object(retirement, '_fresh', side_effect=lambda x: x), \
                        patch('content.identity._get', return_value=response):
                    self.assertFalse(retirement.eligible(row('show'), [show], USER, 'target-met', 90, NOW))

    def test_recent_finale_or_cached_later_acquisition_avoids_plex_calls(self):
        for reason in ('finale', 'acquisition'):
            show, summary, seasons = self.show_data()
            if reason == 'finale': seasons[0].episodes[-1].first_aired = '2026-10-17T00:00:00Z'
            else: show.Seasons[0].Episodes[-1].addedAt = NOW - 86400
            with patch.object(retirement, '_trakt', side_effect=[summary, seasons]), \
                    patch.object(retirement, '_fresh') as fresh, patch('content.identity._get') as get:
                self.assertFalse(retirement.eligible(row('show'), [show], USER, 'target-met', 90, NOW))
                fresh.assert_not_called();get.assert_not_called()

    def test_fresh_recent_later_episode_rejects_before_any_byte_reads(self):
        show, summary, seasons = self.show_data()
        episodes = copy.deepcopy(show.Seasons[0].Episodes);episodes[-1].addedAt = NOW - 86400
        with patch.object(retirement, '_trakt', side_effect=[summary, seasons]), \
                patch.object(retirement, '_fresh', return_value=show), \
                patch('content.identity._get', return_value=NS(MediaContainer=NS(size=2, Metadata=episodes))), \
                patch.object(retirement, '_readable') as read:
            self.assertFalse(retirement.eligible(row('show'), [show], USER, 'target-met', 90, NOW))
            read.assert_not_called()

    def test_off_or_fresh_list_entry_makes_no_network_or_removal_calls(self):
        worker = monitor.Worker();worker.rows = [row()];worker.main_list_id = 11;worker.library = [self.movie]
        for policy in ('off', 'target-met'):
            with patch.object(monitor, 'retirement_policy', policy), patch.object(retirement, '_remove') as write, \
                    patch.object(retirement, 'eligible') as check:
                worker.rows[0].listed_at = '2026-10-18T00:00:00Z'
                retirement.run(worker, NOW)
                check.assert_not_called();write.assert_not_called()

    def test_removal_rechecks_exact_main_entry_timestamp_and_confirms_absence(self):
        worker = monitor.Worker();worker.rows = [row()];worker.main_list_id = 11;worker.library = [self.movie]
        with patch.object(retirement, 'eligible', return_value=True), \
                patch.object(monitor, 'pages', side_effect=[[row()], []]), \
                patch.object(retirement, '_remove', return_value=NS(deleted=NS(movies=1, shows=0))) as write, \
                patch.object(plex.session, 'delete') as delete, patch('os.remove') as remove:
            self.assertEqual(retirement.run(worker, NOW), [('movie', 1)])
            self.assertEqual(write.call_args.args[0], '/users/me/lists/11/items/remove')
            delete.assert_not_called();remove.assert_not_called()

    def test_readd_or_list_failure_prevents_removal(self):
        for result in (None, [row()]):
            if result: result[0].listed_at = '2026-10-18T00:00:00Z'
            worker = monitor.Worker();worker.rows = [row()];worker.main_list_id = 11;worker.library = [self.movie]
            with patch.object(retirement, 'eligible', return_value=True), patch.object(monitor, 'pages', return_value=result), \
                    patch.object(retirement, '_remove') as write:
                self.assertEqual(retirement.run(worker, NOW), [])
                write.assert_not_called()

    def test_failed_or_unconfirmed_removal_keeps_internal_subscription(self):
        for response, confirmation in ((None, []), (NS(deleted=NS(movies=0)), []),
                                       (NS(deleted=NS(movies=1)), None),
                                       (NS(deleted=NS(movies=1)), [row()])):
            worker = monitor.Worker();worker.rows = [row()];worker.main_list_id = 11;worker.library = [self.movie]
            reads = [[row()]] if response is None or response.deleted.movies == 0 else [[row()], confirmation]
            with patch.object(retirement, 'eligible', return_value=True), patch.object(monitor, 'pages', side_effect=reads), \
                    patch.object(retirement, '_remove', return_value=response):
                self.assertEqual(retirement.run(worker, NOW), [])
                self.assertEqual(len(worker.rows), 1)

    def test_one_old_candidate_per_poll_rotates_even_when_ineligible(self):
        worker = monitor.Worker();second = row();second.movie.ids.trakt = 2
        worker.rows = [row(), second];worker.main_list_id = 11;worker.library = [self.movie]
        visited = []
        def check(candidate, *args):visited.append(candidate.movie.ids.trakt);return False
        with patch.object(retirement, 'eligible', side_effect=check), patch.object(retirement, '_remove') as write:
            retirement.run(worker, NOW);retirement.run(worker, NOW + 600)
            write.assert_not_called()
        self.assertEqual(visited, [1, 2])

    def test_retirement_run_contains_service_errors(self):
        worker = monitor.Worker();worker.rows = [row()];worker.main_list_id = 11;worker.library = [self.movie]
        with patch.object(retirement, 'eligible', side_effect=TimeoutError), patch.object(retirement, '_remove') as write:
            self.assertEqual(retirement.run(worker, NOW), []);write.assert_not_called()

    def test_wrong_canonical_trakt_summary_id_never_retires(self):
        summary = NS(ids=NS(trakt=999, imdb='tt1234'), status='released', released='2000-01-01')
        with patch.object(retirement, '_trakt', return_value=summary):
            self.assertFalse(retirement.eligible(row(), [self.movie], USER, 'target-met', 90, NOW))

    def test_destructive_timeout_is_not_retried(self):
        with patch.object(trakt.session, 'post', side_effect=TimeoutError) as post:
            with self.assertRaises(TimeoutError): retirement._remove('/users/me/lists/11/items/remove', {}, USER)
            post.assert_called_once()

    def test_single_attempt_remove_uses_only_bound_owner_and_expected_endpoint(self):
        response = NS(status_code=200, content=b'{"deleted":{"movies":1,"shows":0}}')
        with patch.object(trakt.session, 'post', return_value=response) as post:
            result = retirement._remove('/users/me/lists/11/items/remove', {'movies': [{'ids': {'trakt': 1}}]}, USER)
        self.assertEqual(result.deleted.movies, 1)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer ' + USER[1])
        self.assertTrue(post.call_args.args[0].endswith('/users/me/lists/11/items/remove'))


if __name__ == '__main__':
    unittest.main()