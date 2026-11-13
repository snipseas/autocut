"""Transcribe a recording for auto-cutting.

The audio is split at real pauses (by loudness), and every stretch of speech is heard twice:
  * plain  -> the words and their timing (most accurate; no hints, so nothing gets invented)
  * primed -> with a "write stutters down" hint and the script's keywords. Only used to tell
              whether the phrase was cut off mid-sentence, and to spot keywords for images.
Splitting at pauses keeps an abandoned attempt ("and remo-") from being merged into its retake.

Usage: python transcribe.py <video> <out.json> [--model small.en] [--device cpu|cuda] [--vocab "a, b"]
"""
import argparse
import json
import subprocess

import numpy as np
from faster_whisper import WhisperModel

SR = 16000
FORMAT = 2   # bump when the JSON layout changes (autocut keys its cache on this)
# Showing Whisper disfluent text makes it mark cut-off words ("wa-") instead of tidying them.
VERBATIM_PROMPT = "Umm, so, uh, there are two wa- two ways to, um, re- reset it. I- I mean, you can remo- remove the, the files."


def load_model(name, device):
    if device == "cuda":
        return WhisperModel(name, device="cuda", compute_type="int8_float16")
    return WhisperModel(name, device="cpu", compute_type="int8")


def find_silences(audio, hop=0.02, min_len=0.25):
    """Unvoiced stretches from loudness, since Whisper stretches words over pauses."""
    n = int(SR * hop)
    frames = audio[: len(audio) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    floor = np.percentile(db, 10)
    peak = np.percentile(db, 98)
    thr = max(floor + 10, peak - 35)
    voiced = db > thr
    out, start = [], None
    for i, v in enumerate(list(voiced) + [True]):
        if not v and start is None:
            start = i
        elif v and start is not None:
            if (i - start) * hop >= min_len:
                out.append([round(start * hop, 3), round(i * hop, 3)])
            start = None
    return out


def voiced_chunks(silences, duration, min_gap=0.3, min_len=0.15):
    """Speech stretches between pauses of at least min_gap seconds."""
    chunks, pos = [], 0.0
    for s, e in silences:
        if e - s >= min_gap:
            if s - pos >= min_len:
                chunks.append([pos, s])
            pos = e
    if duration - pos >= min_len:
        chunks.append([pos, duration])
    return chunks


def strip_invented_tail(words):
    """Whisper tacks made-up text onto the end of a phrase ("Thank you for watching!",
    "Microsoft Office Word Document ...") with all the words crammed into a few milliseconds.
    Real speech can't be that fast, so drop a tail of 3+ near-zero-length words."""
    i = len(words)
    while i > 0 and words[i - 1]["end"] - words[i - 1]["start"] < 0.2:
        span = words[-1]["end"] - words[i - 1]["start"]
        if (len(words) - i + 1) / max(span, 1e-3) <= 12:
            break
        i -= 1
    if len(words) - i < 3:
        return words
    # the "-" Whisper writes when you break off is real information: keep it
    if words[i]["word"].strip() in ("-", "—", "–", "..."):
        i += 1
    return words[:i]


def hear(model, audio, offset, **opts):
    segs, _ = model.transcribe(audio, word_timestamps=True, condition_on_previous_text=False, **opts)
    segs = list(segs)
    words = [{"start": round(w.start + offset, 3), "end": round(w.end + offset, 3),
              "word": w.word.strip(), "p": round(w.probability, 3)}
             for s in segs for w in (s.words or []) if w.word.strip()]
    no_speech = max((s.no_speech_prob for s in segs), default=1.0)
    return strip_invented_tail(words), round(no_speech, 3)


def ends_cut_off(words):
    return bool(words) and words[-1]["word"].rstrip().endswith(("-", "...", "—", "–"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--model", default="small.en")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--vocab", default="", help="comma-separated terms Whisper should spell right (script keywords)")
    a = ap.parse_args()

    # Decode with ffmpeg ourselves (avoids PyAV version issues).
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", a.video, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    duration = len(audio) / SR
    model = load_model(a.model, a.device)
    primed = dict(initial_prompt=VERBATIM_PROMPT + (f" Terms: {a.vocab}." if a.vocab else ""),
                  hotwords=a.vocab or None)

    silences = find_silences(audio)
    chunks = []
    for s, e in voiced_chunks(silences, duration):
        a0, a1 = max(0.0, s - 0.1), min(duration, e + 0.1)
        clip = audio[int(a0 * SR):int(a1 * SR)]
        words, no_speech = hear(model, clip, a0)
        alt, _ = hear(model, clip, a0, **primed)
        chunks.append({"start": round(s, 3), "end": round(e, 3),
                       "text": " ".join(w["word"] for w in words), "words": words,
                       "cut_off": ends_cut_off(words) or ends_cut_off(alt),
                       "alt_text": " ".join(w["word"] for w in alt), "alt_words": alt,
                       "no_speech": no_speech})

    json.dump({"format": FORMAT, "duration": duration, "silences": silences, "chunks": chunks,
               "words": [w for c in chunks for w in c["words"]]}, open(a.out, "w"), indent=1)
    print(f"{len(chunks)} phrases, {sum(len(c['words']) for c in chunks)} words, {duration:.1f}s")


if __name__ == "__main__":
    main()
