"""Keyword images: find which words in your script get an image, when you say them, and fetch the image.

Script markup (paste your script into a .txt/.md file):
    [[oxmysql]]                         keyword; image from your library folder, else looked up online
    [[server.cfg | ~/Pictures/cfg.png]] keyword with a local image
    [[Cfx.re | https://forum.cfx.re]]   keyword with a web page (its preview image) or a direct image URL
    [Cfx.re](http://Cfx.re)             markdown links count too (page preview image)

Library folder (checked first for every keyword, so you can curate; move it with AUTOCUT_KEYWORDS):
    ~/Videos/media imports/keywords/<keyword>.png|jpg|webp
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from html.parser import HTMLParser

UA = {"User-Agent": "autocut/1.0"}
LIBRARY = os.path.expanduser(os.environ.get("AUTOCUT_KEYWORDS") or "~/Videos/media imports/keywords")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


# ---------- script parsing ----------
MARK = re.compile(r"\[\[([^\]|]+?)(?:\s*\|\s*([^\]]+?))?\]\]|\[([^\]\[]+)\]\((https?://[^)\s]+)\)")


def parse_script(text):
    """Returns (tokens, keywords). tokens: normalized words of the script.
    keywords: [{name, src, tok}] with tok = index of the keyword's first token."""
    tokens, keywords, pos = [], [], 0

    def add_words(s):
        for w in re.findall(r"[A-Za-z0-9'.\-]+", s):
            if norm(w):
                tokens.append(norm(w))

    for m in MARK.finditer(text):
        add_words(text[pos:m.start()])
        name = (m.group(1) or m.group(3)).strip()
        src = (m.group(2) or m.group(4) or "").strip() or None
        keywords.append({"name": name, "src": src, "tok": len(tokens)})
        add_words(name)
        pos = m.end()
    add_words(text[pos:])
    return tokens, keywords


# ---------- when is each keyword said ----------
SPOKEN_PUNCTUATION = {"underscore", "dot", "dash", "slash", "hyphen"}


def heard(keyword, spoken):
    """Every place the keyword may have been said, allowing for Whisper's spelling
    ('crxwords' ~ 'Cfx.re'). Returns [(score, coverage, index into spoken)]:
    score = overall similarity, coverage = how much of the keyword was actually heard."""
    parts = [norm(w) for w in keyword.split() if norm(w)]
    target = "".join(parts)
    hits = []
    for j in range(len(spoken)):
        # a phrase like "ensure vMenu" must start with its first word; "install vMenu" is not it
        if len(parts) > 1 and SequenceMatcher(None, parts[0], spoken[j][0]).ratio() < 0.75:
            continue
        best = (0.0, 0.0, 1)
        for n in range(1, 6):                    # "server.cfg" may come out as "server" + "cfg"
            if j + n > len(spoken):
                break
            said = "".join(spoken[j + m][0] for m in range(n))
            if len(said) > len(target) + 4:
                break
            sm = SequenceMatcher(None, target, said)
            cover = sum(blk.size for blk in sm.get_matching_blocks()) / len(target)
            best = max(best, (sm.ratio(), cover, n))
        if best[0] >= 0.4:
            hits.append((best[0], best[1], j, j + best[2] - 1))
    return hits


def align(tokens, keywords, streams):
    """streams: one or more hearings of the edit, each [(normalized word, timeline frame)] in order.
    Returns {keyword index: frame} for keywords you actually said.

    A keyword is placed where it was clearly heard, or, for a looser match, only right where
    the script says it should come. Longer keywords claim their spot first, so
    "mysql_connection_string" wins over "oxmysql" when you say "MySQL connection string"."""
    streams = [[(w, f) for w, f in st if w.rstrip("s") not in SPOKEN_PUNCTUATION] for st in streams if st]
    if not streams:
        return {}
    main = streams[0]
    sm = SequenceMatcher(None, tokens, [w for w, _ in main], autojunk=False)
    blocks = [blk for blk in sm.get_matching_blocks() if blk.size >= 2]   # lone "the"/"and" matches are noise

    def expected(k):
        before = [blk for blk in blocks if blk.a <= k]
        if not before:
            return main[0][1]
        blk = before[-1]
        return main[min(len(main) - 1, blk.b + min(k - blk.a, blk.size))][1]

    first_of = {}
    for i, kw in enumerate(keywords):
        first_of.setdefault(norm(kw["name"]), i)
    cands = []
    for key, i in first_of.items():
        name = keywords[i]["name"]
        exps = [expected(kw["tok"]) for kw in keywords if norm(kw["name"]) == key]
        for st in streams:
            for score, cover, j, j2 in heard(name, st):
                frame, frame2 = st[j][1], st[j2][1]
                dist = min(abs(frame - e) for e in exps)
                clear = score >= 0.7 and cover >= 0.75
                # looser, but only within 3s of where the script has it, and most of the keyword heard
                loose = dist <= 180 and st[j][0][:1] == key[:1] and score >= 0.45 and cover >= 0.6
                if clear or loose:
                    cands.append((-round(cover * len(key)), not clear, dist, frame, frame2, i))
    out, taken = {}, []
    for _, _, _, frame, frame2, i in sorted(cands):
        # the same spoken words can't be two keywords ("MySQL" inside "MySQL connection string")
        if i in out or any(frame <= b + 6 and frame2 >= a - 6 for a, b in taken):
            continue
        out[i] = frame
        taken.append((frame, frame2))
    return out


# ---------- fetching ----------
class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("content"):
            k = a.get("property") or a.get("name") or ""
            if k in ("og:image", "twitter:image", "og:image:url"):
                self.found.setdefault(k, a["content"])
        if tag == "link" and "icon" in (a.get("rel") or "") and a.get("href"):
            self.found.setdefault("icon:" + (a.get("sizes") or ""), a["href"])


def _get(url, limit=15_000_000):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.headers.get_content_type(), r.read(limit), r.geturl()


def _to_png(data, out_png):
    """Any common image -> PNG, longest side at most 1600px (ffmpeg handles jpg/webp/gif)."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "pipe:0", "-frames:v", "1",
                        "-vf", "scale='min(1600,iw)':-2:force_original_aspect_ratio=decrease", out_png],
                       input=data, capture_output=True)
    return p.returncode == 0 and os.path.getsize(out_png) > 0


def _from_url(url):
    ctype, data, final = _get(url)
    if ctype.startswith("image/") and ctype != "image/svg+xml":
        return data, url
    if ctype == "text/html":
        m = _Meta()
        m.feed(data.decode("utf-8", "replace"))
        pick = m.found.get("og:image") or m.found.get("og:image:url") or m.found.get("twitter:image")
        if not pick:
            icons = sorted((k for k in m.found if k.startswith("icon:")), reverse=True)
            pick = m.found[icons[0]] if icons else None
        if pick:
            img = urllib.parse.urljoin(final, pick)
            ctype, data, _ = _get(img)
            if ctype.startswith("image/") and ctype != "image/svg+xml":
                return data, img
    return None, None


def _wikipedia(q):
    url = ("https://en.wikipedia.org/w/api.php?action=query&format=json&prop=pageimages|info&inprop=url"
           "&piprop=thumbnail&pithumbsize=1200&redirects=1&titles=" + urllib.parse.quote(q))
    _, data, _ = _get(url)
    for page in json.loads(data).get("query", {}).get("pages", {}).values():
        thumb = page.get("thumbnail", {}).get("source")
        if thumb:
            return _get(thumb)[1], f"Wikipedia: {page.get('fullurl')}"
    return None, None


def _openverse(q):
    url = "https://api.openverse.org/v1/images/?page_size=1&q=" + urllib.parse.quote(q)
    _, data, _ = _get(url)
    res = json.loads(data).get("results", [])
    if res:
        r = res[0]
        credit = f"{r.get('title')} by {r.get('creator')} ({r.get('license')} {r.get('license_version')}) {r.get('foreign_landing_url')}"
        return _get(r["url"])[1], credit
    return None, None


def text_card(keyword, out_png, accent="0x3b82f6"):
    """Fallback: a clean card with the keyword in monospace (good for file names like server.cfg)."""
    font = subprocess.run(["fc-match", "-f", "%{file}", "monospace:bold"], capture_output=True, text=True).stdout
    text = _ff_text(keyword)
    size = max(48, min(110, int(1800 / max(6, len(keyword)))))
    vf = (f"drawbox=x=0:y=0:w=iw:h=ih:color={accent}:t=10,"
          f"drawbox=x=0:y=ih-26:w=iw:h=26:color={accent}:t=fill,"
          f"drawtext=expansion=none:fontfile='{font}':text='{text}':fontcolor=white:fontsize={size}:x=(w-tw)/2:y=(h-th)/2-10")
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x161a22:s=1240x698",
                        "-frames:v", "1", "-vf", vf, out_png], capture_output=True)
    return p.returncode == 0


def _ff_text(s):
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")


def image_card(img_png, keyword, out_png, accent="0x3b82f6"):
    """Put an image on the same card as text cards: readable over any background, keyword as caption."""
    font = subprocess.run(["fc-match", "-f", "%{file}", "monospace:bold"], capture_output=True, text=True).stdout
    fc = (f"[1:v]scale='min(1100,iw*3)':'min(470,ih*3)':force_original_aspect_ratio=decrease:flags=lanczos[img];"
          f"[0:v][img]overlay=x=(W-w)/2:y=40+(470-h)/2,"
          f"drawbox=x=0:y=0:w=iw:h=ih:color={accent}:t=10,"
          f"drawbox=x=0:y=ih-26:w=iw:h=26:color={accent}:t=fill,"
          f"drawtext=expansion=none:fontfile='{font}':text='{_ff_text(keyword)}':fontcolor=white:fontsize=64:x=(w-tw)/2:y=548")
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x161a22:s=1240x698",
                        "-i", img_png, "-frames:v", "1", "-filter_complex", fc, out_png], capture_output=True)
    return p.returncode == 0


def fetch(keyword, src, out_dir, online=True, cards=True, accent="0x3b82f6"):
    png, credit = _fetch(keyword, src, out_dir, online, cards, accent)
    if png and cards and not credit.startswith("text card"):
        raw = png[:-4] + ".src.png"
        os.replace(png, raw)
        if not image_card(raw, keyword, png, accent):
            os.replace(raw, png)
    return png, credit


def _fetch(keyword, src, out_dir, online=True, cards=True, accent="0x3b82f6"):
    """Returns (png path, credit) or (None, reason)."""
    out_dir = os.path.abspath(os.path.expanduser(out_dir))   # Kdenlive resolves relative paths from the project folder
    os.makedirs(out_dir, exist_ok=True)
    out_png = os.path.join(out_dir, re.sub(r"[^\w.-]+", "_", keyword).strip("_") + ".png")
    # 1. your library
    for ext in IMG_EXT:
        lib = os.path.join(LIBRARY, keyword + ext)
        if os.path.exists(lib):
            return (out_png, f"library: {lib}") if _to_png(open(lib, "rb").read(), out_png) else (None, "unreadable library image")
    attempts = []
    if src and not src.startswith("http"):
        path = os.path.expanduser(src)
        if os.path.exists(path):
            return (out_png, f"file: {path}") if _to_png(open(path, "rb").read(), out_png) else (None, "unreadable image file")
        return None, f"file not found: {path}"
    if not online:
        if cards and text_card(keyword, out_png, accent):
            return out_png, "text card"
        return None, "not in library (online lookup off)"
    if src:
        attempts.append(lambda: _from_url(src))
    attempts += [lambda: _wikipedia(keyword), lambda: _openverse(keyword)]
    for a in attempts:
        try:
            data, credit = a()
        except Exception:
            continue
        if data and _to_png(data, out_png):
            return out_png, credit
    if cards and text_card(keyword, out_png, accent):
        return out_png, "text card (no image found; drop one in the keywords folder to replace it)"
    return None, "no image found"
