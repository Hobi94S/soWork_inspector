"""Schemas observed in the SoWork HAR; no authentication data is imported."""
from datetime import datetime
import json
from .models import MemberRecord


class PresenceError(RuntimeError):
    pass


def firestore_documents(text):
    """Read JSON arrays separated by WebChannel length prefixes."""
    decoder = json.JSONDecoder()
    cursor = 0
    def walk(value):
        if isinstance(value, dict):
            if 'documentChange' in value:
                yield value['documentChange']['document']
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
    while cursor < len(text):
        if text[cursor] != '[':
            cursor += 1
            continue
        try:
            value, consumed = decoder.raw_decode(text[cursor:])
        except ValueError:
            cursor += 1
            continue
        cursor += consumed
        yield from walk(value)


def decode_value(value):
    if 'mapValue' in value:
        return {key: decode_value(v) for key, v in value['mapValue'].get('fields', {}).items()}
    if 'arrayValue' in value:
        return [decode_value(v) for v in value['arrayValue'].get('values', [])]
    return next(iter(value.values()), None)


def membermap_identities(documents, group_id):
    identities = {}
    prefix = group_id + '_'
    for doc in documents:
        name = doc.get('name', '').split('/')
        if len(name) < 2 or name[-2] != 'group_membermaps' or not name[-1].startswith(prefix):
            continue
        fields = {k: decode_value(v) for k, v in doc.get('fields', {}).items()}
        for uid, member in fields.get('users', {}).items():
            identities[uid] = {'name': member.get('userName', '')}
    return identities


def parse_rooms(payload, group_id, observed_at: datetime, identities=None):
    """Full-office snapshot, never infer offline from malformed/partial responses."""
    identities = identities or {}
    if not isinstance(payload, dict) or not isinstance(payload.get('roomDefinitionMap'), dict):
        raise PresenceError('Missing roomDefinitionMap')
    definitions = payload['roomDefinitionMap']
    # An empty map cannot distinguish a partial/error response from an empty office.
    if not definitions:
        raise PresenceError('No room definitions; snapshot completeness unknown')
    users = {}
    for definition in definitions.values():
        if not isinstance(definition, dict):
            raise PresenceError('Invalid room definition')
        metadata = definition.get('roomDefinition', {})
        if metadata.get('groupId') != group_id:
            raise PresenceError('Response belongs to another office')
        if not isinstance(definition.get('rooms'), list):
            raise PresenceError('Missing rooms array')
        for room in definition['rooms']:
            if not isinstance(room, dict) or not isinstance(room.get('userIds'), list) or not isinstance(room.get('id'), str):
                raise PresenceError('Invalid room membership')
            ids = room['userIds']
            if room.get('userCount') != len(ids) or any(not isinstance(uid, str) or not uid for uid in ids):
                raise PresenceError('Inconsistent room user count')
            for uid in ids:
                identity = identities.get(uid, {})
                users[uid] = MemberRecord(
                    timestamp=observed_at, membership_id=f'{group_id}:{uid}', user_id=uid,
                    display_name=identity.get('name') or uid,
                    full_name=identity.get('name', ''), state='Present',
                    room_id=room['id'], group_id=group_id,
                    source='sowork_rooms', event_type='snapshot',
                )
    return list(users.values())


def parse_passive_members(members, group_id, observed_at):
    """Read sanitized members from a complete, synchronized Firestore observer."""
    if not isinstance(members, dict):
        raise PresenceError('Invalid passive member list')
    records = []
    for uid, member in members.items():
        if not isinstance(uid, str) or not uid or not isinstance(member, dict) or type(member.get('online')) is not bool:
            raise PresenceError('Invalid passive member presence')
        if not member['online']:
            continue
        name = member.get('name') or uid
        records.append(MemberRecord(
            timestamp=observed_at, membership_id=f'{group_id}:{uid}', user_id=uid,
            display_name=name, full_name=name, state='Present',
            room_id=member.get('roomId') or None, group_id=group_id,
            source='sowork_firestore', event_type='snapshot'))
    return records
