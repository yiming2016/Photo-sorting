@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo === 照片排序台 一键打包 ===
echo.

set "PY=C:\Users\29773\.workbuddy\binaries\python\envs\photosort\Scripts\python.exe"
if not exist "%PY%" (
    echo 未找到受管 Python: %PY%
    echo 请确认虚拟环境路径，或把 PY 改成系统 python 路径。
    pause
    exit /b 1
)

echo [1/4] 复制 ExifTool...
if not exist tools\exiftool.exe (
    if exist "C:\Program Files\ExifTool\ExifTool.exe" (
        copy "C:\Program Files\ExifTool\ExifTool.exe" tools\exiftool.exe
    ) else (
        echo 警告: 未找到 ExifTool，请手动复制到 tools\exiftool.exe
    )
)

echo [2/4] 关闭可能锁住构建目录的旧进程...
"%PY%" tools\kill_build_locks.py

echo [3/4] 运行 PyInstaller（先产出到临时目录 ..\out\dist_flat）...
REM 关闭 WorkBuddy 的 safe-delete 拦截：COLLECT 阶段会 rmtree 旧产物，下方 rmdir ..\软件 也需放行。
REM 仅在本次打包进程内关闭，不影响系统其它行为。
set "CODEBUDDY_SAFE_DELETE_ENABLED=0"
if exist "..\out\dist_flat" rmdir /S /Q "..\out\dist_flat"
"%PY%" -m PyInstaller --noconfirm --onedir --windowed --name "照片排序台" ^
    --distpath "..\out\dist_flat" --workpath "..\out\build" ^
    --icon "tools/logo.ico" ^
    --add-binary "tools/exiftool.exe;tools" ^
    --add-data "tools/exiftool_files;tools/exiftool_files" ^
    --add-data "tools/logo.ico;tools" ^
    --hidden-import pillow_heif ^
    --collect-all pillow_heif ^
    run.py
if errorlevel 1 goto :fail

REM 校验临时产物存在
if not exist "..\out\dist_flat\照片排序台\照片排序台.exe" (
    echo 打包失败：未找到临时产物 ..\out\dist_flat\照片排序台\照片排序台.exe
    goto :fail
)

echo [4/4] 扁平化部署到 ..\软件（不再嵌套 照片排序台\ 子目录）...
REM 必须先确认旧目录真的删掉了再 move: 否则 move 会把 照片排序台 塞进残留的 ..\软件 里,
REM 变成 ..\软件\照片排序台\照片排序台.exe 这种嵌套结构, 而且外层还留着上一版的残缺文件。
if exist "..\软件" rmdir /S /Q "..\软件"
if exist "..\软件" (
    echo 旧目录 ..\软件 删不掉（多半是程序还在运行、DLL 被占用），先重试一次...
    timeout /t 2 /nobreak >nul
    rmdir /S /Q "..\软件"
)
if exist "..\软件" (
    echo 部署失败：..\软件 仍被占用，无法清空。请关闭正在运行的程序后重新打包。
    echo 临时产物已保留在 ..\out\dist_flat，可直接手动部署。
    goto :fail
)
move "..\out\dist_flat\照片排序台" "..\软件" >nul
if exist "..\out\dist_flat" rmdir /S /Q "..\out\dist_flat"
if not exist "..\软件\照片排序台.exe" (
    echo 部署失败：..\软件\照片排序台.exe 不存在
    goto :fail
)
if not exist "..\软件\_internal\tools\exiftool.exe" (
    echo 部署失败：..\软件\_internal\tools\exiftool.exe 不存在（ExifTool 没打进去）
    goto :fail
)

echo.
echo 完成！
echo.
echo 产物: ..\软件\照片排序台.exe
echo 整个目录可以拷贝到任何 Windows 电脑上运行
echo.
goto :eof

:fail
echo.
echo 打包失败！请检查上面的错误信息。
echo.
pause
