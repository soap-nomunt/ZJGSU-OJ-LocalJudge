import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config


@dataclass
class Bank:
    name: str
    path: Path
    ids: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.ids)


def parse_ids(text: str) -> list[int]:
    ids: list[int] = []
    saw_structured = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"[\d\s,;，、]+", line):
            saw_structured = True
            for token in re.findall(r"\d+", line):
                try:
                    value = int(token)
                except ValueError:
                    continue
                if value not in ids:
                    ids.append(value)
    if not saw_structured:
        for token in re.findall(r"(?<!\d)(\d{3,6})(?!\d)", text):
            try:
                value = int(token)
            except ValueError:
                continue
            if value not in ids:
                ids.append(value)
    return ids


def discover_banks() -> list[Bank]:
    paths: list[Path] = []
    for directory in (config.BANKS_DIR, config.USER_BANKS_DIR):
        if directory.exists():
            paths.extend(sorted(directory.glob("*.csv")))
    seen: set[Path] = set()
    banks: list[Bank] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = config.read_text_file(path)
        except OSError:
            continue
        ids = parse_ids(text)
        if not ids:
            continue
        banks.append(Bank(path.stem, path, ids))
    banks.sort(key=lambda bank: (0 if "月考" in bank.name else 1, bank.name))
    return banks


def problem_key(problem_id: int) -> str:
    return str(problem_id)


class Progress:
    def __init__(self, path: Path | None = None):
        self.path = path or config.PROGRESS_PATH
        self.data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8-sig") as fp:
                    value = json.load(fp)
                if isinstance(value, dict):
                    self.data = {
                        str(key): entry
                        for key, entry in value.items()
                        if isinstance(entry, dict)
                    }
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        config.ensure_user_dirs()
        try:
            with self.path.open("w", encoding="utf-8") as fp:
                json.dump(self.data, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, problem_id: int | str) -> dict:
        return self.data.get(problem_key(int(problem_id)), {})

    def attempts(self, problem_id: int | str) -> int:
        return int(self.get(problem_id).get("attempts", 0))

    def is_passed(self, problem_id: int | str) -> bool:
        return bool(self.get(problem_id).get("local_passed"))

    def is_sample_passed(self, problem_id: int | str) -> bool:
        return bool(self.get(problem_id).get("sample_only_passed"))

    def update(
        self,
        problem_id: int,
        verdict: str,
        local_passed: bool,
        sample_only_passed: bool,
        elapsed: float,
    ) -> None:
        key = problem_key(problem_id)
        entry = dict(self.data.get(key, {}))
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_verdict"] = verdict
        entry["local_passed"] = bool(local_passed)
        entry["sample_only_passed"] = bool(sample_only_passed)
        entry["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if elapsed > 0:
            best = entry.get("best_time")
            if best is None or elapsed < float(best):
                entry["best_time"] = round(float(elapsed), 3)
        self.data[key] = entry
        self.save()

    def reset(self) -> None:
        self.data = {}
        self.save()


class RandomPicker:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.eligible: list[int] = []
        self.queue: list[int] = []
        self.last: int | None = None

    def set_eligible(self, ids: list[int]) -> None:
        self.eligible = list(dict.fromkeys(ids))
        self.queue = []
        self.last = None

    def _refill(self) -> None:
        self.queue = list(self.eligible)
        self.rng.shuffle(self.queue)
        if len(self.queue) > 1 and self.queue[-1] == self.last:
            self.queue[0], self.queue[-1] = self.queue[-1], self.queue[0]

    def next(self) -> int | None:
        if not self.eligible:
            return None
        if not self.queue:
            self._refill()
        value = self.queue.pop()
        self.last = value
        return value

    def remaining(self) -> int:
        return len(self.queue)
