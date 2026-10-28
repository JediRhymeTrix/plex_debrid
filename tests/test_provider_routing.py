"""Provider preference and actual cached availability; all provider I/O is mocked."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import ui  # Legacy package import order matches main.py; never load live settings.
import debrid
import releases
from debrid.services import realdebrid as rd, torbox as tb


HASH = 'a' * 40
TITLE = 'Example.S01E01.2160p.WEB-DL'
MAGNET = 'magnet:?xt=urn:btih:' + HASH + '&dn=Example'


def candidate(cached=(), kind='torrent'):
    row = releases.release('[test]', kind, TITLE, [], 4, [MAGNET])
    row.cached = list(cached)
    return row


def element(row=None, wanted=None):
    return NS(Releases=[row or candidate()], files=lambda: wanted or ['S01E01'],
              deviation=lambda: '.*', query=lambda: 'example.S01E01.',
              version=NS(name='Main', rules=[]))


def rd_job(**changes):
    values = dict(id='old', hash=HASH, status='downloaded', filename=TITLE,
                  files=[NS(id=1, path=TITLE + '.mkv', bytes=4_000_000_000, selected=1)],
                  links=['https://rd.invalid/file'])
    values.update(changes)
    return NS(**values)


def tb_job(**changes):
    values = dict(id=7, hash=HASH, download_state='cached', download_present=True,
                  name=TITLE, files=[NS(id=1, name=TITLE + '.mkv', size=4_000_000_000)])
    values.update(changes)
    return NS(**values)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.rd = self.provider('RD')
        self.tb = self.provider('TB')
        self.enterContext(patch.object(debrid.services, 'get', return_value=[self.tb, self.rd]))
        self.enterContext(patch.object(debrid, 'tracker', []))

    def provider(self, short, result=True):
        def grab(item, **kwargs):
            self.calls.append((short, kwargs['stream']))
            return result
        return NS(short=short, download=Mock(side_effect=grab))

    def test_both_cached_prefers_rd_even_when_active_order_tb_first(self):
        self.assertTrue(debrid.download(element(candidate(['TB', 'RD']))))
        self.assertEqual(self.calls, [('RD', True)])

    def test_tb_only_cached_does_not_attempt_unknown_rd_first(self):
        self.assertTrue(debrid.download(element(candidate(['TB']))))
        self.assertEqual(self.calls, [('TB', True)])

    def test_rd_unknown_tb_absent_is_still_probed_without_manufacturing_cache(self):
        row = candidate()
        self.assertTrue(debrid.download(element(row)))
        self.assertEqual(self.calls, [('RD', True)])
        self.assertEqual(row.cached, [])

    def test_rd_refusal_falls_through_to_tb_cached_or_probe(self):
        for cached in (['RD', 'TB'], ['RD']):
            with self.subTest(cached=cached):
                self.calls.clear()
                self.rd.download.side_effect = lambda item, **kw: self.calls.append(('RD', kw['stream'])) or False
                self.assertTrue(debrid.download(element(candidate(cached))))
                self.assertEqual(self.calls, [('RD', True), ('TB', True)])

    def test_uncached_failure_falls_through_regardless_of_cache_list(self):
        self.rd.download.side_effect = lambda item, **kw: self.calls.append(('RD', kw['stream'])) or False
        self.assertTrue(debrid.download(element(candidate(['RD'])), stream=False))
        self.assertEqual(self.calls, [('RD', False), ('TB', False)])

    def test_usenet_never_calls_rd_even_with_bad_cached_fact(self):
        self.assertTrue(debrid.download(element(candidate(['RD', 'TB'], 'usenet'))))
        self.rd.download.assert_not_called()

    def test_tracker_route_filters_without_fabricating_cache(self):
        row = candidate(['RD'])
        with patch.object(debrid, 'tracker', [['test', ['TB']]]):
            item = element(row)
            self.assertTrue(debrid.download(item))
            self.assertEqual(self.calls, [('TB', True)])
            self.assertEqual(item.Releases[0].cached, ['RD'])

    def test_explicit_route_order_is_honored(self):
        with patch.object(debrid, 'tracker', [['test', ['TB', 'RD']]]):
            self.assertTrue(debrid.download(element(candidate(['RD', 'TB']))))
            self.assertEqual(self.calls, [('TB', True)])

    def test_third_provider_order_and_cache_eligibility_are_preserved(self):
        other = self.provider('AD')
        with patch.object(debrid.services, 'get', return_value=[other, self.tb, self.rd]):
            self.assertTrue(debrid.download(element(candidate(['AD', 'RD', 'TB']))))
            self.assertEqual(self.calls, [('AD', True)])
        self.calls.clear()
        with patch.object(debrid.services, 'get', return_value=[other]):
            self.assertFalse(debrid.download(element(candidate())))
            self.assertTrue(debrid.download(element(candidate()), stream=False))
            self.assertEqual(self.calls, [('AD', False)])

    def test_failed_provider_mutation_does_not_poison_fallback(self):
        def refuse(item, **kw):
            item.Releases[0].download = []
            item.Releases[0].files = ['bad']
            return False
        self.rd.download.side_effect = refuse
        def accept(item, **kw):
            self.assertEqual(item.Releases[0].download, [MAGNET])
            self.assertEqual(item.Releases[0].files, [])
            return True
        self.tb.download.side_effect = accept
        self.assertTrue(debrid.download(element(candidate(['RD', 'TB']))))

    def test_deferred_stops_fallback_and_success_bookkeeping(self):
        self.rd.download.side_effect = debrid.Deferred('pending')
        item = element(candidate(['RD', 'TB']))
        with self.assertRaises(debrid.Deferred):
            debrid.download(item)
        self.tb.download.assert_not_called()
        self.assertFalse(getattr(item, 'downloaded_releases', []))

    def test_other_cached_fact_does_not_exclude_rd_tb_uncached_fallback(self):
        self.rd.download.return_value = False
        self.rd.download.side_effect = lambda item, **kw: False
        self.assertTrue(debrid.download(element(candidate(['AD'])), stream=False))
        self.tb.download.assert_called_once()

    def test_legacy_uncached_third_provider_is_not_expanded_when_other_cache_known(self):
        other = self.provider('AD')
        with patch.object(debrid.services, 'get', return_value=[other]):
            self.assertFalse(debrid.download(element(candidate(['PM'])), stream=False))
            other.download.assert_not_called()

    def test_usenet_uncached_route_still_never_calls_rd(self):
        self.assertTrue(debrid.download(element(candidate([], 'usenet')), stream=False))
        self.rd.download.assert_not_called()

    def test_route_limits_both_phases_even_after_rd_refusal(self):
        self.rd.download.side_effect = lambda *args, **kwargs: False
        with patch.object(debrid, 'tracker', [['test', ['RD']]]):
            self.assertFalse(debrid.download(element(candidate(['TB', 'RD']))))
            self.assertFalse(debrid.download(element(candidate()), stream=False))
        self.tb.download.assert_not_called()

    def test_temporary_inventory_scope_does_not_leak_on_success_or_deferred(self):
        item = element(candidate(['RD']))
        self.assertTrue(debrid.download(item))
        self.assertFalse(hasattr(item, '_provider_inventory'))
        self.rd.download.side_effect = debrid.Deferred('pending')
        with self.assertRaises(debrid.Deferred): debrid.download(item)
        self.assertFalse(hasattr(item, '_provider_inventory'))

    def test_explicit_inventory_scope_is_preserved_across_candidate_calls(self):
        item = element(candidate(['RD'])); item._provider_inventory = {'RD': []}
        self.assertTrue(debrid.download(item))
        self.assertEqual(item._provider_inventory, {'RD': []})


class RealDebridTests(unittest.TestCase):
    def setUp(self):
        self.row = candidate()
        self.item = element(self.row)
        self.enterContext(patch.object(rd, 'ui_print'))
        self.get = self.enterContext(patch.object(rd, 'get', return_value=[]))
        self.post = self.enterContext(patch.object(rd, 'post', return_value=NS(download='https://direct.invalid/video.mkv')))
        self.delete = self.enterContext(patch.object(rd, 'delete', return_value=True))
        self.enterContext(patch.object(rd.time, 'sleep'))
        self.enterContext(patch.object(debrid, 'downloading', []))

    def existing(self, job=None):
        job = job or rd_job()
        self.get.side_effect = lambda url: job if '/info/' in url else [job]
        return job

    def created(self, job=None):
        self.row.cached = ['RD']
        job = job or rd_job(id='new')
        self.get.side_effect = lambda url: job if '/info/' in url else []
        self.post.side_effect = lambda url, data: NS(id='new') if '/addMagnet' in url else NS(download='https://direct.invalid/video.mkv')
        return job

    def test_unknown_cache_only_probes_inventory_does_not_add(self):
        self.assertFalse(rd.download(self.item))
        self.post.assert_not_called()
        self.assertEqual(self.row.cached, [])

    def test_unknown_cache_reuses_existing_downloaded_job_and_validates_links(self):
        self.existing()
        self.assertTrue(rd.download(self.item))
        self.assertEqual(self.row.download, ['https://direct.invalid/video.mkv'])
        self.assertEqual(len(self.row.files), 1)
        self.assertTrue(all('/unrestrict/link' in call.args[0] for call in self.post.call_args_list))
        self.delete.assert_not_called()

    def test_existing_pending_job_is_deferred_without_delete_or_add(self):
        self.existing(rd_job(status='downloading', links=[]))
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.post.assert_not_called(); self.delete.assert_not_called()
        self.assertEqual(debrid.downloading, [])

    def test_explicit_infringing_refusal_is_false(self):
        self.created()
        self.post.side_effect = None
        self.post.return_value = NS(error='infringing_torrent', error_code=35)
        self.assertFalse(rd.download(self.item))
        self.delete.assert_not_called()

    def test_new_cached_probe_that_is_actually_pending_is_cleaned_not_success(self):
        self.created(rd_job(id='new', status='downloading', links=[]))
        self.assertFalse(rd.download(self.item))
        self.delete.assert_called_once()
        self.assertEqual(debrid.downloading, [])

    def test_downloaded_status_required_even_if_link_count_matches(self):
        self.created(rd_job(id='new', status='queued'))
        self.assertFalse(rd.download(self.item))
        self.delete.assert_called_once()

    def test_existing_missing_links_deferred_and_retained(self):
        for links in (None, [], ['']):
            with self.subTest(links=links):
                self.existing(rd_job(links=links))
                with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()

    def test_missing_unrestricted_link_cannot_silently_succeed(self):
        self.created()
        self.post.side_effect = lambda url, data: NS(id='new') if '/addMagnet' in url else None
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()
        self.assertEqual(self.row.download, [MAGNET])

    def test_every_selected_link_must_work_before_committing_release(self):
        job = self.existing(rd_job(files=[
            NS(id=1, path=TITLE + '.mkv', bytes=4_000_000_000, selected=1),
            NS(id=2, path=TITLE + '.mp4', bytes=4_000_000_000, selected=1)],
            links=['https://rd.invalid/one', 'https://rd.invalid/two']))
        self.post.side_effect = [NS(download='https://direct.invalid/one'), None]
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.assertEqual(self.row.download, [MAGNET]); self.delete.assert_not_called()

    def test_uncached_accepted_pending_is_deferred_not_completed(self):
        self.created(rd_job(id='new', status='queued', links=[]))
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.delete.assert_not_called()
        self.assertEqual(debrid.downloading, [])

    def test_uncached_refused_returns_false_for_tb_fallback(self):
        self.post.return_value = NS(error='permission_denied', error_code=13)
        self.assertFalse(rd.download(self.item, stream=False))

    def test_uncertain_submission_defers_instead_of_duplicate_fallback(self):
        self.post.return_value = None
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.post.assert_called_once()

    def test_failed_cached_cleanup_defers_instead_of_orphaning(self):
        self.created(rd_job(id='new', status='queued', links=[]))
        self.delete.return_value = False
        with self.assertRaises(debrid.Deferred): rd.download(self.item)

    def test_inventory_failure_never_adds_duplicate(self):
        self.get.return_value = None
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.post.assert_not_called()

    def test_usenet_is_unsupported_even_when_called_directly(self):
        self.row.type = 'usenet'
        self.assertFalse(rd.download(self.item, stream=False))
        self.get.assert_not_called(); self.post.assert_not_called()

    def test_disabled_cache_checker_unknown_is_not_negative_or_assumed_cached(self):
        self.get.return_value = NS(error='disabled_endpoint', error_code=37)
        rd.check(self.item)
        self.assertNotIn('RD', self.row.cached)
        self.assertEqual(self.row.provider_cache['RD'], 'unknown')

    def test_known_negative_is_distinct_from_disabled_endpoint(self):
        self.get.return_value = NS(**{HASH: NS(rd=[])})
        rd.check(self.item)
        self.assertEqual(self.row.provider_cache['RD'], 'uncached')

    def test_check_preserves_usenet_and_other_provider_metadata(self):
        usenet = candidate(['TB'], 'usenet'); usenet.files = ['keep']
        self.item.Releases.append(usenet)
        self.get.return_value = NS(error='disabled_endpoint', error_code=37)
        rd.check(self.item)
        self.assertEqual(usenet.files, ['keep'])
        self.assertEqual(usenet.cached, ['TB'])

    def test_existing_unrestrict_refusal_allows_fallback_without_deleting(self):
        self.existing()
        self.post.return_value = NS(error='infringing_torrent', error_code=35)
        self.assertFalse(rd.download(self.item))
        self.delete.assert_not_called()

    def test_info_explicit_refusal_never_deletes_unverified_identity(self):
        self.created()
        self.get.side_effect = lambda url: NS(error='infringing_torrent', error_code=35) if '/info/' in url else []
        self.assertFalse(rd.download(self.item))
        self.delete.assert_not_called()

    def test_existing_identity_change_is_never_deleted(self):
        for changed in (rd_job(hash='b' * 40), rd_job(id='different')):
            self.get.side_effect = lambda url, row=changed: row if '/info/' in url else [rd_job()]
            with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()

    def test_malformed_inventory_is_not_authority_to_create(self):
        self.get.return_value = [NS(id='oops')]
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.post.assert_not_called()

    def test_inventory_page_bound_is_finite(self):
        self.get.return_value = [rd_job(hash='b' * 40)] * 100
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.assertEqual(self.get.call_count, 10)
        self.post.assert_not_called()

    def test_incomplete_inventory_page_never_starts_uncached(self):
        self.get.side_effect = [[rd_job(hash='b' * 40)] * 100, None]
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.post.assert_not_called()

    def test_actual_file_quality_rejection_happens_before_links(self):
        self.existing()
        self.item.version.rules = [['size', 'requirement', '>=', '5']]
        self.assertFalse(rd.download(self.item))
        self.post.assert_not_called(); self.delete.assert_not_called()
        self.assertEqual(self.row.download, [MAGNET])

    def test_actual_ready_cache_satisfies_cached_requirement_without_assumed_cache(self):
        self.existing()
        self.item.version.rules = [['cache status', 'requirement', 'cached', '']]
        self.assertTrue(rd.download(self.item))
        self.assertEqual(self.row.cached, ['RD'])

    def test_actual_file_facts_recorded_without_provider_rename(self):
        self.existing(rd_job(filename='Unrelated.provider.rename'))
        self.assertTrue(rd.download(self.item))
        self.assertEqual(self.row.title, TITLE)
        self.assertEqual(self.row.size, 4)
        self.assertEqual(self.row.wanted, 1)

    def test_duplicate_or_invalid_file_metadata_is_deferred(self):
        for files in ([NS(id=1, path=TITLE + '.mkv', bytes=float('nan'), selected=1)],
                      [NS(id=1, path=TITLE + '.mkv', bytes=-1, selected=1)],
                      [NS(id=1, path=TITLE + '.mkv', bytes=10, selected=1)] * 2):
            self.existing(rd_job(files=files))
            with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()

    def test_missing_wanted_episode_coverage_is_not_success(self):
        self.existing(); self.item.files = lambda: ['S01E01', 'S01E02']
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.post.assert_not_called()

    def test_failed_existing_status_false_not_success(self):
        self.existing(rd_job(status='magnet_error'))
        self.assertFalse(rd.download(self.item, stream=False))
        self.post.assert_not_called(); self.delete.assert_not_called()

    def test_disabled_checker_does_not_preserve_old_positive(self):
        self.row.cached = ['RD', 'TB']
        self.get.return_value = NS(error='disabled_endpoint', error_code=37)
        rd.check(self.item)
        self.assertEqual(self.row.cached, ['TB'])
        self.assertEqual(self.row.provider_cache['RD'], 'unknown')

    def test_positive_checker_then_status_refusal_is_not_download_success(self):
        self.get.return_value = NS(**{HASH: NS(rd=[NS(**{'1': NS(filename=TITLE + '.mkv', filesize=4_000_000_000)})])})
        rd.check(self.item)
        self.assertEqual(self.row.cached, ['RD'])
        self.assertEqual(self.row.provider_cache['RD'], 'cached')
        self.created(rd_job(id='new', status='magnet_error'))
        self.assertFalse(rd.download(self.item))
        self.delete.assert_called_once()

    def test_created_info_unavailable_is_retained_and_deferred(self):
        self.created(); self.get.side_effect = lambda url: None if '/info/' in url else []
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()

    def test_created_incomplete_links_never_delete_ready_wanted_job(self):
        self.created(rd_job(id='new', links=None))
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()

    def test_created_unrestrict_refusal_falls_through_but_keeps_ready_job(self):
        self.created()
        self.post.side_effect = lambda url, data: (NS(id='new') if '/addMagnet' in url else
                                                   NS(error='infringing_torrent') if '/unrestrict/' in url else NS())
        self.assertFalse(rd.download(self.item))
        self.delete.assert_not_called()

    def test_unknown_provider_state_is_not_a_delete_verdict(self):
        self.created(rd_job(id='new', status='future_state', links=[]))
        with self.assertRaises(debrid.Deferred): rd.download(self.item)
        self.delete.assert_not_called()

    def test_scoped_inventory_reused_but_detail_is_fresh_each_candidate(self):
        self.existing(); self.item._provider_inventory = {}
        self.assertTrue(rd.download(self.item)); self.assertTrue(rd.download(self.item))
        self.assertEqual(sum('/torrents?' in c.args[0] for c in self.get.call_args_list), 1)
        self.assertEqual(sum('/info/' in c.args[0] for c in self.get.call_args_list), 2)

    def test_submission_invalidates_inventory_snapshot(self):
        self.created(rd_job(id='new', status='queued', links=[]))
        self.item._provider_inventory = {}
        with self.assertRaises(debrid.Deferred): rd.download(self.item, stream=False)
        self.assertNotIn('RD', self.item._provider_inventory)

    def test_inventory_is_fresh_without_explicit_scope(self):
        self.assertFalse(rd.download(self.item)); self.assertFalse(rd.download(self.item))
        self.assertEqual(self.get.call_count, 2)


class TorBoxTorrentTests(unittest.TestCase):
    def setUp(self):
        self.row = candidate(['TB']); self.item = element(self.row)
        self.enterContext(patch.object(tb, 'ui_print'))
        self.get = self.enterContext(patch.object(tb, 'get', return_value=NS(success=True, data=[])))
        self.post = self.enterContext(patch.object(tb, 'post', return_value=NS(success=True, data=NS(torrent_id=8))))
        self.detail = self.enterContext(patch.object(tb, 'get_torrent', return_value=tb_job(id=8)))
        self.link = self.enterContext(patch.object(tb, 'requestdl', return_value='https://tb.invalid/video.mkv'))
        self.delete = self.enterContext(patch.object(tb, 'delete', return_value=True))
        self.enterContext(patch.object(debrid, 'downloading', []))

    def existing(self, job=None):
        job = job or tb_job()
        self.get.return_value = NS(success=True, data=[job])
        self.detail.return_value = job
        return job

    def test_cached_success_requires_wanted_links(self):
        self.assertTrue(tb.download(self.item))
        self.assertEqual(self.row.download, ['https://tb.invalid/video.mkv'])

    def test_no_wanted_link_never_claims_success_or_deletes_needed_job(self):
        self.existing(); self.link.return_value = None
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.post.assert_not_called(); self.delete.assert_not_called()
        self.assertEqual(self.row.download, [MAGNET])

    def test_partial_links_defer_without_committing_partial_result(self):
        self.existing(tb_job(files=[NS(id=1, name=TITLE + '.mkv', size=100),
                                    NS(id=2, name=TITLE + '.mp4', size=100)]))
        self.link.side_effect = ['https://tb.invalid/one', None]
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.assertEqual(self.row.download, [MAGNET]); self.delete.assert_not_called()

    def test_existing_pending_job_retained_and_deferred(self):
        self.existing(tb_job(download_state='downloading', download_present=False))
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.post.assert_not_called(); self.delete.assert_not_called()
        self.assertEqual(debrid.downloading, [])

    def test_stale_cached_new_job_is_cleaned_not_accepted_as_uncached(self):
        self.detail.return_value = tb_job(id=8, download_state='downloading', download_present=False)
        self.assertFalse(tb.download(self.item))
        self.delete.assert_called_once_with(8)
        self.assertEqual(debrid.downloading, [])

    def test_unknown_cache_probe_does_not_create(self):
        self.row.cached = []
        self.assertFalse(tb.download(self.item))
        self.post.assert_not_called()

    def test_existing_job_is_reused_not_created_or_deleted_on_info_failure(self):
        self.existing(); self.detail.return_value = None
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.post.assert_not_called(); self.delete.assert_not_called()

    def test_known_no_wanted_files_never_deletes_preexisting_job(self):
        self.existing(tb_job(files=[NS(id=1, name='Other.S09E09.mkv', size=100)]))
        self.assertFalse(tb.download(self.item))
        self.delete.assert_not_called()

    def test_created_no_wanted_files_keeps_existing_torrent_probe_cleanup(self):
        self.detail.return_value = tb_job(id=8, files=[NS(id=1, name='Other.S09E09.mkv', size=100)])
        self.assertFalse(tb.download(self.item))
        self.delete.assert_called_once_with(8)

    def test_usenet_dispatch_preserves_existing_deferred_safety(self):
        self.row.type = 'usenet'
        with patch.object(tb, 'download_usenet', side_effect=debrid.Deferred('pending')):
            with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.get.assert_not_called(); self.post.assert_not_called(); self.delete.assert_not_called()

    def test_created_needed_job_with_missing_link_retained_for_retry(self):
        self.link.return_value = None
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.delete.assert_not_called()
        self.assertEqual(self.row.download, [MAGNET])

    def test_uncached_pending_returns_deferred_not_success(self):
        self.row.cached = []
        self.detail.return_value = tb_job(id=8, download_state='downloading', download_present=False)
        with self.assertRaises(debrid.Deferred): tb.download(self.item, stream=False)
        self.delete.assert_not_called(); self.assertEqual(debrid.downloading, [])

    def test_uncertain_submission_never_blindly_retries(self):
        self.post.return_value = None
        with self.assertRaises(debrid.Deferred): tb.download(self.item, stream=False)
        self.post.assert_called_once(); self.delete.assert_not_called()

    def test_hash_or_id_change_never_deletes_unknown_identity(self):
        self.existing()
        for changed in (tb_job(hash='b' * 40), tb_job(id=9)):
            self.detail.return_value = changed
            with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.delete.assert_not_called()

    def test_failed_cleanup_defers_instead_of_orphaning_probe(self):
        self.detail.return_value = tb_job(id=8, download_state='downloading', download_present=False)
        self.delete.return_value = False
        with self.assertRaises(debrid.Deferred): tb.download(self.item)

    def test_file_quality_rejection_never_deletes_preexisting_needed_job(self):
        self.existing(); self.item.version.rules = [['size', 'requirement', '>=', '5']]
        self.assertFalse(tb.download(self.item))
        self.link.assert_not_called(); self.delete.assert_not_called()

    def test_file_metadata_and_wanted_coverage_are_required(self):
        for changes in (dict(files=None), dict(files=[NS(id=1, name=TITLE + '.mkv', size=float('nan'))]),
                        dict(files=[NS(id=1, name=TITLE + '.mkv', size=100)] * 2)):
            self.existing(tb_job(**changes))
            with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.delete.assert_not_called()

    def test_wanted_coverage_incomplete_is_not_success(self):
        self.existing(); self.item.files = lambda: ['S01E01', 'S01E02']
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.link.assert_not_called()

    def test_inventory_page_bound_is_finite(self):
        self.get.return_value = NS(success=True, data=[tb_job(hash='b' * 40)] * 1000)
        with self.assertRaises(debrid.Deferred): tb.download(self.item, stream=False)
        self.assertEqual(self.get.call_count, 10)
        self.post.assert_not_called()

    def test_incomplete_inventory_does_not_create_duplicate(self):
        self.get.side_effect = [NS(success=True, data=[tb_job(hash='b' * 40)] * 1000), None]
        with self.assertRaises(debrid.Deferred): tb.download(self.item, stream=False)
        self.post.assert_not_called()

    def test_created_info_unavailable_is_retained_and_deferred(self):
        self.detail.return_value = None
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.delete.assert_not_called()

    def test_unknown_provider_state_is_not_a_delete_verdict(self):
        self.detail.return_value = tb_job(id=8, download_state='future_state', download_present=False)
        with self.assertRaises(debrid.Deferred): tb.download(self.item)
        self.delete.assert_not_called()

    def test_scoped_inventory_reused_but_detail_remains_fresh(self):
        self.existing(); self.item._provider_inventory = {}
        self.assertTrue(tb.download(self.item)); self.assertTrue(tb.download(self.item))
        self.get.assert_called_once(); self.assertEqual(self.detail.call_count, 2)

    def test_submission_invalidates_inventory_snapshot(self):
        self.item._provider_inventory = {}
        self.detail.return_value = tb_job(id=8, download_state='downloading', download_present=False)
        with self.assertRaises(debrid.Deferred): tb.download(self.item, stream=False)
        self.assertNotIn('TB', self.item._provider_inventory)


class ActualWantedVideoTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(rd, 'ui_print'))
        self.enterContext(patch.object(tb, 'ui_print'))

    def attempt(self, provider, names, force=False, flags=None, infected=False, wanted=None):
        flags = flags or {}
        row = candidate(); item = element(row, wanted)
        if provider == 'RD':
            job = rd_job(files=[NS(id=i, path=name, bytes=1_000_000_000, selected=1, **flags)
                                for i, name in enumerate(names, 1)],
                         links=['https://rd.invalid/' + str(i) for i in range(1, len(names) + 1)],
                         infected=infected)
            with patch.object(rd, 'get', side_effect=lambda url: job if '/info/' in url else [job]), \
                    patch.object(rd, 'post', return_value=NS(download='https://direct.invalid/video')) as link, \
                    patch.object(rd, 'delete') as delete:
                try:
                    result = rd.download(item, force=force)
                except debrid.Deferred:
                    result = False
                delete.assert_not_called()
                return result, row, link.call_args_list
        job = tb_job(files=[NS(id=i, name=name, size=1_000_000_000, **flags)
                            for i, name in enumerate(names, 1)], infected=infected)
        with patch.object(tb, 'get', return_value=NS(success=True, data=[job])), \
                patch.object(tb, 'get_torrent', return_value=job), \
                patch.object(tb, 'requestdl', return_value='https://direct.invalid/video') as link, \
                patch.object(tb, 'delete') as delete:
            try:
                result = tb.download(item, force=force)
            except debrid.Deferred:
                result = False
            delete.assert_not_called()
            return result, row, link.call_args_list

    def test_sidecars_samples_longer_episode_and_folder_tokens_never_claim_success(self):
        for provider in ('RD', 'TB'):
            for name in (TITLE + '.nfo', TITLE + '.txt', TITLE + '.sample.mkv',
                         'Example.S01E010.mkv', 'Example.S01E01/Other.S09E09.mkv',
                         'Example.S01E01\\Other.S09E09.mkv', TITLE + '.partial.mkv',
                         TITLE + '.incomplete.mkv', TITLE + '.downloading.mkv'):
                with self.subTest(provider=provider, name=name):
                    result, row, calls = self.attempt(provider, [name])
                    self.assertFalse(result)
                    self.assertEqual(calls, [])
                    self.assertEqual(row.download, [MAGNET])

    def test_infected_and_partial_video_flags_never_claim_success(self):
        for provider in ('RD', 'TB'):
            for flags in ({'partial': True}, {'infected': True}):
                with self.subTest(provider=provider, flags=flags):
                    result, row, calls = self.attempt(provider, [TITLE + '.mkv'], flags=flags)
                    self.assertFalse(result); self.assertEqual(calls, [])
            with self.subTest(provider=provider, infected_job=True):
                self.assertFalse(self.attempt(provider, [TITLE + '.mkv'], infected=True)[0])

    def test_valid_video_still_succeeds_and_skips_sidecar_link_with_alignment_intact(self):
        for provider in ('RD', 'TB'):
            with self.subTest(provider=provider):
                result, row, calls = self.attempt(provider, [TITLE + '.nfo', TITLE + '.mkv'])
                self.assertTrue(result)
                self.assertEqual(len(row.files), 1)
                self.assertEqual(row.files[0].match, 'S01E01')
                self.assertTrue(row.files[0].name.endswith('.mkv'))
                self.assertEqual(len(calls), 1)
                if provider == 'RD':
                    self.assertEqual(calls[0].args[1], {'link': 'https://rd.invalid/2'})
                else:
                    self.assertEqual(calls[0].args[1], 2)

    def test_coverage_and_match_use_basename_not_other_episode_in_parent_folder(self):
        names = ['S01E02/Example.S01E01.mkv', 'S01E01/Example.S01E02.mkv']
        for provider in ('RD', 'TB'):
            with self.subTest(provider=provider):
                result, row, calls = self.attempt(provider, names, wanted=['S01E01', 'S01E02'])
                self.assertTrue(result)
                self.assertEqual([f.match for f in row.files], ['S01E01', 'S01E02'])
                self.assertEqual(len(calls), 2)

    def test_force_preserves_manual_all_file_selection(self):
        for provider in ('RD', 'TB'):
            with self.subTest(provider=provider):
                self.assertTrue(self.attempt(provider, [TITLE + '.nfo'], force=True)[0])


class RealDebridHTTPTests(unittest.TestCase):
    def test_all_http_methods_have_bounded_timeouts(self):
        response = NS(status_code=200, content=b'{}')
        with patch.object(rd, 'ui_print'), patch.object(rd.session, 'get', return_value=response) as get, \
                patch.object(rd.session, 'post', return_value=response) as post, \
                patch.object(rd.requests, 'delete', return_value=response) as delete:
            rd.get('https://rd.invalid/get'); rd.post('https://rd.invalid/post', {})
            rd.delete('https://rd.invalid/delete')
            for request in (get, post, delete):
                timeout = request.call_args.kwargs.get('timeout')
                self.assertIsNotNone(timeout)
                self.assertTrue(all(0 < x <= 60 for x in timeout) if isinstance(timeout, tuple) else 0 < timeout <= 60)

    def test_plain_http_refusal_is_explicit_but_5xx_is_uncertain(self):
        with patch.object(rd, 'ui_print'):
            for status in (400, 401, 403):
                response = NS(status_code=status, content=b'Forbidden')
                with patch.object(rd.session, 'get', return_value=response), patch.object(rd.session, 'post', return_value=response):
                    self.assertTrue(rd._refused(rd.get('https://rd.invalid')))
                    self.assertTrue(rd._refused(rd.post('https://rd.invalid', {})))
            for status in (429, 500, 503):
                response = NS(status_code=status, content=b'{}')
                with patch.object(rd.session, 'post', return_value=response):
                    self.assertIsNone(rd.post('https://rd.invalid', {}))


if __name__ == '__main__':
    unittest.main()