import argparse
import os
from pathlib import Path

# load KEY=VALUE lines from .env in the repo root (API keys live there, never in git)
_env = Path(__file__).resolve().parents[2] / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
import webbrowser

import uvicorn

p = argparse.ArgumentParser(description="Giselle Select 视频本地化 · 网页版")
p.add_argument("--port", type=int, default=8000)
p.add_argument("--no-browser", action="store_true")
a = p.parse_args()
if not a.no_browser:
    webbrowser.open(f"http://localhost:{a.port}")
uvicorn.run("localizer.web.server:app", host="127.0.0.1", port=a.port)
