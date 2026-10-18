# 开发与重建说明

这是判题器的开发仓库目录。最终给其他人使用的是 `dist/CJudge-v1.0.0-win64.zip`，
它包含 Python 运行时、MinGW gcc 和题库数据，目标电脑不需要安装任何环境。

## 目录

- `app.py`：Tkinter GUI 入口
- `core/`：题库、题面、测试和判题引擎
- `data/`：题库、题面、测试数据
- `tools/`：数据抓取、测试生成、测试校验脚本（不放进分发包）
- `build.ps1`：生成便携版 ZIP
- `dist/`：构建输出（已在 .gitignore 中忽略）

## 环境

- Windows 10/11 x64
- Python 3.11 x64
- PyInstaller 6.14.2
- MinGW.org GCC 6.3.0-1，默认在 `C:\MinGW`

## 重建步骤

1. 抓取题面、样例和参考代码（只需要开发时联网）：

   ```powershell
   $env:OJ_USER="你的学号"
   $env:OJ_PASSWORD="你的密码"
   cd tools
   python fetch_data.py
   ```

   只会读取 OJ 页面，不会提交代码。参考代码保存在 `data/reference/`，
   生成测试后应删除。

2. 用参考代码生成额外测试，并校验官方样例：

   ```powershell
   cd tools
   python seed_tests.py --keep-reference
   ```

   `--keep-reference` 会保留参考代码，方便下一步校验；平时可以不加这个参数。

3. 校验所有参考解能通过全部本地测试：

   ```powershell
   cd tools
   python validate_tests.py
   ```

4. 构建便携版：

   ```powershell
   cd ..
   powershell -ExecutionPolicy Bypass -File build.ps1
   ```

   输出：
   - `dist/CJudge/`：解压后的程序目录
   - `dist/CJudge-v1.0.0-win64.zip`：分发包

   `build.ps1` 会运行验证和打包后自检。如果启动时提示找不到 `C:\MinGW`，
   用 `-MinGW "实际路径"` 指定。

5. 清理临时数据（可选）：

   ```powershell
   Remove-Item -Recurse -Force data\reference, build, userdata -ErrorAction SilentlyContinue
   ```

## 添加题库

把新的 CSV 放到 `data/banks/`，每行一个题号或用逗号分隔。程序会自动识别。
没有测试数据的题仍可随机抽题，但判题按钮会提示暂无测试数据。

## 手动添加测试

在 `data/tests/<题号>/` 下放 `testN.in` 和 `testN.out`，
再在 `meta.json` 里更新 `generated_count`/`test_count`（非必需，程序会自动发现文件）。
也可以直接在程序“测试结果”页点击“添加测试”，输入和期望输出会保存到用户数据目录。

## 注意

- 不要把 `data/reference/`、`userdata/` 或个人进度放进分发包。
- 题库题面和样例来自学校 OJ，请按课程学习用途使用。
- 测试数据是用你 OJ 上 AC 的代码生成的，不是 OJ 官方隐藏测试。
