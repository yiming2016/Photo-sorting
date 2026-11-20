# 网格版式 / 勾选 / 交互 / 顶栏参数条（自 MEMORY.md 迁入）

`MEMORY.md` 只留护栏与铁律；这里放改网格、勾选、折叠、组标题缩进、顶栏参数条前必须照着走的细节。逐版本经过见 `YYYY-MM-DD.md`，完整断言见 `out/repro/verify_v2xx.py`。

## 网格版式 / 勾选 / 交互
- **行类型守卫**：`RowBreak`/`CollapsedPack` 不继承 PhotoRow、photo 无关，`data()` 除 ROLE_ROW 全 None、`flags()` 只 ItemIsEnabled——新行类型必须守卫，否则 Qt 查 DisplayRole 炸在 row.photo。所有按 PhotoRow 找图/勾选/框选/拖拽路径天然跳过它们。
- **紧凑卡(PackRow)**：单文件叶组=组头+唯一卡合成一格（`counts[leaf]==1 and max_depth>=2`），一行可并排多个；继承 PhotoRow 是关键。`_pad_break_before_pack(rows, separator=False)` 三态分流：统一页**不垫**（可与别组照片同排）、排序页垫隐形条、合并页垫条+画 ACCENT 3px 蓝线（折叠的无损组→独立卡边界也垫）。统一头高=delegate `_pack_uni_h()`（全部 PackRow 头高最大值，缓存键 cell_w+rows 对象）；`pack_layout` 文本**顶部锚定**、续行起点=PACK_CONT_X（勾选框左缘，draw 要加 head_rect.x() 偏移）；单段过卡缘 elide 截断。几何单一来源 `_pack_geo/_pack_pitch/_pack_gap`（sizeHint/draw/_pack_sub_at 共用）。⚠️ QListView spacing 两侧各一份：列距=卡宽+2×spacing。
- **统一页叶级组块(v231/v232)**：叶级组（最深层级）打包成 PackRow 宽块（`extra: list[PhotoRow]`，头横贯块宽、子卡横排其下）。**收起=CollapsedPack 原位一格**（只画头块、位置不跳，不再变满宽标题）；中间层(编码/分辨率)折叠仍满宽标题。绘制/命中/`_pack_uni_h`/`_sync_group_checks`/grouplist press·悬浮都要认 CollapsedPack。
- **排序页单文件组收起(v237)**：曾收起变满宽 HeaderRow 独占下一排（实测 `(174,76)`→`(2,331)` "这组往下走"）。`_rebuild_sort` 加 `leaf_collapsed`(collapsed_found & max_depth==len(key) & >=2 & counts==1)→`CollapsedPack` 原位，`last_collapsed` 去重；主窗口 `_sync_group_checks` 排序分支认 CollapsedPack。多文件叶组/月/年收起仍满宽标题、自身不动、下面上移。
- **网格勾选=路径集合(v220)**：`MainWindow._grid_sel_paths` 是合并/统一两态勾选唯一权威；排序态勾选=Qt 选中本身。`beginResetModel` 清空选中且**不发 selectionChanged** → 折叠/重分组/探测回填会丢勾选：`modelReset`→`_on_grid_model_reset` 按路径还原（排序态靠 `modelAboutToBeReset`→`_on_grid_about_to_reset` 按 Photo 对象快照，`modelReset` 按 id 重建，因行号会变）。`_group_members()` 含收起组里的；开始合并/统一/右栏预览读集合不读 `selected_photos()`。
- **统一页点击语义(v233)**：三种卡（多文件块子卡/单文件紧凑卡/普通照片格）同走 `gridPathPress` 路径级语义（左=只勾这张替换可见、Ctrl/Shift=加减、右=补勾绝不取消；收起组勾选保留）——GroupView 靠 `norm_mode` 标志分流（goto_norm/leave_norm/enter_merge/leave_merge 四处同步设）。⚠️ 统一页借用 `merge_mode=True` 只是渲染，**点击语义看 norm_mode**，左键拦截放在 `_press_index=idx.row()` 之前。信号传 bool 不传修饰键整数。
- **左键选中远处文件(v236)**：`ExtendedSelection` press 锚点残留——组标题/组块/空白的 press 拦截分支不调 super 时，带键移动被 `super().mouseMoveEvent` 用残留锚点范围选中远处一批。修复：press 开头一律清 `_press_index=-1`（只有点照片格才重设为拖拽起点）+ `mouseMoveEvent` 末尾带键时直接 return（框选/拖拽路径已先 return 不受影响）。
- **组标题缩进(v240)**：用户口径=**一级<二级<三级越深越靠右**(三角实际 x：2<8<14)。两套坐标系必须联动：① 满宽标题 `delegate.header_layout` 绘制与 `hit_test_header` 命中**共用同一函数**，写死绝对值 level0=0/level1=6/level2=12（历史 v235=12/20/64、v237=6/14/64、v238=3/10/14、v239=0/6/12）。② **单文件叶组紧凑卡**走 `pack_layout` 的 `PACK_X`（卡内相对缩进），v240 从 4 改 12 使三角与三级满宽对齐——⚠️ 曾因 PACK_X=4 导致三级卡三角 x=6 < 二级 x=8，用户报"三级比二级还靠左"。并排多卡绝对 x 不同(首列14/次列106)、相对卡左缘恒=PACK_X。`HEADER_INDENT=26` 常量已无引用但保留。`hit_test_header` 兜底 `"collapse"`→整行标题都能收起（groupcheck/toggle 优先）。
- **配色(v220)**：`theme.HOVER_SOFT`(#E9F1FD) / `SEL_SOFT`(#C8DCFA) / `SEL_EDGE` 资源管理器口径。主网格走 `delegate.draw_photo`；添加弹窗自绘代理不认 QSS `:hover`，由 `_FileListView._sync_hover()` 喂 `set_hover_path()`；树/导航走 `_SELECTED_QSS`（`:hover` 写在 `:selected` 前）。帧率 `NormWorkspace._fps_label` 按 num/den 算（`30000/1001` 不能啃成 `30000001fps`，它还是分组键）。
- **光标(v216/v224)**：全局抓手过滤器 `cursor.py` 把 QAbstractItemView 整个 viewport 染小手。添加弹窗四视图设 `handCursor=False` 豁免、逐点判；`_PanImage` 有图才抓手。改判定前必读 `cursor.py` 头注释。
- **格子规格行去重(v224)**：`grouping.cell_line(spec, wide, skip=...)`——组标题已展示字段不再重复；skip 来源合并页 `header_skip_keys(spec)`、统一页 `_cell_skip_keys(p)`（按三级开关截取）。全跳过且无时长→空串；不传 skip 保持"规格未知"兜底。
- **顶层分组分隔线(v224)**：`delegate.draw_header` 对 level==0 在标题上缘画 ACCENT 3px 粗线。
- 组标题交互(v218)：「统一」没素材时 `NormPanel.set_result(text="")` 收起横幅，`_refresh_result` 先判 `not plan.items`。

## 顶栏参数条(v228)
`norm_param_slot` 用 **`NormParamSlot`**（norm_page.py）：`minimumSizeHint` 压小 + `resizeEvent` 按比例缩放 4 个 combo 的 `setFixedWidth`（富余=fit 文字完整、挤压=整体缩不外溢）。⚠️ **确定性缩放比 layout 魔法可靠**（QBoxLayout stretch/hint 组合实测非单调）。combo 弹层 `view().setMinimumWidth(sizeHint)`；默认窗口 1700→1800（v213"文字完整"口径在 1800 成立）。
