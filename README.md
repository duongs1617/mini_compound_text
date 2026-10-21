# mini_compound_text

Decodes ISO 2022 compound text streams used in T.61 and X.400 messaging into Unicode strings. Single-byte character sets only; multibyte escapes (e.g. ISO-2022-JP) are consumed but ignored.

## Usage

```python
from mini_compound_text import CompoundTextDecoder, decode_compound_text

# One-shot
text = decode_compound_text(b"\x1b(CHello\x1b(BWorld")
assert text == "HelloWorld"

# Streaming (state preserved across chunks)
d = CompoundTextDecoder()
part1 = d.decode(b"\x1b(")          # -> ""
part2 = d.decode(b"CHello")          # -> "Hello"
```

Exported names: `CompoundTextDecoder` (class) and `decode_compound_text` (function).

## Why

T.61 and X.400 messaging systems used ISO 2022 compound text to mix character sets (ASCII, T.61 primary, ISO-8859-1, ISO-8859-7 Greek) within a single stream. Python's standard library has no codec for this. This library exists to decode such streams without third-party dependencies.

The trade-off: I only handle single-byte character sets. Multibyte escapes (ESC $ ...) are consumed to keep the stream from desynchronizing, but they don't change the active slots. This covers the common T.61 terminal cases; full ISO-2022-JP/-KR support would require a much larger table and a different decoding model.

## Edge cases

- **Incomplete escapes across chunks**: an ESC at the end of a chunk is buffered internally until more data arrives. Call `decode(data, final=True)` at end-of-stream to flush; any pending incomplete escape becomes one U+FFFD.
- **Unrecognised final bytes**: `ESC ( Z` (Z is not a known designation final) consumes three bytes and emits one U+FFFD, so the stream stays aligned.
- **C0 control characters** (0x00-0x1F) are preserved as-is because real T.61 streams carry CR, LF, etc.
- **State inspection**: `getstate()` / `setstate()` let you save and restore decoder state. The pending-bytes buffer is capped at 3 bytes (longest supported escape); `setstate` rejects anything larger.
