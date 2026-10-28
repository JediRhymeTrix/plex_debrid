"""TorBox Usenet lifecycle: provider state is durable; every request is mocked."""
import json
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import ui
import debrid
import releases
from content.classes import media
from debrid.services import torbox
from debrid.services import torbox_usenet as usenet


TITLE = 'VisionQuest.S01E06.Deadlock.2160p.DSNP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-TEST'


def response(data, status=200):
    return NS(status_code=status, content=json.dumps(data).encode(), json=lambda: data)


def video(name=TITLE + '.mkv', number=0):
    return NS(id=number, name=name, size=5_000_000_000, mimetype='video/x-matroska', infected=False)


def job(name=TITLE, **changes):
    values = dict(id=123, name=name, hash='a' * 32, download_finished=True,
                  download_present=True, download_state='completed', files=[video()])
    values.update(changes)
    return NS(**values)


class TorBoxUsenetTests(unittest.TestCase):
    def setUp(self):
        self.release = releases.release('[newznab]', 'usenet', TITLE, [], 5.0, ['https://indexer.invalid/example.nzb'])
        self.element = NS(type='episode', parentIndex=1, index=6, grandparentEID=['imdb://tt23112594'],
                          query=lambda: 'visionquest.S01E06.', files=lambda: ['S01E06'],
                          version=NS(name='Main', rules=[]))
        self.enterContext(patch.object(torbox, 'api_key', 'not-a-secret'))
        self.enterContext(patch.object(torbox, 'ui_print'))
        self.enterContext(patch.object(debrid, 'downloading', []))
        self.enterContext(patch.object(media, 'downloaded_versions', []))
        self.enterContext(patch.object(media, 'session_releases', {}))
        self.get = self.enterContext(patch.object(torbox.session, 'get'))
        self.post = self.enterContext(patch.object(torbox.requests, 'post'))
        self.session_post = self.enterContext(patch.object(torbox.session, 'post'))
        self.real_link = usenet.request_link
        self.link = self.enterContext(patch.object(usenet, 'request_link', return_value='https://download.invalid/video.mkv'))
        self.fresh = self.enterContext(patch.object(usenet, 'detail', return_value=job()))
        self.inventory = self.enterContext(patch.object(usenet, 'inventory', return_value=[job()]))
        self.enterContext(patch.object(torbox.time, 'sleep'))

    def tearDown(self):
        for request in self.post.call_args_list + self.session_post.call_args_list:
            self.assertNotIn('/usenet/controlusenetdownload', request.args[0])

    def download(self, wanted=None):
        return torbox.download_usenet(self.release, self.element, wanted or ['S01E06'])

    def test_link_request_passes_required_query_token_without_leaking_logs(self):
        # Use the actual function beneath the patched module attribute.
        self.get.return_value = response({'success': True, 'data': 'https://download.invalid/video.mkv'})
        self.assertEqual(self.real_link(123, 0), 'https://download.invalid/video.mkv')
        self.assertEqual(self.get.call_args.kwargs['params'], {'token': 'not-a-secret', 'usenet_id': 123, 'file_id': 0})
        self.assertEqual(self.get.call_args.kwargs['timeout'], 20)

    def test_complete_existing_release_is_reused_without_nzb_fetch_or_submission(self):
        self.assertTrue(self.download())
        self.get.assert_not_called();self.post.assert_not_called()
        self.assertEqual(len(self.release.files), 1)
        self.assertEqual(self.release.files[0].match, 'S01E06')

    def test_link_api_failure_keeps_needed_job_and_defers_fallback(self):
        self.link.return_value = None
        with self.assertRaises(debrid.Deferred): self.download()
        self.post.assert_not_called()
        self.assertEqual(debrid.downloading, [])
        self.assertEqual(media.downloaded_versions, [])

    def test_pending_existing_job_does_not_claim_success_or_create_duplicate(self):
        self.fresh.return_value = job(download_finished=False, download_present=False, download_state='downloading', files=[])
        with self.assertRaises(debrid.Deferred): self.download()
        self.link.assert_not_called();self.post.assert_not_called()
        self.assertEqual(debrid.downloading, [])

    def test_pending_then_complete_is_reconstructed_on_next_attempt(self):
        self.fresh.return_value = job(download_finished=False, download_state='downloading', files=[])
        with self.assertRaises(debrid.Deferred): self.download()
        self.fresh.return_value = job()
        self.assertTrue(self.download())
        self.post.assert_not_called()

    def test_unknown_or_incomplete_file_inventory_never_deletes(self):
        for changes in ({'files': []}, {'files': None}, {'download_present': False}, {'files': [NS(id=0)]}):
            with self.subTest(changes=changes):
                self.fresh.return_value = job(**changes)
                with self.assertRaises(debrid.Deferred): self.download()

    def test_only_wanted_videos_get_links_not_samples_subtitles_or_other_episodes(self):
        self.fresh.return_value = job(files=[video(), video('sample.' + TITLE + '.mkv', 1),
                                            video('other.S01E07.mkv', 2), video(TITLE + '.srt', 3)])
        self.assertTrue(self.download())
        self.link.assert_called_once_with(123, 0)

    def test_all_required_episode_patterns_must_be_covered(self):
        with self.assertRaises(debrid.Deferred): self.download(['S01E06', 'S01E07'])

    def test_duplicate_file_ids_cannot_claim_multiple_episode_coverage(self):
        self.fresh.return_value = job(files=[video('VisionQuest.S01E06.mkv', 0), video('VisionQuest.S01E07.mkv', 0)])
        with self.assertRaises(debrid.Deferred): self.download(['S01E06', 'S01E07'])
        self.link.assert_not_called()

    def test_legacy_wrong_episode_is_preserved_not_ours_to_delete(self):
        self.fresh.return_value = job(files=[video('Wrong.S01E07.mkv')])
        self.assertFalse(self.download())

    def test_inventory_outage_blocks_duplicate_submission(self):
        self.inventory.side_effect = debrid.Deferred('inventory unavailable')
        with self.assertRaises(debrid.Deferred): self.download()
        self.post.assert_not_called()

    def test_new_submission_has_stable_provider_ownership_and_no_polling_sleep(self):
        self.inventory.return_value = []
        self.get.return_value = NS(status_code=200, content=b'<nzb>' + b'x' * 200 + b'</nzb>')
        self.post.return_value = response({'success': True, 'data': {'usenetdownload_id': 123}})
        from debrid.services.torbox_usenet import marker
        self.fresh.return_value = job(name=marker(self.release, self.element))
        self.assertTrue(self.download())
        self.assertEqual(self.post.call_args.kwargs['data']['name'], marker(self.release, self.element))
        torbox.time.sleep.assert_not_called()

    def test_create_timeout_is_not_retried_and_no_fallback_is_claimed_successful(self):
        self.inventory.return_value = []
        self.get.return_value = NS(status_code=200, content=b'<nzb>' + b'x' * 200 + b'</nzb>')
        self.post.side_effect = TimeoutError
        with self.assertRaises(debrid.Deferred): self.download()
        self.post.assert_called_once()

    def test_provider_inventory_duplicate_identities_are_not_guessed(self):
        self.inventory.return_value = [job(), job(id=124)]
        with self.assertRaises(debrid.Deferred): self.download()
        self.post.assert_not_called()

    def test_failed_or_wrong_marked_jobs_are_never_deleted_and_not_resubmitted(self):
        name = usenet.marker(self.release, self.element)
        self.inventory.return_value = [job(name=name)]
        for changes in ({'download_state': 'failed', 'download_finished': False}, {'files': [video('Wrong.S01E07.mkv')]}):
            with self.subTest(changes=changes):
                self.fresh.return_value = job(name=name, **changes)
                self.assertFalse(self.download())
        self.post.assert_not_called()

    def test_partial_infected_folder_only_and_longer_episode_tokens_never_claim_success(self):
        for name in ('Other.S01E060.mkv', 'Folder.S01E06/Other.S01E07.mkv', TITLE + '.partial.mkv'):
            with self.subTest(name=name):
                self.fresh.return_value = job(files=[video(name)])
                try: result = self.download()
                except debrid.Deferred: result = False
                self.assertFalse(result)
        infected = video();infected.infected = True
        self.fresh.return_value = job(files=[infected])
        with self.assertRaises(debrid.Deferred): self.download()

    def test_actual_file_requirements_are_rechecked_before_links(self):
        self.element.version.rules = [['file sizes', 'requirement', 'video files >=', '10']]
        self.assertFalse(self.download())
        self.link.assert_not_called()

    def test_actual_size_must_improve_existing_copy_not_just_indexed_size(self):
        self.element._monitor_managed = True
        self.element.version = releases.sort.version('Main', [], 'en', [['size', 'preference', 'highest', '']])
        old = releases.release('', 'torrent', TITLE, [], 6.0, [])
        with patch('content.quality.existing', return_value=[old]):
            self.assertFalse(self.download())
        self.link.assert_not_called()

    def test_changed_top_candidate_does_not_duplicate_pending_owned_request(self):
        earlier = copy.deepcopy(self.release);earlier.title = TITLE.replace('-TEST', '-EARLIER')
        self.inventory.return_value = [job(name=usenet.marker(earlier, self.element))]
        self.fresh.return_value = job(name=usenet.marker(earlier, self.element), download_finished=False,
                                      download_present=False, download_state='downloading', files=[])
        with self.assertRaises(debrid.Deferred): self.download()
        self.post.assert_not_called()

    def test_provider_title_truncation_with_intact_token_reuses_job(self):
        name = usenet.marker(self.release, self.element).split(' ', 1)[0] + ' truncated'
        self.inventory.return_value = [job(name=name)]
        self.fresh.return_value = job(name=name)
        self.assertTrue(self.download())
        self.post.assert_not_called()

    def test_missing_indexer_result_can_recover_completed_owned_release(self):
        earlier = copy.deepcopy(self.release);earlier.title = TITLE.replace('-TEST', '-EARLIER')
        name = usenet.marker(earlier, self.element)
        self.inventory.return_value = [job(name=name)]
        self.fresh.return_value = job(name=name)
        self.assertTrue(self.download())
        self.assertEqual(self.release.title, earlier.title)
        self.post.assert_not_called()

    def test_preflight_prevents_torrent_fallback_while_owned_usenet_is_pending(self):
        name = usenet.marker(self.release, self.element)
        self.inventory.return_value = [job(name=name)]
        self.fresh.return_value = job(name=name, download_finished=False, download_present=False)
        with self.assertRaises(debrid.Deferred): usenet.preflight(self.element)
        self.post.assert_not_called()

    def test_preflight_keeps_complete_but_uncertain_file_inventory_from_racing_fallback(self):
        name = usenet.marker(self.release, self.element)
        self.inventory.return_value = [job(name=name)]
        for changes in ({'files': []}, {'files': None}, {'infected': True}):
            self.fresh.return_value = job(name=name, **changes)
            with self.assertRaises(debrid.Deferred): usenet.preflight(self.element)
        self.post.assert_not_called()

    def test_real_provider_get_redacts_response_and_exception_credentials(self):
        secret = 'token-not-for-logs'
        self.get.side_effect = RuntimeError('https://provider.invalid?token=' + secret)
        with self.assertRaises(debrid.Deferred): usenet.provider_get('/usenet/mylist')
        self.assertNotIn(secret, str(torbox.ui_print.call_args_list))

class UsenetHelperTests(unittest.TestCase):
    def test_inventory_pagination_and_incomplete_page_fail_closed(self):
        first = [{'id': i, 'name': 'job'} for i in range(1000)]
        with patch.object(usenet, 'provider_get', side_effect=[first, [{'id': 1001, 'name': 'last'}]]) as get:
            self.assertEqual(len(usenet.inventory()), 1001)
            self.assertIn('offset=1000', get.call_args.args[0])
        for value in (None, {}, [{'id': 1}], [{'id': 1, 'name': 'x'}, {'id': 1, 'name': 'x'}]):
            with self.subTest(value=value), patch.object(usenet, 'provider_get', return_value=value), patch.object(torbox, 'ui_print'):
                with self.assertRaises(debrid.Deferred): usenet.inventory()

    def test_link_failure_status_invalid_json_or_non_url_never_leaks(self):
        for result in (response({}, 422), response({'success': False, 'data': 'https://bad.invalid'}),
                       response({'success': True, 'data': 'not-a-url'})):
            with patch.object(torbox.session, 'get', return_value=result), patch.object(torbox, 'ui_print'):
                self.assertIsNone(usenet.request_link(1, 0))

    def test_detail_must_return_requested_id(self):
        with patch.object(usenet, 'provider_get', return_value={'id': 999, 'name': TITLE}), patch.object(torbox, 'ui_print'):
            with self.assertRaises(debrid.Deferred): usenet.detail(123)

    def test_deferral_does_not_mark_downloaded_or_retry_second_candidate_in_same_leaf(self):
        from test_quality import video as library_episode
        item = library_episode();item._monitor_managed = True;item.bitrate = lambda: None
        candidate = releases.release('', 'usenet', TITLE, [], 5, [])
        candidate.cached = ['TB'];item.Releases = [candidate, copy.deepcopy(candidate)]
        version = releases.sort.version('Main', [], 'en', [])
        with patch.object(item, 'versions', return_value=[version]), patch.object(debrid, 'check'), \
                patch.object(debrid, 'download', side_effect=debrid.Deferred('pending')) as grab, \
                patch.object(item, 'downloaded') as downloaded, patch('content.quality.existing', return_value=[]):
            self.assertEqual(item.debrid_download(), (False, True))
            self.assertEqual(item.debrid_download(), (False, True))
            grab.assert_called_once();downloaded.assert_not_called()
            self.assertTrue(item._monitor_retry)

    def test_episode_parent_deferral_skips_second_scrape_but_next_attempt_resets(self):
        from test_quality import video as library_episode
        import scraper
        item = library_episode();item.isanime = lambda: False
        item.deviation = lambda: '.*';item.alternate_titles = ['War']
        candidate = releases.release('', 'usenet', 'War.S01E03.2160p.DV.HDR', [], 5, [])
        calls = []
        def attempt(*args, **kwargs):
            calls.append(item._debrid_deferred)
            item._debrid_deferred = True
            return False, True
        with patch.object(item, 'debrid_download', side_effect=attempt), patch.object(scraper, 'scrape') as scrape:
            self.assertEqual(item.download(parentReleases=[candidate]), (False, True))
            self.assertEqual(item.download(parentReleases=[candidate]), (False, True))
            self.assertEqual(calls, [False, False])
            scrape.assert_not_called()

    def test_pending_preflight_precedes_all_providers_not_only_torbox(self):
        from test_quality import video as library_episode
        import scraper
        item = library_episode();item.Releases = []
        with patch.object(debrid.services, 'active', ['Real Debrid', 'TorBox']), \
                patch.object(scraper.services, 'active', ['torrentio', 'newznab']), \
                patch.object(usenet, 'preflight', side_effect=debrid.Deferred('pending')), \
                patch.object(debrid, 'check') as check, patch.object(debrid, 'download') as download:
            self.assertEqual(item.debrid_download(), (False, True))
            check.assert_not_called();download.assert_not_called()

    def test_legacy_pending_work_does_not_consume_ignore_retry_budget(self):
        from test_quality import video as library_episode
        from content.classes import ignore
        item = library_episode();item._debrid_deferred = True
        with patch.object(media, 'ignore_queue', []), patch.object(ignore, 'add') as add:
            for _ in range(60): item.watch()
            self.assertEqual(media.ignore_queue, [])
            add.assert_not_called()

    def test_request_identity_survives_movie_year_or_title_enrichment(self):
        item = NS(type='movie', EID=['imdb://tt1234'], query=lambda: 'old.2026')
        before = usenet.request_prefix(item)
        item.query = lambda: 'renamed.2024'
        self.assertEqual(usenet.request_prefix(item), before)
        first = NS(type='season', parentEID=['imdb://tt1234'], index=1)
        second = NS(type='season', parentEID=['imdb://tt1234'], index=2)
        self.assertNotEqual(usenet.request_prefix(first), usenet.request_prefix(second))

    def test_incomplete_pagination_never_returns_partial_inventory(self):
        full = [{'id': i, 'name': 'job'} for i in range(1000)]
        with patch.object(usenet, 'provider_get', side_effect=[full, None]), patch.object(torbox, 'ui_print'):
            with self.assertRaises(debrid.Deferred): usenet.inventory()


if __name__ == '__main__':
    unittest.main()