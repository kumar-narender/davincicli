import json
import shutil
import subprocess

import pytest

from dava import looklut
from dava.cli import main
from dava.commands.grading import check_instagram
from recorder import Rec, rec_resolve


def test_look_protects_neutrals_and_pushes_sky_and_gold():
    p = looklut.PRESETS["vibrant"]
    assert looklut.look(0.78, 0.78, 0.77, p) == pytest.approx((0.78, 0.78, 0.77), abs=1e-6)  # white dome
    assert looklut.look(0.02, 0.02, 0.03, p) == pytest.approx((0.02, 0.02, 0.03), abs=1e-6)  # black
    r, g, b = looklut.look(0.64, 0.68, 0.77, p)  # pale lavender sky
    assert b - r > 0.77 - 0.64 and b < 0.77  # bluer and deeper
    r, g, b = looklut.look(0.55, 0.45, 0.25, p)  # dull gold
    assert (r - b) > (0.55 - 0.25)  # richer gold


def test_cube_file_shape(tmp_path):
    path = tmp_path / "x.cube"
    looklut.write_cube(str(path), looklut.PRESETS["subtle"], size=5)
    lines = path.read_text().splitlines()
    assert "LUT_3D_SIZE 5" in lines
    data = [line for line in lines if line[:1].isdigit()]
    assert len(data) == 125
    assert data[0] == "0.000000 0.000000 0.000000" and data[-1] == "1.000000 1.000000 1.000000"


def test_look_apply_writes_lut_and_sets_it_on_each_clip(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BMD_RESOLVE_LUT_DIR", str(tmp_path))
    graphs = [Rec("g1", GetNumNodes=1), Rec("g2", GetNumNodes=1)]
    items = [Rec("i1", GetName="a", GetNodeGraph=graphs[0]), Rec("i2", GetName="b", GetNodeGraph=graphs[1])]
    resolve, parts = rec_resolve(items=items)
    code = main(["look", "apply", "-p", "bold", "--sky-sat", "1.9", "-n", "My Look"], resolve=resolve)
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert (tmp_path / "dava" / "My Look.cube").is_file()
    assert out["params"]["sky_sat"] == 1.9 and out["params"]["gold_sat"] == looklut.PRESETS["bold"]["gold_sat"]
    assert parts["project"].called("RefreshLUTList") == [()]
    assert [g.called("SetLUT") for g in graphs] == [[(1, "dava/My Look.cube")]] * 2


def test_look_apply_reports_refused_lut(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BMD_RESOLVE_LUT_DIR", str(tmp_path))
    items = [Rec("i1", GetName="a", GetNodeGraph=Rec("g", GetNumNodes=1, SetLUT=False))]
    resolve, _ = rec_resolve(items=items)
    assert main(["look", "apply"], resolve=resolve) == 1
    assert "did not accept LUT" in capsys.readouterr().err


def test_deliver_instagram_settings(tmp_path, capsys):
    resolve, parts = rec_resolve()
    parts["timeline"].set(GetSettings={"timelineFrameRate": 30})
    parts["project"].set(SetCurrentTimeline=True, SetCurrentRenderFormatAndCodec=True, SetCurrentRenderMode=True,
                         AddRenderJob="job-9", StartRendering=True,
                         SetRenderSettings=lambda s: "EncodingProfile" not in s)
    code = main(["deliver", "instagram", "-o", str(tmp_path), "-n", "Reel"], resolve=resolve)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    sent = {k: v for (s,) in parts["project"].called("SetRenderSettings") for k, v in s.items()}
    assert sent["FormatWidth"] == 1080 and sent["FormatHeight"] == 1920 and sent["VideoQuality"] == 12000
    assert sent["NetworkOptimization"] is True and sent["ExportAudio"] is False
    assert sent["MarkIn"] == 86400 and sent["MarkOut"] == 86400 + 240 - 1
    assert out["warnings"] == ["setting rejected: EncodingProfile"] and out["job"] == "job-9"


def test_deliver_instagram_fails_on_essential_rejection(tmp_path, capsys):
    resolve, parts = rec_resolve()
    parts["project"].set(SetCurrentTimeline=True, SetCurrentRenderFormatAndCodec=True, SetCurrentRenderMode=True,
                         SetRenderSettings=lambda s: "FormatWidth" not in s)
    assert main(["deliver", "instagram", "-o", str(tmp_path)], resolve=resolve) == 1
    assert "FormatWidth" in capsys.readouterr().err
    assert parts["project"].called("AddRenderJob") == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_check_instagram_on_real_files(tmp_path):
    good = tmp_path / "good.mp4"
    bad = tmp_path / "bad.mp4"
    base = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=1"]
    tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    # Constant bitrate (a test pattern would otherwise compress far below 5 Mbps) and BT.709 written into the
    # H.264 stream itself, the way Resolve's renders carry it.
    subprocess.run(base + ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-b:v", "8M",
                           "-minrate", "8M", "-maxrate", "8M", "-bufsize", "8M", "-x264-params", "nal-hrd=cbr",
                           *tags, "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
                           "matrix_coefficients=1", "-movflags", "+faststart", str(good)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv444p", str(bad)], check=True)
    ok = check_instagram(str(good))
    assert ok["ok"], ok["problems"]
    result = check_instagram(str(bad))
    joined = " ".join(result["problems"])
    assert not result["ok"] and "9:16" in joined and "yuv420p" in joined and "bt709" in joined and "moov" in joined


def test_travel_look_does_not_push_vivid_colors_into_neon():
    """Regression for the client review: vivid lawns, pond water and deep skies went neon/acid/cobalt."""
    import colorsys
    t = looklut.PRESETS["travel"]
    for vivid in [(0.45, 0.75, 0.15), (0.55, 0.55, 0.12), (0.1, 0.35, 0.85), (0.7, 0.4, 0.2)]:
        before = colorsys.rgb_to_hsv(*vivid)[1]
        after = colorsys.rgb_to_hsv(*looklut.look(*vivid, t))[1]
        assert after - before < 0.06, vivid
    dull = (0.64, 0.68, 0.77)  # pale sky still gets bluer
    assert colorsys.rgb_to_hsv(*looklut.look(*dull, t))[1] - colorsys.rgb_to_hsv(*dull)[1] > 0.04


@pytest.mark.parametrize("rgb, expected", [
    ((0.64, 0.68, 0.77), (0.538672, 0.607589, 0.728416)),
    ((0.55, 0.45, 0.25), (0.5665, 0.41715, 0.11845)),
])
def test_vibrant_preset_is_locked(rgb, expected):
    """The videos were graded with this preset; changing it would silently change their look."""
    assert looklut.look(*rgb, looklut.PRESETS["vibrant"]) == pytest.approx(expected, abs=1e-5)
