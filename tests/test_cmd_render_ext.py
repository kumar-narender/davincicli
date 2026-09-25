"""Tests for dava render (formats, codecs, settings, jobs, wait), dava preset and dava quick."""

import json
import os

import pytest

from dava import connect
from dava.bridge import Session
from dava.cli import build_parser, main
from dava.commands import render_ext
from dava.mcp import Server
from dava.registry import leaf_parsers
from dava.spec import Spec
from recorder import rec_resolve

MINI_SPEC = os.path.join(os.path.dirname(__file__), "data", "mini.pyi")
HAS_STUB = os.path.isfile(connect.stub_path() or "")
needs_stub = pytest.mark.skipif(not HAS_STUB, reason="Resolve scripting API definition not installed")

FORMATS = {"QuickTime": "mov", "MP4": "mp4"}
CODECS = {"mov": {"Apple ProRes 422 HQ": "ProRes422HQ", "H.264": "H264"}, "mp4": {"H.264": "H264"}}


def session(**project_returns):
    """A recorded Resolve whose project knows FORMATS and CODECS; keyword arguments override project returns."""
    resolve, parts = rec_resolve()
    parts["project"].set(
        GetRenderFormats=FORMATS, GetAudioRenderFormats={"Wave": "wav"},
        GetRenderCodecs=lambda ext: CODECS.get(ext, {}),
        GetAudioRenderCodecs=lambda ext: {"Linear PCM": "LinearPCM"} if ext == "wav" else {},
        GetCurrentRenderFormatAndCodec={"format": "mov", "codec": "ProRes422HQ"})
    parts["project"].set(**project_returns)
    return resolve, parts


def run(resolve, capsys, *argv):
    code = main(["--json", *argv], resolve=resolve)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return code, out, captured.err


def names(rec):
    return [name for name, _ in rec.calls]


# --- formats, codecs, format, mode, resolutions ----------------------------------------


def test_formats_are_rows_sorted_by_name(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "render", "formats")
    assert code == 0
    assert out == [{"format": "MP4", "extension": "mp4"}, {"format": "QuickTime", "extension": "mov"}]
    code, out, _ = run(resolve, capsys, "render", "formats", "--audio")
    assert out == [{"format": "Wave", "extension": "wav"}]
    assert "GetAudioRenderFormats" in names(parts["project"])


def test_formats_fail_when_resolve_lists_none(capsys):
    resolve, _ = session(GetRenderFormats={})
    code, _, err = run(resolve, capsys, "render", "formats")
    assert code == 1 and "no render formats" in err


def test_codecs_accept_a_format_name_and_pass_the_extension(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "render", "codecs", "QuickTime")
    assert code == 0
    assert parts["project"].called("GetRenderCodecs")[-1] == ("mov",)
    assert out == [{"codec": "ProRes422HQ", "description": "Apple ProRes 422 HQ"},
                   {"codec": "H264", "description": "H.264"}]
    code, out, _ = run(resolve, capsys, "render", "codecs", "wav", "--audio")
    assert code == 0 and out == [{"codec": "LinearPCM", "description": "Linear PCM"}]
    assert parts["project"].called("GetAudioRenderCodecs")[-1] == ("wav",)


def test_codecs_of_unknown_format_fail_and_list_formats(capsys):
    resolve, _ = session()
    code, _, err = run(resolve, capsys, "render", "codecs", "avi")
    assert code == 1
    assert "no codecs for format 'avi'" in err and "QuickTime (mov)" in err and "MP4 (mp4)" in err


def test_formats_with_codecs_nest_them_and_print_json(capsys):
    resolve, parts = session()
    code = main(["render", "formats", "--codecs"], resolve=resolve)
    out = json.loads(capsys.readouterr().out)  # nested rows are JSON even without --json
    assert code == 0
    assert out == [
        {"format": "MP4", "extension": "mp4", "codecs": [{"codec": "H264", "description": "H.264"}]},
        {"format": "QuickTime", "extension": "mov",
         "codecs": [{"codec": "ProRes422HQ", "description": "Apple ProRes 422 HQ"},
                    {"codec": "H264", "description": "H.264"}]}]
    assert parts["project"].called("GetRenderCodecs") == [("mp4",), ("mov",)]


def test_formats_without_codecs_print_a_table(capsys):
    resolve, parts = session()
    assert main(["render", "formats"], resolve=resolve) == 0
    assert capsys.readouterr().out.splitlines()[0].split() == ["format", "extension"]
    assert "GetRenderCodecs" not in names(parts["project"])


def test_codecs_default_to_the_current_format(capsys):
    resolve, parts = session(GetCurrentRenderFormatAndCodec={"format": "mp4", "codec": "H264"})
    code, out, _ = run(resolve, capsys, "--read-only", "render", "codecs")
    assert code == 0 and out == [{"codec": "H264", "description": "H.264"}]
    assert parts["project"].called("GetRenderCodecs") == [("mp4",)]


def test_audio_codecs_need_a_format(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "render", "codecs", "--audio")
    assert code == 1 and "Give an audio format" in err and "Wave (wav)" in err
    assert "GetAudioRenderCodecs" not in names(parts["project"])


def test_format_without_arguments_reads_the_current_one(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "--read-only", "render", "format")
    assert code == 0 and out == {"format": "mov", "codec": "ProRes422HQ"}
    assert "SetCurrentRenderFormatAndCodec" not in names(parts["project"])


def test_format_set_maps_names_to_extension_and_codec_name(capsys):
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "render", "format", "QuickTime", "apple prores 422 hq")
    assert code == 0
    assert parts["project"].called("SetCurrentRenderFormatAndCodec") == [("mov", "ProRes422HQ")]
    assert out == {"format": "mov", "codec": "ProRes422HQ"}
    # Values Resolve knows by name already are passed as they are.
    run(resolve, capsys, "render", "format", "mp4", "H264")
    assert parts["project"].called("SetCurrentRenderFormatAndCodec")[-1] == ("mp4", "H264")


def test_format_set_rejected_lists_the_codecs(capsys):
    resolve, parts = session(SetCurrentRenderFormatAndCodec=False)
    code, _, err = run(resolve, capsys, "render", "format", "mp4", "DNxHR")
    assert code == 1
    assert parts["project"].called("SetCurrentRenderFormatAndCodec") == [("mp4", "DNxHR")]
    assert "rejected format 'mp4' with codec 'DNxHR'" in err and "Codecs for mp4: H.264 (H264)" in err


def test_format_without_codec_fails_before_changing_anything(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "--read-only", "render", "format", "mov")
    assert code == 1
    assert "Give the codec too" in err and "ProRes422HQ" in err
    assert "SetCurrentRenderFormatAndCodec" not in names(parts["project"])


def test_format_set_is_blocked_in_read_only_mode(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "--read-only", "render", "format", "mp4", "H264")
    assert code == 1 and "read-only" in err
    assert parts["project"].calls == []


def test_mode_get_and_set(capsys):
    resolve, parts = session(GetCurrentRenderMode=1)
    code, out, _ = run(resolve, capsys, "render", "mode")
    assert code == 0 and out == "single"
    code, _, _ = run(resolve, capsys, "render", "mode", "individual")
    assert code == 0 and parts["project"].called("SetCurrentRenderMode") == [(0,)]
    parts["project"].set(SetCurrentRenderMode=False)
    code, _, err = run(resolve, capsys, "render", "mode", "single")
    assert code == 1 and "refused render mode 'single'" in err
    with pytest.raises(SystemExit) as exc:
        main(["render", "mode", "all"], resolve=resolve)
    assert exc.value.code == 2


@pytest.mark.parametrize("failure", [None, False])
def test_mode_fails_when_resolve_reports_nothing(capsys, failure):
    # False must not read as mode 0 ('individual'): False == 0 in Python.
    resolve, _ = session(GetCurrentRenderMode=failure)
    code, out, err = run(resolve, capsys, "render", "mode")
    assert code == 1 and out is None and "did not report the render mode" in err


def test_resolutions_with_and_without_format(capsys):
    rows = [{"Width": 1920, "Height": 1080}, {"Width": 3840, "Height": 2160}]
    resolve, parts = session(GetRenderResolutions=lambda *a: rows)
    code, out, _ = run(resolve, capsys, "render", "resolutions", "MP4", "H.264")
    assert code == 0 and out == rows
    assert parts["project"].called("GetRenderResolutions") == [("mp4", "H264")]
    run(resolve, capsys, "render", "resolutions")
    assert parts["project"].called("GetRenderResolutions")[-1] == ()


def test_resolutions_empty_is_an_error(capsys):
    resolve, _ = session(GetRenderResolutions=[])
    code, _, err = run(resolve, capsys, "render", "resolutions", "avi", "x")
    assert code == 1 and "no render resolutions for avi x" in err and "Formats:" in err


# --- render settings ------------------------------------------------------------------


@needs_stub
def test_settings_are_typed_and_applied_one_key_at_a_time(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolve, parts = session()
    code, out, _ = run(resolve, capsys, "render", "settings", "MarkIn=86400", "MarkOut=86639",
                       "ExportAudio=false", "FrameRate=23.976", "VideoQuality=8000", "TargetDir=out")
    assert code == 0
    target = str(tmp_path / "out")
    assert parts["project"].called("SetRenderSettings") == [
        ({"MarkIn": 86400},), ({"MarkOut": 86639},), ({"ExportAudio": False},), ({"FrameRate": 23.976},),
        ({"VideoQuality": 8000},), ({"TargetDir": target},)]
    assert out == {"MarkIn": 86400, "MarkOut": 86639, "ExportAudio": False, "FrameRate": 23.976,
                   "VideoQuality": 8000, "TargetDir": target}


@needs_stub
def test_settings_keep_named_video_quality_as_text(capsys):
    resolve, parts = session()
    code, _, _ = run(resolve, capsys, "render", "settings", "VideoQuality=High")
    assert code == 0 and parts["project"].called("SetRenderSettings") == [({"VideoQuality": "High"},)]


@needs_stub
def test_settings_unknown_key_fails_without_calling_resolve(capsys):
    resolve, parts = session()
    code, _, err = run(resolve, capsys, "render", "settings", "Bogus=1")
    assert code == 1 and "Unknown RenderSettings key(s): Bogus" in err and "MarkIn" in err
    assert "SetRenderSettings" not in names(parts["project"])


@needs_stub
def test_settings_rejected_key_is_an_error_naming_it(capsys):
    resolve, parts = session(SetRenderSettings=lambda s: "SelectAllFrames" not in s)
    code, _, err = run(resolve, capsys, "render", "settings", "SelectAllFrames=false", "MarkIn=10")
    assert code == 1
    assert "rejected render setting(s): SelectAllFrames=False" in err and "Applied: MarkIn" in err
    assert "set MarkIn and MarkOut" in err
    resolve, parts = session(SetRenderSettings=False)
    code, _, err = run(resolve, capsys, "render", "settings", "CustomName=x")
    assert code == 1 and "CustomName='x'" in err and "Applied: none" in err


def test_settings_needs_at_least_one_pair():
    resolve, _ = session()
    with pytest.raises(SystemExit) as exc:
        main(["render", "settings"], resolve=resolve)
    assert exc.value.code == 2


# --- jobs: info, status, delete --------------------------------------------------------


JOBS = [
    {"JobId": "aaaa1111-0000", "TimelineName": "Edit v1", "TargetDir": "/r", "OutputFilename": "edit.mov"},
    {"JobId": "bbbb2222-0000", "TimelineName": "Edit v2", "TargetDir": "/r", "OutputFilename": "v2.mov"},
]


def jobs_session(statuses=None, **returns):
    statuses = statuses or {"aaaa1111-0000": {"JobStatus": "Complete", "CompletionPercentage": 100},
                            "bbbb2222-0000": {"JobStatus": "Ready", "CompletionPercentage": 0}}
    values = {"GetRenderJobList": [dict(job) for job in JOBS], "GetRenderJobStatus": lambda jid: statuses.get(jid),
              "IsRenderingInProgress": False}
    values.update(returns)
    return session(**values)


def test_info_merges_job_details_and_status(capsys):
    resolve, parts = jobs_session()
    code, out, _ = run(resolve, capsys, "--read-only", "render", "info", "bbbb")
    assert code == 0
    assert out == [{**JOBS[1], "JobStatus": "Ready", "CompletionPercentage": 0}]
    assert parts["project"].called("GetRenderJobStatus") == [("bbbb2222-0000",)]
    code, out, _ = run(resolve, capsys, "render", "info")
    assert [row["JobId"] for row in out] == ["aaaa1111-0000", "bbbb2222-0000"]


def test_unknown_or_ambiguous_job_ids_are_errors(capsys):
    resolve, _ = jobs_session()
    code, _, err = run(resolve, capsys, "render", "info", "zzz")
    assert code == 1 and "'zzz' matches no render job" in err and "aaaa1111-0000, bbbb2222-0000" in err
    resolve, _ = jobs_session(GetRenderJobList=[{"JobId": "ab1"}, {"JobId": "ab2"}])
    code, _, err = run(resolve, capsys, "render", "status", "ab")
    assert code == 1 and "matches several render jobs" in err


def test_status_reports_rendering_and_job_status(capsys):
    resolve, parts = jobs_session()
    parts["project"].set(IsRenderingInProgress=True)
    code, out, _ = run(resolve, capsys, "--read-only", "render", "status", "aaaa1111-0000")
    assert code == 0
    assert out == {"rendering": True,
                   "jobs": [{"JobId": "aaaa1111-0000", "JobStatus": "Complete", "CompletionPercentage": 100}]}


def test_status_fails_when_resolve_gives_no_status(capsys):
    resolve, _ = jobs_session(statuses={"aaaa1111-0000": {}})
    code, _, err = run(resolve, capsys, "render", "status", "aaaa")
    assert code == 1 and "no status for render job aaaa1111-0000" in err


def test_delete_jobs_by_id_in_order(capsys):
    resolve, parts = jobs_session()
    code, out, _ = run(resolve, capsys, "render", "delete", "bbbb", "aaaa1111-0000")
    assert code == 0
    assert parts["project"].called("DeleteRenderJob") == [("bbbb2222-0000",), ("aaaa1111-0000",)]
    assert out.startswith("Deleted 2 render job(s)")


def test_delete_refused_is_an_error_and_names_what_was_deleted(capsys):
    resolve, parts = jobs_session(DeleteRenderJob=lambda jid: jid.startswith("aaaa"), IsRenderingInProgress=True)
    code, _, err = run(resolve, capsys, "render", "delete", "aaaa", "bbbb")
    assert code == 1
    assert "refused to delete render job bbbb2222-0000" in err and "Already deleted: aaaa1111-0000" in err
    assert "dava render stop" in err


def test_delete_unknown_id_deletes_nothing(capsys):
    resolve, parts = jobs_session()
    code, _, _ = run(resolve, capsys, "render", "delete", "aaaa", "nope")
    assert code == 1 and "DeleteRenderJob" not in names(parts["project"])


def test_delete_all(capsys):
    resolve, parts = jobs_session()
    code, out, _ = run(resolve, capsys, "render", "delete", "--all")
    assert code == 0 and out == "Deleted 2 render job(s)"
    assert parts["project"].called("DeleteAllRenderJobs") == [()]
    parts["project"].set(DeleteAllRenderJobs=False)
    code, _, err = run(resolve, capsys, "render", "delete", "--all")
    assert code == 1 and "refused to clear the render queue" in err


@pytest.mark.parametrize("argv", [["render", "delete"], ["render", "delete", "--all", "aaaa"]])
def test_delete_needs_ids_or_all_but_not_both(argv):
    resolve, _ = jobs_session()
    with pytest.raises(SystemExit) as exc:
        main(argv, resolve=resolve)
    assert exc.value.code == 2


# --- render wait -------------------------------------------------------------------------


class Clock:
    """Stands in for the time module: sleep advances monotonic() instead of waiting.

    tick: seconds that pass on every monotonic() call (time spent between two clock reads).
    """

    def __init__(self, tick=0.0):
        self.now = 0.0
        self.tick = tick
        self.sleeps = []

    def monotonic(self):
        now = self.now
        self.now += self.tick
        return now

    def sleep(self, seconds):
        # A wait loop that never ends fails the test instead of hanging the suite.
        assert len(self.sleeps) < 1000, "render wait did not stop polling"
        if seconds < 0:  # like time.sleep
            raise ValueError("sleep length must be non-negative")
        self.sleeps.append(seconds)
        self.now += seconds


def sequence(*steps):
    """A GetRenderJobStatus that returns the next step on each call (then keeps the last one)."""
    remaining = list(steps)

    def status(job_id):
        return dict(remaining.pop(0) if len(remaining) > 1 else remaining[0])
    return status


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(render_ext, "time", fake)
    return fake


def test_wait_polls_until_complete_and_shows_progress(capsys, clock):
    resolve, parts = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 40, "EstimatedTimeRemainingInMs": 3000},
        {"JobStatus": "Rendering", "CompletionPercentage": 90},
        {"JobStatus": "Complete", "CompletionPercentage": 100, "TimeTakenToRenderInMs": 5000}))
    code, out, err = run(resolve, capsys, "--read-only", "render", "wait", "aaaa", "--interval", "2")
    assert code == 0
    assert out == [{"JobId": "aaaa1111-0000", "JobStatus": "Complete", "CompletionPercentage": 100,
                    "TimeTakenToRenderInMs": 5000, "OutputPath": "/r/edit.mov"}]
    assert clock.sleeps == [2.0, 2.0]
    assert "aaaa1111 Rendering 40% ~3s left" in err and "Complete 100%" in err
    assert parts["project"].called("GetRenderJobStatus") == [("aaaa1111-0000",)] * 3


def test_wait_quiet_prints_no_progress(capsys, clock):
    resolve, _ = jobs_session()
    code, _, err = run(resolve, capsys, "render", "wait", "aaaa", "--quiet")
    assert code == 0 and err == ""


def test_wait_reports_failed_jobs(capsys, clock):
    resolve, _ = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 10},
        {"JobStatus": "Failed", "CompletionPercentage": 10, "Error": "disk full"}))
    code, _, err = run(resolve, capsys, "render", "wait", "aaaa")
    assert code == 1 and "1 render job(s) did not complete: aaaa1111-0000: Failed disk full" in err


def test_wait_times_out_with_a_message(capsys, clock):
    resolve, _ = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 50}))
    code, _, err = run(resolve, capsys, "render", "wait", "aaaa", "--timeout", "5", "--interval", "1")
    assert code == 1
    assert "Timed out after 5s" in err and "aaaa1111-0000 (Rendering 50%)" in err
    assert clock.now == 5.0


def test_wait_timeout_is_kept_with_a_long_interval(capsys, clock):
    resolve, parts = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 50}))
    code, _, err = run(resolve, capsys, "render", "wait", "aaaa", "--timeout", "5", "--interval", "60")
    assert code == 1 and "Timed out after 5s" in err
    assert clock.sleeps == [5.0] and clock.now == 5.0
    assert len(parts["project"].called("GetRenderJobStatus")) == 2


def test_wait_never_sleeps_a_negative_time_when_the_deadline_passes_after_the_check(capsys, clock):
    # Clock reads: 0.0 (deadline 1.0), 0.6 (check: not yet), 1.2 (time left: -0.2).
    clock.tick = 0.6
    resolve, _ = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 50}))
    code, _, err = run(resolve, capsys, "render", "wait", "aaaa", "--timeout", "1", "--quiet")
    assert code == 1 and "Timed out after 1s" in err
    assert clock.sleeps == [0.0]


def test_wait_without_timeout_keeps_polling(capsys, clock):
    resolve, _ = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        *[{"JobStatus": "Rendering", "CompletionPercentage": p} for p in range(0, 100, 10)],
        {"JobStatus": "Complete", "CompletionPercentage": 100}))
    code, out, _ = run(resolve, capsys, "render", "wait", "aaaa", "--interval", "30")
    assert code == 0 and out[0]["JobStatus"] == "Complete"
    assert clock.sleeps == [30.0] * 10


def test_wait_progress_blanks_out_a_longer_previous_line(capsys, clock):
    resolve, _ = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 40, "EstimatedTimeRemainingInMs": 3000},
        {"JobStatus": "Complete", "CompletionPercentage": 100}))
    code, _, err = run(resolve, capsys, "render", "wait", "aaaa")
    first, second = err.rstrip("\n").split("\r")[1:]
    assert code == 0 and first == "aaaa1111 Rendering 40% ~3s left"
    assert second.rstrip() == "aaaa1111 Complete 100%" and len(second) == len(first)


def test_wait_fails_for_a_job_that_is_not_rendering(capsys, clock):
    resolve, parts = jobs_session()
    code, _, err = run(resolve, capsys, "render", "wait", "bbbb")
    assert code == 1
    assert "bbbb2222-0000 (Ready) have not finished" in err and "dava render start bbbb2222-0000" in err
    assert clock.sleeps == []


def test_wait_rechecks_a_job_that_finished_as_rendering_stopped(capsys, clock):
    resolve, _ = jobs_session(IsRenderingInProgress=False, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 99},
        {"JobStatus": "Complete", "CompletionPercentage": 100}))
    code, out, _ = run(resolve, capsys, "render", "wait", "aaaa")
    assert code == 0 and out[0]["JobStatus"] == "Complete"


@pytest.mark.parametrize("option", [["--timeout", "-1"], ["--timeout", "nan"], ["--interval", "0"],
                                    ["--interval", "-2"], ["--interval", "nan"], ["--interval", "inf"],
                                    ["--interval", "x"]])
def test_wait_rejects_bad_timeout_and_interval(option):
    resolve, parts = jobs_session()
    with pytest.raises(SystemExit) as exc:
        main(["render", "wait", "aaaa", *option], resolve=resolve)
    assert exc.value.code == 2 and parts["project"].calls == []


def test_wait_interval_is_a_float(capsys, clock):
    resolve, _ = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 50}, {"JobStatus": "Complete", "CompletionPercentage": 100}))
    code, _, _ = run(resolve, capsys, "render", "wait", "aaaa", "--interval", "0.25", "-q")
    assert code == 0 and clock.sleeps == [0.25]


# --- presets ---------------------------------------------------------------------------


def preset_session(**project_returns):
    values = {"GetRenderPresetList": ["H.264 Master", "YouTube 1080p", "Mine"],
              "GetQuickExportRenderPresets": ["H.264 Master", "YouTube 1080p"]}
    values.update(project_returns)
    resolve, parts = session(**values)
    resolve.set(GetBurnInPresetList=["Timecode"])
    return resolve, parts


def test_preset_list_marks_quick_export_presets(capsys):
    resolve, _ = preset_session()
    code, out, _ = run(resolve, capsys, "--read-only", "preset", "list")
    assert code == 0
    assert out == [{"name": "H.264 Master", "quick_export": True}, {"name": "YouTube 1080p", "quick_export": True},
                   {"name": "Mine", "quick_export": False}]
    code, out, _ = run(resolve, capsys, "preset", "list", "--burn-in")
    assert out == ["Timecode"]
    assert resolve.called("GetBurnInPresetList") == [()]


@pytest.mark.parametrize("failure", [None, False])
def test_preset_lists_fail_when_resolve_returns_nothing(capsys, tmp_path, failure):
    resolve, _ = preset_session(GetRenderPresetList=failure)
    code, _, err = run(resolve, capsys, "preset", "list")
    assert code == 1 and "did not return the render preset list" in err
    preset_file = tmp_path / "p.xml"
    preset_file.write_text("<preset/>")
    code, _, err = run(resolve, capsys, "preset", "import", str(preset_file))
    assert code == 1 and "did not return the render preset list" in err
    assert resolve.called("ImportRenderPreset") == []

    resolve, _ = preset_session(GetQuickExportRenderPresets=failure)
    code, _, err = run(resolve, capsys, "quick", "list")
    assert code == 1 and "did not return the Quick Export preset list" in err

    resolve, _ = preset_session()
    resolve.set(GetBurnInPresetList=failure)
    code, _, err = run(resolve, capsys, "preset", "list", "--burn-in")
    assert code == 1 and "did not return the data burn-in preset list" in err


@pytest.mark.parametrize("failure", [None, False])
def test_job_commands_fail_when_resolve_returns_no_job_list(capsys, failure):
    resolve, parts = jobs_session(GetRenderJobList=failure)
    for argv in (["render", "info"], ["render", "status"], ["render", "delete", "aaaa"], ["render", "delete", "--all"]):
        code, _, err = run(resolve, capsys, *argv)
        assert code == 1 and "did not return the render job list" in err, argv
    assert "DeleteRenderJob" not in names(parts["project"])
    assert "DeleteAllRenderJobs" not in names(parts["project"])


def test_preset_load_render_and_burn_in(capsys):
    resolve, parts = preset_session()
    assert run(resolve, capsys, "preset", "load", "Mine")[0] == 0
    assert run(resolve, capsys, "preset", "load", "Timecode", "--burn-in")[0] == 0
    assert parts["project"].called("LoadRenderPreset") == [("Mine",)]
    assert parts["project"].called("LoadBurnInPreset") == [("Timecode",)]


def test_preset_load_failure_lists_presets(capsys):
    resolve, _ = preset_session(LoadRenderPreset=False, LoadBurnInPreset=False)
    code, _, err = run(resolve, capsys, "preset", "load", "Nope")
    assert code == 1 and "Could not load render preset 'Nope'" in err and "H.264 Master, YouTube 1080p, Mine" in err
    code, _, err = run(resolve, capsys, "preset", "load", "Nope", "-b")
    assert code == 1 and "data burn-in preset 'Nope'" in err and "Data burn-in presets: Timecode" in err


def test_preset_save_update_delete(capsys):
    resolve, parts = preset_session()
    assert run(resolve, capsys, "preset", "save", "New")[0] == 0
    assert run(resolve, capsys, "preset", "update", "Mine")[0] == 0
    assert run(resolve, capsys, "preset", "delete", "Mine")[0] == 0
    assert run(resolve, capsys, "preset", "delete", "Timecode", "--burn-in")[0] == 0
    project = parts["project"]
    assert project.called("SaveAsNewRenderPreset") == [("New",)]
    assert project.called("UpdateRenderPreset") == [("Mine",)]
    assert project.called("DeleteRenderPreset") == [("Mine",)]
    assert resolve.called("DeleteBurnInPreset") == [("Timecode",)]


def test_preset_save_update_delete_failures(capsys):
    resolve, _ = preset_session(SaveAsNewRenderPreset=False, UpdateRenderPreset=False, DeleteRenderPreset=False)
    resolve.set(DeleteBurnInPreset=False)
    code, _, err = run(resolve, capsys, "preset", "save", "Mine")
    assert code == 1 and "dava preset update Mine" in err
    code, _, err = run(resolve, capsys, "preset", "update", "Gone")
    assert code == 1 and "Could not update render preset 'Gone'" in err
    code, _, err = run(resolve, capsys, "preset", "delete", "Gone")
    assert code == 1 and "Could not delete render preset 'Gone'" in err
    code, _, err = run(resolve, capsys, "preset", "delete", "Gone", "-b")
    assert code == 1 and "Could not delete data burn-in preset 'Gone'" in err


def test_preset_import_reports_new_presets(capsys, tmp_path):
    preset_file = tmp_path / "Social.xml"
    preset_file.write_text("<preset/>")
    resolve, parts = preset_session()
    lists = iter([["Mine"], ["Mine", "Social"]])
    parts["project"].set(GetRenderPresetList=lambda: next(lists))
    code, out, _ = run(resolve, capsys, "preset", "import", str(preset_file))
    assert code == 0
    assert resolve.called("ImportRenderPreset") == [(str(preset_file),)]
    assert out == {"imported": str(preset_file), "kind": "render", "new_presets": ["Social"]}


def test_preset_import_burn_in_and_failures(capsys, tmp_path):
    preset_file = tmp_path / "tc.xml"
    preset_file.write_text("<preset/>")
    resolve, _ = preset_session()
    lists = iter([["Timecode"], ["Timecode", "tc"]])
    resolve.set(GetBurnInPresetList=lambda: next(lists))
    code, out, _ = run(resolve, capsys, "preset", "import", str(preset_file), "--burn-in")
    assert code == 0 and out["new_presets"] == ["tc"]
    assert resolve.called("ImportBurnInPreset") == [(str(preset_file),)]

    resolve, _ = preset_session()
    resolve.set(ImportRenderPreset=False)
    code, _, err = run(resolve, capsys, "preset", "import", str(preset_file))
    assert code == 1 and "could not import render preset file" in err

    code, _, err = run(resolve, capsys, "preset", "import", str(tmp_path / "missing.xml"))
    assert code == 1 and "Preset file not found" in err
    assert len(resolve.called("ImportRenderPreset")) == 1


def test_preset_export(capsys, tmp_path):
    resolve, _ = preset_session()
    target = tmp_path / "mine.xml"
    assert run(resolve, capsys, "preset", "export", "Mine", str(target))[0] == 0
    assert run(resolve, capsys, "preset", "export", "Timecode", str(target), "--burn-in")[0] == 0
    assert resolve.called("ExportRenderPreset") == [("Mine", str(target))]
    assert resolve.called("ExportBurnInPreset") == [("Timecode", str(target))]


def test_preset_paths_reach_resolve_absolute(capsys, tmp_path, monkeypatch):
    # Resolve runs in another process with another working directory: relative and ~ paths must be expanded.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "in.xml").write_text("<preset/>")
    resolve, _ = preset_session()
    assert run(resolve, capsys, "preset", "export", "Mine", "out.xml")[0] == 0
    assert run(resolve, capsys, "preset", "export", "Timecode", "~/tc.xml", "-b")[0] == 0
    assert run(resolve, capsys, "preset", "import", "in.xml")[0] == 0
    assert run(resolve, capsys, "preset", "import", "~/in.xml", "-b")[0] == 0
    assert resolve.called("ExportRenderPreset") == [("Mine", str(tmp_path / "out.xml"))]
    assert resolve.called("ExportBurnInPreset") == [("Timecode", str(tmp_path / "tc.xml"))]
    assert resolve.called("ImportRenderPreset") == [(str(tmp_path / "in.xml"),)]
    assert resolve.called("ImportBurnInPreset") == [(str(tmp_path / "in.xml"),)]


def test_preset_export_failures(capsys, tmp_path):
    resolve, _ = preset_session()
    code, _, err = run(resolve, capsys, "preset", "export", "Mine", str(tmp_path / "no" / "such" / "x.xml"))
    assert code == 1 and "Folder not found" in err and resolve.called("ExportRenderPreset") == []
    resolve.set(ExportRenderPreset=False)
    code, _, err = run(resolve, capsys, "preset", "export", "Nope", str(tmp_path / "x.xml"))
    assert code == 1 and "Could not export render preset 'Nope'" in err and "Render presets: H.264 Master" in err


# --- quick export ------------------------------------------------------------------------


def test_quick_list(capsys):
    resolve, _ = preset_session()
    code, out, _ = run(resolve, capsys, "--read-only", "quick", "list")
    assert code == 0 and out == ["H.264 Master", "YouTube 1080p"]


def test_quick_export_passes_settings_and_returns_status(capsys, tmp_path):
    done = {"JobStatus": "Render Complete", "CompletionPercentage": 100, "TimeTakenToRenderInMs": 900}
    resolve, parts = preset_session(RenderWithQuickExport=lambda name, info: done)
    code, out, _ = run(resolve, capsys, "quick", "export", "H.264 Master", "-o", str(tmp_path), "-n", "cut",
                       "--quality", "8000", "--upload")
    assert code == 0
    settings = {"TargetDir": str(tmp_path), "CustomName": "cut", "VideoQuality": 8000, "EnableUpload": True}
    assert parts["project"].called("RenderWithQuickExport") == [("H.264 Master", settings)]
    assert out == {"preset": "H.264 Master", "timeline": "Edit v1", "settings": settings, **done}
    assert "SetCurrentTimeline" not in names(parts["project"])


def test_quick_export_switches_timeline_when_asked(capsys):
    done = {"JobStatus": "Render Complete"}
    resolve, parts = preset_session(RenderWithQuickExport=lambda name, info: done)
    code, _, _ = run(resolve, capsys, "quick", "export", "H.264 Master", "-t", "Edit v1")
    assert code == 0
    assert parts["project"].called("SetCurrentTimeline") == [(parts["timeline"],)]
    assert parts["project"].called("RenderWithQuickExport") == [("H.264 Master", {})]
    code, _, err = run(resolve, capsys, "quick", "export", "H.264 Master", "-t", "Nope")
    assert code == 1 and "No timeline named 'Nope'" in err


def test_quick_export_failures(capsys):
    resolve, _ = preset_session(RenderWithQuickExport=None)
    code, _, err = run(resolve, capsys, "quick", "export", "Bogus")
    assert code == 1 and "did not run" in err and "Quick Export presets: H.264 Master, YouTube 1080p" in err
    failed = {"JobStatus": "Render Failed", "Error": "codec unavailable"}
    resolve, _ = preset_session(RenderWithQuickExport=lambda name, info: failed)
    code, _, err = run(resolve, capsys, "quick", "export", "H.264 Master")
    assert code == 1 and "ended with 'Render Failed': codec unavailable" in err


def test_quick_enable_and_disable(capsys):
    resolve, parts = preset_session()
    assert run(resolve, capsys, "quick", "enable", "Mine")[0] == 0
    assert run(resolve, capsys, "quick", "disable", "Mine")[0] == 0
    assert parts["project"].called("SetQuickExportEnabledForRenderPreset") == [("Mine", True), ("Mine", False)]
    parts["project"].set(SetQuickExportEnabledForRenderPreset=False)
    code, _, err = run(resolve, capsys, "quick", "enable", "Nope")
    assert code == 1 and "Could not enable Quick Export for render preset 'Nope'" in err


# --- declarations ------------------------------------------------------------------------


OWN_GROUPS = {"preset", "quick"}
OWN_RENDER_ACTIONS = {"formats", "codecs", "format", "mode", "resolutions", "settings", "info", "status",
                      "delete", "wait"}


def own_leaves():
    for words, leaf, help_text in leaf_parsers(build_parser()):
        if words[0] in OWN_GROUPS or (words[0] == "render" and words[1] in OWN_RENDER_ACTIONS):
            yield words, leaf, help_text


def test_every_command_declares_handler_read_only_and_ascii_help():
    leaves = list(own_leaves())
    assert len(leaves) == len(OWN_RENDER_ACTIONS) + 7 + 4
    for words, leaf, help_text in leaves:
        assert leaf._defaults["func"].__module__ == render_ext.__name__, words
        assert "read_only" in leaf._defaults, words
        texts = [help_text] + [action.help or "" for action in leaf._actions]
        assert all(text.isascii() for text in texts), words


def mcp_server(resolve):
    with open(MINI_SPEC) as handle:
        return Server(session=Session(resolve=resolve, spec=Spec(handle.read(), MINI_SPEC)))


def mcp_call(server, name, arguments):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    result = server.handle(message)["result"]
    return result["isError"], result["content"][0]["text"]


def test_mcp_tools_offer_codecs_as_a_boolean_and_interval_as_a_number(clock):
    resolve, parts = jobs_session(IsRenderingInProgress=True, GetRenderJobStatus=sequence(
        {"JobStatus": "Rendering", "CompletionPercentage": 50}, {"JobStatus": "Complete", "CompletionPercentage": 100}))
    server = mcp_server(resolve)
    tools = {t["name"]: t for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert tools["dava_render_formats"]["inputSchema"]["properties"]["codecs"]["type"] == "boolean"
    assert tools["dava_render_wait"]["inputSchema"]["properties"]["interval"]["type"] == "number"

    is_error, text = mcp_call(server, "dava_render_formats", {"codecs": True})
    assert not is_error, text
    assert json.loads(text)[0] == {"format": "MP4", "extension": "mp4",
                                   "codecs": [{"codec": "H264", "description": "H.264"}]}
    is_error, text = mcp_call(server, "dava_render_wait", {"job_ids": ["aaaa"], "interval": 0.5, "quiet": True})
    assert not is_error, text
    assert json.loads(text)[0]["JobStatus"] == "Complete" and clock.sleeps == [0.5]
    is_error, text = mcp_call(server, "dava_render_wait", {"job_ids": ["aaaa"], "interval": 0})
    assert is_error and "more than 0" in text


@pytest.mark.parametrize("argv", [
    ["render", "settings", "MarkIn=1"], ["render", "delete", "--all"], ["render", "mode", "single"],
    ["preset", "load", "Mine"], ["preset", "save", "x"], ["preset", "update", "x"], ["preset", "delete", "x"],
    ["preset", "export", "x", "/tmp/x.xml"], ["preset", "import", "/tmp/x.xml"], ["quick", "export", "x"], ["quick", "enable", "x"],
    ["quick", "disable", "x"],
])
def test_mutating_commands_are_blocked_in_read_only_mode(capsys, argv):
    resolve, parts = preset_session()
    code, _, err = run(resolve, capsys, "--read-only", *argv)
    assert code == 1 and "read-only mode is on" in err
    assert parts["project"].calls == [] and resolve.calls == []
