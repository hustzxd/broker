"""Byte-limited UTF-8 output retained by the broker."""

from collections import deque


DEFAULT_MAX_LOG_BYTES = 1024 * 1024


class LogBuffer:
    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self.chunks = deque()
        self.size = 0
        self.total = 0

    def append(self, stream, text):
        data = text.encode("utf-8")
        if not data:
            return
        self.chunks.append((stream, data))
        self.size += len(data)
        self.total += len(data)
        while self.size > self.max_bytes:
            stream, data = self.chunks.popleft()
            excess = self.size - self.max_bytes
            self.size -= len(data)
            if excess < len(data):
                # Drop a partial UTF-8 character together with the old prefix.
                data = data[excess:].decode("utf-8", errors="ignore").encode("utf-8")
                if data:
                    self.chunks.appendleft((stream, data))
                    self.size += len(data)

    def read(self, cursor=None, tail=None):
        if cursor is not None and (
            isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= self.total
        ):
            raise ValueError("cursor must be an integer between 0 and next_cursor")
        start = self.total - self.size
        requested = 0 if cursor is None else cursor
        position = start
        chunks = []
        for stream, data in self.chunks:
            end = position + len(data)
            if end > requested:
                offset = max(0, requested - position)
                if offset and data[offset] & 0xC0 == 0x80:
                    raise ValueError("cursor must be at a UTF-8 character boundary")
                chunks.append((stream, data[offset:]))
            position = end
        truncated = requested < start
        # An explicit cursor takes precedence over tail, so incremental reads
        # never silently skip new output due to the default service tail.
        if cursor is None and tail is not None:
            truncated = truncated or len(chunks) > tail
            chunks = chunks[-tail:] if tail else []
        return {
            "entries": [{"stream": stream, "data": data.decode("utf-8")} for stream, data in chunks],
            "truncated": truncated,
            "next_cursor": self.total,
        }
