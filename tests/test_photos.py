import os
import shutil
import subprocess

import pytest

from dava import looklut, photos
from dava.cli import main

pytestmark = pytest.mark.skipif(not shutil.which("magick"), reason="ImageMagick not installed")


def test_cdl_from_stats_dehazes_and_normalises():
    hazy = photos.cdl_from_stats(black=0.16, white=0.88, mean=0.42)
    assert hazy["offset"] < -0.1 and hazy["slope"] > 1.2  # lifted blacks pulled down, contrast restored
    dark = photos.cdl_from_stats(black=0.01, white=0.95, mean=0.3)
    assert dark["power"] < 1  # dark photo: midtones lifted
    clipped = photos.cdl_from_stats(black=0.0, white=1.0, mean=0.46)
    assert abs(clipped["slope"] - 0.95) < 1e-6 and abs(clipped["power"] - 1.0) < 0.05


def test_hald_matches_the_look_function(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "HALD_LEVEL", 3)  # 9^3 cube, small and fast
    path = tmp_path / "look.ppm"
    photos.write_hald(str(path), looklut.PRESETS["travel"])
    data = path.read_bytes()
    header_end = data.index(b"65535\n") + len(b"65535\n")
    pixels = data[header_end:]
    assert len(pixels) == 27 * 27 * 6
    # pixel for r=8/8, g=0, b=0 (pure red) sits at index 8
    r, g, b = (int.from_bytes(pixels[8 * 6 + i * 2:8 * 6 + i * 2 + 2], "big") / 65535 for i in range(3))
    assert (r, g, b) == pytest.approx(looklut.look(1.0, 0.0, 0.0, looklut.PRESETS["travel"]), abs=1e-4)


def test_bright_clouds_and_shadows_come_out_without_color_blotches(tmp_path, monkeypatch):
    """Regression: without -clamp, near-white clouds pushed above 1.0 hit the LUT out of range (magenta blotches)
    and negative shadows through pow() became NaN (green patches)."""
    monkeypatch.setattr(photos, "HALD_LEVEL", 4)
    src = tmp_path / "scene.png"
    # Hazy scene like a real photo: pale clouds on top, lifted shadows below, plus a few pixels brighter than the
    # measured white point (slightly blue highlight) and darker than the black point (the measurement ignores the
    # extreme 0.5%). The grade pushes the highlight above 1.0 and the black patch below 0.
    subprocess.run(["magick", "-size", "100x50", "xc:rgb(230,232,236)", "-size", "100x50", "xc:rgb(40,38,38)", "-append",
                    "-fill", "rgb(250,252,255)", "-draw", "rectangle 0,0 4,4",
                    "-fill", "rgb(0,0,0)", "-draw", "rectangle 95,95 99,99", str(src)], check=True)
    out_dir = tmp_path / "out"
    result = photos.grade_photos([str(src)], str(out_dir), look="travel", jobs=1, profile=None)
    graded = result["photos"][0]["output"]
    grade = result["photos"][0]
    assert 0.99 * grade["slope"] + grade["offset"] > 1 and grade["offset"] < 0  # out of range before clamping
    # Absolute chroma (max - min channel): about 0 for clean neutrals, large for magenta or green blotches.
    # (HSL saturation is not usable here: near black, (1,0,0) already counts as fully saturated.)
    chroma = subprocess.run(["magick", graded, "-fx", "max(r,max(g,b))-min(r,min(g,b))", "-format", "%[fx:maxima]",
                             "info:"], capture_output=True, text=True, check=True).stdout
    assert float(chroma) < 0.12  # every region of this neutral scene must stay neutral


def test_photos_grade_command(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(photos, "HALD_LEVEL", 3)
    folder = tmp_path / "in"
    folder.mkdir()
    subprocess.run(["magick", "-size", "40x30", "gradient:rgb(40,90,160)-rgb(220,200,120)", str(folder / "a.jpg")],
                   check=True)
    code = main(["photos", "grade", str(folder), "-o", str(tmp_path / "out"), "-j", "1"])
    assert code == 0
    assert (tmp_path / "out" / "a graded.jpg").is_file()
    assert not any(f.startswith(".dava-look") for f in os.listdir(tmp_path / "out"))  # temp LUT removed
