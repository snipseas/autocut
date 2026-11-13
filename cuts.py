"""Decide which parts of a raw recording to keep, from a word-level transcript.

Rules:
  1. Speech is grouped into phrases (words closer than PHRASE_GAP seconds).
  2. Retakes: when a run of >= MIN_REPEAT words is said again shortly after,
     the earlier (abandoned) attempt is dropped and the later take is kept.
  3. Filler-only phrases ("okay", "there we go", "um") are dropped.
  4. Silences longer than MAX_SILENCE are shortened to KEEP_SILENCE.

Output: list of (start, end) source ranges in seconds, plus a log of what was cut and why.
"""
import json
import re
import sys
from difflib import SequenceMatcher

PHRASE_GAP = 0.6       # s between words that still counts as one phrase
PAD_BEFORE = 0.15      # s kept before first word of a phrase
PAD_AFTER = 0.30       # s kept after last word
MAX_SILENCE = 0.8      # pauses longer than this get shortened
MIN_REPEAT = 4         # words that must repeat to count as a retake
RETAKE_WINDOW = 12.0   # s: how far back a retake can reach
MAX_ABANDONED = 14     # words: abandoned attempts are short
FILLERS = {"okay", "ok", "um", "uh", "uhm", "erm", "alright", "there we go", "okay there we go",
           "so", "yeah", "hmm", "no", "wait", "sorry", "oops", "hold on", "one sec", "one second"}


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def drop_retakes(words, log):
    """Remove abandoned attempts. Returns list of kept words."""
    keep = [True] * len(words)
    toks = [norm(w["word"]) for w in words]
    n = len(words)
    i = 0
    while i < n:
        # look for the same MIN_REPEAT-word run starting later at j
        best = None
        for j in range(i + 1, n):
            if words[j]["start"] - words[i]["start"] > RETAKE_WINDOW or j - i > MAX_ABANDONED:
                break
            # a finished sentence in between means it wasn't abandoned
            if any(re.search(r"[.?!]$", words[t]["word"]) and not words[t]["word"].endswith("...")
                   for t in range(i, j - 1)):
                break
            k = 0
            while j + k < n and i + k < j and toks[i + k] == toks[j + k] and toks[i + k]:
                k += 1
            if k >= MIN_REPEAT:
                best = j  # keep searching: later restarts win
        if best is not None:
            for t in range(i, best):
                keep[t] = False
            said = " ".join(w["word"] for w in words[i:best])
            log.append(("retake", words[i]["start"], words[best]["start"], said))
            i = best
        else:
            i += 1
    return [w for w, k in zip(words, keep) if k]


def phrases(words):
    out = []
    for w in words:
        if out and w["start"] - out[-1][-1]["end"] < PHRASE_GAP:
            out[-1].append(w)
        else:
            out.append([w])
    return out


HALLUCINATIONS = {"you", "thank you", "thanks for watching", "thank you for watching"}
RESTART_LOOKAHEAD = 2      # chunks after an attempt that may hold the retake
RESTART_WINDOW = 8.0       # s
ABANDONED_WINDOW = 20.0    # s: a cut-off phrase may be picked up again after a longer silent stretch
FILLER_WORDS = {"um", "umm", "uh", "uhm", "erm", "hmm", "mm", "ok", "okay", "so", "yeah", "no", "alright"}
JOINERS = {"and", "so", "then", "to", "the", "a", "now", "just", "you", "click", "go", "or", "but"}


def strip_hallucination(words):
    """Whisper invents 'You' / 'Thanks for watching!' after a cut-off word. Drop them.
    Also drops trailing words it gave ~0% confidence (echoes of script keywords)."""
    while len(words) > 1 and words[-1].get("p", 1) < 0.01:
        words = words[:-1]
    for n in (4, 3, 2, 1):
        tail = words[-n:]
        # invented words take no time; a real "...allows you" does
        if (len(words) > n and " ".join(norm(w["word"]) for w in tail) in HALLUCINATIONS
                and tail[-1]["end"] - tail[0]["start"] < 0.12):
            return words[:-n]
    return words


DASHES = ("-", "—", "–", "...")


def _alt_dash(chunk):
    """Index of the break-off dash in the primed hearing, if it sits at the very end
    (allowing a made-up word or two after it, like "- You" or "- space.")."""
    alt = chunk.get("alt_words") or []
    for k in range(len(alt) - 1, max(-1, len(alt) - 5), -1):
        if alt[k]["word"].strip().endswith(DASHES):
            after = alt[k + 1:]
            if not after or after[-1]["end"] - after[0]["start"] < 0.3:
                return k
    return None


def chunk_tokens(chunk):
    """Words of a chunk (no bare dashes), hallucinated tail removed.

    The plain hearing gives the words, but it tidies away a false start at the end
    ("...menu. It allows you-"). The primed hearing keeps it, so add what it heard after
    the plain hearing's last word."""
    words = [w for w in chunk["words"] if norm(w["word"])]
    alt = chunk.get("alt_words")
    if alt is not None and words:      # nothing heard plainly = nothing was said
        k = _alt_dash(chunk)
        last_end = words[-1]["end"] if words else chunk["start"] - 0.2
        if k is not None:
            # where does the plain hearing's last word sit in the primed one?
            pos = None
            if words:
                last = norm(words[-1]["word"])
                for idx in range(k - 1, -1, -1):
                    a = norm(alt[idx]["word"])
                    if a and (a == last or SequenceMatcher(None, a, last).ratio() >= 0.8):
                        pos = idx
                        break
            if pos is not None:
                extra = [w for w in alt[pos + 1:k] if norm(w["word"])]
                if extra:   # the plain hearing stretched its last word over the false start
                    words[-1] = dict(words[-1], end=min(words[-1]["end"], alt[pos]["end"]))
            else:
                extra = [w for w in alt[:k] if norm(w["word"]) and w["start"] >= last_end - 0.05
                         and w["end"] - w["start"] >= 0.08]
            words = words + extra
    return strip_hallucination(words)


def same(a, b, last=False):
    a, b = norm(a), norm(b)
    if a == b or (last and len(a) >= 2 and b.startswith(a)):        # "remo-" ~ "remove"
        return True
    return min(len(a), len(b)) >= 2 and SequenceMatcher(None, a, b).ratio() >= 0.75   # "no" ~ "now"


def cut_off(chunk):
    """Whisper marks a sentence you abandoned with a trailing '-' or '...'."""
    if "cut_off" in chunk:
        return chunk["cut_off"] or _alt_dash(chunk) is not None
    ws = strip_hallucination([w for w in chunk["words"] if w["word"].strip()])
    return bool(ws) and ws[-1]["word"].rstrip().endswith(("-", "...", "—"))


def text_of(ws):
    return " ".join(norm(w) for w in ws)


def restart_point(cur, nxt, abandoned, adjacent):
    """Index in cur where an attempt begins that nxt says again (and completes), else None.

    Matches when the rest of cur is said again near the start of nxt
    ("there are two way-" -> "there are two ways to reset it", also "where should you upload-"
    -> "now, where should you upload this"), or, when cur was cut off, when nxt starts with
    the same 2+ words ("and remove the wo-" -> "and remove all the files").
    """
    nt = [w["word"] for w in nxt]
    for p in range(len(cur)):
        tail = [w["word"] for w in cur[p:]]
        for q in range(0, min(3, len(nt))):          # the retake may start with "now," / "so,"
            k = 0
            while k < len(tail) and q + k < len(nt) and same(tail[k], nt[q + k], k == len(tail) - 1):
                k += 1
            if k == len(tail) and (k >= 2 or (adjacent and abandoned and len(norm(tail[-1])) < len(norm(nt[q])))):
                return p
            # cut off, and a later phrase starts the same way: 2+ words if next, 3+ if one phrase later
            if abandoned and q == 0 and k >= (2 if adjacent else 3):
                return p
    # reworded restart of the last sentence ("...menu. It allows you -" -> "It lets you spawn vehicles")
    if adjacent and abandoned:
        for p in range(len(cur) - 1):
            if p and not re.search(r"[.?!]$", cur[p - 1]["word"].strip()):
                continue                              # only from the start of a sentence
            tail = [w["word"] for w in cur[p:]]
            if (len(tail) <= 6 and norm(tail[0]) == norm(nt[0])
                    and SequenceMatcher(None, text_of(tail), text_of(nt[:len(tail)])).ratio() >= 0.6):
                return p
    # short false start picked up again with a word added ("and add-" -> "and then add a double quotation")
    if adjacent and abandoned and 2 <= len(cur) <= 3:
        it = iter(norm(w) for w in nt[:len(cur) + 3])
        if all(any(norm(w["word"]) == x for x in it) for w in cur):
            return 0
    # garbled attempt right before the retake ("already I have a worthy p-" -> "do you already have a world")
    tail = [w["word"] for w in cur]
    if adjacent and abandoned and len(tail) >= 3:
        for q in range(0, min(3, len(nt))):
            if SequenceMatcher(None, text_of(tail), text_of(nt[q:q + len(tail)])).ratio() >= 0.6:
                return 0
    return None


def compute_chunks(transcript):
    """Cut list from pause-separated speech chunks: drop abandoned attempts, keep the complete retake."""
    log = []
    duration = transcript["duration"]
    chunks = [dict(c, toks=chunk_tokens(c)) for c in transcript["chunks"]]
    pieces = []
    skip = set()
    for i, c in enumerate(chunks):
        if i in skip:
            log.append(("retake", c["start"], c["end"], f'"{c["text"]}" -> rest of an abandoned attempt'))
            continue
        toks = c["toks"]
        text = " ".join(norm(w["word"]) for w in toks).strip()
        # spelled-out letters ("C F G") and "dot" are quick to say; don't count them
        rate = sum(1 for w in toks if len(norm(w["word"])) > 1 and norm(w["word"]) != "dot") / max(0.1, c["end"] - c["start"])
        lone_invention = text in HALLUCINATIONS and (c["no_speech"] > 0.3 or i < len(chunks) - 1)
        if not text or lone_invention or (rate > 8 and len(toks) >= 6):
            # nothing said, a stray "Thank you." Whisper hears in a click, or it echoing its prompt
            log.append(("noise", c["start"], c["end"], c["text"]))
            continue
        if text in FILLERS or all(norm(w["word"]) in FILLER_WORDS for w in toks):
            log.append(("filler", c["start"], c["end"], c["text"]))
            continue
        end = c["end"]
        for n, nxt in enumerate(chunks[i + 1:i + 1 + RESTART_LOOKAHEAD]):
            window = ABANDONED_WINDOW if (cut_off(c) and n == 0) else RESTART_WINDOW
            if nxt["start"] - c["end"] > window or not nxt["toks"]:
                break
            p = restart_point(toks, nxt["toks"], cut_off(c), n == 0)
            if p is not None:
                skip.update(range(i + 1, i + 1 + n))   # chunks between attempt and retake
                said = " ".join(w["word"] for w in toks[p:])
                if p == 0:
                    log.append(("retake", c["start"], c["end"], f'"{said}" -> said again'))
                    end = None
                else:
                    end = toks[p]["start"] - 0.05
                    log.append(("retake", end, c["end"], f'"{said}" -> said again'))
                break
        if end is None:
            continue
        # retakes said without a pause, inside this chunk
        inner_log = []
        kept = drop_retakes(toks, inner_log) if len(toks) > MIN_REPEAT else toks
        log += inner_log
        s = c["start"]
        if len(kept) < len(toks):
            dropped = [w for w in toks if w not in kept]
            first_bad = dropped[0]
            nxt_kept = next((w for w in kept if w["start"] >= dropped[-1]["end"]), None)
            if first_bad["start"] - s > 0.3:
                pieces.append([max(0.0, s - PAD_BEFORE), first_bad["start"] - 0.05, None])
            s = nxt_kept["start"] - 0.05 if nxt_kept else end
        if end - s > 0.15:
            # no padding after a mid-chunk cut, or the start of the abandoned attempt leaks in
            pad = PAD_AFTER if end == c["end"] else 0.0
            lo = c["start"] - 0.3 if s == c["start"] else s - 0.06   # words can start a hair before the phrase
            inside = [w for w in kept if lo <= w["start"] < end]
            meta = {"first": inside[0], "last": inside[-1], "cut": cut_off(c) or end != c["end"]} if inside else None
            pieces.append([max(0.0, s - PAD_BEFORE), min(duration, end + pad), meta])
    pieces.sort(key=lambda p: p[0])
    # "...the three dots and-" + "And click Visit..." : don't say the joining word twice
    for a, b in zip(pieces, pieces[1:]):
        if a[2] and b[2] and len(norm(a[2]["last"]["word"])) and \
                norm(a[2]["last"]["word"]) == norm(b[2]["first"]["word"]) and \
                (a[2]["cut"] or norm(a[2]["last"]["word"]) in JOINERS):
            log.append(("stutter", a[2]["last"]["start"], a[1], f'"{a[2]["last"]["word"]}" said twice across a pause'))
            a[1] = a[2]["last"]["start"] - 0.03
    pieces = [p[:2] for p in pieces if p[1] - p[0] > 0.2]
    merged = []
    for s, e in pieces:
        if merged and s - merged[-1][1] <= MAX_SILENCE:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged, log


def compute(transcript, duration=None):
    if transcript.get("chunks"):
        return compute_chunks(transcript)
    words = [w for w in transcript["words"] if norm(w["word"])]
    duration = duration or transcript["duration"]
    log = []
    words = drop_retakes(words, log)
    ranges = []
    for ph in phrases(words):
        text = " ".join(norm(w["word"]) for w in ph).strip()
        if text in FILLERS:
            log.append(("filler", ph[0]["start"], ph[-1]["end"], text))
            continue
        s = max(0.0, ph[0]["start"] - PAD_BEFORE)
        e = min(duration, ph[-1]["end"] + PAD_AFTER)
        ranges.append([s, e])
    # cut real (loudness-based) silences that fall inside a phrase
    for qs, qe in transcript.get("silences", []):
        a, b = qs + PAD_AFTER, qe - PAD_BEFORE   # leave breathing room either side
        if b - a < MAX_SILENCE - PAD_AFTER - PAD_BEFORE:
            continue
        nxt = []
        for s, e in ranges:
            if a < e and b > s:
                if a > s:
                    nxt.append([s, a])
                if b < e:
                    nxt.append([b, e])
                log.append(("pause", a, b, f"{b - a:.1f}s mid-phrase"))
            else:
                nxt.append([s, e])
        ranges = nxt
    ranges = [r for r in ranges if r[1] - r[0] > 0.3]
    # merge ranges separated by short silences; long silences get cut
    merged = []
    for s, e in ranges:
        if merged and s - merged[-1][1] <= MAX_SILENCE:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            if merged:
                log.append(("silence", merged[-1][1], s, f"{s - merged[-1][1]:.1f}s"))
            merged.append([s, e])
    return merged, log


def total(r):
    return sum(e - s for s, e in r)


def overlap(a, b):
    o = 0.0
    for s1, e1 in a:
        for s2, e2 in b:
            o += max(0.0, min(e1, e2) - max(s1, s2))
    return o


def compare(auto, manual):
    """How close the automatic keep-list is to a hand edit (both in source seconds)."""
    o = overlap(auto, manual)
    return {
        "auto_kept": round(total(auto), 1),
        "manual_kept": round(total(manual), 1),
        "agree": round(o, 1),
        "precision": round(o / total(auto), 3) if auto else 0,   # of what I kept, how much you kept
        "recall": round(o / total(manual), 3) if manual else 0,  # of what you kept, how much I kept
    }


if __name__ == "__main__":
    t = json.load(open(sys.argv[1]))
    r, log = compute(t)
    for kind, s, e, info in log:
        print(f"CUT {kind:8s} {s:7.2f}-{e:7.2f}  {info}")
    for s, e in r:
        print(f"KEEP {s:7.2f}-{e:7.2f}")
    print(f"kept {total(r):.1f}s of {t['duration']:.1f}s")
