"""Find where on screen the action happens in a time range, to aim a zoom-in.

Grabs low-res grayscale frames with ffmpeg and looks at which pixels change.
If the change is concentrated in a small area, return a zoom target; otherwise None.
"""
import subprocess

import numpy as np

W, H, FPS = 96, 54, 5


class ActionMap:
    def __init__(self, video):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", video, "-vf", f"fps={FPS},scale={W}:{H},format=gray",
             "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
        self.frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.int16)

    def target(self, start, end, max_area=0.22, min_scale=1.3, max_scale=1.8):
        a, b = int(start * FPS), int(end * FPS)
        f = self.frames[a:b + 1]
        if len(f) < 3:
            return None
        change = (np.abs(np.diff(f, axis=0)) > 18).sum(axis=0)
        mask = change >= 2                       # changed in at least two frame-pairs
        mask[:3] = False                         # desktop status bar (clock ticks every second)
        mask[-2:] = False                        # taskbar / bottom edge
        if mask.sum() < 12:
            return None                          # nothing happening
        # drop isolated specks (mouse cursor trail is thin; UI changes are blocky)
        ys, xs = np.nonzero(mask)
        y0, y1 = np.percentile(ys, [5, 95])
        x0, x1 = np.percentile(xs, [5, 95])
        bw, bh = (x1 - x0 + 1) / W, (y1 - y0 + 1) / H
        if bw * bh > max_area:
            return None                          # action is spread out: no zoom
        scale = float(np.clip(0.85 / max(bw, bh), min_scale, max_scale))
        cx, cy = (x0 + x1 + 1) / 2 / W, (y0 + y1 + 1) / 2 / H
        return {"scale": round(scale, 2), "cx": round(float(cx), 3), "cy": round(float(cy), 3)}
