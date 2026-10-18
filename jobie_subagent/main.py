import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent / "linkedin-job-agent"
sys.path.insert(0, str(APP_ROOT))

from app.main import app
