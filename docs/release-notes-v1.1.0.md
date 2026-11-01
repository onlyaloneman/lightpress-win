首个公开版本。

## 下载即用

下载 `LightPress-v1.1.0-win64.zip`，解压后双击 `轻压.exe`。
包内已带 ffmpeg / pngquant / gifsicle，无需另外安装，也不写注册表。

## 功能

- 填一个目标体积（比如 7），图片 / GIF / 视频都会压到该体积以内
- 支持 JPG / PNG / WebP / BMP / TIFF / GIF / MP4 / MOV / MKV / AVI / WebM
- 批量处理、拖拽添加，原文件不动，输出到 `_压缩` 文件夹
- 可选增强引擎：pngquant（PNG）、gifsicle（GIF）
- 命令行模式：`轻压.exe --cli 视频.mp4 -t 7 -o D:\out\`

## 实测

7.63MB / 2.5 秒的 GIF 压到 2MB：

| 方式 | 分辨率 | 像素数 |
| --- | --- | --- |
| 仅内置算法 | 246×376 | 1× |
| 启用 gifsicle | 390×600 | 2.5× |

## 说明

- 目标体积按 1MB = 1,000,000 字节计算，比资源管理器的口径更严格，各平台上传限制都不会卡线
- exe 未签名，首次运行 Windows SmartScreen 可能提示未知发布者，点「更多信息 → 仍要运行」即可
- 需要 Windows 10/11 64 位
