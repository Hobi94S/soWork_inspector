import base64
from datetime import timedelta
import json
from urllib.parse import urlsplit
from .presence import firestore_documents, membermap_identities, parse_rooms, PresenceError
from .utils import parse_timestamp


def content(entry):
    value = entry['response'].get('content', {})
    text = value.get('text', '')
    return base64.b64decode(text).decode() if value.get('encoding') == 'base64' else text


def snapshots(path, group_id=''):
    entries = json.loads(path.read_text())['log']['entries']
    room_entries = [e for e in entries if urlsplit(e['request']['url']).hostname == 'api.sowork.com'
                    and urlsplit(e['request']['url']).path == '/api/v1/gaia/rooms'
                    and e['request']['method'] == 'POST' and e['response']['status'] == 200]
    if not room_entries:
        raise PresenceError('HAR contains no successful rooms snapshots')
    groups = {json.loads(e['request']['postData']['text'])['groupId'] for e in room_entries}
    if not group_id:
        if len(groups) != 1:
            raise PresenceError('HAR contains multiple offices; specify SOWORK_GROUP_ID')
        group_id = next(iter(groups))
    if group_id not in groups:
        raise PresenceError('Configured office not found in HAR')
    identities = {}
    # Only names are retained. No request headers, cookies, tokens or sessions.
    for entry in entries:
        if urlsplit(entry['request']['url']).hostname == 'firestore.googleapis.com':
            identities.update(membermap_identities(firestore_documents(content(entry)), group_id))
    result = []
    for entry in room_entries:
        body = json.loads(entry['request']['postData']['text'])
        if body['groupId'] != group_id:
            continue
        timestamp = parse_timestamp(entry['startedDateTime']) + timedelta(milliseconds=max(0, entry.get('time', 0)))
        result.append((timestamp, parse_rooms(json.loads(content(entry)), group_id, timestamp, identities)))
    return sorted(result, key=lambda item: item[0])
