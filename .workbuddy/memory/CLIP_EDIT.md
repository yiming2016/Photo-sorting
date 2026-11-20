# 剪辑态 / 无损 / 工具箱 / 命名 — 项目记忆（拆分自 MEMORY.md）

MEMORY.md 放通用铁律；这一份专放剪辑形态的细节，需要时再读。

## 剪辑态（v114 起第三形态；v117 多轨自由摆放；**v128 起独立窗口**）
入口=合并形态顶栏「剪辑」。**v128 起剪辑是独立的顶层窗口**（`ui/clip_window.py::ClipWindow`，`parent=None`），主界面原封不动留在后面、两窗口同时可用：
- 页内那条「← 返回分组」按钮和「剪辑」标题**已删**（关窗即回）；`ClipEditor.backRequested` 废弃。
- `window_stack` 只剩 splitter（排序/合并），不再装 `clip_editor`。
- `closeEvent` 是 `event.ignore() + hide()` —— **关窗只是藏起来**，时间线/素材/撤销历史全保留；`shutdown()` 才真关（主窗口 closeEvent 里调）。
- `enter_edit` 只在**第一次**清撤销历史（`_edit_entered`），关窗再开要能接着撤销。
- ⚠️ **QShortcut 的 parent 必须是 `clip_editor`**（不能用主窗口）：`Qt.WindowShortcut` 只对"父控件的顶层窗口"生效，挂主窗口的话在独立剪辑窗口里按空格/S 全没反应；换顶层窗口后要 `_rebuild_edit_shortcuts()`。
- `_sync_mode_ui`：`edit_btn`/`lossless_box`/`solo_box` 不再因 `edit_mode` 隐藏，按钮文案「剪辑」↔「收起剪辑」。
- 剪辑页 = 垂直 splitter[ 上: 素材框(240~420) | 预览 | 属性(240~340) ／ 下: 工具条 + 时间线横跨全宽 ]。
  - **v163 起不留白**：`ul`（三栏）margins 0 / spacing 2，`ll`（下部）margins (0,2,0,2) / spacing 3。三栏靠各自 1px 描边 + 底色区分（用户：页面空间宝贵，能看出是不同元素就够了）。
  - **画面按比例收紧**（`ClipEditor._fit_preview`）：`PreviewView.aspect()` → 预览高度 = 宽度 ÷ 画面比例（超过上部可用高度才夹住，cap 要扣掉进度条那一条）。画面正好铺满，进度条贴着**画面**下沿而不是控件下沿（以前上下各一条黑边，进度条离画面差一截）。`_preview_col` 垂直方向设 `Fixed` + `ul.setAlignment(..., AlignTop)`，否则布局会在画面和进度条之间平摊空隙。
  - 上部撑不满的高度直接还给时间线：`splitter.setSizes([need, total-need])`（slack > 4px 才调）。
  - ⚠️ **`splitter.setSizes()` 不发 `splitterMoved`** → 给 `_upper` 装 eventFilter 收 `QEvent.Resize` 再重算，否则上部被压矮时画面会溢出和进度条叠在一起。
  - ⚠️ `PreviewView` 改成存**源图**自己重缩（`set_source(pm)` / `apply_scale()`），`sizePolicy=(Ignored, Fixed)`：QLabel 会拿 pixmap 反推 sizeHint，不 Ignored 会跟自适应高度打架。
- `core/clips.py`：`Clip(source/kind/track/start/src_in/src_out/hold/speed/muted/keyframes/src_duration)` + `Timeline(tracks=3)`。显式 `start`；多轨**通用不分音视频**；**同轨不叠**（`free_slot` 就近避让）；`magnetic` 开=整轨合拢（默认关）；`snap_on` 默认开（Alt 临时关）。画面归属 `top_clip_at()`=轨号小优先。
- ⚠️ **构造时间线要 append 一个 place 一个**（一次性 `clips += [...]` 会让未摆放的互相挤走）。
- `ui/timeline.py` **全自绘**：`_track_y/_th()` 按轨数动态(34~62px)；`_paint_filmstrip` 铺真帧；`_paint_text_scrims` 左右渐变纱（采样取**片段中间**）。`set_pps` 下限=`fit_pps`；`DEFAULT_PPS=40`。
- `core/filmstrip.py`：`quantize(0.5s)` + 落盘缓存 → `FilmstripWorker` 串行抽帧。⚠️ **`FilmstripWorker(parent=self)`：第一个位置参数是 ffmpeg 路径**。
- **素材框工作流**：进剪辑时文件只进素材框、**时间线保持空**；点格子**右下角 ＋**（`plus_rect`）才上轨（落点=播放头）；点别处=只选中；右键=加入/在文件夹显示/移除。`set_sources` 必须按路径**原地更新**。
- **「添加素材」也只进素材框（v128）**：`_on_media_picked` 以前直接调 `_edit_add_source` → 选进来的文件全被自动塞进轨道（用户明确说不对）。现在只走 `_media_add_source`（记 `_edit_extra` + 刷新素材框），上轨一律等用户点 ＋。
- **右键「x个发送到剪辑」**：在「x个移动到…」正下方，`BoldPartMenuAction` 让"剪辑"加粗；只送素材框、**不动时间线**。菜单构建拆成 `_merge_menu()`/`_media_menu()`。
- ⚠️ **按钮 NoFocus 铁律**：`_kill_button_focus()` 全设 NoFocus（留住焦点后空格=再点一次它）。键盘一律走 QShortcut。⚠️ **按钮可用状态必须挂 `orderChanged`**（只挂 `clipSelected` 时加完片按钮还灰着）。
- 时长来源：`merge_specs` 垫底 → `ClipProbeWorker` 后台补（会把 `spec` 回写进 `merge_specs`）。⚠️ 探测异步会覆盖手填值 → 测试直接构造 `Clip`。
- `QLabel` 占位：**先 setPixmap(QPixmap()) 再 setText**。离屏拖动测试：move 事件 `button` 必须 NoButton。

## 预览 / 播放 / 多选 / 产物存处（v123~v127）
- **裁剪线在哪、预览就必须跟到哪**：⚠️ `_on_playhead_moved` 绝不能"播放中就 return"（旧写法 → 播着动画面留在老位置）。播着时走 `_seek_while_playing()`：先 `p.stop()`+`_retire_thread()` 换掉播放器，立刻抓一帧顶住画面，再用 140ms 单次 QTimer 合并连续拖动，手停才 `_play_segment()` 续播（每动一格重启 ffmpeg 会卡死）。
- **播放器代号 `_player_gen`**：换过代的旧播放器送来的 `frameReady/failed/ended` 一律不认（`self.sender()._gen != self._player_gen` 就 return）。新建播放器处都要 +1。
- 预览抓帧走常驻 `FrameGrabber`（覆盖式任务，天然节流）；`_refresh_preview_at(t)` 是唯一入口；`_edit_pause` 里要 `clear_frame()` 再刷一次。
- **多选**：`TimelineWidget._sel: list`，`selected_clip()` 恒等于第一个；`_set_selection` 唯一入口。Ctrl 点加/撤，拖时 `_drag["group"]` 整组平移。
- ⚠️ **Delete 删谁看焦点**：时间线必须 `Qt.ClickFocus`，素材框分支要"焦点在素材框 **且** 真有选中项"。
- **素材框缩略图自己补**：`set_sources` 记 `_missing` → `thumb_worker` → `_on_thumb_ready` 里 `media.set_thumb()` 原地填（重建整表会丢多选）。
- **生成的新文件默认不落原文件夹**：`media_tools.output_path_for(src, tag, ext)` 唯一入口（`settings.clip_out_mode`=subfolder/same/custom，默认 `原文件夹/照片排序台剪辑件`）。`ExtractAudioWorker(...)` ⚠️ 要写 `parent=self`。
- **分离音频 = 声音搬家**：原视频 `clip.muted = True`，波形只在 `not clip.muted` 时画。
- **右栏片段清单（v140 建；v141 改实时拖动 + 段内进度条）**：`ui/clip_list.py`，自绘卡片（序号/缩略图/文件名/`时长 · 第 N 轨`）。
  - 拖动**自己接管**（`_ClipListView` + `_drag_start/_drag_move/_drag_end`）：拖动中实时挪位并 `orderPreview`（主窗口只改轨道起点 + 重画），松手 `orderChanged` 提交（`_after_edit_changed` 重算无损/记撤销）。⚠️ 拖动中 `_refresh_clip_list()` 用 `is_dragging()` 挡住；`_row_at` 必须按"越过中线"算，按"第一个中线在下面的"会让拖到中线时不动。
  - 点卡片（v159 两套口径）：`clipPicked(clip, 是否强制回起点)`。裁剪线**不在这段里** → 单击就跳起点；**正好在这段里** → 单击只选中（不动裁剪线），双击才回起点。都要 `select_clip` + `_begin_clip_scrub(clip)`。⚠️ `timeline_view.select_clip()` 不发 `clipSelected`，得手动补 `_on_edit_clip_selected(clip)`，否则属性面板/工具按钮不刷新。⚠️ 别连 `itemActivated`（Windows 双击会连着发 clicked + activated，跳两次）。
  - Ctrl+A 在这栏 = 全选轨道片段（`set_multi_highlight` 让卡片一起亮，列表本身仍是单选，多选会和拖动打架）。
- **素材框选中即预览（v124）**：点中素材就显示它（视频抓第 0 秒），点画面/空格/双击即播（不推播放头、不改总长），点空白/动裁剪线/点片段回裁剪线那一秒。
- **声音（v127）**：`ui/audio_out.py` —— ctypes 调 winmm 的 **waveOut** 放 PCM（系统里**没有 Qt6Multimedia.dll**，引 QAudioOutput 必崩）；ffmpeg `-vn -sn -f s16le -ar 48000 -ac 2 -`；环形缓冲 8 槽×8192B，**播完才复用 → 天然实时**。常驻 `SoundPlayer`（覆盖式任务 + `_gen` 顶掉旧段；`silence()` 只 reset；`stop()` 才 close）。⚠️ **`waveOutReset` 后必须轮询到 DONE 再 unprepare/释放缓冲区**，否则驱动往已回收内存里写 → 之后在**完全不相干的代码里** access violation（v127 查了两小时）。
- **预览下方那条进度条（v127；v130 修好"拖不动"；v141 兼管片段；v159 兼管整条轨道；v161 挪出画面 + 加开关；v163 紧贴画面）**：`clip_editor.MediaScrubber`（自绘胶囊+轨道+手柄+`mm:ss / mm:ss`，**H=22**，信号 `seekRequested` 拖动中 / `seekCommitted` 松手）。**同一条按语境换范围**：点素材框里的素材（长度=素材，`_media_preview`）/ 点右栏片段卡片（长度=**这一段**，`main._clip_scrub`）/ 播整条轨道、**停着拖裁剪线**（长度=**所有轨道总长**，`main._tl_scrub`）。`_scrub_scope()` 判 `media→timeline→clip→none`（**素材优先**）。点空白 → `_end_media_preview()` + 回整条轨道；换选别的片段/那段被删 → `_end_clip_scrub()`。
  - ⚠️ **v161 起它是预览列里的普通控件**（`_preview_col` = 预览 + 进度条，`QVBoxLayout`），不再是浮在画面上的浮层。以前位置靠 `preview` 几何手算，拖中间那道上下分隔条时本页不 resize → 进度条停在老地方且压着画面。现在布局自己管，画面怎么变都跟着走，也绝不与画面重叠；`retainSizeWhenHidden` 让收起时预览不被拉高。
  - **「进度条」开关**（`scrub_btn`，在「跟随移动」右边，默认开，存 `settings.save_scrub_on`）：关掉 = 干净画面。`_begin_timeline_scrub`/`_begin_clip_scrub`/`_sync_scrub`/`_preview_media` 都要过 `_scrub_on`；状态照留，开回来立刻恢复。
  - **拖动中实时预览**：`MediaScrubber.mouseMoveEvent` 每 40ms 发一次 `seekRequested`（以前只在按下/松手发 → 拖的时候画面不动）。主窗口 `_on_media_seek_live` 只更新位置 + 抓帧（时间线/片段走 `_seek_while_playing`，素材走 `_stop_player` + `_show_media_frame_at`），**不重启播放器**；松手 `_on_media_seek` 才定案并续播（素材用 `_media_seek_paused` 记住"刚才在播"）。
  - ⚠️ **拖"这一段"的进度条也会改 playhead**：`_on_playhead_moved` 里"停着拖裁剪线 → 换成整条轨道"必须靠 `_clip_seeking` 放行，否则范围被抢成整条（v161 自测抓到）。
  - ⚠️ **拖整体进度条自己会发 `playheadMoved`**：靠 `_tl_seeking` 放行，否则手柄一按就消失。
  - ⚠️ **拖片段条续播不能 `_edit_pause() + _edit_play()`**（会重新 `_begin_timeline_scrub`，范围跳成整条轨道）→ 用 `_seek_while_playing()`。
  - ⚠️ **「跟随移动」两套 seek 都要调 `follow_scroll()`**（v165）：`_seek_timeline_scrub` 早有，
    `_seek_clip_scrub` 漏了 → 开跟随、点右栏卡片后再点轨道上**同一个**片段（`clipSelected` 不重发，
    `_clip_scrub` 还留着）→ 拖的是片段条 → 裁剪线跑出视口不跟随。`follow_scroll` 自己看开关，照调无妨。
  - ⚠️ `set_range/set_pos` 值没变要直接 return：播放时每帧调两次，重画会拖慢（v156 的掉帧口径）。
  - **拖不动的两个真因**：① 只有 26px 高，鼠标一抖就滑出去，Qt 没抓鼠标 → move/release 落到别的控件，拖一半断 → **press 里 `grabMouse()`、release 里 `releaseMouse()`**；② 播放推进的 `_media_ui_timer`(100ms) 直接改 `_pos`，手柄被拽回去 → **`set_pos()` 在 `_drag` 时直接 return**。
  - 按下即发一次 seek（点一下也能定位），松手再发一次定案。
- **暂停不清零（v130）**：`_edit_pause()` 以前把 `_media_pos` 置 0 → 再播永远从头。现在保留位置、画面停在暂停那一秒；`edit_toggle_play` / `mediaPlayRequested` 都传 `_media_pos` 续播；只有"自然播完"才回 0。
- **键位可改（v130 起）**：F1 面板点任意键位 → 独立「录入」窗口（`_KeyCaptureDialog`）；
  v132 起：没有标题/取消，只剩「清除」（清空显示、**不关窗**）和「保存」；滚轮**一律分上下**
  （`Ctrl+Wheel+Up` / `+Down`），没有"不分上下"的开关。旧配置里不带方向的 `Ctrl+Wheel`
  仍然有效（`match_wheel` 里当 loose 兜底，上滚正向 / 下滚走 `WHEEL_PAIRS` 的反向动作）。
- **滚轮键位不走 QShortcut**（它只吃键盘）：`TimelineView.wheel_hook` →
  `MainWindow._edit_wheel_dispatch`，返回 False 就退回时间线自带行为。
- **关键帧快捷键（v130）**：照 LossCut —— `Alt+←`/`Alt+→` 跳上/下一个关键帧、`K` 贴到最近关键帧。关键帧在 `Clip.keyframes`（源时间轴），换算 `t = start + (k - src_in)`。
  - ⚠️ `probe_ex` 不返回关键帧（只有 `probe_keyframes` 单独读）→ 主列表里时长已知的素材上轨后**没有关键帧**。`_start_clip_probe_if_needed()` 为此单独加了一批 `kf_need`（`_kf_tried` 防重探），`_edit_add_source` 里也补一次触发。
- **同轨拖动不许重叠（v130）**：`Timeline.clamp_start(track, start, dur, cur, ignore)` —— 只许在**当前空隙里滑动**，推到邻居身上就停（撞墙，不是弹回）。`free_slot` 是"绕过去"，最近空位常常正好是原位，所以在拖动过程中用它 = 松手弹回。⚠️ `magnetic`（磁吸/整轨合拢）开着时**不夹**：整轨本来就没空隙，靠松手 `compact()` 换顺序。
- ⚠️ **"进度条不显示"多半是时长=0（v128）**：`_preview_media` 原来 `if dur>0` 才 show，而刚「添加素材」进来的文件 `merge_specs` 里没记录 → 永远不显示。修：`_start_clip_probe_if_needed()` 现在**也探素材框里时长未知的素材**（`_probe_tried` 防重复），探测回来在 `_on_clip_probe_one` 里补 `show_scrubber`。

## 无损剪辑（v119 起，用户核心诉求）
用户原话：「我想做的是无损剪辑，如果过程中是有损的（传统剪辑软件那样、很慢的）必须告知"不建议剪辑"」。定死四条：**一做就提醒 / 能导出但必须先警告 / 切点自动贴最近关键帧 / 画面重叠直接判不建议**。
- **判定唯一入口 `core/lossless.py::evaluate(timeline, specs, ffmpeg_ok)`** → `ExportPlan(ok/mode/segments/reasons/total/gap)`。fatal=重叠/图片/倍速/音视频混排/规格不一致/有无音轨混排/没 ffmpeg；warn=空隙/静音/没关键帧/入点偏>2s。**导出与界面都读这一个 plan**。`apply_fix(code)`：compact/demute/reset_speed/drop_image/drop_audio/separate。
- **提示只留一处（v162）**：顶部那条「不建议剪辑 —— …」红条已删（`loss_banner` 不再存在）。**导出按钮左侧的胶囊**就是唯一常驻提示：不能无损时写「× 不建议剪辑 / × …」，原因挂 `loss_chip.toolTip()`，点胶囊打开原因清单（`lossReportRequested`）。⚠️ 别再用 `✓`（U+2713）：雅黑缺字形，胶囊里会显示成空方块，直接用「可以无损」。
- ⚠️ 跨轨重叠修不了是因为 `free_slot` 只看同轨：`separate` 要"按时间往后推再同轨避让"。
- **导出 `merge/export.py`**：无损=逐段 `-c copy` + concat demuxer；转码=一条 filter_complex（黑底 + scale/pad/setsar/fps/setpts + overlay enable=between + adelay/amix）。走 `ExportWorker` + ProgressDialog。
- ⚠️ **"有没有音轨"会连带改 5 个 fatal 字段**（audio_codec/sample_fmt/sample_rate/channels/**nb_streams**）→ 判"有的有声音有的没有"时画面比对要剔除这 5 项。
- **⚠️ ffmpeg 位置参数铁律：所有 `-i` 先写完，再写 `-map`**（否则 "Option map cannot be applied to input url anullsrc"）。**⚠️ 转码路径只能引用确实存在的流**（`[{i}:a]` 对无音轨/静音/图片会崩在 "Stream specifier ':a'"）。
- **⚠️ 验证"有没有重新编码"**：`ffprobe -show_data_hash md5 -show_entries packet=data_hash` 比**包级 payload 哈希**（`-f md5` 连时间戳一起算；按秒取帧也不行；concat 后**每段首包**会变 → 跳过）。素材必须有细节（testsrc2/smptebars）。
- copy 裁剪有 GOP 边界精度（`-t 2` 切出 2.08s），断言容差 ±0.3~0.4s。无损失败要留台阶：错误信息以「无损拼接没能成功」开头 + UI 给「改用重新编码导出（慢 · 画质有损）」。

## 剪辑工具箱（v122；`core/media_tools.py` + `ui/tools_dialog.py`）
- **无损改帧率=变速**：`-itsscale 1/speed -c copy`，包级 md5 逐包一致；帧率与倍速同一件事，界面必须联动。
  工具条那个按钮（v136 起写数字；v165 定名）文字口径：没选片段 = 「帧率/倍速」；选中 = `45/1.3`
  （帧率一位小数去尾 `.0`、倍速永一位）→ `set_fps_label(fps, speed)`。⚠️ 两态文字不等宽，
  要 `setMinimumWidth(sizeHint)` 钉住，否则一选片段整排按钮横移。
- **大幅削减**（Decimate）：`-discard nokey` → annexb 裸流 → `-r fps` 重封装。⚠️ 直接 copy 成 mp4 只减帧不减时长；仅 h264/hevc，输出无音轨。
- 波形 `-ar 2000 -f s16le` + `proc.run_bytes`；检测 `-progress pipe:1` 且 `-f null os.devnull`（别写 `-`，会抢 stdout）。⚠️ blackdetect 的 start/end **在同一行**。修复时长靠重封装（`-fflags +genpts -c copy`），临时文件必须保留扩展名。
- ⚠️ `drawPath` 会用**当前 brush 填充**：描边前必须 `setBrush(Qt.NoBrush)`。⚠️ 重叠判定只看**画面**，音频叠在视频上是正常的。工具一律生成新文件、原文件不动。

## 命名模板（v97~v103）
- `merge/naming.py`：`%` 变量 + `CATEGORIES` 五类三元组；输入框纯中文靠 `to_display/from_display` 双向翻译，**所有 render/folder_name 取 `template_text()`**；dt 缺失时时序变量返回空串，绝不能把 `%y` 原样写进文件名。
- **⚠️ 用户铁律：点输入框弹变量面板 + 手动打字必须同时存在；面板绝不能用独立窗口形态**（Qt.Popup 也失败）。`_VarPanel` = 主窗口内普通子控件（`QWidget(edit.window())` + `WA_StyledBackground` + 子控件 NoFocus），收起靠 app 级 eventFilter；定位=纵向贴输入框正下方、横向贴窗口最右。

## 快捷键可改 / 滚轮键位（v131 起草, v132 定型, v133 拆分+修好录入）
- 表在 `ui/edit_shortcuts.py::ACTIONS`（id/默认键/说明/分组/可改）。存盘 `merge.json` 的 `edit_keys`，**只存与默认不同的项**（`load_bindings` 只认表里还存在的 id → 改过 id 名的旧配置自然失效）。
- **键位串语法**：键盘 `Ctrl+Shift+K`；滚轮 `Ctrl+Wheel+Up` / `Ctrl+Wheel+Down`。`parse_seq / normalize_seq / is_wheel_seq / match_wheel`。**带方向的优先于不分方向的**。
- **v133 起滚轮一个方向一个条目**（8 条）：`wheel_zoom_in/out`、`wheel_pan_left/right`、`wheel_seek_next/prev`、`wheel_frame_next/prev`。方向口径：`Ctrl+上=放大`、`Alt+上=前进`、`Shift+上=向左移`。
- **滚轮不走 QShortcut**（它只吃键盘）：`TimelineView.wheel_hook = MainWindow._edit_wheel_dispatch`，命中就执行并 `return True`；没命中/抛异常返回 False 退回时间线自带行为（Ctrl+滚轮缩放、直接平移），所以把滚轮键位全清也不会"滚轮失灵"。
- 反过来，`wheel_*` 这些 id **也在 `_rebuild_edit_shortcuts` 的 table 里**，用户把它们改成普通键时会正常建 QShortcut。
- **录入弹窗（v133 真修好；v142 只剩一格）**：`_KeyCaptureDialog` 打开时在 **QApplication 上挂 eventFilter** 收 KeyPress/Wheel（`return True` 吞掉，别让剪辑区快捷键跟着响），关窗摘掉；**不再用 `grabKeyboard()`**（窗口还没显示就抓 = 抓空，键盘一个都进不来，滚轮却正常 —— 就是这个 bug 的表现）。点中间那块捕获区 = 重新录入。
  - v142：弹窗里**只有一块捕获区**，录哪一格由打开它的按钮决定（`slot=0/1`），标题写「主键 · 播放 / 暂停」。`result_seq()` 只返回这一格的新键 —— ⚠️ **别拿 `result_slots()` 整条塞回 bindings**（另一格是空串，会把人家清掉，所以主窗口只认 `result_seq()`）。
  - **冲突当场拦（v167）**：`_begin_capture` 把「已占用的键 → 占用者名字」当 `occupied` 传进去（**排除正在改的这一格**，同动作另一个槽要算占用）。录入时红字点名「已和「播放 / 暂停」冲突」，捕获框边框变红，**「保存」点灰 + 回车也拦**；换掉冲突键就恢复。主面板原来那段事后提示保留作双保险。
    ⚠️ 测试里的假录入窗要接 `occupied=None`（v142 已同步）；**离屏跑快捷键弹窗，关窗前必须 `_dirty=False`** —— 「恢复默认」后是有未保存改动的，`closeEvent` 会弹真 QMessageBox，离屏下直接挂死（v157 在批量回归里挂了 900 秒）。
- **面板布局（v142）**：一行两个动作，整张表用**一个** `QGridLayout`（每组各用一个网格 → 列宽各自算 → 组与组之间键位块错开 9px）。配对由 `ROW_PAIRS` 定（左移/右移、进/退一帧、开头/末尾…），漏掉的动作按原顺序两个一行兜底。键位块**统一固定宽度**（`_fit_key_buttons()` 按 `fontMetrics().horizontalAdvance()` 算，别用 `sizeHint()` —— 固定宽度后它永远返回老值）；录进更长的组合键会整列一起放宽。说明写短标签，细节放 `DESC_TIPS` 当悬浮提示（长句子会把两列挤歪、折行）。
- 截图/封面：`capture_frame = C`（照 LosslessCut 的捕获快照）、`set_cover = Shift+C`。截图落**源文件同目录**，`原名_HH-MM-SS-mmm.ext`，重名加 `_2`；格式/质量在工具条（PNG 时质量禁用）。封面 = 抽一帧 JPEG 后 `-c copy -disposition:v:1 attached_pic` 重封装（不重编码），只支持 mp4/m4v/mov。

## 剪辑功能口径（用户已定，别重新问）

- 素材框只列合并页勾选的文件（v140）：`view.selected_photos()`；进剪辑记 `_edit_allowed`，`_refresh_media_panel` 过滤，`_edit_extra` 不受限。一个都没勾就空着。
- 右栏 = 轨道片段清单（`ui/clip_list.py`）：卡片拖动改轨道先后（第 i 位沿用原轨道号，之后首尾相接）。点卡片（v159）：裁剪线不在这段里 → 单击跳起点；正好在这段里 → 单击只选中、双击才跳起点。两者都浮出该段进度条（`_clip_scrub`）。⚠️ 不用 `InternalMove`（松手才给结果）→ 自接管 press/move/release，`orderPreview`（拖动中只改轨道+重画）/`orderChanged`（松手提交）；拖动中 `_refresh_clip_list()` 靠 `is_dragging()` 挡住。
- **Ctrl+A / Del 都看"最后点的是哪一块"（v141/v145）**：`_edit_area` ∈ media/clips/timeline，由 `_AreaTap` eventFilter 更新；**不能看焦点** —— 右栏清单 `NoFocus`，焦点一直留在素材框。v145 的"按 Del 删了素材、片段动都不动"就是这么来的。剪辑态快捷键挂在 `clip_editor` 上（独立窗口，挂主窗口收不到）。
- **左右互斥（v145）**：选中素材 → `_clear_clip_selection()`（撤轨道选中/右栏高亮/段内进度条/属性/工具按钮）；选中片段 → `media.clear_selection()`（内部走 `_quiet`，别被当成点了空白）。
- **撤销快照含 `_media_hidden`（v145）**：移出素材框也算一步编辑；`_restore_edit_snapshot` 必须复原它并 `_refresh_clip_list()`，否则时间线退了、右栏卡片还是旧的。
- 快捷键每动作两槽：`bindings[aid]=[主键,备用键]`，一律 `edit_shortcuts.as_slots()`；两槽都注册 QShortcut，用 `seen` 去重（同键两次 = ambiguous，两个都不响应）。
- 关键帧自动读：`_edit_add_source` → `_start_clip_probe_if_needed` → `ClipProbeWorker`；`_kf_tried` 去重。界面入口只当补救（已读到就置灰显示「已读到 N 个」）。同一素材任一片段读到即算。
- **变速三条路，别混**（v167）：
  - 视频改帧率/倍速（「帧率/倍速」按钮，显示 `45/1.3`）= `-itsscale` 生成新文件，**无损**；帧率与倍速是同一件事。
  - 音频变速（同一按钮，显示 `1.5`）= **只把 `clip.speed` 记在片段上，原文件不动**，导出时 `atempo` 重编（音调不变）；不生成新文件、可反复改、可撤销。
  - 两套都让 `lossless.py` 把非 1 倍速判致命（导出要转码），这是**正确**提示，别去消掉。
- **`export.py::_reencode` 的滤镜图：音频段绝对不能建 `[i:v]` 链**（音频没视频流 → `matches no streams` → 整条导出失败）。
  `vsegs` 只收真有画面的段，overlay 循环按 vsegs 对齐；音频段 `[i:a]` 一律按"有音轨"处理（音频不在 merge_specs 里）。
  纯音频时间线由 `lossless.py` 的 `audio_only` FATAL 逼到转码（输出黑画面 + 声音）。
- 音量未做（全项目无 volume/loudnorm/gain）：可行路线画面 `-c:v copy`、只重编码音轨；口径写「画面无损 · 声音已重做」，入口放片段右键菜单。

## 分离音频口径（v146 定稿）

- 触发：时间线右键片段 →「分离音频」。`ExtractAudioWorker` 走 `-vn -map a:0 -c:a copy`，输出 `原名_音频.m4a`。
- 落地三条：
  1. **区间对齐**：音频片段的 `src_in/src_out` 照抄原视频片段的源区间（以前固定 0 秒整段搬 → 声画错位）；
  2. **时长当场读**：`_ensure_duration(out)`（不是 `_known_duration`，刚生成的文件不在缓存里 → 0 秒、右栏写 0.0s）；
  3. **没轨就加轨**：`target = src_track + 1`，越界就 `add_track()`（以前塞回原轨 → 排到视频后面，时间线凭空长一倍）。
- 原视频 `clip.muted = True`（导出时丢它的音轨，波形也不画）。
- 右栏片段清单的类型标签必须认 `audio → 音频`（以前一律「图片」）。

## 播放出声的口径（v147）

- **声音按"时刻"从**每一条轨**上收，不是只看画面那一轨**：`_sound_jobs_at(t)` 遍历所有轨，
  跳过 image / muted / 没音轨的，各自按 `src_in + (t - clip_start)` 算源起点。
  ⚠️ 分离音频后画面段是 `muted=True`、声音在下面那条音频轨 ⇒ 只认 `top_clip_at` 就是
  "分离完反而没声"（v147 用户报的）。`top_clip_at` 本就跳过 audio，所以"只剩音频"时同样要收。
- `SoundPlayer.request(jobs)`：一路走最朴素的命令（不冒险），两路以上才 `amix=inputs=N:normalize=0`。
  ⚠️ 多输入 `-t` 必须写在 `-i` **前**（写在后是输出选项，只有最后一个输入被截断）。
- 混音前每路都要 `_certain_audio()` 确认有音轨（amix 混进没音轨的输入 → 整条命令报错 → 全哑）。
  探测结果存 `_audio_known`，**别写进 merge_specs**（那是无损判定用的）。
- `_play_sound` 有 `_sound_sig` 去重：空隙处播放头每 0.1s 重走 `_play_segment`，不去重就一秒
  重启十次 ffmpeg；`_stop_audio` 清签名。
- ⚠️ **改函数签名必须 grep 全部调用点**：v147 把 `_play_sound(src, start, end)` 改成 `(jobs)`
  漏了素材预览两处三参数调用 → 抛 TypeError 被 except 吞成"播放失败"，只有 v130 回归抓到。
  现在签名是 `(jobs, start=None, end=None)`，老写法也认。

## 分离音频 ≠ 静音（v148 定稿）

- 两个状态两件事：`muted` = 用户不要这段声音（导出补静音轨）；`detached` = 声音被
  「分离音频」搬到下面那条轨了（一个字节没丢）。**只共用一个"出不出声"的判定**：
  `Clip.silent = muted or detached`。判断"为什么没声"再看具体哪个字段。
- ⚠️ 别再用 `clip.muted = True` 来表示分离过：时间线会写「（静音）」、右栏写「· 静音」、
  无损检查报「N 段被静音了」并卖「取消静音」修复（点了变两份声音）、
  分离出的音频还会落进"音频素材和视频混在一起 → 去掉音频素材"（点了声音没了）。
- 音频片段用 `src_parent` 记来源视频；`Timeline.remove()` 会 `_reattach_audio()`
  自动把原视频的 detached 还原（删了音频片段就该有声，不能"删了个文件整段哑掉"）。
- 菜单（**v150 改**）：`detached` → **只给**「取消分离（声音放回视频）」，不再同时摆
  「分离音频」（再点一次会多出第二份声音）；没分离 → 「分离音频」+ 「静音这一段 / 取消静音」。
  ⚠️ 标签一句话只说一件事：以前那个「删除原音（静音）」是把两个概念缝在一起，用户明确否掉。
  改名「重命名这个片段…」永远在菜单第一条。
- 无损检查：`Segment.muted = muted or detached`（导出不重复出声），但「被静音」那栏
  只统计真静音；分离单独一条 FATAL「声音被分离到独立音轨」+ fix `undetach`。
  `drop_audio` 只删没有 `src_parent` 的外来音频。

## 提示文案口径（v149 定稿）

用户明确要求：**能一眼看懂的提示全部删掉，留下的越短越好**；并且**所有提示一律白底黑字**。

- 配色：`theme.TOOLTIP_BG="#FFFFFF"` / `TOOLTIP_FG="#1A1A1A"`，`QToolTip` QSS 必须**引用变量**
  （别再写死深色）。自绘浮窗（`dialogs._InfoLabel`）引用同一组常量，改一处全跟着变。
- 已取消的提示（别再好心加回来）：预览窗「点画面播放/暂停」（两个状态都不要）、
  声波按钮。帧率按钮那条长提示取消，但 v165 起按钮改叫「帧率/倍速」、文字变成
  `45/1.3`，所以留**一句短的**「帧率/倍速（改帧率就是变速，无损）」（v149 允许的口径）。
- 一行能说清的写法：`截图(主键)`（键位读 `load_bindings()["capture_frame"]` 主槽，
  改键后由 `_on_bindings_changed` → `refresh_shortcut_tips()` 换字）、`截图格式`、
  `同轨自动相接`、`无需重编码，无损拼接，速度快`。
- 「生成的文件存到哪」：三个选项是**勾选式三选一**（`widgets.CheckCheckBox` + `clicked` 互斥），
  只有一个「保存」按钮（`_ModalBase._buttons(..., cancel=False)`）。
  ⚠️ 互斥**不能**用 `toggled`：点已勾的那个会被取消成"一个都没选"。

## 预览清晰度 / 工具条配色（v150 定稿）

- **预览帧按"预览窗真实像素"要，不许写死 640。** `player.out_size(src_w, src_h, target_w)`：
  `main_window._preview_out_size(spec, cap)` = 预览窗宽 × `devicePixelRatioF()`，
  `PREVIEW_MAX_W=1920`（抓静帧）、`PLAY_MAX_W=1280`（播放：15fps 原始帧走管道，太宽会跟手变慢）。
  只缩小不放大（源比窗口小时用源尺寸）。`ClipEditor._scaled_frame` 也按 dpr 缩 + `setDevicePixelRatio`。
- 图片片段：`ClipEditor._large_image()` 用 `QImageReader` 按预览尺寸解码**原图**
  （主列表那张 `thumb` 只有格子大，铺满预览必糊）；读不到再退回 thumb / 时间线抽帧。
- 工具条按钮：`QPushButton#chipToggle` 的 `color` 就是 `TEXT`，**灰字只留给 `:disabled`**。
  用户的原话："如果可以被点击，就不要用灰色显示" —— 灰字 = 让人以为点不了。
- 截图范围不再用下拉：`QPushButton#shotScope`（`padding: 3px 4px`，40×28 刚好两个汉字），
  点一下循环（`SHOT_SCOPES`）。⚠️ **提示只讲三种范围是什么**（v152 用户明确要求），
  不许再写"点一下换下一个"。
- 截图质量右边是 `设为封面`（`coverRequested` → `_edit_set_cover`）。

## 操作反馈 / 右栏卡片 / 导出弹窗（v152 定稿）

- **"成了"要在画面旁边看得见**：`ClipEditor.flash(text, kind)` 在预览窗上方浮一条
  绿字（`#EAF7EF` 底 + `T.OK` 字），2.6 秒自收。截图成功、封面写入成功都发。
  状态栏在最底下，用户眼睛在画面中间，只写状态栏等于没提示。
- **右栏「轨道片段」卡片要能右键**：`_request_menu()` → `menuRequested(clip, gpos)` →
  主窗口只给两条（重命名这个片段… / 删除这个片段）。卡片标题取 `clip.name`（改名要立刻看见），
  **不许再用 `basename(source)`**。
- **导出弹窗底部只有「导出」**（v152 去掉「取消」；右上角 × 就是取消），
  meta 行是「共 N 段，导出后约 m:ss.s，约 X MB」，大小由 `clip_export.estimate_size(plan)`
  按"每段占源时长比例 × 源文件大小"算，转码/图片才退回码率估。

## 预览"没画面/发灰"（v151 定稿）

- **轨道空着时不许只显示灰白空框**：`_refresh_preview_at()` 的空轨道分支要去取
  `clip_editor.media.first_path()`，用 `_show_media_frame_at(0.0, path)` 把素材框
  第一个素材的画面（视频抓首帧 / 图片读原图）显示出来。
  用户点「剪辑」、刚「添加素材」看到的就是这块空框，会描述成"预览是黑白的"。
- `_on_media_picked()` 加完素材要立刻 `_refresh_preview_at(playhead)`，别等用户再点一下。
- 抓帧/播放都是 rgb24 → `QImage.Format_RGB888`，色彩没问题；HDR(PQ/HLG) 合成素材实测也正常。
  素材本身"发灰"是内容（白墙空镜），不是我们丢色。

## 预览画面"变灰/错位"（v153 定稿）

- 只要预览画面"发灰、斜条纹、颜色发糊"，**先怀疑 `QImage` 的行对齐**，不是内容、HDR、色彩管理。
  `QImage(data, w, h, Format_RGB888)` 会把每行补齐到 4 字节；rgb24 是紧密的 →
  宽度 %4≠0 时每行错 2 字节。抓帧和播放统一走 `player.image_rgb(buf, w, h)`（显式 `w*3`）。
- 预览帧宽 = 预览窗逻辑宽 × `devicePixelRatioF()`（`_preview_out_size`），
  所以只有**某些窗口尺寸 / 高缩放比**才会踩到，看着像"偶发"或"只在轨道空着时"。
  调试就把 `QT_SCALE_FACTOR=3.5` 用上，别再用低缩放的离屏结果下结论。

## 右栏卡片缩略图 / 跟随移动（v154 定稿）

- 右栏「轨道片段」卡片的图不许只指望主列表的 `photo.thumb`（那是逐张补的，视频常常还没生成）。
  `_refresh_clip_list()` 三级兜底：主列表 thumb → `timeline_view.cached_frame()` →
  图片 `_large_image()`。抽帧晚到走 `TimelineWidget.frameArrived` →
  `ClipListPanel.set_thumb(source, pm)` **只补图不重建**（重建会把正拖着的卡片抽走）。
- 「跟随移动」= 播放时裁剪线钉住、轨道反着滚：`TimelineWidget.follow/_follow_x`、
  `follow_scroll()`（`scroll = 偏移 + playhead*pps - 锚点`，夹到滚动条范围）。
  **只在播放路径里调用**（`_advance_playhead` / `_on_player_ended`），
  暂停时用户拖滚动条不该被拽回；`_edit_play()` 每次开播 `reset_follow_anchor()`。
- **⚠️ 片段不许画到轨名区（v166）**：`paintEvent` 里给片段那一批套了
  `setClipRect(GUTTER_W+1, …)`，但 `_paint_clip` 内部 4 处 `p.setClipPath(path)`
  **默认是 ReplaceClip**，会把外层那条整个替换掉 → 横向一滚，片段左边界跑到负 x 就
  直接盖住「轨3」这些字。全部改成 `setClipPath(path, Qt.IntersectClip)`。
  同类坑：**setClipRect/setClipPath 的默认 op 是替换，不是求交**。
- **片段右下角两行（v166）**：上=时长（`fmt_dur`），下=`clip_info_text()` =
  `120fps/1.511×`（源帧率 / 倍速）。帧率由 `set_fps_map({源: fps})` 传入
  （主窗口 `_clip_fps_map()` 在 `_refresh_lossless()` 里同步），音频/未知帧率返回 ""。
  倍速写法 `_fmt_speed`：最多三位小数、末尾 0 连小数点一起去（1.0→1）。

## 跟随移动的掉帧 / 菜单与多选口径（v155 定稿）

- **「跟随移动」掉帧不是绘制问题，是抽帧队列被每帧清空**：`_hbar.valueChanged` →
  `_film_drop_stale()` → `worker.clear_pending()`（连 `_seen` 去重表一起清）。
  跟随滚动每帧都在改 value ⇒ 每帧清一次 ⇒ 抽帧线程刚取的活被打断、同批格子反复重抽，
  缩略图永远长不出来。滚动手势里用 `TimelineWidget._scroll_quiet` 包住 `setValue`，
  `_film_drop_stale` 见到 >0 只重绘不清队列。**除此之外别在滚动回调里清队列。**
- 帧的 `pm.scaled(...)` 结果要缓存（`_scaled_cell` / `_cell_cache`，key = 源+量化时间+格子宽高，
  600 条封顶）：以前每帧每格都重做 SmoothTransformation，播放时整条时间线重绘 = 白烧 CPU。
- **右键菜单的作用范围 = 选中集**：右键**不改选中**，所以 `_edit_clip_menu` 要分两件事 ——
  右键那一块（菜单挂谁、重命名谁）与 `targets`（右键的块在多选里就是整组）。
  分离音频 / 取消分离 / 静音 / 分割都按 `targets` 批量做；`edit_split` 切的是
  `selected_clips()` 全部（只切真跨住裁剪线的那几段）。`edit_extract_audio` 收列表 +
  `_audio_queue` 串行排队（同一时刻只能有一条 `_audio_worker`）。
- **菜单文案**：「在裁剪线处分割」（不写播放头）、「设为封面（写入视频文件）」。
  「删除「轨 N」」下面固定跟一条「清空「轨 N」」（`_clear_track`：二次确认 + 可撤销，
  轨还留着；没片段的轨置灰）。片段菜单和轨道菜单**两处都有**。
- `_on_track_menu` 拆出 `_track_menu(track)`：`QMenu.exec` 在 PySide6 里**打不了桩**
  （给类属性赋值对 C++ 侧无效），离屏测试只能调构造菜单的那个方法。
- **片段块右侧写的是"这一段有多长"**，用 `timeline.fmt_dur()` → `12.3 秒` / `5 分 12.4 秒` /
  `1 时 05 分`。不要用 `fmt_time`（`00:12.3` 会被读成"结束在时间轴 12.3 秒处"）。
  窄块（<110px）只留时长不留名字；画名字时省略宽度要给时长让位。
- **F11 全屏必须挂 `QShortcut` 到 `clip_editor`**（WindowShortcut）：剪辑是独立顶层窗口，
  主窗口的 `keyPressEvent` 收不到按键。全屏时 `Esc` 先退全屏（`edit_clear_selection` 开头）。

## 跟随移动的顺滑度 / 播放头推进口径（v156 定稿）

- **播放头由墙上时间驱动，与解码帧率无关**：`MainWindow._smooth_timer`（16ms，PreciseTimer）
  → `_on_smooth_tick` 用 `perf_counter` 差值 `_advance_playhead(dt)`。开播起表、暂停停表。
  画面仍旧 `PLAY_FPS = 15`（ffmpeg `fps=15` 滤镜，性能取舍），但**位置更新是 60Hz** ——
  这决定了轨道视觉上是否顺滑。
- **别在 `_on_player_frame` 里推进播放头**（v156 之前就是这样，15fps → 每 66ms 跳 4px）。
  也别在 `_on_play_tick` 里推进（它只负责"图片/音频段到段尾了没"，33ms 检查一次）。
- `_advance_playhead` 的终点夹到 `min(总长, clip_end(_playing_clip))`：视频段等
  `_on_player_ended` 切段，播放头先停在段尾。
- **播放中拖裁剪线必须停表**（`_seek_while_playing` 里 `_smooth_timer.stop()`），
  停稳后 `_on_seek_settled` 重起 —— 否则用户拖到哪、播放头自己又往前走。
- `clip_editor._refresh_time_label()` 只在文本变化时 setText（60Hz 调用，别白触发布局）。
- 上限就是 60Hz（显示器 vblank）；剩下的 0/1px 交替是整数像素的物理下限。不要为此上
  浮点 `drawPixmap`（重采样会把缩略图糊掉）。

## 跟随移动时裁剪线"小范围晃动"（v160 定稿）

- **根因是量化不对齐，不是播放头走不稳**：滚动条只能停在整数像素，播放头的时间却每帧
  在变 ⇒ 它相对屏幕的位置 = 跟随点 ±0.5px 来回扫，抗锯齿把三角形/竖线画成不同灰度，
  看起来就是"裁剪线在原地抖"。实测峰峰值 0.96px、**每 4 帧有 3 帧在往回走**。
- 修法两件事，缺一不可：
  1. `timeline.snap_px(v) = floor(v + 0.5)`（**不要用 `round`**，银行家舍入在 .5 上差 1px）；
  2. `_follow_x` 取整 + `_playhead_x()` 统一按整数像素画（含 `_playhead_handle_rect`、
     `_zoom_anchor`），`follow_scroll` 的 `v` 也用它。
- 恒等式（别破坏）：`x = snap(px) - snap(px - follow_x)`，`follow_x` 是整数 ⇒ **恒等于
  follow_x**。只改一边就会重新出现 ≤1px 的抖。
- 滚动条**没有余量**时（内容比窗口窄，`hbar.maximum()==0`）播放头自己往前走是正常的，
  别当成 bug 去钉。
- 其它元素（片段块/刻度/缩略图）不用量化：它们的时间不变，滚动值是整数 ⇒ 亚像素小数
  恒定，只是整体整像素平移。

## 快捷键表结构（v157 定稿）

- **一个动作就一条，滚轮录在它的备用键里**。不要再给"滚轮版"另起动作 —— v157 删掉的
  6 条（wheel_zoom_in/out、wheel_seek_next/prev、wheel_frame_next/prev）都是同一件事
  的另一种按法。只有"平移"(`wheel_pan_left/right`) 是纯滚轮动作（没有键盘版）。
- 出厂滚轮键在 `edit_shortcuts.DEFAULT_ALT`（`default_bindings()` 用它）：
  Ctrl+Wheel+Up/Down = 放大/缩小时间线；Alt+Wheel+Up/Down = 右/左移 0.5 秒；
  Ctrl+Alt+Wheel+Up/Down = 进/退一帧。改默认就改这一张表。
- **`match_wheel()` 返回 `(动作, 命中的键位)`**，不要退化成只返回动作 id：
  键位带方向 = 只干这一件事（`_edit_actions[aid]()`），不带方向 = 下滚执行反向动作
  （`_edit_wheel_actions[aid](sign, x)`，靠 `WHEEL_PAIRS`）。搞混就会出现
  "缩小录了 Ctrl+滚轮下，往下滚反而放大"。
- 加新动作时：`ACTIONS` 加一条 +（可选）`DESC_TIPS` 长说明，滚轮版直接塞 `DEFAULT_ALT`。
  `load_bindings()` 会把盘上只有一格的旧配置用出厂值补齐。

## 面板排版 / 「排序」页（v168 定稿）

- **顺序即排版，只有一套**：整张表（快捷键页 + 排序页）都是 `row_pairs(aids)`
  = 顺序两列一排。v142 的 `ROW_PAIRS` 固定配对表已删 —— 两套排法必然"两张皮"
  （排序页显示 A|B，快捷键页却按配对显示成 A 单独一行）。
- **排序页不显示键位**（用户明确要求）：每项 = 名字输入框 + `↑ ↓ ← →` + 「移动到」。
  `←→`(±1) 换隔壁、`↑↓`(±2) 换一行（两列一排 → 隔一位）；到边的按钮直接 `setEnabled(False)`。
- **「移动到」是按钮 + `QMenu`**：列其它分组 + 「新建分组…」（`QInputDialog` 问名字）。
  当前组不列；触发后走 `_set_group(aid, 组显示名)`，新名字 = 新建一组（落在末尾）。
  ⚠️ **菜单动作必须 `QTimer.singleShot(0, …)` 推后执行**：`_set_group` 会 `_rebuild()`
  把菜单的宿主按钮一起删掉，直接在 `triggered` 里干，`menu.exec()` 还挂在栈上就收到
  删除事件 → 崩。
- **旧出厂顺序要迁移**：v168 改了 `ACTIONS` 顺序（play 挪到播放组末尾、fit 挪到视图组
  末尾），老存盘里那份"没动过的旧出厂顺序"读进来会把 0.5s/2s 混一行 ——
  `load_layout()` 认出 `_LEGACY_DEFAULT_ORDER` 就丢掉换新出厂；用户真挪过的顺序照留。
- **v170 补的四条**（用户点名）：
  - 组头也有 `↑ ↓`（`_move_group(gid, ±1)`）：组顺序 = 组首个动作在 order 里的位置，
    所以挪组 = 把两组在 order 里换成两个**连续块**（块内顺序不变）；跨组穿插的旧存盘
    顺带被块化。到顶/到底的箭头灰掉。
  - 「移动到」的菜单条目**只写组名**（按钮已经叫「移动到」了，条目再写一遍是废话）。
  - **没有「完成」按钮**：进排序模式后 `edit_btn` 直接 `setVisible(False)`；
    `closeEvent` 里先判 `_editing` → `_exit_edit()` + `event.ignore()`（点右上角 × =
    回快捷键页，不是关窗），再点一次 × 才走"没保存要不要存"的询问。
  - `edgeModeBtn` 必须显式写 QSS `:checked`（主题色实底白字）：光 `setChecked(True)`
    在 QSS 下一点看不出来，用户分不清选的哪个。`_pair_hint` 的键名列固定 150、
    说明列 `setMinimumWidth(190)`，否则说明列被压成窄柱折四五行。

## 右键菜单 / 片段属性（v170）

- 时间线菜单 `_edit_clip_menu()` 与右栏菜单 `_clip_list_menu()` **都要抽成"返回 QMenu"
  的方法**：`_on_clip_list_menu` 里直接建菜单再 `exec` 的话，离屏测试一调就挂在
  模态循环里（PySide6 的 `QMenu.exec` monkeypatch 拦不住）。
- 两处菜单最下面都是「属性…」→ `_show_clip_props()` → `ClipPropsDialog`（只读：
  类型/文件/轨道/源时长/入点出点/这一段多长/时间线位置/帧率/关键帧/倍速/音量 dB/
  声音状态，底部只有「关闭」）。**跟主界面那个读 EXIF 的「属性」不是一回事**。
- 右栏菜单：重命名 / 音量…（只有 `kind=="audio"` 或 `video and not silent`）/
  删除 / 属性…。音量沿用 `_edit_clip_volume([clip])`。

## 时间线排版 / 音量 / 缩放（v169）

- **片段文字在片段上方**: 轨道行 = 顶部 `CAPTION_H` 字带 + 下面片段本体。
  v170: 字号 8pt→**10pt**、`CAPTION_H` 14→**20**、`TRACK_H_MIN` 34→**42**
  （8pt 太小, 用户看不清; 本体高保持 20 左右不被压薄）。
  ⚠️ `_clip_rect()` 只是本体; **命中判定一律用 `_clip_zone()`**(含字带) ——
  `_clip_at`/`_trim_side`/裁剪气泡都用 zone, 用 rect 会导致"点字带点不到片段"。
  字带右半边是硬信息(`caption_text()`: 视频「时长 帧率/倍速」, 音频「时长 倍速」),
  窄了先丢名字 → 只留时长 → 省略号。**别在片段内部再画字**。
- **拖边两档**: `settings.load_edge_drag_mode()` = "trim"/"speed", F1 面板「鼠标操作」
  第一行两个按钮即时存盘(不走「保存」)。speed 档: 倍速 = 源段长/拖出时长,
  夹 0.05~20×, 邻居限块长, 不吸关键帧; 图片两档都调 hold。
  ⚠️ `_apply_trim` 每次按下才读一次盘(拖动过程中不重复读)。
- **音量 `Clip.volume`**: 1.0=原样。必须进 `snapshot/restore`、`split_at`、
  `_same_edit_state` 的 keys —— 漏了就是"改音量 → 撤销"退成更早一步。
  只在导出时生效(转码链插 `volume=`); **音量 ≠100% 是无损的 FATAL**(音量滤镜
  做不了流复制) → 整条转码, 修复项 `reset_volume`。
  v170: 对话框**没有「取消」**(只有「应用」, 右上角 × 就是不改);
  提示显示**响度换算** `20·log10(倍数)`: 50%→-6.0 dB、200%→+6.0 dB、0%→静音,
  不再写"100% = 不变"那种废话。
- **缩放滑轨左端 = 动态 `min_pps()`**(当前全览档), 右端 `MAX_PPS`, 对数刻度;
  `TimelineWidget.zoomChanged` 在 `set_pps` 和 resize 夹紧时发。用固定 MIN_PPS
  做左端的话"全览"只落在 73% 位置。QSS 在 `theme.py` 的 `QSlider#zoomSlider`。

## 撤销栈 / 快照口径（v158 定稿）

- **`Timeline.snapshot()` 必须带上所有"用户看得见、能改"的字段**：现在有
  source/kind/track/start/src_in/src_out/hold/speed/muted/detached/src_parent/
  src_duration/keyframes/**name**。加新字段时**两处一起改**（snapshot + restore），
  漏一处的表现是"改了 → 撤销 → 那项悄悄回到默认值"。
- **`_same_edit_state()` 的 keys 是另一份名单**，也要跟着加（它决定"这次算不算
  一步"）。⚠️ 但 `src_duration` / `keyframes` 是后台 ffprobe 回填的，**不能**进
  keys —— 一进就变成"每探一次多记一步撤销"。
- **改完状态要 `_edit_commit()`**：`_after_edit_changed()` 里自带一次，但
  "不改变结构"的操作（静音 / 改名 / 只改字段）不会走它，得自己调。
  漏了的表现不是"不能撤销"，而是"撤销退掉的是更早那步"，用户会以为撤销把东西删了。
- **`selected_clips()` 会过滤掉"已不在 timeline.clips 里"的对象**：删除片段只动
  `timeline.clips`，`_sel` 里还留着旧对象；不过滤的话删完再按 S / C 动的还是
  "幽灵片段"。`selected_clip()` 直接取 `selected_clips()[0]`。
- **结构变化后 `_after_edit_changed` 会把 playhead 夹回 `[0, total_duration()]`**：
  不夹的话删掉后面几段时时间读数会出现「00:10.0 / 00:05.0」。
- `leave_edit()` 里若剪辑窗口还全屏，先 `showNormal()` —— `hide()` 不改
  windowState，否则下次点「剪辑」一进来就是全屏。

## 右栏「轨道片段」多选口径（v181 定稿；覆盖 v152「菜单只给两条」）

- 用户说的"右侧轨道片段" = `ui/clip_list.py::ClipListPanel`，**不是**下方时间线。
  面板自己记 `_sel`(有序) + `_hi`(高亮 id 集)，唯一入口 `_apply_selection()`；
  列表本体仍是 SingleSelection；鼠标事件发到 `list.viewport()` 才是真实路径。
- **Ctrl+点**：`mousePressEvent` 里直接 `_toggle_clip()`，并在 **release 时吞掉事件**
  (`_ctrl_press`) —— Qt 会在 release 补发 `itemClicked`，那会走 `_on_clicked`
  把多选冲成单个、还顺手跳裁剪线。⚠️ **只模拟 press 的测试测不出这个 bug**。
- **长按空白 220ms → Marquee 框选**：拖动中只画不通知外部，松手才发 `selChanged`。
- **点一下空白(没拉框、没按 Ctrl) → 取消多选**：`clear_selection()` 把面板 / Qt 选中 /
  轨道三处一起清；`_on_clip_list_sel_changed([])` 必须真的清轨道(原来直接 return)。
  按住 Ctrl 点空白才保留原选区(追加框选用)。
- **右键**：落在卡片上 = 单段菜单；落在空白处但已有选区 = 发
  `menuRequested(None, gpos)` 弹整组菜单(框选完右键常落在空白，原来什么都不弹)。
  主窗口 `_on_clip_list_menu` 问 `clip_list.selected_clips()` 拿整组；≥2 段且
  源文件 ≥2 个时「对比…」才可点(和时间线右键同口径)，另给「删除选中的 N 个片段」
  (走 `_delete_clip_group`，别用 `edit_delete`，那个按 `_edit_area` 分流会删错)
  和「取消选择」。
- ⚠️ `menuRequested` 信号保持 **(clip, gpos) 两个参数**(旧接线 / 回归脚本兼容)，
  整组选区不塞进信号；`_on_clip_list_menu(..., build_only=True)` 只造菜单不 exec，
  给离屏验证用(真 `QMenu.exec` 在离屏会挂死)。
- ⚠️ 轨道选中回灌 `set_multi_highlight(sel)` 时**单选也要传**，别再
  `if len(sel)>1 else None` —— 否则轨道那边刷一次选中就把右栏刚 Ctrl 多选出来的
  选区清成空。`_echo` 只挡得住"自己 emit 出去又回灌"那一圈。

## 右键菜单分组（v182 定稿，用户明确要求）

- 三处右键菜单（素材框 `_media_menu` / 时间线片段 `_edit_clip_menu` /
  右栏卡片 `_clip_list_menu`）的**最后一组只放「对比」+「属性」两项**：
  两者紧挨着、中间不插横线，这条横线下面也不许再有任何项。
- 「删除」「取消选择」「在文件夹里显示」等操作一律放横线上方。
  v181 曾把「取消选择」夹在对比和属性中间，v182 挪到删除那一组。
- 空白处右键（`clip=None`）也补了「属性…」，用选区里的第一段当代表。
- ⚠️ 只调**分隔线位置**，别顺手增删菜单项：时间线菜单被十几个旧回归脚本按文本列表
  核对，加一个置灰项就会连锁失败。（v183 补记：确实新增了 7 个操作项，但全部放在
  最后一组横线上方、且不改已有项文案，完整回归没有连锁失败。）

## 快速剪辑四件套（v183）

- **剪辑方案** `.clipproj`：直接复用 `Timeline.snapshot()/restore()`，别另写一份字段表。
  载入要二次确认（会替换当前时间线）、要 `_edit_commit()` 记一步；`_project_sources`
  保证方案素材不会被「每次进剪辑重算 `_edit_allowed`」挤掉。
- **复制/重复**：`Clip` 不可哈希且 `eq=True`，判身份一律用 `is`；`keyframes` 必须
  `list()` 另存；新片段 append 后逐个 `tl.place()` 避让，别自己算坐标。
- **批量倍速只管音频**：视频变速是「改帧率生成新文件」的慢路径，两条路别混
  （和原先「音频只记 speed、导出用 atempo」的口径一致）。
- **整体重排**复用 `_apply_clip_order(new, old)`：按位置沿用原轨道号 + 同轨首尾相接，
  和右栏拖拽改顺序是同一套口径，别另写一套摆位。
- 工具条第二行三个新按钮（存剪辑 / 打开剪辑 / 重排）过 1000px 窄窗实测；
  「重排」是四选一 QMenu，其余两个一键直达。

## 拖边倍速 / 片段剪贴板（v184）

- ⚠️ **拖边松手必须走 `_after_edit_changed()`**：`_on_clip_trimmed()` 以前只刷时间
  读数和无损判定，结果「改变倍率」档明明改了 `clip.speed`，工具条按钮、右栏清单、
  预览全不动，用户认定"设置没生效"。`_after_edit_changed()` 内已含 `_edit_commit()`，
  别再单独调一次（会记两步撤销，Ctrl+Z 退不干净）。
- 预览变速：`_play_segment` 的起点要 `* clip.speed`；`FramePlayer.play` 多一个
  `speed` 参数，只缩放**出帧间隔** `step=1/(PLAY_FPS*sp)`，不动 ffmpeg 解码帧率，
  播放头也不用乘 speed（时间线时长已经是 源时长/speed）。
- **Ctrl+C / Ctrl+V 不进 F1 键位表、不写配置**。注册前把 `seen` 的键规范化
  （`QKeySequence(k).toString().lower()`）比对，用户已占用就跳过注册 —— 同键两条
  QShortcut 会 ambiguous，连用户原有那条一起哑掉。剪贴板存描述 dict（撤销会重建
  Clip 对象，存引用会指到旧对象）；粘贴落点是 `tl.playhead`。

## 变速统一成时间线记号（v185）

- 用户口径：**「导出前全部只在时间线上操作，最后导出才落地」**，且「按钮修改的是
  选中的时间线片段」。此前按钮会 `MT.change_fps` 另存新素材，再 `_after_change_fps`
  把时间线上**所有**引用该素材的片段一起换掉 —— 两条都废了。`_after_change_fps`
  仅为兼容旧回归脚本保留，不再被调用。
- `_tool_change_fps` 现在：读 `merge_specs` 的源帧率（不跑 ffprobe）→ `FpsDialog`
  填目标帧率 → `speed = new_fps / fps` → 只写选中片段 → `tl.place()` 避让 +
  磁吸时 `compact()` → `_after_edit_changed()`（内含一次 `_edit_commit`）。
- `FpsDialog` 新增 `cur_speed`：初始显示**等效帧率**（再打开时不会显示成 1×），
  「没改动就禁用确定」要跟 `cur_speed` 比而不是跟 1.0 比；按钮文案「确定」。
- `set_fps_label()` 前面那个数字改成 `fps * speed`（等效帧率），格式仍是"50/2.0"。
  v165/v167 的旧断言因此从 45/1.3 改成 58.5/1.3。
- ⚠️ `lossless.py` 的 speed 从 FATAL 降 **WARN**：以前一段变速就把整条判成
  reencode（整条画面重压），现在 `plan.mode` 仍是 lossless。`reset_speed` 修复项保留。
- `_fast_cmds()` 两步都是流复制：① 用 `_cut_cmd` 切出 `[src_in, +duration×speed)`
  （**输入 -t 是源时间轴，别用时间线时长**）；② `-itsscale 1/speed` 压缩整段
  时间轴 → 时长正好是时间线时长、等效帧率 = 源帧率×speed。⚠️ ②会连音频时间戳
  一起缩放（音调变高），和旧按钮一致。
- 变速段等效帧率与其它段不同，concat 偶尔拼不上 → `_lossless` 失败时只对变速段
  调 `_fast_reencode_cmd()`（显式 `fps=` / `-r` / scale/pad 对齐规格）重生成再拼，
  其它段仍是 copy 原件。真机 4s 正常段 + 4s/2× 段实测**一次拼成、没退回重编码**，
  输出 6.07s 可完整解码（ffmpeg 会报几条 non-monotonic dts 警告，不致命）。

## v186 ← / → 找关键帧（"跳了吗 / 落准没 / 看得见吗"）
- 用户把「上一个/下一个关键帧」从出厂 Alt+←/→ 改成裸 `←`/`→`，并清空了
  「播放头左右移 0.5 秒」。实测**键位本身完全生效**：`load_bindings()` 正确保留
  空槽，23 条 QShortcut 里 Left/Right 都 enabled，真实 `QKeyEvent` 能触发跳转。
  → 排查方向别再怀疑"保存/冲突/焦点"，问题全在**反馈与坐标**。
- 五处根因：① `TimelineWidget.set_playhead()` 不 emit `playheadChanged`，而
  `refresh_shot_keyframe_state()` 只挂在 `_on_playhead_changed` 上 ⇒ 快捷键跳完
  **截图按钮不刷新**（鼠标拖裁剪线那条路才刷）；② 回执写在**主窗口**状态栏，
  剪辑是独立窗口，用户根本看不见；③ `_edit_keyframe_times` 漏 `/speed`，
  倍速 2.0 时一次跳过好几个关键帧、落点和红线错开，且被 `clip.end` 过滤掉一半；
  ④ 变蓝容差最宽 0.12s（约 3 帧），差 0.08s 也蓝 ⇒ "蓝了"不代表真在关键帧上；
  ⑤ 裁剪线和关键帧刻度都是红色，重合分不出，且 `KF_MIN_GAP=4` 抽稀会漏线。
- 统一：`Clip.timeline_keyframe_times()` 是**唯一换算入口**（源→时间线，含
  speed、只留 `[src_in,src_out]`、不 round）。`_edit_keyframe_times()` 保留为
  兼容壳委托给它；跳关键帧/贴最近改用 `_edit_all_keyframe_times()` 汇总**整条**
  时间线所有视频片段（选中音频/图片也不再失灵，能跨片段一路找完）。
- 反馈三件套：`set_playhead/nudge_playhead` 补刷新 → 按钮蓝；`ce.flash()` 在
  剪辑窗口内浮「已落到关键帧 x.xxx s · 可无损下刀」（没读到时 warn）；
  命中时把片段内那截线加粗成橙 `_KF_HIT_COLOR`，**画在播放头之后**（片段先画、
  播放头后画，画在 `_paint_clip` 里会被红裁剪线整根盖住，第一次实测橙像素=0）。
- `KF_HIT_TOL=0.02`（半帧@25fps）由 timeline 导出，clip_editor 共用；变蓝
  `tol=max(0.004, min(_snap_tol(), KF_HIT_TOL))`。红线绘制条件放宽到
  `rect.width()>=12 / _th()>=24`，抽稀保留但命中线单独画、不受抽稀影响。
- 旧脚本 `verify_v177.py` 的 `FakeClip` 要补 `end` + `timeline_keyframe_times()`
  才跟得上新入口（假对象缺方法会被 try/except 吞掉，表现为"永远不蓝"）。

## 必守坑（v215 从 MEMORY.md 迁入，权威摘要）
⚠️ `export.py::_reencode` 绝不给音频段建 `[i:v]` 链(音频无视频流→导出失败)。变速(v185)只写 `clip.speed` 不生成文件；工具条等效帧率=源帧率×speed；`lossless.py` 里 speed 是 WARN 非 FATAL；导出走 `_fast_cmds()` 两步 `-itsscale` 流复制，拼不上才 `_fast_reencode_cmd()` 只重编变速段。
- 两坐标系：`clip.keyframes` 源文件时间，`tl.playhead`/`clip.start` 时间线时间(源=`src_in+(时间线-start)*speed`)；换算唯一入口 `Clip.timeline_keyframe_times()`(不 round)；`set_playhead()` 不 emit→代码设播放头补 `refresh_shot_keyframe_state()`；`KF_HIT_TOL=0.02`；框选命中 `_clip_zone()`。
- 右栏「轨道片段」=`clip_list.py::ClipListPanel`(非 timeline)，自维护 `_sel`(有序)+`_hi`、`_apply_selection()`、事件发 viewport、面板↔轨道 `_echo` 挡回灌；⚠️ 测交互要 press+release(itemClicked 在 release emit)。
- 挤占(v194-195)：`TimelineWidget.crowd`+`_insert_ind` 虚线+`Timeline.insert_place()`(落点后同轨整体右移不裁)；拖动中 clip.start/track 已改→先顺延别人再钉回 at，按 `id()` 跳自己。
- 排版：上部区高=画面+进度条(`_fit_preview` cap/need)、`setRetainSizeWhenHidden(False)`、右栏标题在 `#clipListBox` 白框内；改名后缀 `widgets.split_name_stem()` 取磁盘真扩展名、`OutsideClickWatcher.add_inside()` 算标签为内部；关键帧缓存 `_kf_cache[路径]`、`FEATURE_DEFAULTS` 6 键、F1 默认。
