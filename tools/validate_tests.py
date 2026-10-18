import shutil
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
JUDGE_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(JUDGE_DIR))
sys.path.insert(0, str(TOOLS_DIR))

from core import config, judge, testdata  # noqa: E402
from seed_tests import compile_reference, run_reference  # noqa: E402


def main() -> None:
    gcc_path = config.find_gcc({})
    data_dir = config.BUNDLE_DATA_DIR
    catalog = testdata.load_catalog()
    problems = sorted(catalog, key=int)
    failures = []
    total = 0
    for pid_text in problems:
        pid = int(pid_text)
        problem = testdata.load_problem(pid, catalog)
        if not problem.tests:
            failures.append(f"{pid}: 没有测试")
            continue
        sources = sorted((data_dir / "reference" / pid_text).glob("*"), key=lambda p: p.stem, reverse=True)
        if not sources:
            failures.append(f"{pid}: 没有参考代码")
            continue
        workdir = Path(config.TEMP_DIR) / f"validate_{pid}"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)
        exe, output = compile_reference(sources[0], gcc_path, workdir)
        if exe is None:
            failures.append(f"{pid}: 参考代码编译失败 {output[:120]}")
            shutil.rmtree(workdir, ignore_errors=True)
            continue
        for test in problem.tests:
            total += 1
            actual = run_reference(exe, test.input_path, workdir, gcc_path)
            expected = config.read_text_file(test.expected_path)
            if actual is None:
                failures.append(f"{pid} {test.name}: 运行失败或超时")
            elif judge.normalize_output(actual) != judge.normalize_output(expected):
                failures.append(f"{pid} {test.name}: 输出不一致")
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"检查 {len(problems)} 题，{total} 组测试")
    if failures:
        print("失败：")
        for item in failures:
            print(" ", item)
        sys.exit(1)
    print("全部参考解通过所有本地测试。")


if __name__ == "__main__":
    main()
