import argparse
import getpass
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = "http://10.21.11.101/JudgeOnline/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CJudgeDataFetcher/1.0"


def decode_html(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


TAG_RE = re.compile(r"(?is)</?[a-zA-Z][^>]*>")


def clean_text(value: str) -> str:
    value = re.sub(r"(?is)<(?:br|/p|/div|/tr|/li|/h\d)\s*/?>", "\n", value)
    value = TAG_RE.sub("", value)
    value = html.unescape(value).replace("\xa0", " ")
    lines = []
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line.startswith("document."):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_sample(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = TAG_RE.sub("", value)
    value = html.unescape(value).replace("\xa0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = value.split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    return "\n".join(lines) + "\n"


class OJClient:
    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self.cookies = urllib.request.HTTPCookieProcessor()
        self.opener = urllib.request.build_opener(self.cookies)
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def get(self, path: str) -> str:
        url = BASE_URL + path
        with self.opener.open(url, timeout=20) as response:
            return decode_html(response.read())

    def login(self) -> None:
        data = urllib.parse.urlencode(
            {"user_id": self.user, "password": self.password, "submit": "Submit"}
        ).encode("utf-8")
        request = urllib.request.Request(
            BASE_URL + "login.php",
            data=data,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(request, timeout=20) as response:
            decode_html(response.read())
        probe = self.get(f"status.php?user_id={urllib.parse.quote(self.user)}")
        if "修改帐号" not in probe and "注销" not in probe:
            raise RuntimeError("OJ 登录失败，请检查账号密码。")
        print(f"已登录 OJ：{self.user}")


def parse_contest_ids(markdown_path: Path) -> list[int]:
    if markdown_path.exists():
        text = markdown_path.read_text(encoding="utf-8", errors="replace")
        cids = []
        for match in re.finditer(r"\(cid=(\d+)\)", text):
            value = int(match.group(1))
            if value not in cids:
                cids.append(value)
        if cids:
            return cids
    return [5234, 5235, 5250, 5251, 5268, 5269, 5270, 5271]


def fetch_contest_problems(client: OJClient, cid: int) -> list[dict]:
    page = client.get(f"contest.php?cid={cid}")
    pattern = re.compile(
        r"<td>\s*(\d+)\s+Problem\s+[A-Z]\s*<td>\s*<a\s+href='problem\.php\?cid=(\d+)&pid=(\d+)'>(.*?)</a>",
        re.I | re.S,
    )
    problems = []
    for match in pattern.finditer(page):
        pid = int(match.group(1))
        title = clean_text(match.group(4)).replace("\n", " ").strip()
        problems.append({"id": pid, "cid": int(match.group(2)), "pid": int(match.group(3)), "title": title})
    return problems


def split_sections(page: str) -> dict[str, str]:
    parts = re.split(r"(?is)<h2>\s*(.*?)\s*</h2>", page)
    sections: dict[str, str] = {}
    for index in range(1, len(parts) - 1, 2):
        heading = clean_text(parts[index]).replace("\n", " ").strip()
        content = parts[index + 1]
        content = re.split(r"(?is)<center>\s*\[", content)[0]
        content = content.split("<div id=foot>")[0]
        sections.setdefault(heading, content)
    return sections


def get_section(sections: dict[str, str], *names: str) -> str:
    for name in names:
        for heading, content in sections.items():
            if heading == name or heading.startswith(name):
                return content
    return ""


def extract_sample_values(content: str) -> list[str]:
    spans = re.findall(
        r"(?is)<span[^>]*class=['\"]?sampledata['\"]?[^>]*>(.*?)</span>", content
    )
    if spans:
        return [clean_sample(item) for item in spans]
    pres = re.findall(r"(?is)<pre[^>]*>(.*?)</pre>", content)
    if pres:
        return [clean_sample(item) for item in pres]
    text = clean_text(content)
    return [text + "\n"] if text else []


def parse_problem_page(page: str, problem: dict) -> dict:
    sections = split_sections(page)
    description = clean_text(get_section(sections, "题目描述", "Description"))
    input_text = clean_text(get_section(sections, "输入", "Input"))
    output_text = clean_text(get_section(sections, "输出", "Output"))
    hint = clean_text(get_section(sections, "提示", "Hint"))
    source = clean_text(get_section(sections, "来源", "Source"))
    if "Where are the input and the output" in hint or len(hint) > 3000:
        hint = ""
    if not description and not input_text and not output_text:
        fallback = clean_text(page)
        description = fallback[:4000]
    sample_inputs = extract_sample_values(get_section(sections, "样例输入", "Sample Input"))
    sample_outputs = extract_sample_values(get_section(sections, "样例输出", "Sample Output"))

    time_limit = 1.0
    match = re.search(r"时间限制[\s\S]{0,120}?(\d+(?:\.\d+)?)\s*Sec", page)
    if not match:
        match = re.search(r"Time Limit[\s\S]{0,120}?(\d+(?:\.\d+)?)\s*Sec", page, re.I)
    if match:
        time_limit = float(match.group(1))
    memory_limit = 128
    match = re.search(r"内存限制[\s\S]{0,120}?(\d+)\s*MB", page)
    if not match:
        match = re.search(r"Memory Limit[\s\S]{0,120}?(\d+)\s*MB", page, re.I)
    if match:
        memory_limit = int(match.group(1))
    special_judge = bool(re.search(r"special\s*judge|spj", page, re.I))

    problem = dict(problem)
    problem.update(
        {
            "description": description,
            "input": input_text,
            "output": output_text,
            "hint": hint,
            "source_text": source,
            "sample_inputs": sample_inputs,
            "sample_outputs": sample_outputs,
            "time_limit": time_limit,
            "memory_limit": memory_limit,
            "special_judge": special_judge,
        }
    )
    return problem


def statement_text(problem: dict) -> str:
    lines = [
        f"{problem['id']}: {problem['title']}",
        f"时间限制: {problem['time_limit']:g} Sec    内存限制: {problem['memory_limit']} MB",
        f"来源: {problem.get('source_text') or 'ZJGSU OJ'}",
        f"OJ 页面: {BASE_URL}problem.php?cid={problem['cid']}&pid={problem['pid']}",
        "",
    ]
    for label, key in (
        ("题目描述", "description"),
        ("输入", "input"),
        ("输出", "output"),
    ):
        value = problem.get(key, "").strip()
        if value:
            lines.extend([f"【{label}】", value, ""])
    inputs = problem.get("sample_inputs", [])
    outputs = problem.get("sample_outputs", [])
    for index in range(max(len(inputs), len(outputs))):
        if index < len(inputs):
            lines.extend([f"【样例输入 {index + 1}】" if len(inputs) > 1 else "【样例输入】", inputs[index].rstrip(), ""])
        if index < len(outputs):
            lines.extend([f"【样例输出 {index + 1}】" if len(outputs) > 1 else "【样例输出】", outputs[index].rstrip(), ""])
    hint = problem.get("hint", "").strip()
    if hint:
        lines.extend(["【提示】", hint, ""])
    return "\n".join(lines).rstrip() + "\n"


def fetch_ac_rows(client: OJClient, user: str, pid: int) -> list[dict]:
    rows: list[dict] = []
    for language_filter in ("&language=0", ""):
        page = client.get(
            f"status.php?user_id={urllib.parse.quote(user)}&problem_id={pid}&jresult=4{language_filter}"
        )
        for match in re.finditer(r"(?is)<tr align=center[^>]*>(.*?)</tr>", page):
            raw = match.group(1)
            cells = re.split(r"(?i)<td[^>]*>", raw)[1:]
            cleaned = [clean_text(cell).replace("\n", " ").strip() for cell in cells]
            if not cleaned:
                continue
            sid_match = re.search(r"(\d+)", cleaned[0])
            if not sid_match:
                continue
            language = cleaned[6] if len(cleaned) > 6 else ""
            rows.append({"sid": int(sid_match.group(1)), "language": language})
        if rows:
            break
    rows.sort(key=lambda item: item["sid"], reverse=True)
    return rows[:3]


def fetch_source(client: OJClient, sid: int) -> tuple[str, str] | None:
    page = client.get(f"showsource.php?id={sid}")
    match = re.search(r"(?is)<pre[^>]*class=['\"]brush:([a-zA-Z0-9+#]+);?['\"][^>]*>(.*?)</pre>", page)
    if not match:
        match = re.search(r"(?is)<pre[^>]*>(.*?)</pre>", page)
        if not match:
            return None
        brush = ""
        source = html.unescape(match.group(1))
    else:
        brush = match.group(1).lower()
        source = html.unescape(match.group(2))
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    extension = ".cpp" if brush in ("cpp", "c++", "cplusplus") else ".c"
    return source, extension


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    judge_dir = script_dir.parent
    default_markdown = script_dir.parents[2] / "第一次月考" / "List.md"
    parser = argparse.ArgumentParser(description="抓取月考题目、样例和 AC 参考代码")
    parser.add_argument("--user", default=os.environ.get("OJ_USER", ""))
    parser.add_argument("--list-md", default=str(default_markdown))
    parser.add_argument("--output", default=str(judge_dir / "data"))
    parser.add_argument("--only", default="", help="只抓指定题号，逗号分隔")
    parser.add_argument("--skip-source", action="store_true")
    parser.add_argument("--contests", default="", help="逗号分隔的 contest id，默认从 List.md 读取")
    args = parser.parse_args()

    user = args.user or input("OJ 学号：").strip()
    password = os.environ.get("OJ_PASSWORD") or getpass.getpass("OJ 密码：")
    if not user or not password:
        print("需要 OJ 账号密码。")
        sys.exit(1)

    output_dir = Path(args.output)
    statements_dir = output_dir / "statements"
    tests_dir = output_dir / "tests"
    reference_dir = output_dir / "reference"
    for directory in (statements_dir, tests_dir, reference_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if args.contests.strip():
        cids = [int(item) for item in re.findall(r"\d+", args.contests)]
    else:
        cids = parse_contest_ids(Path(args.list_md))
    only = {int(item) for item in re.findall(r"\d+", args.only)}

    client = OJClient(user, password)
    client.login()

    problems: list[dict] = []
    contest_map: dict[str, dict] = {}
    for cid in cids:
        print(f"读取 contest {cid} ...")
        entries = fetch_contest_problems(client, cid)
        for entry in entries:
            if only and entry["id"] not in only:
                continue
            if any(item["id"] == entry["id"] for item in problems):
                continue
            problems.append(entry)
            contest_map[str(entry["id"])] = {"cid": entry["cid"], "pid": entry["pid"]}
        time.sleep(0.05)

    print(f"共 {len(problems)} 道题")
    catalog = {}
    for index, problem in enumerate(problems, start=1):
        pid = problem["id"]
        print(f"[{index}/{len(problems)}] 抓取 {pid} {problem['title']} ...")
        try:
            page = client.get(f"problem.php?cid={problem['cid']}&pid={problem['pid']}")
            detail = parse_problem_page(page, problem)
        except Exception as exc:
            print(f"  题面失败：{exc}")
            detail = dict(problem)
            detail.update(
                {
                    "description": "",
                    "input": "",
                    "output": "",
                    "hint": "",
                    "source_text": "",
                    "sample_inputs": [],
                    "sample_outputs": [],
                    "time_limit": 1.0,
                    "memory_limit": 128,
                    "special_judge": False,
                }
            )
        statement_path = statements_dir / f"{pid}.txt"
        statement_path.write_text(statement_text(detail), encoding="utf-8", newline="\n")
        test_dir = tests_dir / str(pid)
        test_dir.mkdir(parents=True, exist_ok=True)
        for old in test_dir.glob("sample*"):
            old.unlink(missing_ok=True)
        sample_inputs = detail.get("sample_inputs", [])
        sample_outputs = detail.get("sample_outputs", [])
        for sample_index in range(min(len(sample_inputs), len(sample_outputs))):
            name = f"sample{sample_index + 1}"
            (test_dir / f"{name}.in").write_text(sample_inputs[sample_index], encoding="utf-8", newline="\n")
            (test_dir / f"{name}.out").write_text(sample_outputs[sample_index], encoding="utf-8", newline="\n")
        meta = {
            "id": pid,
            "title": detail.get("title", ""),
            "time_limit": detail.get("time_limit", 1.0),
            "memory_limit": detail.get("memory_limit", 128),
            "special_judge": bool(detail.get("special_judge")),
            "sample_count": min(len(sample_inputs), len(sample_outputs)),
            "reference_sids": [],
        }
        ac_rows = []
        if not args.skip_source:
            try:
                ac_rows = fetch_ac_rows(client, user, pid)
            except Exception as exc:
                print(f"  查询 AC 提交失败：{exc}")
            reference_problem_dir = reference_dir / str(pid)
            reference_problem_dir.mkdir(parents=True, exist_ok=True)
            for row in ac_rows:
                try:
                    fetched = fetch_source(client, row["sid"])
                except Exception as exc:
                    print(f"  读取提交 {row['sid']} 失败：{exc}")
                    continue
                if not fetched:
                    continue
                source, extension = fetched
                if row.get("language", "").lower().startswith("c++"):
                    extension = ".cpp"
                (reference_problem_dir / f"{row['sid']}{extension}").write_text(
                    source, encoding="utf-8", newline="\n"
                )
                meta["reference_sids"].append(row["sid"])
                print(f"  参考提交 {row['sid']} ({row.get('language', '?')})")
        meta["sample_count"] = min(len(sample_inputs), len(sample_outputs))
        (test_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        catalog[str(pid)] = {
            "title": detail.get("title", ""),
            "cid": problem["cid"],
            "pid": problem["pid"],
            "time_limit": detail.get("time_limit", 1.0),
            "memory_limit": detail.get("memory_limit", 128),
            "special_judge": bool(detail.get("special_judge")),
            "sample_count": min(len(sample_inputs), len(sample_outputs)),
            "reference_sids": meta["reference_sids"],
            "statement": f"statements/{pid}.txt",
        }
        time.sleep(0.05)

    catalog_data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "problems": catalog,
    }
    (output_dir / "catalog.json").write_text(
        json.dumps(catalog_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "contest_map.json").write_text(
        json.dumps(contest_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    version = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "problems": len(catalog),
        "tests": 0,
        "compiler": "",
    }
    (output_dir / "data_version.json").write_text(
        json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"完成：{len(catalog)} 道题，数据目录 {output_dir}")


if __name__ == "__main__":
    main()
