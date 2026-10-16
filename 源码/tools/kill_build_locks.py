#!/usr/bin/env python3
"""打包前关闭可能锁住构建目录的旧进程（照片排序台.exe / exiftool.exe 孤儿）。

Windows 下若上次运行的 exe 没退出，会锁住 _internal 里的 DLL，导致 PyInstaller
在 COLLECT 阶段清理旧产物时 WinError 5 失败。这里按 PID 精准关闭，避免 cmd 中文
进程名匹配问题。
"""
import subprocess


TARGETS = ("照片排序台".encode("gbk"), b"exiftool.exe")


def main():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True).stdout
    except Exception as e:  # pragma: no cover
        print("tasklist 失败:", e)
        return
    killed = 0
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
            except Exception:  # pragma: no cover
                pass
    print(f"已尝试关闭 {killed} 个锁进程")


if __name__ == "__main__":
    main()
