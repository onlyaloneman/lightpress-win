@echo off
chcp 65001 >nul
setlocal

echo [1/2] 生成图标...
python make_icon.py || goto :err

echo [2/2] 打包 exe...
python -m PyInstaller --noconfirm --onefile --windowed --name LightPress ^
  --icon app.ico --add-data "app.ico;." --collect-all tkinterdnd2 ^
  --exclude-module numpy --exclude-module matplotlib --exclude-module scipy ^
  --exclude-module pandas app.py || goto :err

echo.
echo 完成：dist\LightPress.exe
echo 记得把 ffmpeg.exe / pngquant.exe / gifsicle.exe 复制到 dist 目录。
goto :eof

:err
echo.
echo 构建失败，请检查上面的报错。
exit /b 1
