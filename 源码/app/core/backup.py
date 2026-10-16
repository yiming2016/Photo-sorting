# -*- coding: utf-8 -*-
"""写入前快照与一键回滚。

快照只记录"将被修改字段的旧值", 体积很小, 不复制原文件(需求 6.A)。
旧值为 None 表示该字段原本不存在, 回滚时会把它删掉。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from . import fields as F

SNAPSHOT_ROOT = None


def snapshot_root() -> str:
    global SNAPSHOT_ROOT
    if SNAPSHOT_ROOT:
        return SNAPSHOT_ROOT
    base = os.environ.get("PHOTOSORT_HOME") or os.path.join(
        os.path.expanduser("~"), ".photosort")
    path = os.path.join(base, "snapshots")
    os.makedirs(path, exist_ok=True)
    SNAPSHOT_ROOT = path
    return path


def make_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class Snapshot:
    def __init__(self, snap_id: str = "", folder: str = "", created: str = ""):
        self.id = snap_id or make_id()
        self.folder = folder
        self.created = created or datetime.now().isoformat(timespec="seconds")

    @property
    def path(self) -> str:
        return os.path.join(snapshot_root(), f"snapshot_{self.id}.json")

    @staticmethod
    def load(path: str) -> "Snapshot":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        snap = Snapshot(data.get("id", ""), data.get("folder", ""), data.get("created", ""))
        snap.data = data
        return snap

    @property
    def data(self) -> dict:
        if not hasattr(self, "_data"):
            self._data = {"id": self.id, "created": self.created, "folder": self.folder,
                          "files": [], "labels": []}
        return self._data

    @data.setter
    def data(self, value: dict):
        self._data = value

    def add_file(self, path: str, old_values: dict, stat: tuple | None = None):
        entry = {"path": path, "fields": old_values}
        if stat:
            entry["mtime"] = stat[0]
            entry["ctime"] = stat[1]
        self.data["files"].append(entry)

    def save(self) -> str:
        os.makedirs(snapshot_root(), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        return self.path

    @property
    def file_count(self) -> int:
        return len(self.data.get("files", []))

    @property
    def change_count(self) -> int:
        return sum(len(e.get("fields", {})) for e in self.data.get("files", []))


def list_snapshots() -> list[dict]:
    root = snapshot_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root), reverse=True):
        if not name.startswith("snapshot_") or not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        out.append({
            "id": data.get("id", name[9:-5]),
            "path": path,
            "created": data.get("created", ""),
            "folder": data.get("folder", ""),
            "files": len(data.get("files", [])),
            "changes": sum(len(e.get("fields", {})) for e in data.get("files", [])),
            "rolled_back": data.get("rolled_back", False),
        })
    return out


def build_assignments_for_rollback(entry: dict) -> list[tuple[str, str]]:
    """把快照条目还原成 exiftool 赋值列表。"""
    items: list[tuple[str, str]] = []
    file_items: list[tuple[str, str]] = []
    for key, value in entry.get("fields", {}).items():
        if value is None:
            value = ""
        if key in F.FILE_TIME_KEYS:
            file_items.append((key, value))
        else:
            items.append((key, value))
    return items + file_items
