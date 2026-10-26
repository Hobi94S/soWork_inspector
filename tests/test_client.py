import logging
from unittest import TestCase
from unittest.mock import Mock, patch

from main import main
from sowork_inspector.client import SoWorkClient
from sowork_inspector.presence import PresenceError


class ClientDiagnosticsTests(TestCase):
    def client(self, result):
        client = SoWorkClient.__new__(SoWorkClient)
        client.config = Mock(group_id='office', poll_seconds=60)
        client.driver = Mock()
        client.driver.execute_async_script.return_value = result
        return client

    def test_http_status_is_distinct_from_network_error(self):
        with self.assertRaisesRegex(PresenceError, 'HTTP 403'):
            self.client({'error': 'http', 'status': 403}).snapshot()
        with self.assertRaisesRegex(PresenceError, 'network/CORS'):
            self.client({'error': 'transport'}).snapshot()

    def test_stale_response_is_rejected(self):
        with self.assertRaisesRegex(PresenceError, 'not synchronized or is stale'):
            self.client({'error': 'stale'}).snapshot()

    def test_office_readiness_error_is_specific(self):
        with self.assertRaisesRegex(PresenceError, 'application has not requested rooms'):
            self.client({'error': 'office_not_ready'}).snapshot()

    def test_cli_prints_safe_presence_error(self):
        with patch('main.AppConfig.from_env', side_effect=PresenceError('Presence API rejected the request (HTTP 403)')):
            with self.assertLogs('sowork', logging.ERROR) as logs:
                self.assertEqual(main(['collect-once']), 1)
        self.assertIn('HTTP 403', logs.output[0])

    def test_cli_keeps_third_party_session_details_private(self):
        with patch('main.AppConfig.from_env', side_effect=RuntimeError('sensitive-session-value')):
            with self.assertLogs('sowork', logging.ERROR) as logs:
                self.assertEqual(main(['collect-once']), 1)
        self.assertNotIn('sensitive-session-value', logs.output[0])


class OfficeEntryTests(TestCase):
    def client(self):
        client = SoWorkClient.__new__(SoWorkClient)
        client.config = Mock(group_id='office', login_timeout=20)
        client.driver = Mock()
        client.snapshot = Mock()
        return client

    def test_clicks_enter_office_and_waits_for_connection_before_snapshot(self):
        import itertools
        client = self.client()
        button = Mock()
        button.is_displayed.return_value = True
        button.is_enabled.return_value = True
        client.driver.find_elements.side_effect = [[button], []]
        client.driver.execute_script.side_effect = [
            {'groupId': 'office', 'seenRooms': True, 'connected': False},
            {'groupId': 'office', 'seenRooms': True, 'connected': True},
        ]
        with patch('sowork_inspector.client.time.monotonic', side_effect=itertools.count()), patch('sowork_inspector.client.time.sleep') as sleep:
            client.wait_ready()
        button.click.assert_called_once()
        sleep.assert_called_once_with(1)
        client.snapshot.assert_called_once()

    def test_lobby_rooms_are_not_sufficient_for_readiness(self):
        import itertools
        client = self.client()
        client.driver.find_elements.return_value = []
        client.driver.execute_script.return_value = {'groupId': 'office', 'seenRooms': True, 'connected': False}
        with patch('sowork_inspector.client.time.monotonic', side_effect=itertools.count()), patch('sowork_inspector.client.time.sleep'):
            with self.assertRaisesRegex(PresenceError, 'Office entry not confirmed'):
                client.wait_ready()
        client.snapshot.assert_not_called()

    def test_timeout_preserves_passive_sync_failure(self):
        import itertools
        client = self.client()
        client.driver.find_elements.return_value = []
        client.driver.execute_script.return_value = {'groupId': 'office', 'seenRooms': True, 'connected': True}
        client.snapshot.side_effect = PresenceError('Passive Firestore member list is incomplete')
        with patch('sowork_inspector.client.time.monotonic', side_effect=itertools.count()), patch('sowork_inspector.client.time.sleep'):
            with self.assertRaisesRegex(PresenceError, 'Office entry not confirmed: Passive Firestore member list is incomplete'):
                client.wait_ready()

    def test_disabled_entry_button_is_not_clicked(self):
        client = self.client()
        button = Mock()
        button.is_displayed.return_value = True
        button.is_enabled.return_value = False
        client.driver.find_elements.return_value = [button]
        self.assertFalse(client._enter_office())
        button.click.assert_not_called()


class PassiveMembersTests(TestCase):
    def test_only_explicit_online_members_are_present(self):
        from datetime import datetime, timezone
        from sowork_inspector.presence import parse_passive_members
        records = parse_passive_members({
            'u1': {'online': True, 'name': 'Example', 'roomId': 'room'},
            'u2': {'online': False, 'name': 'Offline'},
        }, 'office', datetime.now(timezone.utc))
        self.assertEqual([record.user_id for record in records], ['u1'])
        self.assertEqual(records[0].source, 'sowork_firestore')

    def test_missing_online_flag_is_not_treated_as_offline(self):
        from datetime import datetime, timezone
        from sowork_inspector.presence import parse_passive_members
        with self.assertRaises(PresenceError):
            parse_passive_members({'u1': {'name': 'Example'}}, 'office', datetime.now(timezone.utc))
