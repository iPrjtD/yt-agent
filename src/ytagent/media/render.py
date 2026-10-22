"""Shorts renderer (FFmpeg): per-beat 9:16 segments -> concat -> captions, mascot, loudness-normalised voice."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ytagent.brand import FONTS
from ytagent.brand import OUT as BRAND_DIR
from ytagent.config import get_settings
from ytagent.media.voice import Word

log = logging.getLogger(__name__)

HONEY_ASS = "&H0000C3FF&"  # #FFC300 in ASS BGR order
WHITE_ASS = "&H00FFFFFF&"


@dataclass
class Segment:
    source: Path          # video clip or still image
    duration: float
    still: bool = False


MUSIC_DIR = FONTS.parent / "music"
MUSIC_VOLUME = 0.10  # music bed level under the voice (before loudness normalisation)


def pick_music(seed: int, mood: str = "") -> Path | None:
    """A royalty-free track from assets/music/<mood>/ (or assets/music/), rotated by video id."""
    def tracks_in(folder: Path) -> list[Path]:
        return sorted(p for p in folder.glob("*") if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"})

    tracks = (tracks_in(MUSIC_DIR / mood) if mood else []) or tracks_in(MUSIC_DIR)
    return tracks[seed % len(tracks)] if tracks else None


def ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    binary = get_settings().env.ffmpeg_binary
    cmd = [binary, "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-800:]}")


def probe(path: Path) -> dict:
    binary = Path(get_settings().env.ffmpeg_binary)
    ffprobe = str(binary.with_name("ffprobe" + binary.suffix)) if binary.parent != Path(".") else "ffprobe"
    out = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _ts(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def caption_chunks(words: list[Word], max_words: int = 3, max_chars: int = 16) -> list[list[Word]]:
    """Group words into short on-screen chunks; break at line ends and after punctuation."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.line != cur[-1].line
                    or len(" ".join(x.text for x in [*cur, w])) > max_chars):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?,:;]$", w.text):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[Word], labels: list[tuple[float, float, str]], width: int, height: int) -> str:
    """Karaoke-style captions (current word in honey) plus top labels."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Poppins ExtraBold,118,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,9,4,2,50,50,600,1
Style: Label,Poppins Black,74,&H0000C3FF,&H0000C3FF,&H00141414,&H00141414,0,0,0,0,100,100,0,0,3,18,0,8,70,70,310,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for start, end, text in labels:
        if text:
            events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Label,,0,0,0,,{_escape(text.upper())}")
    chunks = caption_chunks(words)
    for ci, chunk in enumerate(chunks):
        chunk_end = chunks[ci + 1][0].start if ci + 1 < len(chunks) else chunk[-1].end + 0.3
        for wi, word in enumerate(chunk):
            w_end = chunk[wi + 1].start if wi + 1 < len(chunk) else chunk_end
            if w_end <= word.start:
                continue
            text = " ".join(
                (f"{{\\c{HONEY_ASS}}}{_escape(w.text.upper())}{{\\c{WHITE_ASS}}}" if j == wi else _escape(w.text.upper()))
                for j, w in enumerate(chunk))
            events.append(f"Dialogue: 0,{_ts(word.start)},{_ts(w_end)},Caption,,0,0,0,,{text}")
    return head + "\n".join(events) + "\n"


def _segment(seg: Segment, out: Path, width: int, height: int, fps: int) -> None:
    if seg.still:
        frames = max(1, round(seg.duration * fps))
        # Cover-crop to 9:16 at 2x, then a slow Ken Burns zoom (photos and cards).
        vf = (f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,crop={width * 2}:{height * 2},"
              f"zoompan=z='min(zoom+0.0007,1.08)':d={frames}"
              f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},format=yuv420p")
        ffmpeg(["-loop", "1", "-i", str(seg.source), "-t", f"{seg.duration:.3f}", "-vf", vf, "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
        return
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,"
          f"fps={fps},eq=brightness=-0.05:saturation=1.08,format=yuv420p")
    ffmpeg(["-stream_loop", "-1", "-i", str(seg.source), "-t", f"{seg.duration:.3f}", "-vf", vf, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


def render_short(segments: list[Segment], voice_wav: Path, words: list[Word], work: Path,
                 labels: list[tuple[float, float, str]], music: Path | None = None) -> Path:
    cfg = get_settings().video
    w, h, fps = cfg.width, cfg.height, cfg.fps
    parts = []
    for i, seg in enumerate(segments):
        part = work / f"seg_{i:02d}.mp4"
        _segment(seg, part, w, h, fps)
        parts.append(part)
    (work / "concat.txt").write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    ffmpeg(["-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "video_only.mp4"], cwd=work)

    total = sum(seg.duration for seg in segments)
    (work / "captions.ass").write_text(build_ass(words, labels, w, h), encoding="utf-8")
    fonts = work / "fonts"
    shutil.copytree(FONTS, fonts, dirs_exist_ok=True)
    shutil.copy(BRAND_DIR / "mascot.png", work / "mascot.png")
    if voice_wav.resolve() != (work / "voice.wav").resolve():
        shutil.copy(voice_wav, work / "voice.wav")

    # The badge is looped for the whole video: a single-frame overlay can drop out after concat timestamp jumps.
    inputs = ["-i", "video_only.mp4", "-i", "voice.wav", "-loop", "1", "-i", "mascot.png"]
    graph = ("[2:v]scale=170:-1[m];[0:v][m]overlay=36:90:shortest=1[v1];"
             "[v1]ass=captions.ass:fontsdir=fonts[v];")
    if music:
        shutil.copy(music, work / f"music{music.suffix}")
        inputs += ["-stream_loop", "-1", "-i", f"music{music.suffix}"]
        fade_at = max(0.0, total - 1.5)
        graph += (f"[3:a]volume={MUSIC_VOLUME},afade=t=in:d=0.5,afade=t=out:st={fade_at:.2f}:d=1.5[bg];"
                  "[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                  "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")
    else:
        graph += "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    final = work / "final.mp4"
    ffmpeg([*inputs, "-filter_complex", graph,
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", final.name], cwd=work)
    return final


def qa(final: Path) -> list[str]:
    """Return problems found (empty list = pass)."""
    cfg = get_settings().video
    info = probe(final)
    problems = []
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    duration = float(info["format"]["duration"])
    if not video or (video["width"], video["height"]) != (cfg.width, cfg.height):
        problems.append(f"resolution {video and (video['width'], video['height'])} != {cfg.width}x{cfg.height}")
    if not audio:
        problems.append("no audio track")
    if duration > cfg.max_short_seconds + 0.5:
        problems.append(f"duration {duration:.1f}s > {cfg.max_short_seconds}s")
    if duration < 15:
        problems.append(f"duration {duration:.1f}s is too short")
    return problems
