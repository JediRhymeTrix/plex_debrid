"""Newznab discovery and mixed-source ranking; no network or credentials."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

# Follow main.py's import order for the legacy circular imports.
import ui
import releases
import scraper
from scraper import services
from scraper.services import newznab, torrentio


def feed(title, size=4_000_000_000):
    return NS(content=(f'<rss><channel><item><title>{title}</title>'
                       '<link>https://indexer.invalid/nzb/example</link>'
                       f'<enclosure length="{size}" /></item></channel></rss>').encode())


class NewznabTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.multiple(services, active=['torrentio', 'newznab'], overwrite=[]))
        self.enterContext(patch.object(newznab, 'api_key', 'test-not-a-key'))
        self.enterContext(patch.object(newznab, 'ui_print'))
        self.enterContext(patch.object(scraper, 'ui_print'))
        self.enterContext(patch.object(scraper, 'failed', False))
        self.lookup = self.enterContext(patch.object(newznab, 'resolve_title', return_value=('VisionQuest', '2026')))
        self.get = self.enterContext(patch.object(newznab, 'get', return_value=feed(
            'VisionQuest.S01E03.2160p.DSNP.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-TEST')))

    def test_lowercase_config_selects_both_sources(self):
        self.assertEqual(newznab.name, 'newznab')
        self.assertEqual(services.get(), [torrentio, newznab])

    def test_historical_casing_selects_and_activates_newznab(self):
        for name in ('newznab', 'Newznab', 'NEWZNAB'):
            with self.subTest(name=name), patch.object(services, 'active', ['torrentio', name]):
                self.assertEqual(services.get(), [torrentio, newznab])
                self.assertEqual(len(newznab.scrape('tt23112594', 'S01E03')), 1)

    def test_explicit_source_groups_preserve_order_and_case_compatibility(self):
        with patch.object(services, 'overwrite', [['Newznab'], ['torrentio']]):
            self.assertEqual(services.sequential(), [[newznab], [torrentio]])

    def test_setup_reuses_shared_scraper_settings(self):
        with patch.object(services, 'setup') as setup:
            newznab.setup(newznab, new=True)
            setup.assert_called_once_with(newznab, True)

    def test_editing_historical_source_does_not_register_duplicate_adapter(self):
        setting = NS(cls=newznab, setup=Mock())
        with patch.object(services, 'active', ['torrentio', 'Newznab']), \
                patch('settings.settings_list', [['Test', [setting]]]), patch('builtins.print'):
            newznab.setup(newznab, new=True)
            setting.setup.assert_called_once()
            self.assertEqual(services.active, ['torrentio', 'Newznab'])
            self.assertEqual(services.get(), [torrentio, newznab])

    def test_episode_title_query_uses_imdb_in_matching_expression(self):
        for episode in (3, 4):
            with self.subTest(episode=episode):
                altquery = rf'[^A-Za-z0-9]*(visionquest|vision.quest).(S01E{episode:02d}.)(tt23112594)?'
                result = newznab.scrape(f'visionquest.S01E{episode:02d}.', altquery)
                self.lookup.assert_called_with('show', 'tt23112594')
                params = self.get.call_args.args[1]
                self.assertEqual(params['t'], 'tvsearch')
                self.assertEqual(params['q'], f'VisionQuest s01e{episode:02d}')
                self.assertEqual(params['apikey'], 'test-not-a-key')
                self.assertEqual(result[0].type, 'usenet')
                self.assertEqual(result[0].source, '[newznab]')
                self.assertEqual(result[0].size, 4.0)

    def test_imdb_in_query_remains_preferred(self):
        newznab.scrape('tt23112594', 'S01E04.(tt9999999)?')
        self.lookup.assert_called_once_with('show', 'tt23112594')
        self.assertEqual(self.get.call_args.args[1]['q'], 'VisionQuest s01e04')

    def test_movie_title_and_imdb_queries_remain_supported(self):
        for query, alternate in (('tt23112594', '(.*)'), ('visionquest.2026.', '(tt23112594)?')):
            with self.subTest(query=query):
                newznab.scrape(query, alternate)
                self.assertEqual(self.get.call_args.args[1]['t'], 'search')
                self.assertEqual(self.get.call_args.args[1]['q'], 'VisionQuest 2026')

    def test_missing_id_key_or_disabled_source_does_not_request_indexer(self):
        for mode in ('missing-id', 'missing-key', 'disabled'):
            with self.subTest(mode=mode):
                self.get.reset_mock();self.lookup.reset_mock()
                active = [] if mode == 'disabled' else ['newznab']
                key = '' if mode == 'missing-key' else 'test-not-a-key'
                alternate = 'S01E03' if mode == 'missing-id' else 'S01E03.(tt23112594)?'
                with patch.object(services, 'active', active), patch.object(newznab, 'api_key', key):
                    self.assertEqual(newznab.scrape('visionquest.S01E03.', alternate), [])
                self.get.assert_not_called();self.lookup.assert_not_called()

    def test_empty_indexer_or_failed_title_lookup_returns_no_results(self):
        with patch.object(newznab, 'resolve_title', return_value=(None, None)):
            self.assertEqual(newznab.scrape('tt23112594', 'S01E03'), [])
            self.get.assert_not_called()
        for response in (None, NS(content=b'<rss><channel /></rss>')):
            with self.subTest(response=response), patch.object(newznab, 'get', return_value=response):
                self.assertEqual(newznab.scrape('tt23112594', 'S01E03'), [])

    def test_scrape_aggregates_torrent_and_usenet_before_ranking(self):
        torrent = releases.release('[torrentio]', 'torrent',
                                   'VisionQuest.S01E03.1080p.WEB.h264-ETHEL', [], 1.83, [], 30)
        torrent.cached = ['TB']
        with patch.object(torrentio, 'scrape', return_value=[torrent]) as search:
            found = scraper.scrape('visionquest.S01E03.', 'S01E03.(tt23112594)?')
        search.assert_called_once()
        self.get.assert_called_once()
        self.assertEqual({r.type for r in found}, {'torrent', 'usenet'})
        self.assertEqual(len(found), 2)
        for release in found: release.cached = ['TB']
        rules = [['resolution', 'upgrade', '==', '2160'],
                 ['title', 'upgrade', 'include', r'\bDV\b'],
                 ['title', 'upgrade', 'include', 'HDR'],
                 ['resolution', 'preference', 'highest', ''],
                 ['title', 'preference', 'include', 'ATMOS'],
                 ['cache status', 'preference', 'cached', ''],
                 ['size', 'requirement', '>=', '0.1']]
        version = releases.sort.version('Main', [], 'en', rules)
        for order in (found, found[::-1]):
            ranked = releases.sort(copy.deepcopy(order), version, doprint=False)
            self.assertEqual(ranked[0].type, 'usenet')
            self.assertEqual(ranked[0].resolution, '2160')

    def test_unavailable_usenet_does_not_discard_torrent_fallback(self):
        torrent = releases.release('[torrentio]', 'torrent', 'VisionQuest.S01E03.1080p', [], 1.0, [])
        with patch.object(torrentio, 'scrape', return_value=[torrent]), patch.object(newznab, 'get', return_value=None):
            found = scraper.scrape('visionquest.S01E03.', 'S01E03.(tt23112594)?')
        self.assertEqual(found, [torrent])

    def test_discovered_usenet_is_routed_to_torbox_usenet_handler(self):
        from debrid.services import torbox
        release = newznab.scrape('visionquest.S01E03.', 'S01E03.(tt23112594)?')[0]
        element = NS(Releases=[release], files=lambda: ['S01E03'])
        with patch.object(torbox, 'get') as get, patch.object(torbox, 'post') as post, \
                patch.object(torbox, 'download_usenet', return_value=True) as download:
            torbox.check(element, force=True)
            self.assertIn('TB', release.cached)
            self.assertTrue(torbox.download(element, query='.*'))
            download.assert_called_once_with(release, element, ['S01E03'])
            get.assert_not_called();post.assert_not_called()


if __name__ == '__main__':
    unittest.main()