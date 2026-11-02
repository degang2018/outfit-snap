@echo off
rem ============================================================
rem  AI Outfit Video Studio —— 一键启动 (Windows)
rem
rem  使用方法:
rem    1) 首次运行会自动把 backend/.env.example 复制成 backend/.env,
rem       然后停住让你编辑 COMFY_OUTPUT_DIR 等配置
rem    2) 改完 .env 后再次双击本脚本
rem    3) 浏览器自动打开 http://127.0.0.1:8000
rem
rem  依赖:
rem    - Python 3.11+ (加入 PATH)
rem    - ComfyUI 已经在运行 (默认端口 8188)
rem    - backend/.env 里 COMFY_OUTPUT_DIR 指向正确的 ComfyUI output 目录
rem ============================================================
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python,请先安装 Python 3.11+ 并加入 PATH
  pause
  exit /b 1
)

rem ---- 首次启动:帮用户生成 .env ----
if not exist "backend\.env" (
  if exist "backend\.env.example" (
    echo [提示] 未找到 backend\.env,正在从 .env.example 复制...
    copy backend\.env.example backend\.env >nul
    echo.
    echo 第一次运行需要填写配置:
    echo   1) 用记事本打开 backend\.env
    echo   2) 找到 COMFY_OUTPUT_DIR,改成你的 ComfyUI\output 目录路径
    echo   3) 保存后重新双击本脚本
    echo.
    start notepad "backend\.env"
    pause
    exit /b 0
  ) else (
    echo [错误] backend\.env 和 backend\.env.example 都找不到
    pause
    exit /b 1
  )
)

rem ---- 检查依赖 ----
if not exist "backend\run.py" (
  echo [错误] 找不到 backend\run.py,项目似乎不完整
  pause
  exit /b 1
)
if not exist "backend\app\__init__.py" (
  echo [错误] backend\app 目录缺失,请重新克隆
  pause
  exit /b 1
)

rem ---- 检查后端是否已经在运行 ----
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/api/health | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
  echo 后端已在运行,直接打开浏览器。
  start "" http://127.0.0.1:8000
  timeout /t 2 >nul
  exit /b 0
)

rem ---- 检查 pip 依赖是否已装 ----
python -c "import fastapi; import uvicorn; import sqlalchemy; import av" >nul 2>nul
if errorlevel 1 (
  echo [首次运行] 正在安装 Python 依赖 (只需一次,几分钟)...
  cd backend
  python -m pip install -r requirements.txt || (echo 依赖安装失败,请手动运行: python -m pip install -r backend\requirements.txt & pause & exit /b 1)
  cd /d "%~dp0"
  echo [完成] 依赖安装完毕。
)

rem ---- 启动 ----
echo ============================================================
echo  AI Outfit Video Studio
echo ============================================================
echo.
echo  后端数据: backend\app.db
echo  按 Ctrl+C 或关闭本窗口即可停止服务。
echo.
echo  浏览器即将打开: http://127.0.0.1:8000
echo.
start "" http://127.0.0.1:8000

cd backend
python run.py
echo.
echo 服务已退出 (退出码 %errorlevel%)
pause
