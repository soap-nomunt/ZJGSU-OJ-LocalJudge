from . import config


def statement_path(problem_id: int):
    custom = config.CUSTOM_STATEMENTS_DIR / f"{problem_id}.txt"
    if custom.exists():
        return custom
    bundled = config.STATEMENTS_DIR / f"{problem_id}.txt"
    if bundled.exists():
        return bundled
    return None


def load_statement(problem_id: int, title: str = "") -> str:
    path = statement_path(problem_id)
    if path is not None:
        try:
            return config.read_text_file(path)
        except OSError:
            pass
    heading = f"{problem_id}"
    if title:
        heading += f"：{title}"
    return (
        f"{heading}\n\n"
        "本地题库中暂未缓存这道题的题面。\n\n"
        "你仍然可以编写代码；如果本题已有测试数据，可以点击“测评”查看结果。\n"
        "如果本题没有测试数据，可以在“自定义输入”里手动运行程序。\n"
    )
