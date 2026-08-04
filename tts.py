"""
tts.py — shared edge-tts helper.

Pulled out of app.py so the daily-briefing scheduler (daily_briefing.py) can
generate spoken audio the same way the live command flow does, without
importing app.py itself (which would be circular: app.py starts the
scheduler thread).
"""

import base64
import logging

import edge_tts

logger = logging.getLogger("jarvis.tts")

VOICE = "he-IL-AvriNeural"  # run `edge-tts --list-voices` to see others (he-IL-HilaNeural is the female alternative)

# A few percent slower than the model's default pace reads as measured and
# formal (the "butler" cadence the en-GB-RyanNeural persona had) rather than
# the clipped, slightly rushed default. Deliberately NOT touching pitch —
# shifting pitch is a DSP transform bolted onto the synthesized audio rather
# than a genuinely different voice, and pushing it tends to make neural TTS
# sound *more* robotic/artificial, the opposite of what we want here.
RATE = "-8%"


async def generate_tts_base64(text: str, voice: str = VOICE, rate: str = RATE):
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        audio_bytes = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
        return base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None
