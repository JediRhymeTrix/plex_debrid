"""Progressive quality decisions; all provider/search requests are mocked."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import ui
import debrid
import releases
import scraper
from content import quality
from content.classes import media
from content.services import plex
from scraper import services
from scraper.services import torrentio, newznab
from test_quality import QualityFixture, versions, video, part
from content import monitor


def candidate(title, size=5, kind='torrent'):
    result = releases.release('[test]', kind, title, [], size, [])
    result.cached = ['TB']
    return result


class DecisionTests(QualityFixture, unittest.TestCase):
    def test_intermediate_hdr_upgrade_preserves_resolution_without_waiting_for_dv(self):
        main = releases.sort.version(*versions()[0])
        old = candidate('War.S01E03.2160p.SDR', 10)
        hdr = candidate('War.S01E03.2160p.HDR', 5)
        self.assertTrue(quality.improves(hdr, old, main))
        self.assertFalse(quality.improves(old, hdr, main))

    def test_resolution_or_fulfilled_target_cannot_regress(self):
        main = releases.sort.version(*versions()[0])
        cases = [('War.2160p.HDR', 'War.1080p.DV.HDR'),
                 ('War.2160p.DV', 'War.2160p.HDR'),
                 ('War.2160p.DV.HDR', 'War.2160p.DV.REMUX')]
        for old, new in cases:
            with self.subTest(old=old, new=new):
                self.assertFalse(quality.improves(candidate(new, 20), candidate(old), main))

    def test_targets_remain_soft_for_intermediate_upgrade_version(self):
        self.library_episode([part(1, 'War.S01E03.2160p.mkv', '4k')])
        upgrade = video().versions()[0]
        self.assertEqual(upgrade.name, 'Main upgrade')
        self.assertTrue(quality.matches(candidate('War.S01E03.2160p.HDR'), upgrade))
        self.assertFalse(quality.matches(candidate('War.S01E03.2160p.HDR'), upgrade, True))

    def test_managed_optional_buckets_continue_improving_after_target_is_met(self):
        self.library_episode([part(1, 'War.S01E03.1080p.IMAX.mkv'), part(2, 'War.S01E03.1080p.3D.mkv')])
        item = video();item._monitor_managed = True
        names = [v.name for v in item.versions()]
        self.assertIn('IMAX upgrade', names)
        self.assertIn('3D upgrade', names)
        self.assertNotIn('Open Matte', names)

    def test_optional_buckets_inherit_missing_primary_quality_preferences(self):
        releases.sort.versions[0][3].insert(-1, ['title', 'preference', 'include', 'ATMOS'])
        config = next(v for v in quality.active_versions(video(), collected=True) if v.name == '3D')
        self.assertTrue(quality.improves(candidate('War.1080p.3D.HDR.ATMOS', 3),
                                         candidate('War.1080p.3D', 5), config))
        self.assertFalse(quality.improves(candidate('War.2160p.3D', 8),
                                          candidate('War.1080p.3D.HDR.ATMOS', 5), config))

    def test_optional_bucket_inherits_primary_soft_targets_without_audio_trigger(self):
        config = next(v for v in quality.active_versions(video(), collected=True) if v.name == '3D')
        self.assertTrue(quality.improves(candidate('War.1080p.3D.HDR', 3),
                                         candidate('War.1080p.3D', 5), config))
        self.assertFalse(quality.improves(candidate('War.2160p.3D', 8),
                                          candidate('War.1080p.3D.HDR', 5), config))

    def test_no_cross_bucket_replacement_for_3d_or_expanded_presentations(self):
        for config, old, new in ((versions()[2], 'War.1080p.3D', 'War.2160p.HDR'),
                                 (versions()[1], 'War.1080p.IMAX', 'War.2160p.HDR'),
                                 (versions()[3], 'War.1080p.MATTE', 'War.2160p.IMAX')):
            with self.subTest(config=config[0]):
                self.assertFalse(quality.improves(candidate(new), candidate(old), releases.sort.version(*config)))

    def test_protected_imax_can_coexist_with_better_main_but_not_duplicate_equal_main(self):
        main = releases.sort.version(*versions()[0])
        old = candidate('War.1080p.IMAX')
        self.assertTrue(quality.accepts(candidate('War.2160p.HDR'), [old], main))
        self.assertFalse(quality.accepts(candidate('War.1080p'), [old], main))

    def test_3d_is_not_a_primary_fallback(self):
        main = releases.sort.version(*versions()[0])
        self.assertFalse(quality.matches(candidate('War.2160p.DV.HDR.3D'), main))
        self.assertTrue(quality.matches(candidate('House.2160p.DV.HDR'), main))
        for tag in ('3DHSBS', '3DHOU', 'FSBS'):
            self.assertFalse(quality.matches(candidate('War.2160p.DV.HDR.' + tag), main))

    def test_preference_improvement_is_allowed_after_full_target_without_cache_bias(self):
        main = releases.sort.version(*versions()[0])
        self.assertTrue(quality.improves(candidate('War.2160p.DV.HDR.REMUX', 20),
                                         candidate('War.2160p.DV.HDR', 10), main))
        self.assertFalse(quality.improves(candidate('War.2160p.DV.HDR', 10),
                                          candidate('War.2160p.DV.HDR', 10), main))

    def test_optional_hdr_or_audio_features_cannot_disappear_for_bigger_copy(self):
        rules = [['resolution', 'preference', 'highest', ''],
                 ['title', 'requirement', 'include', 'IMAX'],
                 ['title', 'preference', 'include', 'HDR'],
                 ['title', 'preference', 'include', 'ATMOS'],
                 ['size', 'preference', 'highest', '']]
        version = releases.sort.version('IMAX', [], 'en', rules)
        self.assertTrue(quality.improves(candidate('War.2160p.IMAX.HDR.ATMOS', 8),
                                         candidate('War.1080p.IMAX.HDR.ATMOS', 10), version))
        self.assertFalse(quality.improves(candidate('War.2160p.IMAX', 20),
                                          candidate('War.1080p.IMAX.HDR.ATMOS', 10), version))

    def test_hard_requirement_is_not_relaxed_for_intermediate_targets(self):
        version = releases.sort.version(*versions()[0])
        version.rules.append(['title', 'requirement', 'include', 'ENGLISH'])
        self.assertFalse(quality.improves(candidate('War.2160p.HDR'), candidate('War.2160p.ENGLISH'), version))

    def test_provider_file_contradicting_indexed_resolution_or_hdr_is_not_success(self):
        item = NS(version=releases.sort.version(*versions()[0]))
        for name in ('War.1080p.HDR.mkv', 'War.2160p.SDR.mkv'):
            proposed = candidate('War.2160p.DV.HDR')
            proposed.files = [NS(name=name, size=5)]
            self.assertFalse(quality.confirmed(item, proposed))

    def test_known_plex_stream_features_are_not_lost_when_filename_is_generic(self):
        m = part(1, 'War.2160p.mkv', '4k')
        m.Part[0].Stream = [NS(displayTitle='English (EAC3 5.1 + Atmos)', extendedDisplayTitle='',
                              DOVIPresent='1', colorTrc='smpte2084')]
        value = quality.file_release(m)
        self.assertIn('ATMOS', value.title)
        self.assertIn('DV', value.title)
        self.assertIn('HDR', value.title)

    def test_recent_movie_gets_hourly_upgrade_opportunity_without_new_setting(self):
        import datetime
        now = datetime.datetime(2026, 10, 28, tzinfo=datetime.timezone.utc).timestamp()
        worker = object.__new__(monitor.Worker)
        worker.hours = (1, 24, 168, 720);worker.library = []
        movie = media(NS(type='movie', EID=['imdb://test'], originallyAvailableAt='2026-10-27'))
        self.assertEqual(worker.interval(('movie', 1), movie, now), 3600)

    def test_cleanup_can_plan_proven_intermediate_progress_without_full_target(self):
        item = video();item.Media = [part(1, 'War.S01E03.2160p.mkv', '4k'),
                                     part(2, 'War.S01E03.2160p.HDR.mkv', '4k')]
        self.assertEqual([(a.id, b.id) for a, b in quality.superseded(item)], [('1', '2')])

    def test_split_existing_targets_do_not_block_improvement_of_one_copy(self):
        main = releases.sort.version(*versions()[0])
        copies = [candidate('War.2160p.DV'), candidate('War.1080p.HDR')]
        self.assertTrue(quality.accepts(candidate('War.2160p.HDR'), copies, main))
        self.assertFalse(quality.accepts(candidate('War.1080p.HDR'), copies, main))

    def test_weaker_leftover_cannot_cause_regrab_of_already_owned_better_copy(self):
        main = releases.sort.version(*versions()[0])
        copies = [candidate('War.1080p'), candidate('War.2160p.HDR')]
        self.assertFalse(quality.accepts(candidate('War.2160p.HDR'), copies, main))

    def test_scanned_sdr_does_not_acknowledge_unscanned_intermediate_hdr_grab(self):
        self.library_episode([part(1, 'War.S01E03.2160p.mkv', '4k')])
        pending = candidate('War.S01E03.2160p.HDR')
        media.session_releases['war.S01E03.'] = [pending]
        existing = quality.existing(video(), plex.current_library, media.session_releases)
        self.assertIn('war.S01E03.', media.session_releases)
        self.assertEqual(len(existing), 2)

    def test_pending_edition_and_language_are_not_acknowledged_by_another_copy(self):
        main = releases.sort.version(*versions()[0])
        for old, new in [('War.2160p.DV.HDR.ENG', 'War.2160p.DV.HDR.GER'),
                         ('War.2160p.DV.HDR.EXTENDED', 'War.2160p.DV.HDR')]:
            self.assertFalse(quality._covers(candidate(new), candidate(old), main))

    def test_new_explicit_language_tag_does_not_block_progress_from_unknown(self):
        main = releases.sort.version(*versions()[0])
        self.assertTrue(quality.improves(candidate('War.2160p.HDR.ENGLISH'), candidate('War.2160p'), main))
        self.assertFalse(quality.improves(candidate('War.2160p.DV.HDR.GERMAN'), candidate('War.2160p.HDR.ENGLISH'), main))

    def test_cached_torrent_wave_precedes_higher_quality_usenet_then_uncached(self):
        item = video();item._monitor_managed = True;item.bitrate = lambda: None
        torrents = candidate('War.S01E03.1080p', 3)
        nzb = candidate('War.S01E03.2160p.DV.HDR', 5, 'usenet')
        plain = candidate('War.S01E03.2160p.DV.HDR.REMUX', 20);plain.cached = []
        item.Releases = [nzb, plain, torrents]
        order = []
        def grab(element, stream=True, **kwargs):
            r = element.Releases[0];order.append((r.type, r.title, stream));return r.type == 'usenet'
        with patch.object(debrid, 'check'), patch.object(debrid, 'download', side_effect=grab), \
                patch.object(item, 'versions', return_value=[releases.sort.version(*versions()[0])]), \
                patch.object(item, 'downloaded'), patch('content.quality.existing', return_value=[]):
            item.debrid_download()
        self.assertEqual(order[0][1], torrents.title)
        self.assertEqual(order[-1][0], 'usenet')

    def test_explicit_torrent_groups_cannot_short_circuit_enabled_usenet(self):
        torrent = candidate('War.S01E03.1080p')
        nzb = candidate('War.S01E03.2160p.HDR', kind='usenet')
        with patch.multiple(services, active=['torrentio', 'newznab'], overwrite=[['torrentio'], ['newznab']]), \
                patch.object(torrentio, 'scrape', return_value=[torrent]) as t, \
                patch.object(newznab, 'scrape', return_value=[nzb]) as n, patch.object(scraper, 'ui_print'):
            found = scraper.scrape('war.S01E03.', 'S01E03')
        self.assertEqual({r.type for r in found}, {'torrent', 'usenet'})
        t.assert_called_once();n.assert_called_once()

    def test_usenet_results_do_not_short_circuit_later_torrent_fallback_groups(self):
        from scraper.services import prowlarr
        torrent = candidate('War.S01E03.1080p')
        nzb = candidate('War.S01E03.2160p.HDR', kind='usenet')
        with patch.multiple(services, active=['torrentio', 'prowlarr', 'newznab'],
                            overwrite=[['torrentio'], ['prowlarr'], ['newznab']]), \
                patch.object(torrentio, 'scrape', return_value=[]), \
                patch.object(prowlarr, 'scrape', return_value=[torrent]) as t, \
                patch.object(newznab, 'scrape', return_value=[nzb]) as n, patch.object(scraper, 'ui_print'):
            found = scraper.scrape('war.S01E03.', 'S01E03')
        self.assertEqual({r.type for r in found}, {'torrent', 'usenet'})
        t.assert_called_once();n.assert_called_once()


if __name__ == '__main__':
    unittest.main()