#!/usr/bin/env python3
"""打包前关闭可能锁住构建目录的旧进程（照片排序台.exe / exiftool.exe 孤儿）。

Windows 下若上次运行的 exe 没退出，会锁住 _internal 里的 DLL，导致 PyInstaller
在 COLLECT 阶段清理旧产物时 WinError 5 失败。这里按 PID 精准关闭，避免 cmd 中文
进程名匹配问题。
"""
import subprocess
import time


TARGETS = ("照片排序台".encode("gbk"), b"exiftool.exe")


def _alive(pid: str) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True).stdout
    return pid.encode() in out


def main():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True).stdout
    except Exception as e:  # pragma: no cover
        print("tasklist 失败:", e)
        return
    killed = 0
    pids = []
    for line in out.split(b"\n"):
        if not any(t in line for t in TARGETS):
            continue
        # CSV 格式: "Name","PID","Session Name",...
        parts = line.decode("gbk", "ignore").split('","')
        if len(parts) < 2:
            continue
        pid = parts[1].strip('"')
        if pid.isdigit():
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                killed += 1
                pids.append(pid)
            except Exception:  # pragma: no cover
                pass
    # 等进程真的退出: taskkill 是异步的, 立刻开始删目录会遇到"文件正在使用"
    deadline = time.time() + 5
    while pids and time.time() < deadline:
        pids = [p for p in pids if _alive(p)]
        if not pids:
            break
        time.sleep(0.2)
    if pids:
        print(f"警告: 进程 {', '.join(pids)} 仍未退出, 可能占用构建目录")
    print(f"已尝试关闭 {killed} 个锁进程")


if __name__ == "__main__":
    main()
