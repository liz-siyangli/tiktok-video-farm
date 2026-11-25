#!/bin/bash
# 双击启动 Giselle Select 视频本地化工作台（Mac）
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null; then
  echo "需要先安装 ffmpeg：在终端运行  brew install ffmpeg"; read -n 1; exit 1
fi
if [ ! -d .venv ]; then
  echo "第一次运行，正在安装依赖（几分钟）..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || { read -n 1; exit 1; }
fi
.venv/bin/python -m localizer.web
