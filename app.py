import logging
import logging.handlers
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from core import bank as bank_module
from core import config, judge, statements, testdata

VERDICT_TEXT = {
    "AC": "通过",
    "WA": "答案错误",
    "TLE": "运行超时",
    "RE": "运行错误",
    "OLE": "输出超限",
    "CE": "编译错误",
    "ERROR": "错误",
    "OK": "运行成功",
}

VERDICT_TAG = {
    "AC": "ac",
    "WA": "wa",
    "TLE": "tle",
    "RE": "re",
    "OLE": "ole",
    "CE": "ce",
    "ERROR": "ce",
    "OK": "ac",
}

STATUS_TEXT = {
    "AC": "通过",
    "WA": "答案错误",
    "TLE": "超时",
    "RE": "运行错误",
    "OLE": "输出超限",
    "CE": "编译错误",
    "SKIPPED": "未运行",
}

LOG = logging.getLogger("cjudge")


def setup_logging() -> None:
    config.ensure_user_dirs()
    log_path = config.LOGS_DIR / "app.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)


class JudgeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = config.load_settings()
        self.progress = bank_module.Progress()
        self.catalog = testdata.load_catalog()
        self.banks = bank_module.discover_banks()
        self.picker = bank_module.RandomPicker()
        self.picker_key = None
        self.current_bank: bank_module.Bank | None = None
        self.current_id: int | None = None
        self.current_problem: testdata.ProblemData | None = None
        self._code_dirty = False
        self._loading_code = False
        self._busy = False
        self._last_results: dict[str, judge.TestResult] = {}
        self._last_judge_result: judge.JudgeResult | None = None
        self._coverage_cache: dict[str, int] = {}
        self._toolchain_checked = False
        self.queue: queue.Queue = queue.Queue()
        self._search_after = None

        config.set_dpi_awareness()
        config.ensure_user_dirs()
        judge.clean_old_temp_dirs()

        self._build_ui()
        self._populate_banks()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)
        self.root.after(300, self._start_toolchain_check)
        self.root.after(500, self._first_run_hint)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.root.title(f"{config.APP_TITLE} v{config.APP_VERSION}")
        try:
            self.root.geometry(self.settings.get("window_geometry", "1280x820"))
        except tk.TclError:
            pass
        self.root.minsize(980, 640)

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure("Treeview", rowheight=24)
        style.configure("Status.TLabel", padding=(6, 3))

        self.editor_font = tkfont.Font(family="Consolas", size=int(self.settings.get("font_size", 11)))
        self.ui_font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        toolbar = ttk.Frame(self.root, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(toolbar, text="题库：").pack(side=tk.LEFT)
        self.bank_var = tk.StringVar()
        self.bank_combo = ttk.Combobox(toolbar, textvariable=self.bank_var, state="readonly", width=34)
        self.bank_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.bank_combo.bind("<<ComboboxSelected>>", self._on_bank_changed)

        self.random_button = ttk.Button(toolbar, text="随机抽题", command=self._random_pick)
        self.random_button.pack(side=tk.LEFT, padx=(0, 10))

        self.only_tests_var = tk.BooleanVar(value=bool(self.settings.get("only_with_tests", True)))
        self.skip_passed_var = tk.BooleanVar(value=bool(self.settings.get("skip_passed", False)))
        ttk.Checkbutton(
            toolbar, text="只抽有测试数据", variable=self.only_tests_var, command=self._reset_picker
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            toolbar, text="跳过已通过", variable=self.skip_passed_var, command=self._reset_picker
        ).pack(side=tk.LEFT, padx=(6, 10))

        ttk.Label(toolbar, text="搜索：").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=16)
        search_entry.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(toolbar, text="设置", command=self._open_settings).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(toolbar, text="关于", command=self._show_about).pack(side=tk.RIGHT, padx=(4, 0))

        main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        left = ttk.Frame(main_pane, padding=(6, 4))
        main_pane.add(left, weight=1)

        self.bank_stats_label = ttk.Label(left, text="", font=self.ui_font, foreground="#555555")
        self.bank_stats_label.pack(side=tk.TOP, anchor=tk.W, padx=2, pady=(0, 4))

        list_frame = ttk.Frame(left)
        list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.problem_tree = ttk.Treeview(
            list_frame, columns=("id", "title", "status"), show="headings", selectmode="browse"
        )
        self.problem_tree.heading("id", text="题号")
        self.problem_tree.heading("title", text="标题")
        self.problem_tree.heading("status", text="状态")
        self.problem_tree.column("id", width=58, anchor=tk.CENTER, stretch=False)
        self.problem_tree.column("title", width=210, anchor=tk.W)
        self.problem_tree.column("status", width=76, anchor=tk.CENTER, stretch=False)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.problem_tree.yview)
        self.problem_tree.configure(yscrollcommand=scrollbar.set)
        self.problem_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.problem_tree.tag_configure("passed", foreground="#148a3a")
        self.problem_tree.tag_configure("sample", foreground="#a86b00")
        self.problem_tree.tag_configure("attempted", foreground="#b3261e")
        self.problem_tree.tag_configure("none", foreground="#666666")
        self.problem_tree.tag_configure("untested", foreground="#999999")
        self.problem_tree.bind("<<TreeviewSelect>>", self._on_problem_selected)

        right = ttk.PanedWindow(main_pane, orient=tk.VERTICAL)
        main_pane.add(right, weight=5)

        code_frame = ttk.Frame(right, padding=(4, 2))
        right.add(code_frame, weight=3)

        code_toolbar = ttk.Frame(code_frame)
        code_toolbar.pack(side=tk.TOP, fill=tk.X)
        self.problem_label = ttk.Label(code_toolbar, text="未选择题目", font=self.ui_font)
        self.problem_label.pack(side=tk.LEFT, padx=(2, 8))
        self.judge_button = ttk.Button(code_toolbar, text="测评 (F5)", command=self._judge_current)
        self.judge_button.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(code_toolbar, text="保存", command=self._save_workspace_clicked).pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        ttk.Button(code_toolbar, text="导入代码", command=self._import_code).pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        ttk.Button(code_toolbar, text="导出代码", command=self._export_code).pack(
            side=tk.RIGHT, padx=(4, 0)
        )

        editor_frame = ttk.Frame(code_frame)
        editor_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 0))
        self.line_numbers = tk.Text(
            editor_frame,
            width=5,
            padx=4,
            pady=2,
            takefocus=0,
            borderwidth=0,
            background="#f3f3f3",
            foreground="#888888",
            state=tk.DISABLED,
            wrap=tk.NONE,
            font=self.editor_font,
        )
        self.line_numbers.grid(row=0, column=0, sticky="ns")
        self.code_text = tk.Text(
            editor_frame,
            undo=True,
            maxundo=-1,
            autoseparators=True,
            wrap=tk.NONE,
            font=self.editor_font,
            padx=6,
            pady=2,
            insertwidth=2,
        )
        self.code_text.grid(row=0, column=1, sticky="nsew")
        code_scroll = ttk.Scrollbar(editor_frame, orient=tk.VERTICAL, command=self._on_code_scroll)
        code_scroll.grid(row=0, column=2, sticky="ns")
        code_hscroll = ttk.Scrollbar(editor_frame, orient=tk.HORIZONTAL, command=self.code_text.xview)
        code_hscroll.grid(row=1, column=1, sticky="ew")
        self.code_text.configure(yscrollcommand=lambda first, last: self._on_code_yscroll(first, last, code_scroll))
        self.code_text.configure(xscrollcommand=code_hscroll.set)
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(1, weight=1)

        self.code_text.bind("<<Modified>>", self._on_code_modified)
        self.code_text.bind("<Tab>", self._on_tab)
        self.code_text.bind("<Shift-Tab>", self._on_shift_tab)
        self.code_text.bind("<Control-s>", lambda event: self._save_workspace_clicked())
        self.code_text.bind("<F5>", lambda event: self._judge_current())
        self.code_text.bind("<Control-Return>", lambda event: self._judge_current())
        self.code_text.bind("<Control-a>", self._select_all)

        bottom = ttk.Notebook(right)
        right.add(bottom, weight=2)

        statement_tab = ttk.Frame(bottom)
        bottom.add(statement_tab, text="题目")
        self.statement_text = ScrolledText(
            statement_tab, wrap=tk.WORD, font=("Microsoft YaHei UI", 10), padx=10, pady=8
        )
        self.statement_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.statement_text.configure(state=tk.DISABLED)
        self.statement_text.tag_configure("section", font=("Microsoft YaHei UI", 11, "bold"), spacing1=8)
        self.statement_text.tag_configure("title", font=("Microsoft YaHei UI", 14, "bold"), spacing3=6)
        self.statement_text.tag_configure("mono", font=("Consolas", 10), background="#f5f5f5")

        result_tab = ttk.Frame(bottom)
        bottom.add(result_tab, text="测试结果")
        result_top = ttk.Frame(result_tab)
        result_top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 2))
        self.result_summary = ttk.Label(result_top, text="尚未测评", font=self.ui_font)
        self.result_summary.pack(side=tk.LEFT)
        ttk.Button(result_top, text="添加测试", command=self._add_test_dialog).pack(side=tk.RIGHT)
        result_frame = ttk.Frame(result_tab)
        result_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.result_tree = ttk.Treeview(
            result_frame, columns=("test", "status", "time", "message"), show="headings", height=5
        )
        self.result_tree.heading("test", text="测试")
        self.result_tree.heading("status", text="结果")
        self.result_tree.heading("time", text="用时")
        self.result_tree.heading("message", text="说明")
        self.result_tree.column("test", width=80, anchor=tk.CENTER, stretch=False)
        self.result_tree.column("status", width=70, anchor=tk.CENTER, stretch=False)
        self.result_tree.column("time", width=72, anchor=tk.CENTER, stretch=False)
        self.result_tree.column("message", width=380, anchor=tk.W)
        result_scroll = ttk.Scrollbar(result_frame, orient=tk.VERTICAL, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=result_scroll.set)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        result_scroll.grid(row=0, column=1, sticky="ns")
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)
        self.result_tree.tag_configure("ac", foreground="#148a3a")
        self.result_tree.tag_configure("wa", foreground="#b3261e")
        self.result_tree.tag_configure("tle", foreground="#b35c00")
        self.result_tree.tag_configure("re", foreground="#7b1fa2")
        self.result_tree.tag_configure("ole", foreground="#7b1fa2")
        self.result_tree.tag_configure("ce", foreground="#555555")
        self.result_tree.bind("<<TreeviewSelect>>", self._on_result_selected)

        detail_frame = ttk.Frame(result_tab)
        detail_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))
        self.result_detail = ScrolledText(
            detail_frame, height=7, wrap=tk.WORD, font=("Microsoft YaHei UI", 9)
        )
        self.result_detail.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.result_detail.configure(state=tk.DISABLED)
        self.result_detail.tag_configure("header", font=("Microsoft YaHei UI", 10, "bold"))
        self.result_detail.tag_configure("mono", font=("Consolas", 9), background="#f5f5f5")
        self.result_detail.tag_configure("bad", foreground="#b3261e")
        self.result_detail.tag_configure("good", foreground="#148a3a")
        self.result_detail.tag_configure("muted", foreground="#777777")

        custom_tab = ttk.Frame(bottom)
        bottom.add(custom_tab, text="自定义输入")
        custom_top = ttk.Frame(custom_tab)
        custom_top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Label(custom_top, text="输入（stdin）：", font=self.ui_font).pack(side=tk.LEFT)
        self.custom_run_button = ttk.Button(custom_top, text="运行当前代码", command=self._run_custom)
        self.custom_run_button.pack(side=tk.RIGHT)
        custom_pane = ttk.PanedWindow(custom_tab, orient=tk.VERTICAL)
        custom_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.custom_input = ScrolledText(custom_pane, height=5, wrap=tk.NONE, font=("Consolas", 10))
        custom_pane.add(self.custom_input, weight=1)
        self.custom_output = ScrolledText(custom_pane, height=8, wrap=tk.WORD, font=("Consolas", 10))
        self.custom_output.configure(state=tk.DISABLED)
        custom_pane.add(self.custom_output, weight=2)

        compile_tab = ttk.Frame(bottom)
        bottom.add(compile_tab, text="编译信息")
        self.compile_text = ScrolledText(
            compile_tab, wrap=tk.WORD, font=("Consolas", 9), padx=8, pady=6
        )
        self.compile_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.compile_text.configure(state=tk.DISABLED)
        self.compile_text.tag_configure("bad", foreground="#b3261e")

        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Frame(self.root)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = ttk.Label(
            status_bar, textvariable=self.status_var, style="Status.TLabel", anchor=tk.W
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.gcc_label = ttk.Label(status_bar, text="", style="Status.TLabel", anchor=tk.E)
        self.gcc_label.pack(side=tk.RIGHT)

        self._update_problem_label()
        self._set_statement("")

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _first_run_hint(self) -> None:
        if self.settings.get("first_run_done"):
            return
        self.settings["first_run_done"] = True
        config.save_settings(self.settings)
        messagebox.showinfo(
            "欢迎使用",
            "这是离线版 C 语言判题器。\n\n"
            "1. 选择题库，点击“随机抽题”。\n"
            "2. 在编辑器中写代码，点击“测评 (F5)”。\n"
            "3. 结果会显示每组测试的通过情况和第一处不同。\n\n"
            "判题完全在本机运行，不需要联网。",
            parent=self.root,
        )

    def _start_toolchain_check(self) -> None:
        gcc_path = config.find_gcc(self.settings)

        def worker() -> None:
            version = config.gcc_version(gcc_path)
            ok, message = judge.smoke_test(gcc_path) if gcc_path else (False, "没有找到 gcc。")
            self.queue.put(("toolchain", gcc_path, version, ok, message))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "toolchain":
                    self._on_toolchain_checked(*message[1:])
                elif kind == "judge":
                    self._on_judge_finished(*message[1:])
                elif kind == "custom":
                    self._on_custom_finished(*message[1:])
                elif kind == "error":
                    self._set_busy(False)
                    messagebox.showerror("出错", str(message[1]), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_toolchain_checked(self, gcc_path: str, version: str, ok: bool, message: str) -> None:
        self._toolchain_checked = True
        if gcc_path:
            self.gcc_label.configure(text=f"gcc: {version}")
            LOG.info("toolchain gcc=%s version=%s ok=%s", gcc_path, version, ok)
        else:
            self.gcc_label.configure(text="gcc: 未找到")
            LOG.warning("gcc not found")
        self._set_status(message if ok else f"编译器自检失败：{message}")
        if not ok:
            messagebox.showwarning(
                "编译器自检失败",
                f"{message}\n\n"
                "请检查“设置”里的 gcc 路径，或确认杀毒软件没有拦截临时目录。",
                parent=self.root,
            )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.judge_button.configure(state=state)
        self.custom_run_button.configure(state=state)

    # --------------------------------------------------------------- banks
    def _populate_banks(self) -> None:
        if not self.banks:
            self.bank_combo.configure(values=[])
            self._set_status("没有找到题库 CSV，请把题库放到 data/banks 目录。")
            return
        labels = [self._bank_label(bank) for bank in self.banks]
        self.bank_combo.configure(values=labels)
        index = 0
        last = str(self.settings.get("last_bank", ""))
        for i, bank in enumerate(self.banks):
            if bank.name == last:
                index = i
                break
        self.bank_combo.current(index)
        self._activate_bank(self.banks[index])

    def _bank_label(self, bank: bank_module.Bank) -> str:
        coverage = self._coverage_cache.get(bank.name)
        if coverage is None:
            coverage = sum(1 for pid in bank.ids if testdata.has_tests(pid))
            self._coverage_cache[bank.name] = coverage
        if coverage:
            return f"{bank.name}（{bank.count} 题，可判 {coverage} 题）"
        return f"{bank.name}（{bank.count} 题，暂无测试数据）"

    def _on_bank_changed(self, _event=None) -> None:
        index = self.bank_combo.current()
        if 0 <= index < len(self.banks):
            self._activate_bank(self.banks[index])

    def _activate_bank(self, bank: bank_module.Bank) -> None:
        if self.current_id is not None and self._code_dirty:
            self._save_workspace(silent=True)
        self.current_bank = bank
        self.settings["last_bank"] = bank.name
        config.save_settings(self.settings)
        self._populate_problem_tree()
        self._reset_picker()
        self._set_status(f"已选择题库：{bank.name}（{bank.count} 题）")

    def _on_search_changed(self) -> None:
        if self._search_after is not None:
            try:
                self.root.after_cancel(self._search_after)
            except tk.TclError:
                pass
        self._search_after = self.root.after(160, self._populate_problem_tree)

    def _populate_problem_tree(self) -> None:
        if self.current_bank is None:
            return
        self._search_after = None
        query = self.search_var.get().strip().lower()
        selected = str(self.current_id) if self.current_id is not None else None
        children = self.problem_tree.get_children()
        if children:
            self.problem_tree.delete(*children)
        for pid in self.current_bank.ids:
            entry = testdata.catalog_entry(self.catalog, pid)
            title = str(entry.get("title", "")).strip() or ("暂无题面" if not entry else "")
            if query:
                haystack = f"{pid} {title}".lower()
                if query not in haystack:
                    continue
            status, tag = self._problem_status(pid)
            iid = str(pid)
            self.problem_tree.insert("", tk.END, iid=iid, values=(pid, title, status), tags=(tag,))
            if selected == iid:
                self.problem_tree.selection_set(iid)
                self.problem_tree.see(iid)
        total = len(self.current_bank.ids)
        shown = len(self.problem_tree.get_children())
        coverage = self._coverage_cache.get(self.current_bank.name, 0)
        self.bank_stats_label.configure(
            text=f"共 {total} 题，可判 {coverage} 题，当前显示 {shown} 题"
        )

    def _problem_status(self, pid: int) -> tuple[str, str]:
        if not testdata.has_tests(pid):
            return "仅抽题", "untested"
        if self.progress.is_passed(pid):
            return "通过", "passed"
        if self.progress.is_sample_passed(pid):
            return "样例通过", "sample"
        if self.progress.attempts(pid) > 0:
            return "尝试过", "attempted"
        return "未做", "none"

    def _refresh_problem_row(self, pid: int) -> None:
        iid = str(pid)
        if not self.problem_tree.exists(iid):
            return
        status, tag = self._problem_status(pid)
        values = list(self.problem_tree.item(iid, "values"))
        if len(values) >= 3:
            values[2] = status
        self.problem_tree.item(iid, values=values, tags=(tag,))

    def _eligible_ids(self) -> list[int]:
        if self.current_bank is None:
            return []
        ids = list(self.current_bank.ids)
        if self.only_tests_var.get():
            ids = [pid for pid in ids if testdata.has_tests(pid)]
        if self.skip_passed_var.get():
            ids = [pid for pid in ids if not self.progress.is_passed(pid)]
        return ids

    def _reset_picker(self) -> None:
        self.picker_key = None
        self.picker.set_eligible(self._eligible_ids())
        if self.current_bank is not None:
            self._coverage_cache.pop(self.current_bank.name, None)
            self._populate_bank_label_only()

    def _populate_bank_label_only(self) -> None:
        if self.current_bank is None:
            return
        label = self._bank_label(self.current_bank)
        values = list(self.bank_combo.cget("values"))
        index = self.bank_combo.current()
        if 0 <= index < len(values):
            values[index] = label
            self.bank_combo.configure(values=values)
            self.bank_combo.set(label)
        self.bank_stats_label.configure(
            text=f"共 {self.current_bank.count} 题，可判 "
            f"{self._coverage_cache.get(self.current_bank.name, 0)} 题"
        )

    def _random_pick(self) -> None:
        if self.current_bank is None:
            messagebox.showwarning("提示", "请先选择题库。", parent=self.root)
            return
        key = (self.current_bank.name, self.only_tests_var.get(), self.skip_passed_var.get())
        if key != self.picker_key:
            self.picker_key = key
            self.picker.set_eligible(self._eligible_ids())
        if not self.picker.eligible:
            if self.only_tests_var.get():
                messagebox.showinfo(
                    "没有可抽的题",
                    "当前筛选条件下没有可判题的题目。\n"
                    "可以取消“只抽有测试数据”，或换一个题库。",
                    parent=self.root,
                )
            else:
                messagebox.showinfo("没有可抽的题", "当前筛选条件下没有题目。", parent=self.root)
            return
        pid = self.picker.next()
        if pid is None:
            return
        iid = str(pid)
        if not self.problem_tree.exists(iid):
            self.search_var.set("")
            self._populate_problem_tree()
        if self.problem_tree.exists(iid):
            self.problem_tree.selection_set(iid)
            self.problem_tree.focus(iid)
            self.problem_tree.see(iid)
        self._set_status(f"抽中 {pid} 号题，本轮还剩 {self.picker.remaining()} 题")

    # ------------------------------------------------------------ problems
    def _on_problem_selected(self, _event=None) -> None:
        selection = self.problem_tree.selection()
        if not selection:
            return
        try:
            pid = int(selection[0])
        except ValueError:
            return
        if pid == self.current_id:
            return
        if self._busy:
            if self.current_id is not None and self.problem_tree.exists(str(self.current_id)):
                self.problem_tree.selection_set(str(self.current_id))
            self._set_status("正在判题，请稍候再切换题目。")
            return
        if self.current_id is not None and self._code_dirty:
            self._save_workspace(silent=True)
        self._load_problem(pid)

    def _load_problem(self, pid: int) -> None:
        self.current_id = pid
        self.current_problem = testdata.load_problem(pid, self.catalog)
        entry = testdata.catalog_entry(self.catalog, pid)
        title = str(entry.get("title", "")).strip() or f"{pid} 号题"
        self._set_statement(statements.load_statement(pid, title))
        code = self._load_code_for(pid)
        self._set_code(code)
        self._clear_results()
        self._update_problem_label()
        problem = self.current_problem
        sample_count = problem.sample_count
        generated_count = problem.generated_count
        custom_count = problem.custom_count
        if problem.has_tests:
            self._set_status(
                f"{pid} 号题：样例 {sample_count} 组，额外 {generated_count} 组，"
                f"自定义 {custom_count} 组"
            )
        else:
            self._set_status(f"{pid} 号题：暂无本地测试数据，可先写代码或使用自定义输入。")
        LOG.info("load problem id=%s tests=%s", pid, len(problem.tests))

    def _load_code_for(self, pid: int) -> str:
        workspace_path = config.WORKSPACE_DIR / f"{pid}.c"
        if workspace_path.exists():
            try:
                return config.read_text_file(workspace_path)
            except OSError:
                pass
        external = self._external_code_dir()
        if external is not None:
            candidate = external / f"{pid}.c"
            if candidate.exists():
                try:
                    return config.read_text_file(candidate)
                except OSError:
                    pass
        return ""

    def _external_code_dir(self) -> Path | None:
        configured = str(self.settings.get("external_code_dir", "")).strip()
        if configured:
            path = Path(configured)
            if path.is_dir():
                return path
            return None
        if not config.IS_FROZEN:
            candidate = config.APP_DIR.parent.parent / "problems"
            if candidate.is_dir():
                return candidate
        return None

    def _update_problem_label(self) -> None:
        if self.current_id is None:
            self.problem_label.configure(text="未选择题目")
            return
        title = self.current_problem.title if self.current_problem else f"{self.current_id} 号题"
        self.problem_label.configure(text=f"{self.current_id}  {title}")

    def _set_statement(self, text: str) -> None:
        self.statement_text.configure(state=tk.NORMAL)
        self.statement_text.delete("1.0", tk.END)
        self.statement_text.insert("1.0", text)
        for line_index, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if line_index == 1:
                self.statement_text.tag_add("title", f"{line_index}.0", f"{line_index}.end")
            elif stripped.startswith("【") or stripped.startswith("##"):
                self.statement_text.tag_add("section", f"{line_index}.0", f"{line_index}.end")
            elif stripped in ("题目描述", "输入", "输出", "样例输入", "样例输出", "提示", "来源"):
                self.statement_text.tag_add("section", f"{line_index}.0", f"{line_index}.end")
        self.statement_text.configure(state=tk.DISABLED)
        self.statement_text.yview_moveto(0)

    def _set_code(self, text: str) -> None:
        self._loading_code = True
        self.code_text.delete("1.0", tk.END)
        if text:
            self.code_text.insert("1.0", text)
        self.code_text.edit_reset()
        self.code_text.edit_modified(False)
        self._loading_code = False
        self._code_dirty = False
        self._update_line_numbers()
        self.code_text.yview_moveto(0)
        self.code_text.xview_moveto(0)

    def _on_code_modified(self, _event=None) -> None:
        if not self.code_text.edit_modified():
            return
        if not self._loading_code:
            self._code_dirty = True
        self._update_line_numbers()
        self.code_text.edit_modified(False)

    def _update_line_numbers(self) -> None:
        try:
            end_index = self.code_text.index("end-1c")
            line_count = int(end_index.split(".")[0])
        except (tk.TclError, ValueError):
            line_count = 1
        numbers = "\n".join(str(i) for i in range(1, line_count + 1))
        self.line_numbers.configure(state=tk.NORMAL)
        self.line_numbers.delete("1.0", tk.END)
        self.line_numbers.insert("1.0", numbers)
        self.line_numbers.configure(state=tk.DISABLED)
        self.line_numbers.yview_moveto(self.code_text.yview()[0])

    def _on_code_yscroll(self, first, last, scrollbar) -> None:
        scrollbar.set(first, last)
        self.line_numbers.yview_moveto(first)

    def _on_code_scroll(self, *args) -> None:
        self.code_text.yview(*args)
        self.line_numbers.yview(*args)

    def _on_tab(self, event):
        self.code_text.insert(tk.INSERT, "    ")
        return "break"

    def _on_shift_tab(self, _event):
        index = self.code_text.index(tk.INSERT)
        line_start = self.code_text.index(f"{index} linestart")
        prefix = self.code_text.get(line_start, index)
        remove = min(4, len(prefix) - len(prefix.lstrip(" ")))
        if remove > 0:
            self.code_text.delete(f"{line_start}+{len(prefix) - remove}c", index)
        return "break"

    def _select_all(self, _event):
        self.code_text.tag_add(tk.SEL, "1.0", tk.END)
        self.code_text.mark_set(tk.INSERT, "1.0")
        return "break"

    def _save_workspace(self, silent: bool = False) -> None:
        if self.current_id is None:
            return
        try:
            path = config.WORKSPACE_DIR / f"{self.current_id}.c"
            config.write_text_file(path, self.code_text.get("1.0", "end-1c"))
            self._code_dirty = False
            if not silent:
                self._set_status(f"已保存到 {path}")
        except OSError as exc:
            if not silent:
                messagebox.showerror("保存失败", str(exc), parent=self.root)

    def _save_workspace_clicked(self) -> None:
        self._save_workspace(silent=False)

    def _import_code(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="导入 C 代码",
            filetypes=[("C 源文件", "*.c"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            text = config.read_text_file(path)
        except OSError as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        self._set_code(text)
        self._code_dirty = True
        self._set_status(f"已导入 {path}（保存后写入 workspace）")

    def _export_code(self) -> None:
        if self.current_id is None:
            messagebox.showinfo("提示", "请先选择题目。", parent=self.root)
            return
        default_name = f"{self.current_id}.c"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出 C 代码",
            initialfile=default_name,
            defaultextension=".c",
            filetypes=[("C 源文件", "*.c"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            config.write_text_file(path, self.code_text.get("1.0", "end-1c"))
            self._set_status(f"已导出到 {path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    # ---------------------------------------------------------------- judge
    def _judge_current(self) -> None:
        if self._busy:
            return
        if self.current_id is None or self.current_problem is None:
            messagebox.showinfo("提示", "请先随机抽题或从左侧选择一道题。", parent=self.root)
            return
        problem = self.current_problem
        if not problem.tests:
            messagebox.showinfo(
                "暂无测试数据",
                "这道题还没有本地测试数据。\n"
                "可以在“自定义输入”里运行程序，或先给这道题添加测试数据。",
                parent=self.root,
            )
            return
        code = self.code_text.get("1.0", "end-1c")
        if not code.strip():
            messagebox.showinfo("提示", "编辑器中还没有代码。", parent=self.root)
            return
        gcc_path = config.find_gcc(self.settings)
        if not gcc_path:
            messagebox.showerror(
                "找不到编译器", "没有找到 gcc，请在“设置”中指定 gcc.exe 的路径。", parent=self.root
            )
            return
        self._save_workspace(silent=True)
        self._clear_results()
        self._set_busy(True)
        self._set_status(f"正在编译并测评 {self.current_id} 号题……")
        pid = self.current_id
        tests = list(problem.tests)
        limit = problem.time_limit
        grace = float(self.settings.get("time_grace", 0.5))
        LOG.info("judge start id=%s tests=%s gcc=%s", pid, len(tests), gcc_path)

        def worker() -> None:
            try:
                result = judge.judge(code, tests, gcc_path, time_limit=limit, grace=grace)
                self.queue.put(("judge", pid, result))
            except Exception as exc:
                LOG.exception("judge worker failed")
                self.queue.put(("error", f"判题过程出错：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_judge_finished(self, pid: int, result: judge.JudgeResult) -> None:
        self._set_busy(False)
        non_sample = any(test.kind != "sample" for test in result.tests)
        local_passed = result.verdict == "AC" and non_sample
        sample_only_passed = result.verdict == "AC" and not non_sample
        self.progress.update(
            pid,
            result.verdict,
            local_passed=local_passed,
            sample_only_passed=sample_only_passed,
            elapsed=result.elapsed,
        )
        self._refresh_problem_row(pid)
        if self.current_id != pid:
            self._set_status(f"{pid} 号题测评完成：{VERDICT_TEXT.get(result.verdict, result.verdict)}")
            return
        self._last_judge_result = result
        self._display_judge_result(result)
        self._set_status(
            f"{pid} 号题：{VERDICT_TEXT.get(result.verdict, result.verdict)}，"
            f"用时 {result.elapsed:.2f} 秒"
        )
        LOG.info("judge done id=%s verdict=%s executed=%s/%s", pid, result.verdict, result.executed, result.total)
        if self.skip_passed_var.get() and self.progress.is_passed(pid):
            self._reset_picker()

    def _display_judge_result(self, result: judge.JudgeResult) -> None:
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)
        self._last_results = {}
        if result.verdict == "CE":
            self.result_summary.configure(text="编译错误", foreground="#b3261e")
            self.compile_text.configure(state=tk.NORMAL)
            self.compile_text.delete("1.0", tk.END)
            self.compile_text.insert("1.0", result.compile_output or "编译失败")
            self.compile_text.configure(state=tk.DISABLED)
            self._show_detail_text("编译错误\n\n" + (result.compile_output or ""), bad=True)
            return

        verdict_text = VERDICT_TEXT.get(result.verdict, result.verdict)
        if result.verdict == "AC":
            if result.tests and all(test.kind == "sample" for test in result.tests):
                summary = f"样例通过（仅 {len(result.tests)} 组样例，未完整验证）"
                color = "#a86b00"
            else:
                summary = f"本地通过（{len(result.tests)}/{len(result.tests)} 组测试）"
                color = "#148a3a"
        else:
            summary = (
                f"{verdict_text}（已运行 {result.executed}/{result.total} 组，"
                f"总用时 {result.elapsed:.2f} 秒）"
            )
            color = "#b3261e"
        self.result_summary.configure(text=summary, foreground=color)

        for index, test in enumerate(result.tests, start=1):
            status_text = STATUS_TEXT.get(test.status, test.status)
            self.result_tree.insert(
                "",
                tk.END,
                iid=f"test_{index}",
                values=(test.name, status_text, f"{test.elapsed:.3f}s", test.message),
                tags=(VERDICT_TAG.get(test.status, "ce"),),
            )
            self._last_results[f"test_{index}"] = test
        children = self.result_tree.get_children()
        if children:
            first_bad = None
            for iid in children:
                test = self._last_results.get(iid)
                if test is not None and test.status != "AC":
                    first_bad = iid
                    break
            target = first_bad or children[0]
            self.result_tree.selection_set(target)
            self.result_tree.focus(target)
            self.result_tree.see(target)
            self._show_test_detail(self._last_results.get(target))
        self.compile_text.configure(state=tk.NORMAL)
        self.compile_text.delete("1.0", tk.END)
        self.compile_text.insert("1.0", result.compile_output or "编译成功（没有警告）。")
        self.compile_text.configure(state=tk.DISABLED)

    def _on_result_selected(self, _event=None) -> None:
        selection = self.result_tree.selection()
        if not selection:
            return
        test = self._last_results.get(selection[0])
        if test is not None:
            self._show_test_detail(test)

    def _show_test_detail(self, test: judge.TestResult | None) -> None:
        if test is None:
            self._show_detail_text("")
            return
        lines = [
            f"{test.name}：{STATUS_TEXT.get(test.status, test.status)}",
            f"用时：{test.elapsed:.3f} 秒    {test.message}",
        ]
        if test.kind:
            lines.append(f"类型：{test.kind}")
        lines.extend(["", "【输入】", test.input_text.rstrip("\n")])
        lines.extend(["", "【期望输出】", test.expected_text.rstrip("\n")])
        lines.extend(["", "【实际输出】", test.actual_text.rstrip("\n")])
        if test.stderr_text.strip():
            lines.extend(["", "【stderr】", test.stderr_text.rstrip("\n")])
        if test.exit_code is not None:
            lines.append(f"\n退出码：{test.exit_code}")
        self._show_detail_text("\n".join(lines), bad=test.status != "AC")

    def _show_detail_text(self, text: str, bad: bool = False) -> None:
        self.result_detail.configure(state=tk.NORMAL)
        self.result_detail.delete("1.0", tk.END)
        self.result_detail.insert("1.0", text)
        if bad:
            self.result_detail.tag_add("bad", "1.0", "1.end")
        self.result_detail.configure(state=tk.DISABLED)

    def _clear_results(self) -> None:
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)
        self._last_results = {}
        self.result_summary.configure(text="尚未测评", foreground="#555555")
        self._show_detail_text("")
        self.compile_text.configure(state=tk.NORMAL)
        self.compile_text.delete("1.0", tk.END)
        self.compile_text.configure(state=tk.DISABLED)

    def _add_test_dialog(self) -> None:
        if self.current_id is None or self.current_problem is None:
            messagebox.showinfo("提示", "请先选择一道题。", parent=self.root)
            return
        pid = self.current_id
        window = tk.Toplevel(self.root)
        window.title(f"为 {pid} 号题添加测试")
        window.transient(self.root)
        window.grab_set()
        window.geometry("760x560")

        frame = ttk.Frame(window, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="输入（stdin）：").pack(anchor=tk.W)
        input_text = ScrolledText(frame, height=8, wrap=tk.NONE, font=("Consolas", 10))
        input_text.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        ttk.Label(frame, text="期望输出（stdout）：").pack(anchor=tk.W)
        output_text = ScrolledText(frame, height=10, wrap=tk.WORD, font=("Consolas", 10))
        output_text.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        ttk.Label(
            frame,
            text="测试会保存到用户数据目录，不会修改程序自带的题库文件。",
            foreground="#666666",
        ).pack(anchor=tk.W)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(8, 0))

        def save() -> None:
            input_value = input_text.get("1.0", "end-1c")
            output_value = output_text.get("1.0", "end-1c")
            if not input_value and not output_value:
                messagebox.showwarning("提示", "输入和期望输出都是空的。", parent=window)
                return
            try:
                testdata.save_custom_test(pid, input_value, output_value)
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc), parent=window)
                return
            if self.current_id is not None and self._code_dirty:
                self._save_workspace(silent=True)
            self.current_problem = testdata.load_problem(pid, self.catalog)
            if self.current_bank is not None:
                self._coverage_cache.pop(self.current_bank.name, None)
                self._populate_bank_label_only()
            self._refresh_problem_row(pid)
            self._clear_results()
            self._set_status(
                f"已为 {pid} 号题添加自定义测试，共 {len(self.current_problem.tests)} 组。"
            )
            window.destroy()

        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="保存", command=save).pack(side=tk.RIGHT, padx=(0, 6))

    def _run_custom(self) -> None:
        if self._busy:
            return
        if self.current_id is None or self.current_problem is None:
            messagebox.showinfo("提示", "请先选择题目。", parent=self.root)
            return
        code = self.code_text.get("1.0", "end-1c")
        if not code.strip():
            messagebox.showinfo("提示", "编辑器中还没有代码。", parent=self.root)
            return
        gcc_path = config.find_gcc(self.settings)
        if not gcc_path:
            messagebox.showerror("找不到编译器", "请在“设置”中指定 gcc.exe 的路径。", parent=self.root)
            return
        self._save_workspace(silent=True)
        input_text = self.custom_input.get("1.0", "end-1c")
        self._set_busy(True)
        self._set_status("正在编译并运行自定义输入……")
        pid = self.current_id
        limit = self.current_problem.time_limit
        grace = float(self.settings.get("time_grace", 0.5))

        def worker() -> None:
            try:
                result = judge.run_custom(code, input_text, gcc_path, time_limit=limit, grace=grace)
                self.queue.put(("custom", pid, result))
            except Exception as exc:
                LOG.exception("custom runner failed")
                self.queue.put(("error", f"运行过程出错：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_custom_finished(self, pid: int, result: dict) -> None:
        self._set_busy(False)
        if self.current_id != pid:
            self._set_status("自定义运行完成。")
            return
        verdict = str(result.get("verdict", "ERROR"))
        text = []
        text.append(f"状态：{VERDICT_TEXT.get(verdict, verdict)}    {result.get('message', '')}")
        if result.get("elapsed"):
            text.append(f"用时：{float(result['elapsed']):.3f} 秒")
        if result.get("exit_code") is not None:
            text.append(f"退出码：{result['exit_code']}")
        if result.get("compile_output", "").strip():
            text.extend(["", "【编译信息】", result["compile_output"].rstrip()])
        text.extend(["", "【stdout】", str(result.get("stdout", "")).rstrip("\n")])
        if str(result.get("stderr", "")).strip():
            text.extend(["", "【stderr】", str(result["stderr"]).rstrip("\n")])
        self.custom_output.configure(state=tk.NORMAL)
        self.custom_output.delete("1.0", tk.END)
        self.custom_output.insert("1.0", "\n".join(text))
        self.custom_output.configure(state=tk.DISABLED)
        self._set_status(f"自定义运行：{VERDICT_TEXT.get(verdict, verdict)}")

    # ------------------------------------------------------------- settings
    def _open_settings(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("设置")
        window.transient(self.root)
        window.grab_set()
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)

        gcc_var = tk.StringVar(value=str(self.settings.get("gcc_path", "")))
        external_var = tk.StringVar(value=str(self.settings.get("external_code_dir", "")))
        font_var = tk.StringVar(value=str(self.settings.get("font_size", 11)))
        grace_var = tk.StringVar(value=str(self.settings.get("time_grace", 0.5)))

        row = 0
        ttk.Label(frame, text="gcc 路径：").grid(row=row, column=0, sticky=tk.W, pady=4)
        gcc_entry = ttk.Entry(frame, textvariable=gcc_var, width=52)
        gcc_entry.grid(row=row, column=1, sticky="ew", pady=4)

        def browse_gcc() -> None:
            path = filedialog.askopenfilename(
                parent=window,
                title="选择 gcc.exe",
                filetypes=[("gcc", "gcc.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
            )
            if path:
                gcc_var.set(path)

        def auto_gcc() -> None:
            found = config.find_gcc({"gcc_path": ""})
            gcc_var.set(found)

        ttk.Button(frame, text="浏览", command=browse_gcc).grid(row=row, column=2, padx=(4, 0))
        row += 1
        ttk.Button(frame, text="自动检测", command=auto_gcc).grid(row=row, column=1, sticky=tk.W, pady=(0, 6))

        ttk.Label(frame, text="外部代码目录：").grid(row=row + 1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=external_var, width=52).grid(
            row=row + 1, column=1, sticky="ew", pady=4
        )

        def browse_external() -> None:
            path = filedialog.askdirectory(parent=window, title="选择代码目录")
            if path:
                external_var.set(path)

        ttk.Button(frame, text="浏览", command=browse_external).grid(
            row=row + 1, column=2, padx=(4, 0)
        )
        row += 2

        ttk.Label(frame, text="编辑器字号：").grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(frame, from_=8, to=24, textvariable=font_var, width=8).grid(
            row=row, column=1, sticky=tk.W, pady=4
        )
        row += 1
        ttk.Label(frame, text="超时宽限（秒）：").grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(
            frame, from_=0.0, to=3.0, increment=0.1, textvariable=grace_var, width=8
        ).grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1
        ttk.Label(
            frame,
            text="比较规则：忽略行末空格、CRLF 差异和文件末尾空行（与 OJ 类似）。",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(8, 4))
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        def reset_progress() -> None:
            if messagebox.askyesno("清空进度", "确定要清空所有做题进度吗？", parent=window):
                self.progress.reset()
                self._populate_problem_tree()
                self._set_status("做题进度已清空。")

        def check_gcc() -> None:
            path = gcc_var.get().strip() or config.find_gcc({"gcc_path": ""})
            if not path:
                messagebox.showerror("编译器检测", "没有找到 gcc。", parent=window)
                return
            version = config.gcc_version(path)
            ok, message = judge.smoke_test(path)
            if ok:
                messagebox.showinfo(
                    "编译器检测", f"{version}\n\n{message}", parent=window
                )
            else:
                messagebox.showerror(
                    "编译器检测", f"{version}\n\n{message}", parent=window
                )

        def save() -> None:
            try:
                font_size = int(font_var.get())
                grace = float(grace_var.get())
            except ValueError:
                messagebox.showerror("设置错误", "字号或超时宽限不是有效数字。", parent=window)
                return
            font_size = max(8, min(24, font_size))
            grace = max(0.0, min(3.0, grace))
            self.settings["gcc_path"] = gcc_var.get().strip()
            self.settings["external_code_dir"] = external_var.get().strip()
            self.settings["font_size"] = font_size
            self.settings["time_grace"] = grace
            config.save_settings(self.settings)
            self.editor_font.configure(size=font_size)
            self._update_line_numbers()
            self._toolchain_checked = False
            self._start_toolchain_check()
            window.destroy()
            self._set_status("设置已保存。")

        ttk.Button(buttons, text="清空进度", command=reset_progress).pack(side=tk.LEFT)
        ttk.Button(buttons, text="检测 gcc", command=check_gcc).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="打开用户数据目录", command=lambda: self._open_path(config.USER_DATA_DIR)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(buttons, text="打开日志目录", command=lambda: self._open_path(config.LOGS_DIR)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="保存", command=save).pack(side=tk.RIGHT, padx=(0, 6))

        frame.columnconfigure(1, weight=1)
        window.update_idletasks()
        window.geometry(f"+{self.root.winfo_rootx() + 120}+{self.root.winfo_rooty() + 100}")

    def _show_about(self) -> None:
        version = config.load_data_version()
        gcc_path = config.find_gcc(self.settings)
        gcc = config.gcc_version(gcc_path) if gcc_path else "未找到"
        data_text = (
            f"题库数据：{version.get('generated_at', '未知')}\n"
            f"题目数量：{version.get('problems', '未知')}\n"
            f"测试组数：{version.get('tests', '未知')}\n"
            f"数据生成编译器：{version.get('compiler', '未知')}\n"
        )
        messagebox.showinfo(
            f"关于 {config.APP_TITLE}",
            f"{config.APP_TITLE} v{config.APP_VERSION}\n\n"
            f"{data_text}\n"
            f"当前 gcc：{gcc}\n\n"
            f"程序目录：{config.APP_DIR}\n"
            f"用户数据：{config.USER_DATA_DIR}\n\n"
            "本地判题结果只代表本地测试通过，不等于 OJ AC。\n"
            "题库题面与样例来自学校 OJ，请勿公开传播。",
            parent=self.root,
        )

    def _open_path(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    # ---------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno("正在运行", "判题还在进行中，确定要退出吗？", parent=self.root):
                return
        if self._code_dirty:
            self._save_workspace(silent=True)
        try:
            self.settings["window_geometry"] = self.root.geometry()
            self.settings["only_with_tests"] = bool(self.only_tests_var.get())
            self.settings["skip_passed"] = bool(self.skip_passed_var.get())
            config.save_settings(self.settings)
        except tk.TclError:
            pass
        LOG.info("application closing")
        self.root.destroy()


def run_selftest() -> int:
    report = []
    ok = True

    def add(text: str) -> None:
        report.append(text)

    config.ensure_user_dirs()
    add(f"CJudge {config.APP_VERSION}")
    add(f"frozen={config.IS_FROZEN}")
    add(f"app_dir={config.APP_DIR}")
    add(f"user_data={config.USER_DATA_DIR}")
    add(f"data_dir={config.BUNDLE_DATA_DIR}")

    gcc_path = config.find_gcc({})
    add(f"gcc_path={gcc_path}")
    version = config.gcc_version(gcc_path)
    add(f"gcc_version={version}")
    if gcc_path:
        smoke_ok, smoke_message = judge.smoke_test(gcc_path)
    else:
        smoke_ok, smoke_message = False, "没有找到 gcc"
    add(f"gcc_smoke={smoke_ok} {smoke_message}")
    ok = ok and smoke_ok

    catalog = testdata.load_catalog()
    add(f"catalog_problems={len(catalog)}")
    banks = bank_module.discover_banks()
    add("banks=" + ", ".join(f"{bank.name}:{bank.count}" for bank in banks))
    total_tests = 0
    missing_tests = []
    for key in catalog:
        loaded = testdata.load_problem(int(key), catalog)
        total_tests += len(loaded.tests)
        if not loaded.tests:
            missing_tests.append(key)
    add(f"tests_total={total_tests} missing={','.join(missing_tests) or 'none'}")
    ok = ok and bool(catalog) and bool(banks) and not missing_tests

    problem = testdata.load_problem(1000, catalog)
    add(f"problem_1000_tests={len(problem.tests)}")
    code = (
        "#include <stdio.h>\n"
        "int main(void){int a,b;while(scanf(\"%d %d\",&a,&b)==2)"
        "printf(\"%d\\n\",a+b);return 0;}\n"
    )
    if problem.tests and gcc_path:
        result = judge.judge(
            code,
            problem.tests,
            gcc_path,
            time_limit=problem.time_limit,
            grace=0.5,
        )
        add(f"judge_1000={result.verdict} executed={result.executed}/{result.total}")
        ok = ok and result.verdict == "AC"
    else:
        add("judge_1000=SKIP")
        ok = False

    add(f"result={'OK' if ok else 'FAIL'}")
    text = "\n".join(report) + "\n"
    try:
        (config.USER_DATA_DIR / "selftest.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            sys.stdout.write(text)
        except (OSError, ValueError):
            pass
    return 0 if ok else 1


def main() -> None:
    setup_logging()
    LOG.info("application start version=%s frozen=%s", config.APP_VERSION, config.IS_FROZEN)
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    config.set_dpi_awareness()
    root = tk.Tk()

    def report_callback_exception(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        LOG.error("tk callback exception:\n%s", text)
        messagebox.showerror("程序出错", f"{exc_value}\n\n详细信息已写入日志。", parent=root)

    root.report_callback_exception = report_callback_exception
    try:
        JudgeApp(root)
        root.mainloop()
    except Exception as exc:
        LOG.exception("fatal error")
        messagebox.showerror("程序无法启动", f"{exc}\n\n详细信息已写入日志。")
        raise


if __name__ == "__main__":
    main()
