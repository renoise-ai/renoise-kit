# rn edit 直接改 MMK 工程（v0.14.0，0.14.1 补了空工程与轨道识别）

> 2026-11-27。对照 mmk 0.3.0（Renoise 沙箱装的版本）。实现在 `rn/project.py`，测试在 `tests/test_project_edit.py`。

## 1. 做成了什么样

- **工程是正本**。`rn edit X <命令>` 的 X 是 MMK 工程文件夹（`.mmkproj`，或 Renoise 剪辑工作区 `~/projects/edit-<pid>`，
  在里面就是 `.`）时，每条命令（`batch` 整批）是一次 `mmk edit` 事务，`actor: "rn"`，命令原文是 `label`。
  每一步都进 MMK 的历史，App 看得到、能撤销。
- **命令和 edit.json 上一样**：同样的命令、字段、秒为单位的时间，同样的摘要、`next:`、`proof:`、一行 `rn: error:`。
  agent 不写 mmk-edit JSON，也不算 tick。
- **App 里的修改不会被覆盖**：rn 每次都从工程读当前状态（`mmk info`），`set` 只写这次改到的字段决定的属性。
- **`rn/edit.json` 只读**：`rn export` 留下的那份不再读也不再写，命令结果里提示它已经跟不上工程了。
- **不用标准特效包**：转场只用 MMK 内置的叠化和经色场；其他 ffmpeg xfade 由 rn 渲染成片段（§4）。
- 本地的 `rn edit edit.json`、`rn assemble edit.json` 不变。

## 2. 约定

- 时间：秒（或帧、镜头、锚点），换算成 tick（每秒 120000）并对齐到整帧。输出帧率必须整除 120000。
- id：rn 的 id 就是 MMK 对象的 id。
- 轨道：主轨是第一条视频轨（rn 建的叫 `track_V1`），主轨声音是主轨片段 link 的那条音频轨（`track_A1`，没有就加），`track_V2…` 叠加层，音频条目按 role 分轨：
  `track_voice`、`track_music`、`track_sfx`（重叠时开 `_2`、`_3`…），没有 role 的在 `track_A2`。`rn export` 出的工程音频条目在
  `track_A2…`，`duck` 没有 music 轨时把它们当音乐。
- 主轨上的增删改都 ripple 主轨和它的声音，其他轨不动：
  - 修剪、删除用 `ripple: track`，后面片段的声音经 link 跟着走；
  - 插入、移动先把其他轨锁住，再用 `ripple: all_unlocked_tracks`，然后解锁（只解自己锁的）。
- rn 在工程里另存 `rn/state.json`：每个片段的 rn 字段（fit、style、keys、淡入淡出等），用来推导 `set` 后的属性；
  锚点（§5）；rn 自己每一步的撤销令牌；渲染进片段的配方（§4）。

## 3. 命令

| 命令 | MMK | 说明 |
| --- | --- | --- |
| `split ID --at T` | `split_clip`（links follow） | 后半段 `<id>-2` |
| `delete ID [--lift]` | 主轨 `ripple_delete`；其余或 `--lift`：`delete` | |
| `insert --clip JSON [--before/--after ID] [--track N\|audio]` | `import_asset`（同内容的素材只登记一次）+ rn 自己的转换（`rn/mmk.py`）+ ripple | 叠加层与音频要有 `at`（秒或锚点） |
| `move ID --before/--after ID` | 锁住其他轨后 `move_clip` | `move ID --track N`：叠加层换轨 |
| `set ID k=v…` | `in`：`trim_clip head`（时间线起点不动）；`out`/`duration`：`trim_clip`/`extend_clip tail`；`speed`：`set_linear_time_warp` 再改尾部，保持 rn 的含义（素材不变、片段变短）；`transition`：§3.1；`src`：删掉重插，位置长度不变；`color`：MMK 场景（实心矩形）；其余字段：rn 的转换器推导，只写对应属性 | `vf`/`af`：§4 |
| `hits SFX --at cuts\|beats\|T…` | 每个点一个音频条目，音效之间重叠就分 lane | 剪点、拍点是锚点，之后改剪辑会跟着走 |
| `silences ID --listen J` | 头尾 `trim_clip`，中间每段停顿 `split_clip` ×2 + `ripple_delete` | 片段变成 `ID`、`ID-2`…，App 里能单独调 |
| `snap --listen J [--to downbeats]` | 每个剪点一次 `roll` | 素材不够、附近没有拍点的剪点照旧列出原因 |
| `init --size WxH [--fps N]` | 空工程：一条主轨和它的声音轨 | 新的 Renoise 剪辑工作区先这样建，再 insert；已有剪辑要 `--force` |
| `init --from-shots S` | `rn edit init` 的结果经 `rn export` 写进工程（`placeholders`，不写 `rn/edit.json`） | 空槽位是占位卡，`set sNN src=…` 换成真素材；经黑 / 白场 → `dip_to_color`；工作区 `assets/` 里原有的素材保留 |
| `batch` | 每步在工程副本（媒体硬链接）上做，全部通过后合成一个事务提交 | 历史里是一步 |
| `show [--around T]` | `mmk render` 出帧拼成接触表 | 打印时间线并给 `proof:`（默认在临时目录，不进工程） |
| `undo [N]` | `mmk agent --tool undo` | 只撤 rn 自己的步骤 |
| `marks --on ID --listen J [--kind beats\|downbeats\|words]` | `add_marker` 加在播放这段声音的片段上 | 标记跟着片段走，锚点写 `{"marker": "beat 8"}` |
| `duck [--voice --music --depth]` | `mmk duck --dry-run` | 在副本上把音乐设成 0 dB 再测，加回各条目自己的增益和淡入淡出，跑两次也只压一次 |
| `subtitles FILE [--size --offset]` | `mmk subtitles --dry-run` | 文字片段，MMK 自己的样式；带动画的字幕用 `rn captions` 渲染后插入 |

`rn assemble X -o out.mp4` 用 `mmk export` 出片（与 App 导出同一引擎，响度 −14 LUFS）；`rn qa out.mp4 --edl X` 按工程检查
（帧数、尺寸、帧率、剪点切断说话等）。

### 3.1 转场

居中在剪点上：剪点前右边片段用入点之前的素材，剪点后左边片段用出点之后的素材，先按两边各一半分，一边不够再挪给另一边。
两边余量加起来不够时报错，说明各差多少，以及要改成多短或剪哪边。`fade`/`dissolve` → `cross_dissolve`，`fadeblack`/`fadewhite`
→ `dip_to_color`，片段自带的声音同时交叉淡化。

## 4. MMK 做不了的：rn 渲染成片段

- **主轨片段的 `vf` / `af`**：rn assemble 把这个片段按工程的尺寸帧率渲染进 `rn/baked/<id>-<配方哈希>.mp4`，工程把它当素材播。
  配方存在 `rn/state.json`，之后 `set ID vf=…` 从原素材重新渲染，`--unset vf` 换回原素材。
- **其他 ffmpeg xfade**（wipe、slide、circleopen…）：剪点两边各让出转场的一半再各加一帧，rn assemble 渲染这一段（声音交叉淡化）成
  `x_<id>`，放在剪点上。`set ID transition=…` 先把两边还原再做新的，`transition=none` 回到硬切。
- App 里这些片段只能整体剪，改不了效果；结果里有一行 `note:` 说明。
- 叠加层或音频上的 `vf` / `af`、`mask`：报错，让 agent 先自己渲染再插入。

## 5. 锚点

叠加层和音频条目的 `at` 写成锚点（片段、片段结尾、标记、拍点、字、帧）时，rn 记下锚点和放的位置，之后每条命令跟着重算：

- 命令执行前，条目不在 rn 上次放的位置，说明有人挪过（App 或 rn 的显式移动），就丢掉锚点，保留现在的位置，并给一行提示。
- 命令执行后重算锚点，需要挪的用第二个事务挪，和命令本身算一个撤销步骤。
- 锚点指向的片段没了：条目留在原处，丢掉锚点并提示。
- `undo` 之后以撤销后的位置为准继续跟。
- 为什么不用 MMK 的 `link_clips`：试过，一个片段只能在一个 link 组里（会把它和自己声音的 link 换掉），而且主轨 ripple 不带动
  其他轨上 link 的片段；也表达不了拍点、字这类锚点。

## 6. 已知限制

- 拆分时片段自带声音的后半段 id 由 MMK 起（一串随机字符）。
- 渲染出的 xfade 片段（`x_<id>`）两边的片段如果被删掉或挪走，再改这个转场会报错；先 `transition=none`。
- `cut_fade`、叠加层的遮罩、其他混合模式还没做。
- 导出设置（响度）没存在工程里：rn 和 App 都用 −14 LUFS。
