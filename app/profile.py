import json
from pathlib import Path
from typing import Any, Dict

from app.config import settings


class CandidateProfile:
    def __init__(self, profile_path: str | None = None) -> None:
        self.profile_path = Path(profile_path or settings.PROFILE_PATH)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.profile_path.exists():
            raise FileNotFoundError(f"Candidate profile not found: {self.profile_path}")

        with open(self.profile_path, "r", encoding="utf-8") as file:
            return json.load(file)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_form_values(self) -> Dict[str, str]:
        profile = {
            "name": self.data.get("name", ""),
            "email": self.data.get("email", ""),
            "phone": self.data.get("phone", ""),
            "location": self.data.get("location", ""),
            "years_experience": str(self.data.get("years_experience", "")),
            "visa_status": self.data.get("visa_status", ""),
            "salary_expectation": self.data.get("salary_expectation", ""),
            "work_authorization": self.data.get("work_authorization", ""),
            "professional_summary": self.data.get("professional_summary", ""),
        }
        profile.update(self.data.get("questions", {}))
        return profile
