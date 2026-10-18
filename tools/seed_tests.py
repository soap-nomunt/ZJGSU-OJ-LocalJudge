import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
JUDGE_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(JUDGE_DIR))

from core import config, judge  # noqa: E402
from test_inputs import EXTRA_INPUTS  # noqa: E402


def compile_reference(source_path: Path, gcc_path: str, workdir: Path) -> tuple[Path | None, str]:
    is_cpp = source_path.suffix.lower() in (".cpp", ".cc", ".cxx", ".c++")
    compiler = gcc_path
    if is_cpp:
        gxx = Path(gcc_path).with_name("g++.exe")
        if gxx.exists():
            compiler = str(gxx)
    target_name = "main.cpp" if is_cpp else "main.c"
    target = workdir / target_name
    shutil.copyfile(source_path, target)
    exe = workdir / "main.exe"
    exe.unlink(missing_ok=True)
    cmd = [
        compiler,
        "-std=gnu++14" if is_cpp else "-std=gnu99",
        "-O2",
        "-Wall",
        "-Wextra",
        "-fdiagnostics-color=never",
        "-static",
        target_name,
        "-o",
        "main.exe",
        "-lm",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=config.short_path(workdir),
            env=judge.build_env(compiler),
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    output = judge.decode_bytes((proc.stdout or b"") + (proc.stderr or b"")).strip()
    if proc.returncode != 0 or not exe.exists():
        return None, output or "编译失败"
    return exe, output


def run_reference(exe: Path, input_path: Path, workdir: Path, gcc_path: str, timeout: float = 6.0) -> str | None:
    raw = judge._run_program(
        exe_path=exe,
        stdin_path=input_path,
        workdir=workdir,
        env=judge.build_env(gcc_path),
        timeout=timeout,
        output_prefix="ref",
    )
    if raw["timed_out"] or raw["output_exceeded"] or raw["exit_code"] != 0:
        return None
    return judge.decode_bytes(raw["stdout"])


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="用 AC 参考代码为月考题目生成额外测试")
    parser.add_argument("--gcc", default="", help="gcc 路径，默认自动查找")
    parser.add_argument("--only", default="", help="只处理指定题号，逗号分隔")
    parser.add_argument("--keep-reference", action="store_true", help="生成后保留参考代码")
    parser.add_argument("--skip-cross-check", action="store_true", help="只使用第一份参考代码")
    args = parser.parse_args()

    gcc_path = args.gcc or config.find_gcc({})
    if not gcc_path or not Path(gcc_path).exists():
        print("找不到 gcc，请用 --gcc 指定。")
        sys.exit(1)
    print(f"使用编译器：{gcc_path}")
    print(config.gcc_version(gcc_path))

    data_dir = config.BUNDLE_DATA_DIR
    catalog_path = data_dir / "catalog.json"
    catalog_data = load_json(catalog_path)
    catalog = catalog_data.get("problems", {})
    if not catalog:
        print("catalog.json 不存在或为空。")
        sys.exit(1)

    only = {int(item) for item in args.only.replace(",", " ").split() if item.isdigit()}
    problem_ids = sorted((int(key) for key in catalog if not only or int(key) in only))
    temp_root = Path(tempfile.mkdtemp(prefix="cjudge_seed_", dir=str(config.TEMP_DIR)))
    started = time.time()
    total_samples = 0
    total_extra = 0
    skipped_inputs = 0
    failed_problems = []

    try:
        for index, pid in enumerate(problem_ids, start=1):
            entry = catalog[str(pid)]
            reference_dir = data_dir / "reference" / str(pid)
            sources = sorted(reference_dir.glob("*"), key=lambda p: p.stem, reverse=True)
            tests_dir = data_dir / "tests" / str(pid)
            tests_dir.mkdir(parents=True, exist_ok=True)
            samples = sorted(tests_dir.glob("sample*.in"))
            total_samples += len(samples)

            valid_refs: list[tuple[Path, Path, str]] = []
            for source in sources:
                workdir = Path(tempfile.mkdtemp(prefix=f"ref_{pid}_", dir=str(temp_root)))
                exe, output = compile_reference(source, gcc_path, workdir)
                if exe is None:
                    print(f"[{pid}] 参考 {source.name} 编译失败：{output[:160]}")
                    shutil.rmtree(workdir, ignore_errors=True)
                    continue
                sample_ok = True
                for sample in samples:
                    expected_path = sample.with_suffix(".out")
                    if not expected_path.exists():
                        continue
                    expected = config.read_text_file(expected_path)
                    actual = run_reference(exe, sample, workdir, gcc_path)
                    if actual is None or judge.normalize_output(actual) != judge.normalize_output(expected):
                        sample_ok = False
                        print(f"[{pid}] 参考 {source.stem} 未通过样例 {sample.stem}，跳过")
                        break
                if sample_ok:
                    valid_refs.append((source, exe, str(workdir)))
                else:
                    shutil.rmtree(workdir, ignore_errors=True)

            if not valid_refs:
                print(f"[{pid}] 没有可用参考解，保留样例，跳过额外测试")
                failed_problems.append(pid)
                continue

            for old in tests_dir.glob("test*.in"):
                old.unlink(missing_ok=True)
            for old in tests_dir.glob("test*.out"):
                old.unlink(missing_ok=True)

            inputs = EXTRA_INPUTS.get(pid, [])
            saved = 0
            for input_index, input_text in enumerate(inputs, start=1):
                input_path = tests_dir / f"__seed_{input_index}.in"
                config.write_text_file(input_path, input_text)
                outputs: list[str] = []
                valid = True
                for _, exe, workdir_text in valid_refs[: (1 if args.skip_cross_check else 3)]:
                    workdir = Path(workdir_text)
                    actual = run_reference(exe, input_path, workdir, gcc_path)
                    if actual is None:
                        valid = False
                        break
                    outputs.append(actual)
                if not valid or not outputs:
                    skipped_inputs += 1
                    input_path.unlink(missing_ok=True)
                    continue
                normalized = {judge.normalize_output(item) for item in outputs}
                if len(normalized) != 1:
                    print(f"[{pid}] 第 {input_index} 组输入参考解输出不一致，跳过")
                    skipped_inputs += 1
                    input_path.unlink(missing_ok=True)
                    continue
                output_text = outputs[0]
                if output_text and not output_text.endswith("\n"):
                    output_text += "\n"
                final_in = tests_dir / f"test{saved + 1}.in"
                final_out = tests_dir / f"test{saved + 1}.out"
                input_path.replace(final_in)
                config.write_text_file(final_out, output_text)
                saved += 1
            total_extra += saved

            meta_path = tests_dir / "meta.json"
            meta = load_json(meta_path)
            meta.update(
                {
                    "id": pid,
                    "title": entry.get("title", ""),
                    "time_limit": entry.get("time_limit", 1.0),
                    "memory_limit": entry.get("memory_limit", 128),
                    "special_judge": bool(entry.get("special_judge", False)),
                    "sample_count": len(samples),
                    "generated_count": saved,
                    "test_count": len(samples) + saved,
                    "reference_sids": [int(source.stem) for source, _, _ in valid_refs],
                    "seed_compiler": config.gcc_version(gcc_path),
                }
            )
            save_json(meta_path, meta)
            entry["sample_count"] = len(samples)
            entry["generated_count"] = saved
            entry["test_count"] = len(samples) + saved
            entry["seed_compiler"] = config.gcc_version(gcc_path)
            print(f"[{index}/{len(problem_ids)}] {pid} 样例 {len(samples)}，额外 {saved}/{len(inputs)}")
            for _, _, workdir_text in valid_refs:
                shutil.rmtree(Path(workdir_text), ignore_errors=True)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    catalog_data["problems"] = catalog
    catalog_data["seeded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_json(catalog_path, catalog_data)
    version = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "problems": len(catalog),
        "samples": total_samples,
        "extra_tests": total_extra,
        "tests": total_samples + total_extra,
        "compiler": config.gcc_version(gcc_path),
    }
    save_json(data_dir / "data_version.json", version)

    if not args.keep_reference:
        reference_root = data_dir / "reference"
        for path in reference_root.glob("*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        try:
            reference_root.rmdir()
        except OSError:
            pass

    print("=" * 60)
    print(f"完成：{len(problem_ids)} 题，样例 {total_samples}，额外 {total_extra}，共 {total_samples + total_extra} 组")
    print(f"跳过输入 {skipped_inputs} 组，耗时 {time.time() - started:.1f} 秒")
    if failed_problems:
        print("没有可用参考解的题目：" + ", ".join(str(item) for item in failed_problems))


if __name__ == "__main__":
    main()
