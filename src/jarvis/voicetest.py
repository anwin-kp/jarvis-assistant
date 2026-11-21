"""Guided voice test: record 20 phrases in your own voice, to compare speech-recognition setups.

  .\\jarvis.ps1 voice-test          (stop JARVIS first, so it doesn't answer the phrases)

For each phrase: read it on screen, wait for the beep, say it, stop talking - it saves automatically.
Recordings go to data/voice-test/ (private, never committed). Then `python -m jarvis.voicetest --compare`."""

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

from jarvis.core.config import DATA

RATE, FRAME = 16000, 480  # 30 ms
OUT = DATA / "voice-test"
PHRASES = [
    "Hey Jarvis, what's the weather in Goa?",
    "Play my Mix-Up Mash playlist.",
    "Stop.",
    "Pause the music.",
    "Next song.",
    "Set the volume to thirty percent.",
    "Open the Calculator app.",
    "What do I do for work?",
    "Remind me to call Priya at six thirty.",
    "Search the web for Flutter Riverpod tutorials.",
    "Tell me about the Siemens project in Goa.",
    "What's the time in Kochi?",
    "Turn on dark mode.",
    "Read my unread emails.",
    "Jarvis, go to sleep.",
    "How far is Thrissur from Bangalore?",
    "Open YouTube and play lofi music.",
    "Take a screenshot.",
    "What's on my calendar tomorrow?",
    "Cancel.",
]


def beep() -> None:
    import winsound

    winsound.Beep(880, 160)
    time.sleep(0.15)  # Bluetooth speakers lag: don't record our own beep


def record(threshold: float, max_s: float = 7.0, silence_s: float = 0.9) -> np.ndarray:
    """The mic opens BEFORE the beep (opening it takes a moment, which cut first words), then records until you
    stop talking (or max_s)."""
    import sounddevice as sd

    frames, voiced, quiet, started = [], 0, 0, False
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
        time.sleep(0.25)  # let the device start
        beep()
        t0 = time.time()
        while time.time() - t0 < max_s:
            block, _ = stream.read(FRAME)
            frames.append(block.copy())
            rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768) ** 2)))
            if rms > threshold:
                voiced, quiet, started = voiced + 1, 0, started or voiced >= 3
            else:
                quiet += 1
            if started and quiet * FRAME / RATE > silence_s:
                break
            if not started and time.time() - t0 > 5:  # nothing said
                break
    return np.concatenate(frames)[:, 0]


def calibrate() -> float:
    import sounddevice as sd

    print("  Measuring background noise for 1 second - stay quiet...")
    audio = sd.rec(RATE, samplerate=RATE, channels=1, dtype="int16")
    sd.wait()
    noise = float(np.sqrt(np.mean((audio.astype(np.float32) / 32768) ** 2)))
    return min(0.03, max(0.004, noise * 3))


def save(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())


def run() -> None:
    import msvcrt

    OUT.mkdir(parents=True, exist_ok=True)
    print("\n  JARVIS VOICE TEST - 20 short phrases, about 3 minutes")
    print("  Read the phrase, wait for the BEEP, say it naturally. It saves when you stop talking.")
    print("  Keys between phrases: Enter = next, R = redo the last one, Q = quit\n")
    threshold = calibrate()
    i = 0
    while i < len(PHRASES):
        print(f"\n  [{i + 1}/{len(PHRASES)}]  \"{PHRASES[i]}\"")
        time.sleep(0.8)
        audio = record(threshold)
        secs = len(audio) / RATE
        save(OUT / f"{i + 1:02d}.wav", audio)
        print(f"        saved ({secs:.1f}s).  Enter = next, R = redo, Q = quit")
        key = msvcrt.getwch().lower()
        if key == "q":
            break
        if key != "r":
            i += 1
    (OUT / "phrases.json").write_text(json.dumps(PHRASES[:i] if i < len(PHRASES) else PHRASES, indent=1),
                                      encoding="utf-8")
    print(f"\n  Done: {i} phrases saved in {OUT}. Tell Claude \"recorded\".\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true")
    if ap.parse_args().compare:
        from jarvis.voicecompare import compare

        compare()
    else:
        run()


if __name__ == "__main__":
    sys.exit(main())
