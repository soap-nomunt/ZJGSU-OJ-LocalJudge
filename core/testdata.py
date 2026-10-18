import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class TestCase:
    name: str
    input_path: Path
    expected_path: Path
    kind: str
    time_limit: float = 1.0


@dataclass
class ProblemData:
    problem_id: int
    catalog: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    tests: list[TestCase] = field(default_factory=list)

    @property
    def title(self) -> str:
        value = str(self.catalog.get("title", "")).strip()
        return value or f"{self.problem_id} 号题"

    @property
    def time_limit(self) -> float:
        try:
            value = float(self.meta.get("time_limit", self.catalog.get("time_limit", 1.0)))
        except (TypeError, ValueError):
            value = 1.0
        return max(0.1, value)

    @property
    def sample_count(self) -> int:
        return sum(1 for test in self.tests if test.kind == "sample")

    @property
    def generated_count(self) -> int:
        return sum(1 for test in self.tests if test.kind == "generated")

    @property
    def custom_count(self) -> int:
        return sum(1 for test in self.tests if test.kind == "custom")

    @property
    def has_tests(self) -> bool:
        return bool(self.tests)


def load_catalog() -> dict:
    try:
        if config.CATALOG_PATH.exists():
            with config.CATALOG_PATH.open("r", encoding="utf-8-sig") as fp:
                value = json.load(fp)
            if isinstance(value, dict):
                problems = value.get("problems", value)
                if isinstance(problems, dict):
                    return {str(key): entry for key, entry in problems.items() if isinstance(entry, dict)}
    except (OSError, ValueError):
        pass
    return {}


def catalog_entry(catalog: dict, problem_id: int) -> dict:
    return catalog.get(str(problem_id), {})


def load_meta(problem_id: int) -> dict:
    path = config.TESTS_DIR / str(problem_id) / "meta.json"
    try:
        if path.exists():
            with path.open("r", encoding="utf-8-sig") as fp:
                value = json.load(fp)
            if isinstance(value, dict):
                return value
    except (OSError, ValueError):
        pass
    return {}


def _number_from_name(name: str) -> int:
    match = re.search(r"(\d+)", name)
    return int(match.group(1)) if match else 9999


def _kind_order(kind: str) -> int:
    return {"sample": 0, "generated": 1, "custom": 2}.get(kind, 3)


def _discover_tests(directory: Path, time_limit: float, custom: bool = False) -> list[TestCase]:
    if not directory.exists():
        return []
    tests: list[TestCase] = []
    for input_path in directory.glob("*.in"):
        stem = input_path.stem
        expected_path = input_path.with_suffix(".out")
        if not expected_path.exists():
            continue
        lower = stem.lower()
        if custom:
            kind = "custom"
            name = f"自定义 {stem.replace('custom', '') or ''}".strip()
        elif lower.startswith("sample"):
            kind = "sample"
            name = f"样例 {_number_from_name(stem)}"
        else:
            kind = "generated"
            name = f"测试 {_number_from_name(stem)}"
        tests.append(
            TestCase(
                name=name,
                input_path=input_path,
                expected_path=expected_path,
                kind=kind,
                time_limit=time_limit,
            )
        )
    tests.sort(key=lambda test: (_kind_order(test.kind), _number_from_name(test.input_path.stem)))
    return tests


def load_problem(problem_id: int, catalog: dict | None = None) -> ProblemData:
    catalog = catalog if catalog is not None else load_catalog()
    meta = load_meta(problem_id)
    try:
        time_limit = float(meta.get("time_limit", catalog_entry(catalog, problem_id).get("time_limit", 1.0)))
    except (TypeError, ValueError):
        time_limit = 1.0
    tests = _discover_tests(config.TESTS_DIR / str(problem_id), time_limit)
    tests.extend(
        _discover_tests(config.CUSTOM_TESTS_DIR / str(problem_id), time_limit, custom=True)
    )
    return ProblemData(
        problem_id=problem_id,
        catalog=catalog_entry(catalog, problem_id),
        meta=meta,
        tests=tests,
    )


def test_summary(problem_id: int, catalog: dict | None = None) -> dict:
    data = load_problem(problem_id, catalog)
    return {
        "total": len(data.tests),
        "samples": data.sample_count,
        "generated": data.generated_count,
        "custom": data.custom_count,
        "has_tests": data.has_tests,
    }


def has_tests(problem_id: int) -> bool:
    return bool(
        list((config.TESTS_DIR / str(problem_id)).glob("*.in"))
        or list((config.CUSTOM_TESTS_DIR / str(problem_id)).glob("*.in"))
    )


def save_custom_test(problem_id: int, input_text: str, output_text: str) -> Path:
    directory = config.CUSTOM_TESTS_DIR / str(problem_id)
    directory.mkdir(parents=True, exist_ok=True)
    index = 1
    while (directory / f"custom{index}.in").exists():
        index += 1
    config.write_text_file(directory / f"custom{index}.in", input_text)
    config.write_text_file(directory / f"custom{index}.out", output_text)
    return directory / f"custom{index}.in"


def delete_custom_tests(problem_id: int) -> None:
    directory = config.CUSTOM_TESTS_DIR / str(problem_id)
    if not directory.exists():
        return
    for path in directory.glob("*"):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass
