"""Run with: python -B -m unittest discover -s tests -v. No network or credentials."""
import copy
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

# Match main.py's import order (the legacy packages have circular imports).
import ui
import releases
from content.classes import media
from content.services import plex


def versions():
    return [
        ['Main', [], 'en', [
            ['resolution', 'upgrade', '==', '2160'],
            ['title', 'upgrade', 'include', r'\bDV\b'],
            ['title', 'upgrade', 'include', 'HDR'],
            ['resolution', 'preference', 'highest', ''],
            ['title', 'preference', 'include', 'REMUX'],
            ['size', 'preference', 'highest', ''],
            ['size', 'requirement', '>=', '0.1']]],
        ['IMAX', [], 'en', [['title', 'requirement', 'include', 'IMAX'],
                           ['title', 'upgrade', 'include', 'IMAX'],
                           ['resolution', 'preference', 'highest', '']]],
        ['3D', [], 'en', [['title', 'requirement', 'include', '3D'],
                         ['title', 'upgrade', 'include', '3D']]],
        ['Open Matte', [], 'en', [['title', 'requirement', 'include', 'MATTE'],
                                 ['title', 'requirement', 'exclude', 'IMAX'],
                                 ['title', 'upgrade', 'include', 'MATTE']]],
    ]


def video(title='War', kind='episode', index=3):
    obj = media(NS(type=kind, title=title, guid='war-' + str(index),
                   index=index, parentIndex=1, grandparentTitle='War',
                   grandparentEID=['imdb://war'], EID=['imdb://war'], year=2026,
                   ratingKey=str(index), originallyAvailableAt='2026-01-01'))
    obj.query = lambda *a: 'war.S01E%02d.' % index if kind == 'episode' else 'war.2026'
    obj.watched = lambda: False
    obj.released = lambda: True
    return obj


def part(mid, title, resolution='1080', size=3_000_000_000, duration=2700000):
    return NS(id=str(mid), videoResolution=resolution, duration=duration,
              bitrate=10000, Part=[NS(id=str(mid), file='T:\\' + title,
                                      size=size, duration=duration,
                                      exists=True, accessible=True)])


class QualityFixture:
    def setUp(self):
        self.patches = [patch.object(releases.sort, 'versions', versions()),
                        patch.object(media, 'downloaded_versions', []),
                        patch.object(media, 'session_releases', {}, create=True),
                        patch.object(media, 'ignore_queue', []),
                        patch('content.classes.library', return_value=[NS(name='Plex Library')]),
                        patch.object(plex, 'current_library', [])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def library_episode(self, parts):
        ep = video()
        ep.Media = parts
        season = media(NS(type='season', guid='s1', parentEID=['imdb://war'],
                          index=1, Episodes=[ep], leafCount=1))
        show = media(NS(type='show', guid='war', EID=['imdb://war'],
                        Seasons=[season], leafCount=1))
        plex.current_library = [show]
        return ep

    def names(self, item):
        return [v.name for v in item.versions()]


class QualityTests(QualityFixture, unittest.TestCase):
    def test_fallback_does_not_block_upgrade_when_alternates_missing(self):
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        media.downloaded_versions = ['war.S01E03. [Main]']
        self.assertEqual(self.names(video()), ['Main upgrade', 'IMAX', '3D', 'Open Matte'])

    def test_restart_rebuilds_targets_from_plex(self):
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        self.assertIn('Main upgrade', self.names(video()))
        media.downloaded_versions.clear()
        self.assertIn('Main upgrade', self.names(video()))

    def test_one_file_can_cover_main_and_imax_after_restart(self):
        self.library_episode([part(1, 'War.S01E03.2160p.DV.HDR.IMAX.mkv', '4k')])
        self.assertEqual(self.names(video()), ['3D'])

    def test_targets_must_be_met_by_same_file(self):
        self.library_episode([part(1, 'War.S01E03.2160p.DV.mkv', '4k'),
                              part(2, 'War.S01E03.1080p.HDR.mkv')])
        self.assertIn('Main upgrade', self.names(video()))

    def test_presentation_requirements_not_satisfied_by_plain_file(self):
        self.library_episode([part(1, 'War.S01E03.2160p.DV.HDR.mkv', '4k')])
        self.assertEqual(self.names(video()), ['IMAX', '3D', 'Open Matte'])

    def test_new_item_keeps_fallback(self):
        self.assertEqual(self.names(video()), ['Main', 'IMAX', '3D', 'Open Matte'])

    def test_session_release_suppresses_cross_version_duplicate(self):
        r = releases.release('', 'torrent', 'War.S01E03.2160p.DV.HDR.IMAX', [], 5, [])
        media.session_releases['war.S01E03.'] = [r]
        media.downloaded_versions = ['war.S01E03. [Main]']
        self.assertEqual(self.names(video()), ['3D'])

    def test_refresh_does_not_leave_stale_filename_cache(self):
        ep = self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        item = video()
        self.assertIn('Main upgrade', self.names(item))
        ep.Media = [part(2, 'War.S01E03.2160p.DV.HDR.mkv', '4k')]
        self.assertNotIn('Main upgrade', self.names(item))

    def test_group_does_not_hide_episode_upgrade(self):
        ep = self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        season = plex.current_library[0].Seasons[0]
        self.assertIn('Main upgrade', self.names(season))
        self.assertIn('Main upgrade', self.names(plex.current_library[0]))

    def test_cleanup_strict_dominance(self):
        from content.quality import superseded
        item = video()
        item.Media = [part(1, 'War.S01E03.1080p.mkv'),
                      part(2, 'War.S01E03.2160p.DV.HDR.mkv', '4k')]
        plan = superseded(item)
        self.assertEqual([(old.id, keep.id) for old, keep in plan], [('1', '2')])

    def test_cleanup_preserves_presentations_and_editions(self):
        from content.quality import superseded
        for tag in ('IMAX', '3D', 'OPEN.MATTE', 'EXTENDED', 'DIRECTORS.CUT'):
            with self.subTest(tag=tag):
                item = video()
                item.Media = [part(1, 'War.S01E03.1080p.' + tag + '.mkv'),
                              part(2, 'War.S01E03.2160p.DV.HDR.mkv', '4k')]
                self.assertEqual(superseded(item), [])

    def test_cleanup_ties_keep_both(self):
        from content.quality import superseded
        item = video()
        item.Media = [part(1, 'War.S01E03.1080p.A.mkv'), part(2, 'War.S01E03.1080p.B.mkv')]
        self.assertEqual(superseded(item), [])

    def test_cleanup_preserves_fulfilled_target(self):
        from content.quality import superseded
        item = video()
        item.Media = [part(1, 'War.S01E03.2160p.DV.HDR.mkv', '4k'),
                      part(2, 'War.S01E03.2160p.REMUX.mkv', '4k')]
        self.assertEqual(superseded(item), [])

    def test_cleanup_respects_actual_rule_order(self):
        from content.quality import superseded
        releases.sort.versions = [versions()[0]]
        releases.sort.versions[0][3] = [['size', 'preference', 'highest', '']]
        item = video()
        item.Media = [part(1, 'War.S01E03.1080p.mkv', size=10_000_000_000),
                      part(2, 'War.S01E03.2160p.mkv', '4k', size=1_000_000_000)]
        self.assertEqual([(x.id, y.id) for x, y in superseded(item)], [('2', '1')])

    def test_cleanup_unknown_rules_fail_closed(self):
        from content.quality import superseded
        releases.sort.versions[0][3].append(['source', 'preference', 'include', 'something'])
        item = video()
        item.Media = [part(1, 'War.S01E03.1080p.mkv'),
                      part(2, 'War.S01E03.2160p.DV.HDR.mkv', '4k')]
        self.assertEqual(superseded(item), [])

    def test_cleanup_multipart_shared_path_and_short_replacement_kept(self):
        from content.quality import superseded
        for mode in ('multipart', 'same_path', 'short', 'missing_size'):
            with self.subTest(mode=mode):
                a = part(1, 'War.S01E03.1080p.mkv')
                b = part(2, 'War.S01E03.2160p.DV.HDR.mkv', '4k')
                if mode == 'multipart': b.Part.append(copy.deepcopy(b.Part[0]))
                if mode == 'same_path': b.Part[0].file = a.Part[0].file
                if mode == 'short': b.duration = b.Part[0].duration = 1000
                if mode == 'missing_size': b.Part[0].size = 0
                item = video(); item.Media = [a, b]
                self.assertEqual(superseded(item), [])

    def test_retry_cap_does_not_disable_collected_upgrades(self):
        releases.sort.versions[0][1] = [['retries', '<=', '48']]
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        item = video(); item.ignored_count = 99
        self.assertIn('Main upgrade', self.names(item))

    def test_parent_folder_tags_cover_versions(self):
        ep = self.library_episode([part(1, 'War.S01E03.1080p.mkv', '4k')])
        ep.Media[0].Part[0].file = r'T:\War.S01.2160p.DV.HDR.IMAX\War.S01E03.mkv'
        self.assertEqual(self.names(video()), ['3D'])

    def test_main_and_imax_one_debrid_request(self):
        import debrid
        item = video()
        item.Releases = [releases.release('', 'torrent', 'War.S01E03.2160p.DV.HDR.IMAX', [], 5, [])]
        item.Releases[0].cached = ['TB']
        item.bitrate = lambda: None
        with patch.object(debrid, 'check'), patch.object(debrid, 'download', return_value=True) as grab:
            success, retry = item.debrid_download()
        self.assertTrue(success)
        self.assertEqual(grab.call_count, 1)
        self.assertEqual(self.names(item), ['3D'])

    def test_cleanup_same_title_different_size_uses_identity(self):
        from content.quality import superseded
        releases.sort.versions = [['Size', [], 'en', [['size', 'preference', 'highest', '']]]]
        item = video()
        item.Media = [part(1, 'War.S01E03.1080p.mkv', size=1_000_000_000),
                      part(2, 'War.S01E03.1080p.mkv', size=9_000_000_000)]
        item.Media[1].Part[0].file = r'T:\another\War.S01E03.1080p.mkv'
        self.assertEqual([(a.id, b.id) for a, b in superseded(item)], [('1', '2')])

    def test_pending_target_is_acknowledged_then_manual_removal_reopens_upgrade(self):
        ep = self.library_episode([part(1, 'War.S01E03.2160p.DV.HDR.mkv', '4k')])
        r = releases.release('', '', 'War.S01E03.2160p.DV.HDR', [], 5, [])
        media.session_releases['war.S01E03.'] = [r]
        self.assertNotIn('Main upgrade', self.names(video()))
        self.assertNotIn('war.S01E03.', media.session_releases)
        ep.Media = [part(2, 'War.S01E03.1080p.mkv')]
        self.assertIn('Main upgrade', self.names(video()))

    def test_season_grab_bridges_episode_coverage(self):
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        ep = video()
        season = media(NS(type='season', Episodes=[ep]))
        season.version = releases.sort.version(*versions()[0])
        season.Releases = [releases.release('', '', 'War.S01.2160p.DV.HDR.IMAX',
                              [NS(match='S01E03', name='War.S01E03.mkv')], 10, [])]
        season.downloaded()
        self.assertEqual(self.names(ep), ['3D'])

    def test_cache_requirement_and_preference_are_not_noops(self):
        a = releases.release('', '', 'A.1080p', [], 1, [])
        b = releases.release('', '', 'B.1080p', [], 1, []); b.cached = ['TB']
        rule = releases.sort.version.cache_status('cache status', 'requirement', 'cached')
        self.assertEqual(rule.apply([a, b]), [b])
        rule = releases.sort.version.cache_status('cache status', 'preference', 'cached')
        self.assertEqual(rule.apply([a, b]), [b, a])

    def test_quick_versions_does_not_touch_library(self):
        with patch('content.classes.library', side_effect=AssertionError('library touched')):
            self.assertEqual([v.name for v in video().versions(quick=True)],
                             ['Main', 'IMAX', '3D', 'Open Matte'])

    def test_decimal_gigabytes_match_scraper_units(self):
        from content.quality import file_release
        self.assertEqual(file_release(part(1, 'War.1080p.mkv')).size, 3.0)

    def test_pending_grab_expires_and_reopens_upgrade(self):
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        r = releases.release('', '', 'War.S01E03.2160p.DV.HDR', [], 5, [])
        r.grabbed_at = 0
        media.session_releases['war.S01E03.'] = [r]
        self.assertIn('Main upgrade', self.names(video()))

    def test_expired_record_is_refreshed_by_same_title_grab(self):
        import time
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        r = releases.release('', '', 'War.S01E03.2160p.DV.HDR', [], 5, [])
        r.grabbed_at = 0
        other = releases.release('', '', 'War.S01E03.1080p.3D', [], 5, [])
        other.grabbed_at = time.time()
        media.session_releases['war.S01E03.'] = [r, other]
        item = video()
        self.assertIn('Main upgrade', self.names(item))
        item.Releases = [r]; item.version = releases.sort.version(*versions()[0])
        item.downloaded()
        self.assertNotIn('Main upgrade', self.names(item))

    def test_download_guard_uses_string_keys_without_version_attribute(self):
        import debrid
        with patch.object(debrid, 'downloading', ['war.S01E03. [Main]']):
            self.assertTrue(video().downloading())

    def test_lower_retry_threshold_still_applies(self):
        releases.sort.versions[0][1] = [['retries', '>=', '10']]
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        item = video(); item.ignored_count = 0
        self.assertNotIn('Main upgrade', self.names(item))

    def test_cleanup_requires_joint_target_even_for_fallbacks(self):
        from content.quality import superseded
        item = video()
        item.Media = [part(1, 'War.S01E03.720p.mkv', '720'),
                      part(2, 'War.S01E03.1080p.mkv')]
        self.assertEqual(superseded(item), [])

    def test_transport_source_is_not_a_reason_to_reacquire_plex_file(self):
        releases.sort.versions = [['Source', [], 'en', [['source', 'requirement', 'include', 'Torrentio']]]]
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        self.assertEqual(self.names(video()), [])

    def test_nonplex_collected_items_not_regrabbed_after_restart(self):
        with patch('content.classes.library', return_value=[NS(name='Trakt Collection')]):
            self.assertFalse(video().version_missing())
            media.downloaded_versions = ['war.S01E03. [Main]']
            self.assertTrue(video().version_missing())

    def test_raw_plex_episode_query_without_trakt_parent_year(self):
        item = media(NS(type='episode', grandparentTitle='War', parentIndex=1, index=3))
        self.assertEqual(item.query(), 'war.S01E03.')

    def test_mixed_episode_group_prioritizes_upgrade_before_fallback(self):
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        old, new = video(), video(index=4)
        for episodes in ([new, old], [old, new]):
            group = media(NS(type='season', Episodes=episodes))
            self.assertEqual(self.names(group)[0], 'Main upgrade')

    def test_file_name_requirement_is_checked_on_actual_plex_filename(self):
        releases.sort.versions = [['HDR file', [], 'en', [['file names', 'requirement', 'include', 'HDR']]]]
        self.library_episode([part(1, 'War.S01E03.1080p.mkv')])
        self.assertIn('HDR file', self.names(video()))


class CleanupExecutionTests(QualityFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.item = video()
        self.item.Media = [part(1, 'War.S01E03.1080p.mkv'),
                           part(2, 'War.S01E03.2160p.DV.HDR.mkv', '4k')]
        for m in self.item.Media:
            m.Part[0].key = '/library/parts/' + m.id + '/1/file.mkv'
        self.patches2 = [patch.object(plex, 'users', [['test', 'not-a-real-token']]),
                         patch.object(plex, 'get', side_effect=self.get),
                         patch('content.cleanup._readable', return_value=True),
                         patch('content.cleanup._independent_references', return_value=True),
                         patch('content.cleanup._torbox_inventory', return_value=[]),
                         patch('content.cleanup._backend_plan', return_value=(
                             {'id': 100, 'hash': 'a' * 40, 'files': [{'name': 'old.mkv', 'size': 3}]},
                             {'id': 200, 'hash': 'b' * 40, 'files': [{'name': 'new.mkv', 'size': 4}]}))]
        for p in self.patches2:
            p.start(); self.addCleanup(p.stop)

    def get(self, url):
        if '/status/sessions' in url:
            return NS(MediaContainer=NS(size=0, Metadata=[]))
        return NS(MediaContainer=NS(Metadata=[self.item]))

    def test_dry_run_never_deletes(self):
        from content.cleanup import clean
        with patch('content.cleanup._delete_torbox') as delete:
            self.assertEqual(len(clean(self.item, dry_run=True)), 1)
            delete.assert_not_called()

    def test_delete_targets_one_verified_torrent_not_mount_or_plex(self):
        from content.cleanup import clean
        with patch('content.cleanup._delete_torbox', return_value=True) as delete, patch.object(plex.session, 'delete') as plex_delete:
            self.assertEqual(len(clean(self.item)), 1)
            delete.assert_called_once_with(100)
            plex_delete.assert_not_called()
            self.assertEqual([x.id for x in self.item.Media], ['2'])

    def test_missing_or_inaccessible_keeper_never_deletes(self):
        from content.cleanup import clean
        for flag in ('exists', 'accessible'):
            setattr(self.item.Media[1].Part[0], flag, False)
            with patch('content.cleanup._delete_torbox') as delete:
                self.assertEqual(clean(self.item), [])
                delete.assert_not_called()
            setattr(self.item.Media[1].Part[0], flag, True)

    def test_http_error_never_falls_back_to_local_unlink(self):
        from content.cleanup import clean
        with patch('content.cleanup._delete_torbox', return_value=False), patch('os.remove') as remove:
            self.assertEqual(clean(self.item), [])
            remove.assert_not_called()
            self.assertEqual(len(self.item.Media), 2)

    def test_playing_or_sessions_unreachable_blocks_deletion(self):
        from content.cleanup import clean
        for response in (None, NS(MediaContainer=NS(size=1, Metadata=[NS(ratingKey='3')]))):
            def get(url):
                return response if '/status/sessions' in url else self.get(url)
            with patch.object(plex, 'get', side_effect=get), patch('content.cleanup._delete_torbox') as delete:
                self.assertEqual(clean(self.item), [])
                delete.assert_not_called()

    def test_keeper_vanishing_between_plan_and_execute_blocks_deletion(self):
        from content.cleanup import clean
        calls = []
        def get(url):
            if '/library/metadata/' in url:
                calls.append(url)
                if len(calls) > 1:
                    gone = copy.copy(self.item); gone.Media = self.item.Media[:1]
                    return NS(MediaContainer=NS(Metadata=[gone]))
            return self.get(url)
        with patch.object(plex, 'get', side_effect=get), patch('content.cleanup._delete_torbox') as delete:
            self.assertEqual(clean(self.item), [])
            delete.assert_not_called()

    def test_backend_keeper_changes_after_range_read_blocks_deletion(self):
        from content.cleanup import clean
        a = {'id': 100, 'hash': 'a' * 40, 'files': [{'name': 'old.mkv', 'size': 3}]}
        b = {'id': 200, 'hash': 'b' * 40, 'files': [{'name': 'new.mkv', 'size': 4}]}
        changed = copy.deepcopy(b); changed['id'] = 999
        with patch('content.cleanup._backend_plan', side_effect=[(a, b), (a, changed)]), patch('content.cleanup._delete_torbox') as delete:
            self.assertEqual(clean(self.item), [])
            delete.assert_not_called()

    def test_keeper_accessibility_change_at_final_fetch_blocks_deletion(self):
        from content.cleanup import clean
        count = []
        def get(url):
            if '/library/metadata/' in url:
                count.append(url)
                if len(count) >= 3:
                    changed = copy.deepcopy(self.item)
                    changed.Media[1].Part[0].accessible = False
                    return NS(MediaContainer=NS(Metadata=[changed]))
            return self.get(url)
        with patch.object(plex, 'get', side_effect=get), patch('content.cleanup._delete_torbox') as delete:
            self.assertEqual(clean(self.item), [])
            delete.assert_not_called()

    def test_special_stereo_and_filename_variants_never_deleted(self):
        from content.cleanup import clean
        for marker in ('3DHSBS', 'HSBS', 'HOU'):
            for m in self.item.Media:
                m.Part[0].file = m.Part[0].file.replace('.mkv', '.' + marker + '.mkv')
            with patch('content.cleanup._delete_torbox') as delete:
                self.assertEqual(clean(self.item), [])
                delete.assert_not_called()


class BackendSafetyTests(unittest.TestCase):
    def setUp(self):
        self.old = part(1, 'War.S01E03.1080p.mkv')
        self.keeper = part(2, 'War.S01E03.2160p.mkv', '4k')
        self.inventory = [dict(id=10, hash='a' * 40, size=3_000_000_000,
                               download_finished=True, download_present=True,
                               files=[dict(name='War.S01E03.1080p.mkv', size=3_000_000_000)]),
                          dict(id=20, hash='b' * 40, size=3_000_000_000,
                               download_finished=True, download_present=True,
                               files=[dict(name='War.S01E03.2160p.mkv', size=3_000_000_000)])]

    def test_isolated_exact_mapping_allowed(self):
        from content.cleanup import _backend_plan
        self.assertEqual(_backend_plan(self.old, self.keeper, self.inventory, 'T:\\')[0]['id'], 10)

    def test_pack_ambiguous_mapping_same_hash_unmapped_root_and_wrong_size_denied(self):
        from content.cleanup import _backend_plan
        for mode in ('pack', 'ambiguous', 'same_hash', 'root', 'size', 'offline', 'unknown', 'alias_wrong_size'):
            with self.subTest(mode=mode):
                inv = copy.deepcopy(self.inventory); root = 'T:\\'
                if mode == 'pack': inv[0]['files'].append(dict(name='S01E04.mkv', size=1))
                if mode == 'ambiguous': inv.append(copy.deepcopy(inv[0]))
                if mode == 'alias_wrong_size':
                    alias = copy.deepcopy(inv[0]); alias['files'][0]['size'] = 1
                    inv.append(alias)
                if mode == 'same_hash': inv[1]['hash'] = inv[0]['hash']
                if mode == 'root': root = 'R:\\torrents'
                if mode == 'size': inv[0]['files'][0]['size'] = 1
                if mode == 'offline': inv[1]['download_present'] = False
                if mode == 'unknown': root = ''
                self.assertIsNone(_backend_plan(self.old, self.keeper, inv, root))


if __name__ == '__main__':
    unittest.main()