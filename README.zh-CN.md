# renoise-kit · `rn`

[English](README.md) · **简体中文**

`rn` 把视频剪辑里机械、容易出错的那一半做成了一个命令行工具，包括：帧精确的探测、能直接看的接触表、把声音变成数字、把代码渲染成帧、按剪辑决策表（EDL）帧精确地拼接成片、交付规格收尾、成片检查，以及导出给其他剪辑软件。

`rn` 只负责测量和保证机械部分做对，创意决定都留给你或你的 agent。它最初是为 [Renoise](https://renoise.ai) 媒体沙箱里剪辑生成视频的 AI agent 做的，在你自己的电脑上也能用同一个工具。

- **量**：把媒体变成模型能读的事实，包括帧、镜头、拍点、字词时间、人脸、画面文字和逐帧数值曲线。
- **渲**：把任意代码变成帧精确的画面，支持透明通道。HTML/canvas、numpy、Manim、Blender 渲染的结果都统一交成 PNG 序列。
- **拼与验**：按 EDL 帧精确地拼接成片，再按交付标准检查。
- **不做具名特效，也不自动剪辑**：EDL 只加通用能力（关键帧、锚点、遮罩、混合模式、原始 ffmpeg 滤镜），不提供风格菜单。常用效果以可读、可改的源码形式提供，可以直接用、改着用，或者不用。

## 安装

`rn` 调用系统里的 ffmpeg 和 ffprobe，要求 7 以上的版本，并且带 libass（烧录字幕要用）。

| | macOS | Debian / Ubuntu |
|---|---|---|
| 带 libass 的 ffmpeg | `brew install ffmpeg-full`，再执行 `export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"`（它是 keg-only；Homebrew 普通的 `ffmpeg` 不带 libass） | `apt install ffmpeg fontconfig` |
| `rn text` 用的 OCR（可选） | `ffmpeg-full` 已自带 | `apt install tesseract-ocr tesseract-ocr-chi-sim` |

```sh
uv tool install 'renoise-kit[all]'     # 或者：pipx install 'renoise-kit[all]'
playwright install chromium            # 只有 rn capture 需要
rn --version
```

核心依赖只有 Pillow 和 numpy。较重的依赖按功能拆成了 extras，用到哪个装哪个。某条命令缺少依赖时，报错的最后一行会写明该装哪个 extra。

| Extra | 用于 | 安装 |
|---|---|---|
| `listen` | `rn listen`、`rn ref`、`rn signal -o` | librosa、matplotlib |
| `transcribe` | `rn listen --transcribe` | faster-whisper |
| `faces` | `rn faces` | OpenCV（headless 版）；YuNet 模型在首次使用时下载到 `~/.cache/rn`，并校验 sha256 |
| `capture` | `rn capture` | Playwright |
| `captions` | `rn captions` | pysubs2 |
| `fonts` | `rn fonts --licenses` | fontTools |
| `matte` | `rn matte` | rembg（onnxruntime）；模型在首次使用时下载 |
| `export` | `rn export` | OpenTimelineIO，以及 Final Cut / FCP 7 适配器 |
| `all` | 以上全部 | |

## 剪第一条片子

素材是两段生成镜头、一段配乐、一个嗖声音效和一张结尾卡：

```sh
rn sheet shots/s2.mp4 --fps 4                    # 先看素材：带帧号的接触表，大小刚好能看
rn listen music.wav --json music.json            # 响度、拍点、速度
rn capture title.html -o title --frames 90 --size 1080x300   # 用 HTML 逐帧渲染一个透明标题
```

`title.html` 在被调用时画出第 `i` 帧。帧号就是时间，所以不管按什么顺序渲染、分几片，画面都完全一样：

```html
<canvas id=c></canvas><script>
const x = c.getContext('2d'); c.width = RN.width; c.height = RN.height;
window.renderFrame = i => {
  x.clearRect(0, 0, c.width, c.height);
  x.globalAlpha = Math.min(1, i / 10);
  x.fillStyle = '#fff'; x.font = '700 120px sans-serif'; x.textAlign = 'center';
  x.fillText('RENOISE', c.width / 2, 160);
};</script>
```

再用 `edit.json` 把它们拼起来：

```json
{
  "output": {"width": 1080, "height": 1920, "fps": 30},
  "tracks": [
    {"clips": [
      {"id": "open", "src": "shots/s1.mp4", "in": 0.4, "out": 3.2},
      {"id": "hero", "src": "shots/s2.mp4", "ranges": [[0, 1.5], [2.5, 4]],
       "transition": {"type": "fade", "duration": 0.3}},
      {"id": "end", "image": "card.png", "duration": 1.5, "fit": "contain"}
    ]},
    {"clips": [
      {"id": "title", "frames": "title/%05d.png", "fps": 30, "y": 1500,
       "at": {"clip": "hero", "offset": 0.2}, "until": {"clip_end": "hero"}}
    ]}
  ],
  "audio": [
    {"src": "music.wav", "role": "music", "pivot": 12.0, "at": {"clip": "hero"}, "fade_out": 1},
    {"src": "whoosh.wav", "role": "sfx", "pivot": 0.18, "at": {"clip": "hero"}}
  ],
  "mix": {"lufs": -14, "cut_fade": 0.01}
}
```

每一部分的作用：

- **主轨**：片段首尾相接地播放。`hero` 取同一条素材的两段，从 `open` 叠化进来。
- **标题**：按 id 跟着 `hero` 走，而不是写死秒数。`open` 变长了，标题会跟着移动。
- **配乐**：`pivot` 让配乐第 12 秒的那个点（比如音乐爆发处）正好落在切进 `hero` 的剪点上，配乐开头多出来的部分会被裁掉。
- **嗖声**：它的峰值也落在同一个剪点上。

```sh
rn assemble edit.json --dry-run                  # 只排布时间线、逐帧列出，不渲染
rn assemble edit.json -o draft.mp4 --preview     # 半尺寸草稿，大约快 3 倍
rn assemble edit.json -o cut.mp4                 # 全量渲染；没改动的片段直接复用 .rn-cache
rn qa cut.mp4 --edl edit.json                    # FAIL 时退出码为 1；WARN 表示需要看一眼
```

```text
timeline 1080x1920 30fps  210 frames  7.0s
  open: frames 0..83
  hero: frames 75..119  (xfade 9f)
  hero.1: frames 120..164
  end: frames 165..209
  title: overlay frames 81..164
...
WARN  level_jumps: 6.0s -40->-13 dB, 6.5s -45->-19 dB
qa PASS: 0 failed, 1 warnings, 17 passed  (cut.mp4)
```

WARN 不一定是错，可能是有意为之。这里的电平跳变是配乐本身的重拍。你可以在 EDL 里写 `"accept": [{"check": "level_jumps", "note": "配乐的重拍"}]`，或者用 `rn qa --accept level_jumps` 豁免这一项。FAIL 永远不能豁免。

之后的修改用操作来完成，不用手改 JSON：

```sh
rn edit edit.json split open --at 1.5
rn edit edit.json set title meta.copy="新口号"
rn edit edit.json delete end --lift
```

## 命令

具体参数以 `rn <command> --help` 为准。每条命令在终端只打印几行摘要，`--json FILE` 会写出完整结果。

**量**

| 命令 | 作用 |
|---|---|
| `rn probe FILE` | 解码帧数、有理帧率、尺寸、色彩标记、视频和音频各自的时长、是否 faststart（输出 JSON） |
| `rn frame FILE --index N \| --time T \| --last -o out.png` | 精确取一帧，例如取尾帧来接续镜头 |
| `rn sheet FILE... [--fps R \| --every N \| --indices ... \| --around T,.. --diff]` | 带帧号和时间码的接触表，每张不超过 125 KB |
| `rn cuts FILE [--sheet JPG]` | 按解码帧号给出镜头边界，较弱的变化列为待确认的 suspects |
| `rn listen FILE [--transcribe]` | 响度、静音、起音、速度（附半速 / 倍速候选）、拍点时间、Whisper 字词时间，外加一张图 |
| `rn signal FILE [-o PNG]` | 逐帧的亮度、运动、饱和度、响度、低 / 中 / 高频段、起音强度，以及闪帧和爆音位置 |
| `rn align A B` | 通过声音判断录音 B 在 A 里的位置：偏移量、`match`、`distinct` |
| `rn faces FILE` | 逐帧人脸框和关键点（只测量，不改脸） |
| `rn text FILE` | 画面上的文字行、位置框和出现时间（tesseract） |
| `rn matte FILE -o DIR` | 逐帧主体 alpha，输出灰度 PNG，可直接作为 EDL 的 `mask`；加 `--cutout` 输出 RGBA 抠像 |
| `rn ref FILE` | 参考片拆解：镜头、声音、剪点和拍点的对齐情况、所有帧的接触表，附一份读片指南 |

**渲**

| 命令 | 作用 |
|---|---|
| `rn capture page.html -o DIR --frames N --size WxH` | HTML 页面（`window.renderFrame(i)`）逐帧渲染成 RGBA PNG，可用多个 Chromium 分片并行 |
| `rn fx [--source]` | 效果库（颗粒、暗角、漏光、闪白、故障……），以可读源码形式提供，网页和 numpy 都能用 |
| `rn captions WORDS.json --size WxH -o caps.ass` | 把你的原稿对齐到 Whisper 的时间：中日文按字分块、两行均衡断行、留在安全区内、避开人脸 |
| `rn fonts [--script ja]` | 已安装字体实际有的字重，以及覆盖的文字 |
| `rn assets search\|get` | 有版权保障的音效和叠加素材，下载后校验 sha256 |

**改与拼**

| 命令 | 作用 |
|---|---|
| `rn edit EDL ids\|set\|split\|delete\|insert\|move` | 按 clip id 修改 EDL，每一步都先校验整条时间线，通过了才写入文件 |
| `rn assemble EDL -o OUT [--preview] [--range A-B] [--placeholders]` | 帧精确地渲染 EDL；`--placeholders` 会把还没生成的镜头渲染成带说明的卡片 |

**验与交付**

| 命令 | 作用 |
|---|---|
| `rn qa FILE [--edl EDL] [--accept CHECK[@T]]` | 交付检查。格式：帧数、帧率、尺寸、编码、色彩、faststart、固定帧率。声音：音画时长、响度、true peak、片中无声、电平跳变、爆音。画面：黑场、冻帧、黑边 |
| `rn compare A B [--cuts] [--video sbs.mp4]` | 两个视频按同一时间点对齐到一张图上，并配对比较剪点时间；可另外输出左右并排的视频 |
| `rn finish FILE -o OUT [--height N]` | 把任意视频处理成交付标准；`--height` 另出一个缩放版本 |
| `rn export EDL -o cut.otio\|.fcpxml\|.xml [--srt]` | 导出成 Final Cut、Premiere、达芬奇或任何 OTIO 工具能打开的工程 |

## 一屏看懂 EDL

- **轨道**：`tracks[0]` 是主轨，片段首尾相接地播放。其余轨道都是叠加层，用 `at` 定时间，用 `x / y / w / h` 定画面位置。
- **音频项**：`audio` 里的每一项用 `at` 定位。`role` 可以是 `music`、`voice` 或 `sfx`；设置了 `mix.duck_db` 时，配乐会在人声处自动压低。
- **唯一时间网格**：`output.fps` 是唯一的时钟。一个 N 秒的片段严格等于 `round(N × fps)` 帧。
- **素材来源**：每个片段的来源是以下之一：`src`（视频，或者另一个 `.json` EDL，即嵌套）、`frames`（编号 PNG 序列，保留透明通道）、`image`、`color`。
- **素材内的位置**：`in` 和 `out` 可以写秒数、`{"frame": n}` 或 `{"shot": n}`（`rn cuts` 找到的第 n 个镜头）。`ranges` 可以把同一素材的多段依次播放。
- **在时间线上定位**：`at` 和 `until` 可以写秒数，也可以写锚点加 `offset`：
  - `{"clip": "id"}`、`{"clip_end": "id"}`
  - `{"marker": 名字}`
  - `{"beat": n, "listen": 文件}`、`{"word": n, "listen": 文件}`

  `pivot` 让素材内部的某一点（而不是素材开头）对齐到锚点。
- **关键帧**：写作 `[t, 值, 缓动]`。叠加层可以动 `scale`、`x`、`y`、`rotate`、`opacity`、`speed`；主轨可以动 `scale`、`x`、`y`、`rotate`、`speed`、`gain_db`；音频项可以动 `gain_db`。
- **合成**：`blend` 可选 `screen`、`multiply`、`add` 等混合模式。`mask` 接收一张全画幅遮罩，可按亮度或 alpha 取用，也可以反相。`vf` 和 `af` 可以插入一条原始 ffmpeg 滤镜链。
- **标记与豁免**：任何元素都可以带 `id` 和 `meta`，`meta` 原样保留不做解释。`accept` 用来豁免判断为有意为之的 qa 警告。

完整字段见 `rn assemble --help` 和 [SPEC.md](SPEC.md) §4。

## 约定

- **输出**：终端只打印几行摘要，细节写在 `--json FILE` 里。`rn probe` 直接打印 JSON。
- **报错**：出错时退出码非 0，最后一行是 `rn: error: <该改什么>`，可以原样转述给用户。找不到 ffmpeg 时退出码为 2。`rn qa` 有 FAIL 时退出码为 1。
- **帧精确**：取帧和裁切一律按解码帧号或解码时间戳，不用输入端跳转。每次渲染都会核对帧数是否和预期一致。
- **查看预算**：接触表默认不超过 125 KB，四张正好放进一次 512 KB 的查看。
- **缓存**：渲染好的片段和嵌套 EDL 按内容缓存在 EDL 旁边的 `.rn-cache/` 里。移动或重命名片段时会直接复用。`--no-cache` 全部重新渲染；删掉这个目录即可回收空间。
- **环境变量**：
  - `RN_CACHE`：缓存目录
  - `RN_YUNET_MODEL`：人脸模型文件
  - `RN_ASSETS_URL` / `RN_ASSETS_CACHE`：素材库地址和缓存目录
  - `U2NET_HOME`：rembg 模型目录

## 交付标准

`rn assemble` 和 `rn finish` 输出符合这个标准的文件，`rn qa` 按它来检查：

- **视频**：H.264 High、yuv420p、BT.709 limited range（真正做了色彩转换，不只是打标签）、方形像素、`moov` 在文件开头。SPS 里写入 `fixed_frame_rate_flag=1`；如果是 0，有些播放器会把 30 fps 的视频显示成一半时长。
- **音频**：AAC 48 kHz 立体声，响度 −14 LUFS（可配置）；true peak 在**编码后**的 AAC 上测量，不超过 −1 dBTP。
- **时长**：音频按采样点裁到和视频一样长，容器时长永远不会比画面长。

## 和 agent 一起用

`rn` 就是为模型调用而设计的：

- `--help` 就是文档，需要时才加载，不占对话上下文。
- 一次沙箱调用可以串起多条命令。
- 输出摘要很短，报错会直接说该改什么。

接触表、`listen`、`signal`、`align` 之所以存在，是因为模型看不了视频、也听不到声音。检查结果要靠它们，不能只看格式对不对：`rn qa` 通过，不代表剪得好。

在 Renoise 媒体沙箱里，`rn` 已经预装好，版本由沙箱模板固定。

## 开发

```sh
uv run pytest -q    # Python 3.12（和沙箱一致）；需要带 libass 的 ffmpeg；capture 的测试需要 playwright install chromium
```

CI 在两个环境里跑：Debian trixie + ffmpeg 7.1（和沙箱同一个基础），以及 macOS + ffmpeg-full。测试用的媒体都是现场生成的。

发布步骤：

1. 修改 `rn/__init__.py` 里的 `__version__`。
2. 打 tag 并推送：

   ```sh
   git tag v$(sed -n "s/^__version__ = '\(.*\)'/\1/p" rn/__init__.py) && git push --tags
   ```

之后 `.github/workflows/release.yml` 会通过 trusted publishing 自动发布到 PyPI。

设计说明、完整的 EDL 约定和路线图见 [SPEC.md](SPEC.md)。

## 许可证

MIT
