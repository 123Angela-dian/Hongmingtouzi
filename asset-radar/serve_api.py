from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.asset_radar.api_server import main


if __name__ == "__main__":
    main()
