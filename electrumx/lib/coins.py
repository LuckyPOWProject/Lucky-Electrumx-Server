# Copyright (c) 2016-2017, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# The MIT License (MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

'''Module providing coin abstraction.

Anything coin-specific should go in this file and be subclassed where
necessary for appropriate handling.
'''
import re
import struct
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from hashlib import sha256
from typing import Sequence, Tuple

import electrumx.lib.util as util
from electrumx.lib.hash import Base58, double_sha256, hash_to_hex_str
from electrumx.lib.hash import HASHX_LEN, hex_str_to_hash
from electrumx.lib.script import (_match_ops, Script, ScriptError,
                                  ScriptPubKey, OpCodes)
import electrumx.lib.tx as lib_tx
from electrumx.lib.tx import Tx
import electrumx.lib.tx_dash as lib_tx_dash
import electrumx.lib.tx_axe as lib_tx_axe
import electrumx.server.block_processor as block_proc
import electrumx.server.daemon as daemon
from electrumx.server.session import (ElectrumX, DashElectrumX,
                                      SmartCashElectrumX, AuxPoWElectrumX,
                                      NameIndexElectrumX, NameIndexAuxPoWElectrumX)


@dataclass
class Block:
    __slots__ = "raw", "header", "transactions"
    raw: bytes
    header: bytes
    transactions: Sequence[Tuple[Tx, bytes]]


class CoinError(Exception):
    '''Exception raised for coin-related errors.'''


class Coin:
    '''Base class of coin hierarchy.'''

    REORG_LIMIT = 200
    # Not sure if these are coin-specific
    RPC_URL_REGEX = re.compile('.+@(\\[[0-9a-fA-F:]+\\]|[^:]+)(:[0-9]+)?')
    VALUE_PER_COIN = 100000000
    CHUNK_SIZE = 2016
    BASIC_HEADER_SIZE = 80
    STATIC_BLOCK_HEADERS = True
    SESSIONCLS = ElectrumX
    DEFAULT_MAX_SEND = 1000000
    DESERIALIZER = lib_tx.Deserializer
    DAEMON = daemon.Daemon
    BLOCK_PROCESSOR = block_proc.BlockProcessor
    HEADER_VALUES = ('version', 'prev_block_hash', 'merkle_root', 'timestamp',
                     'bits', 'nonce')
    HEADER_UNPACK = struct.Struct('< I 32s 32s I I I').unpack_from
    P2PKH_VERBYTE = bytes.fromhex("00")
    P2SH_VERBYTES = (bytes.fromhex("05"),)
    XPUB_VERBYTES = bytes('????', 'utf-8')
    XPRV_VERBYTES = bytes('????', 'utf-8')
    WIF_BYTE = bytes.fromhex("80")
    ENCODE_CHECK = Base58.encode_check
    DECODE_CHECK = Base58.decode_check
    GENESIS_HASH = ('000000000019d6689c085ae165831e93'
                    '4ff763ae46a2a6c172b3f1b60a8ce26f')
    GENESIS_ACTIVATION = 100_000_000

    MEMPOOL_HISTOGRAM_REFRESH_SECS = 500
    # first bin size in vbytes. smaller bins mean more precision but also bandwidth:
    MEMPOOL_COMPACT_HISTOGRAM_BINSIZE = 30_000

    # Peer discovery
    PEER_DEFAULT_PORTS = {'t': '50001', 's': '50002'}
    PEERS = []
    CRASH_CLIENT_VER = None
    BLACKLIST_URL = None
    ESTIMATEFEE_MODES = (None, 'CONSERVATIVE', 'ECONOMICAL')

    RPC_PORT: int
    NAME: str
    NET: str

    # only used for initial db sync ETAs:
    TX_COUNT_HEIGHT: int  # at a given snapshot of the chain,
    TX_COUNT: int         # there have been this many txs so far,
    TX_PER_BLOCK: int     # and from that height onwards, we guess this many txs per block

    @classmethod
    def lookup_coin_class(cls, name, net):
        '''Return a coin class given name and network.

        Raise an exception if unrecognised.'''
        req_attrs = ('TX_COUNT', 'TX_COUNT_HEIGHT', 'TX_PER_BLOCK')
        for coin in util.subclasses(Coin):
            if (coin.NAME.lower() == name.lower() and
                    coin.NET.lower() == net.lower()):
                missing = [
                    attr
                    for attr in req_attrs
                    if not hasattr(coin, attr)
                ]
                if missing:
                    raise CoinError(
                        f'coin {name} missing {missing} attributes'
                    )
                return coin
        raise CoinError(f'unknown coin {name} and network {net} combination')

    @classmethod
    def sanitize_url(cls, url):
        # Remove surrounding ws and trailing /s
        url = url.strip().rstrip('/')
        match = cls.RPC_URL_REGEX.match(url)
        if not match:
            raise CoinError(f'invalid daemon URL: "{url}"')
        if match.groups()[1] is None:
            url = f'{url}:{cls.RPC_PORT:d}'
        if not url.startswith(('http://', 'https://')):
            url = f'http://{url}'
        return url + '/'

    @classmethod
    def max_fetch_blocks(cls, height):
        if height < 130000:
            return 1000
        return 100

    @classmethod
    def genesis_block(cls, block):
        '''Check the Genesis block is the right one for this coin.

        Return the block less its unspendable coinbase.
        '''
        header = cls.block_header(block, 0)
        header_hex_hash = hash_to_hex_str(cls.header_hash(header))
        if header_hex_hash != cls.GENESIS_HASH:
            raise CoinError(f'genesis block has hash {header_hex_hash} '
                            f'expected {cls.GENESIS_HASH}')

        return header + b'\0'

    @classmethod
    def hashX_from_script(cls, script):
        '''Returns a hashX from a script.'''
        return sha256(script).digest()[:HASHX_LEN]

    @staticmethod
    def lookup_xverbytes(verbytes):
        '''Return a (is_xpub, coin_class) pair given xpub/xprv verbytes.'''
        # Order means BTC testnet will override NMC testnet
        for coin in util.subclasses(Coin):
            if verbytes == coin.XPUB_VERBYTES:
                return True, coin
            if verbytes == coin.XPRV_VERBYTES:
                return False, coin
        raise CoinError('version bytes unrecognised')

    @classmethod
    def address_to_hashX(cls, address):
        '''Return a hashX given a coin address.'''
        return cls.hashX_from_script(cls.pay_to_address_script(address))

    @classmethod
    def hash160_to_P2PKH_script(cls, hash160):
        return ScriptPubKey.P2PKH_script(hash160)

    @classmethod
    def hash160_to_P2PKH_hashX(cls, hash160):
        return cls.hashX_from_script(cls.hash160_to_P2PKH_script(hash160))

    @classmethod
    def pay_to_address_script(cls, address):
        '''Return a pubkey script that pays to a pubkey hash.

        Pass the address (either P2PKH or P2SH) in base58 form.
        '''
        raw = cls.DECODE_CHECK(address)

        # Require version byte(s) plus hash160.
        verbyte = -1
        verlen = len(raw) - 20
        if verlen > 0:
            verbyte, hash160 = raw[:verlen], raw[verlen:]

        if verbyte == cls.P2PKH_VERBYTE:
            return cls.hash160_to_P2PKH_script(hash160)
        if verbyte in cls.P2SH_VERBYTES:
            return ScriptPubKey.P2SH_script(hash160)

        raise CoinError(f'invalid address: {address}')

    @classmethod
    def privkey_WIF(cls, privkey_bytes, compressed):
        '''Return the private key encoded in Wallet Import Format.'''
        payload = bytearray(cls.WIF_BYTE + privkey_bytes)
        if compressed:
            payload.append(0x01)
        return cls.ENCODE_CHECK(payload)

    @classmethod
    def header_hash(cls, header):
        '''Given a header return hash'''
        return double_sha256(header)

    @classmethod
    def header_prevhash(cls, header):
        '''Given a header return previous hash'''
        return header[4:36]

    @classmethod
    def static_header_offset(cls, height):
        '''Given a header height return its offset in the headers file.

        If header sizes change at some point, this is the only code
        that needs updating.'''
        assert cls.STATIC_BLOCK_HEADERS
        return height * cls.BASIC_HEADER_SIZE

    @classmethod
    def static_header_len(cls, height):
        '''Given a header height return its length.'''
        return (cls.static_header_offset(height + 1)
                - cls.static_header_offset(height))

    @classmethod
    def block_header(cls, block, height):
        '''Returns the block header given a block and its height.'''
        return block[:cls.static_header_len(height)]

    @classmethod
    def block(cls, raw_block, height):
        '''Return a Block namedtuple given a raw block and its height.'''
        header = cls.block_header(raw_block, height)
        txs = cls.DESERIALIZER(raw_block, start=len(header)).read_tx_block()
        return Block(raw_block, header, txs)

    @classmethod
    def decimal_value(cls, value):
        '''Return the number of standard coin units as a Decimal given a
        quantity of smallest units.

        For example 1 BTC is returned for 100 million satoshis.
        '''
        return Decimal(value) / cls.VALUE_PER_COIN

    @classmethod
    def warn_old_client_on_tx_broadcast(cls, _client_ver):
        return False

    @classmethod
    def bucket_estimatefee_block_target(cls, n: int) -> int:
        '''For caching purposes, it might be desirable to restrict the
        set of values that can be queried as an estimatefee block target.
        '''
        return n


class AuxPowMixin:
    STATIC_BLOCK_HEADERS = False
    DESERIALIZER = lib_tx.DeserializerAuxPow
    SESSIONCLS = AuxPoWElectrumX
    TRUNCATED_HEADER_SIZE = 80
    # AuxPoW headers are significantly larger, so the DEFAULT_MAX_SEND from
    # Bitcoin is insufficient.  In Namecoin mainnet, 5 MB wasn't enough to
    # sync, while 10 MB worked fine.
    DEFAULT_MAX_SEND = 10000000

    @classmethod
    def header_hash(cls, header):
        '''Given a header return hash'''
        return double_sha256(header[:cls.BASIC_HEADER_SIZE])

    @classmethod
    def block_header(cls, block, height):
        '''Return the AuxPow block header bytes'''
        deserializer = cls.DESERIALIZER(block)
        return deserializer.read_header(cls.BASIC_HEADER_SIZE)


class Luckycoin(AuxPowMixin, Coin):
    NAME = "Luckycoin"
    SHORTNAME = "LUCKY"
    NET = "mainnet"
    XPUB_VERBYTES = bytes.fromhex("0488b21e")
    XPRV_VERBYTES = bytes.fromhex("0488ade4")
    P2PKH_VERBYTE = bytes.fromhex("2f")
    P2SH_VERBYTES = (bytes.fromhex("05"),)
    WIF_BYTE = bytes.fromhex("b0")
    TX_COUNT = 1
    TX_COUNT_HEIGHT = 1
    TX_PER_BLOCK = 200
    REORG_LIMIT = 2000    
    GENESIS_ACTIVATION = 0
    GENESIS_HASH = ('9b7bce58999062b63bfb18586813c424'
                    '91fa32f4591d8d3043cb4fa9e551541b')
    DESERIALIZER = lib_tx.DeserializerAuxPowSegWit



