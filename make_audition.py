#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色试听表生成器：同一段话，用本机所有可用中文音色各合成一遍。

为什么要有这个：音色是主观的，"我能选音色"这种话没有说服力，必须让耳朵判断。
产出一张可点击播放的 HTML（外加一个连续播放的合集），选定后把对应命令抄进 make_audiobook.py 即可。

用法：
  python make_audition.py                          # 用内置中性样本
  python make_audition.py --text-file my.txt --chars 140
  python make_audition.py --offline-only           # 只跑离线音色（不联网）
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_audiobook as M  # noqa: E402

DEFAULT_TEXT = (
    "这是一段用来挑选音色的样本。它包含陈述、疑问和感叹：今天的天气不错，你打算出门走走吗？"
    "真希望这本书能念得再自然一点！样本里还混了数字 2026 和英文词 audiobook，"
    "用来听听中英混排与数字的读法。"
)


def collect_edge_voices() -> list[dict]:
    sys.path.insert(0, str(M.LIBS))
    import edge_tts
    voices = asyncio.run(edge_tts.list_voices())
    out = []
    for v in sorted(voices, key=lambda x: x["ShortName"]):
        if v["Locale"].startswith("zh"):
            out.append({"engine": "edge", "voice": v["ShortName"],
                        "label": f"{v['ShortName']}（{v['Gender']}，{v['Locale']}）",
                        "kind": "在线"})
    return out


def collect_offline_voices() -> list[dict]:
    return [{"engine": eng, "voice": key, "engine_voice": vid,
             "label": f"{label}", "kind": "离线"}
            for key, (eng, vid, label) in M.OFFLINE_VOICES.items()]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成中文音色试听表")
    ap.add_argument("--text-file", default=None)
    ap.add_argument("--chars", type=int, default=110, help="样本字数上限")
    ap.add_argument("--out", default=str(HERE / "audition"))
    ap.add_argument("--offline-only", action="store_true")
    ap.add_argument("--rate", type=int, default=0)
    args = ap.parse_args()

    text = (Path(args.text_file).read_text(encoding="utf-8") if args.text_file
            else DEFAULT_TEXT)
    text = re.sub(r"\s+", "", text)[:args.chars]
    outdir = Path(args.out).resolve()
    work = outdir / "work"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"样本 {len(text)} 字：{text[:36]}…")

    voices = collect_offline_voices()
    if not args.offline_only:
        try:
            voices += collect_edge_voices()
        except Exception as e:                                   # noqa: BLE001
            print(f"!! 取在线音色失败（{type(e).__name__}: {e}），只跑离线音色")

    records, t0 = [], time.time()
    for i, v in enumerate(voices, 1):
        engine = v["engine"]
        engine_voice = v.get("engine_voice") or v["voice"]
        tag = f"{i:02d}-{v['kind']}-" + re.sub(r'[\\/:*?"<>|\s]+', "_", v["label"])[:40]
        target = outdir / f"{tag}.mp3"
        try:
            files, results = M.synthesize([text], engine, engine_voice, args.rate,
                                          None, 0, work / engine, force=False)
            bad = [r for r in results if not r.get("ok")]
            if bad:
                raise RuntimeError(bad[0].get("error", "合成失败"))
            ar = 24000 if engine == "edge" else 22050
            M.concat_chapter(files, target, "64k", ar)
            lv = M.probe_levels(target)
            dur = M.probe_audio(target)["duration"]
            sound = lv["mean_db"] is not None and lv["mean_db"] > -50
            records.append({**v, "engine_voice": engine_voice, "file": target.name,
                            "duration": dur, "mean_db": lv["mean_db"], "has_sound": sound})
            print(f"  [{i}/{len(voices)}] {v['label']:<44} {dur:6.1f}s "
                  f"mean={lv['mean_db']}dB sound={sound}")
        except Exception as e:                                   # noqa: BLE001
            print(f"  [{i}/{len(voices)}] {v['label']:<44} FAIL {type(e).__name__}: {e}")
            records.append({**v, "engine_voice": engine_voice, "file": None,
                            "error": f"{type(e).__name__}: {e}"})

    ok = [r for r in records if r.get("file")]
    # 连续播放合集：统一重编码后拼接，方便一次性听完全部音色
    combined = None
    if ok:
        lst = outdir / "_audition_list.txt"
        lst.write_text("".join(f"file '{(outdir / r['file']).as_posix()}'\n" for r in ok),
                       encoding="utf-8")
        combined = outdir / "音色合集.mp3"
        import subprocess
        r = subprocess.run([str(M.FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "24000", "-ac", "1",
                            str(combined)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        lst.unlink(missing_ok=True)
        if r.returncode != 0:
            print("  合集生成失败：", r.stderr[:200])
            combined = None

    # ---- HTML 试听表
    rows = []
    for r in records:
        cli = (f'--engine {r["engine"]} --voice {r["voice"]}' if r["engine"] != "edge"
               else f'--voice {r["voice"]}')
        if r.get("file"):
            player = f'<audio controls preload="none" src="{html.escape(r["file"])}"></audio>'
            meta = f'{r["duration"]:.1f}s · 平均电平 {r["mean_db"]} dB'
        else:
            player = '<span class="fail">合成失败</span>'
            meta = html.escape(r.get("error", ""))
        rows.append(f"""    <tr>
      <td class="kind">{r['kind']}</td>
      <td class="name">{html.escape(r['label'])}</td>
      <td>{player}</td>
      <td class="meta">{meta}</td>
      <td><code>python make_audiobook.py book.md {html.escape(cli)}</code></td>
    </tr>""")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>中文音色试听表</title>
<style>
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; color: #1a1a1a; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  .sample {{ background: #f5f7fa; border-left: 3px solid #4a7; padding: 10px 12px; font-size: 13px;
             margin-bottom: 18px; white-space: pre-wrap; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border-bottom: 1px solid #e3e6ea; padding: 8px 10px; text-align: left;
            vertical-align: middle; font-size: 13px; }}
  th {{ background: #f0f2f5; position: sticky; top: 0; }}
  audio {{ width: 260px; height: 32px; }}
  .kind {{ color: #666; white-space: nowrap; }}
  .name {{ font-weight: 600; }}
  .meta {{ color: #666; white-space: nowrap; }}
  code {{ background: #f0f2f5; padding: 2px 5px; border-radius: 3px; font-size: 12px; }}
  .fail {{ color: #c00; }}
  .foot {{ margin-top: 18px; color: #666; font-size: 12px; }}
</style>
</head>
<body>
<h1>中文音色试听表</h1>
<div class="sub">同一段文字、同一语速，逐个音色合成。共 {len(records)} 个音色，
成功 {len(ok)} 个{('，合并文件：<a href="' + html.escape(combined.name) + '">音色合集.mp3</a>') if combined else ''}。</div>
<div class="sample">样本（{len(text)} 字）：{html.escape(text)}</div>
<table>
  <thead><tr><th>来源</th><th>音色</th><th>试听</th><th>时长 / 电平</th><th>选定后这样用</th></tr></thead>
  <tbody>
{chr(10).join(rows)}
  </tbody>
</table>
<div class="foot">离线音色不需要联网（SAPI/OneCore 引擎，2013 年拼接式，机械感明显）；
在线音色为神经合成，质量明显更好但需要网络。平均电平接近 -91 dB 即表示没有声音。</div>
</body>
</html>
"""
    (outdir / "试听表.html").write_text(doc, encoding="utf-8")
    (outdir / "audition.json").write_text(
        json.dumps({"text": text, "records": records, "combined": combined.name if combined else None,
                    "elapsed_s": round(time.time() - t0, 1)}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n试听表：{outdir / '试听表.html'}")
    print(f"成功 {len(ok)}/{len(records)}，耗时 {time.time()-t0:.1f}s")
    return 0 if len(ok) == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
