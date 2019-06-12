
# server.py - hxsocks server

# Copyright (C) 2016 - 2018, v3aqb

# This file is a part of hxsocks.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import socket
import struct
import logging
import re
import io
import time
import traceback
import urllib.parse
import random
import hashlib
import hmac
from collections import defaultdict, deque

import asyncio
import asyncio.streams

from hxcrypto import BufEmptyError, InvalidTag, IVError, is_aead, Encryptor, AEncryptor, ECC, compare_digest
from .hxs2_conn import hxs2_connection
from .util import open_connection


DEFAULT_METHOD = 'aes-128-cfb'
DEFAULT_HASH = 'SHA256'
MAC_LEN = 16
CTX = b'hxsocks'


def parse_hostport(host, default_port=80):
    m = re.match(r'(.+):(\d+)$', host)
    if m:
        return m.group(1).strip('[]'), int(m.group(2))
    else:
        return host.strip('[]'), default_port


class KeyManager(object):
    def __init__(self, server_cert):
        '''server_cert: path to server_cert'''
        self.SERVER_CERT = ECC(from_file=server_cert)
        self.USER_PASS = {}

    def add_user(self, user, password):
        self.USER_PASS[user] = password

    def remove_user(self, user):
        del self.USER_PASS[user]

    def key_xchange(self, user_pkey, key_len):
        # create_key
        ecc = ECC(key_len)
        shared_secret = ecc.get_dh_key(user_pkey)
        return ecc.get_pub_key(), shared_secret

    def verify_user(self, usn, psw):
        if usn not in self.USER_PASS:
            raise ValueError('no such user (%s)' % usn)
        if psw != self.USER_PASS[usn]:
            raise ValueError('wrong password (%s)' % usn)


class ForwardContext:
    def __init__(self):
        self.last_active = time.time()
        # eof recieved
        self.remote_eof = False
        self.local_eof = False
        # link status
        self.writeable = True
        self.readable = True


class HandlerFactory:
    def __init__(self, _class, serverinfo, kmgr, log_level):
        self._class = _class
        self.kmgr = kmgr

        self.serverinfo = serverinfo
        p = urllib.parse.urlparse(serverinfo)
        q = urllib.parse.parse_qs(p.query)
        if p.scheme == 'ss':
            self.PSK, self.method = p.password, p.username
            self.ss_enable = True
        elif p.scheme == 'hxs':
            self.PSK = q.get('PSK', [''])[0]
            self.method = q.get('method', [DEFAULT_METHOD])[0]
            self.ss_enable = self.PSK and urllib.parse.parse_qs(p.query).get('ss', ['1'])[0] == '1'
        else:
            raise ValueError('bad serverinfo: {}'.format(self.serverinfo))

        self.aead = is_aead(self.method)

        # HTTP proxy only
        proxy = q.get('proxy', [''])[0]
        self.proxy = parse_hostport(proxy) if proxy else None

        self.address = (p.hostname, p.port)

        self.logger = logging.getLogger('hxs_%d' % self.address[1])
        self.logger.setLevel(int(q.get('log_level', [log_level])[0]))
        hdr = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s %(name)s:%(levelname)s %(message)s',
                                      datefmt='%H:%M:%S')
        hdr.setFormatter(formatter)
        self.logger.addHandler(hdr)

        self.logger.warning('starting server: {}'.format(serverinfo))

    async def handle(self, reader, writer):
        _handler = self._class(self)
        await _handler.handle(reader, writer)


class HXsocksHandler:
    bufsize = 8192

    def __init__(self, server):
        self.server = server
        self.logger = server.logger
        self.encryptor = Encryptor(self.server.PSK, self.server.method)
        self._buf = b''

    async def _read(self, size=None):
        if self.server.aead:
            _len = await self.client_reader.readexactly(18)
            if not _len:
                return b''
            _len = self.encryptor.decrypt(_len)
            _len, = struct.unpack("!H", _len)
            ct = await self.client_reader.readexactly(_len + 16)
            if not ct:
                return b''
        else:
            size = size or self.bufsize
            ct = await self.client_reader.read(size)
        return self.encryptor.decrypt(ct)

    async def read(self, size=None):
        # compatible with shadowsocks aead
        if not size:
            if self._buf:
                buf, self._buf = self._buf, b''
                return buf
            else:
                return await self._read()
        else:
            while len(self._buf) < size:
                self._buf += (await self._read(size - len(self._buf)))
            _buf, self._buf = self._buf[:size], self._buf[size:]
            return _buf

    async def handle(self, client_reader, client_writer):
        try:
            await self._handle(client_reader, client_writer)
        except Exception as e:
            self.logger.error(repr(e))
            self.logger.error(traceback.format_exc())
        client_writer.close()

    async def _handle(self, client_reader, client_writer):
        self.client_address = client_writer.get_extra_info('peername')
        self.client_reader = client_reader
        self.logger.debug('incoming connection {}'.format(self.client_address))

        try:
            fut = self.client_reader.readexactly(self.encryptor._iv_len)
            iv = await asyncio.wait_for(fut, timeout=10)
            self.encryptor.decrypt(iv)
        except IVError:
            self.logger.error('iv reused, {}'.format(self.client_address))
            await self.play_dead()
            return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionResetError):
            self.logger.warning('iv read failed, {}'.format(self.client_address))
            return

        req_count = 0

        while True:
            if req_count:
                # Not shadowsocks request
                self.logger.debug('excepting next request: {}'.format(self.client_address))
                fut = client_reader.readexactly(2)
                try:
                    await asyncio.wait_for(fut, timeout=120)
                except (OSError, ConnectionResetError, asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                    self.logger.debug('closed: {} {}'.format(e, self.client_address))
                    return

            try:
                fut = self.read(1)
                cmd = await asyncio.wait_for(fut, timeout=10)
            except asyncio.TimeoutError:
                self.logger.debug('read cmd timed out. {}'.format(self.client_address))
                return
            except (ConnectionResetError, asyncio.IncompleteReadError):
                self.logger.debug('read cmd reset. {}'.format(self.client_address))
                return
            except InvalidTag:
                self.logger.error('InvalidTag while read cmd. {}'.format(self.client_address))
                await self.play_dead()
                return
            cmd = cmd[0]
            self.logger.debug('cmd: {} {}'.format(cmd, self.client_address))

            if cmd in (1, 3, 4):
                # A shadowsocks request
                result = await self.handle_ss(client_reader, client_writer, addr_type=cmd)
                if result:
                    await self.play_dead()
                return
            elif cmd == 20:  # hxsocks2 client key exchange
                req_count += 1
                rint = random.randint(64, 2048)
                req_len = await self.read(2)
                req_len, = struct.unpack('>H', req_len)
                data = await self.read(req_len)
                data = io.BytesIO(data)

                pklen = data.read(1)[0]
                client_pkey = data.read(pklen)
                client_auth = data.read(32)

                def _send(data):
                    if self.encryptor._encryptor:
                        data = struct.pack('>H', len(data)) + data
                        ct = self.encryptor.encrypt(data)
                        client_writer.write(struct.pack('>H', len(ct)) + ct)
                    else:
                        data = struct.pack('>H', len(data)) + data
                        client_writer.write(self.encryptor.encrypt(data))

                if not compare_digest(
                        hmac.new(self.server.PSK.encode(), client_pkey, hashlib.sha256).digest(),
                        client_auth):
                    self.logger.error('handshake info mishash. {}'.format(self.client_address))
                    await self.play_dead()
                    return
                pkey, secret = self.server.kmgr.key_xchange(client_pkey, self.encryptor._key_len)
                if pkey:
                    self.logger.info('new key exchange. {}'.format(self.client_address))
                    auth = hmac.new(self.server.PSK.encode(), client_pkey + pkey, hashlib.sha256).digest()
                    scert = self.server.kmgr.SERVER_CERT.get_pub_key()
                    signature = self.server.kmgr.SERVER_CERT.sign(auth, DEFAULT_HASH)
                    data = bytes((0, len(pkey), len(scert), len(signature))) + pkey + auth + scert + signature + os.urandom(rint)
                    _send(data)

                    client_pkey = hashlib.md5(client_pkey).digest()
                    conn = hxs2_connection(client_reader,
                                           client_writer,
                                           secret,
                                           self.server.kmgr,
                                           self.server.method,
                                           self.server.proxy,
                                           self.logger)
                    err = await conn.wait_close()
                    if err:
                        await self.play_dead()
                    return
                else:
                    self.logger.error('Private_key already registered. {}'.format(self.client_address))
                    await self.play_dead()
                    return
            else:
                # TODO: security
                self.logger.error('bad cmd: %s, %s' % (cmd, self.client_address))
                await self.play_dead()
                return

    async def play_dead(self, timeout=1):
        for _ in range(10):
            fut = self.client_reader.read(self.bufsize)
            try:
                await asyncio.wait_for(fut, timeout)
            except (asyncio.TimeoutError, ConnectionResetError):
                return

    async def handle_ss(self, client_reader, client_writer, addr_type):
        # if error, return 1
        # get header...
        try:
            assert addr_type in (1, 3, 4)
            if addr_type & 15 == 1:
                addr = await self.read(4)
                addr = socket.inet_ntoa(addr)
            elif addr_type & 15 == 3:
                data = await self.read(1)
                addr = await self.read(data[0])
                addr = addr.decode('ascii')
            else:
                data = await self.read(16)
                addr = socket.inet_ntop(socket.AF_INET6, data)
            port = await self.read(2)
            port, = struct.unpack('>H', port)
        except Exception as e:
            self.logger.error('error on read ss header: {} {}'.format(e, self.client_address))
            self.logger.error(traceback.format_exc())
            return 1

        self.logger.info('connect to {}:{} {!r} {!r}'.format(addr, port, self.client_address, self.server.proxy))

        try:
            remote_reader, remote_writer = await open_connection(addr, port, self.server.proxy)
        except Exception as e:
            self.logger.error('connect to {}:{} failed! {!r}'.format(addr, port, e))
            return

        context = ForwardContext()

        tasks = [self.ss_forward_A(client_reader, remote_writer, self.encryptor.decrypt, context),
                 self.ss_forward_B(remote_reader, client_writer, self.encryptor.encrypt, context),
                 ]
        try:
            await asyncio.wait(tasks)
        except Exception as e:
            self.logger.error(repr(e))
            self.logger.error(traceback.format_exc())
        remote_writer.close()

    async def ss_forward_A(self, read_from, write_to, cipher, context, timeout=60):
        # data from ss client
        while True:
            try:
                fut = self.read()
                data = await asyncio.wait_for(fut, timeout=5)
                context.last_active = time.time()
            except asyncio.TimeoutError:
                if time.time() - context.last_active > timeout or context.remote_eof:
                    data = b''
                else:
                    continue
            except (BufEmptyError, asyncio.IncompleteReadError, InvalidTag, ConnectionResetError, OSError):
                data = b''

            if not data:
                break
            try:
                write_to.write(data)
                await write_to.drain()
            except ConnectionResetError:
                context.local_eof = True
                return
        context.local_eof = True
        try:
            write_to.write_eof()
        except (ConnectionResetError, OSError):
            pass

    async def ss_forward_B(self, read_from, write_to, cipher, context, timeout=60):
        # data from remote
        while True:
            try:
                fut = read_from.read(self.bufsize)
                data = await asyncio.wait_for(fut, timeout=5)
                context.last_active = time.time()
            except asyncio.TimeoutError:
                if time.time() - context.last_active > timeout or context.local_eof:
                    data = b''
                else:
                    continue
            except (ConnectionResetError, OSError):
                data = b''

            if not data:
                break
            data = cipher(data)
            try:
                write_to.write(data)
                await write_to.drain()
            except ConnectionResetError:
                context.remote_eof = True
                return
        context.remote_eof = True
        # write_to.write_eof()
