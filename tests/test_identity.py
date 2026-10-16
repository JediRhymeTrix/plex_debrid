"""Post-scan identity checks never reject or delete a download."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import ui
from content.classes import media
from content.services import plex


IMDB = 'tt31349844'
CORRECT = 'plex://movie/65d2daf3359932bcaa86b538'
WRONG = 'plex://movie/6738e193d70f01a8cec05ff0'
FILE = r'Runner.2026\Runner.2026.2160p.mkv'


def movie(imdb='tt34564059', path='T:\\' + FILE, size=9000000000):
    return NS(type='movie', ratingKey='144671', title='The Runner', guid=WRONG,
              EID=['imdb://' + imdb], Guid=[NS(id='imdb://' + imdb)],
              Media=[NS(id='100', Part=[NS(id='200', file=path, size=size)])])


class IdentityTests(unittest.TestCase):
    def setUp(self):
        from content import identity
        self.identity = identity
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'plex_download_identity.json'
        p = patch.object(identity, '_state_path', return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(plex, 'users', [['test', 'not-a-real-token']])
        p.start(); self.addCleanup(p.stop)
        self.request = NS(type='movie', EID=['imdb://' + IMDB])
        self.release = NS(title='Runner.2026.2160p', files=[NS(name=FILE, size=9.0, wanted=True)])
        self.current = movie()

    def queue(self):
        return self.identity.remember(self.request, self.release)

    def test_missing_id_allows_without_creating_state(self):
        self.request.EID = []
        self.assertFalse(self.queue())
        self.assertFalse(self.path.exists())

    def test_unknown_file_or_unselected_file_leaves_download_unchanged(self):
        for files in ([], [NS(name=FILE, size=9.0, wanted=False)], [NS(name='sample.txt', size=1, wanted=True)]):
            self.release.files = files
            self.assertFalse(self.queue())

    def test_multiple_wanted_videos_or_extras_never_get_one_movies_id(self):
        self.release.files.append(NS(name=r'Bonus\Bonus.mkv', size=1, wanted=True))
        self.assertFalse(self.queue())
        self.assertFalse(self.path.exists())

    def test_original_id_survives_metadata_enrichment(self):
        self.identity.capture(self.request)
        self.request.EID = ['imdb://tt34564059']
        self.assertTrue(self.queue())
        self.assertEqual(self.identity._load()[0]['imdb'], IMDB)

    def test_actual_match_hook_captures_before_other_service_overwrites_id(self):
        obj = media(NS(type='movie', title='Runner', year=2026, EID=['imdb://' + IMDB]))
        enriched = NS(type='movie', EID=['imdb://tt34564059'], guid=WRONG,
                      watchlist=object())
        with patch.object(plex, 'match', return_value=enriched):
            self.assertTrue(obj.match('content.services.plex'))
        self.assertEqual(obj.EID, ['imdb://tt34564059'])
        self.assertEqual(obj._requested_imdb, IMDB)
        self.assertTrue(self.identity.remember(obj, self.release))
        self.assertEqual(self.identity._load()[0]['imdb'], IMDB)

    def test_missing_original_id_does_not_use_later_guessed_id(self):
        self.request.EID = []
        self.identity.capture(self.request)
        self.request.EID = ['imdb://' + IMDB]
        self.assertFalse(self.queue())

    def test_persistent_exact_path_and_size(self):
        self.assertTrue(self.queue())
        records = self.identity._load()
        self.assertEqual(records[0]['imdb'], IMDB)
        self.assertEqual(records[0]['size'], 9000000000)
        self.assertTrue(self.identity.linked(self.current, records[0]))
        self.assertFalse(self.identity.linked(movie(path=r'T:\other\Runner.2026.2160p.mkv'), records[0]))
        self.assertFalse(self.identity.linked(movie(size=9000000001), records[0]))
        self.assertNotIn('token', self.path.read_text())

    def test_no_match_or_api_failure_is_fail_open(self):
        self.queue()
        for value in (None, NS(MediaContainer=NS(SearchResult=[]))):
            with patch.object(self.identity, '_get', return_value=value), patch.object(plex.session, 'put') as put:
                self.identity.reconcile([self.current])
                put.assert_not_called()
                self.assertEqual(self.current.title, 'The Runner')

    def test_conflicting_requests_do_not_rematch(self):
        self.queue()
        self.request.EID = ['imdb://tt34564059']; self.queue()
        with patch.object(self.identity, '_get') as get, patch.object(plex.session, 'put') as put:
            self.identity.reconcile([self.current])
            get.assert_not_called(); put.assert_not_called()

    def test_multiple_library_items_linking_same_file_are_ambiguous(self):
        self.queue()
        other = movie(); other.ratingKey = '999'
        with patch.object(self.identity, '_get') as get:
            self.identity.reconcile([self.current, other]); get.assert_not_called()

    def test_mixed_or_multipart_item_not_rematched(self):
        self.queue()
        self.current.Media.append(NS(id='101', Part=[NS(id='201', file=r'T:\unrelated.mkv', size=1)]))
        with patch.object(self.identity, '_get') as get:
            self.identity.reconcile([self.current]); get.assert_not_called()

    def test_expected_imdb_not_first_title_candidate_wins(self):
        candidates = NS(MediaContainer=NS(SearchResult=[NS(guid=WRONG, name='The Runner'), NS(guid=CORRECT, name='Runner')]))
        def get(url, params=None):
            if '/matches' in url:
                self.assertEqual(params['title'], IMDB)
                return candidates
            if 'discover.provider.plex.tv' in url:
                imdb = IMDB if CORRECT.rsplit('/', 1)[1] in url else 'tt34564059'
                return NS(MediaContainer=NS(Metadata=[NS(Guid=[NS(id='imdb://' + imdb)])]))
            return None
        with patch.object(self.identity, '_get', side_effect=get):
            self.assertEqual(self.identity.exact_match('144671', IMDB).guid, CORRECT)

    def test_mismatching_or_missing_candidate_id_is_not_guessed(self):
        result = NS(MediaContainer=NS(SearchResult=[NS(guid=WRONG, name='Runner')]))
        with patch.object(self.identity, '_get', side_effect=[result, NS(MediaContainer=NS(Metadata=[NS(Guid=[])]))]):
            self.assertIsNone(self.identity.exact_match('144671', IMDB))

    def test_verified_rematch_updates_item_without_deletion(self):
        self.queue()
        correct = movie(imdb=IMDB); correct.title = 'Runner'; correct.guid = CORRECT
        responses = [self.current, self.current, correct]
        with patch.object(self.identity, '_local', side_effect=responses), \
                patch.object(self.identity, 'exact_match', return_value=NS(guid=CORRECT, name='Runner')), \
                patch.object(plex.session, 'put', return_value=NS(status_code=200)) as put, \
                patch.object(plex.session, 'delete') as delete, patch('os.remove') as remove:
            self.identity.reconcile([self.current])
            put.assert_called_once()
            self.assertTrue(put.call_args.args[0].endswith('/library/metadata/144671/match'))
            self.assertEqual(put.call_args.kwargs['params']['guid'], CORRECT)
            delete.assert_not_called(); remove.assert_not_called()
            self.assertEqual(self.current.title, 'Runner')
            self.assertEqual(self.current.EID, ['imdb://' + IMDB])
            self.assertEqual(self.identity._load(), [])

    def test_already_correct_is_noop_and_acknowledges_record(self):
        self.queue()
        correct = movie(imdb=IMDB)
        with patch.object(self.identity, '_local', return_value=correct), patch.object(plex.session, 'put') as put:
            self.identity.reconcile([self.current]); put.assert_not_called()
        self.assertEqual(self.identity._load(), [])

    def test_changed_file_before_put_is_not_rematched(self):
        self.queue()
        with patch.object(self.identity, '_local', side_effect=[self.current, movie(size=2)]), \
                patch.object(self.identity, 'exact_match', return_value=NS(guid=CORRECT, name='Runner')), \
                patch.object(plex.session, 'put') as put:
            self.identity.reconcile([self.current]); put.assert_not_called()

    def test_changed_file_after_put_not_acknowledged(self):
        self.queue()
        changed = movie(imdb=IMDB, size=1)
        with patch.object(self.identity, '_local', side_effect=[self.current, self.current, changed]), \
                patch.object(self.identity, 'exact_match', return_value=NS(guid=CORRECT, name='Runner')), \
                patch.object(plex.session, 'put', return_value=NS(status_code=200)):
            self.identity.reconcile([self.current])
        self.assertEqual(self.current.title, 'The Runner')
        self.assertEqual(len(self.identity._load()), 1)

    def test_one_request_failure_does_not_starve_other_items_or_drop_backoff(self):
        self.queue()
        other_record = dict(self.identity._load()[0], file=r'other\other.mkv')
        self.identity._save(self.identity._load() + [other_record])
        second = movie(path=r'T:\other\other.mkv'); second.ratingKey = '999'
        with patch.object(self.identity, '_repair', side_effect=[TimeoutError(), second]) as repair:
            self.identity.reconcile([self.current, second])
            self.assertEqual(repair.call_count, 2)
        remaining = self.identity._load()
        self.assertEqual(len(remaining), 1)
        self.assertIn('next_check', remaining[0])

    def test_failed_put_retains_existing_match_and_record(self):
        self.queue()
        with patch.object(self.identity, '_local', return_value=self.current), \
                patch.object(self.identity, 'exact_match', return_value=NS(guid=CORRECT, name='Runner')), \
                patch.object(plex.session, 'put', return_value=NS(status_code=500)):
            self.identity.reconcile([self.current])
        self.assertEqual(self.current.title, 'The Runner')
        self.assertEqual(len(self.identity._load()), 1)

    def test_corrupt_state_and_unwritable_state_do_not_raise(self):
        self.path.write_text('{broken')
        self.assertEqual(self.identity._load(), [])
        with patch('pathlib.Path.replace', side_effect=OSError('disk error')):
            self.assertFalse(self.queue())

    def test_download_hook_identity_failure_never_rejects_grab(self):
        obj = media(NS(type='movie', title='Runner', year=2026, EID=['imdb://' + IMDB],
                       version=NS(name='Main'), Releases=[self.release]))
        with patch.object(media, 'downloaded_versions', []), patch.object(media, 'session_releases', {}), \
                patch.object(self.identity, 'remember', side_effect=RuntimeError('offline')):
            obj.downloaded()
            self.assertIn('runner.2026 [Main]', media.downloaded_versions)


if __name__ == '__main__':
    unittest.main()