"""Grade still photos outside Resolve with the same look: measured dehaze/exposure CDL + a look LUT.

Pipeline per photo (ImageMagick):
  color-manage to sRGB (iPhone photos carry Apple's wide-gamut profile; the look is designed for sRGB)
  -> CDL: slope, offset, power from the photo's own black point, white point and mean
  -> saturation -> look LUT (HALD) -> 8-bit JPEG with the sRGB profile embedded.
Every step is followed by -clamp: ImageMagick 7 is HDRI (floating point), so without it values above 1 reach
the LUT lookup (magenta/cyan blotches in bright clouds) and pow() of negative values yields NaN (green patches).
"""

import concurrent.futures
import math
import os
import shutil
import struct
import subprocess
import sys

from . import looklut
from .connect import ResolveError

SRGB_PROFILES = [
    "/System/Library/ColorSync/Profiles/sRGB Profile.icc",
    "/usr/share/color/icc/colord/sRGB.icc",
    "/usr/share/color/icc/sRGB.icc",
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "spool", "drivers", "color", "sRGB Color Space Profile.icm"),
]
HALD_LEVEL = 8  # 64^3 lookup cube in a 512x512 image


def magick():
    tool = shutil.which("magick")
    if not tool:
        raise ResolveError("ImageMagick 7 ('magick') is required: brew install imagemagick.")
    return tool


def srgb_profile(path=None):
    if path:
        if not os.path.isfile(path):
            raise ResolveError(f"sRGB profile not found: {path}")
        return path
    return next((p for p in SRGB_PROFILES if os.path.isfile(p)), None)


def write_hald(path, params):
    """Write the look as a 16-bit HALD image (binary PPM) computed straight from looklut.look."""
    size = HALD_LEVEL ** 2
    side = HALD_LEVEL ** 3
    n = size - 1
    out = bytearray()
    for b in range(size):
        for g in range(size):
            for r in range(size):
                rgb = looklut.look(r / n, g / n, b / n, params)
                out += struct.pack(">HHH", *(max(0, min(65535, round(c * 65535))) for c in rgb))
    with open(path, "wb") as handle:
        handle.write(f"P6\n{side} {side}\n65535\n".encode("ascii"))
        handle.write(out)


def measure(path):
    raw = subprocess.run([magick(), path, "-auto-orient", "-resize", "240x240", "-colorspace", "Gray", "-depth", "8",
                          "gray:-"], capture_output=True, check=True).stdout
    values = sorted(raw)
    count = len(values)
    pct = lambda q: values[min(count - 1, int(q * count))] / 255
    return pct(0.005), pct(0.995), sum(values) / count / 255


def cdl_from_stats(black, white, mean, target_mid=0.46, saturation=1.05):
    """Black point -> 0.02, white point -> 0.97, then a gamma that brings the mean toward target_mid."""
    slope = min(max(0.95 / max(white - black, 0.3), 0.9), 1.35)
    offset = min(max(0.02 - black * slope, -0.12), 0.03)
    mid = min(max(mean * slope + offset, 0.05), 0.95)
    power = min(max(math.log(target_mid) / math.log(mid), 0.8), 1.2)
    return {"slope": round(slope, 4), "offset": round(offset, 4), "power": round(power, 4), "saturation": saturation}


def grade_one(src, out, hald, profile, cdl, quality=95):
    offset = cdl["offset"]
    cmd = [magick(), src, "-auto-orient"]
    if profile:
        cmd += ["-profile", profile]
    cmd += ["-depth", "16",
            "-evaluate", "multiply", str(cdl["slope"]), "-clamp",
            "-evaluate", "add" if offset >= 0 else "subtract", f"{abs(offset) * 100:.4f}%", "-clamp",
            "-evaluate", "pow", str(cdl["power"]), "-clamp",
            "-modulate", f"100,{cdl['saturation'] * 100:.0f}", "-clamp"]
    if hald:
        cmd += [hald, "-hald-clut", "-clamp"]
    cmd += ["-depth", "8", "-quality", str(quality), out]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ResolveError(f"ImageMagick failed on {os.path.basename(src)}: {result.stderr.strip()}")


def grade_photos(sources, out_dir, look="travel", saturation=1.05, suffix=" graded", jobs=8, quality=95, profile=None,
                 target_mid=0.46):
    magick()
    os.makedirs(out_dir, exist_ok=True)
    profile = srgb_profile(profile)
    hald = None
    if look:
        if look not in looklut.PRESETS:
            raise ResolveError(f"Unknown look {look!r}; choose from {', '.join(looklut.PRESETS)}.")
        hald = os.path.join(out_dir, f".dava-look-{look}.ppm")
        write_hald(hald, looklut.PRESETS[look])

    def work(src):
        black, white, mean = measure(src)
        cdl = cdl_from_stats(black, white, mean, target_mid, saturation)
        name = os.path.splitext(os.path.basename(src))[0]
        out = os.path.join(out_dir, f"{name}{suffix}.jpg")
        grade_one(src, out, hald, profile, cdl, quality)
        return {"source": src, "output": out, "black": round(black, 3), "white": round(white, 3),
                "mean": round(mean, 3), **cdl}

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            results = list(pool.map(work, sources))
    finally:
        if hald and os.path.exists(hald):
            os.remove(hald)
    return {"graded": len(results), "out_dir": out_dir, "look": look,
            "color_managed": bool(profile), "photos": results}


def collect(paths):
    """Expand folders into the image files inside them (non-recursive), keep files as given."""
    found = []
    for path in paths:
        if os.path.isdir(path):
            found += sorted(os.path.join(path, f) for f in os.listdir(path)
                            if f.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"))
                            and not f.startswith("."))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise ResolveError(f"Not found: {path}")
    if not found:
        raise ResolveError("No photos found.")
    return found
