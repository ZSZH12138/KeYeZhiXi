@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0课业智析.exe" (
  "%~dp0课业智析.exe" stop
  goto :result
)

if defined CONDA_EXE (
  "%CONDA_EXE%" run -n course-insight python -m course_insight.desktop_launcher stop
  goto :result
)

where conda >nul 2>nul
if not errorlevel 1 (
  conda run -n course-insight python -m course_insight.desktop_launcher stop
  goto :result
)

if exist "D:\software\MyAnaconda\envs\course-insight\python.exe" (
  "D:\software\MyAnaconda\envs\course-insight\python.exe" -m course_insight.desktop_launcher stop
  goto :result
)

echo 未找到课业智析可执行程序或 course-insight Conda 环境。
pause
exit /b 1

:result
if errorlevel 1 (
  echo 课业智析停止失败，请查看上方错误信息。
  pause
  exit /b 1
)
echo 课业智析相关进程已停止。
timeout /t 2 /nobreak >nul
exit /b 0
