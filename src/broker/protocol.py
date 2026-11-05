"""Messages exchanged between the execution broker and executors."""

import uuid


def request_id():
    return uuid.uuid4().hex


def message(message_type, **payload):
    return {"type": message_type, **payload}
