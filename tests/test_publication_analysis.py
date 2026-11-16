import json
import tempfile
import unittest
from pathlib import Path

from community_fitness.publication_analysis import PublicationAnalysis
from tests.helpers import Scenario, TEN, ELEVEN


class PublicationAnalysisTests(Scenario):
    def setUp(self):
        super().setUp()
        self.analysis = PublicationAnalysis(self.store)

    def _published(self):
        self.seed()
        self.score_all()
        self.close()
        return self.publish()

    def _codes(self, report):
        return {notice['code'] for notice in report['notices']}

    def test_no_published_version_is_not_official(self):
        self.seed()
        report = self.analysis.explain('evt')
        self.assertIsNone(report['official'])
        self.assertEqual(report['summary']['status'], 'unpublished')
        self.assertIn('no_published_version', self._codes(report))
        self.assertFalse(report['summary']['official'])
        self.assertIsNone(report['visible_version']['version'])

    def test_in_sync_after_publication(self):
        self._published()
        report = self.analysis.explain('evt')
        self.assertEqual(report['summary']['status'], 'in_sync')
        self.assertEqual(report['summary']['official_version'], 1)
        self.assertFalse(report['summary']['live_ahead'])
        self.assertTrue(report['summary']['official'])
        self.assertEqual(report['metrics']['delta'], [])
        self.assertIn('in_sync', self._codes(report))

    def test_late_service_record_is_version_lag_not_incident(self):
        self._published()
        self.services.record_encounter('evt', 'e1', 'tea', ELEVEN, 'v1', 3)
        report = self.analysis.explain('evt')
        self.assertEqual(report['summary']['status'], 'version_lag')
        self.assertIn('version_lag', self._codes(report))
        self.assertNotIn('unexplained_difference', self._codes(report))
        self.assertEqual(report['metrics']['official']['service_visits'], 0)
        self.assertEqual(report['metrics']['live']['service_visits'], 3)
        delta = {row['metric']: row for row in report['metrics']['delta']}
        self.assertEqual(delta['service_visits']['delta'], 3)
        self.assertEqual(delta['service_visits']['change_types'], ['late_service_record'])
        self.assertEqual(report['attribution']['expected_from_audit']['service_visits'], 3)
        self.assertEqual(report['summary']['official'], False)
        # Official version is untouched and still what the export serves.
        self.assertEqual(self.publication.get('evt')['services']['total_visits'], 0)
        self.assertEqual(report['services_detail']['zone_changes'][0]['zone_code'], 'tea')

    def test_old_service_record_backfill_is_flagged_as_backfill(self):
        self._published()
        # Occurred before publication but submitted after it.
        self.services.record_encounter('evt', 'e1', 'tea', TEN, 'v1', 2)
        report = self.analysis.explain('evt')
        self.assertEqual(report['changes'][0]['arrival'], 'backfill')
        self.assertEqual(report['attribution']['by_change_type'][0]['backfilled'], 1)

    def test_late_check_in_changes_only_attendance(self):
        self._published()
        self.attendance.check_in('evt', 't0', 'm1', 'north', ELEVEN, 'r1')
        report = self.analysis.explain('evt')
        delta = {row['metric']: row for row in report['metrics']['delta']}
        self.assertEqual(set(delta), {'attendance_arrived'})
        self.assertEqual(delta['attendance_arrived']['change_types'], ['late_check_in'])
        self.assertEqual(report['attendance_detail']['gate_changes'][0]['gate'], 'north')
        self.assertEqual(report['metrics']['official']['service_visits'], 0)

    def _delta(self, report):
        return {row['metric']: row for row in report['metrics']['delta']}

    def _audited_score_revision(self, team_id, judge_id, total):
        '''Simulate a revision recorded by an integration after publication.

        The public API refuses score edits once the event is closed, so a real
        revision after publication can only arrive through an audited write.
        '''
        with self.store.transaction() as db:
            db.execute("UPDATE score_cards SET revision=revision+1,total=?,submitted_at=? "
                       "WHERE event_id='evt' AND team_id=? AND judge_id=?",
                       (str(total), TEN, team_id, judge_id))
            db.execute("INSERT INTO score_history VALUES('evt',?,?,2,?,?,?,?)",
                       (team_id, judge_id, json.dumps({'form': '60', 'rhythm': '40'}),
                        str(total), TEN, f'rev-{team_id}-{judge_id}'))
            self.store.audit(db, 'evt', 'score_submitted', 'team', team_id,
                             {'judge': judge_id, 'revision': 2, 'total': str(total)}, TEN)
            self.store.bump(db, 'evt')

    def test_score_revision_drives_standings_fields(self):
        self._published()
        self._audited_score_revision('t0', 'j0', 100)
        report = self.analysis.explain('evt')
        changed = {row['team_id']: row for row in report['standings_detail']['changed']}
        self.assertIn('t0', changed)
        self.assertIn('score', changed['t0']['fields'])
        self.assertEqual(changed['t0']['change_types'], ['score_card_revision'])
        self.assertEqual(report['summary']['unpublished_data_changes'], 1)
        self.assertEqual(
            report['changes'][0]['affected_metrics'][:4],
            ['team_score', 'team_median', 'team_rank', 'team_criteria'])

    def test_replay_is_reported_but_moves_nothing(self):
        self._published()
        self.services.record_encounter('evt', 'e1', 'tea', ELEVEN, 'v1', 3)
        self.services.record_encounter('evt', 'e1', 'tea', ELEVEN, 'v1', 3)
        report = self.analysis.explain('evt')
        self.assertEqual(len(report['replays']), 1)
        self.assertEqual(report['replays'][0]['change_type'], 'request_replay')
        self.assertFalse(report['replays'][0]['data_change'])
        self.assertEqual(self._delta(report)['service_visits']['delta'], 3)
        self.assertEqual(report['attribution']['expected_from_audit']['service_visits'], 3)
        self.assertIn('replay_ignored', self._codes(report))
        self.assertNotIn('unexplained_difference', self._codes(report))

    def test_open_dispute_marks_contested_without_hiding_published_version(self):
        self._published()
        self.disputes.open('evt', 'd1', 't0', 'j0', 'Check card')
        report = self.analysis.explain('evt')
        self.assertEqual(report['summary']['status'], 'contested')
        self.assertEqual(report['summary']['official_version'], 1)
        self.assertEqual(report['disputes']['open'][0]['id'], 'd1')
        self.assertTrue(report['disputes']['published_version_remains_visible'])
        self.assertTrue(report['disputes']['blocks_new_version'])
        self.assertIsNotNone(report['official'])
        self.assertIn('unresolved_disputes', self._codes(report))

    def test_publish_after_late_records_creates_new_head(self):
        self._published()
        self.services.record_encounter('evt', 'e1', 'tea', ELEVEN, 'v1', 3)
        self.assertEqual(self.publish()['version'], 2)
        report = self.analysis.explain('evt')
        self.assertEqual(report['summary']['status'], 'in_sync')
        self.assertEqual(report['summary']['official_version'], 2)
        self.assertIsNone(report['summary']['next_version'])

    def test_boundary_uses_audit_sequence(self):
        self._published()
        report = self.analysis.explain('evt')
        boundary = report['official']['boundary']
        self.assertEqual(boundary['basis'], 'audit_sequence')
        self.assertFalse(boundary['ambiguous'])
        self.assertIsInstance(boundary['sequence'], int)

    def test_missing_publication_audit_marks_boundary_ambiguous(self):
        self._published()
        with self.store.transaction() as db:
            db.execute("DELETE FROM audit WHERE action='snapshot_published'")
        report = self.analysis.explain('evt')
        self.assertTrue(report['official']['boundary']['ambiguous'])
        self.assertIn('boundary_ambiguous', self._codes(report))

    def test_unexplained_difference_is_quality_incident(self):
        self._published()
        # Bypass the service layer, as an out-of-band write would.
        with self.store.transaction() as db:
            db.execute('INSERT INTO encounters VALUES(?,?,?,?,?,?,?,?)',
                       ('evt', 'shadow', 'tea', ELEVEN, 'v9', 5, '{}', ELEVEN))
        report = self.analysis.explain('evt')
        self.assertEqual(report['summary']['status'], 'quality_incident')
        self.assertIn('unexplained_difference', self._codes(report))
        metrics = {item['metric'] for item in report['attribution']['unexplained']}
        self.assertIn('service_visits', metrics)

    def test_regressed_scores_are_incident_not_lag(self):
        self._published()
        with self.store.transaction() as db:
            db.execute("DELETE FROM score_cards WHERE team_id='t1' AND judge_id='j1'")
        report = self.analysis.explain('evt')
        self.assertEqual(report['summary']['status'], 'quality_incident')
        self.assertIn('scores_regressed', self._codes(report))

    def test_batch_rejections_surfaced_after_publish(self):
        self._published()
        self.services.ingest_batch('evt', 'batch1', [
            {'id': 'e1', 'zone_code': 'tea', 'occurred_at': ELEVEN,
             'visitor_token': 'v1', 'count': 1},
            {'id': 'e2', 'zone_code': 'tea', 'occurred_at': ELEVEN,
             'visitor_token': 'v2', 'count': 0},
        ])
        report = self.analysis.explain('evt')
        self.assertIn('ingest_rejections', self._codes(report))
        self.assertEqual(report['attribution']['expected_from_audit']['service_visits'], 1)

    def test_publish_and_live_record_interleaved_is_consistent(self):
        self.seed()
        self.score_all()
        self.close()
        self.publish()
        self.services.record_encounter('evt', 'e1', 'tea', ELEVEN, 'v1', 2)
        self.publish()
        self.services.record_encounter('evt', 'e2', 'tea', ELEVEN, 'v2', 4)
        report = self.analysis.explain('evt')
        # Version 2 is official; only the record after it is unattributed-live.
        self.assertEqual(report['summary']['official_version'], 2)
        self.assertEqual(report['metrics']['official']['service_visits'], 2)
        self.assertEqual(report['metrics']['live']['service_visits'], 6)
        self.assertEqual(self._delta(report)['service_visits']['delta'], 4)
        self.assertEqual(self._delta(report)['service_encounters']['delta'], 1)
        self.assertEqual(report['summary']['unpublished_data_changes'], 1)

    def test_unpublished_data_never_reported_as_official(self):
        self._published()
        self.services.record_encounter('evt', 'e1', 'tea', ELEVEN, 'v1', 3)
        report = self.analysis.explain('evt')
        self.assertFalse(report['summary']['official'])
        for change in report['changes']:
            self.assertFalse(change['official'])
        self.assertEqual(report['official']['metrics']['service_visits'], 0)
        self.assertEqual(report['official']['version'], 1)


if __name__ == '__main__':
    unittest.main()
