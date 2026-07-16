from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

sys.stdout = (LOG_DIR / "api_stdout.log").open("a", encoding="utf-8", buffering=1)
sys.stderr = (LOG_DIR / "api_stderr.log").open("a", encoding="utf-8", buffering=1)
sys.path.insert(0, str(ROOT))

from src.asset_radar.api_server import main


if __name__ == "__main__":
    print(f"\n[{datetime.utcnow().isoformat()}] starting Asset Radar API")
    main()
