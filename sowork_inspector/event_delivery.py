"""Transactional presence state/outbox; delivery is at least once, in FIFO order."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4
from urllib.request import Request, urlopen

from .models import MemberRecord
from .tracker import OFFLINE_GROUP_ID

EVENT_TIME_ZONE = timezone.utc


class EventDelivery:
    def __init__(self, path: Path, endpoint: str, logger) -> None:
        self.path = path
        self.endpoint = endpoint
        self.logger = logger
        self.wake = threading.Event()
        self.stop = threading.Event()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS presence (
                    user_id TEXT PRIMARY KEY, name TEXT, email TEXT,
                    missing_count INTEGER NOT NULL DEFAULT 0, first_missing TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, records: list[MemberRecord], observed_at: datetime) -> None:
        happened_at = observed_at.astimezone(EVENT_TIME_ZONE).isoformat(timespec='seconds')
        online = {
            r.user_id: r for r in records
            if r.user_id and r.state in {'Present', 'Active', 'Away'}
            and r.group_id is not None and r.group_id != OFFLINE_GROUP_ID
        }
        with self.connect() as db:
            previous = {r['user_id']: r for r in db.execute('SELECT * FROM presence')}
            for user_id, record in online.items():
                name = (record.full_name or record.display_name or '').strip() or None
                email = (record.email or '').strip() or None
                if user_id not in previous:
                    self.enqueue(db, 'entry', user_id, name, email, happened_at)
                db.execute('''INSERT INTO presence (user_id, name, email) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, email=excluded.email,
                    missing_count=0, first_missing=NULL''', (user_id, name, email))
            for user_id, old in previous.items():
                if user_id in online:
                    continue
                if old['missing_count'] == 0:
                    db.execute('UPDATE presence SET missing_count=1, first_missing=? WHERE user_id=?',
                               (happened_at, user_id))
                else:
                    self.enqueue(db, 'exit', user_id, old['name'], old['email'], old['first_missing'])
                    db.execute('DELETE FROM presence WHERE user_id=?', (user_id,))
        self.wake.set()

    def reset_pending_absences(self):
        # A gap breaks the consecutive-observations guarantee.
        with self.connect() as db:
            db.execute('UPDATE presence SET missing_count=0, first_missing=NULL')

    @staticmethod
    def enqueue(db, kind, user_id, name, email, happened_at):
        payload = dict(collectorId='sowork-inspector', type=kind, user=name, userId=user_id,
                       email=email, happenedAt=happened_at, eventId=uuid4().hex, source='sowork-inspector')
        db.execute('INSERT INTO outbox (payload) VALUES (?)', (json.dumps(payload),))

    def deliver_one(self) -> bool:
        with self.connect() as db:
            row = db.execute('SELECT * FROM outbox ORDER BY id LIMIT 1').fetchone()
        if row is None or row['next_attempt'] > time.time():
            return False
        try:
            request = Request(self.endpoint, data=row['payload'].encode('utf-8'),
                              headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f'HTTP {response.status}')
        except Exception as exc:
            attempts = row['attempts'] + 1
            delay = min(300, 5 * 2 ** min(attempts - 1, 6))
            with self.connect() as db:
                db.execute('UPDATE outbox SET attempts=?, next_attempt=? WHERE id=?',
                           (attempts, time.time() + delay, row['id']))
            self.logger.warning('Event %s delivery failed (%s); retry in %ss. FIFO queue retained.',
                                row['id'], f'{type(exc).__name__}: {getattr(exc, "code", "transport/status error")}', delay)
            return False
        with self.connect() as db:
            db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
        self.logger.info('Event %s delivered successfully.', row['id'])
        return True

    def _run(self):
        while not self.stop.is_set():
            try:
                if self.deliver_one():
                    continue
            except Exception:
                self.logger.exception('Event delivery worker failed; queue retained.')
            self.wake.wait(1)
            self.wake.clear()

    @contextmanager
    def running(self):
        self.stop.clear()
        worker = threading.Thread(target=self._run, name='event-delivery', daemon=True)
        worker.start()
        try:
            yield
        finally:
            self.stop.set()
            self.wake.set()
            worker.join()
