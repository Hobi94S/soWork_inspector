from contextlib import nullcontext
from dataclasses import replace
import logging
import time
from .event_delivery import EventDelivery
from .presence import PresenceError
from .storage import Storage
from .tracker import HourTracker


class SoWorkInspectorApp:
    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger('sowork')
        self.storage = Storage(config)
        self.tracker = HourTracker.from_state(self.storage.load_state(),
            time_zone_name=config.time_zone_name, away_counts_as_online=True, logger=self.logger)

    def persist(self):
        self.storage.save_state(self.tracker.export_state())
        summary = self.tracker.project_summary()
        self.storage.write_summary(summary)
        self.storage.write_reports(summary)

    def suspend(self):
        # Charge only through the last successful observation, excluding downtime.
        records = [replace(self.tracker.last_presence[mid],
                   timestamp=session.last_event_at, state='Offline', room_id=None,
                   source='collector_gap', event_type='presence')
                   for mid, session in self.tracker.active_sessions.items()]
        if records:
            events = self.tracker.process_presence_updates(records)
            self.storage.append_member_events(records)
            self.storage.append_derived_events(events)
            self.persist()

    def cycle(self, observed_at, records, delivery=None):
        events = self.tracker.process_snapshot(records, observed_at=observed_at)
        # Explicit offline observations make empty snapshots replayable.
        persisted = [replace(record, event_type='presence') for record in records]
        persisted.extend(replace(record, event_type='presence') for record in self.tracker.last_presence.values()
                         if record.timestamp == observed_at and record.state == 'Offline')
        self.storage.append_member_events(persisted)
        self.storage.append_derived_events(events)
        self.storage.write_snapshot(records, observed_at=observed_at)
        self.persist()
        if delivery:
            delivery.observe(records, observed_at)
        self.logger.info('Snapshot validado: %s usuários online', len(records))

    def run(self, *, once=False):
        from .client import SoWorkClient
        delivery = (EventDelivery(self.config.output_dir / 'notifications.sqlite', self.config.webhook_url, self.logger)
                    if self.config.webhook_url else None)
        client = SoWorkClient(self.config)
        try:
            client.wait_ready()
            if delivery:
                delivery.reset_pending_absences()
            if any(record.group_id != self.config.group_id for record in self.tracker.last_presence.values()):
                raise ValueError('Runtime belongs to another office; use a separate SOWORK_OUTPUT_DIR')
            self.suspend()
            with delivery.running() if delivery else nullcontext():
                while True:
                    try:
                        self.cycle(*client.snapshot(), delivery=delivery)
                    except Exception as exc:
                        if delivery:
                            delivery.reset_pending_absences()
                        self.suspend()
                        # Selenium errors can contain page/session data: log only type.
                        if isinstance(exc, PresenceError):
                            self.logger.warning('Coleta falhou: %s; presença não inferida', exc)
                        else:
                            self.logger.warning('Coleta falhou (%s); presença não inferida', type(exc).__name__)
                        if once:
                            return 1
                        from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException
                        if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
                            return 1
                    else:
                        if once:
                            return 0
                    time.sleep(self.config.poll_seconds)
        finally:
            self.suspend()
            client.close()
