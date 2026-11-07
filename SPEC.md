# renoise-kit：剪辑底层 CLI `rn`

> 状态：v0.7.0（2026-11-07）：新增 `rn shots`：镜头边界（硬切、叠化、经黑 / 白场的淡变、闪白切）与逐镜头画面测量（静止、24→30 重复帧节奏、整帧刚体推拉拟合、质感、像素不变比例），只给数字不下结论，按 CPU 上限多进程测量；移植自 [rea-video](https://github.com/JuneYaooo/rea-video)（Apache-2.0，见 `NOTICE` 与 `LICENSES/`，包的许可证表达式改为 `MIT AND Apache-2.0`）；新 extra `shots`（OpenCV）。`rn qa --placeholders`：按 `rn assemble --placeholders` 的方式读 `--edl`，缺失素材是占位卡而不是报错。`rn edit EDL batch`：多步修改一次执行，逐步校验、全部通过才写回一次（任一步失败不写），步骤可写在参数里或从 stdin 每行一步；`rn probe` 的结果在进程内按文件身份（inode、大小、mtime、ctime）缓存，批量修改时每个素材只探测一次。`rn sheet --crop W:H:X:Y`：每格只取源画面的一块（招牌、字幕、人脸、logo），视频与静帧文件夹都可用。`rn compare --shots`：两边跑 `rn shots`，逐镜头配对比较转场类型（连同测到的帧数、擦除方向、淡变颜色）和运镜（复刻成片与原片对照）。`rn shots` 另加：直线擦除（给出边的走向）与叠化 / 淡变 / 擦除的持续帧数；快叠化不再判成硬切；镜头内速度（四段变化量、快慢比、最长定格）；`--beats` 每个剪点与音乐拍点的偏差及与随机剪点的对照；`--faces` 人脸占画面比例；逐帧缩略图写到临时文件再内存映射（16.5 分钟视频的扫描内存峰值从约 500 MB 降到约 50 MB，结果不变）。`rn edit EDL hits`：同一个音效按剪点、拍点或给定秒数一次放成多条 `sfx` 音频项，剪点与拍点写成锚点，之后改剪辑会跟着走。`rn text` 的每个时间段加 `style`：首次读到时的字色、背景色、描边色（没有为 null）和字高，复刻叠字不用猜；同一行 2 秒内在同一位置再读到算同一段，并给出读到次数与平均置信度。`rn doctor`：一条离线命令（约 0.1 s）报告本机能用 rn 做什么，每项 ok / warn / missing，附受影响的命令和一行修复办法，取代各 skill 自带的 doctor.py；核心（Python 与必需模块、ffmpeg、ffprobe、H.264 / AAC 编码器）坏了才返回 1。`rn schema`：edit.json 的 JSON Schema 随包发布（从仓库的 `schema/edit.schema.json` 移到包内 `rn/edit.schema.json`，作为 package data），沙箱等只装了包的环境也能拿到；`rn doctor` 与 `rn schema` 不要求 PATH 上有 ffmpeg。
>
> v0.6.0（2026-10-19）：§3.1 的通用原语全部实现。v0.2.0–v0.3.4 在 Renoise 主仓库 `packages/media-sandbox/kit` 随沙箱模板发布；v0.4.0 起拆为独立仓库与 PyPI 包 `renoise-kit`（命令仍为 `rn`），沙箱模板固定一个版本安装，集成见主仓库 `spec/mcp/03-media-kit.md`。v0.4.0 新增：`rn compare`、`rn assemble --preview`、`rn qa` 的 dead_air / level_jumps / fixed_frame_rate 检查、成片写 `fixed_frame_rate_flag=1`、本地安装（按功能的 extras、人脸模型首次使用时下载并校验、缺 libass 时提前报错）。
>
> v0.6.0（2026-10-19）：edit.json 第 2 版：位置与外观进 `style`，用 CSS 属性名和单位；keys 的 `x` / `y` 改为 `left` / `top`（像素）；
> 第 1 版加载时自动升级，`rn edit FILE upgrade` 改写文件；共享 schema `schema/edit.schema.json` 与一致性用例 `schema/fixtures/`，见 §4.1。
>
> v0.5.1（2026-10-17）：Windows 支持：文本一律 UTF-8、拼接列表与长滤镜图可移植、没有 fontconfig 时直接读系统字体目录、字幕在 Windows 上用微软雅黑；CI 加 Windows。
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
| `rn qa ... --placeholders` | v0.7.0：`--edl` 按 `rn assemble --placeholders` 的方式读，缺失素材不报错（先排整片再补素材时，草稿也能过检查） |
| `rn assemble ... --preview` | v0.4.0：半尺寸草稿（叠加层像素位置与尺寸同比缩放，主轨 keys 与 ASS 字幕本就按比例），ultrafast / CRF 30。不做区间渲染：锚点、转场、字幕都要平移，见 §3.1 |
| `rn edit EDL ids\|set\|split\|delete\|insert\|move ...` | v0.5.0：按 `id`（或 `track:index`）改 EDL；每步重新排布整条时间线校验，通过才写回。主轨天然波纹，`delete --lift` 留背景色空隙 |
| `rn edit EDL batch ["OP ..." ...]` | v0.7.0：多步一次执行：在 EDL 旁的工作副本上逐步执行并校验，最后一步通过后才替换原文件；任一步失败（含无法解析的行）报 `step N (<行>): <原因>; nothing written`，原文件不变。不给参数时从 stdin 每行读一步（跳过空行和 `#` 注释），`insert --clip` 的 JSON 不必再套一层 shell 引号。每行与单独的 `rn edit` 子命令同一解析器，按 shell 规则分词（引号分组）；Windows 上反斜杠是路径分隔符，原样保留、不作转义 |
| `rn edit EDL hits SFX --at cuts\|beats\|T1,T2,.. [--listen L.json] [--offset S] [--pivot S] [--gain DB] [--every N] [--id-prefix P]` | v0.7.0：同一个音效放到多个点上，每个点一条 `audio[]` 音频项（`role: sfx`）。`cuts`：主轨除第一个以外每个片段的起点（带转场的片段是转场开始处，`ranges` 每段都算），写成锚点 `{"clip": "<id>"}`，片段没有 `id` 时写 `{"clip": i}` 并给 warning（序号在前面增删片段后会指到别的片段；先跑 `rn edit EDL ids`）；`beats`：`rn listen --json` 结果的每个拍点，写成 `{"beat": n, "listen": ...}`（拍点时间相对被分析的文件，配乐不从 0 开始时把它的起点加进 `--offset`）；给定秒数原样写成数字。锚点的好处是之后改剪辑音效跟着走。`--offset` 加到每个点上（负数提前，写进锚点的 `offset`）；`--pivot` 让音效内某一秒（如 whoosh 的峰值）落在点上（`pivot` 字段，落到 0 之前的部分照常裁掉）；`--gain` 写 `gain_db`；`--every N` 只取第 1、N+1、2N+1… 个点。id 为前缀加序号（默认 `sfx1`、`sfx2`…），跳过 EDL 里已用的 id。点落在 0 s 之前或主轨结束处及之后时不放，在结果的 `skipped` 和摘要里列出；一个都放不下时报错不写。路径按当前目录给（也可相对 EDL），写入时改为相对 EDL。与其他操作一样整条重排校验后才写回，可在 `batch` 里用。音效可来自 `rn assets get <id> -o assets/`。只按给的点放，不判断哪里该放 |
| `rn edit EDL snap --listen L.json [--to beats\|downbeats] [--tolerance S] [--only ID,..] [--offset S]` | v0.7.0：把主轨剪点挪到最近的拍点（或强拍）上，用滚动剪辑（rolling edit）：剪点前的片段晚 / 早结束，剪点后的片段在自己的素材里晚 / 早开始，挪同样的时长，所以时间线总长和其他剪点都不动。按帧取整，结果里给出每个剪点的位移帧数和取整后与拍点的残差 `off_by`。带转场的剪点按转场中点对拍。`--offset` 是被分析的配乐在时间线上的起点（它的音频项 `at`）；`--only` 只挪进入这些片段的剪点；`--tolerance`（默认 0.2 s）以外的不挪。挪不了的剪点留在原处并给出原因：最近的拍点超出容差、前一个片段的素材不够长（rn 会定格末帧的情况一律算不够）、后一个片段会早于素材开头、`ranges` 或变速曲线片段。一个都挪不了时报错不写。可在 `batch` 里用 |
| `rn music fit MUSIC --duration S [-o bed.wav\|.m4a\|.mp3\|.flac] [--listen L.json] [--start S] [--end-on phrase\|downbeat\|beat] [--fade S] [--loop]` | v0.7.0：配乐裁成正好 S 秒、结尾干净的音乐床：从强拍开始（比跟踪到的拍点早 50 ms，保住起音），最后的淡出从乐句边界开始（从起点数每 4 小节；找不到合适的就退到小节、再退到拍），淡到最后一个样本为零。默认淡出约一小节（1–3 s），`--fade` 指定想要的长度；起点在几个候选强拍里挑，偏向歌曲开头。音乐不够长时在乐句边界循环（80 ms 等功率交叉淡化），保持拍子连续；`--loop` 强制循环。不给 `-o` 且不需要循环时不渲染，只打印 EDL 音频项 `{"src", "role": "music", "in", "out", "fade_out"}`；需要循环时要求 `-o`。结果给出起点、淡出起点（成片时间、原曲时间、小节号、拍号）、每个循环点、一句结论（clean / plain）。拍子不清楚时（`rn listen` 的 `clear` 为 false）退化为从 0 或 `--start` 直接裁加淡出。拍点来自音乐本身（extra `listen`）或 `--listen` |
| `rn listen` 的强拍 | v0.7.0：`beats` 加 `meter`（按 4/4 假设）、`downbeat_phase`（第几个拍是小节第一拍）、`downbeat_strength`（0 = 四个相位看不出区别，越大越确定）、`downbeats`（强拍时间）。相位取低频（< 200 Hz）起音加整体起音最强的那一路。3/4 或中途丢拍的歌会判错相位 |
| `rn capture URL -o DIR [--mobile\|--desktop] [--full] [--sections "A,B"] [--scroll-video OUT.mp4 --duration S] [--hide "SEL,.."] [--size WxH]` | v0.7.0：给 http(s) 地址时拍真实网站（App / SaaS 广告用）：手机（默认，输出 1080×1920 = 360×640 CSS px ×3，带移动 UA 与触屏）或桌面（1920×1080 = 1280×720 ×1.5）。先往下滚一遍让懒加载内容出来再回顶。`--full`（什么都不给时的默认）整页 `full.png`；`--sections` 按标题文字找区块（优先 h1–h6，找最近的 section / article / header / footer），每个一张 `section-<名>.png`，找不到的列出来；`--scroll-video` 从顶滚到底的视频：滚动位置是帧号的函数（首尾各停 12%，中间缓入缓出），逐帧截图后按 `--fps` 编成 H.264，帧精确、与机器快慢无关；`--hide` 先隐藏 cookie 横幅、客服气泡等。页面自己的 JS 动画不受控制。本地 HTML 页面的行为不变（仍需 `--frames`） |
| `rn assemble ... --placeholders / --range A-B / --no-cache` | v0.5.0：缺失素材渲染成卡片（id、`meta.label` / `meta.prompt`、缺的文件，中日文可读）；只渲时间线的一段（整条照常排布，锚点与转场不变，响度按整段混音计算）；slot 与嵌套 EDL 默认按内容缓存在 EDL 旁的 `.rn-cache/`（`RN_CACHE` 可改） |
| `rn signal FILE [-o png]` | v0.5.0：逐帧数值：亮度、帧差、饱和度（0..1），RMS、低 / 中 / 高频段（dB）、onset（32 个对数频带的谱通量，dB），闪帧帧号、爆音时间。纯音读数 ≈ 0，鼓点每拍一个峰 |
| `rn align A B [--max-offset S]` | v0.5.0：B 在 A 时钟上的偏移（onset 包络互相关 + 8 kHz 波形细化，精度约 0.1 ms）；`match` 为该偏移处波形相关（1 = 同一段声音），`distinct` 为与 0.25 s 外次优偏移的差距（重复性音乐偏低） |
| `rn matte FILE -o DIR [--model] [--every N] [--cutout]` | v0.5.0：rembg 逐帧主体 alpha，灰度 PNG 序列（白 = 主体），直接作 EDL `mask`；`--cutout` 输出 RGBA 抠像。extra `matte` |
| `rn text FILE [--every N] [--lang]` | v0.5.0：tesseract OCR，默认每秒读两帧；逐帧行与框，以及同一行在连续采样中出现的时间段。CJK 字间不加空格。v0.7.0 起同一行在 2 秒内、差不多同一位置（框中心上下差不到一行高、左右差不到一个框宽）再次读到也算同一段（OCR 在花哨背景上会时读时漏，实测一行字幕 7 次采样只读到 3 次），每段给出读到的次数 `reads` 和平均置信度 `conf`。系统需装 tesseract（macOS 的 ffmpeg-full 自带；Debian `tesseract-ocr` + `-chi-sim`）。v0.7.0 每个时间段加 `style`，在首次读到它的那一帧上、框外扩 15% 行高的范围里测：`color` 字的颜色、`background` 字后面的颜色、`outline` 描边颜色（围住字的一圈，与字和背景都明显不同；没有描边或只是投影时为 null），都是 `#rrggbb` 中位数；`height_px` 框高（源像素）、`height_share` 框高 / 画面高（4 位小数）。浅字深底、深字浅底都行；抗锯齿和 4:2:0 色度模糊的字边不算描边。只用 numpy，不需要 OpenCV。约 24 px 以下的字经有损压缩后颜色偏差变大 |
| `rn export EDL -o x.otio\|.fcpxml\|.xml [--srt]` | v0.5.0：OpenTimelineIO / Final Cut X / FCP7 XML（Premiere、Resolve）。剪切、裁切、变速、转场（剪点两侧各半）、嵌套（Stack）、叠加层与音频的时间位置都带过去；纯色导出为 Gap；位置尺寸、keys、blend、mask、滤镜、增益曲线写在 `rn` metadata 并在结果里列出。extra `export` |
| `rn finish ... --height N` | v0.5.0：按高度缩放交付（宽度保持比例取偶数），如 4K 母版另出 1080p |
| `rn compare A B [--count N \| --seconds ..] [--cuts] [--shots] [--video sbs.mp4]` | v0.4.0：同一时间点 A 在上 B 在下的接触表，各自按自己的帧率标帧号；`--cuts` 两边跑 `rn cuts` 并按 0.25 s 配对剪点、给出偏移；`--video` 输出左右并排视频（A 的声音）。v0.7.0 `--shots`：两边跑 `rn shots`，同一时钟上把每个 A 镜头和与它重叠最多的 B 镜头配对，给出两边的转场类型、起止、覆盖比例、运镜（缩放 / 秒、按画面宽度计的平移 / 秒、方向、刚性拟合）和相同帧比例；没被配上的 B 镜头单列。只给数字 |
| `rn sheet ... --crop W:H:X:Y` | v0.7.0：每格只取源画面的一块（源像素，ffmpeg crop 的顺序，在缩放到 `--width` 之前裁），放大看招牌、字幕、人脸、logo；旋转过的手机素材按显示方向计；超出画面报错。静帧文件夹同样适用 |
| `rn sheet ... --around T,.. [--span N] [--diff]` | v0.3.0：剪点 / 转场 / 拍点前后高密度取帧；`--diff` 显示与前一帧的差（×4），看运动、闪烁和重复帧 |
| `rn cuts FILE [--threshold] [--sheet JPG]` | v0.3.0：按解码帧号的镜头边界（ffmpeg scene score），弱变化列为 suspects；结果即 EDL `{"shot": n}` 的含义 |
| `rn shots FILE [--jobs N] [--sheet JPG] [--faces] [--beats [LISTEN.json]]` | v0.7.0：镜头边界含叠化、淡变、闪白切、直线擦除（中间帧在一条直边两侧分别是前后两个画面，且这条边扫过画面；给出边的走向 left / right / up / down）；叠化、淡变、擦除给出持续帧数 `transition.frames`（淡变另给淡出 / 近黑或近白段 / 淡入各几帧，亮度低于门槛的帧算作近黑段；形状擦除（圆形、时钟）与推拉滑入不区分：形状擦除记作硬切，滑入可能不记边界）（`rn cuts` 只找硬切，仍是 EDL `{"shot": n}` 的含义；两者镜头数可以不同），每个镜头的画面测量：`still`（逐像素相同的相邻帧比例、首尾差）、`cadence`（重复帧比例与周期，24→30 拉帧每 4–6 帧一次）、`motion`（整帧单一缩放 / 平移能解释多少：`rigid_fit` 0..1，静图推拉高、视差和画面内运动低；缩放 / 秒、平移源像素 / 秒与方向、独立运动点比例）、`texture`（平坦 8×8 块（全部，以及非近黑区域）、覆盖 95% 像素的颜色数、横竖直边、噪声、去掉平滑渐变后的细纹理、缓坡（柔和渐变）占比、饱和度与鲜艳像素占比、暗 / 亮占比、亮部（亮度 > 50）与墨部（亮度 < 205）的平滑度、暗底亮细节与亮底暗细节）、`pixels`（相邻帧完全不变的像素比例、静止区噪声）、`speed`（镜头内按四段给出的相邻帧变化、最快段与最慢段之比（变速、慢放在这里体现）、镜头内最长的完全静止帧段秒数（定格））；`still` 另给相邻帧差的 p90；`--faces` 时加 `faces`（最多 4 个采样帧里人脸占画面 2% 以上的比例和最大人脸面积，YuNet，与 `rn faces` 同一模型）。`--beats`：每个边界与最近拍点的偏差（秒、帧，负数表示剪点在拍点之前），拍点取自文件自己的声音（rn listen 的节拍检测，需 extra `listen`）或另一个文件的 `rn listen --json` 结果（重剪时换的音乐）；全片给出 1 帧、2 帧以内的边界比例，以及随机剪点在同样节拍下的比例作对照。只给数字，不判断画面怎么做出来的。默认用本机或容器 CPU 上限的全部核（亲和性 + cgroup `cpu.max`），工作进程用 spawn 启动（OpenCV 用过线程池后 fork 会卡死）。extra `shots`。移植自 rea-video（Apache-2.0）；淡变改进：淡入 / 淡出斜坡上被误判的硬切并入淡变；快叠化改进：两边画面差异大、某一帧的变化够得上硬切时，只要中间帧是两边的线性混合就判为叠化（硬切没有混合帧），不再判成硬切或两个相邻硬切；一秒以内的过黑 / 白场与片头片尾淡入淡出的斜坡不算叠化 |
| `rn listen FILE [--transcribe]` | v0.3.0：响度（整体、true peak、0.5 s 短时曲线）、静音段、起音点、节拍（含半 / 倍速候选与强度）、Whisper 词级时间；一张共享时间轴的图。转写由 rn 先解码成 16 kHz 样本再交给 faster-whisper（其自带的文件解码依赖未锁版本的 PyAV） |
| `rn faces FILE [--every N] [--sheet JPG]` | v0.3.0：YuNet 逐帧人脸框与关键点（源像素坐标）、并集框；只测量 |
| `rn ref FILE` | v0.3.0（v0.3.1 起加 `cut_sync`、`GUIDE.md`、合并剪点留作 suspect）：`ref/<name>/` 下的 ref.json（probe、镜头、suspects、listen、`cut_sync` 剪点踩声比例与逐剪点偏移帧数、每页帧范围）、cuts.jpg、listen.jpg、全帧覆盖接触表，以及 `GUIDE.md`（读片方法：先写一句话手法、逐帧看全并记覆盖范围、检测器漏判/误判清单、文字/图层/调色/运镜/帧节奏、BPM 倍频校验、含义与镜头角色，产出 `breakdown.md`；参照 Katana analysis.md 改写，去掉其 EXACT 模式、记账与不问用户的规则）。`rn cuts` 因 `min_gap` 合并掉的近邻剪点保留为 suspect（可能是 1–2 帧插帧或碎剪）。OCR 未做 |
| `rn capture page.html -o DIR --frames N --size WxH [--shards K]` | v0.3.0：页面实现 `window.renderFrame(i)`；逐帧调用后截图成 RGBA PNG 序列，多 Chromium 分片。页面运行前注入 `window.RN`、`window.RNFX`，`window.rnReady` 会被等待 |
| `rn fx [--source]` | v0.3.0：效果代码库目录（`rn/fx/fx.js` 的 `RNFX.*` 与 `rn.fx.*` numpy 版）：grain、vignette、leak、flash、letterbox、rgb_split、glitch、strobe、punch、shake |
| `rn fonts [--family] [--script] [--licenses]` | v0.3.0：fontconfig 实时数据（没有 fontconfig 时，如 Windows，用 fontTools 直接读系统字体目录）：实际存在的 CSS 字重、可变字体、覆盖文字、许可证（fontTools 读 name 表） |
| `rn assets search/get` | v0.3.0：CDN 素材库客户端（`RN_ASSETS_URL` 指向 manifest，sha256 校验并缓存）。素材内容尚未采购发布 |
| `rn doctor [--json FILE]` | v0.7.0：本机能用 rn 做什么，离线、约 0.1 s：Python 版本与 rn 版本（含包内数据文件）、ffmpeg / ffprobe 是否可用及版本（低于 7 为 warn：超过 8000 字符的滤镜图要走 ffmpeg 7 的 `-/filter_complex 文件`）、libx264 / aac 编码器、rn 自己的滤镜图用到的每个 ffmpeg 滤镜（`ffmpeg -filters`，含 libass 的 `subtitles`、`xfade`、`sidechaincompress`、`alimiter`、`ebur128`，逐个标出依赖它的命令；EDL 里原始 `vf` / `af` 另用的滤镜不在内）、各 extra 的 Python 模块（只用 `find_spec` 查找、不导入，版本取自发行包元数据）、Playwright 对应版本的 Chromium、tesseract 及已装语言、fontconfig 与字幕字体（`rn captions` 用的 Noto Sans / Noto Sans CJK，Windows 上为微软雅黑等）、YuNet 模型（`RN_YUNET_MODEL`、沙箱预置路径或 `~/.cache/rn`）、Whisper `small` 与 rembg 默认模型是否已在本地缓存、素材库 manifest 地址（`RN_ASSETS_URL`，只报告不请求）。不下载、不联网。每项 ok / warn / missing，附依赖它的命令和一行修复办法（按 macOS / Linux / Windows 给出）；`--json` 每项固定字段 `name group core status version path detail commands fix`。核心（Python 与 numpy、Pillow，ffmpeg、ffprobe，H.264 / AAC 编码器）坏了返回 1，缺 extra、模型、OCR、字体只缩小能用的范围，返回 0 |
| `rn schema [--path]` | v0.7.0：打印包内的 edit.json JSON Schema（`rn/edit.schema.json`，与文件逐字节相同），`--path` 打印文件位置，`--json FILE` 另写一份。只装了包的环境（沙箱）也能拿到契约 |
| `rn captions WORDS.json --size WxH -o caps.ass [--script] [--faces]` | v0.3.0：原稿对齐 Whisper 时间、CJK 按字分块与避头尾、两行均衡断行、平台安全区、避脸；输出 .ass（单一可读样式；字体 Noto Sans CJK，Windows 上用微软雅黑 / Yu Gothic / Malgun Gothic / Segoe UI）、.srt、带词级时间的 .json |

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

`assemble`、`qa --edl`、`export`、`edit` 共用。以下按第 2 版书写；第 1 版的字段名见 §4.1。

```json
{
  "version": 2,
  "output": { "width": 1080, "height": 1920, "fps": 30 },
  "tracks": [
    { "clips": [
      { "src": "s1.mp4", "in": 0.4, "out": 3.2 },
      { "src": "s2.mp4", "in": { "frame": 6 }, "duration": 2.5, "speed": 0.5,
        "transition": { "type": "fade", "duration": 0.4 } },
      { "image": "card.png", "duration": 1.5, "style": { "object-fit": "contain" } },
      { "color": "#000000", "duration": 0.5 }
    ]},
    { "clips": [
      { "image": "title.png", "at": 0.5, "duration": 2, "style": { "left": 90, "top": 1400, "width": 900 },
        "fade_in": 0.2, "fade_out": 0.2 }
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
- **其余轨道是叠加层**：片段用 `at` 定位在时间线上，用 `style` 的 `left / top / width / height` 定位在画面上（只给 `width` 或 `height` 时按比例缩放），可选 `opacity`、`fade_in`、`fade_out`。PNG / ProRes 4444 / VP9·VP8（WebM `alpha_mode=1`，自动改用 libvpx 解码）的透明通道保留。纯色叠加层缺省的宽或高取画面尺寸。叠加层不带声音：要声音就放进 `audio[]` 并用 `at` 对齐。
- `style["object-fit"]`：`cover`（默认，裁切铺满）、`contain`（留边，用 `output.background`）、`fill`（拉伸）。
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
  主轨：`scale`、`left`、`top`（画面移动的像素）、`rotate`（度），以画面中心为原点；用 ffmpeg `perspective` 逐帧变换（亚像素、cubic 插值），
  画面外采样会复制边缘像素，所以主轨 `scale` 保持 ≥ 1。叠加层：`scale`、`rotate`（绕图层中心）、`left`、`top`（像素，图层左上角，覆盖静态值）、
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
- **`style["mix-blend-mode"]`**（叠加层）：CSS 混合模式，映射到 ffmpeg blend（`plus-lighter` = addition），在 RGB 里计算，进出都显式按
  BT.709 limited 转换；`normal` 与普通叠加逐像素一致。ffmpeg 独有的 linearlight / vividlight / pinlight / average / negation / phoenix 也可用，属于 rn 扩展。
- **`mask`**（叠加层）：任意片段源作全画幅遮罩（拉伸到画面），`mode` luma（白 = 保留）或 alpha，`invert` 可选；时间默认与叠加层相同，
  也可写自己的 `at` / `in` / `duration`；遮罩没有帧的时间里叠加层不被遮。
- **`vf` / `af`**：一条原始 ffmpeg 滤镜链（不允许 `;` `[` `]`，接不到图的其他部分）。主轨在适配尺寸后、帧数定格前；叠加层在缩放后；
  `af` 用于主轨片段自带声音与音频项。
- **`keys.gain_db`**：主轨片段自带声音与音频项的音量曲线（-60..24 dB），t 为该声音在时间线上的第一个样本起算。
- **`mix.cut_fade`**：主轨硬切处，两侧片段自带声音各淡出 / 淡入这么多秒（≤ 0.25；有转场的剪点已有交叉淡化，不再加）。
- **`accept`**（顶层）：`[{"check": "dead_air", "at": 12.5, "note": "..."}]`，`at` 落在某条发现的区间内即豁免该条，无 `at` 豁免整项 WARN；
  FAIL 不能豁免。`rn qa --accept CHECK[@T]` 同义。

### 4.1 第 2 版与共享 schema

**为什么**：edit.json 要成为 agent（sandbox 里用 rn）和人（Web 时间线编辑器）共同编辑的同一份剪辑文档，两边各自解析、各自渲染。
第 1 版自造了一套小 CSS（`x / y / w / h`、`fit`、`blend`，主轨与叠加层单位还不一样），模型要先读 `--help` 才会写；第 2 版把位置与外观
改用 CSS 的属性名和单位，模型和 Web 端都能直接沿用已有的认知。剪辑层面的东西（波纹主轨、锚点、`pivot`、keys、`ranges`）CSS 没有，保留 rn 的写法。

**改动**：

| 第 1 版 | 第 2 版 |
|---|---|
| 无 `version` | `"version": 2`（必填） |
| `x` / `y` / `w` / `h`（叠加层，像素） | `style.left` / `top` / `width` / `height`：像素数字、`"90px"`，或画面的百分比 `"50%"` |
| `opacity` | `style.opacity` |
| `fit`: cover / contain / stretch | `style["object-fit"]`: cover / contain / fill |
| `blend`（ffmpeg 名，`add`） | `style["mix-blend-mode"]`（CSS 名，`plus-lighter`；ffmpeg 独有的几种保留为 rn 扩展） |
| keys `x` / `y`（主轨：画面比例；叠加层：像素） | keys `left` / `top`，一律像素 |
| — | `style.scale` / `style.rotate`（`5` 或 `"5deg"`）：静态值，等于整段保持的 key；与同名 key 同时给出报错 |

主轨铺满画面，`style` 只接受 `object-fit`、`left`、`top`、`scale`、`rotate`。

**兼容**：没有 `version`（或为 1）的文件在加载时升级为第 2 版（`edl.upgrade`），渲染结果不变；文件本身不改，`rn edit FILE upgrade` 才改写。
在第 2 版文件里写了第 1 版字段时，报错直接给出新名字（`x is now style.left`）。

**共享 schema**：`rn/edit.schema.json`（JSON Schema 2020-12，v0.7.0 起随包发布，`rn schema` 打印；一致性用例仍在仓库的 `schema/fixtures/`）是给其他宿主（Web 时间线编辑器等）的契约。标了 `"x-rn-only": true` 的是
rn 扩展：`vf` / `af`、`mask`、图像序列 `frames`、嵌套 `.json`、`{"shot"}` 位置、`beat` / `word` 锚点、`subtitles`、`mix.lufs` / `true_peak`、
`output.crf` / `preset`、ffmpeg 独有的混合模式与转场（可移植的转场只有 fade / dissolve / fadeblack / fadewhite）。其余部分是**可移植核心**：
另一个宿主只要渲染这部分，预览就等于 rn 的成片。进入共享文档的 rn 扩展效果，应先由 rn 烘焙成素材。

schema 表达不了的跨字段规则由 rn 检查：锚点只能指向之前出现的 id、id 全局唯一、转场不能长于相邻片段、`ranges` 与 `in` / `out` 互斥等。

**一致性用例**：`schema/fixtures/valid/*.json` 必须同时通过 schema 与 rn；`invalid/*.json` 每个只有一处错，必须同时被两者拒绝
（`tests/test_edit_v2.py`）。其他宿主引入同一批用例，各自断言。用例只用纯色片段，不依赖媒体文件。

**待与 Web 时间线（youmeng PR 1030）对齐**：

1. 轨道顺序：rn 是 `tracks[0]` 主轨在最下、越往后越靠上；v3 TimelineDocument 是数组第一个在最上。
2. 音频：rn 是顶层 `audio[]`（可重叠，带 `role`）；v3 是 absolute 音轨。
3. 轨道标记：v3 有 `muted` / `locked`，rn 还没有；`locked` 是人与 agent 协作的边界，`rn edit` 应拒绝改锁定轨道。
4. 素材引用：rn 用相对路径；Web 端用 material id。需要约定一种引用写法（例如 `material:<id>`），由宿主解析成文件。
5. 存储：时间线作为项目下独立的、带版本的对象，而不是画布快照的一部分。

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
  缺哪个就在 `rn: error:` 行里点名，`rn doctor` 一次列全。人脸模型首次使用时下载到 `~/.cache/rn` 并校验 sha256（沙箱里已预置）。
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
