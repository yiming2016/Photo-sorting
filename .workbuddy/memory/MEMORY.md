# 照片排序台 — 项目记忆（durable）

PySide6 桌面应用：拖拽排序照片/视频后写时间戳回 EXIF/XMP/QuickTime/文件系统；三形态 排序/合并/统一（剪辑独立窗口）。最新 **v257**（2026-11-20）。源码 `源码/`、产物 `软件/`（gitignored），git 身份 `PhotoSort Dev <photosort@local>`。
细则分流：界面/元数据/布局/光标/回归 → `UI_DETAIL.md`；网格/勾选/顶栏参数条 → `GRID_DETAIL.md`；剪辑/无损 → `CLIP_EDIT.md`；**逐版本经过/根因/验收数据 → `YYYY-MM-DD.md`（写日志前先读当天最后一条）**；本文件只留当前仍生效的硬约束，案例细节去日志查。

## 用户护栏（不可违背）
- 第1张=最新递减；北京时间显示、写入转 UTC；手填起始时间；文件系统+EXIF+XMP+QuickTime 全写；图片+视频；快照不复制原文件；年+月可叠加；独立 exe；只处理当前文件夹(可选子目录)；EXIF 可改。
- **统一参数配置弹窗必须非模态(v261)**：打开后主界面照常可操作；单一实例 show()+NonModal+WA_DeleteOnClose，别回退 exec()。弹窗下拉当前项/悬浮必须浅蓝高亮，由 `_ComboHighlightDelegate` 自绘（QSS 改不动）。
- **不得重置/覆盖用户快捷键**：测试只把 `APPDATA` 指临时目录、**绝不改 `LOCALAPPDATA`**（FFmpeg 靠它找）；`settings._config_path()` 离屏兜底 `Temp\photosort_test_<pid>\`；`_save()` 备份 `merge.json.bak`；回归里 `save_edit_keys*`/`save_as_default`/`clear_default` 先沙箱；真机 `merge.json` 两槽新格式、备用键全空 → **不得改配置结构**，动配置链路需授权。
- 新功能取舍(v182后)：只做「比传统剪辑明显更快出片」的；画面不动或只重做声音可接受；转场/字幕/调色/抠像等整条重渲染不做；加什么先商讨。
- 偏好：先给根因与方案、确认后动手（模糊点用 AskUserQuestion 立刻问）；不超范围；零 bug 容忍、自行离屏验证、不反复让用户回报；导出前只在时间线上操作。

## 排错铁律（案例/取证细节见当天日志）
- **QLabel 富文本分隔线**：禁用带 background 的 `<div>`（铺满到文本末尾，v249 整片灰）、禁用 `<hr>`（间距硬编码 10/9px 不可改）。定案 `<table width=100% cellpadding=0 cellspacing=0><tr><td style="border-top:1px solid 色;height:1px;line-height:1px;font-size:1px;">&nbsp;</td></tr></table>`。块级线前后不插 `<br>`（用 `compare_model.join_rows`）。单一来源 `DIVIDER_HTML`/`SECTION_LINE_HTML`/`DIVIDER_COLOR`/`SECTION_LINE_COLOR`。ⓘ：`grouping.FIELD_DESC`(文案导出在根目录`参数说明.txt`)+`compare_model.info_html()` 自绘圆i套 `<a href=info:key title=说明>`；右键表格用 `_CompareInfoDelegate`+`helpEvent`。三处对比区都加ⓘ。
- 按钮无反应又不崩 = Qt 吞构造异常(--windowed 无 stderr) → offscreen 直接构造 + traceback；慢活不进 `__init__`（`QTimer.singleShot(0)`→QThread→信号回主线程→try/except+`QMessageBox.critical`）。子进程走 `merge/proc.py::proc.run/popen`，须打 --windowed exe 验。
- **离屏验证**：offscreen + `WA_DontShowOnScreen` + `show()` + processEvents + `grab()`；不要 `app.exec()`；先建 QApplication 再 `addApplicationFont(msyh.ttc)`；真 QMessageBox/QMenu/QDialog.exec 挂死→打桩；结尾停 `thumb_worker` 后 `os._exit()`、启动加 `-u`；**测宽/配色必须 `theme.apply(app)` 后实测**。⚠️ 合成鼠标事件不达对象过滤器→直接调 `eventFilter`/`_sync_hover`/`mouseMoveEvent`；QMouseEvent 第4参是 buttons、悬停必须 `NoButton`；`QStandardItem(Model)` 在 QtGui；别信 QFlags 的 repr/int()；spy `selectionChanged` 用 `f_back` 定位；别用 disconnect 包装 handler 验证时序。
- **禁止模块级构造任何 Qt 绘制/字体对象**（QImage/QPainter/QFont/QPixmap）：导入链早于 QApplication 创建→打包 exe 静默闪退（v190/v251 踩）。离屏脚本会掩盖此问题，须靠"不建 QApplication 直接 import"或打包起 exe 验。做法：`@lru_cache` 延迟到首次业务函数调用。
- **符号字形不可靠**：微软雅黑缺 ⚙✅ 等字形→`setText` 变空方框；一律 `edit_icons.make_icon()`/QPainter 自绘 QIcon；**必须 grab 截图目检**，只断言 `text()` 会漏。
- **小图标 tooltip 不可靠(v255)**：富文本小ⓘ配原生 tooltip 命中区小+延迟→实测不弹。三处对比区统一 `compare_model.InfoPopupFilter`（镜像 QTextDocument 的 `anchorAt()` 命中 `info:key`，立即弹无边框 QLabel）。离屏平台根本不生成 tooltip 窗口，别用离屏实验下"能弹"结论；测命中直接调 `eventFilter`。INFO_COLOR=`#B0B8C6`(淡灰实心，断言 R≈G≈B 差<25)。
- Qt "嘟嘟"声=`QApplication.beep()`→`qwindows.dll`→`user32!MessageBeep`；`app/ui/beep.py::silence()` 在 `main()` patch 入口；`PHOTOSORT_BEEP=1` 留原声。
- 菜单判可用别调有副作用函数(`split_at()`→`can_split_at()`)；`settings._save` 空值删键把 0 当空→`value is not None and value != ""`；合并页行列空→`_rebuild_norm_pending()`+`_resync_merge_list()`；**同名方法后定义覆盖先定义**；bash `mv A B已存在目录` 会嵌套成 B/A。
- **改铺行结构/口径后必改旧脚本写死的断言再复跑**（v226/v235 假失败）；全量回归 NG 以实测为准（见当天日志），非环境限制项必须 0。

## 业务逻辑红线（改这几处必看）
- **输出路径**：`pathsafe.check_output()` 必挂合并/导出/工具入口——撞素材或落 `_photosort_norm/` 一律拒。合并走 `-f concat -i list.txt` 看不到真输入，`-y` 会**截断源视频**。
- **快照**：读不出元数据 → `snapshot_ok=False`，写入/回滚都跳过。
- **写入成败**：`exiftool.write` 返回 `(ok,text)`，看 `_judge_write`；别用 `"error" in 输出.lower()`。
- **统一档位单一来源**：`settings.normalize_profile_current`（生成副本、`norm_paths.profile_key(None)`、`NormProfile.from_dict(None)` 全用它）。「保存为默认」写 `normalize_profile` 基准档，日常不读。
- **统一判定(v219)**：`normalize.target_spec()`+`_FIXABLE` 与 `grouping.signature()` fatal 清单对齐 → need=False 就真能无损拼，别退回手写 if。目标值要与 ffprobe 同类型同大小写。HDR 靠分组隔离。
- **画面类可重渲染(v242)**：档位 `target_res/target_fps/target_color`，默认全 `keep`=等价v241（`key()` 只在非keep追加段→老副本指纹不变）。显式目标时 `target_spec` 反映目标+重算 level、`build_cmd` 出 `-vf`(scale+pad→fps→zscale/tonemap→format)、有帧率目标去掉 `-fps_mode passthrough`。⚠️ zscale 链先 `setparams` 补源色彩元数据(缺源报 "no path between colorspaces")。⚙弹窗 `NORM_PARAM_ROWS` 13项(可调/推导/固定)，7 combo 权威(`panel.profile()` 从 combo 读)；13项表 key(width/r_frame_rate/color_transfer)≠profile字段(target_*)，靠 `_NORM_KEY_TO_PROFILE` 映射。改 settings 后等事件循环重算再量高度（`QTimer.singleShot(0)`；`deleteLater()` 后 `sizeHint()` 是 0，直接 `setFixedHeight(0)` 锁死顶栏）。外露勾选 v246 实时生效+关窗(X/ESC)回滚(`_orig_exposed`)，`_apply`/`_save_default` 标 `_saved=True`；脚本构造主窗口前须重置 `save_norm_exposed(DEFAULT_NORM_EXPOSED)`。v247 四类各套 `QFrame#normGroupCard`。
- **副本**：指纹=大小+首尾64KB哈希（非mtime）；`collect_files` 跳过 `_photosort_norm`；清理入口在合并右栏 ⚙，`find_work_copies` 只收 `is_work_copy()`(_ns_ 前缀)。
- **顺序/拖拽**：`model.orderChanged`+`view.orderChanged` 都接 `_on_order_changed`；`move_rows` 只在排序态写 `group_override`；统一态跨组拦截用 `GroupView._group_lock_key`→`norm_key`；统一页铺行前按 key **稳定排序**(`_rebuild_norm` 靠同前缀相邻画组头)。
- 扫描忙时 `_scan_files` 一律**排队**(`_picker_pending`/`_pending_replace`)，不丢文件；间隔下限1秒。
- **统一非模态(v222/223)**：进度框 `modal=False+show()`(存 `_norm_dialog`)。**冲突五处必拦**(`_normalize_worker` 非空→提示)：重复统一/清副本/开始合并/`do_write` 写时间戳/`_export_timeline` 剪辑导出。`norm_paths.mark_writing`+`resolve()` 对转码中源回退原片；退出时 `nd._allow_close=True`。删文件不用拦。模块级集合别写 `set = {}`。
- **层级级联(v225)**：排序 年→月→日、统一 编码→分辨率→帧率：开子级自动开父级、关父级连带关子级；共享 toggled 用信号 `_name` 区分触发源；落盘在级联后读最终状态再 set_master。测试改开关走 `set_master`/`set_norm_master`。
- **参数对比排序(v248)**：三处 UI 共用 `grouping.compare_rows`+`compare_model`。三档大项(fatal→warn→None)→档内 media(video→audio→other)→`SUB_ORDER`→出厂顺序；归类表 `FIELD_CATEGORY` 只影响展示；视频/音频交界 `media_divider_before`+`DIVIDER_HTML`。
- **v256 bug 自查（改这些必看）**：
  - 写入/导出取消**绝不删用户旧输出**：ffmpeg 写 `pathsafe.temp_output()`(同目录 dotfile)→全成功才 `os.replace(tmp,out)`；禁止 `os.remove(out_path)`。
  - `pathsafe.norm` 只 `normcase(abspath)`，绝不 expandvars/expanduser。
  - 读回不一致=失败：`WriteResult.mismatch_paths`+`writer.ok_files` 排除 mismatch，标 status=3 提示重试。
  - 回滚标记 `should_mark_rolled_back(ok,failed)` 仅全成功标 True；`open_rollback` 有 pending 确认守卫。
  - exiftool `start()` 全程 self._lock(RLock)+二次 running 检查；预热 `execute("-ver",_retry=False)` 否则无限递归。
  - ⓘ 单一入口 `compare_model.desc_for(key)`：FIELD_DESC 优先(110条)+`_DESC_FALLBACK`(bitrate_mode)；富文本 title 与弹窗都走它，`info_html` 用 `html.escape(quote=True)`。
  - ⓘ 文案与界面显示值同口径(v257)：level 用 `_level_text`(H.264 idc/10、H.265 idc/30；H.265 idc 120/150/153/180=4.0/5.0/5.1/6.0)，帧率用 `_fps_text`(24/25/30/50/60)，H.265 档次写 Main 10/Main 12，field_order 复用 `_norm_field_order`。技术事实改前用权威表核对。

## 网格版式 / 勾选 / 交互 / 顶栏参数条
→ 详见 `GRID_DETAIL.md`（改前必读）。

## 打包发布（改完必重打包 + 开 exe 验收）
- 命令(`源码/` venv)：`python -m PyInstaller --noconfirm --onedir --windowed --name "照片排序台" --distpath ../out/dist_flat --workpath ../out/build --icon tools/logo.ico --add-binary "tools/exiftool.exe;tools" --add-data "tools/exiftool_files;tools/exiftool_files" --add-data "tools/logo.ico;tools" --hidden-import pillow_heif --collect-all pillow_heif run.py`
- 部署：`taskkill //IM "照片排序台.exe" //F` → `mv dist_flat/照片排序台 out/dist_flat_旧_vNN` → `mv 软件 out/部署前软件_vNN` → `cp -r dist_flat/照片排序台 软件` → `touch 软件/.noactivate` → `explorer.exe "软件\照片排序台.exe"` → 查进程 MainWindowTitle=vNN → `rm -rf out/build`。⚠️ 禁 Move-Item；mv 一步一条；mv 软件 busy=旧 exe 活着；COLLECT 删已存在 dist 被 safe-delete 拦→先 mv 走。
- PyInstaller 6.x add-binary 落 `_internal/`→`find_exiftool()` 用 `parents[2]`；禁 `Qt.WindowDoesNotActivate`。⚠️ 打包与测试别同时跑；源码 mtime 晚于 exe=必重打包。离屏 Python：`C:\Users\29773\.workbuddy\binaries\python\envs\photosort\Scripts\python.exe`。
- **GitHub 发布现状（2026-11-21）**：远端 `yiming2016/Photo-sorting` main = v261 源码快照（commit `ce196688…`），Release `v1.0.0-build261` 含 `PhotoSorting_v1.0.0_build261.zip`（68MB）；旧 `v1.0.0`（build 49）保留回退。软件内 `__version__=1.0.0` 不变，发布标签用 build 号区分。
- ⚠️ 本机 git 配 `http.proxy=127.0.0.1:10808`（代理不开时 `github.com:443` 不通），但 `api.github.com`/`uploads.github.com` 通。发布优先试 git；不通则走 REST：POST blobs → POST tree(base_tree) → POST commit → PATCH refs/heads/main，Release/资产用 `/releases` + uploads API。PAT 只放 HTTP 头/环境变量，绝不写 remote URL 或文件；发布后提醒用户轮换。
