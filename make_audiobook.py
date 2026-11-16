#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文有声书流水线：文本 → 分章 → 分段合成 → mp3 / m4b（带章节标记）。

设计要点
--------
* 引擎可插拔。三种都在这台机器上实测过：
    - edge  在线神经音色（14 个中文音色，含方言/港台），质量最好，需联网
    - sapi  离线经典引擎，中文只有 Huihui 一个音色
    - winrt 离线 OneCore 引擎，中文 3 个音色（Huihui / Yaoyao 女 / Kangkang 男）
* 逐块缓存：同一个块重复合成会命中缓存，长书中断后可续跑。
* 产物自校验：章节标记、时长漂移、块字符数都会读回来核对，异常写进 report.json，
  不靠"看起来没问题"。

用法
----
  python make_audiobook.py book.md --voice zh-CN-XiaoxiaoNeural
  python make_audiobook.py book.txt --engine sapi --rate 10
  python make_audiobook.py --list-voices
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FFMPEG = Path(r"D:\deepseek\.tools\ffmpeg.exe")
FFPROBE = Path(r"D:\deepseek\.tools\ffprobe.exe")   # 2026-10 装入；不存在时降级为正则解析
CONFIG = HERE / "config.json"                       # 默认引擎/音色等；命令行优先
LIBS = HERE / "libs"
PS_HELPER = HERE / "tts_offline.ps1"
POWERSHELL = "powershell"

# 离线音色：key -> (engine, voice id, 展示名)
OFFLINE_VOICES = {
    "huihui":   ("sapi",  "Microsoft Huihui Desktop",      "离线·慧慧 Huihui（女，zh-CN）"),
    "yaoyao":   ("winrt", "MSTTS_V110_zhCN_YaoyaoM",       "离线·瑶瑶 Yaoyao（女，zh-CN）"),
    "kangkang": ("winrt", "MSTTS_V110_zhCN_KangkangM",     "离线·康康 Kangkang（男，zh-CN）"),
}
EDGE_DEFAULT = "zh-CN-XiaoxiaoNeural"

CHAPTER_PATTERNS = [
    re.compile(r"^(#{1,3})\s+(.+?)\s*#*$"),                                  # markdown 标题
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百千零〇两]+\s*[章回节篇卷部].{0,40}$"),
    re.compile(r"^\s*(Chapter|CHAPTER)\s+\d+.{0,40}$"),
    re.compile(r"^\s*[0-9]{1,3}\s*[、.．]\s*\S.{0,40}$"),
]
SENT_END = "。！？；!?;…"
DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
CHAP_RE = re.compile(r"Chapter #(\d+):(\d+): start ([\d.]+), end ([\d.]+)")
CHAP_TITLE_RE = re.compile(
    r"Chapter #\d+:\d+: start [\d.]+, end [\d.]+\s*\n\s*Metadata:\s*\n\s*title\s*:\s*(.+)")


# ---------------------------------------------------------------- 基础工具
def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    """读 config.json（默认引擎/音色/语速…）。缺失或写坏都不致命，但要说明。"""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return {k: v for k, v in cfg.items() if not k.startswith("_")}
    except FileNotFoundError:
        return {}
    except Exception as e:                                       # noqa: BLE001
        log(f"!! {CONFIG.name} 解析失败（{type(e).__name__}: {e}），改用命令行/内置默认")
        return {}


def run(args, **kw):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def scrub_markdown(text: str) -> str:
    """去掉不该被念出来的标记，但保留正文。"""
    text = re.sub(r"```.*?```", "\n", text, flags=re.S)          # 代码块
    text = re.sub(r"^\s*\|.*\|\s*$", "\n", text, flags=re.M)     # 表格行
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)             # 图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)         # 链接保留文字
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)             # 引用符
    text = re.sub(r"(\*\*|__|\*|`|~~)", "", text)                # 强调/行内代码
    text = re.sub(r"^\s*[-+*]\s+", "", text, flags=re.M)         # 列表符
    return text


def load_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        import docx  # python-docx
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    if suffix in (".txt", ".md", ".markdown", ""):
        return path.read_text(encoding="utf-8", errors="replace")
    raise SystemExit(f"暂不支持的输入格式：{suffix}（支持 .txt/.md/.docx）")


def split_chapters(text: str, custom: str | None = None) -> list[tuple[str, str]]:
    """返回 [(章节标题, 正文), ...]。识别不到章节就当成单章。"""
    pats = [re.compile(custom)] if custom else CHAPTER_PATTERNS
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or len(line) > 60:
            continue
        for p in pats:
            m = p.match(raw.rstrip())
            if m:
                title = (m.group(2) if (p.groups >= 2 and m.lastindex and m.lastindex >= 2)
                         else line).strip()
                marks.append((i, title or line))
                break
    if not marks:
        return [("全文", text)]
    chapters: list[tuple[str, str]] = []
    for idx, (line_no, title) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        body = "\n".join(lines[line_no + 1:end]).strip()
        if body:
            chapters.append((title, body))
    return chapters or [("全文", text)]


def split_chunks(body: str, max_chars: int) -> list[str]:
    """按中文句末标点切句，再合并到 max_chars 以内。"""
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n|\n", body):
        para = para.strip()
        if not para:
            continue
        buf = ""
        for ch in para:
            buf += ch
            if ch in SENT_END:
                pieces.append(buf)
                buf = ""
        if buf.strip():
            pieces.append(buf)
    chunks: list[str] = []
    cur = ""
    for s in pieces:
        s = s.strip()
        if not s:
            continue
        if len(s) > max_chars:                      # 超长句按逗号硬切
            for part in re.split(r"(?<=[，,、])", s):
                if len(cur) + len(part) > max_chars and cur:
                    chunks.append(cur); cur = ""
                while len(part) > max_chars:
                    chunks.append(part[:max_chars]); part = part[max_chars:]
                cur += part
            continue
        if len(cur) + len(s) > max_chars and cur:
            chunks.append(cur); cur = ""
        cur += s
    if cur.strip():
        chunks.append(cur)
    return [c for c in (c.strip() for c in chunks) if c]


def safe_name(name: str, limit: int = 40) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    return (name[:limit] or "untitled")


# ---------------------------------------------------------------- 音频探针
def probe_audio(path: Path) -> dict:
    """读音频元数据。优先用 ffprobe（结构化 JSON）；没有 ffprobe 时退回解析 `ffmpeg -i` 的 stderr。

    本机原来没有 ffprobe，正则路径是当时的替代方案，保留作降级——两条路都实测过。
    """
    info = {"path": str(path), "exists": path.exists(), "size": 0,
            "duration": None, "chapters": [], "source": None}
    if not path.exists():
        return info
    info["size"] = path.stat().st_size

    if FFPROBE.exists():
        r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-show_chapters", "-of", "json", path])
        try:
            data = json.loads(r.stdout)
            info["duration"] = round(float(data["format"]["duration"]), 3)
            for ch in data.get("chapters", []):
                info["chapters"].append({
                    "index": int(ch.get("id", len(info["chapters"]))),
                    "start": round(float(ch["start_time"]), 3),
                    "end": round(float(ch["end_time"]), 3),
                    "title": (ch.get("tags") or {}).get("title"),
                })
            info["source"] = "ffprobe"
            return info
        except Exception:                                        # noqa: BLE001
            info["chapters"] = []                                # 退回正则，不把半成品当结果

    err = run([FFMPEG, "-hide_banner", "-i", path]).stderr
    m = DUR_RE.search(err)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                 + float(m.group(3)), 3)
    for c in CHAP_RE.finditer(err):
        info["chapters"].append({"index": int(c.group(1)), "start": float(c.group(3)),
                                 "end": float(c.group(4)), "title": None})
    for i, t in enumerate(CHAP_TITLE_RE.findall(err)):
        if i < len(info["chapters"]):
            info["chapters"][i]["title"] = t.strip()
    info["source"] = "ffmpeg-regex"
    return info


def probe_levels(path: Path) -> dict:
    """量最终音频的实际电平。"有文件"不等于"有声音"——空音频是合法 WAV/MP3。"""
    r = run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
             "-af", "volumedetect", "-f", "null", "-"])
    err = r.stderr
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
    return {"mean_db": float(mean.group(1)) if mean else None,
            "max_db": float(peak.group(1)) if peak else None}


# ---------------------------------------------------------------- 合成引擎
def chunk_cache_path(work: Path, key: str, engine: str) -> Path:
    ext = "mp3" if engine == "edge" else "wav"
    return work / "chunks" / f"{key}.{ext}"


def synth_edge(jobs: list[dict], voice: str, rate: str, volume: str, pitch: str,
               retries: int = 4) -> list[dict]:
    """volume 传字符串（如 "+0%"）；语义与离线引擎的 0-100 绝对音量不同，调用方负责换算。"""
    """online 神经音色；并发 3，逐块重试。"""
    sys.path.insert(0, str(LIBS)) if LIBS.exists() else None
    try:
        import edge_tts
    except ImportError as e:
        raise SystemExit(f"edge 引擎需要 edge-tts：{e}（已装在 {LIBS}）")

    async def one(job, sem):
        async with sem:
            for attempt in range(1, retries + 1):
                try:
                    comm = edge_tts.Communicate(job["text"], voice, rate=rate,
                                                volume=volume, pitch=pitch)
                    await comm.save(str(job["out"]))
                    if job["out"].exists() and job["out"].stat().st_size > 1024:
                        return {"id": job["id"], "ok": True, "chars": len(job["text"]),
                                "bytes": job["out"].stat().st_size, "attempt": attempt}
                    raise RuntimeError("输出文件过小")
                except Exception as e:                      # noqa: BLE001
                    if attempt == retries:
                        return {"id": job["id"], "ok": False, "chars": len(job["text"]),
                                "bytes": 0, "error": f"{type(e).__name__}: {e}"}
                    await asyncio.sleep(1.5 * attempt)

    async def main():
        sem = asyncio.Semaphore(3)
        return await asyncio.gather(*[one(j, sem) for j in jobs])

    return list(asyncio.run(main()))


def assert_ascii_helper() -> None:
    """调用前机械校验 tts_offline.ps1 是纯 ASCII。

    实测（2026-10）：PS 5.1 在本机按 GBK 读无 BOM 的 UTF-8 源码，中文注释的字节会解码成
    吞掉换行的乱码，把下一行代码并进注释里。后果是**脚本照常跑完、不报任何错**，
    但 `$esc = ...` 被吃掉后 SSML 变成空文本，合成出 46 字节的空 WAV（静音）。
    这种"沉默的错误"只能靠机械校验挡住，不能靠记性。
    """
    data = PS_HELPER.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    if bad:
        line = data[:bad[0]].count(b"\n") + 1
        raise SystemExit(
            f"{PS_HELPER.name} 含 {len(bad)} 个非 ASCII 字节（首个在第 {line} 行）。\n"
            f"PS 5.1 会按 GBK 误读它并静默吞掉换行，导致合成出空音频且不报错。\n"
            f"请把该文件里的注释改成英文（或加 UTF-8 BOM）后再运行。")


def synth_offline(jobs: list[dict], engine: str, voice: str, rate: int,
                  volume: int | None) -> list[dict]:
    """离线引擎：一次 PowerShell 调用合成整批（每章一批，便于续跑）。"""
    if not jobs:
        return []
    assert_ascii_helper()
    man_dir = jobs[0]["out"].parent
    manifest = man_dir / f"_manifest_{int(time.time()*1000)}.json"
    result = man_dir / f"_result_{int(time.time()*1000)}.json"
    payload = {
        "engine": engine,
        "voice": voice,
        "rate": rate,
        "rate_ssml": (f"{rate:+d}%" if rate else ""),
        "volume": volume,
        "result_file": str(result),
        "items": [{"id": j["id"], "text_file": str(j["text_file"]), "out_wav": str(j["out"])}
                  for j in jobs],
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r = run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             PS_HELPER, "-Manifest", manifest])
    if not result.exists():
        raise SystemExit(f"离线合成失败（无结果文件）\nstdout: {r.stdout[-800:]}\n"
                         f"stderr: {r.stderr[-800:]}")
    data = json.loads(result.read_text(encoding="utf-8"))
    manifest.unlink(missing_ok=True)
    result.unlink(missing_ok=True)
    return data["results"]


def synthesize(chunks: list[str], engine: str, voice: str, rate_pct: int, volume: int | None,
               pitch: int, work: Path, force: bool = False) -> tuple[list[Path], list[dict]]:
    """合成所有块，返回 (按顺序的音频路径, 结果明细)。命中缓存的块不会重合成。"""
    (work / "chunks").mkdir(parents=True, exist_ok=True)
    (work / "text").mkdir(parents=True, exist_ok=True)
    jobs, cached = [], []
    for i, text in enumerate(chunks):
        key = hashlib.sha1(
            f"{engine}|{voice}|{rate_pct}|{volume}|{pitch}|{text}".encode("utf-8")
        ).hexdigest()[:16]
        out = chunk_cache_path(work, key, engine)
        if out.exists() and out.stat().st_size >= 4096 and not force:
            cached.append({"id": key, "ok": True, "chars": len(text),
                           "bytes": out.stat().st_size, "cached": True})
            jobs.append({"id": key, "text": text, "out": out, "text_file": None,
                         "index": i, "cached": True})
        else:
            tf = work / "text" / f"{key}.txt"
            tf.write_text(text, encoding="utf-8")
            jobs.append({"id": key, "text": text, "out": out, "text_file": tf,
                         "index": i, "cached": False})

    todo = [j for j in jobs if not j["cached"]]
    if todo:
        if engine == "edge":
            # edge 的 volume 是相对偏移；离线引擎的 volume 是绝对音量（0=静音），语义不同
            r = synth_edge(todo, voice, f"{rate_pct:+d}%",
                           f"{volume:+d}%" if volume is not None else "+0%",
                           f"{pitch:+d}Hz")
        else:
            r = synth_offline(todo, engine, voice,
                              max(-10, min(10, round(rate_pct / 10))), volume)
        by_id = {x["id"]: x for x in r}
        for j in todo:
            j["result"] = by_id.get(j["id"], {"ok": False, "error": "结果里没有这个块"})
    results = []
    for j in jobs:
        res = j["result"] if "result" in j else {"id": j["id"], "ok": True,
                                                 "chars": len(j["text"]),
                                                 "bytes": j["out"].stat().st_size,
                                                 "cached": True}
        res["index"] = j["index"]
        res["expect_chars"] = len(j["text"])
        res["chars_ok"] = res.get("chars") == len(j["text"])
        # 以磁盘实况为准：合成器可能报成功却写出空音频（实测 OneCore 会这样）
        on_disk = j["out"].stat().st_size if j["out"].exists() else 0
        res["bytes_ondisk"] = on_disk
        if res.get("ok") and on_disk < 4096:
            res["ok"] = False
            res["error"] = f"输出过小（{on_disk} 字节，疑似空音频）"
        results.append(res)
    return [j["out"] for j in jobs], results


# ---------------------------------------------------------------- 装配
def concat_chapter(files: list[Path], out: Path, bitrate: str, ar: int) -> None:
    lst = out.with_suffix(".list.txt")
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), encoding="utf-8")
    r = run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", ar, "-ac", "1", out])
    lst.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        raise SystemExit(f"章节拼接失败 {out.name}: {r.stderr[-400:]}")


def write_ffmetadata(path: Path, title: str, author: str, chapters: list[dict]) -> None:
    lines = [";FFMETADATA1", f"title={title}", f"artist={author}", f"album={title}",
             f"album_artist={author}", "genre=Audiobook"]
    cursor = 0.0
    for ch in chapters:
        start_ms, end_ms = int(round(cursor * 1000)), int(round((cursor + ch["duration"]) * 1000))
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}",
                  f"title={ch['title']}"]
        cursor += ch["duration"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_container(chapter_files: list[Path], meta: Path, out: Path, container: str,
                    bitrate: str, ar: int, cover: Path | None) -> tuple[bool, str]:
    """container: 'mp3' | 'm4b'。先试 -c copy（mp3 无损拼接），失败或章节丢失再由调用方重编。"""
    lst = out.with_suffix(".list.txt")
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in chapter_files), encoding="utf-8")
    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", lst, "-i", meta]
    if cover and cover.exists():
        args += ["-i", cover]
    args += ["-map", "0:a", "-map_metadata", "1"]
    if container == "mp3":
        args += ["-c:a", "copy", "-id3v2_version", "3"]
    else:
        args += ["-c:a", "aac", "-b:a", bitrate, "-ac", "1", "-ar", ar,
                 "-movflags", "+faststart", "-f", "ipod"]
    if cover and cover.exists():
        args += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    args.append(str(out))
    r = run(args)
    lst.unlink(missing_ok=True)
    return r.returncode == 0 and out.exists(), r.stderr.strip()


def build_container_reencode(chapter_files: list[Path], meta: Path, out: Path, container: str,
                             bitrate: str, ar: int, cover: Path | None) -> tuple[bool, str]:
    lst = out.with_suffix(".list.txt")
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in chapter_files), encoding="utf-8")
    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", lst, "-i", meta]
    if cover and cover.exists():
        args += ["-i", cover]
    args += ["-map", "0:a", "-map_metadata", "1",
             "-c:a", "libmp3lame" if container == "mp3" else "aac",
             "-b:a", bitrate, "-ac", "1", "-ar", ar]
    if container == "mp3":
        args += ["-id3v2_version", "3"]
    else:
        args += ["-movflags", "+faststart", "-f", "ipod"]
    if cover and cover.exists():
        args += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    args.append(str(out))
    r = run(args)
    lst.unlink(missing_ok=True)
    return r.returncode == 0 and out.exists(), r.stderr.strip()


# ---------------------------------------------------------------- 音色清单
def list_voices() -> None:
    log("离线音色（不需要联网，音质为 2013 年拼接式引擎）：")
    for k, (eng, vid, label) in OFFLINE_VOICES.items():
        log(f"  --engine {eng:5s} --voice {k:9s}  {label}")
    log("\n在线神经音色（edge 引擎，需联网）：")
    try:
        sys.path.insert(0, str(LIBS))
        import edge_tts
        vs = asyncio.run(edge_tts.list_voices())
        zh = [v for v in vs if v["Locale"].startswith("zh")]
        for v in sorted(zh, key=lambda x: x["Locale"]):
            log(f"  --voice {v['ShortName']:34s} {v['Gender']:6s} {v['Locale']}")
        log(f"  共 {len(zh)} 个中文音色")
    except Exception as e:                                   # noqa: BLE001
        log(f"  取不到在线清单：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="中文有声书流水线")
    ap.add_argument("input", nargs="?", help="输入文件（.txt/.md/.docx）")
    ap.add_argument("--out", default=None, help="输出目录")
    ap.add_argument("--engine", default="auto", choices=["auto", "edge", "sapi", "winrt"])
    ap.add_argument("--voice", default=None, help="音色：edge 用 ShortName，离线用 huihui/yaoyao/kangkang")
    ap.add_argument("--rate", type=int, default=None, help="语速偏移百分比（edge 用，离线按 /10 映射）")
    ap.add_argument("--volume", type=int, default=None,
                    help="edge：相对音量偏移百分比；离线引擎：绝对音量 0-100（0 会静音，慎用）")
    ap.add_argument("--pitch", type=int, default=None, help="音高偏移 Hz（edge 用）")
    ap.add_argument("--chunk-chars", type=int, default=None, help="每块最大字数")
    ap.add_argument("--title", default=None)
    ap.add_argument("--author", default=None)
    ap.add_argument("--cover", default=None, help="封面图片（jpg/png）")
    ap.add_argument("--chapter-regex", default=None, help="自定义章节标题正则")
    ap.add_argument("--max-chars", type=int, default=None, help="只处理前 N 字（试跑用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印分章分段，不合成")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新合成")
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args()

    if args.list_voices:
        list_voices()
        return 0
    if not args.input:
        ap.error("需要输入文件，或用 --list-voices")

    src = Path(args.input).resolve()
    if not src.exists():
        raise SystemExit(f"找不到输入文件：{src}")

    # ---- 参数优先级：命令行 > config.json > 内置默认（并把来源打印出来，避免"参数悄悄没生效"）
    cfg = load_config()
    source: dict[str, str] = {}

    def pick(name, cli_val):
        if cli_val is not None:
            source[name] = "命令行"
            return cli_val
        if cfg.get(name) is not None:
            source[name] = "config.json"
            return cfg[name]
        source[name] = "内置默认"
        return None

    engine = pick("engine", args.engine if args.engine != "auto" else None) or "auto"
    voice_arg = pick("voice", args.voice)
    rate_val = pick("rate", args.rate)
    rate_val = 0 if rate_val is None else int(rate_val)
    volume_val = pick("volume", args.volume)
    pitch_val = pick("pitch", args.pitch)
    pitch_val = 0 if pitch_val is None else int(pitch_val)
    author_val = pick("author", args.author) or "未知"

    if engine == "auto":
        sys.path.insert(0, str(LIBS))
        try:
            import edge_tts  # noqa: F401
            engine = "edge"
        except ImportError:
            engine = "sapi"
        log(f"[auto] 选用引擎：{engine}")

    if engine == "edge":
        voice_key = voice_arg or EDGE_DEFAULT
        engine_voice = voice_key          # edge 直接用 ShortName
        offline = None
    else:
        voice_key = voice_arg or "huihui"
        if voice_key not in OFFLINE_VOICES:
            raise SystemExit(f"离线音色只能是 {list(OFFLINE_VOICES)}（当前 {voice_key}）")
        if OFFLINE_VOICES[voice_key][0] != engine:
            raise SystemExit(f"音色 {voice_key} 属于 {OFFLINE_VOICES[voice_key][0]} 引擎，"
                             f"与 --engine {engine} 不符（或用 --engine auto）")
        offline = OFFLINE_VOICES[voice_key]
        engine_voice = offline[1]        # 传给引擎的真实音色名（不是 CLI 的短键）

    log(f"参数来源：engine={source['engine']} voice={source['voice']} "
        f"rate={source['rate']} → 实际使用 engine={engine} voice={voice_key} "
        f"rate={rate_val} pitch={pitch_val}")

    title = args.title or src.stem
    outdir = Path(args.out).resolve() if args.out else (HERE / "out" / safe_name(title))
    work = outdir / "work"
    chapters_dir = outdir / "chapters"
    for d in (outdir, work, chapters_dir):
        d.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    raw = load_text(src)
    if args.max_chars:
        raw = raw[:args.max_chars]
    text = scrub_markdown(raw)
    chapters = split_chapters(text, args.chapter_regex)
    chunk_chars = args.chunk_chars or (900 if engine == "edge" else 240)
    plan = [(t, split_chunks(b, chunk_chars)) for t, b in chapters]
    total_chars = sum(len(c) for _, cs in plan for c in cs)
    total_chunks = sum(len(cs) for _, cs in plan)
    log(f"输入 {src.name}：{len(chapters)} 章 / {total_chunks} 块 / {total_chars} 字"
        f"（块上限 {chunk_chars} 字）")
    for i, (t, cs) in enumerate(plan, 1):
        log(f"  第{i:02d}章 {t}  {len(cs)} 块 {sum(len(c) for c in cs)} 字")
    if args.dry_run:
        return 0

    ar = 24000 if engine == "edge" else 22050
    bitrate = "64k"
    all_results: list[dict] = []
    ch_records: list[dict] = []

    for i, (chtitle, chunks) in enumerate(plan, 1):
        cstart = time.time()
        files, results = synthesize(chunks, engine, engine_voice, rate_val, volume_val,
                                    pitch_val, work, args.force)
        bad = [r for r in results if not r.get("ok") or not r.get("chars_ok")]
        if bad:
            log(f"  !! 第{i}章有 {len(bad)} 块合成异常：{bad[0].get('error', '字符数不符')}")
        for r in results:
            r["chapter"] = i
            all_results.append(r)
        cfile = chapters_dir / f"{i:02d}-{safe_name(chtitle)}.mp3"
        concat_chapter(files, cfile, bitrate, ar)
        dur = probe_audio(cfile)["duration"] or 0.0
        ch_records.append({"index": i, "title": chtitle, "file": cfile.name, "chars":
                           sum(len(c) for c in chunks), "chunks": len(chunks),
                           "duration": dur})
        log(f"  [{i}/{len(plan)}] {chtitle} -> {cfile.name}  {dur:.1f}s "
            f"({time.time()-cstart:.1f}s, 缓存命中 "
            f"{sum(1 for r in results if r.get('cached'))}/{len(results)})")

    meta = outdir / "_ffmetadata.txt"
    write_ffmetadata(meta, title, author_val, ch_records)
    declared = round(sum(c["duration"] for c in ch_records), 3)
    ch_files = [chapters_dir / c["file"] for c in ch_records]
    cover = Path(args.cover).resolve() if args.cover else None

    outputs, notes = {}, []
    for container, name in (("mp3", f"{safe_name(title)}.mp3"), ("m4b", f"{safe_name(title)}.m4b")):
        target = outdir / name
        ok, err = build_container(ch_files, meta, target, container, bitrate, ar, cover)
        info = probe_audio(target) if ok else {"chapters": [], "duration": None}
        if not ok or len(info["chapters"]) != len(ch_records):
            note = f"{container}: 直接封装未写入章节标记（{err[:120] or '章节数不符'}），改为重编码"
            notes.append(note)
            log("  " + note)
            ok, err = build_container_reencode(ch_files, meta, target, container, bitrate, ar, cover)
            info = probe_audio(target)
        info["build_error"] = err if not ok else ""
        outputs[container] = info

    # ---- 自校验：不靠"看起来没问题"
    verification = {
        "chunks_total": total_chunks,
        "chunks_ok": sum(1 for r in all_results if r.get("ok")),
        "chunks_chars_mismatch": sum(1 for r in all_results if not r.get("chars_ok")),
        "offline_synth_failures": [r for r in all_results
                                   if not r.get("ok") and engine != "edge"],
        "chapter_files": len(ch_records),
        "declared_total_s": declared,
    }
    for k, info in outputs.items():
        got = len(info["chapters"])
        titles_ok = all((info["chapters"][i].get("title") or "") == ch_records[i]["title"]
                        for i in range(min(got, len(ch_records))))
        levels = probe_levels(Path(info["path"])) if info["size"] else {"mean_db": None,
                                                                        "max_db": None}
        verification[f"{k}_chapters"] = got
        verification[f"{k}_chapters_ok"] = got == len(ch_records)
        verification[f"{k}_titles_match"] = titles_ok
        verification[f"{k}_duration_s"] = info["duration"]
        verification[f"{k}_duration_delta_s"] = (
            round((info["duration"] or 0) - declared, 3) if info["duration"] else None)
        verification[f"{k}_mean_db"] = levels["mean_db"]
        verification[f"{k}_max_db"] = levels["max_db"]
        # 静音判定：正常语音的平均电平远高于 -50 dBFS
        verification[f"{k}_has_sound"] = (levels["mean_db"] is not None
                                          and levels["mean_db"] > -50)

    report = {
        "input": str(src), "title": title, "author": author_val,
        "engine": engine, "voice": voice_key, "engine_voice": engine_voice,
        "rate_pct": rate_val, "volume_pct": volume_val, "pitch_hz": pitch_val,
        "param_source": source,
        "sample_rate": ar, "bitrate": bitrate,
        "chapters": [{k: v for k, v in c.items() if k != "duration"} | {"duration": c["duration"]}
                     for c in ch_records],
        "outputs": {k: {"path": v["path"], "size": v["size"], "duration": v["duration"],
                        "chapters": len(v["chapters"]), "build_error": v.get("build_error", "")}
                    for k, v in outputs.items()},
        "verification": verification, "notes": notes,
        "elapsed_s": round(time.time() - t0, 1),
    }
    (outdir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    meta.unlink(missing_ok=True)

    log("\n=== 自校验 ===")
    for k, v in verification.items():
        if isinstance(v, list):
            log(f"  {k}: {len(v)}")
        else:
            log(f"  {k}: {v}")
    log(f"产物：{outdir}")
    for k, v in outputs.items():
        log(f"  {k}: {v['size']/1024:.0f} KB, {v['duration']}s, {len(v['chapters'])} 章")
    log(f"总耗时 {report['elapsed_s']}s -> report.json")

    good = (verification["chunks_ok"] == total_chunks
            and verification["chunks_chars_mismatch"] == 0
            and all(verification[f"{k}_chapters_ok"] for k in outputs)
            and all(verification[f"{k}_titles_match"] for k in outputs)
            and all(verification[f"{k}_has_sound"] for k in outputs))
    log("VERDICT: " + ("PASS" if good else "FAIL"))
    return 0 if good else 2


if __name__ == "__main__":
    sys.exit(main())
