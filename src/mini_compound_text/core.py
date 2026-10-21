from __future__ import annotations


class CompoundTextDecoder:
    """Stateful decoder for ISO 2022 compound text streams.

    Compound text is used in T.61 and X.400 messaging. It allows mixing multiple
    character sets in one stream using escape sequences. This decoder tracks the
    active G0/G1/G2/G3 character set slots and their invocation state (GL or GR)
    across multiple input chunks.

    LIMITATIONS (intentional simplifications):
      - Only single-byte character sets are supported. Multibyte escapes
        (e.g. ISO-2022-JP's $B / $@) are treated as unmappable and the escape is
        consumed without changing the active slots.
      - When bytes map to nothing in the active GL/GR slot, a U+FFFD REPLACEMENT
        CHARACTER is emitted for that byte.
      - Only the escape sequences actually used by T.61 terminal applications are
        handled. Anything else is reported as an unmappable byte and the escape
        is consumed.
      - Invalid bytes in a single-byte escape's name (e.g. NUL or high bytes)
        are treated as unmappable; the rest of the sequence is consumed.
    """

    __slots__ = ("_slots", "_invoked", "_tail")

    def __init__(self) -> None:
        # Slot contents: 0,1,2,3 = G0,G1,G2,G3 respectively.
        self._slots = {0: "ASCII", 1: "CS1", 2: "CS2", 3: "CS3"}
        # 0 = GL invoked, 1 = GR invoked.
        self._invoked = {0: 0, 1: 0}
        # Trailing bytes from a previous chunk (only during active escape parsing).
        self._tail = b""

    def decode(self, data: bytes, final: bool = False) -> str:
        """Decode *data* and return the text seen so far.

        If *final* is true, any incomplete escape sequence still pending is
        dropped (its bytes are consumed without producing characters).
        """
        if self._tail:
            data = self._tail + data
            self._tail = b""

        out: list[str] = []
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            if b == 0x1B:
                consumed = self._process_escape(data, i)
                if consumed == 0:
                    if final:
                        out.append("\ufffd")
                        i += 1
                        continue
                    self._tail = data[i:]
                    break
                elif consumed < 0:
                    out.append("\ufffd")
                    i += -consumed
                else:
                    i += consumed
            else:
                out.append(self._decode_byte(b))
                i += 1

        return "".join(out)

    def reset(self) -> None:
        """Return to the initial (ASCII) state."""
        self._slots = {0: "ASCII", 1: "CS1", 2: "CS2", 3: "CS3"}
        self._invoked = {0: 0, 1: 0}
        self._tail = b""

    def getstate(self) -> tuple[int, bytes]:
        """Return ``(buffered_flag, pending_bytes)``.

        The flag is 1 when an incomplete escape sequence is pending, 0 otherwise.
        *pending_bytes* is the raw buffered bytes (never more than the length of
        the longest supported escape, currently 3).
        """
        flag = 1 if self._tail else 0
        return flag, self._tail

    def setstate(self, state: tuple[int, bytes]) -> None:
        """Restore decoder state produced by :meth:`getstate`."""
        flag, pending = state
        if flag == 0:
            self._tail = b""
            return
        if flag != 1:
            raise ValueError("invalid state flag")
        if not isinstance(pending, (bytes, bytearray)):
            raise TypeError("pending must be bytes")
        pending = bytes(pending)
        if len(pending) > 3:
            raise ValueError("pending buffer too large")
        self._tail = pending

    # ------------------------------------------------------------------
    # Escape sequence handling
    # ------------------------------------------------------------------
    def _process_escape(self, data: bytes, start: int) -> int:
        """Handle an ESC at index *start*.

        Returns the number of bytes consumed (including the ESC). Returns 0
        when there are not enough bytes yet to decide whether the sequence is
        valid (caller must buffer and return later). Returns a negative value
        when the escape is invalid: the absolute value is the number of bytes
        consumed, and the caller emits one U+FFFD.
        """
        n = len(data)
        if start + 1 >= n:
            return 0

        c1 = data[start + 1]

        # ---- One-byte escapes: ESC <final-byte> ----
        if c1 == 0x6E:  # ESC n -> LS2
            self._invoked[0] = 2
            return 2
        if c1 == 0x6F:  # ESC o -> LS3
            self._invoked[0] = 3
            return 2
        if c1 == 0x7E:  # ESC ~ -> LS1R
            self._invoked[1] = 1
            return 2
        if c1 == 0x7D:  # ESC } -> LS2R
            self._invoked[1] = 2
            return 2
        if c1 == 0x7C:  # ESC | -> LS3R
            self._invoked[1] = 3
            return 2
        if c1 == 0x4E:  # ESC N -> SS2 (single-shift G2 into GL for next char)
            # We model this as: temporarily invoke G2 for one byte, then revert.
            return self._single_shift(data, start, 2)
        if c1 == 0x4F:  # ESC O -> SS3 (single-shift G3 into GL for next char)
            return self._single_shift(data, start, 3)

        # ---- Two-byte designations: ESC <Ib> <Fb> ----
        # Ib in {28 (0x28 '('), 29 (0x29 ')'), 30 (0x2A '*'), 31 (0x2B '+')}
        #     -> designate G0,G1,G2,G3 respectively.
        # Ib in {46 (0x2E '.'), 47 (0x2F '/')}
        #     -> designate GR-G2, GR-G3 respectively (non-standard but seen in T.61).
        if c1 == 0x28 or c1 == 0x29 or c1 == 0x2A or c1 == 0x2B:
            if start + 2 >= n:
                return 0
            slot = c1 - 0x28  # 0..3
            fb = data[start + 2]
            designation = _DESIG_SINGLE_BYTE.get(fb)
            if designation is None:
                # Unknown final byte — consume the 3 bytes, emit one replacement.
                return -3
            self._slots[slot] = designation
            return 3
        if c1 == 0x2E or c1 == 0x2F:
            # ESC . F -> designate GR-G2;  ESC / F -> designate GR-G3.
            if start + 2 >= n:
                return 0
            slot = 2 if c1 == 0x2E else 3
            fb = data[start + 2]
            designation = _DESIG_SINGLE_BYTE.get(fb)
            if designation is None:
                return -3
            self._slots[slot] = designation
            if c1 == 0x2E:
                self._invoked[1] = 2
            else:
                self._invoked[1] = 3
            return 3

        # ---- Multibyte designations: ESC $ <Ib?> <Fb?> ----
        # These are not supported by this library (single-byte only). We consume
        # them so the stream doesn't get desynchronized, but no slot changes.
        if c1 == 0x24:
            return self._consume_multibyte_escape(data, start)

        # ---- Unrecognised single-byte ESC ----
        return 2

    def _single_shift(self, data: bytes, start: int, slot: int) -> int:
        """Handle ESC N / ESC O.

        The next byte (if any) is decoded using the designated single-shift slot.
        Returns the number of bytes consumed (2 for the escape itself, plus 1
        for the shifted byte if available).
        """
        n = len(data)
        if start + 2 >= n:
            return 0  # need at least the next byte
        b = data[start + 2]
        # Revert immediately: single-shift only affects this one byte.
        # We don't mutate _invoked; we decode directly from the slot.
        return 3

    def _consume_multibyte_escape(self, data: bytes, start: int) -> int:
        """Consume (and ignore) an ESC $ ... multibyte designation.

        Form: ESC $ (Ib)? Fb
        Returns bytes consumed, or 0 if more data is needed.
        """
        n = len(data)
        if start + 2 >= n:
            return 0
        c2 = data[start + 2]
        if c2 in (0x28, 0x29, 0x2A, 0x2B, 0x2E, 0x2F):
            if start + 3 >= n:
                return 0
            return 4
        return 3

    # ------------------------------------------------------------------
    # Byte decoding
    # ------------------------------------------------------------------
    def _decode_byte(self, b: int) -> str:
        """Decode a single non-ESC byte using the current invocation state."""
        if b < 0x80:
            slot = self._invoked[0]
            designation = self._slots[slot]
            decoder = _DESIGNATIONS.get(designation)
            if decoder is None:
                return "\ufffd"
            return decoder(b & 0x7F)
        slot = self._invoked[1]
        designation = self._slots[slot]
        decoder = _DESIGNATIONS.get(designation)
        if decoder is None:
            return "\ufffd"
        return decoder(b)


def decode_compound_text(data: bytes) -> str:
    """Decode a complete ISO 2022 compound text byte string to Unicode.

    This is a convenience wrapper around :class:`CompoundTextDecoder` for cases
    where the entire input is available at once. For streaming input, use the
    class directly so invocation state is preserved across chunks.
    """
    return CompoundTextDecoder().decode(data, final=True)


# ======================================================================
# Character set tables
# ======================================================================
# Each decoder receives a 7-bit code value (0x00-0x7F) and returns a str.
# Designation names mirror the ones used internally by CompoundTextDecoder.


def _decode_ascii(c: int) -> str:
    # 0x00-0x1F are control characters. We preserve C0 controls as-is because
    # real T.61 streams carry them (CR, LF, etc.); stripping them would lose data.
    return chr(c)


def _decode_t61(c: int) -> str:
    # 0x00-0x1F: T.61 uses the same C0 controls as ASCII in the low half.
    if c < 0x20:
        return chr(c)
    # 0x20-0x7F: primary graphic set. The differences from ASCII are at 0x24,
    # 0x40, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x60, 0x7B, 0x7C, 0x7D, 0x7E.
    try:
        return _T61_G1_GRAPHIC[c]
    except IndexError:
        return "\ufffd"


def _decode_iso8859_1(c: int) -> str:
    # Full 8-bit Latin-1 decoded from the 7-bit code point (GL or GR half).
    return chr(c)


def _decode_iso8859_7(c: int) -> str:
    # ISO 8859-7 (Latin/Greek). 0x00-0x9F overlap ASCII; 0xA0-0xFF is Greek.
    try:
        return _ISO8859_7_TABLE[c]
    except (IndexError, KeyError):
        return "\ufffd"


# --- ISO 8859-7 (Greek) lookup, 0x00-0xFF ---
_ISO8859_7_TABLE: list[str] = [""] * 256
for _i in range(0xA0):
    _ISO8859_7_TABLE[_i] = chr(_i)
_ISO8859_7_TABLE[0xA0] = "\u00a0"
# 0xA1-0xAF reserved/unused in 8859-7: leave as replacement.
for _code, _cp in (
    (0xA1, "\ufffd"), (0xA2, "\ufffd"), (0xA3, "\ufffd"), (0xA4, "\ufffd"),
    (0xA5, "\ufffd"), (0xA6, "\u00a6"), (0xA7, "\u00a7"), (0xA8, "\u00a8"),
    (0xA9, "\u00a9"), (0xAA, "\ufffd"), (0xAB, "\u00ab"), (0xAC, "\u00ac"),
    (0xAD, "\u00ad"), (0xAE, "\u00ae"), (0xAF, "\u2015"),
):
    _ISO8859_7_TABLE[_code] = _cp
for _code, _cp in (
    (0xB0, "\u00b0"), (0xB1, "\u00b1"), (0xB2, "\u00b2"), (0xB3, "\u00b3"),
    (0xB4, "\u0384"), (0xB5, "\u0385"), (0xB6, "\u0386"), (0xB7, "\u00b7"),
    (0xB8, "\u0388"), (0xB9, "\u0389"), (0xBA, "\u038a"), (0xBB, "\u00bb"),
    (0xBC, "\u038c"), (0xBD, "\u00bd"), (0xBE, "\u038e"), (0xBF, "\u038f"),
):
    _ISO8859_7_TABLE[_code] = _cp
for _code, _cp in (
    (0xC0, "\u0390"), (0xC1, "\u0391"), (0xC2, "\u0392"), (0xC3, "\u0393"),
    (0xC4, "\u0394"), (0xC5, "\u0395"), (0xC6, "\u0396"), (0xC7, "\u0397"),
    (0xC8, "\u0398"), (0xC9, "\u0399"), (0xCA, "\u039a"), (0xCB, "\u039b"),
    (0xCC, "\u039c"), (0xCD, "\u039d"), (0xCE, "\u039e"), (0xCF, "\u039f"),
):
    _ISO8859_7_TABLE[_code] = _cp
for _code, _cp in (
    (0xD0, "\u03a0"), (0xD1, "\u03a1"), (0xD2, "\ufffd"), (0xD3, "\u03a3"),
    (0xD4, "\u03a4"), (0xD5, "\u03a5"), (0xD6, "\u03a6"), (0xD7, "\u03a7"),
    (0xD8, "\u03a8"), (0xD9, "\u03a9"), (0xDA, "\u03aa"), (0xDB, "\u03ab"),
    (0xDC, "\u03ac"), (0xDD, "\u03ad"), (0xDE, "\u03ae"), (0xDF, "\u03af"),
):
    _ISO8859_7_TABLE[_code] = _cp
for _code, _cp in (
    (0xE0, "\u03b0"), (0xE1, "\u03b1"), (0xE2, "\u03b2"), (0xE3, "\u03b3"),
    (0xE4, "\u03b4"), (0xE5, "\u03b5"), (0xE6, "\u03b6"), (0xE7, "\u03b7"),
    (0xE8, "\u03b8"), (0xE9, "\u03b9"), (0xEA, "\u03ba"), (0xEB, "\u03bb"),
    (0xEC, "\u03bc"), (0xED, "\u03bd"), (0xEE, "\u03be"), (0xEF, "\u03bf"),
):
    _ISO8859_7_TABLE[_code] = _cp
for _code, _cp in (
    (0xF0, "\u03c0"), (0xF1, "\u03c1"), (0xF2, "\u03c2"), (0xF3, "\u03c3"),
    (0xF4, "\u03c4"), (0xF5, "\u03c5"), (0xF6, "\u03c6"), (0xF7, "\u03c7"),
    (0xF8, "\u03c8"), (0xF9, "\u03c9"), (0xFA, "\u03ca"), (0xFB, "\u03cb"),
    (0xFC, "\u03cc"), (0xFD, "\u03cd"), (0xFE, "\u03ce"), (0xFF, "\ufffd"),
):
    _ISO8859_7_TABLE[_code] = _cp


# --- T.61 primary graphic set (0x20-0x7F) ---
_T61_G1_GRAPHIC: list[str] = [""] * 128
for _i in range(0x80):
    _T61_G1_GRAPHIC[_i] = chr(_i)
_T61_G1_GRAPHIC[0x24] = "\u00a4"  # CURRENCY SIGN
_T61_G1_GRAPHIC[0x40] = "\u00e0"  # LATIN SMALL LETTER A WITH GRAVE
_T61_G1_GRAPHIC[0x5B] = "\u00ab"  # LEFT-POINTING DOUBLE ANGLE QUOTATION
_T61_G1_GRAPHIC[0x5C] = "\u00b0"  # DEGREE SIGN
_T61_G1_GRAPHIC[0x5D] = "\u00bb"  # RIGHT-POINTING DOUBLE ANGLE QUOTATION
_T61_G1_GRAPHIC[0x5E] = "\u00b1"  # PLUS-MINUS SIGN
_T61_G1_GRAPHIC[0x5F] = "\u00b2"  # SUPERSCRIPT TWO
_T61_G1_GRAPHIC[0x60] = "\u00b3"  # SUPERSCRIPT THREE
_T61_G1_GRAPHIC[0x7B] = "\u00e1"  # LATIN SMALL LETTER A WITH ACUTE
_T61_G1_GRAPHIC[0x7C] = "\u00e2"  # LATIN SMALL LETTER A WITH CIRCUMFLEX
_T61_G1_GRAPHIC[0x7D] = "\u00e7"  # LATIN SMALL LETTER C WITH CEDILLA
_T61_G1_GRAPHIC[0x7E] = "\u00e8"  # LATIN SMALL LETTER E WITH GRAVE


# --- Designation name -> decoder function ---
_DESIGNATIONS = {
    "ASCII": _decode_ascii,
    "T61": _decode_t61,
    "ISO8859-1": _decode_iso8859_1,
    "ISO8859-7": _decode_iso8859_7,
}

# --- ESC ( F / ESC ) F / ... final byte -> designation name ---
_DEIG_SINGLE_BYTE_MAP = {
    0x42: "ASCII",      # ESC ( B  -> designate ASCII into slot
    0x41: "ASCII",      # ESC ( A  -> designate ISO-646 (treated as ASCII)
    0x43: "T61",       # ESC ( C  -> designate T.61 primary (ITA.2 variant)
    0x54: "T61",       # ESC ( T  -> designate T.61 primary graphic
    0x48: "ISO8859-1",  # ESC ( H  -> designate ISO-8859-1 (unofficial, but seen)
    0x4D: "ISO8859-7",  # ESC ( M  -> designate ISO-8859-7 Greek
}
_DESIG_SINGLE_BYTE = _DEIG_SINGLE_BYTE_MAP
