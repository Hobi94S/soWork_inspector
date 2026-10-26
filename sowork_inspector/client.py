from pathlib import Path
import time
import json
import logging
from datetime import datetime, timezone
from .presence import PresenceError, parse_passive_members

FETCH_ROOMS = '''
const groupId = arguments[0], maxAge = arguments[1], done = arguments[arguments.length - 1];
const observer = window.__soworkObserver;
if (!observer?.seenRooms || !observer.officeConnected || observer.groupId !== groupId) {
  done({error: 'office_not_ready'}); return;
}
const expected = Object.entries(observer.expectedPartitions || {});
if (!expected.length) { done({error: 'incomplete', detail: 'No member subscriptions observed'}); return; }
const partitions = [];
for (const [name, targetId] of expected) {
  const partition = observer.partitions[name];
  if (!partition || !partition.targets.includes(targetId)) {
    done({error: 'incomplete', detail: 'Waiting for subscribed member documents'}); return;
  }
  partitions.push(partition);
}
const members = {}, times = [];
for (const partition of partitions) {
  if (!partition.valid || !partition.targets.length) { done({error: 'incomplete'}); return; }
  for (const id of partition.targets) {
    const target = observer.targets[id];
    if (!target?.current || target.pending || !target.at || Date.now() - target.at > maxAge) {
      done({error: 'stale'}); return;
    }
    times.push(target.at);
  }
  for (const [id, member] of Object.entries(partition.members)) {
    if (Object.hasOwn(members, id)) { done({error: 'incomplete'}); return; }
    members[id] = member;
  }
}
done({members, receivedAt: Math.max(...times)});
'''


class SoWorkClient:
    def __init__(self, config, *, login=False):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        options = webdriver.ChromeOptions()
        config.profile_dir.mkdir(parents=True, exist_ok=True)
        options.add_argument(f'--user-data-dir={config.profile_dir}')
        if config.headless and not login:
            options.add_argument('--headless=new')
        options.add_argument('--disable-dev-shm-usage')
        if config.chrome_binary:
            options.binary_location = config.chrome_binary
        options.add_experimental_option('prefs', {
            'profile.default_content_setting_values.media_stream_mic': 2,
            'profile.default_content_setting_values.media_stream_camera': 2,
        })
        self.config = config
        self.driver = webdriver.Chrome(options=options, service=Service(config.chromedriver) if config.chromedriver else Service())
        try:
            self.driver.set_script_timeout(25)
            source = Path(__file__).with_name('observer.js').read_text()
            if config.group_id:
                source += '\nwindow.__soworkObserver.groupId = ' + json.dumps(config.group_id) + ';'
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': source})
            self.driver.get(config.app_url)
        except BaseException:
            self.close()
            raise

    def _enter_office(self):
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import (
            StaleElementReferenceException, ElementClickInterceptedException,
            ElementNotInteractableException,
        )
        buttons = self.driver.find_elements(By.XPATH,
            "//button[contains(normalize-space(.), 'Enter Office')] | "
            "//*[@role='button' and contains(normalize-space(.), 'Enter Office')]")
        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    logging.getLogger('sowork').info('Enter Office clicked; waiting for the office connection')
                    return True
            except (StaleElementReferenceException, ElementClickInterceptedException,
                    ElementNotInteractableException):
                continue
        return False

    def wait_ready(self):
        deadline = time.monotonic() + self.config.login_timeout
        next_click = 0
        last_reason = None
        while time.monotonic() < deadline:
            if time.monotonic() >= next_click and self._enter_office():
                next_click = time.monotonic() + 5
            state = self.driver.execute_script(
                'const s = window.__soworkObserver; return s ? '
                '{groupId:s.groupId, seenRooms:s.seenRooms, connected:s.officeConnected} : null')
            last_reason = ('Observer not installed' if not state else
                           'Application has not requested rooms' if not state.get('seenRooms') else
                           'Gaia office connection inactive' if not state.get('connected') else last_reason)
            if state and state.get('seenRooms'):
                if self.config.group_id and state.get('groupId') != self.config.group_id:
                    raise PresenceError('Open the configured office in the browser')
                if state.get('connected'):
                    self.config.group_id = state['groupId']
                    try:
                        self.snapshot()
                    except PresenceError as exc:
                        reason = str(exc)
                        if reason != last_reason:
                            logging.getLogger('sowork').info('Waiting for passive presence: %s', reason)
                        last_reason = reason
                        time.sleep(1)
                        continue
                    logging.getLogger('sowork').info('Office connection confirmed; presence collection ready')
                    return
            time.sleep(1)
        raise PresenceError(f'Office entry not confirmed: {last_reason or "readiness timeout"}; no snapshot recorded')

    def snapshot(self):
        result = self.driver.execute_async_script(FETCH_ROOMS, self.config.group_id, max(30, self.config.poll_seconds * 2) * 1000)
        if not isinstance(result, dict) or result.get('error'):
            status = result.get('status') if isinstance(result, dict) else None
            reason = result.get('error') if isinstance(result, dict) else 'invalid_result'
            if reason == 'incomplete':
                raise PresenceError('Passive Firestore member list is incomplete; ' + {
                    'No member subscriptions observed': 'no member subscriptions observed',
                    'Waiting for subscribed member documents': 'waiting for subscribed member documents',
                }.get(result.get('detail'), 'invalid or duplicate member documents'))
            if reason == 'stale':
                raise PresenceError('Passive Firestore presence is not synchronized or is stale; no snapshot recorded')
            if reason == 'http':
                raise PresenceError(f'Presence API rejected the request (HTTP {status}); no snapshot recorded')
            if reason == 'office_not_ready':
                raise PresenceError('Office not ready: the application has not requested rooms or the office connection is inactive')
            raise PresenceError('Presence request failed: browser network/CORS error or timeout; no snapshot recorded')
        observed_at = datetime.fromtimestamp(result['receivedAt'] / 1000, timezone.utc)
        return observed_at, parse_passive_members(result['members'], self.config.group_id, observed_at)

    def close(self):
        self.driver.quit()
