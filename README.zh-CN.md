# LazyScholar · 懒人学者

Windows 悬浮侧边栏 + 本地 llama.cpp 模型 + 论文阅读器。把 arXiv 论文加入队列，生成摘要导读，再用保留数学结构的原文页阅读、划线和局部擦除。

**本地推理 · 不用 Ollama · 不用付费模型 API · 不依赖 Obsidian**

[English](README.md) · [Windows 构建](https://github.com/lood31/lazyscholar/actions/workflows/build.yml) · [Releases](https://github.com/lood31/lazyscholar/releases)

## 能做什么

- 接收 arXiv 链接／编号，或文件名带 arXiv 编号的本地 PDF。
- 通过本地 llama.cpp 和 Gemma／GGUF 模型生成精读导读，支持立即处理和定时处理。
- 自动获取并缓存 arXiv HTML，正文重新排版、插图内嵌；原始 MathML 由本机 WebView2 排版，不让模型猜公式。
- 调整字号，横向滚动超长公式，点击公式或插图放大。
- 鼠标拖选文字新增划线；切换擦除模式后，只取消选中的划线部分。
- 随时打开完整原版 PDF 对照。
- 自选保存目录，不自动搬动旧文件。

## Windows 快速上手

### 1. 准备程序和本地模型

可以下载已有的 Windows Release，或打开对应提交的**成功** `build-windows` Actions 运行记录，下载 `LazyScholar-windows` 构建产物。解压后运行 `LazyScholar.exe`；Actions 产物中的程序位于 `dist/`。

EXE **不需要安装 Python，也不依赖 Obsidian**，但需要：

- Windows 和 Microsoft Edge **WebView2 Runtime**，用于桌面界面及原生 MathML 公式显示。
- 自行准备本地 llama.cpp 和兼容的 **GGUF 模型文件**。本机用过 Gemma 3 4B Q4；速度与内存占用取决于硬件和模型大小。

**注意启动器兼容性：**目前应用按 `llama serve -m <模型> -c <上下文> --port <端口>` 启动服务。「模型可执行文件」应选择支持该命令的 `llama` 启动器；不支持 `serve` 子命令的独立可执行文件不能直接替代。

推送 `main` 会触发 Actions 构建；只有版本标签才触发 Release 发布。源码推送不等于已经发布了新的 Release，需等待对应构建成功。

### 2. 首次配置

1. 双击程序，点击悬浮球打开侧边栏。
2. 在「设置／模型」填写 llama 可执行文件路径和 GGUF 模型路径，保存设置。
3. 检查本地模型端口和上下文长度，默认分别为 **8080**、**16384**。
4. 可在设置里选择保存目录。**退出并重启后生效，旧目录里的文件不会自动搬走。**

可以在「模型」页手动开关服务。若本批处理由应用启动模型，处理结束后会自动停止该进程；已经在运行的外部服务不会被当作应用自己启动的进程关闭。

### 3. 添加论文并生成导读

在「队列」中添加：

- arXiv 链接，例如 `https://arxiv.org/abs/2610.30130`；也可直接输入 `2610.30130`。
- 文件名含 arXiv 编号的本地 PDF，例如 `2610.30130.pdf`。

选择立即处理，或设置每天的处理时间。定时处理要求应用保持运行。失败时先查看队列上的错误，修正原因后重试，不必反复重复添加同一篇论文。

完成后到「笔记」打开论文，可切换「原文」「导读」「原版 PDF」。

## 原文阅读、划线和擦除

### 阅读原文

「原文」页会自动获取 arXiv HTML 并在本地缓存，**不用每篇手动保存网页**。`A− / A+` 调整字号，继续阅读时加载后续部分；长公式可以横向滚动，公式和插图可以点击放大。

没有 HTML 版本或下载失败时，会显示错误和重试入口。此时可打开「原版 PDF」查看完整原文，不会偷偷用碎裂的 PDF 公式切图代替正常数学排版。

### 新增划线

1. 打开「原文」或「原版 PDF」。
2. 点击「划线模式」。
3. 鼠标拖选普通正文文字，松开后保存黄色标记。
4. 再次点击退出划线模式，恢复普通文字选择和复制。

### 只擦除选中的部分

1. 点击「**擦除划线**」进入擦除模式。**按下按钮本身不会删除任何标注。**
2. 拖选需要取消的黄色划线部分。
3. 只擦选中范围与已有划线的交集：选中中间就保留两端，其他划线和未划线文字都不变。
4. 再次点击「退出擦除」恢复普通选择。划线和擦除模式互斥。

原文和 PDF 对照均支持局部擦除，保存后重开仍保留结果；HTML 文字标注也能在调整字号后保留。数学公式、图表图片和导读页暂不作为原文文字划线锚点。

旧 PDF 文字标注只有在原句能唯一、精确匹配时才迁移到 HTML。原始标注文件会保留，不能匹配的原句可在阅读器查看，不会默默贴到另一句话上。

## 保存位置、网络与隐私

- 笔记和阅读数据存放在所选目录，常见子目录包括 `papers/`、`concepts/`、`papers_pdf/`、`html_reader/`、`reader/`、`highlights/`；应用配置和队列状态也保存在该目录。
- 修改保存目录必须**退出重启**；不会自动迁移、删除或合并旧目录。
- 显式 `--vault` 优先；`LAZYSCHOLAR_VAULT` 也会覆盖 GUI 保存的选择，应用会显示覆盖提示。
- 模型推理在本地。arXiv 元数据及未缓存的 HTML／插图／PDF 仍需联网；已缓存的原文不需要每次重新下载。
- 可选「HTML／插图代理」默认留空、沿用系统网络。需要显式代理时再填写，如 `http://127.0.0.1:<你的端口>`。该项只影响 HTML／插图，不影响本地模型，也不改变系统代理或 TUN 设置；不支持在 URL 中填写代理账号密码。
- 下载的论文脚本和样式不会嵌入应用的特权页面，公式保留 MathML 结构并在本地排版。

## 使用边界

- **模型导读基于 arXiv 摘要，不等于模型精读了整篇 PDF。**全文阅读是独立的原文保真功能。
- 部分论文没有 arXiv HTML 版本，请使用完整原版 PDF。
- 不自动做数学 OCR，也不让模型「修补」或改写公式。
- 桌面队列目前需要 arXiv 编号，不是通用的任意 PDF 导入工具。
- 双向链接建议和每日回放不在本轮交付范围内。

## 从源码运行

已验证环境为 **Windows + Python 3.13**。在 PowerShell 中执行：

```powershell
git clone https://github.com/lood31/lazyscholar.git
cd lazyscholar
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe lazyscholar.py --app
```

显式指定保存目录：

```powershell
.\.venv\Scripts\python.exe lazyscholar.py --app --vault "D:\MyScholarVault"
```

只运行 HTTP 界面、不轮询收件箱：

```powershell
.\.venv\Scripts\python.exe lazyscholar.py --ui --vault "D:\MyScholarVault"
```

然后打开 `http://127.0.0.1:8765/app`。原生托盘和文件／目录选择对话框需要桌面模式。旧命令行收件箱轮询参数可通过 `--help` 查看，与桌面队列不是同一条入口。

### 构建 EXE

在仓库根目录，安装运行依赖后执行：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --windowed --onefile --name LazyScholar --icon lazyscholar.ico --collect-all webview --collect-all pystray --collect-all pdfplumber --collect-all pdfminer --collect-all pypdfium2 --collect-all bleach --collect-all bs4 --collect-all defusedxml --add-data "vendor;vendor" --add-data "lazyscholar.ico;." lazyscholar.py
```

输出为 `dist/LazyScholar.exe`。EXE 包含所需 Python 依赖，但不包含 GGUF 模型和系统 WebView2 运行时。旧的 `build-app.cmd`／`deploy-v2.ps1` 带有维护者电脑上的固定路径；其他机器请使用上述通用命令。

## 常见问题

| 情况 | 检查方法 |
| --- | --- |
| 模型无法启动 | 检查启动器／模型路径、`llama serve` 是否可用、端口占用，以及保存目录中的 `model-server.log`。 |
| 论文处理失败 | 检查 arXiv 编号、网络和队列错误提示后重试；本地 PDF 文件名需带编号。 |
| HTML 获取失败 | 重试，检查可选 HTML 代理，或打开原版 PDF；并非每篇论文都有 HTML。 |
| 更换目录后仍看到旧目录 | 退出重启，检查是否被 `--vault`／`LAZYSCHOLAR_VAULT` 覆盖；旧文件不会自动搬动。 |
| 选中文字时总在新增或擦除标记 | 先退出「划线模式／擦除模式」，再普通拖选复制。 |
| 界面或公式不正常显示 | 检查 WebView2 Runtime 是否安装并保持更新。 |

## 开发与测试

以下命令使用安装依赖的同一个 Python 环境；若未激活虚拟环境，将 `python` 替换为 `.\.venv\Scripts\python.exe`。

离线后端与提取器测试：

```powershell
python -B scripts/test_app_backend.py
python -B scripts/test_paper_reader.py
python -B scripts/test_paper_html.py
```

浏览器回归另外需要 Playwright 和 Chromium：

```powershell
python -m pip install playwright
python -m playwright install chromium
python -B scripts/test_reader_browser.py
python -B scripts/test_highlight_eraser.py
```

Windows 真实鼠标验收使用隔离测试目录，会短暂移动并恢复鼠标。请让测试窗口保持前台，暂时停止手动操作：

```powershell
python -B scripts/test_native_highlight.py --exe dist/LazyScholar.exe --focus-wait 60
```

后端／浏览器回归、原生 MathML、真实 Windows 鼠标划线和局部擦除均已在本机验证。浏览器脚本创建选区不能冒充原生输入验收；QA 输出、私人笔记、模型权重和交接快照不随源码上传。

## 项目与许可证

为 DEV Hacktoberfest 2026 第三周 **Slow and Steady** 而作：做一个小而私密的学习助手，而不是云端知识平台。

应用代码使用 [MIT 许可证](LICENSE)。llama.cpp、模型权重和捆绑的第三方组件分别遵循其各自许可证。
