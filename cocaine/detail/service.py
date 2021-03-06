#
#    Copyright (c) 2012+ Anton Tyurin <noxiouz@yandex.ru>
#    Copyright (c) 2013+ Evgeny Safronov <division494@gmail.com>
#    Copyright (c) 2011-2014 Other contributors as noted in the AUTHORS file.
#
#    This file is part of Cocaine.
#
#    Cocaine is free software; you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published by
#    the Free Software Foundation; either version 3 of the License, or
#    (at your option) any later version.
#
#    Cocaine is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import itertools
import logging
import sys
import threading


import msgpack
from tornado.gen import Return
from tornado.concurrent import chain_future
from tornado.tcpclient import TCPClient


from .api import API
from .asyncqueue import AsyncQueue
from ..common import CocaineErrno
from ..decorators import coroutine
from .io import CocaineFuture
from .io import CocaineIO

# cocaine defined exceptions
from ..exceptions import ServiceError
from ..exceptions import ChokeEvent
from ..exceptions import InvalidMessageType
from ..exceptions import InvalidApiVerison


log = logging.getLogger("cocaine")
# sh = logging.StreamHandler()
# log.setLevel(logging.INFO)
# log.addHandler(sh)


class CocaineTCPClient(TCPClient):
    def __init__(self, *args, **kwargs):
        super(CocaineTCPClient, self).__init__(*args, **kwargs)

    def connect(self, host, port):
        result_future = CocaineFuture()

        def migrate_context():
            connection_future = super(CocaineTCPClient, self).connect(host, port)
            chain_future(connection_future, result_future)

        # post this to handle a connection of IOStream
        # in Cocaine IO thread
        self.io_loop.post(migrate_context)
        return result_future


class EmptyResponse(object):
    pass


def StreamedProtocol(name, payload):
    if name == "write":
        return payload
    elif name == "error":
        return ServiceError(*payload)
    elif name == "close":
        return EmptyResponse()


class Rx(object):
    def __init__(self, rx_tree):
        self._queue = AsyncQueue()
        self._done = False
        self.rx_tree = rx_tree

    @coroutine
    def get(self, timeout=0, protocol=StreamedProtocol):
        if self._done and self._queue.empty():
            raise ChokeEvent()

        name, payload = yield self._queue.get()
        res = protocol(name, payload)
        if isinstance(res, Exception):
            raise res
        else:
            raise Return(res)

    def done(self):
        self._done = True

    def push(self, msg_type, payload):
        dispatch = self.rx_tree.get(msg_type)
        log.debug("dispatch %s", dispatch)
        if dispatch is None:
            raise InvalidMessageType(CocaineErrno.INVALIDMESSAGETYPE,
                                     "unexpected message type %s" % msg_type)
        name, rx, _ = dispatch
        log.debug("name `%s` rx `%s` %s", name, rx, _)
        self._queue.put_nowait((name, payload))
        if rx == {}:  # last transition
            self.done()
        elif rx is not None:  # recursive transition
            self.rx_tree = rx


class Tx(object):
    def __init__(self, tx_tree, pipe, session_id):
        self.tx_tree = tx_tree
        self.session_id = session_id
        self.pipe = pipe

    @coroutine
    def _invoke(self, method_name, *args, **kwargs):
        log.debug("_invoke has been called %s %s", str(args), str(kwargs))
        for method_id, (method, tx_tree, rx_tree) in self.tx_tree.items():  # py3 has no iteritems
            if method == method_name:
                log.debug("method `%s` has been found in API map", method_name)
                self.pipe.write(msgpack.packb([self.session_id, method_id, args]))
                raise Return(None)
        raise AttributeError("method_name")

    def __getattr__(self, name):
        def on_getattr(*args, **kwargs):
            return self._invoke(name, *args, **kwargs)
        return on_getattr


class Channel(object):
    def __init__(self, rx, tx):
        self.rx = rx
        self.tx = tx


class BaseService(object):
    # py3: msgpack by default unpacks strings as bytes.
    # Make it to unpack as strings for compatibility.
    _msgpack_string_encoding = None if sys.version_info[0] == 2 else 'utf8'

    def __init__(self, name, host='localhost', port=10053, loop=None):
        self.loop = loop or CocaineIO.instance()
        self.host = host
        self.port = port
        self.name = name

        self._extra = {'service': self.name,
                       'id': id(self)}
        self.log = logging.LoggerAdapter(log, self._extra)

        self.sessions = {}
        self.counter = itertools.count(1)
        self.api = {}

        self._lock = threading.Lock()

        # wrap into separate class
        self.pipe = None
        self.buffer = msgpack.Unpacker(encoding=self._msgpack_string_encoding)

    @coroutine
    def connect(self):
        if not self._connected:
            with self._lock:
                if not self._connected:
                    self.pipe = yield CocaineTCPClient(io_loop=self.loop).connect(self.host,
                                                                                  self.port)
                    self.pipe.read_until_close(callback=self.on_close,
                                               streaming_callback=self.on_read)
                    self.log.debug("connection has been established successfully")

    def disconnect(self):
        with self._lock:
            if self.pipe is not None:
                self.pipe.close()
                # ToDo: push error into current sessions

    def on_close(self, *args):
        self.log.debug("pipe has been closed %s", args)
        with self._lock:
            self.pipe = None
            # ToDo: push error into current sessions

    def on_read(self, read_bytes):
        self.log.debug("read %s", read_bytes)
        self.buffer.feed(read_bytes)
        for msg in self.buffer:
            self.log.debug("unpacked: %s", msg)
            try:
                session, message_type, payload = msg
                self.log.debug("%s, %d, %s", session, message_type, payload)
            except Exception as err:
                self.log.error("malformed message: `%s` %s", err, str(msg))
                continue

            rx = self.sessions.get(session)
            if rx is None:
                self.log.warning("unknown session number: `%d`", session)
                continue

            rx.push(message_type, payload)

    @coroutine
    def _invoke(self, method_name, *args, **kwargs):
        self.log.debug("_invoke has been called %s %s", str(args), str(kwargs))
        yield self.connect()
        self.log.debug("%s", self.api)
        for method_id, (method, tx_tree, rx_tree) in self.api.items():  # py3 has no iteritems
            if method == method_name:
                self.log.debug("method `%s` has been found in API map", method_name)
                counter = next(self.counter)  # py3 counter has no .next() method
                self.log.debug('sending message: %s', [counter, method_id, args])
                self.pipe.write(msgpack.packb([counter, method_id, args]))
                self.log.debug("RX TREE %s", rx_tree)
                self.log.debug("TX TREE %s", tx_tree)

                rx = Rx(rx_tree)
                tx = Tx(tx_tree, self.pipe, counter)
                self.sessions[counter] = rx
                channel = Channel(rx=rx, tx=tx)
                raise Return(channel)
        raise AttributeError(method_name)

    @property
    def _connected(self):
        return self.pipe is not None

    def __getattr__(self, name):
        def on_getattr(*args, **kwargs):
            return self._invoke(name, *args, **kwargs)
        return on_getattr


class Locator(BaseService):
    def __init__(self, host="localhost", port=10053, loop=None):
        super(Locator, self).__init__(name="locator",
                                      host=host, port=port, loop=loop)
        self.api = API.Locator


class Service(BaseService):
    def __init__(self, name, host="localhost", port=10053, version=0, loop=None):
        super(Service, self).__init__(name=name, loop=loop)
        self.locator = Locator(host=host, port=port, loop=loop)
        self.api = {}
        self.host = None
        self.port = None
        self.version = version

    @coroutine
    def connect(self):
        self.log.debug("checking if service connected", extra=self._extra)
        if self._connected:
            log.debug("already connected", extra=self._extra)
            return

        self.log.debug("resolving ...", extra=self._extra)
        channel = yield self.locator.resolve(self.name)
        (self.host, self.port), version, self.api = yield channel.rx.get()
        log.debug("successfully resolved", extra=self._extra)

        # Version compatibility should be checked here.
        if not (self.version == 0 or version == self.version):
            raise InvalidApiVerison(self.name, version, self.version)
        yield super(Service, self).connect()
