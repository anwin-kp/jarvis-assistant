"""Compare speech-recognition setups on the user's own recordings (data/voice-test, from jarvis.voicetest).
Prints word error rate and how many phrases came out exactly right, per setup."""

import json
import re
import time
import wave

import numpy as np

from jarvis.core.config import DATA, load_config

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
       "seventeen eighteen nineteen".split()
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def num_words(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    return str(n)


def norm(text: str) -> list[str]:
    """Compare words, not formatting: "30%" == "thirty percent", "6:30" == "six thirty", "Mix-Up" == "mix up"."""
    t = text.lower().replace("%", " percent")
    t = re.sub(r"(\d+):(\d+)", lambda m: f"{num_words(int(m[1]))} {num_words(int(m[2]))}", t)
    t = re.sub(r"\d+", lambda m: num_words(int(m[0])), t)
    t = re.sub(r"[^\w\s']", " ", t).replace("'", "")
    return t.split()


def wer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def load(path) -> np.ndarray:
    with wave.open(str(path)) as w:
        # the engines take float32 in [-1, 1] (like the live audio engine), not raw int16
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def personal_vocab(base: str) -> str:
    from jarvis.memory.store import Memory

    words = []
    for f in Memory(DATA / "jarvis.db").list_facts():
        body = re.sub(r"^\[\w+\]\s*", "", f["fact"])
        words += re.findall(r"\b[A-Z][\w'-]+(?:\s[A-Z][\w'-]+)*", body)
    extra = ", ".join(w for w in dict.fromkeys(words) if len(w) > 2 and w.lower() not in base.lower())
    return (base + ". " + extra)[:700]


def compare() -> None:
    from jarvis.core.config import get_secret
    from jarvis.stt.groq_stt import GroqSTT

    folder = DATA / "voice-test"
    phrases = json.loads((folder / "phrases.json").read_text(encoding="utf-8"))
    clips = [load(folder / f"{i + 1:02d}.wav") for i in range(len(phrases))]
    vcfg = load_config().get("voice", {})
    base, langs = vcfg.get("vocabulary") or "", vcfg.get("languages")
    full = personal_vocab(base)
    key = get_secret("groq_api_key")
    setups = []
    if key:
        setups += [("Groq turbo (current)", GroqSTT(key, "whisper-large-v3-turbo", base, langs, timeout=15)),
                   ("Groq turbo + your names", GroqSTT(key, "whisper-large-v3-turbo", full, langs, timeout=15)),
                   ("Groq large-v3 + your names", GroqSTT(key, "whisper-large-v3", full, langs, timeout=15))]
    from jarvis.stt.whisper_stt import WhisperSTT

    for model in ("small", "large-v3-turbo"):
        try:
            setups.append((f"Local {model} + your names",
                           WhisperSTT(model, "cuda", "float16", download_root=str(DATA / "models"), vocabulary=full,
                                      languages=langs)))
        except Exception as e:
            print(f"(skipping local {model}: {e})")

    results = {}
    for name, stt in setups:
        rows, t_total = [], 0.0
        for ref, audio in zip(phrases, clips, strict=True):
            hyp = ""
            for attempt in range(3):
                try:
                    t = time.perf_counter()
                    hyp, _ = stt.transcribe(audio)
                    t_total += time.perf_counter() - t
                    break
                except Exception as e:  # Groq free tier: 20 requests/minute
                    if attempt == 2:
                        hyp = f"<error {type(e).__name__}>"
                    time.sleep(20)
            rows.append((ref, hyp, wer(ref, hyp)))
            print(f"  [{name}] {len(rows):2d}/{len(phrases)} said: {ref[:40]!r} heard: {hyp[:40]!r}", flush=True)
            if "Groq" in name:
                time.sleep(3.2)
        results[name] = rows
        (folder / "comparison.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
        exact = sum(r[2] == 0 for r in rows)
        print(f"{name:30s} WER {100 * np.mean([r[2] for r in rows]):5.1f}%  exact {exact:2d}/{len(rows)}  "
              f"avg {t_total / len(rows):.2f}s", flush=True)
    out = folder / "comparison.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"details: {out}")


if __name__ == "__main__":
    compare()
