#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2012, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import numbers
import random
from collections import OrderedDict
from io import BytesIO
from struct import pack

from calibre.ebooks.mobi.utils import align_block
from polyglot.builtins import as_bytes, iteritems

NULL = 0xffffffff


def zeroes(x):
    return (b'\x00' * x)


def nulls(x):
    return (b'\xff' * x)


def short(x):
    return pack(b'>H', x)


class Header(OrderedDict):

    HEADER_NAME = b''

    DEFINITION = '''
    '''

    ALIGN_BLOCK = False
    POSITIONS = {}  # Mapping of position field to field whose position should
    # be stored in the position field
    SHORT_FIELDS = set()

    def __init__(self):
        OrderedDict.__init__(self)

        for line in self.DEFINITION.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            name, val = (x.strip() for x in line.partition('=')[0::2])
            if val:
                val = eval(val, {'zeroes':zeroes, 'NULL':NULL, 'DYN':None,
                    'nulls':nulls, 'short':short, 'random':random})
            else:
                val = 0
            if name in self:
                raise ValueError(f'Duplicate field in definition: {name!r}')
            self[name] = val

    @property
    def dynamic_fields(self):
        return tuple(k for k, v in iteritems(self) if v is None)

    def __call__(self, **kwargs):
        positions = {}
        for name, val in iteritems(kwargs):
            if name not in self:
                raise KeyError(f'Not a valid header field: {name!r}')
            self[name] = val

        buf = BytesIO()
        buf.write(as_bytes(self.HEADER_NAME))
        for name, val in iteritems(self):
            val = self.format_value(name, val)
            positions[name] = buf.tell()
            if val is None:
                raise ValueError(f'Dynamic field {name!r} not set')
            if isinstance(val, numbers.Integral):
                fmt = b'H' if name in self.SHORT_FIELDS else b'I'
                val = pack(b'>'+fmt, val)
            buf.write(val)

        for pos_field, field in iteritems(self.POSITIONS):
            buf.seek(positions[pos_field])
            buf.write(pack(b'>I', positions[field]))

        ans = buf.getvalue()
        if self.ALIGN_BLOCK:
            ans = align_block(ans)
        return ans

    def format_value(self, name, val):
        return val
