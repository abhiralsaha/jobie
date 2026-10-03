import csv
import sys
from pathlib import Path
from typing import Dict, List

from app.job_intake import import_jobs


REQUIRED_COLUMNS = {"job_id", "title", "company", "url"}


def load_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")
        return list(reader)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.import_jobs_csv path\\to\\jobs.csv")
        return 2

    path = Path(sys.argv[1])
    imported = import_jobs(load_rows(path))
    print(f"Imported {len(imported)} new job(s); duplicates were skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
