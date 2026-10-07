"""
fresh_watcher_runner.py
-----------------------
Thin entry-point for deploying the ultra-fresh job watcher as a separate
Render Worker service.

Render start command:
    python fresh_watcher_runner.py
"""
from app.fresh_watcher import main

if __name__ == "__main__":
    main()
