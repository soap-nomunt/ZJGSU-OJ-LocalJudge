import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .testdata import TestCase

config.suppress_error_dialogs()

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
COMPILE_TIMEOUT = 20.0


@dataclass
class TestResult:
    name: str
    status: str
    elapsed: float = 0.0
    input_text: str = ""
    expected_text: str = ""
    actual_text: str = ""
    stderr_text: str = ""
    exit_code: int | None = None
    message: str = ""
    kind: str = ""


@dataclass
class JudgeResult:
    verdict: str
    compile_output: str = ""
    tests: list[TestResult] = field(default_factory=list)
    elapsed: float = 0.0
    message: str = ""
    executed: int = 0
    total: int = 0


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_output(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def first_difference(expected: str, actual: str) -> str:
    expected_norm = normalize_output(expected)
    actual_norm = normalize_output(actual)
    if expected_norm == actual_norm:
        return ""
    expected_lines = expected_norm.split("\n") if expected_norm else []
    actual_lines = actual_norm.split("\n") if actual_norm else []
    limit = max(len(expected_lines), len(actual_lines))
    for index in range(limit):
        exp = expected_lines[index] if index < len(expected_lines) else "<没有这一行>"
        act = actual_lines[index] if index < len(actual_lines) else "<没有这一行>"
        if exp != act:
            exp_show = exp if len(exp) <= 80 else exp[:77] + "..."
            act_show = act if len(act) <= 80 else act[:77] + "..."
            return f"第 {index + 1} 行不同：期望 [{exp_show}]，实际 [{act_show}]"
    return "输出不一致"


def build_env(gcc_path: str) -> dict:
    env = dict(os.environ)
    gcc_dir = config.short_path(Path(gcc_path).resolve().parent)
    env["PATH"] = gcc_dir + os.pathsep + env.get("PATH", "")
    temp = config.short_path(config.TEMP_DIR)
    env["TMP"] = temp
    env["TEMP"] = temp
    env["TMPDIR"] = temp
    env["LANG"] = "C"
    return env


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.SubprocessError:
        pass


def _read_limited(path: Path) -> tuple[bytes, bool]:
    try:
        size = path.stat().st_size
    except OSError:
        return b"", False
    truncated = size > MAX_OUTPUT_BYTES
    try:
        with path.open("rb") as fp:
            data = fp.read(MAX_OUTPUT_BYTES)
    except OSError:
        data = b""
    return data, truncated


def _run_program(
    exe_path: Path,
    stdin_path: Path | None,
    workdir: Path,
    env: dict,
    timeout: float,
    output_prefix: str,
) -> dict:
    short_cwd = config.short_path(workdir)
    out_path = workdir / f"{output_prefix}_out.txt"
    err_path = workdir / f"{output_prefix}_err.txt"
    out_path.unlink(missing_ok=True)
    err_path.unlink(missing_ok=True)

    result = {
        "exit_code": None,
        "timed_out": False,
        "output_exceeded": False,
        "elapsed": 0.0,
        "stdout": b"",
        "stderr": b"",
    }

    stdin_handle = stdin_path.open("rb") if stdin_path is not None else subprocess.DEVNULL
    try:
        with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
            start = time.perf_counter()
            try:
                proc = subprocess.Popen(
                    [str(exe_path)],
                    cwd=short_cwd,
                    env=env,
                    stdin=stdin_handle,
                    stdout=out_handle,
                    stderr=err_handle,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                result["stderr"] = str(exc).encode("utf-8", errors="replace")
                result["exit_code"] = -1
                result["elapsed"] = time.perf_counter() - start
                return result

            stop_monitor = threading.Event()

            def monitor() -> None:
                while not stop_monitor.wait(0.02):
                    if proc.poll() is not None:
                        return
                    try:
                        if out_path.stat().st_size > MAX_OUTPUT_BYTES:
                            result["output_exceeded"] = True
                            _kill_process_tree(proc)
                            return
                    except OSError:
                        return

            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()
            try:
                proc.wait(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                _kill_process_tree(proc)
            finally:
                stop_monitor.set()
                monitor_thread.join(timeout=1)
            result["elapsed"] = time.perf_counter() - start
            result["exit_code"] = proc.returncode
    finally:
        if stdin_handle not in (subprocess.DEVNULL, None):
            try:
                stdin_handle.close()
            except OSError:
                pass

    data, truncated = _read_limited(out_path)
    result["stdout"] = data
    if truncated:
        result["output_exceeded"] = True
    err_data, err_truncated = _read_limited(err_path)
    result["stderr"] = err_data
    if err_truncated:
        result["output_exceeded"] = True
    out_path.unlink(missing_ok=True)
    err_path.unlink(missing_ok=True)
    return result


def compile_source(
    code: str,
    gcc_path: str,
    workdir: Path,
    static: bool = True,
) -> tuple[bool, str, Path | None]:
    source_path = workdir / "main.c"
    config.write_text_file(source_path, code)
    exe_path = workdir / "main.exe"
    exe_path.unlink(missing_ok=True)
    env = build_env(gcc_path)
    short_cwd = config.short_path(workdir)
    base_cmd = [
        config.short_path(gcc_path),
        "-std=gnu99",
        "-O2",
        "-Wall",
        "-Wextra",
        "-fdiagnostics-color=never",
    ]
    if static:
        base_cmd.append("-static")
    cmd = base_cmd + ["main.c", "-o", "main.exe", "-lm"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=short_cwd,
            env=env,
            capture_output=True,
            timeout=COMPILE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "编译超时（超过 20 秒），请检查代码或减少模板/头文件体积。", None
    except OSError as exc:
        return False, f"无法启动编译器：{exc}", None
    output = decode_bytes((proc.stdout or b"") + (proc.stderr or b"")).strip()
    if proc.returncode != 0 or not exe_path.exists():
        return False, output or "编译失败，但没有返回错误信息。", None
    return True, output, exe_path


def _status_for_failure(result: dict) -> str:
    if result["timed_out"]:
        return "TLE"
    if result["output_exceeded"]:
        return "OLE"
    if result["exit_code"] not in (0, None):
        return "RE"
    return "WA"


def run_single_test(
    exe_path: Path,
    test: TestCase,
    workdir: Path,
    env: dict,
    time_limit: float,
    grace: float,
) -> TestResult:
    raw = _run_program(
        exe_path=exe_path,
        stdin_path=test.input_path,
        workdir=workdir,
        env=env,
        timeout=time_limit + grace,
        output_prefix=f"case_{abs(hash((test.name, str(test.input_path)))) % 100000}",
    )
    input_text = ""
    expected_text = ""
    try:
        input_text = config.read_text_file(test.input_path)
    except OSError:
        pass
    try:
        expected_text = config.read_text_file(test.expected_path)
    except OSError:
        pass
    actual_text = decode_bytes(raw["stdout"])
    stderr_text = decode_bytes(raw["stderr"])
    status = "AC"
    message = ""
    if raw["timed_out"]:
        status = "TLE"
        message = f"运行超过 {time_limit + grace:.1f} 秒，已强制结束。"
    elif raw["output_exceeded"]:
        status = "OLE"
        message = "输出超过 8 MB，已强制结束。"
    elif raw["exit_code"] not in (0, None):
        status = "RE"
        message = f"程序退出码为 {raw['exit_code']}。"
    elif normalize_output(expected_text) != normalize_output(actual_text):
        status = "WA"
        message = first_difference(expected_text, actual_text)
    return TestResult(
        name=test.name,
        status=status,
        elapsed=raw["elapsed"],
        input_text=input_text,
        expected_text=expected_text,
        actual_text=actual_text,
        stderr_text=stderr_text,
        exit_code=raw["exit_code"],
        message=message,
        kind=test.kind,
    )


def judge(
    code: str,
    tests: list[TestCase],
    gcc_path: str,
    time_limit: float | None = None,
    grace: float = 0.5,
    stop_on_tle: bool = True,
) -> JudgeResult:
    if not gcc_path:
        return JudgeResult(verdict="ERROR", message="没有找到 gcc，请检查设置。")
    if not tests:
        return JudgeResult(verdict="ERROR", message="这道题还没有本地测试数据。")
    config.ensure_user_dirs()
    workdir = Path(tempfile.mkdtemp(prefix="cjudge_", dir=str(config.TEMP_DIR)))
    total_start = time.perf_counter()
    try:
        ok, compile_output, exe_path = compile_source(code, gcc_path, workdir)
        if not ok or exe_path is None:
            return JudgeResult(
                verdict="CE",
                compile_output=compile_output,
                message="编译错误",
                total=len(tests),
            )
        env = build_env(gcc_path)
        limit = float(time_limit if time_limit is not None else tests[0].time_limit)
        results: list[TestResult] = []
        executed = 0
        for test in tests:
            result = run_single_test(exe_path, test, workdir, env, limit, grace)
            results.append(result)
            executed += 1
            if result.status == "TLE" and stop_on_tle:
                break
        if executed == len(tests) and all(item.status == "AC" for item in results):
            verdict = "AC"
        elif any(item.status == "TLE" for item in results):
            verdict = "TLE"
        elif any(item.status == "RE" for item in results):
            verdict = "RE"
        elif any(item.status == "OLE" for item in results):
            verdict = "OLE"
        elif any(item.status == "WA" for item in results):
            verdict = "WA"
        else:
            verdict = "TLE"
        return JudgeResult(
            verdict=verdict,
            compile_output=compile_output,
            tests=results,
            elapsed=time.perf_counter() - total_start,
            message=f"已运行 {executed}/{len(tests)} 组测试",
            executed=executed,
            total=len(tests),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_custom(
    code: str,
    input_text: str,
    gcc_path: str,
    time_limit: float = 1.0,
    grace: float = 0.5,
) -> dict:
    if not gcc_path:
        return {
            "verdict": "ERROR",
            "compile_output": "",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "elapsed": 0.0,
            "message": "没有找到 gcc，请检查设置。",
        }
    config.ensure_user_dirs()
    workdir = Path(tempfile.mkdtemp(prefix="cjudge_custom_", dir=str(config.TEMP_DIR)))
    try:
        ok, compile_output, exe_path = compile_source(code, gcc_path, workdir)
        if not ok or exe_path is None:
            return {
                "verdict": "CE",
                "compile_output": compile_output,
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "elapsed": 0.0,
                "message": "编译错误",
            }
        input_path = workdir / "input.txt"
        config.write_text_file(input_path, input_text)
        env = build_env(gcc_path)
        raw = _run_program(
            exe_path=exe_path,
            stdin_path=input_path,
            workdir=workdir,
            env=env,
            timeout=time_limit + grace,
            output_prefix="custom",
        )
        if raw["timed_out"]:
            verdict = "TLE"
            message = f"运行超过 {time_limit + grace:.1f} 秒，已强制结束。"
        elif raw["output_exceeded"]:
            verdict = "OLE"
            message = "输出超过 8 MB，已强制结束。"
        elif raw["exit_code"] not in (0, None):
            verdict = "RE"
            message = f"程序退出码为 {raw['exit_code']}。"
        else:
            verdict = "OK"
            message = "运行结束。"
        return {
            "verdict": verdict,
            "compile_output": compile_output,
            "stdout": decode_bytes(raw["stdout"]),
            "stderr": decode_bytes(raw["stderr"]),
            "exit_code": raw["exit_code"],
            "elapsed": raw["elapsed"],
            "message": message,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def smoke_test(gcc_path: str) -> tuple[bool, str]:
    if not gcc_path:
        return False, "没有找到 gcc。"
    config.ensure_user_dirs()
    workdir = Path(tempfile.mkdtemp(prefix="cjudge_smoke_", dir=str(config.TEMP_DIR)))
    try:
        source = '#include <stdio.h>\nint main(void){printf("ok\\n");return 0;}\n'
        ok, output, exe_path = compile_source(source, gcc_path, workdir)
        if not ok or exe_path is None:
            return False, f"gcc 可以启动，但测试编译失败：\n{output}"
        env = build_env(gcc_path)
        raw = _run_program(exe_path, None, workdir, env, 3.0, "smoke")
        text = decode_bytes(raw["stdout"]).strip()
        if raw["timed_out"] or raw["exit_code"] != 0 or text != "ok":
            return False, "测试程序编译成功，但运行失败。请检查杀毒软件或临时目录权限。"
        return True, "gcc 编译、运行正常。"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def clean_old_temp_dirs(max_age_hours: float = 24.0) -> None:
    config.ensure_user_dirs()
    now = time.time()
    for path in config.TEMP_DIR.glob("cjudge_*"):
        try:
            if path.is_dir() and now - path.stat().st_mtime > max_age_hours * 3600:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
