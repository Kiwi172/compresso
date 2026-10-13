"""End-to-end tests. Video tests skip automatically if ffmpeg is unavailable."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from compresso.cli import main
from compresso.discovery import walk_sources, output_path
from compresso.images import compress_image

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "in"
    (root / "sub").mkdir(parents=True)
    for p, color in [(root / "a.jpg", "red"), (root / "sub" / "b.png", "blue")]:
        im = Image.new("RGB", (800, 600), color)
        # noisy content so compression actually shrinks
        px = im.load()
        for x in range(0, 800, 7):
            for y in range(0, 600, 7):
                px[x, y] = ((x * 3) % 256, (y * 5) % 256, (x + y) % 256)
        im.save(p, quality=98)
    return root


def test_walk_skips_sqz_outputs(tree):
    out = tree / "a.sqz.jpg"
    out.write_bytes(b"x")
    kinds = [f.name for _, f in walk_sources(tree)]
    assert "a.jpg" in kinds and "b.png" in kinds
    assert "a.sqz.jpg" not in kinds


def test_output_path_preserves_structure(tree, tmp_path):
    src = tree / "sub" / "b.png"
    o = output_path(src, tree, tmp_path / "out", ".png")
    assert o == tmp_path / "out" / "sub" / "b.sqz.png"


def test_compress_image_smaller(tree, tmp_path):
    src = tree / "a.jpg"
    dst = tmp_path / "out" / "a.sqz.jpg"
    r = compress_image(src, dst, "auto", "medium")
    assert r.out_path == dst and r.out_size < r.in_size


def test_no_regression_guard(tree, tmp_path):
    # A tiny 1x1 image: any re-encode is likely larger -> guard must skip.
    src = tmp_path / "tiny.png"
    Image.new("RGB", (1, 1), "white").save(src)
    dst = tmp_path / "out" / "tiny.sqz.png"
    r = compress_image(src, dst, "auto", "medium")
    assert r.out_path is None and "larger" in r.note
    assert not dst.exists()


def test_cli_end_to_end(tree, tmp_path, capsys):
    rc = main([str(tree), "-o", str(tmp_path / "out"), "--images"])
    assert rc == 0
    assert (tmp_path / "out" / "a.sqz.jpg").exists()
    assert (tmp_path / "out" / "sub" / "b.sqz.png").exists()
    # originals untouched
    assert (tree / "a.jpg").exists()
    # idempotent re-run: outputs are skipped
    out = capsys.readouterr().out
    assert "Done: 2 compressed" in out


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_video_roundtrip(tmp_path):
    src = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", "-qp", "10", str(src)],
        check=True)
    dst = tmp_path / "out" / "clip.sqz.mp4"
    from compresso.videos import compress_video, available_encoders
    r = compress_video(src, dst, "high", "h264", force=True,
                       encoders=available_encoders())
    assert r.out_path == dst
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(dst)],
        capture_output=True, text=True).stdout)
    assert info["streams"][0]["codec_name"] == "h264"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_video_skip_efficient(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=5",
         "-c:v", "libx264", str(src)], check=True)
    from compresso.videos import compress_video, available_encoders
    r = compress_video(src, tmp_path / "out" / "clip.sqz.mp4",
                       encoders=available_encoders())
    assert r.out_path is None and "already efficient" in r.note


# ---- new-feature tests ----

def test_filters_exclude_and_max_depth(tree):
    names = [f.name for _, f in walk_sources(tree, exclude=["sub/*"])]
    assert "a.jpg" in names and "b.png" not in names
    shallow = [f.name for _, f in walk_sources(tree, max_depth=0)]
    assert "b.png" not in shallow


def test_size_filters(tree):
    big = [f.name for _, f in walk_sources(tree, min_size=10_000)]
    small = [f.name for _, f in walk_sources(tree, max_size=100)]
    assert "a.jpg" in big and small == []


def test_since_filter(tree):
    import time
    future = [f.name for _, f in walk_sources(tree, since=time.time() + 3600)]
    assert future == []


def test_include_glob(tree):
    names = [f.name for _, f in walk_sources(tree, include=["*.jpg"])]
    assert names == ["a.jpg"]


def test_custom_marker(tree):
    (tree / "a.cmp.jpg").write_bytes(b"x")
    names = [f.name for _, f in walk_sources(tree, marker=".cmp")]
    assert "a.cmp.jpg" not in names and "a.jpg" in names


def test_parse_since_relative():
    import time
    from compresso.discovery import parse_since
    now = time.time()
    assert abs((now - parse_since("1d")) - 86400) < 5
    assert parse_since("2020-01-01") < now


def test_engine_jobs_and_report(tree, tmp_path):
    from compresso.engine import run_job
    rep = tmp_path / "rep.json"
    s = run_job({"paths": [str(tree)], "output": str(tmp_path / "out"),
                 "images": True, "videos": False, "jobs": 4,
                 "report": str(rep)})
    assert s["compressed"] == 2 and s["failed"] == 0
    assert json.loads(rep.read_text())["compressed"] == 2
    assert ".jpg" in s["by_ext"] and ".png" in s["by_ext"]


def test_engine_resume(tree, tmp_path):
    from compresso.engine import run_job
    opts = {"paths": [str(tree)], "output": str(tmp_path / "out"),
            "images": True, "videos": False, "resume": True}
    s1 = run_job(opts)
    assert s1["compressed"] == 2
    s2 = run_job(opts)
    assert s2["compressed"] == 0 and s2["skipped"] == 2


def test_engine_dedupe(tmp_path):
    from compresso.engine import run_job
    root = tmp_path / "dup"
    root.mkdir()
    data = (root / "x.jpg")
    im = Image.new("RGB", (400, 400))
    im.putdata([(i % 256, (i * 7) % 256, (i * 13) % 256) for i in range(400 * 400)])
    im.save(data, quality=98)
    (root / "y.jpg").write_bytes(data.read_bytes())
    s = run_job({"paths": [str(root)], "output": str(tmp_path / "out"),
                 "images": True, "videos": False, "dedupe": True})
    assert s["total"] == 1  # second identical file dropped


def test_preset_applies(tree, tmp_path):
    from compresso.engine import run_job
    s = run_job({"paths": [str(tree)], "output": str(tmp_path / "out"),
                 "images": True, "videos": False, "preset": "email"})
    assert s["compressed"] >= 1


def test_csv_report(tree, tmp_path):
    from compresso.engine import run_job
    csvp = tmp_path / "rep.csv"
    run_job({"paths": [str(tree)], "output": str(tmp_path / "out"),
             "images": True, "videos": False, "csv": str(csvp)})
    lines = csvp.read_text().splitlines()
    assert lines[0].startswith("file,kind,status") and len(lines) == 3


def test_verify_flag(tree, tmp_path):
    from compresso.engine import run_job
    s = run_job({"paths": [str(tree)], "output": str(tmp_path / "out"),
                 "images": True, "videos": False, "verify": True})
    assert s["compressed"] == 2 and s["failed"] == 0


def test_keep_mtime(tree, tmp_path):
    from compresso.engine import run_job
    src = tree / "a.jpg"
    mt = src.stat().st_mtime
    run_job({"paths": [str(tree)], "output": str(tmp_path / "out"),
             "images": True, "videos": False, "keep_mtime": True})
    out = tmp_path / "out" / "a.sqz.jpg"
    assert abs(out.stat().st_mtime - mt) < 2


def test_png_palette_mode(tmp_path):
    import random
    from compresso.images import compress_image
    src = tmp_path / "p.png"
    rng = random.Random(42)
    im = Image.new("RGB", (300, 300))
    im.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                for _ in range(300 * 300)])
    im.save(src)
    r = compress_image(src, tmp_path / "o.sqz.png", "png", "medium",
                       force=True, png_mode="palette")
    assert r.out_path and r.out_size < r.in_size


def test_numeric_quality(tree, tmp_path):
    from compresso.images import compress_image
    r1 = compress_image(tree / "a.jpg", tmp_path / "q1.sqz.jpg", "jpeg", 30, force=True)
    r2 = compress_image(tree / "a.jpg", tmp_path / "q2.sqz.jpg", "jpeg", 90, force=True)
    assert r1.out_size < r2.out_size


def test_cli_new_flags(tree, tmp_path, capsys):
    rc = main([str(tree), "-o", str(tmp_path / "out2"), "--images",
               "--exclude", "sub/*", "--jobs", "2", "--verify",
               "--csv", str(tmp_path / "r.csv")])
    assert rc == 0
    assert (tmp_path / "out2" / "a.sqz.jpg").exists()
    assert not (tmp_path / "out2" / "sub" / "b.sqz.png").exists()
    assert (tmp_path / "r.csv").exists()


def test_config_file(tmp_path, tree):
    from compresso.config import load_config
    cfg = tmp_path / "c.json"
    cfg.write_text('{"quality": "high", "jobs": 2}')
    o = load_config(str(cfg))
    assert o == {"quality": "high", "jobs": 2}


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_video_hevc_and_bitrate(tmp_path):
    src = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", "-qp", "10", str(src)], check=True)
    from compresso.videos import compress_video, available_encoders
    r = compress_video(src, tmp_path / "out" / "c.sqz.mp4", "high", "hevc",
                       force=True, bitrate="500k", encoders=available_encoders())
    assert r.out_path
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(r.out_path)],
        capture_output=True, text=True).stdout)
    assert info["streams"][0]["codec_name"] == "hevc"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_video_trim(tmp_path):
    src = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=6:size=160x120:rate=10",
         "-c:v", "libx264", str(src)], check=True)
    from compresso.videos import compress_video, available_encoders
    r = compress_video(src, tmp_path / "out" / "c.sqz.mp4", "high", "h264",
                       trim="1s-2s", encoders=available_encoders())
    assert r.out_path
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(r.out_path)],
        capture_output=True, text=True).stdout.strip())
    assert 0.5 < dur < 2.0  # "1s-2s" = start at 1s, end at 2s -> ~1s clip


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_keep_awake_and_notify_smoke():
    from compresso.engine import KeepAwake, notify
    with KeepAwake():
        pass
    notify("test", "test")  # must not raise
