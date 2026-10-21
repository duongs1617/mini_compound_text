import unittest

from mini_compound_text import CompoundTextDecoder, decode_compound_text


class TestAscii(unittest.TestCase):
    def test_plain_ascii(self):
        self.assertEqual(decode_compound_text(b"Hello, world!"), "Hello, world!")

    def test_empty(self):
        self.assertEqual(decode_compound_text(b""), "")

    def test_controls_preserved(self):
        # C0 controls are kept as-is (CR, LF, etc. are meaningful in T.61).
        self.assertEqual(decode_compound_text(b"A\r\nB"), "A\r\nB")


class TestDesignate(unittest.TestCase):
    def test_designate_t61_then_ascii(self):
        data = b"\x1b(CHello\x1b(BWorld"
        self.assertEqual(decode_compound_text(data), "HelloWorld")

    def test_designate_t61_alt_final(self):
        # ESC ( T is another way to designate the T.61 primary set.
        data = b"\x1b(THello"
        self.assertEqual(decode_compound_text(data), "Hello")

    def test_designate_g1_iso8859_1_gr(self):
        # ESC ) B designates ISO-8859-1 into G1; ESC ~ invokes G1 into GR.
        data = b"\x1b)B\x1b~" + bytes([0xC0])
        self.assertEqual(decode_compound_text(data), "\u00c0")

    def test_designate_g2_t61_ls2(self):
        # ESC * C designates T.61 into G2; ESC n (LS2) invokes G2 into GL.
        data = b"\x1b*C\x1bnAB"
        self.assertEqual(decode_compound_text(data), "AB")

    def test_designate_g2_iso8859_7_ls2r(self):
        # ESC * M designates ISO-8859-7 into G2; ESC } (LS2R) invokes G2 into GR.
        data = b"\x1b*M\x1b}" + bytes([0xD1])
        self.assertEqual(decode_compound_text(data), "\u03a1")

    def test_designate_gr_g2_iso8859_7(self):
        # ESC . M designates ISO-8859-7 into GR-G2 directly.
        data = b"\x1b.M" + bytes([0xD1])
        self.assertEqual(decode_compound_text(data), "\u03a1")

    def test_designate_gr_g3_iso8859_7_ls3r(self):
        # ESC / M designates ISO-8859-7 into GR-G3; ESC | (LS3R) invokes G3 into GR.
        data = b"\x1b/M\x1b|" + bytes([0xD1])
        self.assertEqual(decode_compound_text(data), "\u03a1")


class TestT61Graphics(unittest.TestCase):
    def test_t61_currency(self):
        # 0x24 in T.61 primary graphic = CURRENCY SIGN.
        data = b"\x1b(C$"
        self.assertEqual(decode_compound_text(data), "\u00a4")

    def test_t61_degree(self):
        # 0x5C in T.61 = DEGREE SIGN.
        data = b"\x1b(C\\"
        self.assertEqual(decode_compound_text(data), "\u00b0")

    def test_t61_a_grave(self):
        # 0x40 in T.61 = LATIN SMALL LETTER A WITH GRAVE.
        data = b"\x1b(C@"
        self.assertEqual(decode_compound_text(data), "\u00e0")


class TestMultibyteEscape(unittest.TestCase):
    def test_multibyte_escape_consumed_no_change(self):
        # ESC $ ) I is an ISO-2022-KR multibyte designation. We consume it but
        # do NOT change the active slots (single-byte only). The byte after
        # is decoded as ASCII.
        data = b"\x1b$)IA"
        self.assertEqual(decode_compound_text(data), "A")

    def test_multibyte_escape_no_intermediate(self):
        # ESC $ B (ISO-2022-JP). Consumed, no slot change.
        data = b"\x1b$BA"
        self.assertEqual(decode_compound_text(data), "A")


class TestUnmapped(unittest.TestCase):
    def test_unknown_final_byte(self):
        # ESC ( Z: Z is not a recognised final byte. The 3 bytes are consumed
        # and one replacement is emitted.
        data = b"\x1b(ZA"
        self.assertEqual(decode_compound_text(data), "\ufffdA")


class TestStreaming(unittest.TestCase):
    def test_chunked_designation_split(self):
        d = CompoundTextDecoder()
        part1 = d.decode(b"\x1b(")
        self.assertEqual(part1, "")
        part2 = d.decode(b"CHello")
        self.assertEqual(part2, "Hello")

    def test_chunked_at_escape_boundary(self):
        d = CompoundTextDecoder()
        self.assertEqual(d.decode(b"AB\x1b"), "AB")
        self.assertEqual(d.decode(b"(CHello"), "Hello")

    def test_final_drops_incomplete_escape(self):
        d = CompoundTextDecoder()
        self.assertEqual(d.decode(b"AB\x1b"), "AB")
        self.assertEqual(d.decode(b"", final=True), "\ufffd")

    def test_chunked_multibyte_escape(self):
        d = CompoundTextDecoder()
        self.assertEqual(d.decode(b"\x1b$"), "")
        self.assertEqual(d.decode(b")I"), "")
        self.assertEqual(d.decode(b"A"), "A")


class TestGetSetState(unittest.TestCase):
    def test_get_state_idle(self):
        d = CompoundTextDecoder()
        self.assertEqual(d.getstate(), (0, b""))

    def test_get_state_pending(self):
        d = CompoundTextDecoder()
        d.decode(b"\x1b(")
        self.assertEqual(d.getstate(), (1, b"\x1b("))

    def test_set_state_roundtrip(self):
        d = CompoundTextDecoder()
        d.decode(b"\x1b(")
        state = d.getstate()
        d2 = CompoundTextDecoder()
        d2.setstate(state)
        self.assertEqual(d2.decode(b"CA"), "A")

    def test_set_state_rejects_bad_flag(self):
        d = CompoundTextDecoder()
        with self.assertRaises(ValueError):
            d.setstate((2, b""))

    def test_set_state_rejects_bad_type(self):
        d = CompoundTextDecoder()
        with self.assertRaises(TypeError):
            d.setstate((1, "not bytes"))

    def test_set_state_rejects_oversized(self):
        d = CompoundTextDecoder()
        with self.assertRaises(ValueError):
            d.setstate((1, b"\x1b(Bxxxx"))


class TestReset(unittest.TestCase):
    def test_reset_returns_to_ascii(self):
        d = CompoundTextDecoder()
        d.decode(b"\x1b(C$")
        d.reset()
        self.assertEqual(d.decode(b"$"), "$")


if __name__ == "__main__":
    unittest.main()
