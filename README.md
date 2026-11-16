> **独立发布版**：本仓库从个人 DSH 工作区 `D:\deepseek` 抽离，只包含版本控制白名单里的源码与文档；
> 模型、音频、凭据、构建产物与机器数据未包含。文中 `D:\deepseek\...` 路径按本仓库根目录理解。
>
# 有声书流水线（audiobook）

把一份文本变成**带章节标记的有声书**：`txt / md / docx` → 分章 → 分段合成 → `mp3` + `m4b`。
三条引擎路线都在本机实测跑通，音色可选、可试听、可锁定。

```
python make_audiobook.py book.md --voice zh-CN-XiaoxiaoNeural
python make_audiobook.py book.txt --engine sapi --voice huihui --rate 10
python make_audiobook.py --list-voices          # 列出全部可用音色
python make_audiobook.py book.md --dry-run      # 只看分章分段，不合成
```

## 默认音色写在 `config.json` 里

不写 `--voice` 时用 `config.json` 的值（**优先级：命令行 > config.json > 内置默认**，
而且每次运行都会打印**每个参数是从哪来的**，避免"参数悄悄没生效"）：

```json
{ "engine": "edge", "voice": "zh-CN-XiaoxiaoNeural", "rate": 0, "volume": null, "pitch": 0,
  "author": "未署名" }
```

当前选定：**`zh-CN-XiaoxiaoNeural`（晓晓，女声，zh-CN）**——从 17 个音色的试听表里挑的。
实测（`samples/音色样片.md`，876 字 / 3 章 / 172.9 秒）：章节 3/3、标题逐字相等、
电平 -20.3 dB、`VERDICT: PASS`。

`config.json` 写坏不会致命：解析失败会打印一行警告并退回命令行/内置默认。

解释器用运行时常驻 Python（含 python-docx）：
`C:\Users\<you>\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`

## 三个引擎（都实测过，不是"应该能用"）

| 引擎 | 音色 | 联网 | 备注 |
| --- | --- | --- | --- |
| `edge` | **14 个中文神经音色**（含辽宁/陕西话、港台） | 需要 | 质量最好；支持 `--rate/--volume/--pitch`。依赖装在 `libs/`（`pip install --target`） |
| `sapi` | 1 个：`huihui` 慧慧（女） | 不需要 | 2013 年拼接式引擎，机械感明显；`--rate` 按 /10 映射到 -10..10 |
| `winrt` | 3 个：`huihui` / `yaoyao`（女）/ `kangkang`（男） | 不需要 | OneCore 引擎；`--rate` 走 SSML prosody |

**选音色请先跑试听表**，不要凭描述挑：

```
python make_audition.py          # 生成 audition/试听表.html（17 个音色可点播）
python make_audition.py --offline-only   # 只跑离线音色
```

## 实测基线（2026-10，样例 = 3 章公版古文/现代文，619 字）

| 配置 | 产物 | 时长 | 章节 | 时长漂移 | 平均电平 | 生成耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| edge / Xiaoxiao | mp3 1146 KB + m4b 1317 KB | 146.57s | 3/3 标题逐条相等 | +0.01s | -20.2 dB | 12.4s |
| sapi / huihui | mp3 1384 KB + m4b 1409 KB | 177.14s | 3/3 | +0.01s | -25.2 dB | 6.1s |
| winrt / kangkang | mp3 1184 KB + m4b 1352 KB | 151.54s | 3/3 | 0.00s | -23.0 dB | 6.1s |
| winrt / yaoyao `--rate 20` | mp3 1223 KB + m4b 1393 KB | 156.47s | 3/3 | 0.00s | -22.6 dB | 5.9s |

四种配置全部 `VERDICT: PASS`。约 **12 倍实时**（2.4 分钟音频 12 秒生成，含章节封装）。
试听表 17/17 音色合成成功，平均电平 -19.5 ~ -26.9 dB，耗时 60.4s。

## 自校验：为什么不接受"看起来没问题"

每次运行都会把产物**读回来核对**并写进 `report.json`：

* `chapters_ok` / `titles_match`：章节数与**每一条标题逐字相等**（不是数个数）
* `duration_delta_s`：声明总时长 vs 实际时长（正常 <0.1s）
* `has_sound`：`ffmpeg volumedetect` 实测平均电平，> -50 dB 才算有声音
* `chunks_ok` / `chunks_chars_mismatch`：每块的字符数必须与原文一致；输出 <4096 字节判为失败

任一条不过就打印 `VERDICT: FAIL` 并返回退出码 2。**本机没有 ffprobe**，时长与章节是从
`ffmpeg -i` 的 stderr 解析出来的（正则见 `probe_audio()`）。

## 踩过的坑（都带实测证据，别改回去）

1. **`.ps1` 里绝对不能有中文**（最贵的一条）。本机 PS 5.1 按 GBK 读无 BOM 的 UTF-8 源码，
   中文注释的字节会**吞掉后面的换行**，把下一行代码并进注释。
   实测：`tts_offline.ps1` 里 3 行消失，`$esc = ...` 变成注释的一部分 → SSML 变成空文本 →
   合成出 **46 字节的空 WAV**，而**脚本照常跑完、不报任何错**。
   现在 `make_audiobook.py` 调用前会 `assert_ascii_helper()` 机械校验（0 个非 ASCII 字节），
   并把"输出 <4096 字节"判为失败。看到 `helper_non_ascii_bytes > 0` 就是这条又犯了。
2. **OneCore 拒绝"没有属性的 `<prosody>`"**：`<prosody>` 裸标签 → `FormatException`
   （报成 "One or more errors occurred"，真因藏在 `InnerException` 里）。
   rate 为空时必须走纯文本 API。对照实测：`+0%`/`+20%`/`slow`/`fast` 都能出声。
3. **枚举到语音 ≠ 能出声**：`GetInstalledVoices()` 报 3 个语音全部 `enabled=True`，
   合成却可能只写 46 字节。所以现在既检查字节数，也量电平。
4. **SAPI 在受限沙箱下会失败，全权限下正常**：受限时报
   `0x80045040 SPERR_INVALID_REGISTRY_KEY`（连"只用内存流、不碰音频设备"也失败），
   设备路径报 `0x8004503A SPERR_NOT_FOUND`，且 SAPI 看到的音频输出数为 **0**；
   全权限下 SAPI 看到 **4 个**音频输出、合成正常（125,020 字节 WAV）。
   **这不是机器缺语音包**——三个语音和注册表三个视图一直都在。
5. **mp3 也能带章节**：不只 m4b。ID3v2 `CHAP` 帧同样读得回来（实测 3 章，边界精确）。
   封装先用 `-c copy`，章节数不符时自动回退重编码，`notes` 里会记录发生过回退。
6. **`--volume` 语义按引擎不同**：edge 是相对偏移百分比，离线引擎是绝对音量 0-100
   （**0 = 静音**）。默认 `None`，即"不动它"。
7. 长书可续跑：每块按 `sha1(文本|引擎|音色|参数)` 缓存到 `work/chunks/`，
   重跑只补缺失的块；`--force` 忽略缓存。

## 目录

```
audiobook/
  make_audiobook.py     主流水线（分章/分段/合成/封装/自校验）
  tts_offline.ps1       离线引擎 helper（纯 ASCII，务必保持）
  make_audition.py      音色试听表生成器
  libs/                 edge-tts 依赖（pip --target 装在这里，不动全局环境）
  samples/试听样章.md    测试语料（公版古文 + 现代文混合）
  out/<书名>/            产物：<书名>.mp3 / .m4b / chapters/ / work/ / report.json
  audition/             试听表.html + 17 个音色 + 音色合集.mp3
  _probe/               排查过程的探针与对照实验（诊断用，可删）
```

## 已知限制

* 输入只支持 `txt / md / docx`；epub 未做。
* 章节识别靠标题正则（`#`、`第X章`、`Chapter N`、`1.`），识别不到就当单章；可用 `--chapter-regex` 覆盖。
* 封面 `--cover` 已实现但**未实测**（当前样例没跑过，别当成已验证功能）。
* 离线音色只有 4 个中文音色（含重复），想要"某个特定人的音色"需要本地克隆模型
  ——本机无 NVIDIA GPU（AMD 集显 / 13.7 GB 内存），CPU 跑一本书不现实。
* `libs/` 与 `out/`、`audition/`、`_probe/` 都在仓库根的白名单之外，**不纳入 git**。
