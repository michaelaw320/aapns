from __future__ import annotations

import asyncio
import json
from asyncio import CancelledError, TimeoutError, create_task, open_connection, wait_for
from contextlib import suppress
from dataclasses import dataclass, field
from logging import getLogger
from math import inf
from ssl import OP_NO_TLSv1, OP_NO_TLSv1_1, SSLContext, SSLError, create_default_context
from time import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h2.settings

from .errors import Blocked, Closed, FormatError, ResponseTooLarge, Timeout

# Apple limits APN payload (data) to 4KB or 5KB, depending.
# Request header is not subject to flow control in HTTP/2
# Data is subject to framing and padding, but those are minor.
MAX_NOTIFICATION_PAYLOAD_SIZE = 5120
REQUIRED_FREE_SPACE = 6000
# OK response is empty
# Error response is short json, ~30 bytes in size
MAX_RESPONSE_SIZE = 2 ** 16
# Inbound connection flow control window
# It's quite arbitrary, guided by:
# * concurrent requests limit, server limit being 1000 today
# * expected response size, see above
CONNECTION_WINDOW_SIZE = 2 ** 24
# Connection establishment safety time limits
CONNECTION_TIMEOUT = 5
TLS_TIMEOUT = 5
logger = getLogger(__package__)


@dataclass(eq=False)
class Connection:
    """Encapsulates a single HTTP/2 connection to the APN server.

    Use `Connection.create(...)` to make connections.

    Connection states:
    * new (not connected)
    * starting
    * active
    * graceful shutdown (to do: https://github.com/python-hyper/hyper-h2/issues/1181)
    * closing
    * closed
    """

    host: str
    port: int
    protocol: h2.connection.H2Connection
    read_stream: asyncio.StreamReader
    write_stream: asyncio.StreamWriter
    should_write: asyncio.Event = field(init=False)
    channels: Dict[int, Channel] = field(default_factory=dict)
    reader: asyncio.Task = field(init=False)
    writer: asyncio.Task = field(init=False)
    closing: bool = False
    closed: bool = False
    outcome: Optional[str] = None
    max_concurrent_streams: int = 100  # initial per RFC7540#section-6.5.2
    last_stream_id_got: int = -1
    last_stream_id_sent: int = -1  # client streams are odd

    @classmethod
    async def create(cls, origin: str, ssl: Optional[SSLContext] = None):
        """Connect to `origin` and return a Connection"""
        url = urlparse(origin)
        assert url.scheme == "https"
        assert url.hostname
        assert not url.username
        assert not url.password
        assert not url.path
        assert not url.params
        assert not url.query
        assert not url.fragment
        host = url.hostname
        port = url.port or 443

        ssl_context = ssl if ssl else create_ssl_context()
        assert OP_NO_TLSv1 in ssl_context.options
        assert OP_NO_TLSv1_1 in ssl_context.options
        # https://bugs.python.org/issue40111 validate context h2 alpn

        protocol = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )
        protocol.local_settings = h2.settings.Settings(
            client=True,
            initial_values={
                # Apple server settings:
                # HEADER_TABLE_SIZE 4096
                # MAX_CONCURRENT_STREAMS 1000
                # INITIAL_WINDOW_SIZE 65535
                # MAX_FRAME_SIZE 16384
                # MAX_HEADER_LIST_SIZE 8000
                h2.settings.SettingCodes.ENABLE_PUSH: 0,
                h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: 2 ** 20,
                h2.settings.SettingCodes.MAX_HEADER_LIST_SIZE: 2 ** 16 - 1,
                h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: MAX_RESPONSE_SIZE,
            },
        )

        protocol.initiate_connection()
        protocol.increment_flow_control_window(CONNECTION_WINDOW_SIZE)

        read_stream, write_stream = await wait_for(
            open_connection(
                host, port, ssl=ssl_context, ssl_handshake_timeout=TLS_TIMEOUT
            ),
            CONNECTION_TIMEOUT,
        )
        try:
            info = write_stream.get_extra_info("ssl_object")
            if not info:
                raise Closed("Failed TLS handshake")
            proto = info.selected_alpn_protocol()
            if proto != "h2":
                raise Closed("Failed to negotiate HTTP/2")
        except Closed:
            write_stream.close()
            with suppress(SSLError, ConnectionError):
                await write_stream.wait_closed()
            raise

        # FIXME we could wait for settings frame from the server,
        # to tell us how much we can actually send, as initial window is small
        return cls(host, port, protocol, read_stream, write_stream)

    def __post_init__(self):
        self.should_write = asyncio.Event()
        self.should_write.set()
        self.reader = create_task(self.background_read(), name="bg-read")
        self.writer = create_task(self.background_write(), name="bg-write")

    async def post(self, req: "Request") -> "Response":
        assert len(req.body) <= MAX_NOTIFICATION_PAYLOAD_SIZE

        now = time()
        if now > req.deadline:
            raise Timeout("Request timed out")
        if self.closing or self.closed:
            raise Closed(self.outcome)
        if self.blocked:
            raise Blocked()

        try:
            stream_id = self.protocol.get_next_available_stream_id()
        except h2.exceptions.NoAvailableStreamIDError:
            self.closing = True
            if not self.outcome:
                self.outcome = "Exhausted"
            raise Closed(self.outcome)

        assert stream_id not in self.channels

        self.last_stream_id_got = stream_id

        ch = self.channels[stream_id] = Channel()
        self.protocol.send_headers(
            stream_id, req.header_with(self.host, self.port), end_stream=False
        )
        self.protocol.increment_flow_control_window(
            MAX_RESPONSE_SIZE, stream_id=stream_id
        )
        self.protocol.send_data(stream_id, req.body, end_stream=True)
        self.should_write.set()

        try:
            while not self.closed:
                ch.wakeup.clear()
                with suppress(TimeoutError):
                    await wait_for(ch.wakeup.wait(), req.deadline - now)
                now = time()
                if now > req.deadline:
                    raise Timeout()
                for event in ch.events:
                    if isinstance(event, h2.events.ResponseReceived):
                        ch.header = dict(event.headers)
                    elif isinstance(event, h2.events.DataReceived):
                        ch.body += event.data
                    elif isinstance(event, h2.events.StreamEnded):
                        return Response.new(ch.header, ch.body)
                    elif len(ch.body) >= MAX_RESPONSE_SIZE:
                        raise ResponseTooLarge(f"Larger than {MAX_RESPONSE_SIZE}")
                del ch.events[:]
            raise Closed(self.outcome)
        finally:
            # FIXME reset the stream, if:
            # * connection is still alive, and
            # * the stream didn't end yet
            del self.channels[stream_id]

    async def close(self):
        self.closing = True
        if not self.outcome:
            self.outcome = "Closed"
        try:
            # FIXME distinguish between cancellation and context exception
            if self.writer:
                self.writer.cancel()
                with suppress(CancelledError):
                    await self.writer

            if self.reader:
                self.reader.cancel()
                with suppress(CancelledError):
                    await self.reader

            self.closed = True

            # at this point, we must release or cancel all pending requests
            for stream_id, ch in self.channels.items():
                ch.wakeup.set()

            self.write_stream.close()
            with suppress(SSLError, ConnectionError):
                await self.write_stream.wait_closed()
        finally:
            self.closed = True

    @property
    def state(self):
        return (
            "closed"
            if self.closed
            else "closing"
            if self.closing
            else "active"
            if self.writer
            else "starting"
            if self.should_write
            else "new"
        )

    @property
    def buffered(self):
        """ This metric shows how "slow" we are sending requests out. """
        return (self.last_stream_id_got - self.last_stream_id_sent) // 2

    @property
    def pending(self):
        """ This metric shows how "slow" the server is to respond. """
        return len(self.channels)

    @property
    def inflight(self):
        return self.pending - self.buffered

    @property
    def blocked(self):
        return (
            self.closing
            or self.closed
            or self.protocol.outbound_flow_control_window <= REQUIRED_FREE_SPACE
            # FIXME accidentally quadratic: .openxx iterates over all streams
            # could be kinda fixed by caching with clever invalidation...
            or self.protocol.open_outbound_streams >= self.max_concurrent_streams
        )

    async def background_read(self):
        try:
            while not self.closed:
                data = await self.read_stream.read(2 ** 16)
                if not data:
                    raise ConnectionError("Server closed the connection")

                for event in self.protocol.receive_data(data):
                    logger.debug("APN: %s", event)
                    stream_id = getattr(event, "stream_id", 0)
                    error = getattr(event, "error_code", None)
                    ch = self.channels.get(stream_id)

                    if isinstance(event, h2.events.RemoteSettingsChanged):
                        m = event.changed_settings.get(
                            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
                        )
                        if m:
                            self.max_concurrent_streams = m.new_value
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        self.closing = True
                        if not self.outcome:
                            if event.additional_data:
                                try:
                                    self.outcome = json.loads(
                                        event.additional_data.decode("utf-8")
                                    )["reason"]
                                except Exception:
                                    self.outcome = str(event.additional_data[:100])
                            else:
                                self.outcome = str(event.error_code)
                        logger.info("Closing with %s", self.outcome)
                    elif not stream_id and error is not None:
                        logger.warning("Caught off guard: %s", event)
                        raise ConnectionError(str(error))
                    else:
                        if isinstance(event, h2.events.DataReceived):
                            # Stream flow control is responsibility of the channel.
                            # Connection flow control is handled here.
                            self.protocol.increment_flow_control_window(
                                event.flow_controlled_length
                            )
                        if ch:
                            ch.events.append(event)
                            ch.wakeup.set()

                # Somewhat inefficient: wake up background writer just in case
                # it could be that we've received something that h2 needs to acknowledge
                self.should_write.set()

                # FIXME notify pool users about possible change to `.blocked`
                # * h2.events.WindowUpdated
                # * max_concurrent_streams change
                # * [maybe] starting a stream
                # * a stream getting closed (but not half-closed)
                # * closing / closed change
        except ConnectionError as e:
            if not self.outcome:
                self.outcome = str(e)
        except Exception:
            logger.exception("background read task died")
        finally:
            self.closing = self.closed = True
            for stream_id, ch in self.channels.items():
                ch.wakeup.set()

    async def background_write(self):
        try:
            while not self.closed:
                data = None

                while not data:
                    if self.closed:
                        return

                    if data := self.protocol.data_to_send():
                        self.write_stream.write(data)
                        last_stream_id = self.last_stream_id_got
                        await self.write_stream.drain()
                        self.last_stream_id_sent = last_stream_id
                    else:
                        await self.should_write.wait()
                        self.should_write.clear()

        except (SSLError, ConnectionError) as e:
            if not self.outcome:
                self.outcome = str(e)
        except Exception:
            logger.exception("background write task died")
        finally:
            self.closing = self.closed = True


@dataclass
class Channel:
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    events: List[h2.events.Event] = field(default_factory=list)
    header: Optional[dict] = None
    body: bytes = b""


@dataclass
class Request:
    header: tuple
    body: bytes
    deadline: float

    def header_with(self, host: str, port: int) -> tuple:
        """Request header including :authority pseudo header field for target server"""
        return ((":authority", f"{host}:{port}"),) + self.header

    @classmethod
    def new(
        cls,
        path: str,
        header: Optional[dict],
        data: dict,
        timeout: Optional[float] = None,
        deadline: Optional[float] = None,
    ):
        if timeout is not None and deadline is not None:
            raise ValueError("Specify timeout or deadline, but not both")
        elif timeout is not None:
            deadline = time() + timeout
        elif deadline is None:
            deadline = inf

        assert path.startswith("/")
        pseudo = dict(method="POST", scheme="https", path=path)
        h = tuple((f":{k}", v) for k, v in pseudo.items()) + tuple(
            (header or {}).items()
        )
        return cls(h, json.dumps(data, ensure_ascii=False).encode("utf-8"), deadline)


@dataclass
class Response:
    code: int
    header: Dict[str, str]
    data: Optional[dict]

    @classmethod
    def new(cls, header: Optional[dict], body: bytes):
        h = {**(header or {})}
        code = int(h.pop(":status", "0"))
        try:
            return cls(code, h, json.loads(body) if body else None)
        except json.JSONDecodeError:
            raise FormatError(f"Not JSON: {body[:20]!r}")

    @property
    def apns_id(self):
        return self.header.get("apns-id", None)


def create_ssl_context():
    context = create_default_context()
    context.options |= OP_NO_TLSv1
    context.options |= OP_NO_TLSv1_1
    context.set_alpn_protocols(["h2"])
    return context
