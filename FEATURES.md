# Compresso — 50 features

All implemented and exercised by the test suite or CLI.

## Core compression
1. **Batch image compression** — JPEG, PNG, WebP, HEIC/HEIF, TIFF, BMP in one pass
2. **Batch video compression** — MP4, MOV, MKV, AVI, WebM, WMV, TS, MPEG in one pass
3. **GPU-accelerated video** — NVIDIA NVENC, Intel QSV, AMD AMF, VAAPI, Vulkan
4. **Automatic encoder fallback chain** — hardware → software (x264 / x265 / SVT-AV1) when a GPU encoder fails at runtime
5. **Three video codecs** — H.264, H.265/HEVC, AV1 (all to MP4)
6. **Image format conversion** — HEIC→JPEG, any→JPEG/PNG/WebP/AVIF
7. **Quality tiers** — low / medium / high
8. **Numeric quality** — any value 1–100 (`-q 63`)
9. **Target video bitrate** — `--bitrate 2M` overrides quality tiers
10. **Audio bitrate re-encode** — `--audio-bitrate 128k`
11. **Drop audio** — `--no-audio`
12. **Video trimming** — `--trim 5s` or `--trim 1:02-1:30`
13. **HDR → SDR tone-mapping** — PQ/HLG inputs converted for sharing
14. **Resize / downscale** — `--max-edge 1920` for images and videos
15. **PNG palette quantization** — `--png-mode palette` for 256-colour PNG wins
16. **EXIF orientation honoured, metadata stripped by default** — `--keep-exif` to retain
17. **No-regression guard** — if the result would be larger, the original is kept and no output written
18. **Already-efficient video skip** — H.264/HEVC/AV1/VP9 sources skipped unless `--force`

## Folder & file handling
19. **Recursive folder walking** — subfolders included automatically
20. **Output tree mirrors input structure** — parallel tree, originals never modified
21. **Idempotent re-runs** — `.sqz` marker in output names; produced files are never re-processed
22. **Custom marker** — `--marker .cmp`
23. **Glob include / exclude filters** — `--include '*.jpg' --exclude 'thumbnails/*'`
24. **Size filters** — `--min-size 10k`, `--max-size 500m`
25. **Date filter** — `--since 7d` / `--since 2026-01-01`
26. **Depth limit** — `--max-depth 1`
27. **Hidden files option** — `--include-hidden`
28. **Symlink following** — `--follow-symlinks` (loop-safe)
29. **Duplicate-content detection** — `--dedupe` compresses one copy of identical files
30. **Resume / checkpointing** — `--resume` writes `.compresso-state.json`; interrupted runs skip finished files

## Performance & robustness
31. **Parallel processing** — `--jobs 8`
32. **Output verification** — `--verify` decodes every result to prove it isn't corrupt
33. **Keep modification times** — `--keep-mtime`
34. **Keep-awake during long runs** — systemd-inhibit (Linux) / SetThreadExecutionState (Windows)
35. **Dry run** — `--dry-run` lists planned work, touches nothing
36. **Desktop notification on completion** — `--notify` (+ terminal bell)

## Interfaces
37. **Full CLI** — every feature above available as a flag
38. **Graphical UI in a separate window** — `compresso --ui` (pywebview, or Edge/Chrome app-mode, or browser fallback)
39. **Live progress in the UI** — progress bar, per-file table, per-extension savings chips
40. **Cancel a running job from the UI** — Stop button
41. **Environment doctor** — `--doctor` reports ffmpeg, HEIC support, and every usable encoder
42. **JSON + CSV reports** — `--report out.json --csv out.csv`
43. **Presets** — `--preset email|web|archive`
44. **Config file** — TOML/JSON defaults (`~/.config/compresso/config.toml`), flags win
45. **Watch mode** — `--watch --interval 10` processes new files as they appear
46. **Cross-platform** — Linux and Windows, identical behaviour; CI tests both
47. **Zero heavy dependencies** — Pillow + pillow-heif + ffmpeg; UI is stdlib
48. **Optional native window extra** — `pipx install "compresso[ui]"` for pywebview
49. **Test suite** — 27 tests covering engine, filters, reports, video round-trips
50. **Installable one-liner** — `pipx install compresso` gives `compresso` on PATH on both OSes
