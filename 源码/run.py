# -*- coding: utf-8 -*-
"""开发模式启动脚本: python run.py

隐藏自证快照钩子（不设环境变量时完全无感）:
设置 PHOTOSORT_SNAPSHOT=<png路径> 且 QT_QPA_PLATFORM=offscreen 时,
不进入主窗口, 而是按真实流程构造"自动间隔排序"对话框, 保存:
  <png路径>            对话框整幅截图
  <png路径去掉.png>_popup.png   单位下拉弹开时的截图
  <png路径去掉.png>.json        关键控件的几何数据
用于核对"打包 exe 里界面的真实样子"（与用户所见一致）。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _snapshot_dialog() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.core.photo import Photo
    from app.ui.dialogs import IntervalDialog
    from app.ui.theme import apply as apply_theme

    app = QApplication(sys.argv)
    apply_theme(app)

    out = os.environ["PHOTOSORT_SNAPSHOT"]
    # 12 张不存在的假照片: taken_dt 落回"建议值"逻辑, 与真实使用形态一致
    photos = [Photo(path=f"P_{i:03d}.jpg") for i in range(12)]
    d = IntervalDialog(photos)
    d.show()

    uc = d.unit_combo
    info = {
        "dialog_size": [d.width(), d.height()],
        "unit_combo_geometry_xywh": [uc.geometry().x(), uc.geometry().y(),
                                     uc.geometry().width(), uc.geometry().height()],
        "unit_items": [uc.itemText(i) for i in range(uc.count())],
        "unit_fixed_width": uc.minimumWidth(),
        "font_px": app.font().pointSize(),
        "device_pixel_ratio": d.devicePixelRatioF(),
    }
    d.grab().save(out)

    def _after_popup():
        try:
            uc.view().window().grab().save(out[:-4] + "_popup.png")
        except Exception:
            pass
        Path(out).with_suffix(".json").write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        app.quit()

    uc.showPopup()
    QTimer.singleShot(300, _after_popup)
    app.exec()
    return 0


if os.environ.get("PHOTOSORT_SNAPSHOT"):
    raise SystemExit(_snapshot_dialog())

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
