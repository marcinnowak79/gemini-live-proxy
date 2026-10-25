"""Binary protocol between ESP32 and Proxy.

ESP32 → Proxy:
  0x01 + PCM bytes   = audio chunk from microphone
  0x02                = end of speech (audio_stream_end)
  0x03                = barge-in (interrupt playback)

Proxy → ESP32:
  0x01 + PCM bytes   = audio chunk to play (stream immediately)
  0x02                = end of response
  0x03 + JSON bytes   = status message (timer, error, etc.)
  0x04                = state: listening (LED)
  0x05                = state: thinking (LED)
  0x06 + URL/path     = response audio is ready on HTTP stream
"""

# Message types: ESP32 → Proxy
MSG_AUDIO_IN = 0x01
MSG_AUDIO_END = 0x02
MSG_BARGE_IN = 0x03
# Diagnostics only: XMOS channel 1 (no AGC) matching the last MSG_AUDIO_IN chunk,
# and the XMOS voice-to-noise ratio as one byte (0-100).
MSG_AUDIO_AUX = 0x04
MSG_VNR = 0x05

# Message types: Proxy → ESP32
MSG_AUDIO_OUT = 0x01
MSG_RESPONSE_END = 0x02
MSG_STATUS = 0x03
MSG_STATE_LISTENING = 0x04
MSG_STATE_THINKING = 0x05
MSG_RESPONSE_START = 0x06


def pack_message(msg_type: int, data: bytes = b"") -> bytes:
    """Pack a protocol message."""
    return bytes([msg_type]) + data


def unpack_message(raw: bytes) -> tuple[int, bytes]:
    """Unpack a protocol message. Returns (msg_type, data)."""
    if not raw:
        return 0, b""
    return raw[0], raw[1:]
