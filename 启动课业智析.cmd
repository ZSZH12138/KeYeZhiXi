@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if exist "%~dp0课业智析.exe" (
  "%~dp0课业智析.exe" start
  goto :result
)

if defined CONDA_EXE (
  "%CONDA_EXE%" run -n course-insight python -m course_insight.desktop_launcher start
  goto :result
)

where conda >nul 2>nul
if not errorlevel 1 (
  conda run -n course-insight python -m course_insight.desktop_launcher start
  goto :result
)

if exist "D:\software\MyAnaconda\envs\course-insight\python.exe" (
  "D:\software\MyAnaconda\envs\course-insight\python.exe" -m course_insight.desktop_launcher start
  goto :result
)

echo 未找到课业智析可执行程序或 course-insight Conda 环境。
echo 请按照《使用说明》完成安装后重试。
pause
exit /b 1

:result
if errorlevel 1 (
  echo.
  echo 课业智析启动失败，请查看上方错误信息和 runtime\logs 日志。
  pause
  exit /b 1
)
echo 课业智析平台和后台服务已启动。
timeout /t 2 /nobreak >nul
exit /b 0
