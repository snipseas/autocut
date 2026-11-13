# autocut

Turn a raw screen recording into an edited video. You record one take, mistakes and all, and autocut
gives you a Kdenlive project you can open and tweak, plus (if you ask) a finished 1080p60 MP4.

What it does to the take:

- **Cuts retakes.** When you abandon a sentence and say it again, only the complete take is kept.
- **Cuts dead air.** Long pauses, lone "okay" / "um" phrases and stray noises go.
- **Applies your style.** Music bed, intro fade, whoosh and outro are copied from a video you already
  finished, so every video of a brand looks the same.
- **Zooms in on the action** when what changes on screen is confined to a small area.
- **Shows keyword cards.** Mark words in your script and an image or text card pops up top-right at
  the moment you say them.

It runs on your machine. Nothing is uploaded; the only network use is looking up keyword images,
which `--offline` turns off.

## Requirements

- Linux
- `ffmpeg`, `fontconfig` (`fc-match`) and `column` (util-linux)
- Python 3.10 or newer. If you have [uv](https://docs.astral.sh/uv/), the installer uses it and fetches
  a suitable Python for you.
- [Kdenlive](https://kdenlive.org) to open the projects. `--render` uses the `melt` that ships with it
  (the Flatpak is found on its own; otherwise `melt` on your PATH).
- About 2.5 GB of disk for the Python packages and the speech model. No GPU needed.

## Install

```bash
git clone <this repo> ~/.local/share/autocut
```

```bash
~/.local/share/autocut/install.sh
```

The installer creates a Python environment inside the folder, links the `autocut` command into
`~/.local/bin`, and finishes by running `autocut --check`, which tells you what (if anything) is
missing. The folder can live anywhere. Running the installer again is safe; it only adds what is not
there. The speech model (about 0.5 GB) downloads itself the first time you cut a video.

## Your first video

**1. Teach it your style.** Pick a video you already edited in Kdenlive and register it as a brand:

```bash
autocut --set-brand acme "$HOME/Videos/kdenlive/a finished acme video.kdenlive"
```

**2. Build a project from a new recording.**

```bash
autocut "$HOME/Videos/OBS/my take.mp4" --script "$HOME/Videos/scripts/my script.md"
```

This prints what it cut and why, where it zoomed, and which cards it placed, then writes
`my take (autocut).kdenlive`. Nothing is rendered yet.

**3. Read the list, then render.** If the cuts look right:

```bash
autocut "$HOME/Videos/OBS/my take.mp4" --script "$HOME/Videos/scripts/my script.md" --render
```

The second run is fast because the transcript is cached. If something is off, open the project in
Kdenlive and fix it by hand; it is a normal project.

Building first and rendering second is worth the habit: a wrong cut is much cheaper to spot in the
printed list than in a finished video.

## Options

| Option | What it does |
|---|---|
| `--render` | Also export a 1920x1080, 60 fps H.264/AAC MP4 |
| `--script FILE` | Your script. Marked keywords become on-screen cards (see below) |
| `--brand NAME` | Which brand's look to use. Without it, autocut guesses (see Brands) |
| `--template FILE` | Use any finished project as the style, without registering a brand |
| `--offline` | Never look images up online; use your own images or text cards |
| `--no-zoom` | No automatic zoom-ins |
| `--brands` | List your brands |
| `--set-brand NAME PROJECT [COLOR] [MUSIC] [HEARD]` | Add or change a brand |
| `--check` | Check that everything is installed and set up |

## The script and keyword cards

The script is a plain text or Markdown file with what you plan to say. It does two jobs: it helps the
speech model spell your technical terms, and it tells autocut which words get a card.

```markdown
Open [[server.cfg]] and paste your [[mysql_connection_string]].
Download it from [[Cfx.re | https://forum.cfx.re]] first.
You can also use [[the panel | ~/Pictures/panel.png]] or a normal [link](https://example.com).
```

| Markup | Card shows |
|---|---|
| `[[keyword]]` | An image found for the keyword, or a text card |
| `[[keyword \| https://...]]` | That image, or the preview image of that page |
| `[[keyword \| ~/path/image.png]]` | Your image |
| `[text](https://...)` | The preview image of that page |

For each keyword autocut looks in this order: your keywords folder (`<keyword>.png`, `.jpg` or
`.webp`), the file or link you gave, Wikipedia, Openverse, and finally a plain text card with the
keyword on it. Text cards suit file names and commands well, so `--offline` is a fine default for
technical videos. A card is only placed if you actually say the keyword in the take, and the output
lists every keyword with where its image came from. Images found online are not checked for
licensing; the source is printed so you can.

## Brands

A brand is a finished Kdenlive project that autocut copies the style from, plus a card colour.
Brands are stored one per line in the `brands` file in the autocut folder; use `--set-brand` to edit it.

```bash
autocut --set-brand acme "finished video.kdenlive" 0xf5c518 none "ac *me"
```

- **COLOR**: card border colour as `0xRRGGBB` (default `0x3b82f6`).
- **MUSIC**: a music file to add when the template has no music bed of its own, or `none`.
- **HEARD**: how the brand name comes out in a transcript, as a pattern. Speech models often mishear
  names, so "BrightNode" may need `bright *no` to also catch "Bright Note". Defaults to the name.

When you do not pass `--brand`, autocut picks the brand whose HEARD pattern appears in what you said.
If none does, it looks in the script (text and file name). If that fails too, it uses the first brand
in the file and says so.

### What a template project needs

Any video you edited the usual way works if it has:

- the screen recording cut up on **one video track and one audio track** (required);
- an **empty video track on top**, which is where cards go (without it, no cards);
- optionally a **music bed** (an audio clip longer than 10 s on its own track), a **whoosh** (an audio
  clip up to 3 s on its own track), an **outro** clip after the recording, and a **fade from black**
  on the first clip.

autocut replaces the recording's clips with the new cut and keeps the rest: the music runs to
the new end and fades out, the outro and whoosh move to the new end. The recording is recognised
as the file that sits in a folder named `OBS`, or else the clip cut into the most pieces.

## Folders and settings

Out of the box:

| What | Where |
|---|---|
| New projects | `~/Videos/kdenlive/` |
| Rendered videos | `~/Videos/kdenlive exports/` |
| Cards made for each project | `~/Videos/media imports/autocut images/<project>/` |
| Your own keyword images | `~/Videos/media imports/keywords/` |
| Transcripts, speech model, Python environment, brands | inside the autocut folder (`cache/`, `models/`, `venv/`, `brands`) |

To change any of them, copy `config.example` to `~/.config/autocut/config` and edit it:

```bash
mkdir -p ~/.config/autocut && cp ~/.local/share/autocut/config.example ~/.config/autocut/config
```

| Setting | Default | Meaning |
|---|---|---|
| `AUTOCUT_PROJECTS` | `~/Videos/kdenlive` | Where projects are written |
| `AUTOCUT_EXPORTS` | `~/Videos/kdenlive exports` | Where `--render` writes the MP4 |
| `AUTOCUT_IMAGES` | `~/Videos/media imports/autocut images` | Where cards are saved |
| `AUTOCUT_KEYWORDS` | `~/Videos/media imports/keywords` | Your own keyword images |
| `AUTOCUT_MODEL` | `small.en` | Speech model; `medium.en` is slower and more accurate |
| `AUTOCUT_DEVICE` | `cpu` | `cuda` to use an NVIDIA card (needs CUDA 12 and cuDNN installed) |
| `AUTOCUT_MELT` | found automatically | Command used to run `melt` |

The same names work as environment variables for a single run, and those win over the file.

## How it decides what to cut

1. The audio is split at real pauses, by loudness.
2. Each stretch of speech is transcribed twice with [faster-whisper](https://github.com/SYSTRAN/faster-whisper):
   once plainly for the words and timing, and once primed to write stutters down, which reveals
   sentences that were broken off.
3. A phrase is dropped when the next one or two phrases say it again (a retake), when it is only
   filler, or when it is noise the model heard as words. A repeated joining word across a pause
   ("and ... and click") is trimmed to one.
4. What is left is joined. Pauses longer than 0.8 s are closed up. Where two seconds or more of
   footage were skipped the cut gets a dissolve; shorter cuts are hard cuts.
5. Clips of three seconds or more get a zoom when the changing part of the screen is small.

The thresholds are constants at the top of `cuts.py`, `build.py` and `zoom.py`.

## Limits

- **English only.** The default model and the filler-word lists are English.
- **1920x1080 at 60 fps.** Zooms, card positions and the render assume it.
- **It does not blur anything.** If a password or key is on screen in the take, it is in the video.
  Check before publishing.
- **It follows your voice.** Stretches where you click without speaking are cut, so talk through
  what you do.
- One recording per video.

## Troubleshooting

Start with `autocut --check`.

| Problem | What to do |
|---|---|
| `not set up yet` / missing Python environment | Run `install.sh` |
| `no brands yet` | Register one with `--set-brand`, or pass `--template FILE` |
| Wrong brand picked | Pass `--brand NAME`, or give the brand a better HEARD pattern |
| A keyword got no card | The output says why. Usually it was not said, or was misheard: add it to the script so the model learns the spelling |
| A term is spelled wrong in the cut list | Mark it as a `[[keyword]]` in the script |
| A cut is wrong | Fix it in Kdenlive. Deleting the recording's file in `cache/` forces a fresh transcript |
| `render failed` | The project is still fine; render it from Kdenlive. `--check` shows which `melt` is used |
| `can't be used as a template` | The template needs the recording on one video and one audio track |

## What is in this folder

| File | Job |
|---|---|
| `autocut` | The command: options, brands, caching, and calling the steps below |
| `transcribe.py` | Recording to word-level transcript |
| `cuts.py` | Transcript to list of ranges to keep |
| `build.py` | Template project + cut list to new Kdenlive project |
| `zoom.py` | Finds where on screen the action is |
| `images.py` | Script markup, keyword timing, image lookup and cards |
| `render.sh` | Renders a project with `melt` |
| `install.sh`, `requirements.txt`, `config.example`, `brands.example` | Setup |
