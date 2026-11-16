"""Explain why the live collection differs from the publicly visible snapshot.

A published snapshot is frozen, while statistics and export endpoints are
computed from the live tables.  Operators reviewing an event after release see
two numbers and need one place that states:

* how far collection has progressed (and through which transaction sequence),
* which version is actually published and served by the exports,
* which concrete records arrived after that version, and which metrics they move.

Ordering uses ``audit.seq``.  Audit rows are written in the same transaction as
the data they describe, so sequence order stays exact when a publication and a
field record commit at the same wall-clock instant, or when two hosts' clocks
disagree.  Wall-clock timestamps are only a fallback for a snapshot with no
publication audit row; such a boundary is reported as ambiguous instead of
being trusted.

The view never promotes live values to official results.  Every record and
notice carries an ``official`` flag, the summary separates normal version lag
from a real quality incident, and request replays are listed separately because
they change no metric.
"""

import json
from collections import Counter, defaultdict

from .analytics import Analytics
from .attendance import Attendance
from .disputes import Disputes
from .storage import Store
from .validation import instant, zone


#: Static answer to "which indicators can this kind of change move?".  The
#: dynamic attribution below reports which ones actually moved.
IMPACT = {
    'score_card_revision': {
        'label': '评分卡修订',
        'entity': 'score_card',
        'metrics': ['team_score', 'team_median', 'team_rank', 'team_criteria',
                    'teams_ranked', 'teams_complete', 'score_cards'],
        'data_change': True,
    },
    'late_service_record': {
        'label': '现场晚到服务记录',
        'entity': 'encounter',
        'metrics': ['service_encounters', 'service_visits', 'unique_visitors',
                    'zone_encounters', 'zone_visits', 'day_encounters', 'day_visits'],
        'data_change': True,
    },
    'late_check_in': {
        'label': '签到补记',
        'entity': 'attendance',
        'metrics': ['attendance_arrived', 'gate_arrivals', 'team_arrivals'],
        'data_change': True,
    },
    'request_replay': {
        'label': '请求重放',
        'entity': 'request',
        'metrics': [],
        'data_change': False,
    },
    'dispute_activity': {
        'label': '评分争议',
        'entity': 'dispute',
        'metrics': ['open_disputes', 'publishable'],
        'data_change': False,
    },
    'batch_ingest': {
        'label': '批量导入汇总',
        'entity': 'request',
        'metrics': [],
        'data_change': False,
    },
    'structural_change': {
        'label': '结构或规则变更',
        'entity': 'event',
        'metrics': [],
        'data_change': False,
    },
    'other': {
        'label': '其他审计事件',
        'entity': 'audit',
        'metrics': [],
        'data_change': False,
    },
}

CHANGE_ACTION = {
    'score_submitted': 'score_card_revision',
    'encounter_recorded': 'late_service_record',
    'member_checked_in': 'late_check_in',
    'encounter_batch': 'batch_ingest',
    'zone_added': 'structural_change',
    'group_added': 'structural_change',
    'team_registered': 'structural_change',
    'judge_assigned': 'structural_change',
    'state_changed': 'structural_change',
    'scoring_session_opened': 'structural_change',
    'scoring_session_closed': 'structural_change',
}
REPLAY_ACTION = {
    'score_replay': 'score_card',
    'encounter_replay': 'encounter',
    'encounter_batch_replay': 'encounter_batch',
    'attendance_replay': 'attendance',
}
DISPUTE_ACTION = {'dispute_opened', 'dispute_resolved'}

METRIC_LABELS = {
    'teams_total': '队伍数',
    'teams_complete': '评分完整队伍数',
    'teams_ranked': '参与排名队伍数',
    'score_cards': '已收评分卡数',
    'service_encounters': '服务记录数',
    'service_visits': '服务人次',
    'unique_visitors': '去重访客数',
    'attendance_registered': '报名人数',
    'attendance_arrived': '签到人数',
}


class PublicationAnalysis:
    def __init__(self, store: Store):
        self.store = store
        self.analytics = Analytics(store)
        self.attendance = Attendance(store)
        self.disputes = Disputes(store)

    # ------------------------------------------------------------ metric views
    @staticmethod
    def _score_view(scores: dict) -> dict:
        teams = [team for group in scores['groups'] for team in group['teams']]
        return {
            'teams_total': len(teams),
            'teams_complete': sum(1 for team in teams if team['complete']),
            'teams_ranked': sum(1 for team in teams if team.get('rank') is not None),
            'score_cards': sum(team['card_count'] for team in teams),
            'scores_complete': bool(scores['complete']),
        }

    @classmethod
    def _service_view(cls, services: dict) -> dict:
        return {'service_encounters': services['total_encounters'],
                'service_visits': services['total_visits'],
                'unique_visitors': services['unique_visitors']}

    @staticmethod
    def _attendance_view(summary: dict) -> dict:
        return {'attendance_registered': summary['registered_members'],
                'attendance_arrived': summary['arrived_members']}

    def _live_metrics(self, db, event: dict) -> dict:
        scores = self.analytics._scores(db, event['id'])
        services = self.analytics._services(db, event)
        attendance = self.attendance._summary(db, event['id'])
        return {**self._score_view(scores), **self._service_view(services),
                **self._attendance_view(attendance)}

    def _official_metrics(self, payload: dict) -> dict:
        scores = payload['scores']
        return {**self._score_view(scores),
                **self._service_view(payload['services']),
                **self._attendance_view(payload['attendance'])}

    @staticmethod
    def _metric_deltas(official: dict, live: dict) -> list[dict]:
        rows = []
        for key, label in METRIC_LABELS.items():
            before, after = official.get(key), live.get(key)
            if before == after:
                continue
            rows.append({'metric': key, 'label': label, 'official': before,
                         'live': after, 'delta': after - before,
                         'direction': 'increase' if after > before else 'decrease'})
        return rows

    # ------------------------------------------------------- head and boundary
    @staticmethod
    def _head(db, event_id: str) -> dict | None:
        row = db.execute(
            'SELECT version,source_revision,created_at,payload FROM snapshots '
            'WHERE event_id=? AND status=? ORDER BY version DESC LIMIT 1',
            (event_id, 'published')).fetchone()
        if row is None:
            return None
        return {'version': row['version'], 'source_revision': row['source_revision'],
                'published_at': row['created_at'], 'payload': json.loads(row['payload'])}

    @staticmethod
    def _boundary(db, event_id: str, version: int, published_at: str) -> dict:
        rows = Store.many(
            db, 'SELECT seq,at FROM audit WHERE event_id=? AND action=? AND entity=? '
                'AND entity_id=? ORDER BY seq',
            (event_id, 'snapshot_published', 'snapshot', str(version)))
        if rows:
            return {'basis': 'audit_sequence', 'sequence': rows[-1]['seq'],
                    'at': rows[-1]['at'], 'ambiguous': False}
        return {'basis': 'published_at', 'sequence': None, 'at': published_at,
                'ambiguous': True}

    @staticmethod
    def _audit_since(db, event_id: str, boundary: dict) -> list[dict]:
        if boundary['sequence'] is not None:
            return Store.many(db, 'SELECT * FROM audit WHERE event_id=? AND seq>? '
                                  'ORDER BY seq', (event_id, boundary['sequence']))
        return Store.many(db, 'SELECT * FROM audit WHERE event_id=? AND at>=? ORDER BY seq',
                          (event_id, boundary['at']))

    @staticmethod
    def _published_versions(db, event_id: str) -> list[dict]:
        return Store.many(db, 'SELECT version,source_revision,created_at FROM snapshots '
                              'WHERE event_id=? AND status=? ORDER BY version',
                          (event_id, 'published'))

    # --------------------------------------------------------------- classify
    def _classify(self, db, event: dict, boundary: dict,
                  rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        event_id = event['id']
        local = zone(event['timezone'])
        published_at = instant(boundary['at'])
        encounters = {row['id']: row for row in Store.many(
            db, 'SELECT * FROM encounters WHERE event_id=?', (event_id,))}
        arrivals = {(row['team_id'], row['member_ref']): row for row in Store.many(
            db, 'SELECT * FROM attendance WHERE event_id=?', (event_id,))}
        cards = {(row['team_id'], row['judge_id']): row for row in Store.many(
            db, 'SELECT * FROM score_cards WHERE event_id=?', (event_id,))}

        changes, replays, disputes = [], [], []
        for row in rows:
            action = row['action']
            detail = json.loads(row['detail']) if row['detail'] else {}
            base = {'sequence': row['seq'], 'at': row['at'], 'action': action,
                    'entity': row['entity'], 'entity_id': row['entity_id'],
                    'detail': detail, 'official': False}
            if action in REPLAY_ACTION:
                replays.append({**base, 'change_type': 'request_replay',
                                'replayed_entity': REPLAY_ACTION[action],
                                'data_change': False})
                continue
            if action in DISPUTE_ACTION:
                disputes.append({**base, 'change_type': 'dispute_activity',
                                 'data_change': False})
                continue
            change_type = CHANGE_ACTION.get(action, 'other')
            record = {**base, 'change_type': change_type,
                      'label': IMPACT[change_type]['label'],
                      'data_change': IMPACT[change_type]['data_change'],
                      'affected_metrics': IMPACT[change_type]['metrics']}
            if change_type == 'late_service_record':
                encounter = encounters.get(row['entity_id'])
                record['arrival'] = 'unknown'
                if encounter:
                    occurred = instant(encounter['occurred_at'])
                    record.update({
                        'occurred_at': encounter['occurred_at'],
                        'local_date': occurred.astimezone(local).date().isoformat(),
                        'zone_code': encounter['zone_code'],
                        'count': encounter['count'],
                        # Arrival compares when the event happened with the cut
                        # of the published version: ``backfill`` fills a gap
                        # that the official version never saw, while
                        # ``new_after_publish`` is activity that only started
                        # after release.  That it was *submitted* after the cut
                        # follows from its audit sequence, not from this field.
                        'arrival': ('backfill' if occurred < published_at
                                    else 'new_after_publish'),
                    })
            elif change_type == 'late_check_in':
                member = detail.get('member_ref')
                arrival = arrivals.get((row['entity_id'], member))
                record.update({'member_ref': member, 'gate': detail.get('gate'),
                               'arrival': 'unknown'})
                if arrival:
                    record['arrived_at'] = arrival['arrived_at']
                    record['arrival'] = ('backfill' if instant(arrival['arrived_at']) < published_at
                                         else 'new_after_publish')
            elif change_type == 'score_card_revision':
                card = cards.get((row['entity_id'], detail.get('judge')))
                record.update({'judge_id': detail.get('judge'),
                               'card_revision': detail.get('revision'),
                               'total': detail.get('total'),
                               'card_present': card is not None,
                               'criteria': list(json.loads(card['scores']))
                               if card else []})
            elif change_type == 'batch_ingest':
                record.update({'accepted': detail.get('accepted', 0),
                               'rejected': detail.get('rejected', 0)})
            changes.append(record)
        return changes, replays, disputes

    # ------------------------------------------------------------ attribution
    @staticmethod
    def _attribution(official: dict, live: dict, changes: list[dict],
                     replays: list[dict], disputes: list[dict],
                     ambiguous: bool) -> dict:
        deltas = PublicationAnalysis._metric_deltas(official, live)
        drivers = defaultdict(set)
        observed = defaultdict(lambda: defaultdict(int))
        for record in changes:
            change_type = record['change_type']
            if change_type == 'late_service_record' and record['arrival'] != 'unknown':
                drivers['service_encounters'].add(change_type)
                drivers['service_visits'].add(change_type)
                drivers['unique_visitors'].add(change_type)
                observed[change_type]['service_encounters'] += 1
                observed[change_type]['service_visits'] += record['count']
            elif change_type == 'late_check_in' and record['arrival'] != 'unknown':
                drivers['attendance_arrived'].add(change_type)
                observed[change_type]['attendance_arrived'] += 1
            elif change_type == 'score_card_revision' and record['card_present']:
                for metric in ('teams_ranked', 'teams_complete', 'score_cards'):
                    drivers[metric].add(change_type)

        expected = {
            'service_encounters': sum(
                1 for record in changes
                if record['change_type'] == 'late_service_record'
                and record['arrival'] != 'unknown'),
            'service_visits': sum(
                record.get('count', 0) for record in changes
                if record['change_type'] == 'late_service_record'
                and record['arrival'] != 'unknown'),
            'attendance_arrived': sum(
                1 for record in changes
                if record['change_type'] == 'late_check_in'
                and record['arrival'] != 'unknown'),
        }

        by_metric, unexplained = [], []
        for row in deltas:
            metric = row['metric']
            names = sorted(drivers.get(metric, ()))
            matching = sum(1 for record in changes
                           if record['change_type'] in names and record['data_change'])
            by_metric.append({**row, 'change_types': names, 'record_count': matching})
            if not names:
                unexplained.append({'metric': metric, 'delta': row['delta'],
                                    'reason': 'no_audited_change_explains_delta'})
        if not ambiguous:
            actual = {row['metric']: row['delta'] for row in deltas}
            for metric, value in expected.items():
                if actual.get(metric, 0) != value:
                    unexplained.append({'metric': metric,
                                        'delta': actual.get(metric, 0),
                                        'expected_from_audit': value,
                                        'reason': 'audit_and_metric_delta_disagree'})

        by_change_type = []
        for change_type, impact in IMPACT.items():
            records = [record for record in changes
                       if record['change_type'] == change_type]
            if not records:
                continue
            by_change_type.append({
                'change_type': change_type, 'label': impact['label'],
                'record_count': len(records),
                'data_change': impact['data_change'],
                'backfilled': sum(1 for record in records
                                  if record.get('arrival') == 'backfill'),
                'new_after_publish': sum(1 for record in records
                                         if record.get('arrival') == 'new_after_publish'),
                'affected_metrics': impact['metrics'],
                'observed_metric_delta': dict(sorted(observed[change_type].items())),
            })
        return {'metrics': by_metric, 'by_change_type': by_change_type,
                'unexplained': unexplained, 'expected_from_audit': expected,
                'replay_count': len(replays),
                'dispute_activity_count': len(disputes)}

    # ---------------------------------------------------------------- details
    @staticmethod
    def _standings_detail(payload: dict, live_scores: dict,
                          changes: list[dict]) -> dict:
        old = {team['team_id']: team for group in payload['scores']['groups']
               for team in group['teams']}
        new = {team['team_id']: team for group in live_scores['groups']
               for team in group['teams']}
        revised = {record['entity_id'] for record in changes
                   if record['change_type'] == 'score_card_revision'}
        rows = []
        for team_id in sorted(set(old) | set(new)):
            before, after = old.get(team_id), new.get(team_id)
            fields = [field for field in ('score', 'median', 'rank', 'criteria')
                      if (before or {}).get(field) != (after or {}).get(field)]
            if not fields:
                continue
            rows.append({
                'team_id': team_id,
                'published': {key: (before or {}).get(key)
                              for key in ('score', 'median', 'rank')},
                'live': {key: (after or {}).get(key)
                         for key in ('score', 'median', 'rank')},
                'fields': fields,
                'change_types': ['score_card_revision'] if team_id in revised else [],
            })
        return {'changed': rows, 'changed_count': len(rows),
                'rank_changes': sum(1 for row in rows if 'rank' in row['fields'])}

    @staticmethod
    def _services_detail(payload: dict, live_services: dict) -> dict:
        def diff(before: dict | None, after: dict | None, keys: tuple) -> dict:
            return {key: (after or {}).get(key, 0) - (before or {}).get(key, 0)
                    for key in keys
                    if (after or {}).get(key) != (before or {}).get(key)}

        fields = ('encounters', 'visits', 'unique_visitors')
        old_zones = {row['code']: row for row in payload['services']['zones']}
        new_zones = {row['code']: row for row in live_services['zones']}
        zones = []
        for code in sorted(set(old_zones) | set(new_zones)):
            delta = diff(old_zones.get(code), new_zones.get(code), fields)
            if not delta:
                continue
            zones.append({
                'zone_code': code,
                'published': {key: (old_zones.get(code) or {}).get(key) for key in fields},
                'live': {key: (new_zones.get(code) or {}).get(key) for key in fields},
                'delta': delta, 'change_types': ['late_service_record'],
            })
        old_days = {row['date']: row for row in payload['services']['days']}
        new_days = {row['date']: row for row in live_services['days']}
        days = []
        for day in sorted(set(old_days) | set(new_days)):
            delta = diff(old_days.get(day), new_days.get(day), fields)
            if delta:
                days.append({'date': day, 'delta': delta})
        return {'zone_changes': zones, 'day_changes': days,
                'changed_zone_count': len(zones), 'changed_day_count': len(days)}

    @staticmethod
    def _attendance_detail(payload: dict, live_summary: dict,
                           changes: list[dict]) -> dict:
        old = {row['team_id']: row for row in payload['attendance']['teams']}
        new = {row['team_id']: row for row in live_summary['teams']}
        checked_in = {record['entity_id'] for record in changes
                      if record['change_type'] == 'late_check_in'}
        teams = []
        for team_id in sorted(set(old) | set(new)):
            before, after = old.get(team_id), new.get(team_id)
            if (before or {}).get('arrived') == (after or {}).get('arrived'):
                continue
            teams.append({
                'team_id': team_id,
                'published_arrived': (before or {}).get('arrived'),
                'live_arrived': (after or {}).get('arrived'),
                'delta': (after or {}).get('arrived', 0) - (before or {}).get('arrived', 0),
                'change_types': ['late_check_in'] if team_id in checked_in else [],
            })
        old_gates = payload['attendance'].get('by_gate', {})
        new_gates = live_summary.get('by_gate', {})
        gates = [{'gate': gate, 'published': old_gates.get(gate, 0),
                  'live': new_gates.get(gate, 0), 'change_types': ['late_check_in']}
                 for gate in sorted(set(old_gates) | set(new_gates))
                 if old_gates.get(gate, 0) != new_gates.get(gate, 0)]
        return {'team_changes': teams, 'gate_changes': gates,
                'changed_team_count': len(teams)}

    # ---------------------------------------------------------------- notices
    def _notices(self, event: dict, head: dict | None, boundary: dict | None,
                 changes: list[dict], replays: list[dict], disputes: list[dict],
                 open_disputes: list[dict], explanation: dict,
                 publishable: bool, publish_reasons: list[str]) -> list[dict]:
        notices = []

        def add(code, severity, message, official=False, detail=None):
            notices.append({'code': code, 'severity': severity,
                            'official': official, 'message': message,
                            'detail': detail or {}})

        data_changes = [record for record in changes if record['data_change']]
        if head is None:
            add('no_published_version', 'warning',
                '尚无对外发布的版本：实时采集到的数据只是现场记录，不能作为正式结果引用。',
                detail={'live_only': True})
        elif boundary['ambiguous']:
            add('boundary_ambiguous', 'warning',
                f'第 {head["version"]} 版缺少发布审计序号，只能按发布时间近似划分新旧数据；'
                '边界附近记录可能归组不准，差异仅供排查。',
                official=True, detail={'basis': boundary['basis'],
                                       'published_at': boundary['at']})
        if head is not None and data_changes and not boundary['ambiguous']:
            counts = Counter(record['change_type'] for record in data_changes)
            add('version_lag', 'info',
                f'对外仍为第 {head["version"]} 版（发布后未再生成新版），实时采集已继续；'
                '这是正常的版本滞后，不是数据质量事故。',
                detail={'official_version': head['version'],
                        'unpublished_records': len(data_changes),
                        'by_change_type': dict(sorted(counts.items()))})
            add('unpublished_changes', 'info',
                f'有 {len(data_changes)} 条变化尚未进入任何对外版本，导出 CSV 不会包含它们。',
                detail={'records': [{'sequence': record['sequence'],
                                     'change_type': record['change_type'],
                                     'entity': record['entity'],
                                     'entity_id': record['entity_id']}
                                    for record in data_changes]})
        elif head is not None and not explanation['unexplained']:
            add('in_sync', 'info',
                f'实时采集与对外第 {head["version"]} 版一致，未发现发布后的数据变化。',
                official=True, detail={'official_version': head['version']})
        if open_disputes:
            add('unresolved_disputes', 'warning',
                f'有 {len(open_disputes)} 项评分争议未结；对外可见的仍是第 '
                f'{head["version"] if head else "—"} 版，结果处于争议中。'
                '未结争议不是质量事故，但也不能把实时数据当作无争议结果。',
                official=bool(head),
                detail={'disputes': [{'id': row['id'], 'team_id': row['team_id'],
                                      'judge_id': row['card_judge_id']}
                                     for row in open_disputes],
                        'published_remains_visible': bool(head),
                        'blocks_new_version': True})
        if replays:
            add('replay_ignored', 'info',
                f'检测到 {len(replays)} 次请求重放；重放不新增记录，也不影响任何指标。',
                official=bool(head),
                detail={'replays': [{'sequence': row['sequence'], 'action': row['action'],
                                     'entity': row['entity'],
                                     'replayed_entity': row['replayed_entity']}
                                    for row in replays],
                        'affects_metrics': False})
        rejected = sum(record.get('rejected', 0) for record in changes
                       if record['change_type'] == 'batch_ingest')
        if rejected:
            add('ingest_rejections', 'warning',
                f'发布后批量导入有 {rejected} 行被拒收；被拒行未写入数据，'
                '按行修正后重报即可，不构成对已发布版本的修改。',
                detail={'rejected_rows': rejected})
        if explanation['unexplained']:
            add('unexplained_difference', 'critical',
                '实时口径与对外版本的差异无法完全由审计记录解释，'
                '请核对是否有绕过接口的写入或缺少审计序号的数据。',
                detail={'items': explanation['unexplained']})
        if explanation.get('scores_regressed'):
            add('scores_regressed', 'critical',
                '对外版本评分完整，但实时评分卡已不完整；这属于数据质量异常，不是版本滞后。')
        if head is not None and not publishable:
            add('new_version_blocked', 'info', '实时采集的变化暂时不能形成新版本。',
                detail={'reasons': publish_reasons})
        elif head is not None and publishable and data_changes:
            add('new_version_ready', 'info',
                f'实时数据已可发布为第 {head["version"] + 1} 版；'
                '在新版生成前，对外结果仍以当前版本为准。',
                detail={'next_version': head['version'] + 1})
        elif head is None and not publishable:
            add('publication_not_possible', 'info', '当前也不能发布新版本。',
                detail={'reasons': publish_reasons})
        return notices

    @staticmethod
    def _summary_note(head: dict | None, changes: list[dict], replays: list[dict],
                      open_disputes: list[dict]) -> str:
        data_changes = [record for record in changes if record['data_change']]
        parts = [f'对外可见版本为第 {head["version"]} 版' if head else '尚无对外版本']
        if data_changes:
            counts = Counter(record['change_type'] for record in data_changes)
            labels = {'score_card_revision': '评分卡修订',
                      'late_service_record': '服务记录',
                      'late_check_in': '签到补记'}
            parts.append('实时采集另有 ' + '、'.join(
                f'{counts[key]} 条{labels[key]}' for key in labels if counts[key]))
        else:
            parts.append('实时采集未发现发布后的数据变化')
        if replays:
            parts.append(f'{len(replays)} 次重放未产生数据变化')
        if open_disputes:
            parts.append(f'{len(open_disputes)} 项争议未结')
        return '；'.join(parts) + '。'

    @staticmethod
    def _status(head: dict | None, explanation: dict, live_ahead: bool,
                open_disputes: list[dict]) -> str:
        if head is None:
            return 'unpublished'
        if explanation.get('scores_regressed') or explanation['unexplained']:
            return 'quality_incident'
        if open_disputes:
            return 'contested'
        return 'version_lag' if live_ahead else 'in_sync'

    # ----------------------------------------------------------------- public
    def explain(self, event_id: str) -> dict:
        with self.store.consistent_read() as db:
            event = self.store.one(db, 'SELECT * FROM events WHERE id=?', (event_id,))
            head = self._head(db, event_id)
            live_scores = self.analytics._scores(db, event_id)
            live_services = self.analytics._services(db, event)
            live_attendance = self.attendance._summary(db, event_id)
            live_metrics = {**self._score_view(live_scores),
                            **self._service_view(live_services),
                            **self._attendance_view(live_attendance)}
            open_disputes = self.disputes.unresolved(db, event_id)
            latest = Store.many(db, 'SELECT seq,at FROM audit WHERE event_id=? '
                                    'ORDER BY seq DESC LIMIT 1', (event_id,))
            earliest = Store.many(db, 'SELECT seq FROM audit WHERE event_id=? '
                                      'ORDER BY seq LIMIT 1', (event_id,))
            cut = latest[0]['seq'] if latest else 0
            streams = [
                {'stream': 'score_cards',
                 **Store.one(db, 'SELECT COUNT(*) AS records, '
                                 'MAX(submitted_at) AS last_submitted_at '
                                 'FROM score_cards WHERE event_id=?', (event_id,)),
                 'teams_complete': live_metrics['teams_complete'],
                 'teams_total': live_metrics['teams_total']},
                {'stream': 'service_encounters',
                 **Store.one(db, 'SELECT COUNT(*) AS records, '
                                 'MAX(recorded_at) AS last_recorded_at, '
                                 'MAX(occurred_at) AS last_occurred_at '
                                 'FROM encounters WHERE event_id=?', (event_id,)),
                 'visits': live_metrics['service_visits']},
                {'stream': 'attendance',
                 **Store.one(db, 'SELECT COUNT(*) AS records, '
                                 'MAX(arrived_at) AS last_arrived_at '
                                 'FROM attendance WHERE event_id=?', (event_id,)),
                 'registered': live_metrics['attendance_registered']},
            ]
            versions = self._published_versions(db, event_id)

            if head is None:
                boundary = None
                official_metrics = {}
                changes, replays, dispute_changes = [], [], []
                explanation = {'metrics': [], 'by_change_type': [],
                               'unexplained': [], 'expected_from_audit': {},
                               'replay_count': 0, 'dispute_activity_count': 0}
            else:
                official_metrics = self._official_metrics(head['payload'])
                boundary = self._boundary(db, event_id, head['version'],
                                          head['published_at'])
                changes, replays, dispute_changes = self._classify(
                    db, event, boundary, self._audit_since(db, event_id, boundary))
                explanation = self._attribution(
                    official_metrics, live_metrics, changes, replays,
                    dispute_changes, boundary['ambiguous'])

            explanation['live_scores_complete'] = live_scores['complete']
            explanation['scores_regressed'] = bool(
                head is not None and head['payload']['scores']['complete']
                and not live_scores['complete'])
            if boundary and boundary['ambiguous']:
                explanation['unexplained'] = [
                    item for item in explanation['unexplained']
                    if item['reason'] != 'audit_and_metric_delta_disagree']

            data_changes = [record for record in changes if record['data_change']]
            live_ahead = bool(data_changes) or any(
                row['delta'] != 0 for row in explanation['metrics'])
            publish_reasons = []
            if event['state'] != 'closed':
                publish_reasons.append('event_not_closed')
            if not live_scores['complete']:
                publish_reasons.append('incomplete_scores')
            if open_disputes:
                publish_reasons.append('unresolved_disputes')
            publishable = not publish_reasons
            status = self._status(head, explanation, live_ahead, open_disputes)

            if head is None:
                standings = {'changed': [], 'changed_count': 0, 'rank_changes': 0}
                services_detail = {'zone_changes': [], 'day_changes': [],
                                   'changed_zone_count': 0, 'changed_day_count': 0}
                attendance_detail = {'team_changes': [], 'gate_changes': [],
                                     'changed_team_count': 0}
            else:
                standings = self._standings_detail(head['payload'], live_scores, changes)
                services_detail = self._services_detail(head['payload'], live_services)
                attendance_detail = self._attendance_detail(
                    head['payload'], live_attendance, changes)

            notices = self._notices(event, head, boundary, changes, replays,
                                    dispute_changes, open_disputes, explanation,
                                    publishable, publish_reasons)

            official = None
            if head is not None:
                official = {
                    'version': head['version'], 'status': 'published',
                    'published_at': head['published_at'],
                    'source_revision': head['source_revision'],
                    'immutable': True,
                    'boundary': boundary,
                    'metrics': official_metrics,
                    'exports': {
                        'standings_csv': (f'/events/{event_id}/export/standings.csv'
                                          f'?version={head["version"]}'),
                        'services_csv': (f'/events/{event_id}/export/services.csv'
                                         f'?version={head["version"]}'),
                        'snapshot': f'/events/{event_id}/snapshots/{head["version"]}',
                    },
                }
            return {
                'event_id': event_id,
                'summary': {
                    'status': status,
                    'official_version': head['version'] if head else None,
                    'live_ahead': live_ahead,
                    'official_cut_sequence': boundary['sequence'] if boundary else None,
                    'collection_cut_sequence': cut,
                    'unpublished_data_changes': len(data_changes),
                    'replays_ignored': len(replays),
                    'open_disputes': len(open_disputes),
                    'publishable': publishable,
                    'next_version': (head['version'] + 1
                                     if head and publishable and data_changes else None),
                    'note': self._summary_note(head, changes, replays, open_disputes),
                    'official': head is not None and not live_ahead,
                },
                'official': official,
                'visible_version': {
                    'version': head['version'] if head else None,
                    'used_by_default_export': True,
                    'versions': versions,
                    'note': ('默认导出与“当前版本”查询使用对外可见的最新发布版本；'
                             '带 version 参数的导出一律按该版本原样返回。'),
                },
                'collection': {
                    'event_state': event['state'],
                    'source_revision': event['revision'],
                    'cut_sequence': cut,
                    'cut_at': latest[0]['at'] if latest else None,
                    'first_sequence': earliest[0]['seq'] if earliest else None,
                    'streams': streams,
                },
                'metrics': {
                    'official': official_metrics,
                    'live': live_metrics,
                    'delta': explanation['metrics'],
                },
                'attribution': {
                    'by_change_type': explanation['by_change_type'],
                    'expected_from_audit': explanation['expected_from_audit'],
                    'unexplained': explanation['unexplained'],
                    'impact_map': [{'change_type': key, **value}
                                   for key, value in IMPACT.items()],
                },
                'changes': changes,
                'replays': replays,
                'disputes': {
                    'open': [{'id': row['id'], 'team_id': row['team_id'],
                              'judge_id': row['card_judge_id']}
                             for row in open_disputes],
                    'activity': dispute_changes,
                    'blocks_new_version': bool(open_disputes),
                    'published_version_remains_visible': bool(head),
                },
                'standings_detail': standings,
                'services_detail': services_detail,
                'attendance_detail': attendance_detail,
                'notices': notices,
            }
