# 界面 / 元数据 详细口径（v216 快照）

`MEMORY.md` 只留护栏与铁律；这里放"改界面时必须照着走"的细节。版本演进见 `YYYY-MM-DD.md`。

## 元数据
- `fields.writable`：File 组只放行 FileModifyDate/FileCreateDate；只读含 Composite/System/ExifTool/Warning/Error/ICC_Profile；`READONLY_TAGS`+`READONLY_TAG_PREFIXES=("history","derivedfrom")` 也是 writer 白名单，别放宽；树字段名列(0) 永不编辑，只读由 `data(1,ROLE_WRITABLE)` 挡。ExifTool 用 `-G`；图片不写 QuickTime、视频不写 EXIF；大视频 `-overwrite_original_in_place`；文件系统时间最后写；`setForeground` 只收 QBrush/QColor。
- ⚠️ 畸形元数据时区：`fields.parse_dt()` 构造 `timezone()` 前校验 `hh<=23/mm<=59` 且放 try（`+99:99`/`+24:00` 抛 ValueError，`+08:99` 静默偏 1h39m）；`scanner.py` sort 只有 try/finally 无 except→**坏文件让整文件夹加载不出**；非法时区按「没写时区」保留日期时间主体、别回退文件名。

## 主界面骨架
- 分栏：顶栏归左栏 `left_side`，与 `right_col`(max360) 进 splitter；网格四档 88×96/104×116/132×150/168×202；危险操作二次确认默认「否」；永久删除 `send_to_recycle_bin()`；FFmpeg 三态 `ffmpeg_ok` 与有无视频解耦；属性窗非模态可多开；添加弹窗三栏(190|文件区|232)、`pathsAdded` 立即加入不关窗；参数分档在 ⚙️ 改、`grouping.apply_config()` 唯一入口；三处右键菜单(素材框/时间线/右栏轨道片段)最后一组只放「对比/属性」。
- 三形态滑块：`modeSwitch` 轨道 + `modeBtn`×3(checkable+autoExclusive，`goto_sort/goto_merge/goto_norm`)，选中段实心 ACCENT 蓝底白字。⚠️ **尺寸只跟内容**：`msl.setSizeConstraint(QLayout.SetFixedSize)` + 容器 `(Fixed,Fixed)`，按钮只留 `padding:0 13px`（QSS `min-width` 会和 padding 叠加把按钮撑宽→溢出轨道裁蓝块）；轨道 `#E7EAF1`+`1px #D5DBE5`，圆角同心 9=内块6+padding2+边框1。共用 splitter 切 `self.stack`(view/empty) 与 `right_stack`(inspector/merge_panel/norm_workspace.panel)；`merge_mode`/`norm_mode` 互斥。
- ⚠️ **顶栏最小宽别撑大主窗口**：切形态那一帧新旧控件同帧可见会让顶栏 min 冲 1400+，Qt 把窗口撑大且不缩回 → `enter_merge()` 开头先 `norm_param_slot.setVisible(False)`；`_TopBar.minimumSizeHint()` 封顶 1040。默认窗口 **1700×900**（统一态顶栏固定内容≈1280，1600 会把控件叠住——QBoxLayout 溢出是"叠"不是"裁"）。
- 顶栏右侧状态用 `widgets.ElidedLabel`(min 宽 0，**放不下整条就不画**)并 `addWidget(top_status, 1)` 吃富余；小下拉(`#mainViewCombo`/`#normCombo`)QSS 不写死宽度，一律代码 `setFixedWidth`。

## 统一页
- 组件：`app/ui/norm_page.py`(NormWorkspace+NormPanel；NormPreview / 弹窗 NormalizeDialog 保留给 v200-v204 验收)。右栏照搬合并(预览→保存位置→参数对比→底部三按钮「开始统一|打开位置|剪辑」)。4 个参数下拉 reparent 到 `norm_param_slot`(对象仍归 panel)，宽度按最长选项现算(`fit_combo_width`)，`cell()` 里 `w.setFixedWidth(width)` 整格定死(否则换选项 audio 被压到 181px)。
- 三级分组：复用合并网格 `self.view`，`PhotoModel.norm_groups`+`_rebuild_norm()`(key=`photo.norm_key` 三级元组)，`HeaderRow.norm_mode/norm_need`→`draw_header` 精简标题（v241 起达标提示文案走 `delegate.norm_need_text()` 单一来源：`N个`/`可直接快合`，满宽标题与 `pack_lines` 紧凑卡共用，别再各自写死）；`_group_members`/`_sync_group_checks` 按 norm_key 前缀统计；进出统一态清 `norm_groups`+`photo.norm_*`；⚠️ `set_merge_groups` 别误删。素材=主列表全部视频(优先复用 merge_specs、缺的 `_start_probe` 补测)，探测/FFmpeg 回调都要有 norm 分支原位刷新 plan 不动参数档。
- 层级开关：`norm_master{codec,res,fps}`+`per_norm{编码:{res,fps}}`，`norm_key_of()` 截断 `photo.norm_key` 深度(编码关=平铺)；顶栏 `sw_codec/sw_res/sw_fps` 接在 `sw_day` 后(**与年月日同位**，`vl2` 合并态才收)；组标题给 `("fps","res")` 胶囊(`_switch_w()` 按字宽：分辨率 56、帧率 44)，`GroupView.normToggle(tuple,str,bool)`。⚠️ 开关触发 rebuild→Qt 清空选中→「开始统一」按选中路径走，handler 必须存 keep 路径、重建后 `_select_photos()` 还原(`_after_norm_regroup`)。

## 下拉 / 网格 / 间距
- 显示方式下拉三态共用：`_sync_mode_ui` 不藏 `view_mode_combo`/`vl3`；档名「小/中/大/超大图标」(「中等图标」已废)；宽度 `_fit_view_combo()`=最长档名+36(给 `_CenterComboArrow` 叠画三角留位)；`_CenterAlignDelegate` 只留浅蓝底、不画对勾(勾会压末字)。
- 下拉弹层(v215)：宽度 `widgets.fit_popup_width()`=`max(控件宽, 最长项+40)`，挂在各 combo 的 `showPopup`(控件宽只算到文字区，弹层还多 20 项内边距+10 视图 padding/border)；"两层背景"根因=弹层容器 `QComboBoxPrivateContainer` 铺直角底而 view 画圆角，**app 级 QSS 到不了该容器**，只能 `pop.setStyleSheet("QFrame#comboPopup{background:transparent;border:none}")`（**必须带 #id**，裸写会级联洗掉列表）+`WA_TranslucentBackground`/`WA_NoSystemBackground`；入口 `widgets.prepare_combo_popup()`(在 `_CenterAlignDelegate.__init__` 自动调)。⚠️ 透明弹层下离屏 `grab/render` 会错位(文字被切)，验收看真机截图；查四角透明垫洋红背板。
- 下拉高亮(v261)：全局 `QWidget{background:transparent}` 会把 QComboBox 弹层默认 selected 底色冲成白底，**只写 `QComboBox QAbstractItemView::item:selected` 的 QSS 无效**。必须自绘委托：主界面顶栏 `_CenterAlignDelegate`(居中)、弹窗 `_ComboHighlightDelegate`(左对齐)都在 `paint()` 里按「当前项/悬浮/选中」铺 `T.ACCENT_SOFT` 圆角底，再清掉两个 state 交给基类渲染；弹层还要 `view().setMouseTracking(True)` 否则悬浮不触发。`_DownComboBox` 已统一挂左对齐版。
- 网格间距/空态：`delegate.HEADER_H=32`+`GroupView.setSpacing(2)`(Qt 项四周各加 2 → 相邻两项差 4px)；没文件时 `empty_page` 与 `QListView` 必须同一张白卡(`objectName=emptyPage`+`WA_StyledBackground`，底色/1px 边/radius 10 三项同值)，否则清空/切形态时主区从白卡跳成灰底。

## 光标（v216）
- 入口 `app/ui/cursor.py::install()`，`main()` 与 `MainWindow.__init__` 双装(幂等)。判定 → 手：`QAbstractButton`/`QComboBox`/`QMenu`/`QMenuBar`/`QTabBar`/列表表格树(只染 **viewport**)/自己重写 `mousePress|Release|DoubleClickEvent` 的控件/可点链接 QLabel。
- 绝不碰：`QLineEdit`/`QTextEdit`/`QPlainTextEdit`/`QAbstractSpinBox`/`QAbstractSlider`/`QScrollBar`/`QSplitterHandle`（自带 I 形/拖拽箭头），以及任何已 `WA_SetCursor`(自己设过光标)的控件。
- 显式开关：`setProperty("handCursor", False)` 豁免、`True` 强制指定。已用：`GroupView`(自己按区域判光标，标题行空白才是箭头)、`Inspector.set_photos` 多选/空时的 `preview`(点了没用)。
- 行为：禁用自动还箭头(`EnabledChange`)，重新启用又变手。只在 `Polish`/`Show`/`EnabledChange` 三个时机动手，之后不插手(动态改光标的控件照旧)。⚠️ **别用 `QEvent.ChildAdded` 判定**——事件在 C++ 构造中发出，PySide 包装还没升成子类(`ev.child()` 是 QWidget)→ 判定全落空。
- 例外说明：`clip_list` 已删掉自己那套 `setCursor/unsetCursor`（v194 只为小铅笔加手），现在整片片段清单都是手；`clip_editor` 的音量滑杆/数值框仍由它自己设手(有意)。

## QSS / 布局 / 线程 / 抽帧（自 MEMORY.md 迁入，v217）
- QSS：`QListWidget::item` 吞 setBackground→自绘 delegate；不认 `#RRGGBBAA`→`rgba()`；`✕`→`×`；路径用正斜杠；小图标按钮 padding:0；`~Qt.XxxHint` 清高位→int 位运算；QComboBox 必 `setFixedWidth`；普通 QCheckBox→自绘 `widgets.CheckCheckBox`。QImage 显式给每行字节数(rgb24 宽%4≠0 错位)→`player.image_rgb()`。
- 布局：stretch 全 0、富余给能增长项；自绘容器 (Preferred,Fixed)+显式 stretch；`geometry()` 相对父→`QRect(child.mapTo(owner,QPoint(0,0)),child.size())`；叠裁剪 `Qt.IntersectClip`；弹窗摆位放 `showEvent`；键盘依赖焦点→QApplication eventFilter 收 KeyPress/Wheel 并 return True，关窗摘掉；**剪辑弹窗一律挂 `_dlg_parent()`**。
- 线程(每帧 new QThread→Qt6Core.dll 0xc0000409)：①工作线程只产 QImage ②常驻线程+覆盖式 `request()` ③没真停前不丢引用/deleteLater ④自绘夹可见范围、`ok_num()` 守 NaN ⑤`app/crashlog.py` 在 `main()` 首行安装→`%LOCALAPPDATA%/照片排序台/logs/app.log`；`FilmstripWorker.MAX_QUEUE=400`；残留 eventFilter 碰已销毁对象→`try/except RuntimeError` 放行；`closeEvent` 停线程前先调 worker 的 `cancel()/stop()`。
- ffmpeg 批量抽帧：`-ss START -i SRC -vf "select='between(t\,A\,B)+...',scale=160:-2" -frames:v N -fps_mode vfr -q:v 4`；⚠️ `t` 相对 `-ss` 起点、必须 `-frames:v N`、张数不符整批作废退回逐张、一簇≤8s。
- 选择集/框选：真行数 `model.rowCount(rootIndex())`；**`setCurrentItem/Row()` 清选择集**→用 `selectionModel().setCurrentIndex(idx,NoUpdate)`；**Clip 不可哈希**→按身份 `is` 去重；Qt 虚函数抛 TypeError=abort；左键 move 全由 `_mq_move` 吃、press 先 `_mq_teardown()`。

## 回归脚本（自 MEMORY.md 迁入，v218）
- 口径变更后旧脚本里写死的断言（`==N`、菜单文案、光标行为、标题空白处没反应）会假失败 → 改成新口径再复跑，别删断言。
- `_run_all.py` 长期 NG 只剩 5 个 safe-delete 环境限制（v138/143/144/145/152，单独跑能过）；其余脚本必须 0 失败。v190 起每版都入清单。
- 改 `FEATURE_DEFAULTS` 键数 / F1 默认项数 / 改名文案时，同步 `verify_v177`、`v179`、`v192`、`v193`；v152「同一行」别拿 `shot_q` 比 y，用 `shot_fmt`。
- 真机光标：`out/repro/probe_cursor_real.py`（SetCursorPos → GetCursorInfo 比句柄，探完还原鼠标）。
- 假菜单（`_FakeMenu`）要认 `addSeparator()` / 动作 `setToolTip()`，否则 ⚙ 菜单一加分组线就报 AttributeError。
