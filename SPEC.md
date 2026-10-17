# renoise-kit：剪辑底层 CLI `rn`

> 状态：v0.5.0（2026-10-16）：§3.1 的通用原语全部实现。v0.2.0–v0.3.4 在 Renoise 主仓库 `packages/media-sandbox/kit` 随沙箱模板发布；v0.4.0 起拆为独立仓库与 PyPI 包 `renoise-kit`（命令仍为 `rn`），沙箱模板固定一个版本安装，集成见主仓库 `spec/mcp/03-media-kit.md`。v0.4.0 新增：`rn compare`、`rn assemble --preview`、`rn qa` 的 dead_air / level_jumps / fixed_frame_rate 检查、成片写 `fixed_frame_rate_flag=1`、本地安装（按功能的 extras、人脸模型首次使用时下载并校验、缺 libass 时提前报错）。
>
> v0.5.0：`id` / `meta`、`rn edit`、slot 增量缓存、嵌套 EDL、缺失素材占位（批 1）；`ranges`、变速曲线、`pivot`、`--range`、`mix.cut_fade`（批 2）；`blend`、`mask`、`vf` / `af`、`keys.gain_db`（批 3）；`rn signal`、`rn align`、`rn matte`、`rn text`、`rn export`、qa 的 `clicks` 与 `accept`、`finish --height`（批 4）。numpy 成为核心依赖。

## 1. 为什么

`sandbox_exec` 给了 agent 一台装满工具的 Linux（`TOOLBOX.md`），但每一步都要它现写 ffmpeg / Python：
帧号取帧、接触表、拼接、混音、成片检查。这些步骤每次都写、每次都踩同样的坑（`-ss` 偏帧、select 后
drawtext 帧号归零、AAC 编码后 true peak 抬升、容器时长被音频拉长、拼接时帧数漂移），费轮次也费 token。

对标 Higgsfield Katana：它的稳定性来自烘焙在沙箱镜像里的 `$HF_WORKFLOWS/katana/scripts`。我们做同样的事，
但只做它最常用、最常出错的那部分，形态是**一个 CLI**（不是零散脚本、不是 MCP 工具）：

- agent 用 `--help` 按需查参数，不占每次对话的上下文；一次 `sandbox_exec` 能串多步。
- 媒体处理生态（Pillow、ffmpeg）在 Python 侧，沙箱已经装好。
- 不做成 MCP 工具：Worker 跑不了 ffmpeg，最后仍要进沙箱；每个工具描述都会进每次对话。

**原则：工具保证做对，不限制怎么剪。** kit 只接管机械、易错的部分（帧精确、编码规格、响度、检查）。
EDL 是可选捷径：创意部分（HTML / canvas、Manim、Blender、ffmpeg 特效）由 agent 自己写，渲染成片段或透明叠加层
放进 EDL；完全不用 EDL 也行，自己做的成片过 `rn finish` + `rn qa` 即达到同样的交付标准。样式类参数只是可覆盖的默认值，
按视频类型的剪辑方向写在 skill 里，作为指引而非规定。

**kit 只做三类原语，不替模型做决定：**

- **量**：把媒体变成模型能读的事实：帧、时间、镜头边界、拍点、字词时间、人脸坐标。
- **渲**：把任意代码变成帧精确的画面（含透明通道）。
- **拼与验**：帧精确合成、交付规格、检查。

判断标准：一个新功能如果把模型的选择收窄成一组枚举（具名特效、样式库、自动剪点、评分），它属于 skill 里的指引，
不进 kit。EDL 只增加通用能力（关键帧、时间锚点、图像序列），不增加具名效果；常用效果以可读、可改的代码库提供，
模型可以调用、改写或不用。

## 2. 约定

- 一个入口 `rn <command>`，`rn <command> --help` 就是文档。`rn --version` = 模板版本。
- 小结果（`probe`）直接打印 JSON；其他命令终端只打印几行摘要，细节用 `--json <file>` 落盘。
- 失败返回非 0，最后一行是 `rn: error: <原因>`，可直接转述给用户或据此修参数。
- 时间：秒（数字）。源素材的位置也可以写帧号 `{"frame": 12}`，按源的实际帧率换算。
- 帧精确：取帧、拼接一律按解码后的帧序号或解码时间戳，不用输入端 `-ss`。
  ponytail：从头解码，耗时与源时长成正比；生成片段多为 5–15 s，参考片 ≤ 60 s，可接受。

## 3. 命令

| 命令 | 作用 |
|---|---|
| `rn probe FILE [--exact]` | 解码帧数、有理帧率、尺寸 / SAR / pix_fmt / 色彩 tag、视频与音频各自时长、是否 faststart |
| `rn frame FILE (--index N \| --time T \| --last) -o out.png` | 按帧号 / 时间 / 末帧精确取一帧（尾帧接续、首帧参考） |
| `rn sheet FILE... [--every N \| --fps R \| --indices a,b,c \| --count N]` | 带帧号和时间码标签的接触表，自动压到 `--max-kb`（默认 125，`sandbox_exec` 一次最多看 4 张、共 512 KB） |
| `rn assemble edit.json -o out.mp4 [--dry-run] [--keep]` | 按 EDL 渲染成片：帧精确裁切、变速、适配、转场、叠加层、字幕、混音、响度与峰值 |
| `rn finish FILE -o out.mp4 [--lufs N\|keep] [--copy-video]` | 不用 EDL 的成片收尾：与 `assemble` 同一套编码规格、响度和编码后 true peak |
| `rn qa FILE [--edl edit.json] [--expect-*]` | 成片检查：帧数、帧率、尺寸、编码、色彩、faststart、音视频时长、LUFS / true peak、黑场、冻帧、黑边；v0.4.0 加片中数字静音（< -70 dBFS 且 ≥ 60 ms，片头片尾除外）、相邻 100 ms 电平跳变 ≥ 15 dB、H.264 `fixed_frame_rate_flag`（均为 WARN） |
| `rn assemble ... --preview` | v0.4.0：半尺寸草稿（叠加层像素位置与尺寸同比缩放，主轨 keys 与 ASS 字幕本就按比例），ultrafast / CRF 30。不做区间渲染：锚点、转场、字幕都要平移，见 §3.1 |
| `rn edit EDL ids\|set\|split\|delete\|insert\|move ...` | v0.5.0：按 `id`（或 `track:index`）改 EDL；每步重新排布整条时间线校验，通过才写回。主轨天然波纹，`delete --lift` 留背景色空隙 |
| `rn assemble ... --placeholders / --range A-B / --no-cache` | v0.5.0：缺失素材渲染成卡片（id、`meta.label` / `meta.prompt`、缺的文件，中日文可读）；只渲时间线的一段（整条照常排布，锚点与转场不变，响度按整段混音计算）；slot 与嵌套 EDL 默认按内容缓存在 EDL 旁的 `.rn-cache/`（`RN_CACHE` 可改） |
| `rn signal FILE [-o png]` | v0.5.0：逐帧数值：亮度、帧差、饱和度（0..1），RMS、低 / 中 / 高频段（dB）、onset（32 个对数频带的谱通量，dB），闪帧帧号、爆音时间。纯音读数 ≈ 0，鼓点每拍一个峰 |
| `rn align A B [--max-offset S]` | v0.5.0：B 在 A 时钟上的偏移（onset 包络互相关 + 8 kHz 波形细化，精度约 0.1 ms）；`match` 为该偏移处波形相关（1 = 同一段声音），`distinct` 为与 0.25 s 外次优偏移的差距（重复性音乐偏低） |
| `rn matte FILE -o DIR [--model] [--every N] [--cutout]` | v0.5.0：rembg 逐帧主体 alpha，灰度 PNG 序列（白 = 主体），直接作 EDL `mask`；`--cutout` 输出 RGBA 抠像。extra `matte` |
| `rn text FILE [--every N] [--lang]` | v0.5.0：tesseract OCR，默认每秒读两帧；逐帧行与框，以及同一行在连续采样中出现的时间段。CJK 字间不加空格。系统需装 tesseract（macOS 的 ffmpeg-full 自带；Debian `tesseract-ocr` + `-chi-sim`） |
| `rn export EDL -o x.otio\|.fcpxml\|.xml [--srt]` | v0.5.0：OpenTimelineIO / Final Cut X / FCP7 XML（Premiere、Resolve）。剪切、裁切、变速、转场（剪点两侧各半）、嵌套（Stack）、叠加层与音频的时间位置都带过去；纯色导出为 Gap；位置尺寸、keys、blend、mask、滤镜、增益曲线写在 `rn` metadata 并在结果里列出。extra `export` |
| `rn finish ... --height N` | v0.5.0：按高度缩放交付（宽度保持比例取偶数），如 4K 母版另出 1080p |
| `rn compare A B [--count N \| --seconds ..] [--cuts] [--video sbs.mp4]` | v0.4.0：同一时间点 A 在上 B 在下的接触表，各自按自己的帧率标帧号；`--cuts` 两边跑 `rn cuts` 并按 0.25 s 配对剪点、给出偏移；`--video` 输出左右并排视频（A 的声音） |
| `rn sheet ... --around T,.. [--span N] [--diff]` | v0.3.0：剪点 / 转场 / 拍点前后高密度取帧；`--diff` 显示与前一帧的差（×4），看运动、闪烁和重复帧 |
| `rn cuts FILE [--threshold] [--sheet JPG]` | v0.3.0：按解码帧号的镜头边界（ffmpeg scene score），弱变化列为 suspects；结果即 EDL `{"shot": n}` 的含义 |
| `rn listen FILE [--transcribe]` | v0.3.0：响度（整体、true peak、0.5 s 短时曲线）、静音段、起音点、节拍（含半 / 倍速候选与强度）、Whisper 词级时间；一张共享时间轴的图。转写由 rn 先解码成 16 kHz 样本再交给 faster-whisper（其自带的文件解码依赖未锁版本的 PyAV） |
| `rn faces FILE [--every N] [--sheet JPG]` | v0.3.0：YuNet 逐帧人脸框与关键点（源像素坐标）、并集框；只测量 |
| `rn ref FILE` | v0.3.0（v0.3.1 起加 `cut_sync`、`GUIDE.md`、合并剪点留作 suspect）：`ref/<name>/` 下的 ref.json（probe、镜头、suspects、listen、`cut_sync` 剪点踩声比例与逐剪点偏移帧数、每页帧范围）、cuts.jpg、listen.jpg、全帧覆盖接触表，以及 `GUIDE.md`（读片方法：先写一句话手法、逐帧看全并记覆盖范围、检测器漏判/误判清单、文字/图层/调色/运镜/帧节奏、BPM 倍频校验、含义与镜头角色，产出 `breakdown.md`；参照 Katana analysis.md 改写，去掉其 EXACT 模式、记账与不问用户的规则）。`rn cuts` 因 `min_gap` 合并掉的近邻剪点保留为 suspect（可能是 1–2 帧插帧或碎剪）。OCR 未做 |
| `rn capture page.html -o DIR --frames N --size WxH [--shards K]` | v0.3.0：页面实现 `window.renderFrame(i)`；逐帧调用后截图成 RGBA PNG 序列，多 Chromium 分片。页面运行前注入 `window.RN`、`window.RNFX`，`window.rnReady` 会被等待 |
| `rn fx [--source]` | v0.3.0：效果代码库目录（`rn/fx/fx.js` 的 `RNFX.*` 与 `rn.fx.*` numpy 版）：grain、vignette、leak、flash、letterbox、rgb_split、glitch、strobe、punch、shake |
| `rn fonts [--family] [--script] [--licenses]` | v0.3.0：fontconfig 实时数据（没有 fontconfig 时，如 Windows，用 fontTools 直接读系统字体目录）：实际存在的 CSS 字重、可变字体、覆盖文字、许可证（fontTools 读 name 表） |
| `rn assets search/get` | v0.3.0：CDN 素材库客户端（`RN_ASSETS_URL` 指向 manifest，sha256 校验并缓存）。素材内容尚未采购发布 |
| `rn captions WORDS.json --size WxH -o caps.ass [--script] [--faces]` | v0.3.0：原稿对齐 Whisper 时间、CJK 按字分块与避头尾、两行均衡断行、平台安全区、避脸；输出 .ass（单一可读样式）、.srt、带词级时间的 .json |

### 3.1 路线（2026-10-16）：通用原语，不做场景

v0.2–v0.3 的“感知 → 渲染 → 字幕”已实现（实现说明见各命令的 `--help` 与仓库历史）。之后只加**通用原语**：Higgsfield
Katana（参考片复刻）、ChatCut（口播 / 按文稿剪）、promo-video（Remotion 动态设计）三类流程拆到底，用的是同一组机械能力，
场景差异留在各自的 skill 里（流程、判断、内容库）。判断标准不变：把模型的选择收窄成一组枚举的，不进 kit。

| 批次 | 原语 | 说明 |
|---|---|---|
| 1 · 数据与执行 | 稳定 `id` + 自由 `meta` | 每个 clip / track / 音频项可写 `id`，`meta` 原样保留不解释（素材来源、任务号、审阅状态都放这里） |
| | `rn edit <op> <id>` | split / trim / move / ripple / replace / set，按 id 操作，每步校验，可 diff；比手改 JSON 稳 |
| | 增量渲染缓存 | slot 按内容哈希缓存，改一处只重渲那一段 |
| | 嵌套 | `{"src": "intro.edit.json"}`：EDL 作为另一个 EDL 的素材 |
| | 缺失素材占位 | 显式开启时，不存在的源渲染成带 id / meta 的卡片，先排整片再补素材 |
| 2 · 时间 | 多段区间 `ranges` | 一个源上 `[[in, out], ...]`；删词、删停顿、精选都编译成它 |
| | 变速曲线 | `speed` 进入 `keys` |
| | 锚定内部点 `pivot` | 让源内某一秒（音效峰值、音乐进入点、动作）落在时间线锚点上 |
| | 区间渲染 `--range A-B` | 补齐 `--preview` 的另一半 |
| | 音频转场 | 主轨硬切处可选音频交叉淡化（画面不变） |
| 3 · 合成 | 混合模式 | screen / multiply / add / overlay |
| | 遮罩 | 一层的 alpha 或亮度遮另一层（文字在主体后、局部效果） |
| | 原始滤镜 `vf` / `af` | 每个 clip 直接写 ffmpeg filtergraph（调色、LUT、模糊、降噪），不提供具名效果 |
| | 音频自动化 | 音频项 `gain` 可写 `keys`；`duck_db` 是它的特例 |
| 4 · 测量与交换 | `rn signal` | 逐帧数值曲线：亮度、帧差、色彩、音频频段能量、起音强度、爆音；闪帧、冻帧、节奏、音乐驱动画面都从它派生 |
| | `rn align A B` | 两段音频的时间偏移（互相关 + 置信度）：参考片原曲对齐、多机位同步、音画对齐 |
| | `rn matte` | 主体 / 背景 alpha（rembg，可选 extra）；遮罩的输入 |
| | OCR / 文字区域 | 通用测量 |
| | `rn export` | OTIO / FCPXML + SRT，交给任何 NLE |
| | qa 豁免 | 每条发现带 id，可写进 EDL `accept`；有意为之的不再反复报 |
| | `finish --size` | 缩放交付（如 4K 另出 1080p） |

以上全部在 v0.5.0 实现。与上表的差异：`finish --size` 实现为 `--height`；OCR 用系统 tesseract 而不是 pip 包（rapidocr 强依赖非
headless 的 opencv-python，会和 faces 的 headless 版冲突，Linux 上还要 libGL）；qa 豁免以 check 名加可选时间点表达（`{"check", "at", "note"}`），
不另造 id；遮罩是叠加层的属性（全画幅、默认与叠加层同时），不是独立轨道；导出时纯色片段成为 Gap（FCP X 没有生成器引用）。

不归 kit：生成与计费（Renoise MCP / renoise-cli 的 plan → run）、实时编辑器界面、模板 / 风格 / 曲库等内容。

不做：记账（宿主审批负责）、状态包（沙箱持久）、house 风格渲染引擎、EDL 里的具名特效词汇与字幕样式库、自动剪辑（自动选剪点、
自动卡点）、EXACT 逐帧复刻。

## 4. EDL（`edit.json`）

`assemble`、`qa --edl`、后续 `export` 共用。

```json
{
  "output": { "width": 1080, "height": 1920, "fps": 30 },
  "tracks": [
    { "clips": [
      { "src": "s1.mp4", "in": 0.4, "out": 3.2 },
      { "src": "s2.mp4", "in": { "frame": 6 }, "duration": 2.5, "speed": 0.5,
        "transition": { "type": "fade", "duration": 0.4 } },
      { "image": "card.png", "duration": 1.5, "fit": "contain" },
      { "color": "#000000", "duration": 0.5 }
    ]},
    { "clips": [
      { "image": "title.png", "at": 0.5, "duration": 2, "x": 90, "y": 1400, "w": 900, "fade_in": 0.2, "fade_out": 0.2 }
    ]}
  ],
  "audio": [
    { "src": "music.mp3", "role": "music", "gain_db": -6, "fade_out": 1.5 },
    { "src": "vo.wav", "role": "voice", "at": 0.3 }
  ],
  "subtitles": { "file": "captions.ass" },
  "mix": { "lufs": -14, "true_peak": -1.0, "duck_db": 10 }
}
```

规则：

- `output.fps` 是唯一时间网格。每个片段的上屏帧数 = `round((out - in) / speed × fps)`（或 `round(duration × fps)`），渲染后严格等于这个数。`out` 超出源末尾时截到末尾并 warning；`duration` 要求的长度超出源时用最后一帧补齐并 warning。
- **第 1 条轨道是主轨**：片段首尾相接，不留空。`transition` 写在后一个片段上，表示与前一个片段交叠的转场（xfade 名称，如 `fade`、`dissolve`、`wipeleft`、`slideup`、`circleopen`），主轨总长减去交叠部分。
- **其余轨道是叠加层**：片段用 `at` 定位在时间线上，用 `x / y / w / h` 定位在画面上（只给 `w` 或 `h` 时按比例缩放），可选 `opacity`、`fade_in`、`fade_out`。PNG / ProRes 4444 / VP9·VP8（WebM `alpha_mode=1`，自动改用 libvpx 解码）的透明通道保留。纯色叠加层缺省的宽或高取画面尺寸。叠加层不带声音：要声音就放进 `audio[]` 并用 `at` 对齐。
- `fit`：`cover`（默认，裁切铺满）、`contain`（留边，用 `output.background`）、`stretch`。
- 片段自带的音频默认保留（`"audio": false` 关闭，`"gain_db"` 调整），并跟随转场做交叉淡化。
- `audio[]`：外部音频，`at` / `in` / `out` / `gain_db` / `fade_in` / `fade_out`；`role` 为 `music`、`voice`、`sfx`。设置 `mix.duck_db` 时，`music` 在 `voice` 出现的地方自动压低。
- `subtitles`：`.ass` 或 `.srt`，最后一步烧录；可选 `force_style`。
- 路径相对 EDL 文件所在目录。

v0.3.0 新增：

- **图像序列** `{"frames": "title/%05d.png", "fps": 30}`：主轨或叠加层都可用（`fps` 缺省为输出帧率），编号必须连续，起始号自动识别；
  RGBA 透明通道在叠加层上保留，主轨上不透明。`in` / `out` / `duration` / `speed` 与视频相同。这是所有代码渲染的统一交接格式。
- **源素材内的镜头位置** `in` / `out` 可写 `{"shot": n, "offset": s}`：`rn cuts` 默认参数找到的第 n 个镜头（0 起）；作 `in` 是镜头首帧，作 `out` 是镜头结束。
- **时间线锚点**：叠加层 `at` / `until` 与 `audio[].at` 可写秒数或锚点加 `offset`：`{"clip": i}` / `{"clip_end": i}`（主轨第 i 个片段按排布后的起止，含转场交叠：
  `clip_end` 是该片段最后一帧之后，若下一片段带转场，转场开始于 `{"clip": i + 1}`）、`{"marker": "名字"}`（顶层 `"markers": {"名字": 秒}`）、
  `{"frame": n}`、`{"beat": n, "listen": "music.json"}`、`{"word": n, "listen": "vo.json", "edge": "start"|"end"}`。beat / word 的时间相对被分析的文件，
  音频不是从 0 开始放时用 `offset` 补上。`until` 与 `duration` / `out` 互斥。
- **关键帧** `"keys": {"属性": [[t, 值], [t, 值, "缓动"], ...]}`：t 为相对片段首帧的秒数，首个关键帧之前保持首值、末个之后保持末值，缓动写在终点关键帧上。
  主轨：`scale`、`x`、`y`（画面宽高的比例）、`rotate`（度），以画面中心为原点；用 ffmpeg `perspective` 逐帧变换（亚像素、cubic 插值），
  画面外采样会复制边缘像素，所以主轨 `scale` 保持 ≥ 1。叠加层：`scale`、`rotate`（绕图层中心）、`x`、`y`（像素，图层左上角，覆盖静态 x/y）、
  `opacity`（与静态 opacity、淡入淡出相乘）；移动的图层先放到带透明边距的画布上再变换，避免边缘复制。

v0.5.0 新增：

- **`id` / `meta`**：任何 clip、track、音频项可写 `id`（字母数字与 `_ - .`，全 EDL 唯一）与任意 `meta`（原样保留）。锚点 `{"clip": "id"}` /
  `{"clip_end": "id"}` 可指主轨片段或**之前出现的**叠加层。
- **嵌套**：主轨或叠加层 `{"src": "part.json"}` 把另一个 EDL 当视频素材（先渲染并缓存），`in` / `out` / `speed` 照常；循环引用报错。
- **`ranges`**（主轨）：`[[in, out], ...]`，同一源的多段依次播放，展开为每段一个片段；`id` 归第一段（其后 `<id>.1`、`<id>.2`…），
  `{"clip_end": id}` 指最后一段的结束；按序号的锚点按展开后计数。与 `in` / `out` / `duration` / `keys` 互斥。
- **变速曲线** `keys.speed`：输出时间为横轴，值 0.1–8；源时间为其积分，渲染时用反函数的分段线性（≤ 64 段）映射；`out` 给定时帧数由
  积分求得。去掉片段自带声音（atempo 不能随时间变）并给 warning。
- **`pivot`**（叠加层、音频项）：源内某一秒落在 `at` 上（静帧 / 纯色为片段内秒数）；落到 0 之前的部分被裁掉。不与 `until` 或变速曲线同用。
- **`blend`**（叠加层）：ffmpeg blend 模式（`add` = addition），在 RGB 里计算，进出都显式按 BT.709 limited 转换；`normal` 与普通叠加逐像素一致。
- **`mask`**（叠加层）：任意片段源作全画幅遮罩（拉伸到画面），`mode` luma（白 = 保留）或 alpha，`invert` 可选；时间默认与叠加层相同，
  也可写自己的 `at` / `in` / `duration`；遮罩没有帧的时间里叠加层不被遮。
- **`vf` / `af`**：一条原始 ffmpeg 滤镜链（不允许 `;` `[` `]`，接不到图的其他部分）。主轨在适配尺寸后、帧数定格前；叠加层在缩放后；
  `af` 用于主轨片段自带声音与音频项。
- **`keys.gain_db`**：主轨片段自带声音与音频项的音量曲线（-60..24 dB），t 为该声音在时间线上的第一个样本起算。
- **`mix.cut_fade`**：主轨硬切处，两侧片段自带声音各淡出 / 淡入这么多秒（≤ 0.25；有转场的剪点已有交叉淡化，不再加）。
- **`accept`**（顶层）：`[{"check": "dead_air", "at": 12.5, "note": "..."}]`，`at` 落在某条发现的区间内即豁免该条，无 `at` 豁免整项 WARN；
  FAIL 不能豁免。`rn qa --accept CHECK[@T]` 同义。

## 5. 输出与混音标准（`assemble` 保证，`qa` 检查）

- 视频：H.264 High、yuv420p、CRF 18、bt709、`+faststart`、SAR 1:1、帧数等于时间线帧数；SPS `fixed_frame_rate_flag=1`（x264 `force-cfr`，`finish` 只对恒定帧率源加）：为 0 时部分播放器 / 平台按 VUI tick 计时，30 fps 成片显示成一半时长（promo-video 2026-09-29 事故）。
- 色彩：所有素材在适配尺寸时统一转换到 BT.709（limited range），而不是只打标签。RGB 素材（图片、纯色、PNG 叠加层）按 RGB→709 转；YUV 视频有色彩标记按标记，无标记按播放器惯例（高度 ≥ 720 视为 709，否则 601；JPEG 视为 601）。`assemble` 与 `finish` 共用这一策略。
- 音频：AAC 48 kHz 双声道。各路先 `amix normalize=0` 混合，按 ebur128 测量补增益到 `mix.lufs`（默认 -14），
  再过 `alimiter level=disabled`。**在编码后的 AAC 上**测 true peak，高于 `mix.true_peak`（默认 -1.0 dBTP）就把
  limiter 降 0.5 dB 重做，最多 6 次；只重做音频，视频不重渲。
- 时长：混音阶段按样本数把音频裁到视频时长；封装时不用 `-shortest`（ffmpeg 7 会按略短的 AAC 截掉视频末帧）。
- 中间件：每个主轨片段先渲染成无损（x264 `-qp 0`）slot，唯一的有损步骤是最终编码。

## 6. 分发与版本

- 独立仓库，PyPI 包 `renoise-kit`，入口 `rn`。版本 = `rn/__init__.py` 的 `__version__`（`rn --version` 同值），与沙箱模板版本无关。
- 发布：打 tag `v<version>`，`.github/workflows/release.yml` 校验 tag 与 `__version__` 一致后经 PyPI trusted publishing 上传。
- 本地：macOS / Linux / Windows。ffmpeg 7+（带 libass；macOS 用 `ffmpeg-full`，keg-only，需放到 PATH 前面；Windows 用 gyan.dev 的 full 版）+ `uv tool install "renoise-kit[all]"`；
  `rn capture` 另需 `playwright install chromium`，`rn text` 需系统 tesseract。核心依赖 Pillow、numpy；可选依赖按功能分 extras
  （listen、transcribe、faces、capture、captions、fonts、matte、export），
  缺哪个就在 `rn: error:` 行里点名。人脸模型首次使用时下载到 `~/.cache/rn` 并校验 sha256（沙箱里已预置）。
- 沙箱：模板不带 extras 安装固定版本（依赖已由沙箱自己的 install.sh 装好，faces extra 的 opencv 会和 contrib 版冲突）；
  升级流程见主仓库 `spec/mcp/03-media-kit.md`。

## 7. 测试

`uv run pytest`（dev 依赖组带 numpy、librosa、matplotlib、OpenCV、pysubs2、Playwright、fontTools；缺 Chromium、faster-whisper 或
libass 的用例会跳过）：用 ffmpeg 现场生成测试片，覆盖帧数精确、变速、转场交叠、叠加层、字幕烧录、混音峰值收敛、`finish`、qa 的
PASS / WARN / FAIL、preview、compare、打包元数据；v0.5.0 起加 `rn edit` 各操作、缓存命中、嵌套与循环、占位卡、ranges、变速曲线的帧映射、
pivot、`--range` 的帧与响度、cut_fade、`normal` 混合与普通叠加逐像素一致、screen / multiply 数值、遮罩与反相、增益曲线、signal 的
闪帧 / 爆音 / onset、align 的偏移与 match、qa 豁免、finish --height、OTIO 往返与 FCP X / FCP7 写出。rembg 与 tesseract 不在 dev 组里：
缺时 matte / text 用例跳过（CI 的 Debian 与 macOS 都装了 tesseract）。帧号校验用一段无损「双位数」灰度片：左半亮度编码 `N // 8`、右半编码 `N % 8`，
相差 25 级，不受各平台 yuv→rgb 取整差异影响。CI（`.github/workflows/ci.yml`）在 Debian trixie（ffmpeg 7.1，同沙箱）、macOS
（ffmpeg-full）与 Windows（gyan.dev full 版，保留系统默认编码以暴露漏写的 `encoding='utf-8'`）上都跑。
