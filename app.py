"""YouTube Downloader Web Application"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import winreg
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    Response,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024
DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

tasks = {}
task_lock = threading.Lock()
local_subtitle_tasks = {}
local_subtitle_sessions = {}
local_subtitle_lock = threading.Lock()


def sanitize_filename(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def get_subtitle_timeline(url):
    """Fetch auto-generated subtitles and return a timeline text."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return ""

        info = json.loads(result.stdout)
        auto_caps = info.get("automatic_captions", {})
        if not auto_caps:
            return ""

        lang = next((l for l in ("zh", "zh-Hans", "en") if l in auto_caps), next(iter(auto_caps), None))
        if not lang:
            return ""

        formats = auto_caps[lang]
        json3_url = next((f["url"] for f in formats if f.get("ext") == "json3"), None)
        if not json3_url:
            return ""

        resp = urllib.request.urlopen(json3_url, timeout=15)
        sub_data = json.loads(resp.read().decode("utf-8"))

        chunks = []
        current_start = None
        current_text = []

        for event in sub_data.get("events", []):
            start_ms = event.get("tStartMs", 0)
            segs = event.get("segs", [])
            text = "".join(s.get("utf8", "") for s in segs).strip()
            if not text or text == "\n":
                continue

            if current_start is None:
                current_start = start_ms

            if start_ms - current_start >= 30000:
                minutes = current_start // 60000
                seconds = (current_start % 60000) // 1000
                chunks.append(f"{minutes:02d}:{seconds:02d} {' '.join(current_text)}")
                current_start = start_ms
                current_text = [text]
            else:
                current_text.append(text)

        if current_text and current_start is not None:
            minutes = current_start // 60000
            seconds = (current_start % 60000) // 1000
            chunks.append(f"{minutes:02d}:{seconds:02d} {' '.join(current_text)}")

        return "\n".join(chunks)
    except Exception:
        return ""


# ── Subtitle Processing ──────────────────────────────────────────────

def parse_time(time_str):
    """Convert SRT/VTT timestamp to seconds."""
    time_str = time_str.strip().replace(',', '.')
    parts = time_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return 0


def format_ass_time(seconds):
    """Format seconds to ASS timestamp H:MM:SS.CC."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def format_srt_time(seconds):
    """Format seconds to SRT timestamp HH:MM:SS,mmm."""
    seconds = max(0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
    if s >= 60:
        m += 1
        s -= 60
    if m >= 60:
        h += 1
        m -= 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def probe_video_dimensions(video_path):
    """Return (width, height) for a local video, or the ASS default."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            width, height = result.stdout.strip().splitlines()[0].split(",")[:2]
            width, height = int(width), int(height)
            if width > 0 and height > 0:
                return width, height
    except Exception:
        pass
    return 1920, 1080


def parse_subtitle_file(filepath):
    """Parse SRT or VTT into [(start, end, text), ...]."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    if content.startswith('WEBVTT'):
        idx = content.find('\n\n')
        content = content[idx + 2:] if idx >= 0 else ''

    pattern = (
        r'(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*'
        r'(\d{1,2}:\d{2}:\d{2}[.,]\d{3})[^\n]*\n'
        r'((?:(?!\n\n|\n\d+\s*\n).)+)'
    )
    segments = []
    for match in re.finditer(pattern, content, re.DOTALL):
        start = parse_time(match.group(1))
        end = parse_time(match.group(2))
        text = match.group(3).strip()
        text = re.sub(r'<[^>]+>', '', text)
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if line and line not in lines:
                lines.append(line)
        text = ' '.join(lines)
        if text:
            segments.append((start, end, text))

    return segments


def merge_subtitle_tracks(zh_segs, en_segs):
    """Time-align two subtitle tracks into [(start, end, zh, en), ...]."""
    if not zh_segs and not en_segs:
        return []

    times = set()
    for s, e, _ in zh_segs:
        times.add(round(s, 3)); times.add(round(e, 3))
    for s, e, _ in en_segs:
        times.add(round(s, 3)); times.add(round(e, 3))
    times = sorted(times)
    if len(times) < 2:
        return []

    merged = []
    for i in range(len(times) - 1):
        start, end = times[i], times[i + 1]
        if end - start < 0.01:
            continue

        zh_text = ''
        for s, e, t in zh_segs:
            if s <= start + 0.05 and e >= end - 0.05:
                zh_text = t.replace('\n', ' ')
                break

        en_text = ''
        for s, e, t in en_segs:
            if s <= start + 0.05 and e >= end - 0.05:
                en_text = t.replace('\n', ' ')
                break

        if zh_text or en_text:
            merged.append((start, end, zh_text, en_text))

    # merge consecutive segments with identical content
    result = []
    for seg in merged:
        if result and result[-1][2] == seg[2] and result[-1][3] == seg[3]:
            result[-1] = (result[-1][0], seg[1], seg[2], seg[3])
        else:
            result.append(list(seg))
    return result


def hex_to_ass(hex_color):
    """Convert #RRGGBB to ASS &H00BBGGRR."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def generate_dual_ass(merged_segs, opts):
    """Generate ASS content with configurable language order."""
    font = opts.get('font', 'Microsoft YaHei')
    size = opts.get('size', 52)
    color = hex_to_ass(opts.get('color', '#FFFFFF'))
    outline = hex_to_ass(opts.get('outline_color', '#000000'))
    sub_order = opts.get('sub_order', 'zh_top')
    sub_pos = opts.get('sub_pos', 2)
    margin_v = opts.get('margin_v', 30)
    play_res_x = int(opts.get('play_res_x', 1920) or 1920)
    play_res_y = int(opts.get('play_res_y', 1080) or 1080)
    side_margin = max(10, round(play_res_x * 30 / 1920))

    bg_enabled = opts.get('bg_enabled', False)
    bg_color_hex = opts.get('bg_color', '#000000')
    bg_opacity = opts.get('bg_opacity', 50)

    # Use libass' native opaque box so the text and background are laid out
    # by the same renderer. The previous hand-drawn rectangle drifted on some
    # videos because font metrics and line spacing were only estimated.
    if bg_enabled:
        alpha = int((1 - bg_opacity / 100) * 255)
        bg_r = int(bg_color_hex[1:3], 16)
        bg_g = int(bg_color_hex[3:5], 16)
        bg_b = int(bg_color_hex[5:7], 16)
        bg_fill = f"&H{alpha:02X}{bg_b:02X}{bg_g:02X}{bg_r:02X}"
        border_style = 3
        back_colour = bg_fill
        outline_val = 6
    else:
        border_style = 1
        back_colour = "&H80000000"
        outline_val = 2.5
        bg_fill = None

    ass = (
        "[Script Info]\n"
        "Title: Dual Subtitles (ZH+EN)\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{color},{color},"
        f"{outline},{back_colour},-1,0,0,0,100,100,0,0,{border_style},{outline_val},1,"
        f"{sub_pos},{side_margin},{side_margin},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    for seg in merged_segs:
        start_str = format_ass_time(seg[0])
        end_str = format_ass_time(seg[1])
        zh, en = seg[2], seg[3]

        parts = []
        if sub_order == 'en_top':
            if en:
                parts.append(en)
            if zh:
                parts.append(zh)
        else:
            if zh:
                parts.append(zh)
            if en:
                parts.append(en)
        if parts:
            text = "\\N".join(parts)
            ass += f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}\n"

    return ass


ZH_PATTERNS = ('.zh.', '.zh-cn.', '.zh-hans.', '.zh-Hans.', '.zh-tw.', '.zh-hant.', '.zh-Hant.', '.chi.')
EN_PATTERNS = ('.en.', '.eng.', '.en-us.', '.en-gb.')


def find_subtitle_files(downloads_dir):
    """Find latest Chinese and English subtitle files."""
    zh_file = en_file = None
    for f in downloads_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in ('.srt', '.vtt'):
            continue
        name = f.name.lower()
        for pat in ZH_PATTERNS:
            if pat in name:
                if zh_file is None or f.stat().st_mtime > zh_file.stat().st_mtime:
                    zh_file = f
                break
        for pat in EN_PATTERNS:
            if pat in name:
                if en_file is None or f.stat().st_mtime > en_file.stat().st_mtime:
                    en_file = f
                break
    return zh_file, en_file


def translate_subtitles(segments, api_url, api_key, model, update=None):
    """Translate subtitle segments to Chinese using AI."""
    if not segments or not api_url or not api_key:
        return []

    from openai import OpenAI
    client = OpenAI(base_url=api_url, api_key=api_key)

    batch_size = 80
    translated = []
    total_batches = (len(segments) + batch_size - 1) // batch_size

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        batch_num = i // batch_size + 1
        if update:
            update("downloading", 95, f"Translating subtitles ({batch_num}/{total_batches})...")

        lines = []
        for j, (start, end, text) in enumerate(batch):
            lines.append(f"{i + j + 1}. {text}")

        prompt = (
            "将以下字幕逐行翻译为简体中文。只输出翻译结果，每行格式：序号. 翻译文本。"
            "不要添加任何解释、注释或额外内容。保持序号一一对应。\n\n"
            + "\n".join(lines)
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=4000,
            )
            result = resp.choices[0].message.content.strip()
            result_lines = [l.strip() for l in result.splitlines() if l.strip()]

            translations = {}
            for line in result_lines:
                dot = line.find(".")
                if dot > 0 and dot < 6:
                    try:
                        idx = int(line[:dot]) - 1
                        translations[idx] = line[dot + 1:].strip()
                    except ValueError:
                        continue

            for j, (start, end, text) in enumerate(batch):
                idx = i + j
                zh_text = translations.get(idx, "")
                translated.append((start, end, zh_text if zh_text else text))

        except Exception:
            for j, (start, end, text) in enumerate(batch):
                translated.append((start, end, text))

    return translated


def process_dual_subtitles(downloads_dir, opts, update=None):
    """Parse subtitle files and produce a dual ASS subtitle."""
    sub_mode = opts.get('sub_mode', 'zh_en')
    zh_file, en_file = find_subtitle_files(downloads_dir)
    if not zh_file and not en_file:
        return None

    zh_segs = parse_subtitle_file(zh_file) if zh_file else []
    en_segs = parse_subtitle_file(en_file) if en_file else []

    # Translate if no Chinese subtitles and translation is enabled
    if not zh_segs and en_segs and opts.get('translate_sub'):
        api_url = opts.get('api_url', '')
        api_key = opts.get('api_key', '')
        model = opts.get('model', 'gpt-4o-mini')
        if api_url and api_key:
            translated = translate_subtitles(en_segs, api_url, api_key, model, update)
            if translated:
                zh_segs = translated

    if sub_mode == 'zh':
        en_segs = []

    if zh_segs and en_segs:
        merged = merge_subtitle_tracks(zh_segs, en_segs)
    elif zh_segs:
        merged = [[s, e, t, ''] for s, e, t in zh_segs]
    elif en_segs:
        merged = [[s, e, '', t] for s, e, t in en_segs]
    else:
        return None

    if not merged:
        return None

    video_files = sorted(
        [f for f in downloads_dir.glob("*.mp4") if ".hardsub" not in f.name],
        key=lambda x: x.stat().st_mtime, reverse=True,
    )
    if video_files:
        play_res_x, play_res_y = probe_video_dimensions(video_files[0])
        opts = dict(opts)
        opts["play_res_x"] = play_res_x
        opts["play_res_y"] = play_res_y

    ass_content = generate_dual_ass(merged, opts)
    base_name = (zh_file or en_file).stem.split('.')[0]
    ass_path = downloads_dir / f"{base_name}.dual.ass"
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(ass_content)

    return ass_path.name


def burn_subtitles_to_video(downloads_dir, ass_name, update=None):
    """Burn ASS subtitles into the video file using ffmpeg."""
    import tempfile
    import shutil

    ass_path = downloads_dir / ass_name
    if not ass_path.exists():
        return

    video_files = sorted(
        [f for f in downloads_dir.glob("*.mp4") if ".hardsub" not in f.name],
        key=lambda x: x.stat().st_mtime, reverse=True,
    )
    if not video_files:
        return

    video = video_files[0]
    output = video.with_name(video.stem + ".hardsub.mp4")

    # Copy ASS to temp with simple name, use relative path to avoid
    # ffmpeg filter parsing issues with Windows drive letters / special chars
    tmp_dir = Path(tempfile.gettempdir())
    tmp_ass = tmp_dir / "_sub_burn.ass"
    try:
        shutil.copy2(str(ass_path), str(tmp_ass))
    except Exception:
        return

    try:
        if update:
            update("downloading", 99, "Burning subtitles into video...")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video),
             "-vf", "ass=_sub_burn.ass",
             "-c:a", "copy", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
             str(output)],
            capture_output=True, timeout=1800,
            cwd=str(tmp_dir),
        )
        if output.exists() and output.stat().st_size > 0:
            video.unlink()
            output.rename(video)
            # Clean up subtitle files after successful burn
            ass_path.unlink(missing_ok=True)
            base = ass_path.stem.replace('.dual', '')
            for f in downloads_dir.iterdir():
                if f.stem.startswith(base) and f.suffix.lower() in ('.vtt', '.srt', '.ass'):
                    f.unlink(missing_ok=True)
        else:
            if update:
                err = result.stderr.decode("utf-8", errors="replace")[-200:] if result.stderr else ""
                update("downloading", 99, f"Hardsub failed: {err}")
    except Exception as e:
        if output.exists():
            output.unlink(missing_ok=True)
        if update:
            update("downloading", 99, f"Hardsub error: {e}")
    finally:
        tmp_ass.unlink(missing_ok=True)


# ── Local Video Subtitle Translation ─────────────────────────────────

YIMU_PROJECT_DIR = Path(
    r"N:\YiMu-Subtitle-Translator-main\YiMu-Subtitle-Translator-main-pack - 副本\YiMu-Subtitle-Translator-main-pack"
)


def find_whisper_cli():
    candidates = [
        Path(__file__).parent / "tools" / "faster-whisper-xxl" / "faster-whisper-xxl.exe",
        YIMU_PROJECT_DIR / "tools" / "faster-whisper-xxl" / "faster-whisper-xxl.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("faster-whisper-xxl.exe") or shutil.which("faster-whisper-xxl") or ""


def find_whisper_model_dir():
    candidates = [
        Path(__file__).parent / "tools" / "faster-whisper-xxl" / "_models",
        YIMU_PROJECT_DIR / "tools" / "faster-whisper-xxl" / "_models",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def find_local_ffmpeg_dir():
    candidates = [
        Path(__file__).parent / "tools" / "ffmpeg" / "bin",
        YIMU_PROJECT_DIR / "tools" / "ffmpeg" / "bin",
        YIMU_PROJECT_DIR / "tools" / "faster-whisper-xxl",
    ]
    for candidate in candidates:
        if (candidate / "ffmpeg.exe").exists():
            return str(candidate)
    return ""


def local_task_update(task_id, **fields):
    with local_subtitle_lock:
        task = local_subtitle_tasks.get(task_id)
        if task:
            task.update(fields)


def parse_srt_content(content):
    entries = []
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return entries
    blocks = re.split(r"\n\s*\n", content)
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        try:
            start_raw, end_raw = lines[1].split("-->", 1)
            entries.append({
                "index": len(entries) + 1,
                "start": parse_time(start_raw.strip()),
                "end": parse_time(end_raw.strip().split()[0]),
                "source": "\n".join(lines[2:]).strip(),
                "translation": "",
            })
        except Exception:
            continue
    return entries


def entries_to_srt(entries, bilingual=True, order="en_top"):
    blocks = []
    for i, entry in enumerate(entries, 1):
        source = (entry.get("source") or "").strip()
        translation = (entry.get("translation") or "").strip()
        if bilingual and translation:
            if order == "zh_top":
                text = f"{translation}\n{source}" if source else translation
            else:
                text = f"{source}\n{translation}" if source else translation
        else:
            text = translation or source
        if not text:
            continue
        blocks.append(
            f"{i}\n"
            f"{format_srt_time(entry['start'])} --> {format_srt_time(entry['end'])}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def serialize_local_entries(entries, limit=120):
    visible = entries[:limit] if limit else entries
    return [{
        "index": i + 1,
        "start": format_srt_time(e["start"]),
        "end": format_srt_time(e["end"]),
        "source": e.get("source", ""),
        "translation": e.get("translation", ""),
    } for i, e in enumerate(visible)]


def create_local_session(original_name, entries):
    session_id = str(uuid.uuid4())[:8]
    for i, entry in enumerate(entries, 1):
        entry["index"] = i
    with local_subtitle_lock:
        local_subtitle_sessions[session_id] = {
            "id": session_id,
            "name": original_name,
            "entries": entries,
            "corrections": [],
            "created_at": time.time(),
        }
    return session_id


def get_local_session(session_id):
    with local_subtitle_lock:
        session = local_subtitle_sessions.get(session_id)
    if not session:
        return None
    return session


def local_session_payload(session, limit=120):
    entries = session.get("entries", [])
    translated = sum(1 for e in entries if (e.get("translation") or "").strip())
    return {
        "session_id": session["id"],
        "name": session.get("name", ""),
        "segments": len(entries),
        "translated": translated,
        "corrections": session.get("corrections", []),
        "entries": serialize_local_entries(entries, limit=limit),
    }


def text_is_cjk(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def local_text_len(text):
    if text_is_cjk(text):
        return len(re.findall(r"[\u4e00-\u9fff]", text))
    return len(re.findall(r"[A-Za-z0-9]+", text))


def merge_local_short_entries(entries, max_chars_cjk=30, max_words_latin=14, max_gap=0.5):
    merged = []
    changed = 0
    i = 0
    while i < len(entries):
        cur = dict(entries[i])
        while i + 1 < len(entries):
            nxt = entries[i + 1]
            gap = nxt["start"] - cur["end"]
            if gap > max_gap:
                break
            cur_limit = max_chars_cjk if text_is_cjk(cur.get("source", "")) else max_words_latin
            next_limit = max_chars_cjk if text_is_cjk(nxt.get("source", "")) else max_words_latin
            limit = max(cur_limit, next_limit)
            cur_len = local_text_len(cur.get("source", ""))
            combined_source = (cur.get("source", "").strip() + " " + nxt.get("source", "").strip()).strip()
            combined_translation = (
                cur.get("translation", "").strip() + " " + nxt.get("translation", "").strip()
            ).strip()
            if cur_len >= limit or local_text_len(combined_source) > limit * 1.6:
                break
            cur["end"] = nxt["end"]
            cur["source"] = combined_source
            cur["translation"] = combined_translation
            i += 1
            changed += 1
        merged.append(cur)
        i += 1

    for idx, entry in enumerate(merged, 1):
        entry["index"] = idx
    return merged, changed


def strip_code_fence(text):
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text[7:].strip()
    elif text.startswith("```"):
        text = text[3:].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def extract_json_array(text):
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except Exception:
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
                return data if isinstance(data, list) else []
            except Exception:
                return []
    return []


def call_chat_model(api_url, api_key, model, messages):
    from openai import OpenAI

    client = OpenAI(base_url=api_url.rstrip("/"), api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    return (response.choices[0].message.content or "").strip()


def analyze_asr_corrections(entries, api_url, api_key, model, user_hint=""):
    sample = "\n".join(e["source"] for e in entries[:220])
    sample = sample[:16000]
    hint = user_hint.strip() or "视频内容可能涉及 AI、编程、创业、产品名、公司名和人名。"
    prompt = f"""你是专业的 ASR 字幕校正助手。请从下面的英文字幕中找出明显的语音识别误识别词，尤其是产品名、人名、公司名、技术术语和缩写词。

背景提示：
{hint}

待分析字幕：
{sample}

只输出 JSON 数组，不要 Markdown。每个元素格式：
{{"wrong":"字幕中实际出现的错误文本","correct":"应替换为的正确文本","reason":"简短原因"}}

只给高把握建议；如果没有明显错误，输出 []。"""
    messages = [
        {"role": "system", "content": "You output only valid JSON."},
        {"role": "user", "content": prompt},
    ]
    try:
        return extract_json_array(call_chat_model(api_url, api_key, model, messages))
    except Exception:
        return []


def apply_asr_corrections(entries, corrections):
    applied = 0
    normalized = []
    for item in corrections:
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong", "")).strip()
        correct = str(item.get("correct", "")).strip()
        if wrong and correct and wrong.lower() != correct.lower():
            normalized.append((wrong, correct))

    for entry in entries:
        text = entry["source"]
        for wrong, correct in normalized:
            if wrong in text:
                count = text.count(wrong)
                text = text.replace(wrong, correct)
                applied += count
            else:
                text, count = re.subn(re.escape(wrong), correct, text, flags=re.IGNORECASE)
                applied += count
        entry["source"] = text
    return applied


def translate_local_entries(task_id, entries, api_url, api_key, model, user_hint=""):
    batch_size = 60
    total_batches = max(1, (len(entries) + batch_size - 1) // batch_size)
    hint = user_hint.strip()

    for offset in range(0, len(entries), batch_size):
        batch = entries[offset:offset + batch_size]
        batch_no = offset // batch_size + 1
        local_task_update(
            task_id,
            status="running",
            progress=60 + round(batch_no / total_batches * 30),
            message=f"正在翻译并微调字幕 {batch_no}/{total_batches}...",
        )
        lines = "\n".join(f"{e['index']} | {e['source'].replace(chr(10), ' ')}" for e in batch)
        prompt = f"""你是专业字幕翻译和 ASR 校正助手。下面是从本地视频识别出的字幕。
请逐行完成两件事：
1. 轻微修正明显的英文 ASR 错词，不要改写风格，不要合并或拆分字幕。
2. 翻译为自然、简洁的简体中文，适合视频字幕。

背景提示：
{hint or "无"}

输入格式：
序号 | 英文原文

输出要求：
- 只输出逐行结果，不要解释。
- 每行严格使用：序号 | 修正后的英文 | 中文译文
- 输入多少行，输出多少行，序号必须一致。

待处理字幕：
{lines}"""
        messages = [{"role": "user", "content": prompt}]
        result = call_chat_model(api_url, api_key, model, messages)

        by_index = {}
        ordered = []
        for line in result.splitlines():
            match = re.match(r"^\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*$", line)
            if not match:
                continue
            idx = int(match.group(1))
            source = match.group(2).strip()
            zh = match.group(3).strip()
            by_index[idx] = (source, zh)
            ordered.append((source, zh))

        for i, entry in enumerate(batch):
            source, zh = by_index.get(entry["index"], ordered[i] if i < len(ordered) else ("", ""))
            if source:
                entry["source"] = source
            entry["translation"] = zh or entry["source"]


def transcribe_local_media(task_id, media_path, opts):
    cli_exe = find_whisper_cli()
    if not cli_exe:
        raise RuntimeError("未找到 faster-whisper-xxl.exe。请确认 YiMu 工具目录仍在原位置。")

    model_name = opts.get("transcribe_model") or "large-v3"
    device = opts.get("device") or "cuda"
    language = opts.get("language") or "en"
    model_dir = find_whisper_model_dir()
    is_latin = language in ("auto", "en", "fr", "de", "es", "pt", "it", "nl", "pl", "ru")
    max_line_width = "90" if is_latin else "30"

    output_srt = media_path.with_suffix(".srt")
    cmd = [
        cli_exe,
        "-m", model_name,
        "--print_progress",
        str(media_path),
        "-d", device,
        "--output_format", "srt",
        "-o", "source",
        "--beam_size", "8",
        "--sentence",
        "--max_line_width", max_line_width,
        "--max_line_count", "1",
        "--max_comma", "20",
        "--max_comma_cent", "50",
        "--beep_off",
        "--vad_filter", "true",
        "--vad_threshold", "0.40",
        "--vad_min_silence_duration_ms", "300",
    ]
    if model_dir:
        cmd.extend(["--model_dir", model_dir])
    if language != "auto":
        cmd.extend(["-l", language])
    if opts.get("initial_prompt"):
        cmd.extend(["--initial_prompt", opts["initial_prompt"]])

    env = os.environ.copy()
    path_parts = [str(Path(cli_exe).parent)]
    ffmpeg_dir = find_local_ffmpeg_dir()
    if ffmpeg_dir:
        path_parts.append(ffmpeg_dir)
    env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])

    local_task_update(task_id, status="running", progress=8, message="正在启动本地识别引擎...")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )

    recent = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        recent.append(line)
        if len(recent) > 20:
            recent.pop(0)
        match = re.search(r"(\d+)%", line)
        if match:
            pct = int(match.group(1))
            local_task_update(
                task_id,
                progress=min(55, 8 + round(pct * 0.47)),
                message=f"正在本地识别字幕... {pct}%",
            )

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("本地识别失败：" + "；".join(recent[-5:]))
    if not output_srt.exists():
        raise RuntimeError("本地识别完成但未生成 SRT 文件。")
    return output_srt.read_text(encoding="utf-8", errors="replace")


def run_local_subtitle_task(task_id, media_path, original_name, opts):
    try:
        local_task_update(task_id, status="running", progress=2, message="正在准备本地视频...")
        srt_text = transcribe_local_media(task_id, media_path, opts)
        entries = parse_srt_content(srt_text)
        if not entries:
            raise RuntimeError("未识别出可用字幕。")

        api_url = opts.get("api_url", "").strip()
        api_key = opts.get("api_key", "").strip()
        model = opts.get("model", "").strip()
        if not api_url or not api_key or not model:
            raise RuntimeError("请填写 API 地址、API Key 和模型名后再翻译。")

        local_task_update(task_id, progress=56, message="正在用大模型分析识别错误...")
        corrections = analyze_asr_corrections(
            entries, api_url, api_key, model, opts.get("prompt", "")
        )
        applied = apply_asr_corrections(entries, corrections)

        translate_local_entries(task_id, entries, api_url, api_key, model, opts.get("prompt", ""))

        local_task_update(task_id, progress=94, message="正在导出字幕文件...")
        bilingual = opts.get("output_mode", "bilingual") == "bilingual"
        output_text = entries_to_srt(entries, bilingual=bilingual)
        stem = sanitize_filename(Path(original_name).stem or "local_video")
        suffix = "bilingual" if bilingual else "zh"
        output_name = f"{stem}.local.{suffix}.srt"
        output_path = DOWNLOADS_DIR / output_name
        output_path.write_text(output_text, encoding="utf-8")

        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"完成：{len(entries)} 条字幕，应用 {applied} 处校正",
            file=output_name,
            url=f"/files/{output_name}",
            corrections=corrections,
            segments=len(entries),
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))
    finally:
        try:
            shutil.rmtree(media_path.parent, ignore_errors=True)
        except Exception:
            pass


def run_local_import_task(task_id, media_path, original_name, opts):
    try:
        local_task_update(task_id, status="running", progress=2, message="正在识别本地音视频...")
        srt_text = transcribe_local_media(task_id, media_path, opts)
        entries = parse_srt_content(srt_text)
        if not entries:
            raise RuntimeError("未识别出可用字幕。")
        session_id = create_local_session(original_name, entries)
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"完成：{len(entries)} 条字幕",
            session_id=session_id,
            segments=len(entries),
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))
    finally:
        try:
            shutil.rmtree(media_path.parent, ignore_errors=True)
        except Exception:
            pass


def run_local_translate_task(task_id, session_id, opts):
    try:
        session = get_local_session(session_id)
        if not session:
            raise RuntimeError("字幕会话不存在，请重新导入。")
        entries = session["entries"]
        api_url = opts.get("api_url", "").strip()
        api_key = opts.get("api_key", "").strip()
        model = opts.get("model", "").strip()
        if not api_url or not api_key or not model:
            raise RuntimeError("请填写 API 地址、API Key 和模型名后再翻译。")
        local_task_update(task_id, status="running", progress=3, message="正在准备翻译...")
        translate_local_entries(task_id, entries, api_url, api_key, model, opts.get("prompt", ""))
        with local_subtitle_lock:
            if session_id in local_subtitle_sessions:
                local_subtitle_sessions[session_id]["entries"] = entries
        local_task_update(
            task_id,
            status="completed",
            progress=100,
            message=f"翻译完成：{len(entries)} 条字幕",
            session_id=session_id,
            segments=len(entries),
        )
    except Exception as e:
        local_task_update(task_id, status="error", message=str(e), error=str(e))


# ── Download Logic ────────────────────────────────────────────────────


def fix_video_for_ae(downloads_dir, update=None):
    """Re-encode video to H.264+AAC MP4 for After Effects compatibility."""
    for f in list(downloads_dir.glob("*.mp4")):
        if ".hardsub" in f.name or ".ae." in f.name:
            continue
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v",
                 "-show-entries", "stream=codec_name", "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            vcodec = probe.stdout.strip().split('\n')[0].strip() if probe.stdout.strip() else ''
            if vcodec in ('h264', 'avc', ''):
                continue
            if update:
                update("downloading", 95, f"Converting {vcodec}→H.264 for AE...")
            tmp = f.with_suffix('.ae.mp4')
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(f),
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                 "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(tmp)],
                capture_output=True, timeout=1800,
            )
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(f)
        except Exception:
            pass


def fix_audio_codec(downloads_dir, update=None):
    """Re-encode non-AAC audio in MP4 files for compatibility."""
    for f in list(downloads_dir.glob("*.mp4")):
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "a",
                 "-show-entries", "stream=codec_name", "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            codec = probe.stdout.strip().split('\n')[0].strip() if probe.stdout.strip() else ''
            if codec and codec != 'aac':
                if update:
                    update("downloading", 95, f"Converting audio {codec}→aac...")
                tmp = f.with_suffix('.tmp.mp4')
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(f),
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", str(tmp)],
                    capture_output=True, timeout=600,
                )
                if tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(f)
        except Exception:
            pass


def get_format_string(codec, quality):
    base = {
        "best": "bestvideo",
        "h264": "bestvideo[vcodec^=avc]",
        "vp9": "bestvideo[vcodec^=vp9]",
        "av1": "bestvideo[vcodec^=av01]",
    }.get(codec, "bestvideo")
    h = f"[height<={quality}]" if quality != "best" else ""
    return (
        f"{base}{h}+bestaudio[acodec=aac]/"
        f"{base}{h}+bestaudio[acodec!=none]/"
        f"{base}{h}+bestaudio/"
        f"best{h}"
    )


def run_download(task_id, url, options):
    cmd = ["yt-dlp", "--no-warnings", "--ignore-errors", "--js-runtimes", "node", "--remote-components", "ejs:github"]
    output_template = str(DOWNLOADS_DIR / "%(title)s.%(ext)s")

    download_type = options.get("type", "video")
    quality = options.get("quality", "1080")
    audio_format = options.get("audio_format", "mp3")
    codec = options.get("codec", "best")
    dual_sub = options.get("dual_subtitle", False)
    sub_opts = options.get("sub_options", {})

    if download_type == "audio":
        cmd += ["-x", f"--audio-format={audio_format}", "--audio-quality=0"]
    else:
        format_id = options.get("format_id")
        if format_id:
            cmd += ["-f", format_id, "--merge-output-format", "mp4"]
        else:
            h = f"[height<={quality}]" if quality != "best" else ""
            cmd += ["-f", f"bv*{h}+ba/b{h}"]

            sort_parts = ["res", "fps", "hdr"]
            if codec == "h264":
                sort_parts.append("vcodec:h264")
            elif codec == "vp9":
                sort_parts.append("vcodec:vp9")
            elif codec == "av1":
                sort_parts.append("vcodec:av1")
            sort_parts += ["vbr", "abr"]
            cmd += ["-S", ",".join(sort_parts)]
            cmd += ["--merge-output-format", "mp4"]

        if dual_sub:
            sub_mode = options.get("sub_options", {}).get("sub_mode", "zh_en")
            sub_langs = "zh,zh-Hans,zh-Hant" if sub_mode == "zh" else "zh,zh-Hans,zh-Hant,en"
            cmd += [
                "--write-subs", "--write-auto-subs",
                "--sub-langs", sub_langs,
            ]
        elif options.get("subtitles", False):
            sub_langs = options.get("sub_langs", "zh,en")
            cmd += [
                "--write-subs", "--write-auto-subs",
                f"--sub-langs={sub_langs}", "--embed-subs",
            ]

    cmd += [
        "--write-thumbnail",
        "--write-description",
        "--write-info-json",
        "--newline",
        "--progress",
        "-o", output_template,
        url,
    ]

    def update(status, progress=None, message=None, files=None):
        with task_lock:
            tasks[task_id].update({
                "status": status,
                "progress": progress or tasks[task_id].get("progress", 0),
                "message": message or tasks[task_id].get("message", ""),
                "files": files or tasks[task_id].get("files", []),
            })

    update("downloading", 0, "Starting download...")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(DOWNLOADS_DIR),
        )

        last_lines = []
        log_path = DOWNLOADS_DIR / "_download.log"
        logf = open(log_path, "w", encoding="utf-8")
        logf.write(f"CMD: {' '.join(cmd)}\n\n")
        logf.flush()

        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            logf.write(line + "\n")
            logf.flush()
            last_lines.append(line)
            if len(last_lines) > 50:
                last_lines.pop(0)

            pct_match = re.search(r'(\d+\.?\d*)%', line)
            if pct_match:
                pct = float(pct_match.group(1))
                update("downloading", pct, line)

            if "[download]" in line.lower() and "destination" in line.lower():
                update("downloading", message=line)
            elif "merging" in line.lower():
                update("downloading", message="Merging audio and video...")
            elif "extracting" in line.lower():
                update("downloading", message="Extracting audio...")

        logf.close()
        process.wait()

        # Clean up .part files from interrupted downloads
        for f in DOWNLOADS_DIR.glob("*.part"):
            f.unlink(missing_ok=True)

        # yt-dlp may return non-zero if subtitle download fails (e.g. 429)
        # but the video itself might still be downloaded successfully
        has_video = download_type == "audio" or any(DOWNLOADS_DIR.glob("*.mp4"))

        if process.returncode == 0 or has_video:
            if download_type == "video":
                if options.get("ae_compat"):
                    fix_video_for_ae(DOWNLOADS_DIR, update)
                else:
                    fix_audio_codec(DOWNLOADS_DIR, update)

            if dual_sub:
                update("downloading", 95, "Processing dual subtitles...")
                ass_name = process_dual_subtitles(DOWNLOADS_DIR, sub_opts, update)
                if options.get("burn_sub") and ass_name:
                    update("downloading", 96, "Burning subtitles into video...")
                    burn_subtitles_to_video(DOWNLOADS_DIR, ass_name, update)
                elif ass_name:
                    update("downloading", 99, f"Subtitle file generated")
            update("completed", 100, "Download complete!", list_downloaded_files())
        else:
            update("error", message=f"Download failed (exit code {process.returncode}): {'; '.join(last_lines[-5:])}")

    except Exception as e:
        update("error", message=str(e))


def list_downloaded_files():
    files = []
    for f in sorted(DOWNLOADS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix not in ('.json', '.description'):
            size_mb = f.stat().st_size / (1024 * 1024)
            files.append({
                "name": f.name,
                "size": round(size_mb, 2),
                "ext": f.suffix.lstrip('.'),
                "url": f"/files/{f.name}",
            })
    return files


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/local-subtitle/capabilities")
def local_subtitle_capabilities():
    model_dir = find_whisper_model_dir()
    models = []
    if model_dir:
        for item in Path(model_dir).iterdir():
            if item.is_dir() and item.name.startswith("faster-whisper-"):
                if (item / "model.bin").exists():
                    models.append(item.name.replace("faster-whisper-", ""))
    return jsonify({
        "cli_available": bool(find_whisper_cli()),
        "cli_path": find_whisper_cli(),
        "model_dir": model_dir,
        "models": sorted(models),
    })


@app.route("/api/local-subtitle/import", methods=["POST"])
def import_local_subtitle():
    if "file" not in request.files:
        return jsonify({"error": "请先选择本地视频、音频或 SRT 文件"}), 400

    upload = request.files["file"]
    original_name = re.split(r"[\\/]", upload.filename or "local_file")[-1]
    safe_name = sanitize_filename(original_name) or f"{uuid.uuid4().hex}.tmp"
    suffix = Path(safe_name).suffix.lower()

    if suffix == ".srt":
        text = upload.read().decode("utf-8", errors="replace")
        entries = parse_srt_content(text)
        if not entries:
            return jsonify({"error": "没有解析到可用 SRT 字幕"}), 400
        session_id = create_local_session(original_name, entries)
        return jsonify(local_session_payload(get_local_session(session_id)))

    task_id = str(uuid.uuid4())[:8]
    task_dir = DOWNLOADS_DIR / "_local_tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    media_path = task_dir / safe_name
    upload.save(media_path)

    opts = {
        "transcribe_model": request.form.get("transcribe_model", "large-v3"),
        "device": request.form.get("device", "cuda"),
        "language": request.form.get("language", "en"),
        "initial_prompt": request.form.get("initial_prompt", ""),
    }

    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入识别队列...",
            "session_id": None,
            "segments": 0,
        }

    threading.Thread(
        target=run_local_import_task,
        args=(task_id, media_path, original_name, opts),
        daemon=True,
    ).start()

    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/session/<session_id>")
def get_local_subtitle_session(session_id):
    session = get_local_session(session_id)
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404
    return jsonify(local_session_payload(session))


@app.route("/api/local-subtitle/merge", methods=["POST"])
def merge_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    entries, changed = merge_local_short_entries(
        session["entries"],
        max_chars_cjk=int(data.get("max_chars_cjk", 30)),
        max_words_latin=int(data.get("max_words_latin", 14)),
        max_gap=float(data.get("max_gap", 0.5)),
    )
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["entries"] = entries
    payload = local_session_payload(get_local_session(session["id"]))
    payload["merged"] = changed
    return jsonify(payload)


@app.route("/api/local-subtitle/analyze", methods=["POST"])
def analyze_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()
    if not api_url or not api_key or not model:
        return jsonify({"error": "请先填写 AI 设置里的 API 地址、Key 和模型"}), 400

    corrections = analyze_asr_corrections(
        session["entries"], api_url, api_key, model, data.get("prompt", "")
    )
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["corrections"] = corrections
    return jsonify({"corrections": corrections, "count": len(corrections)})


@app.route("/api/local-subtitle/apply-corrections", methods=["POST"])
def apply_local_subtitle_corrections():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404
    corrections = data.get("corrections")
    if corrections is None:
        corrections = session.get("corrections", [])
    applied = apply_asr_corrections(session["entries"], corrections)
    with local_subtitle_lock:
        local_subtitle_sessions[session["id"]]["entries"] = session["entries"]
    payload = local_session_payload(get_local_session(session["id"]))
    payload["applied"] = applied
    return jsonify(payload)


@app.route("/api/local-subtitle/translate", methods=["POST"])
def translate_local_subtitle_session():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    task_id = str(uuid.uuid4())[:8]
    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入翻译队列...",
            "session_id": session["id"],
            "segments": len(session.get("entries", [])),
        }
    opts = {
        "api_url": data.get("api_url", ""),
        "api_key": data.get("api_key", ""),
        "model": data.get("model", ""),
        "prompt": data.get("prompt", ""),
    }
    threading.Thread(
        target=run_local_translate_task,
        args=(task_id, session["id"], opts),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/export", methods=["POST"])
def export_local_subtitle():
    data = request.json or {}
    session = get_local_session(data.get("session_id", ""))
    if not session:
        return jsonify({"error": "字幕会话不存在"}), 404

    output_mode = data.get("output_mode", "bilingual")
    order = data.get("order", "en_top")
    bilingual = output_mode == "bilingual"
    output_text = entries_to_srt(session["entries"], bilingual=bilingual, order=order)
    if not output_text.strip():
        return jsonify({"error": "没有可导出的字幕内容"}), 400

    stem = sanitize_filename(Path(session.get("name") or "local_subtitle").stem)
    suffix = "bilingual" if bilingual else "zh"
    if bilingual:
        suffix += ".zh-top" if order == "zh_top" else ".en-top"
    output_name = f"{stem}.local.{suffix}.srt"
    output_path = DOWNLOADS_DIR / output_name
    output_path.write_text(output_text, encoding="utf-8")
    return jsonify({"file": output_name, "url": f"/files/{output_name}"})


@app.route("/api/local-subtitle/start", methods=["POST"])
def start_local_subtitle():
    if "file" not in request.files:
        return jsonify({"error": "请先选择本地视频或音频文件"}), 400

    upload = request.files["file"]
    original_name = re.split(r"[\\/]", upload.filename or "local_video.mp4")[-1]
    safe_name = sanitize_filename(original_name) or f"{uuid.uuid4().hex}.mp4"

    api_url = request.form.get("api_url", "").strip()
    api_key = request.form.get("api_key", "").strip()
    model = request.form.get("model", "").strip()
    if not api_url or not api_key or not model:
        return jsonify({"error": "请先填写 AI 设置里的 API 地址、Key 和模型"}), 400

    task_id = str(uuid.uuid4())[:8]
    task_dir = DOWNLOADS_DIR / "_local_tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    media_path = task_dir / safe_name
    upload.save(media_path)

    opts = {
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "prompt": request.form.get("prompt", ""),
        "transcribe_model": request.form.get("transcribe_model", "large-v3"),
        "device": request.form.get("device", "cuda"),
        "language": request.form.get("language", "en"),
        "output_mode": request.form.get("output_mode", "bilingual"),
        "initial_prompt": request.form.get("initial_prompt", ""),
    }

    with local_subtitle_lock:
        local_subtitle_tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "已加入队列...",
            "file": None,
            "url": None,
            "segments": 0,
            "corrections": [],
        }

    thread = threading.Thread(
        target=run_local_subtitle_task,
        args=(task_id, media_path, original_name, opts),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/local-subtitle/status/<task_id>")
def local_subtitle_status(task_id):
    with local_subtitle_lock:
        task = dict(local_subtitle_tasks.get(task_id) or {})
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    yt_pattern = r'(youtube\.com|youtu\.be)'
    if not re.search(yt_pattern, url):
        return jsonify({"error": "Please provide a valid YouTube URL"}), 400

    task_id = str(uuid.uuid4())[:8]
    with task_lock:
        tasks[task_id] = {
            "id": task_id,
            "url": url,
            "status": "queued",
            "progress": 0,
            "message": "Queued for download...",
            "files": [],
        }

    options = {
        "type": data.get("type", "video"),
        "quality": data.get("quality", "1080"),
        "audio_format": data.get("audio_format", "mp3"),
        "codec": data.get("codec", "best"),
        "subtitles": data.get("subtitles", False),
        "sub_langs": data.get("sub_langs", "zh,en"),
        "format_id": data.get("format_id"),
        "dual_subtitle": data.get("dual_subtitle", False),
        "sub_options": {
            "font": data.get("sub_font", "Microsoft YaHei"),
            "size": int(data.get("sub_size", 52)),
            "color": data.get("sub_color", "#FFFFFF"),
            "outline_color": data.get("outline_color", "#000000"),
            "sub_mode": data.get("sub_mode", "zh_en"),
            "sub_order": data.get("sub_order", "zh_top"),
            "sub_pos": int(data.get("sub_pos", 2)),
            "margin_v": int(data.get("margin_v", 30)),
            "bg_enabled": data.get("bg_enabled", False),
            "bg_color": data.get("bg_color", "#000000"),
            "bg_opacity": int(data.get("bg_opacity", 50)),
            "bg_radius": int(data.get("bg_radius", 0)),
            "bg_width": int(data.get("bg_width", 80)),
            "bg_height": int(data.get("bg_height", 20)),
            "bg_offset_x": int(data.get("bg_offset_x", 0)),
            "bg_offset_y": int(data.get("bg_offset_y", 0)),
            "translate_sub": data.get("translate_sub", False),
            "api_url": data.get("api_url", ""),
            "api_key": data.get("api_key", ""),
            "model": data.get("model", "gpt-4o-mini"),
        },
        "burn_sub": data.get("burn_sub", False),
        "ae_compat": data.get("ae_compat", False),
    }

    thread = threading.Thread(target=run_download, args=(task_id, url, options))
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def task_progress(task_id):
    def generate():
        while True:
            with task_lock:
                task = tasks.get(task_id)
                if not task:
                    yield f"data: {json.dumps({'error': 'Task not found'})}\n\n"
                    break
                data = {
                    "status": task["status"],
                    "progress": task["progress"],
                    "message": task["message"],
                    "files": task["files"],
                }

            yield f"data: {json.dumps(data)}\n\n"

            if task["status"] in ("completed", "error"):
                break

            import time
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/files")
def list_files():
    return jsonify(list_downloaded_files())


@app.route("/api/formats", methods=["POST"])
def list_formats():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr[:300] or "Failed to fetch"}), 500

        info = json.loads(result.stdout)
        video_fmts, audio_fmts, combined = [], [], []

        for f in info.get("formats", []):
            has_v = f.get("vcodec", "none") != "none"
            has_a = f.get("acodec", "none") != "none"
            entry = {
                "id": f.get("format_id", ""),
                "ext": f.get("ext", ""),
                "res": f.get("resolution", ""),
                "width": f.get("width") or 0,
                "height": f.get("height") or 0,
                "fps": f.get("fps") or 0,
                "vcodec": f.get("vcodec", "none"),
                "acodec": f.get("acodec", "none"),
                "tbr": f.get("tbr") or 0,
                "vbr": f.get("vbr") or 0,
                "abr": f.get("abr") or 0,
                "filesize": f.get("filesize") or 0,
                "proto": f.get("protocol", ""),
            }
            if has_v and has_a:
                combined.append(entry)
            elif has_v:
                video_fmts.append(entry)
            elif has_a:
                audio_fmts.append(entry)

        video_fmts.sort(key=lambda x: (x["height"], x["vbr"]), reverse=True)
        audio_fmts.sort(key=lambda x: x["abr"], reverse=True)
        combined.sort(key=lambda x: (x["height"], x["tbr"]), reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "video": video_fmts,
            "audio": audio_fmts,
            "combined": combined,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout while fetching formats"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thumbnail", methods=["POST"])
def download_thumbnail():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr[:300] or "Failed to fetch info"}), 500

        info = json.loads(result.stdout)
        title = sanitize_filename(info.get("title", "thumbnail"))

        # Pick highest resolution thumbnail
        thumbs = info.get("thumbnails", [])
        best = None
        for t in thumbs:
            w = t.get("width") or 0
            if best is None or w > (best.get("width") or 0):
                best = t
        thumb_url = (best or {}).get("url", "") if best else ""

        if not thumb_url:
            return jsonify({"error": "No thumbnail found"}), 404

        import urllib.request
        raw_path = DOWNLOADS_DIR / f"{title}_thumb_raw"
        urllib.request.urlretrieve(thumb_url, str(raw_path))

        # Convert to JPG with ffmpeg
        thumb_path = DOWNLOADS_DIR / f"{title}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path), "-q:v", "2", str(thumb_path)],
            capture_output=True, timeout=30,
        )
        raw_path.unlink(missing_ok=True)

        return jsonify({
            "saved": thumb_path.name,
            "title": info.get("title", ""),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete/<filename>", methods=["DELETE"])
def delete_file(filename):
    filepath = DOWNLOADS_DIR / filename
    if filepath.exists() and filepath.is_file():
        base = filepath.stem
        for f in DOWNLOADS_DIR.iterdir():
            if f.stem == base or f.stem.startswith(base + "."):
                f.unlink(missing_ok=True)
        return jsonify({"ok": True})
    return jsonify({"error": "File not found"}), 404


@app.route("/api/clear", methods=["POST"])
def clear_files():
    for f in DOWNLOADS_DIR.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/api/fetch-desc", methods=["POST"])
def fetch_description():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr[:300] or "Failed to fetch"}), 500

        info = json.loads(result.stdout)
        timeline = get_subtitle_timeline(url)
        return jsonify({
            "title": info.get("title", ""),
            "description": info.get("description", ""),
            "duration": info.get("duration", 0),
            "tags": info.get("tags", []),
            "uploader": info.get("uploader", ""),
            "channel": info.get("channel", ""),
            "timeline": timeline,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate-desc", methods=["POST"])
def generate_description():
    data = request.json
    url = data.get("url", "").strip()
    style = data.get("style", "").strip()
    api_url = data.get("api_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "gpt-4o-mini").strip()
    include_timeline = data.get("include_timeline", False)
    bilibili = data.get("bilibili", False)

    if not url:
        return jsonify({"error": "目标视频 URL 不能为空"}), 400
    if not api_key:
        return jsonify({"error": "API Key 不能为空"}), 400

    try:
        result = subprocess.run(
            ["yt-dlp", "-j", "--no-warnings", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            return jsonify({"error": "获取视频信息失败"}), 500

        info = json.loads(result.stdout)
        title = info.get("title", "")
        desc = info.get("description", "")
        duration = info.get("duration", 0)
        tags = info.get("tags", [])

        minutes = int(duration // 60)
        seconds = int(duration % 60)
        duration_str = f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒"

        prompt = (
            "你是一个专业的视频简介撰写专家。\n"
            "请参考以下「参考风格」的写作风格、结构和格式，为目标视频撰写一段中文简介。\n\n"
        )
        if style:
            prompt += f"## 参考风格\n{style}\n\n"
        prompt += (
            f"## 目标视频信息\n"
            f"- 标题: {title}\n"
            f"- 时长: {duration_str}\n"
            f"- 标签: {', '.join(tags[:10]) if tags else '无'}\n\n"
            f"## 原始简介\n{desc}\n\n"
        )

        if include_timeline:
            timeline = get_subtitle_timeline(url)
            if timeline:
                prompt += (
                    f"## 字幕时间轴\n{timeline}\n\n"
                    "请在简介中包含时间轴提纲。参考「参考风格」中的时间轴格式，"
                    "将字幕内容概括为简短的中文标题。如无参考格式，使用 \"00:00 标题\" 格式，"
                    "每个节点间隔约 30-60 秒。\n\n"
                )
            else:
                prompt += "注意：该视频无可用字幕，请在简介中手动设计合理的时间轴提纲。\n\n"

        prompt += (
            "请用与参考风格一致的结构、语气和格式，撰写目标视频的中文简介。"
            "保留原文中的关键信息，自然翻译为中文。"
        )

        from openai import OpenAI
        client = OpenAI(base_url=api_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=2000,
        )
        generated = response.choices[0].message.content

        if bilibili:
            generated = (
                f"原视频标题：{title}\n"
                f"原视频链接：{url}\n\n"
                f"{generated}\n\n"
                f"———\n"
                f"转载自 YouTube：{url}"
            )

        safe_title = sanitize_filename(title)
        save_path = DOWNLOADS_DIR / f"{safe_title}.ai-desc.md"
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{generated}\n")

        return jsonify({"description": generated, "saved": save_path.name})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "获取视频信息超时"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


FONT_CN_NAMES = {
    "SmileySans": "得意黑",
    "SmileySans-Oblique": "得意黑 斜体",
    "SmileySans-Bold": "得意黑 粗体",
    "LXGWWenKai": "霞鹜文楷",
    "LXGWWenKai-Bold": "霞鹜文楷 粗体",
    "LXGWWenKai-Light": "霞鹜文楷 细体",
    "LXGWWenKaiMono": "霞鹜文楷等宽",
    "LXGWWenKaiScreen": "霞鹜文楷屏幕",
    "SourceHanSansSC": "思源黑体",
    "SourceHanSerifSC": "思源宋体",
    "SourceCodePro": "Source Code Pro",
    "FiraCode": "Fira Code",
    "JetBrainsMono": "JetBrains Mono",
    "NotoSansSC": "Noto Sans SC",
    "NotoSerifSC": "Noto Serif SC",
}


@app.route("/api/fonts")
def list_fonts():
    fonts = set()
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
        )
        i = 0
        while True:
            try:
                name, _value, _type = winreg.EnumValue(key, i)
                font_name = name.replace(" (TrueType)", "").replace(" (OpenType)", "").strip()
                if font_name:
                    fonts.add(font_name)
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
    except Exception:
        pass

    user_font_dir = Path(os.path.expanduser("~/Fonts"))
    if user_font_dir.exists():
        for f in user_font_dir.rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                fonts.add(FONT_CN_NAMES.get(f.stem, f.stem))

    return jsonify({"fonts": sorted(fonts, key=str.lower)})


@app.route("/files/<path:filename>")
def serve_file(filename):
    return send_from_directory(str(DOWNLOADS_DIR), filename, as_attachment=True)


if __name__ == "__main__":
    import time
    import webbrowser
    from PySide6.QtWidgets import QApplication, QMainWindow
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PySide6.QtCore import QUrl

    port = int(os.environ.get("PORT", 5002))

    server = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    server.start()

    # Wait for Flask to be ready
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.5)

    try:
        qt_app = QApplication([])
        main_win = QMainWindow()
        main_win.setWindowTitle("YouTube Downloader")
        main_win.resize(1000, 750)

        profile = QWebEngineProfile("yt_dl", main_win)
        profile.setHttpUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = QWebEnginePage(profile, main_win)
        browser = QWebEngineView(main_win)
        browser.setPage(page)
        main_win.setCentralWidget(browser)
        main_win.show()
        browser.setUrl(QUrl(f"http://localhost:{port}"))
        qt_app.exec()
    except Exception:
        webbrowser.open(f"http://localhost:{port}")
        while True:
            time.sleep(1)
