#!/usr/bin/env bash
# Render a .kdenlive with the Flatpak's melt and stop it cleanly at the last frame
# (melt never exits by itself on Kdenlive projects: the black background track is endless).
#   render.sh PROJECT.kdenlive OUT.mp4 LAST_FRAME [WxH]
#   render.sh --which        print the melt command that would be used
set -u
# melt: $AUTOCUT_MELT if set, else the one inside the Kdenlive Flatpak, else one on your PATH
if [ -n "${AUTOCUT_MELT:-}" ]; then read -ra melt <<< "$AUTOCUT_MELT"
elif command -v flatpak >/dev/null && flatpak info org.kde.kdenlive >/dev/null 2>&1; then
  melt=(flatpak run --command=melt org.kde.kdenlive)
elif command -v melt >/dev/null; then melt=(melt)
elif command -v melt-7 >/dev/null; then melt=(melt-7)
else echo "render.sh: no melt found (install Kdenlive, or set AUTOCUT_MELT)" >&2; exit 1; fi
[ "${1:-}" = --which ] && { echo "${melt[*]}"; exit 0; }
proj=$1; out=$2; last=$3; size=${4:-1920x1080}
log=$(mktemp)
# -profile: without it melt falls back to PAL (16:15 pixels) and the video plays stretched, not 16:9
setsid "${melt[@]}" -profile atsc_1080p_60 "$proj" \
  -consumer "avformat:$out" in=0 out="$last" s="$size" aspect=1.777778 frame_rate_num=60 frame_rate_den=1 progressive=1 \
  ab=384k acodec=aac channels=2 crf=15 f=mp4 g=15 movflags=+faststart preset=ultrafast \
  real_time=-1 threads=0 vcodec=libx264 >"$log" 2>&1 </dev/null &
pid=$!
prev=-1; still=0
while kill -0 "$pid" 2>/dev/null; do
  cur=$(tr '\r' '\n' <"$log" | grep -o 'Current Frame: *[0-9]*' | tail -1 | grep -o '[0-9]*$')
  cur=${cur:-0}
  if [ "$cur" = "$prev" ]; then still=$((still + 1)); else still=0; prev=$cur; fi
  # done at the last frame, or if progress has stalled for 20s near the end
  if [ "$cur" -ge "$last" ] || { [ "$still" -ge 20 ] && [ "$cur" -gt 0 ]; }; then
    sleep 2
    kill -INT -- -"$pid" 2>/dev/null   # same as Ctrl+C: melt finishes writing the file
    break
  fi
  sleep 1
done
for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
kill -0 "$pid" 2>/dev/null && { echo "melt did not stop; killing"; kill -KILL -- -"$pid"; }
rm -f "$log"
ffprobe -v error -show_entries format=duration -of csv=p=0 "$out"
