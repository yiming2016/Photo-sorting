# -*- coding: utf-8 -*-
"""时间戳分配: 按当前列表顺序铺一串等差时间。

约定(需求 1.A): 列表第 1 张 = 时间最新, 之后逐张递减;
需求 2.A: 结果按北京时间保存, 写入 XMP/QuickTime 时由 fields 层换算成 UTC。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import fields as F
from .photo import STATUS_PENDING

STEP_CHOICES = [
    ("1 秒", 1),
    ("5 秒", 5),
    ("15 秒", 15),
    ("30 秒", 30),
    ("1 分钟", 60),
    ("5 分钟", 300),
    ("30 分钟", 1800),
    ("1 小时", 3600),
    ("3 小时", 10800),
    ("1 天", 86400),
    ("自定义…", -1),
]

DIRECTIONS = {
    "desc": "从新到旧（第 1 张最新）",
    "asc": "从旧到新（第 1 张最旧）",
}

SYNC_MODES = {
    "equal": "三个时间完全一致",
    "cascade": "修改时间比拍摄晚 1 秒",
    "keep": "只改拍摄时间",
}

SYNC_FIELDS = {
    "equal": (F.TAKEN, F.CREATED, F.MODIFIED),
    "cascade": (F.TAKEN, F.CREATED, F.MODIFIED),
    "keep": (F.TAKEN,),
}


def compute_stamps(start: datetime, count: int, step_seconds: float,
                   direction: str = "desc") -> list[datetime]:
    out: list[datetime] = []
    for i in range(count):
        delta = timedelta(seconds=i * step_seconds)
        out.append(start - delta if direction == "desc" else start + delta)
    return out


def apply_plan(photos: list, start: datetime, step_seconds: float,
               direction: str = "desc", mode: str = "equal") -> list[datetime]:
    stamps = compute_stamps(start, len(photos), step_seconds, direction)
    targets = SYNC_FIELDS.get(mode, SYNC_FIELDS["equal"])
    for photo, dt in zip(photos, stamps):
        for key in targets:
            if key == F.MODIFIED and mode == "cascade":
                photo.planned[key] = dt + timedelta(seconds=1)
            else:
                photo.planned[key] = dt
        photo.status = STATUS_PENDING
    return stamps


def clear_plan(photos: list):
    for p in photos:
        p.planned.clear()
        if not p.pending and p.status == 1:
            p.status = 0


def suggest_start(photos: list, fallback: datetime | None = None) -> datetime:
    """默认起始时间: 取第一张的现有拍摄时间, 否则用系统当前时间整分。"""
    for p in photos:
        dt = p.taken_dt()
        if dt:
            return dt.replace(microsecond=0)
    if fallback:
        return fallback
    return datetime.now().replace(microsecond=0)
