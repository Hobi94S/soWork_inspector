from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sowork_inspector.app import SoWorkInspectorApp
from sowork_inspector.config import AppConfig
from sowork_inspector.event_delivery import EventDelivery
from sowork_inspector.har import snapshots
from sowork_inspector.lockfile import ProcessLock
from sowork_inspector.presence import PresenceError, firestore_documents, membermap_identities, parse_rooms
from sowork_inspector.reporting import rebuild_summary_from_events

T = datetime(2026, 10, 13, 23, 59, tzinfo=timezone.utc)
LOGGER = logging.getLogger('test')


def rooms(ids=('u1',), count=None, group='office'):
    return {'roomDefinitionMap': {'floor1': {'roomDefinition': {'groupId': group},
        'rooms': [{'id': 'room1', 'userIds': list(ids), 'userCount': len(ids) if count is None else count}]}}}


def records(ids=('u1',), at=T):
    return parse_rooms(rooms(ids), 'office', at, {'u1': {'name': 'Example'}})


class PresenceTests(unittest.TestCase):
    def test_schema_and_stable_identity(self):
        record = records()[0]
        self.assertEqual(record.membership_id, 'office:u1')
        self.assertEqual(record.full_name, 'Example')
        self.assertFalse(record.raw)

    def test_invalid_snapshots_are_rejected(self):
        for payload in ({}, {'roomDefinitionMap': {}}, rooms(count=99), rooms(group='other'), rooms(ids=(None,))):
            with self.subTest(payload=payload), self.assertRaises(PresenceError):
                parse_rooms(payload, 'office', T)

    def test_empty_office_is_valid_with_definitions(self):
        self.assertEqual(parse_rooms(rooms(ids=()), 'office', T), [])

    def test_user_in_multiple_rooms_is_deduplicated(self):
        payload = rooms()
        payload['roomDefinitionMap']['floor1']['rooms'].append({'id': 'room2', 'userIds': ['u1'], 'userCount': 1})
        self.assertEqual(len(parse_rooms(payload, 'office', T)), 1)

    def test_framed_firestore_names_only_and_office_filter(self):
        doc = {'name': 'projects/p/databases/(default)/documents/group_membermaps/office_0',
               'fields': {'users': {'mapValue': {'fields': {'u1': {'mapValue': {'fields': {
                   'userName': {'stringValue': 'Example'}, 'isOnline': {'booleanValue': True},
                   'token': {'stringValue': 'not-exported'}}}}}}}}}
        chunk = json.dumps([[0, {'documentChange': {'document': doc}}]])
        parsed = list(firestore_documents('123\n' + chunk + '\n8\n[[1,{}]]'))
        self.assertEqual(membermap_identities(parsed, 'office'), {'u1': {'name': 'Example'}})
        self.assertEqual(membermap_identities(parsed, 'another'), {})


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(output_dir=Path(self.temp.name), time_zone_name='UTC')
        self.app = SoWorkInspectorApp(self.config)

    def test_midnight_empty_snapshot_and_report_rebuild(self):
        self.app.cycle(T, records())
        self.app.cycle(T + timedelta(minutes=2), [])
        summary = self.app.tracker.project_summary()
        self.assertEqual(summary['2026-10-13']['office:u1']['tracked_seconds'], 60)
        self.assertEqual(summary['2026-10-14']['office:u1']['tracked_seconds'], 60)
        _, replay = rebuild_summary_from_events(self.app.storage.events_dir.glob('*.jsonl'),
            time_zone_name='UTC', away_counts_as_online=True, logger=LOGGER)
        self.assertEqual(summary, replay)

    def test_downtime_is_not_charged_and_replay_matches(self):
        self.app.cycle(T, records())
        next_at = T + timedelta(seconds=30)
        self.app.cycle(next_at, records(at=next_at))
        restarted = SoWorkInspectorApp(self.config)
        restarted.suspend()
        later = T + timedelta(hours=1)
        restarted.cycle(later, records(at=later))
        restarted.cycle(later + timedelta(seconds=20), [])
        total = sum(b['office:u1']['tracked_seconds'] for b in restarted.tracker.summary.values())
        self.assertEqual(total, 50)
        _, replay = rebuild_summary_from_events(self.app.storage.events_dir.glob('*.jsonl'),
            time_zone_name='UTC', away_counts_as_online=True, logger=LOGGER)
        self.assertEqual(replay, restarted.tracker.project_summary())

    def test_lock_prevents_concurrent_writes(self):
        path = Path(self.temp.name) / 'lock'
        with ProcessLock(path):
            with self.assertRaises(RuntimeError):
                with ProcessLock(path):
                    pass


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.delivery = EventDelivery(Path(self.temp.name) / 'outbox.sqlite', 'https://example.test/webhook', LOGGER)

    def queued(self):
        with self.delivery.connect() as db:
            return [json.loads(row['payload']) for row in db.execute('SELECT * FROM outbox ORDER BY id')]

    def test_entry_dedup_and_two_snapshot_exit_confirmation(self):
        self.delivery.observe(records(), T)
        self.delivery.observe(records(), T + timedelta(seconds=60))
        self.delivery.observe([], T + timedelta(seconds=120))
        self.assertEqual([r['type'] for r in self.queued()], ['entry'])
        self.delivery.observe([], T + timedelta(seconds=180))
        payloads = self.queued()
        self.assertEqual([r['type'] for r in payloads], ['entry', 'exit'])
        self.assertEqual(payloads[1]['happenedAt'], (T + timedelta(seconds=120)).isoformat())
        self.assertEqual(payloads[0]['source'], 'sowork-inspector')
        self.assertEqual(len({r['eventId'] for r in payloads}), 2)
        self.assertNotIn('spotId', payloads[0])

    def test_failure_retains_fifo_and_event_id_then_retries(self):
        self.delivery.observe(records(), T)
        original = self.queued()[0]
        with patch('sowork_inspector.event_delivery.urlopen', side_effect=OSError('failed')):
            self.assertFalse(self.delivery.deliver_one())
        self.assertEqual(self.queued()[0], original)
        with self.delivery.connect() as db:
            db.execute('UPDATE outbox SET next_attempt=0')
        with patch('sowork_inspector.event_delivery.urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            self.assertTrue(self.delivery.deliver_one())
            sent = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(sent, original)
        self.assertEqual(self.queued(), [])

    def test_failure_breaks_consecutive_absence_confirmation(self):
        self.delivery.observe(records(), T)
        self.delivery.observe([], T + timedelta(seconds=60))
        self.delivery.reset_pending_absences()
        self.delivery.observe([], T + timedelta(seconds=180))
        self.assertEqual(len(self.queued()), 1)
        self.delivery.observe([], T + timedelta(seconds=240))
        self.assertEqual(self.queued()[1]['happenedAt'], (T + timedelta(seconds=180)).isoformat())

    def test_return_before_confirmation_does_not_emit_exit(self):
        self.delivery.observe(records(), T)
        self.delivery.observe([], T + timedelta(seconds=60))
        self.delivery.observe(records(), T + timedelta(seconds=120))
        self.assertEqual(len(self.queued()), 1)


class HarTests(unittest.TestCase):
    def test_synthetic_har_ignores_secrets_and_non_success(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sample.har'
            entry = {'startedDateTime': T.isoformat(), 'time': 100,
                'request': {'url': 'https://api.sowork.com/api/v1/gaia/rooms', 'method': 'POST',
                            'headers': [{'name': 'Authorization', 'value': 'not-exported'}],
                            'postData': {'text': json.dumps({'groupId': 'office'})}},
                'response': {'status': 200, 'content': {'text': json.dumps(rooms())}}}
            path.write_text(json.dumps({'log': {'entries': [entry]}}))
            values = snapshots(path)
            self.assertEqual(len(values), 1)
            self.assertNotIn('not-exported', json.dumps(values[0][1][0].to_dict()))
            with self.assertRaises(PresenceError):
                snapshots(path, 'wrong')


if __name__ == '__main__':
    unittest.main()
