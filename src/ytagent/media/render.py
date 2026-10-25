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
from ytagent.proc import NO_WINDOW

log = logging.getLogger(__name__)

HONEY_ASS = "&H0000C3FF&"  # #FFC300 in ASS BGR order
WHITE_ASS = "&H00FFFFFF&"


@dataclass
class Segment:
    source: Path          # video clip or still image
    duration: float
    still: bool = False
    fit: bool = False     # show the whole frame over a blurred copy (archive film, 4:3) instead of cropping


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
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-800:]}")


def probe(path: Path) -> dict:
    binary = Path(get_settings().env.ffmpeg_binary)
    ffprobe = str(binary.with_name("ffprobe" + binary.suffix)) if binary.parent != Path(".") else "ffprobe"
    out = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True, creationflags=NO_WINDOW)
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


def build_ass(words: list[Word], labels: list[tuple[float, float, str]], width: int, height: int,
              memes: list[tuple[float, float, str, str]] = (), buzz_lines: frozenset[int] = frozenset()) -> str:
    """Karaoke-style captions (current word in honey; Buzzbee's lines in honey with white highlight),
    top labels, and classic meme captions (black text on a white box)."""
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
Style: Meme,Poppins ExtraBold,84,&H00141414,&H00141414,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,3,24,0,8,90,90,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    meme_spans = [(s, e) for s, e, *_ in memes]
    for start, end, text in labels:
        # A meme caption replaces the label while it is on screen.
        if text and not any(s < end and start < e for s, e in meme_spans):
            events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Label,,0,0,0,,{_escape(text.upper())}")
    for start, end, top, bottom in memes:
        text = "\\N".join(_escape(t) for t in (top, bottom) if t)
        events.append(f"Dialogue: 2,{_ts(start)},{_ts(end)},Meme,,0,0,0,,{text}")
    chunks = caption_chunks(words)
    for ci, chunk in enumerate(chunks):
        chunk_end = chunks[ci + 1][0].start if ci + 1 < len(chunks) else chunk[-1].end + 0.3
        buzz = chunk[0].line in buzz_lines
        base, hot = (HONEY_ASS, WHITE_ASS) if buzz else (WHITE_ASS, HONEY_ASS)
        for wi, word in enumerate(chunk):
            w_end = chunk[wi + 1].start if wi + 1 < len(chunk) else chunk_end
            if w_end <= word.start:
                continue
            text = " ".join(
                (f"{{\\c{hot}}}{_escape(w.text.upper())}{{\\c{base}}}" if j == wi else _escape(w.text.upper()))
                for j, w in enumerate(chunk))
            events.append(f"Dialogue: 0,{_ts(word.start)},{_ts(w_end)},Caption,,0,0,0,,{{\\c{base}}}{text}")
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
    if seg.fit:
        # Whole frame, centred, over a blurred and darkened full-screen copy of itself.
        graph = (f"[0:v]split[a][b];"
                 f"[a]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                 f"boxblur=24:2,eq=brightness=-0.18[bg];"
                 f"[b]scale={width}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-120,setsar=1,fps={fps},format=yuv420p")
        ffmpeg(["-stream_loop", "-1", "-i", str(seg.source), "-t", f"{seg.duration:.3f}", "-filter_complex", graph,
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])
        return
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,"
          f"fps={fps},eq=brightness=-0.05:saturation=1.08,format=yuv420p")
    ffmpeg(["-stream_loop", "-1", "-i", str(seg.source), "-t", f"{seg.duration:.3f}", "-vf", vf, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


REACTION_XY = (560, 640)   # where Buzzbee pops up (480 px canvas): right side, above captions
SFX_VOLUME = 0.45


def render_short(segments: list[Segment], voice_wav: Path, words: list[Word], work: Path,
                 labels: list[tuple[float, float, str]], music: Path | None = None,
                 memes: list[tuple[float, float, str, str]] = (), buzz_lines: frozenset[int] = frozenset(),
                 reactions: list = (), sfx: list[tuple[float, Path]] = (), host_dir: Path | None = None) -> Path:
    """`reactions` are media.reactions.Reaction with rendered frames; `sfx` are (time, file) pairs;
    `host_dir` holds the talking corner-host frames (else a static badge)."""
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
    (work / "captions.ass").write_text(build_ass(words, labels, w, h, memes, buzz_lines), encoding="utf-8")
    fonts = work / "fonts"
    shutil.copytree(FONTS, fonts, dirs_exist_ok=True)
    shutil.copy(BRAND_DIR / "mascot.png", work / "mascot.png")
    if voice_wav.resolve() != (work / "voice.wav").resolve():
        shutil.copy(voice_wav, work / "voice.wav")

    # Video effects and the audio mix run as SEPARATE FFmpeg passes, then get muxed: in one combined
    # filter graph FFmpeg silently dropped ~20% of the audio (the "voice breaking" bug).
    if host_dir:  # talking, blinking host for the whole video
        v_inputs = ["-i", "video_only.mp4", "-framerate", str(fps), "-i", str(Path(host_dir) / "%05d.png")]
        vgraph = "[0:v][1:v]overlay=20:70:eof_action=repeat[v0];"
    else:  # static badge, looped (a single-frame overlay can drop out after concat timestamp jumps)
        v_inputs = ["-i", "video_only.mp4", "-loop", "1", "-i", "mascot.png"]
        vgraph = "[1:v]scale=170:-1[m];[0:v][m]overlay=36:90:shortest=1[v0];"
    n_in = 2
    # Buzzbee reactions: PNG sequences shifted to their start time; captions are drawn last, on top.
    last = "v0"
    for j, r in enumerate(reactions):
        v_inputs += ["-framerate", str(fps), "-i", str(Path(r.frames_dir) / "%04d.png")]
        x, y = REACTION_XY
        vgraph += (f"[{n_in}:v]setpts=PTS-STARTPTS+{r.start:.3f}/TB[r{j}];"
                   f"[{last}][r{j}]overlay={x}:{y}:eof_action=pass[vr{j}];")
        last = f"vr{j}"
        n_in += 1
    vgraph += f"[{last}]ass=captions.ass:fontsdir=fonts[v]"
    ffmpeg([*v_inputs, "-filter_complex", vgraph, "-map", "[v]", "-an", "-t", f"{total:.3f}", "-c:v", "libx264",
            "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(fps), "video_fx.mp4"], cwd=work)

    a_inputs, mix, agraph, n_in = ["-i", "voice.wav"], ["[0:a]"], "", 1
    if music:
        shutil.copy(music, work / f"music{music.suffix}")
        a_inputs += ["-stream_loop", "-1", "-i", f"music{music.suffix}"]
        fade_at = max(0.0, total - 1.5)
        agraph += f"[{n_in}:a]volume={MUSIC_VOLUME},afade=t=in:d=0.5,afade=t=out:st={fade_at:.2f}:d=1.5[bg];"
        mix.append("[bg]")
        n_in += 1
    for j, (at, path) in enumerate(sfx):
        a_inputs += ["-i", str(path)]
        ms = max(0, int(at * 1000))
        agraph += f"[{n_in}:a]aresample=48000,volume={SFX_VOLUME},adelay=delays={ms}:all=1[fx{j}];"
        mix.append(f"[fx{j}]")
        n_in += 1
    if len(mix) > 1:
        agraph += f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
    else:
        agraph += "[0:a]"
    agraph += "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    ffmpeg([*a_inputs, "-filter_complex", agraph, "-map", "[a]", "-c:a", "pcm_s16le", "audio_mix.wav"], cwd=work)

    final = work / "final.mp4"
    ffmpeg(["-i", "video_fx.mp4", "-i", "audio_mix.wav", "-map", "0:v", "-map", "1:a", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", final.name], cwd=work)
    for tmp in ("video_fx.mp4", "audio_mix.wav"):
        (work / tmp).unlink(missing_ok=True)
    return final


def decoded_audio_seconds(path: Path) -> float:
    """Seconds of audio actually present (decoded), which can be less than the container's stated duration."""
    out = subprocess.run([get_settings().env.ffmpeg_binary, "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "s16le", "-"], capture_output=True, creationflags=NO_WINDOW)
    return len(out.stdout) / 2 / 16000


def qa(final: Path, narration_seconds: float | None = None) -> list[str]:
    """Return problems found (empty list = pass). With `narration_seconds`, also checks that the whole
    narration is really in the file (no dropped audio)."""
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
    if narration_seconds and audio:
        heard = decoded_audio_seconds(final)
        if heard < narration_seconds - 0.5:
            problems.append(f"audio has gaps: {heard:.1f}s decoded of {narration_seconds:.1f}s narration")
    return problems
