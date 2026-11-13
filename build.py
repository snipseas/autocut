"""Build a Kdenlive project from a raw recording, using one of your finished projects as the style template.

    python build.py RAW.mp4 TRANSCRIPT.json TEMPLATE.kdenlive OUT.kdenlive [--zoom] [--dissolve-gap 2.0]

The template supplies everything except the cuts: music bed + level, whoosh, outro clip,
fade from black, track layout. Only the dialogue/video tracks are rebuilt from the cut list.
"""
import argparse
import copy
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cuts import compute  # noqa: E402

HALF_MIX = 30       # frames each side of a dissolve (your template: 60-frame luma, centred)
OUTRO_MIX = 60      # frames the outro overlaps the last clip


# ---------- small helpers ----------
def tc_to_frames(s, fps):
    h, m, sec = s.split(":")
    return round((int(h) * 3600 + int(m) * 60 + float(sec)) * fps)


def frames_to_tc(f, fps):
    s = f / fps
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"


def prop(el, name, value=None):
    for p in el.findall("property"):
        if p.get("name") == name:
            if value is not None:
                p.text = str(value)
            return p.text
    if value is not None:
        p = ET.SubElement(el, "property", name=name)
        p.text = str(value)
        return p.text
    return None


def kdenlive_hash(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= 2_000_000:
            return hashlib.md5(f.read()).hexdigest()
        a = f.read(1_000_000)
        f.seek(size - 1_000_000)
        return hashlib.md5(a + f.read()).hexdigest()


def probe_frames(path, fps):
    d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, check=True).stdout)
    return int(d * fps)


# ---------- template analysis ----------
class Template:
    def __init__(self, path):
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        pr = self.root.find("profile")
        self.fps = int(pr.get("frame_rate_num")) / int(pr.get("frame_rate_den"))
        self.by_id = {el.get("id"): el for el in self.root if el.get("id")}
        self.seq = next(t for t in self.root.findall("tractor") if prop(t, "kdenlive:uuid"))
        tracks = [t.get("producer") for t in self.seq.findall("track")]
        self.tracks = tracks[1:]                         # drop the black background track
        res = {el.get("id"): prop(el, "resource") for el in self.root if el.tag in ("chain", "producer")}
        obs = [r for r in res.values() if r and "/OBS/" in r]
        self.raw_res = max(set(obs), key=obs.count) if obs else self._most_cut_clip(res)
        self.raw_ids = {i for i, r in res.items() if r == self.raw_res}
        self.res = res
        self.dialogue = self.video = None
        others = []
        for idx, tid in enumerate(self.tracks):
            tr = self.by_id[tid]
            pls = [self.by_id[t.get("producer")] for t in tr.findall("track")]
            audio = tr.findall("track")[0].get("hide") == "video"
            uses_raw = any(e.get("producer") in self.raw_ids for pl in pls for e in pl.findall("entry"))
            info = {"idx": idx, "tractor": tr, "pls": pls, "audio": audio}
            if uses_raw and audio:
                self.dialogue = info
            elif uses_raw:
                self.video = info
            elif audio and any(pl.findall("entry") for pl in pls):
                others.append(info)
        # music = longest audio clip; whoosh = the other audio track
        def longest(info):
            return max((tc_to_frames(e.get("out"), self.fps) - tc_to_frames(e.get("in"), self.fps)
                        for pl in info["pls"] for e in pl.findall("entry")), default=0)
        # a long clip is the music bed, a short one is the whoosh (some brands have no music)
        others.sort(key=longest, reverse=True)
        self.music = next((o for o in others if longest(o) > 10 * self.fps), None)
        self.whoosh = next((o for o in others if longest(o) <= 3 * self.fps), None)
        # an empty video track above everything, for keyword images
        self.overlay = None
        for idx, tid in enumerate(self.tracks):
            tr = self.by_id[tid]
            pls = [self.by_id[x.get("producer")] for x in tr.findall("track")]
            if tr.findall("track")[0].get("hide") == "audio" and not any(pl.findall("entry") for pl in pls):
                self.overlay = {"idx": idx, "tractor": tr, "pls": pls}
        if self.dialogue is None or self.video is None:
            sys.exit(f"build.py: {path} can't be used as a template: the recording ({self.raw_res}) "
                     "has to be on one video track and one audio track of its timeline")
        # outro = last non-raw entry on the video track
        self.outro_v = self._last_nonraw(self.video)
        self.outro_a = self._last_nonraw(self.dialogue)
        # fade-from-black filter on the first video clip
        first = next((e for pl in self.video["pls"] for e in pl.findall("entry")
                      if e.get("producer") in self.raw_ids), None)
        self.intro_filters = [f for f in first.findall("filter")] if first is not None else []

    def _most_cut_clip(self, res):
        """Which file is the screen recording, when nothing comes from an OBS folder:
        the one cut into the most pieces on the timeline, with both its picture and its sound used."""
        pieces = {}
        for tid in self.tracks:
            subs = self.by_id[tid].findall("track")
            audio = subs[0].get("hide") == "video"
            for sub in subs:
                for e in self.by_id[sub.get("producer")].findall("entry"):
                    r = res.get(e.get("producer"))
                    if r:
                        pieces.setdefault(r, [0, 0])[audio] += 1
        both = {r: v + a for r, (v, a) in pieces.items() if v and a}
        if not both:
            sys.exit("build.py: can't tell which clip in the template is the screen recording "
                     "(it has to be on one video track and one audio track)")
        return max(sorted(both), key=both.get)

    def _last_nonraw(self, info):
        for pl in info["pls"]:
            for e in pl.findall("entry"):
                if e.get("producer") not in self.raw_ids:
                    return e
        return None


# ---------- building ----------
def chain_for(t, pl, want_ids, fallback_id):
    """Find (or clone) a chain of the wanted media to use in this playlist (Kdenlive keeps one per track)."""
    for e in pl.findall("entry"):
        if e.get("producer") in want_ids:
            return e.get("producer")
    src = t.by_id[fallback_id]
    new = copy.deepcopy(src)
    new.set("id", f"{src.get('id')}_{pl.get('id')}")
    kids = list(t.root)
    t.root.insert(kids.index(pl), new)
    t.by_id[new.get("id")] = new
    return new.get("id")


def clear(pl):
    for el in list(pl):
        if el.tag in ("entry", "blank"):
            pl.remove(el)


def fill(pl, items, fps):
    """items: list of (timeline_start, entry_element) sorted; inserts blanks between."""
    pos = 0
    for start, entry in items:
        if start > pos:
            ET.SubElement(pl, "blank", length=frames_to_tc(start - pos, fps))
        elif start < pos:
            raise ValueError(f"overlap in {pl.get('id')} at {start} < {pos}")
        pl.append(entry)
        pos = start + tc_to_frames(entry.get("out"), fps) - tc_to_frames(entry.get("in"), fps) + 1
    return pos


def make_entry(producer, src_in, src_out, kid, fps):
    e = ET.Element("entry", {"in": frames_to_tc(src_in, fps), "out": frames_to_tc(src_out, fps), "producer": producer})
    prop(e, "kdenlive:id", kid)
    return e


def zoom_filter(entry, z, length, fps, ramp=24):
    """Kdenlive 'Transform' effect: ease into a zoom on the action, hold, ease back out."""
    W, H = 1920, 1080
    s = z["scale"]
    w, h = W * s, H * s
    x = min(0, max(W - w, W / 2 - z["cx"] * w))
    y = min(0, max(H - h, H / 2 - z["cy"] * h))
    full = f"0 0 {W} {H} 1"
    zoomed = f"{x:.0f} {y:.0f} {w:.0f} {h:.0f} 1"
    kf = [(0, full), (ramp, zoomed), (length - 1 - ramp, zoomed), (length - 1, full)]
    f = ET.SubElement(entry, "filter", {"in": entry.get("in"), "out": entry.get("out")})
    for k, v in [("rotate_center", "1"), ("mlt_service", "qtblend"), ("kdenlive_id", "qtblend"),
                 ("rect", ";".join(f"{frames_to_tc(t, fps)}~={r}" for t, r in kf)),
                 ("rotation", "00:00:00.000=0"), ("compositing", "0"), ("distort", "0"),
                 ("kdenlive:collapsed", "0")]:
        prop(f, k, v)


MUSIC_DB = -24.2265   # level of an added music bed, in dB


def add_music_track(t, music):
    """Give a template without a music bed one: a new bottom audio track with the track,
    at your usual level, fading out over the outro (fade timing is set later with the music block)."""
    fps = t.fps
    seq = t.seq
    black = t.by_id["producer0"]
    pos = list(t.root).index(black) + 1
    # reuse the bin's copy of the track if the template already imported it
    bin_chain = next((el for el in t.root if el.tag in ("chain", "producer") and prop(el, "resource") == music
                      and el.get("id") in {e.get("producer") for e in t.by_id["main_bin"].findall("entry")}), None)
    if bin_chain is not None:
        chain = copy.deepcopy(bin_chain)
        kid = prop(bin_chain, "kdenlive:id")
    else:
        kid = str(max(int(p.text) for p in t.root.iter("property")
                      if p.get("name") == "kdenlive:id" and (p.text or "").lstrip("-").isdigit()) + 1)
        n = probe_frames(music, fps)
        chain = ET.Element("chain", {"out": frames_to_tc(n - 1, fps)})
        for k, v in [("length", n), ("eof", "pause"), ("resource", music), ("mlt_service", "avformat"),
                     ("seekable", 1), ("audio_index", 0), ("video_index", -1), ("kdenlive:folderid", -1),
                     ("kdenlive:id", kid), ("kdenlive:clip_type", 1),
                     ("kdenlive:control_uuid", "{" + str(uuid.uuid4()) + "}")]:
            prop(chain, k, v)
        bc = copy.deepcopy(chain)
        bc.set("id", "autocut_music_bin")
        t.root.insert(list(t.root).index(t.by_id["main_bin"]), bc)
        ET.SubElement(t.by_id["main_bin"], "entry", {"producer": "autocut_music_bin", "in": "00:00:00.000",
                                                    "out": chain.get("out")})
    chain.set("id", "autocut_music")
    pa = ET.Element("playlist", {"id": "autocut_music_a"})
    pb = ET.Element("playlist", {"id": "autocut_music_b"})
    for pl in (pa, pb):
        prop(pl, "kdenlive:audio_track", 1)
    e = make_entry("autocut_music", 0, 0, kid, fps)
    f = ET.SubElement(e, "filter", {"in": "00:00:00.000", "out": "00:00:00.000"})
    for k, v in [("window", 75), ("max_gain", "20dB"), ("channel_mask", -1), ("mlt_service", "volume"),
                 ("kdenlive_id", "fadeout"), ("gain", 1), ("end", 0)]:
        prop(f, k, v)
    pa.append(e)
    tr = ET.Element("tractor", {"id": "autocut_music_track", "in": "00:00:00.000"})
    for k, v in [("kdenlive:audio_track", 1), ("kdenlive:trackheight", 75), ("kdenlive:timeline_active", 1),
                 ("kdenlive:collapsed", 0)]:
        prop(tr, k, v)
    ET.SubElement(tr, "track", {"hide": "video", "producer": "autocut_music_a"})
    ET.SubElement(tr, "track", {"hide": "video", "producer": "autocut_music_b"})
    lvl = ET.SubElement(tr, "filter", {"id": "autocut_music_level"})
    for k, v in [("window", 75), ("max_gain", "20dB"), ("level", MUSIC_DB), ("channel_mask", -1),
                 ("mlt_service", "volume"), ("internal_added", 237), ("disable", 0)]:
        prop(lvl, k, v)
    for i, el in enumerate((chain, pa, pb, tr)):
        t.root.insert(pos + i, el)
        t.by_id[el.get("id")] = el
    # new track goes at the bottom (index 1, right above the black background); shift everything else up
    tracks = seq.findall("track")
    seq.insert(list(seq).index(tracks[1]), ET.Element("track", {"producer": "autocut_music_track"}))
    for tn in seq.findall("transition"):
        b = int(prop(tn, "b_track"))
        if b >= 1:
            prop(tn, "b_track", b + 1)
    mix = ET.Element("transition", {"id": "autocut_music_mix"})
    for k, v in [("a_track", 0), ("b_track", 1), ("mlt_service", "mix"), ("kdenlive_id", "mix"),
                 ("internal_added", 237), ("always_active", 1), ("accepts_blanks", 1), ("sum", 1)]:
        prop(mix, k, v)
    seq.insert(list(seq).index(seq.findall("transition")[0]), mix)
    prop(seq, "kdenlive:sequenceproperties.tracksCount", int(prop(seq, "kdenlive:sequenceproperties.tracksCount") or 0) + 1)
    t.tracks.insert(0, "autocut_music_track")
    for info in (t.dialogue, t.video, t.whoosh, t.overlay):
        if info:
            info["idx"] += 1
    t.music = {"idx": 0, "tractor": tr, "pls": [pa, pb], "audio": True}


IMG_SECONDS = 3.0
IMG_BOX = (1920 - 620 - 48, 48, 620, 349)    # top-right card: x, y, w, h


def add_keyword_images(t, script, transcript, img_dir, online, src_to_tl, last_ok, fps, accent):
    """Pop keyword images in on the empty top video track as you say them."""
    from cuts import norm
    from images import align, fetch, parse_script
    if t.overlay is None:
        return [("-", "template has no empty video track for images")]
    tokens, kws = parse_script(open(script).read())
    tr = json.load(open(transcript))

    def stream(key):
        words = [w for c in tr.get("chunks", []) for w in c.get(key, [])]
        out = []
        for w in sorted(words, key=lambda w: w["start"]):
            f = src_to_tl(w["start"])
            # ~0% confidence: words Whisper made up (it can echo script keywords into mumbles)
            if norm(w["word"]) and f is not None and w.get("p", 1) >= 0.01:
                out.append((norm(w["word"]), f))
        return out
    when = align(tokens, kws, [stream("words"), stream("alt_words")])
    report, items = [], []
    next_id = max(int(p.text) for p in t.root.iter("property")
                  if p.get("name") == "kdenlive:id" and (p.text or "").lstrip("-").isdigit()) + 1
    pl = t.overlay["pls"][0]
    main_bin = t.by_id["main_bin"]
    dur = round(IMG_SECONDS * fps)
    prev_end = -1
    for i in sorted(when, key=lambda i: when[i]):
        kw = kws[i]
        start = max(when[i] - 9, prev_end + 12, 96)    # just before the word; never during the opening fade
        if start + dur > last_ok:
            report.append((kw["name"], "said too close to the end, skipped"))
            continue
        png, credit = fetch(kw["name"], kw["src"], img_dir, online, accent=accent)
        if not png:
            report.append((kw["name"], credit))
            continue
        pid = f"autocut_img{next_id}"
        prod = ET.Element("producer", {"id": pid, "in": "00:00:00.000", "out": frames_to_tc(dur - 1, fps)})
        for k, v in [("length", 15000), ("eof", "pause"), ("resource", png), ("ttl", 25), ("aspect_ratio", 1),
                     ("seekable", 1), ("mlt_service", "qimage"), ("kdenlive:duration", frames_to_tc(dur, fps)),
                     ("kdenlive:folderid", -1), ("kdenlive:id", next_id), ("kdenlive:clipname", kw["name"]),
                     ("kdenlive:control_uuid", "{" + str(uuid.uuid4()) + "}"), ("kdenlive:clip_type", 2)]:
            prop(prod, k, v)
        t.root.insert(list(t.root).index(pl), prod)
        ET.SubElement(main_bin, "entry", {"producer": pid, "in": "00:00:00.000", "out": frames_to_tc(dur - 1, fps)})
        e = make_entry(pid, 0, dur - 1, next_id, fps)
        x, y, w, h = IMG_BOX
        small = f"{x + w * .08:.0f} {y + h * .08:.0f} {w * .84:.0f} {h * .84:.0f}"
        full = f"{x} {y} {w} {h}"
        kf = [(0, small + " 0"), (10, full + " 1"), (dur - 11, full + " 1"), (dur - 1, small + " 0")]
        f = ET.SubElement(e, "filter", {"in": e.get("in"), "out": e.get("out")})
        for k, v in [("rotate_center", "1"), ("mlt_service", "qtblend"), ("kdenlive_id", "qtblend"),
                     ("rect", ";".join(f"{frames_to_tc(fr, fps)}~={r}" for fr, r in kf)),
                     ("rotation", "00:00:00.000=0"), ("compositing", "0"), ("distort", "0")]:
            prop(f, k, v)
        items.append((start, e))
        prev_end = start + dur
        next_id += 1
        report.append((kw["name"], f"at {start / fps:.1f}s  ({credit})"))
    placed = {norm(kws[i]["name"]) for i in when}
    for name in dict.fromkeys(kw["name"] for kw in kws):
        if norm(name) not in placed:
            report.append((name, "not said in this take (or not recognised), no image"))
    clear(pl)
    fill(pl, items, fps)
    return report


def build(raw, transcript, template, out, zoom=False, dissolve_gap=2.0, script=None, img_dir=None, online=True,
          accent="0x3b82f6", music=None):
    t = Template(template)
    if music and t.music is None:
        add_music_track(t, os.path.abspath(os.path.expanduser(music)))
    fps = t.fps
    tr_data = json.load(open(transcript))
    ranges, log = compute(tr_data)
    raw_len = probe_frames(raw, fps)

    # --- swap the raw media in every chain that pointed at the template's recording
    for cid in t.raw_ids:
        c = t.by_id[cid]
        for p in list(c.findall("property")):
            if p.get("name", "").startswith(("meta.", "kdenlive:meta.")):
                c.remove(p)
        prop(c, "resource", raw)
        prop(c, "length", raw_len)
        prop(c, "kdenlive:file_size", os.path.getsize(raw))
        prop(c, "kdenlive:file_hash", kdenlive_hash(raw))
        c.set("out", frames_to_tc(raw_len - 1, fps))
    raw_kid = prop(t.by_id[next(iter(t.raw_ids))], "kdenlive:id")

    # --- segments in frames
    segs = []
    for s, e in ranges:
        a, b = round(s * fps), min(raw_len - 1, round(e * fps) - 1)
        if b > a:
            segs.append([a, b])
    # let the outro fade in after the last word, not over it
    if segs and t.outro_v is not None:
        # ...and never pull in anything said after the last kept line (a cut retake, a stray word)
        later = [round(c["start"] * fps) for c in tr_data.get("chunks", []) if round(c["start"] * fps) > segs[-1][1]]
        limit = min(later) - 6 if later else raw_len - 1
        segs[-1][1] = max(segs[-1][1], min(raw_len - 1, segs[-1][1] + OUTRO_MIX, limit))
    starts, pos = [], 0
    for a, b in segs:
        starts.append(pos)
        pos += b - a + 1
    content_end = pos

    # --- which cuts get a dissolve (when a chunk of footage was skipped)
    mix = [False] * len(segs)
    for k in range(1, len(segs)):
        gap = (segs[k][0] - segs[k - 1][1]) / fps
        long_enough = (segs[k][1] - segs[k][0] > 2 * HALF_MIX + 10 and segs[k - 1][1] - segs[k - 1][0] > 2 * HALF_MIX + 10
                       and not mix[k - 1] and segs[k][0] - HALF_MIX >= 0)
        mix[k] = gap >= dissolve_gap and long_enough

    # --- dialogue audio: hard cuts on the first sub-track
    dA, dB = t.dialogue["pls"]
    vA, vB = t.video["pls"]
    # pick each sub-track's own chain before emptying the playlists
    a_chain = {pl.get("id"): chain_for(t, pl, t.raw_ids, next(iter(t.raw_ids))) for pl in (dA, vA, vB)}
    outro_ids = {e.get("producer") for e in (t.outro_v, t.outro_a) if e is not None}
    o_chain = {}
    if t.outro_v is not None:   # video outro on either video sub-track
        for pl in (vA, vB):
            o_chain[pl.get("id")] = chain_for(t, pl, outro_ids, t.outro_v.get("producer"))
    if t.outro_a is not None:   # outro audio (a video outro has sound; a still image doesn't)
        o_chain[dB.get("id")] = chain_for(t, dB, outro_ids, t.outro_a.get("producer"))
    for pl in (dA, dB, vA, vB):
        clear(pl)
    for tr in (t.dialogue["tractor"], t.video["tractor"]):
        for tn in tr.findall("transition"):
            tr.remove(tn)
    fill(dA, [(starts[k], make_entry(a_chain[dA.get("id")], a, b, raw_kid, fps)) for k, (a, b) in enumerate(segs)], fps)

    # --- video: alternate sub-tracks at dissolves
    items = {vA.get("id"): [], vB.get("id"): []}
    trans = []
    groups = []
    side = 0
    v_entries = []
    for k, (a, b) in enumerate(segs):
        start = starts[k]
        if k and mix[k]:
            side ^= 1
            start -= HALF_MIX
            a -= HALF_MIX
            prev = v_entries[-1]
            prev.set("out", frames_to_tc(tc_to_frames(prev.get("out"), fps) + HALF_MIX, fps))
            trans.append((starts[k] - HALF_MIX, starts[k] + HALF_MIX, side ^ 1, side, HALF_MIX))
        pl = (vA, vB)[side]
        e = make_entry(a_chain[pl.get("id")], a, b, raw_kid, fps)
        v_entries.append(e)
        items[pl.get("id")].append((start, e))
        groups.append(((t.video["idx"], start), (t.dialogue["idx"], starts[k])))

    # intro fade from black on the first clip
    if v_entries:
        for f in t.intro_filters:
            nf = copy.deepcopy(f)
            ln = tc_to_frames(f.get("out"), fps) - tc_to_frames(f.get("in"), fps)
            nf.set("in", v_entries[0].get("in"))
            nf.set("out", frames_to_tc(tc_to_frames(v_entries[0].get("in"), fps) + ln, fps))
            v_entries[0].append(nf)
            prop(v_entries[0], "kdenlive:activeeffect", "0")

    # zoom-ins on the action
    zooms = []
    if zoom:
        from zoom import ActionMap
        amap = ActionMap(raw)
        for e in v_entries:
            a, b = tc_to_frames(e.get("in"), fps), tc_to_frames(e.get("out"), fps)
            if (b - a) / fps < 3.0 or e.findall("filter"):
                continue
            z = amap.target(a / fps, b / fps)
            if z:
                zoom_filter(e, z, b - a + 1, fps)
                zooms.append((a / fps, b / fps, z))

    # --- outro (template's logo clip) overlapping the last clip with a 1s mix
    total = content_end
    if t.outro_v is not None:
        ov = copy.deepcopy(t.outro_v)
        olen = tc_to_frames(ov.get("out"), fps) - tc_to_frames(ov.get("in"), fps) + 1
        ostart = content_end - OUTRO_MIX
        oside = side ^ 1
        pl = (vA, vB)[oside]
        ov.set("producer", o_chain[pl.get("id")])
        items[pl.get("id")].append((ostart, ov))
        trans.append((ostart, content_end, side, oside, OUTRO_MIX))
        total = ostart + olen
        if t.outro_a is not None:
            oa = copy.deepcopy(t.outro_a)
            oa.set("producer", o_chain[dB.get("id")])
            fill(dB, [(ostart, oa)], fps)
            at = ET.SubElement(t.dialogue["tractor"], "transition",
                               {"id": "autocut_amix", "in": frames_to_tc(ostart, fps), "out": frames_to_tc(content_end, fps)})
            for k, v in [("a_track", 0), ("b_track", 1), ("mlt_service", "mix"), ("kdenlive_id", "mix"),
                         ("kdenlive:mixcut", OUTRO_MIX), ("start", -1), ("accepts_blanks", 1), ("reverse", 0)]:
                prop(at, k, v)
            groups.append(((t.video["idx"], ostart), (t.dialogue["idx"], ostart)))
        # whoosh lands when the outro starts
        if t.whoosh:
            wpl = next(pl for pl in t.whoosh["pls"] if pl.findall("entry"))
            we = copy.deepcopy(wpl.find("entry"))
            clear(wpl)
            fill(wpl, [(ostart, we)], fps)

    for pl in (vA, vB):
        fill(pl, sorted(items[pl.get("id")], key=lambda x: x[0]), fps)
    for n, (i, o, a_tr, b_tr, cut) in enumerate(trans):
        tn = ET.SubElement(t.video["tractor"], "transition",
                           {"id": f"autocut_luma{n}", "in": frames_to_tc(i, fps), "out": frames_to_tc(o, fps)})
        for k, v in [("a_track", a_tr), ("b_track", b_tr), ("factory", "loader"), ("resource", ""),
                     ("mlt_service", "luma"), ("kdenlive_id", "luma"), ("kdenlive:mixcut", cut), ("softness", 0),
                     ("alpha_over", 0), ("fix_background_alpha", 1), ("invert", 0), ("reverse", 0)]:
            prop(tn, k, v)

    # --- music bed runs to the end and fades out over the outro
    if t.music:
        mpl = next(pl for pl in t.music["pls"] if pl.findall("entry"))
        me = mpl.find("entry")
        clear(mpl)
        me.set("in", "00:00:00.000")
        me.set("out", frames_to_tc(total - 1, fps))
        for f in me.findall("filter"):
            if prop(f, "kdenlive_id") == "fadeout":
                f.set("in", frames_to_tc(max(0, total - 1 - (total - content_end + OUTRO_MIX)), fps))
                f.set("out", frames_to_tc(total - 1, fps))
        fill(mpl, [(0, me)], fps)

    # --- keyword images from the script
    img_report = []
    if script:
        def src_to_tl(sec):
            fr = round(sec * fps)
            for k, (a, b) in enumerate(segs):
                if a <= fr <= b:
                    return starts[k] + fr - a
            return None
        img_report = add_keyword_images(t, script, transcript, img_dir or os.path.dirname(out), online,
                                        src_to_tl, content_end - OUTRO_MIX, fps, accent)

    # --- sequence bookkeeping
    end_tc = frames_to_tc(total - 1, fps)
    for tr in t.root.findall("tractor"):
        if tr.get("out"):
            tr.set("out", end_tc)
        for tk in tr.findall("track"):
            if tk.get("out"):
                tk.set("out", end_tc)
    t.by_id["producer0"].set("out", end_tc) if "producer0" in t.by_id else None
    prop(t.seq, "kdenlive:duration", frames_to_tc(total, fps))
    prop(t.seq, "kdenlive:maxduration", total)
    prop(t.seq, "kdenlive:sequenceproperties.position", 0)
    prop(t.seq, "kdenlive:sequenceproperties.zoneout", total)
    prop(t.seq, "kdenlive:sequenceproperties.guides", "[\n]\n")
    prop(t.seq, "kdenlive:sequenceproperties.groups", json.dumps([
        {"type": "AVSplit", "children": [
            {"data": f"{v[0]}:{v[1]}:-1", "leaf": "clip", "type": "Leaf"},
            {"data": f"{a[0]}:{a[1]}:-1", "leaf": "clip", "type": "Leaf"}]}
        for v, a in groups], indent=4))

    xml = ET.tostring(t.root, encoding="unicode")
    # fresh document identity so it never clashes with the template project
    old_uuid = prop(t.seq, "kdenlive:uuid")
    xml = xml.replace(old_uuid, "{" + str(uuid.uuid4()) + "}")
    xml = re.sub(r'(name="kdenlive:docproperties.documentid">)\d+', lambda m: m.group(1) + str(random.randrange(10**12, 10**13)), xml)
    xml = re.sub(r'(name="kdenlive:docproperties.sessionid">)[^<]*', lambda m: m.group(1) + "{" + str(uuid.uuid4()) + "}", xml)
    with open(out, "w") as f:
        f.write("<?xml version='1.0' encoding='utf-8'?>\n" + xml + "\n")

    return {"segments": len(segs), "dissolves": sum(mix), "length_s": round(total / fps, 1), "last_frame": total - 1,
            "raw_s": round(raw_len / fps, 1), "zooms": zooms, "log": log, "images": img_report}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("transcript")
    ap.add_argument("template")
    ap.add_argument("out")
    ap.add_argument("--zoom", action="store_true")
    ap.add_argument("--dissolve-gap", type=float, default=2.0)
    ap.add_argument("--script", help="script with [[keywords]] / [links](url) to show images for")
    ap.add_argument("--img-dir", help="where fetched keyword images are saved")
    ap.add_argument("--offline", action="store_true", help="keyword images only from your library/files")
    ap.add_argument("--accent", default="0x3b82f6", help="card colour, e.g. 0xf5c518")
    ap.add_argument("--music", help="music bed to add if the template has none")
    a = ap.parse_args()
    r = build(a.raw, a.transcript, a.template, a.out, a.zoom, a.dissolve_gap, a.script, a.img_dir, not a.offline, a.accent, a.music)
    for name, info in r.pop("images"):
        print(f"image {name}: {info}")
    for kind, s, e, info in dict.fromkeys(r.pop("log")):
        print(f"cut {kind:8s} {s:7.2f}-{e:7.2f}  {info}")
    for s, e, z in r.pop("zooms"):
        print(f"zoom {s:7.2f}-{e:7.2f}  {z}")
    print(r)
