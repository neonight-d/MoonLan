"""Reading an SNMP value for what it is.

`bytes(value)` looks like the obvious way to get at an OctetString's
payload, and for an OctetString it is. But pysnmp's integer types —
Counter32, Gauge32, Integer, TimeTicks — implement `__index__`, so
`bytes(value)` takes the `bytes(int)` path instead and **allocates a
zero buffer as long as the number**. No exception is raised, so a
`try/except` around it catches nothing.

    bytes(Integer(3))            -> b'\\x00\\x00\\x00'
    bytes(Counter32(1486971185)) -> a request for 1.49 GB

That is what killed `diag --walk` on an ifOutOctets column, and the
same call sat in the LLDP and STP parsers, where an agent answering
with an integer where an octet string was expected would have taken
the service down rather than a CLI.

So: convert only what actually carries octets, and decide that by the
type rather than by whether `bytes()` happened to succeed.

The same module answers a second question of the same shape: whether
the agent answered at all. In SNMPv2c "there is no such object here"
arrives inside a **successful** PDU, as one of three sentinel values in
the varbind — noSuchObject, noSuchInstance, endOfMibView. No error
status, no error indication, so every check further up passes and a
non-answer travels on as a value. All three derive from OctetString,
so they even survive `as_octets` as an innocent-looking b"". That is
how a probe for a vendor branch succeeded on every device that speaks
SNMP at all, and three switches were told their loop protection was
switched off. `is_no_such` is the one place that knows the difference.
"""

from __future__ import annotations


def is_octets(value) -> bool:
    """True when the value is a string of octets, not a number.

    pysnmp's OctetString (and everything derived from it, IpAddress
    included) exposes `asOctets`; the numeric types do not. Duck typing
    rather than isinstance keeps this module free of a pysnmp import,
    which is what lets lldp.py and stp.py use it.
    """
    return (
        hasattr(value, "asOctets")
        or isinstance(value, (bytes, bytearray, memoryview))
    )


# The three varbind values that mean "no answer for this OID". They are
# matched by class name rather than by isinstance for the same reason
# the rest of this module uses duck typing: it keeps pysnmp out of the
# import graph of lldp.py, stp.py and loopdetect.py.
NO_SUCH_TYPES = frozenset({"NoSuchObject", "NoSuchInstance", "EndOfMibView"})


def is_no_such(value) -> bool:
    """True for noSuchObject / noSuchInstance / endOfMibView.

    An SNMPv2c agent reports "I do not implement that" with a normal,
    successful response carrying one of these in place of the value.
    It is the absence of an answer wearing the shape of one, and
    treating it as data is the mistake this function exists to stop.
    """
    return type(value).__name__ in NO_SUCH_TYPES


def as_octets(value) -> bytes:
    """The octets a value carries; b"" for anything that carries none.

    A number carries none. Its decimal form is not its payload, and
    turning it into one invents data — the "hex: 00 00 00" printed next
    to `lldpLocPortIdSubtype = 3` in an early walk was not an encoding
    of three, it was three bytes of nothing.
    """
    if hasattr(value, "asOctets"):
        return bytes(value.asOctets())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return b""
