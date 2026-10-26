# LazyScholar · 懒人学者

Windows 悬浮侧边栏 + 本地 llama.cpp 模型 + 论文阅读器。把 arXiv 论文加入队列后即可阅读、划线和局部擦除；全文文字导读由本地模型独立生成，不再是阅读的前置条件。

**本地推理 · 不用 Ollama · 不用付费模型 API · 不依赖 Obsidian**

[English](README.md) · [Windows 构建](https://github.com/lood31/lazyscholar/actions/workflows/build.yml) · [Releases](https://github.com/lood31/lazyscholar/releases)

## 能做什么

- 接收 arXiv 链接／编号、文件名带 arXiv 编号的本地 PDF，以及保存的 HTML／MHTML 网页。
- 桌面队列逐段处理全部可提取文字、核对原文引用，再分层汇总导读；支持立即处理和定时处理，不再只读摘要。
- 自动获取并缓存 arXiv HTML，正文重新排版、插图内嵌；原始 MathML 由本机 WebView2 排版，不让模型猜公式。
- 调整字号，横向滚动超长公式，点击公式或插图放大。
- 鼠标拖选文字新增划线；切换擦除模式后，只取消选中的划线部分。
- 队列中的论文可立即阅读，不必等待导读；本地 PDF 可离线打开。
- 原版 PDF 默认 150% 显示，支持缩放、适宽、横向滚动和按需渲染；大窗口可独立调整大小。
- 自选保存目录，不自动搬动旧文件。

## Windows 快速上手

### 1. 准备程序和本地模型

可以下载已有的 Windows Release，或打开对应提交的**成功** `build-windows` Actions 运行记录，下载 `LazyScholar-windows` 构建产物。解压后运行 `LazyScholar.exe`；Actions 产物中的程序位于 `dist/`。

EXE **不需要安装 Python，也不依赖 Obsidian**，但需要：

- Windows 和 Microsoft Edge **WebView2 Runtime**，用于桌面界面及原生 MathML 公式显示。
- **生成导读时**，自行准备本地 llama.cpp 和兼容的 **GGUF 模型文件**；只阅读不需要模型。本机用过 Gemma 3 4B Q4；速度与内存占用取决于硬件和模型大小。

**注意启动器兼容性：**目前应用按 `llama serve -m <模型> -c <上下文> --port <端口>` 启动服务。「模型可执行文件」应选择支持该命令的 `llama` 启动器；不支持 `serve` 子命令的独立可执行文件不能直接替代。

推送 `main` 会触发 Actions 构建；只有版本标签才触发 Release 发布。源码推送不等于已经发布了新的 Release，需等待对应构建成功。

### 2. 首次配置

1. 双击程序，点击悬浮球打开侧边栏。
2. 在「设置／模型」填写 llama 可执行文件路径和 GGUF 模型路径，保存设置。
3. 检查本地模型端口和上下文长度，默认分别为 **8080**、**16384**。
4. 可在设置里选择保存目录。**退出并重启后生效，旧目录里的文件不会自动搬走。**

可以在「模型」页手动开关服务。若本批处理由应用启动模型，处理结束后会自动停止该进程；已经在运行的外部服务不会被当作应用自己启动的进程关闭。

### 3. 添加论文，立即阅读或生成导读

在「队列」中添加：

- arXiv 链接，例如 `https://arxiv.org/abs/2610.30130`；也可直接输入 `2610.30130`。
- 文件名含 arXiv 编号的本地 PDF，例如 `2610.30130.pdf`。
- 点击「导入保存的网页…」，选择 `.html`／`.htm` 或 `.mhtml`／`.mht`；不要求 arXiv 编号。

添加后点击队列项目的「立即阅读」，无需启动模型或生成导读。本地 PDF 默认显示所导入的原版 PDF，不依赖摘要下载；链接默认打开 arXiv HTML，未缓存内容仍需联网。没有导读时会显示明确提示，阅读和标注照常可用。

要生成导读，再选择立即处理或设置每天的处理时间。定时处理要求应用保持运行。导读失败不影响阅读；先查看错误、修正原因后重试，不必反复重复添加同一篇论文。

完成后到「笔记」打开论文，可切换「原文」「导读」；arXiv／PDF 项目另有「原版 PDF」。

### 导入保存的网页

浏览器选择「网页，全部」时，保留 HTML 旁边的资源文件夹；也支持 MHTML 单文件快照。导入只读取本地文件或快照内嵌图片，不联网补下载，不执行原网页脚本。

MathML 保留为原生公式；MathJax／KaTeX 有辅助 MathML 时仅保留这一份，避免重影。SVG 转成经过白名单清洗的独立静态图，部分样式可能简化。Canvas 像素通常不会随网页保存，缺失图形及图片会显示明确提示；这不是对原网站交互的完整复制。只保存了摘要页面，也不会自动变成论文全文。

单个网页文件限 32 MB，HTML 正文限 5 MB；图片只从网页目录内部或快照中读取。原文件不会被修改。

### 全文文字导读

桌面队列按来源读取：arXiv HTML；HTML 不可用时尝试完整 PDF 文字层；本地 PDF 使用本地文件；保存网页使用导入后的静态文字。不会静默改成摘要导读。

全部文字分段处理，模型为要点选择原文片段编号，程序从对应片段取回提取文字，再汇总为带 `[C0001]` 等标识的导读。最终每节也必须选择有效来源编号。队列显示处理进度；依据和来源指纹保存在 `fulltext/`，笔记标明实际来源与限制。索引校验只保证依据来自对应提取文字，不保证模型解释正确。

上下文至少 **8192**，默认 **16384**。过长的来源（超过 128 个处理片段）或不合格的模型结果会报错，不丢弃尾部、不写半成品，也不覆盖已有好笔记。扫描页没有文字层时导读会失败，但原版阅读仍可用。

新生成的模型内容跟随任务开始时保存的界面语言（中文或英文），包括分段分析、中间汇总和最终导读。切换语言不翻译或改写已有 Markdown；进行中的任务保持开始时的语言，原文引用保留来源语言。

## 原文阅读、划线和擦除

### 阅读原文

「原文」页会自动获取 arXiv HTML 并在本地缓存，**不用每篇手动保存网页**。`A− / A+` 调整字号，继续阅读时加载后续部分；长公式可以横向滚动，公式和插图可以点击放大。

没有 HTML 版本或下载失败时，会显示错误和重试入口。此时可打开「原版 PDF」查看完整原文，不会偷偷用碎裂的 PDF 公式切图代替正常数学排版。

### PDF 舒适阅读

「原版 PDF」默认 150% 显示，使用 `− / ＋` 缩放或点击「适宽」；放大后可横向滚动，保持原页公式和图表布局。PDF 缩放与 HTML 的 `A− / A+` 字号调整是两回事。页面按需绘制，高分屏使用更高分辨率画布。

「大窗口阅读」在桌面模式打开可调整大小的独立阅读窗口；HTTP 模式打开新标签页。窗口间共用已保存的标注。扫描 PDF 没有文字层时仍可看原页，但不能直接按文字划线。

同编号但文件内容不同的 PDF 不会静默替换已有缓存，以免把旧标注贴到不同版本。

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

同一应用的多个阅读窗口保存标注时会校验版本；冲突后重新读取，再应用本次新增或擦除，不用旧列表覆盖新标注。持续冲突会提示重试。

旧 PDF 文字标注只有在原句能唯一、精确匹配时才迁移到 HTML。原始标注文件会保留，不能匹配的原句可在阅读器查看，不会默默贴到另一句话上。

## 删除与恢复论文

在笔记页或队列页的**顶部点击「删除」**，选择一篇，再通过紧凑确认框确认。普通卡片没有删除按钮。导读、已确认归属的应用内原文／图片缓存、标注和证据直接交给 **Windows 系统回收站**；一篇论文可能对应多个系统回收站项目。外部原始 PDF／网页不动，不永久删除，也不再新建应用回收站或论文备份库。无法确认身份的手写笔记不会仅因正文引用了 arXiv 链接而被删除。

此功能使用本地固定磁盘上的 Windows 原生回收接口（EXE 包含 pywin32），遇到不支持的卷、取消或权限错误会停止，绝不降级成永久删除。多项回收不是整体原子事务：部分成功会准确提示，并保留元数据记录供重试；不会假称已全部完成。正在生成或实际读写原文／标注时暂不允许回收。

在 Windows 回收站还原本篇相关项目后，刷新笔记页；应用核对路径与指纹，再恢复无冲突的队列元数据，不覆盖还原内容。仅部分还原、内容已修改或队列冲突时会提示核实。也可明确重新导入当前来源，未还原的旧标注不会自动找回。恢复/重导入会更新阅读代号，旧窗口或迟到的标注请求不能写入新状态。

`paper_deletions.json` 仅保存删除收据与队列元数据，不存论文内容副本。旧版 `.paper-trash/` 如果已有记录，只显示一次性兼容提示，需逐条明确恢复；不会自动搬动或清空旧数据。没有本地文件的队列项只移除记录，确认框会说明它不会产生可从系统回收站还原的文件。

## 保存位置、网络与隐私

- 笔记和阅读数据存放在所选目录，常见子目录包括 `papers/`、`concepts/`、`papers_pdf/`、`html_reader/`、`reader/`、`highlights/`、`fulltext/`；应用配置和队列状态也保存在该目录。
- 修改保存目录必须**退出重启**；不会自动迁移、删除或合并旧目录。
- 显式 `--vault` 优先；`LAZYSCHOLAR_VAULT` 也会覆盖 GUI 保存的选择，应用会显示覆盖提示。
- 模型推理在本地。arXiv 元数据及未缓存的 HTML／插图／PDF 仍需联网；已缓存的原文不需要每次重新下载。
- 可选「HTML／插图代理」默认留空、沿用系统网络。需要显式代理时再填写，如 `http://127.0.0.1:<你的端口>`。该项只影响 HTML／插图，不影响本地模型，也不改变系统代理或 TUN 设置；不支持在 URL 中填写代理账号密码。
- 下载的论文脚本和样式不会嵌入应用的特权页面，公式保留 MathML 结构并在本地排版。

## 使用边界

- **桌面队列处理全部可提取的来源文字，但不等于模型看懂整篇论文的图表、公式或页面布局。** PDF 文字提取可能受双栏等排版影响；导入网页不保证包含原论文全文。旧命令行收件箱流程仍保留摘要式笔记路径。
- 来源编号有效不代表模型解释正确；模型仍可能把「尚未证明」误写成「不能」。请回到原文核对关键结论、数字和适用条件。
- 部分论文没有 arXiv HTML 版本，请使用完整原版 PDF。
- 不自动做数学 OCR，也不让模型「修补」或改写公式。
- PDF／链接入口仍需要 arXiv 编号，不是任意 PDF 导入工具；保存网页入口不需要编号。

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
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --windowed --onefile --name LazyScholar --icon lazyscholar.ico --collect-all webview --collect-all pystray --collect-all pdfplumber --collect-all pdfminer --collect-all pypdfium2 --collect-all bleach --collect-all bs4 --collect-all defusedxml --copy-metadata pywin32 --add-data "vendor;vendor" --add-data "lazyscholar.ico;." lazyscholar.py
```

输出为 `dist/LazyScholar.exe`。EXE 包含所需 Python 依赖，但不包含 GGUF 模型和系统 WebView2 运行时。旧的 `build-app.cmd`／`deploy-v2.ps1` 带有维护者电脑上的固定路径；其他机器请使用上述通用命令。

onefile 构建每次启动都会把依赖解包到 `%TEMP%\_MEIxxxxxx`（约 80 MB），而 PyInstaller 只在进程正常退出时才删除该目录——被强杀的运行会把它永久留下。因此 LazyScholar 会在启动时清理过期的解包目录，且放在后台线程执行、不拖慢窗口。它只删除同时满足以下条件的 `_MEI*` 目录：仍带有本构建的 `lazyscholar.ico` 标记、修改时间超过一小时、且不是当前进程正在使用的那一个；其他 PyInstaller 应用和同时启动的实例都不受影响。仍被占用的目录会在下次启动时重试。

## 常见问题

| 情况 | 检查方法 |
| --- | --- |
| 模型无法启动 | 检查启动器／模型路径、`llama serve` 是否可用、端口占用，以及保存目录中的 `model-server.log`。 |
| 论文处理失败 | 检查 arXiv 编号、网络和队列错误提示后重试；本地 PDF 文件名需带编号。 |
| HTML 获取失败 | 重试，检查可选 HTML 代理，或打开原版 PDF；并非每篇论文都有 HTML。 |
| 更换目录后仍看到旧目录 | 退出重启，检查是否被 `--vault`／`LAZYSCHOLAR_VAULT` 覆盖；旧文件不会自动搬动。 |
| 选中文字时总在新增或擦除标记 | 先退出「划线模式／擦除模式」，再普通拖选复制。 |
| 界面或公式不正常显示 | 检查 WebView2 Runtime 是否安装并保持更新。 |
| 每次启动后 `%TEMP%` 越来越大 | 过期的 `_MEI*` 解包目录会在下次启动时清理；只删带本构建标记且超过一小时的目录，因此启动后一小时内被强杀会留下一个，等下次启动清掉。 |

## 开发与测试

以下命令使用安装依赖的同一个 Python 环境；若未激活虚拟环境，将 `python` 替换为 `.\.venv\Scripts\python.exe`。

标注 API 的 POST 必须回传 GET 得到的 `revision`；版本冲突返回 HTTP 409。客户端应重读并重新计算本次操作，不能直接重发旧的完整列表。存在删除生命周期记录后，还需从 `/api/note` 或 `/api/queue/reading` 捕获 `lifecycle`，读写标注时携带 `X-Paper-Lifecycle`。排队操作在选区入队时保存代号，不能借用重新打开的阅读器代号；过期请求返回 HTTP 410。

离线后端与提取器测试：

```powershell
python -B scripts/test_app_backend.py
python -B scripts/test_paper_reader.py
python -B scripts/test_paper_html.py
python -B scripts/test_saved_html.py
python -B scripts/test_fulltext.py
python -B scripts/test_generation_language.py
python -B scripts/test_paper_trash.py
python -B scripts/test_windows_recycle.py
```

浏览器回归另外需要 Playwright 和 Chromium：

```powershell
python -m pip install playwright
python -m playwright install chromium
python -B scripts/test_reader_browser.py
python -B scripts/test_highlight_eraser.py
python -B scripts/test_independent_reading.py
python -B scripts/test_saved_html_browser.py
python -B scripts/test_reader_races.py
python -B scripts/test_pdf_loading_zoom.py
python -B scripts/test_paper_trash_browser.py
python -B scripts/test_delete_ui.py
python -B scripts/test_annotation_lifecycle_browser.py
```

原生系统回收验收为显式选择项：只回收/还原本次创建的隔离测试文件，不清空或处理用户其他回收站项目。

```powershell
python -B scripts/accept_windows_recycle.py
python -B scripts/test_paper_trash_browser.py --exe dist/LazyScholar.exe --native-window
```

Windows 真实鼠标验收使用隔离测试目录，会短暂移动并恢复鼠标。请让测试窗口保持前台，暂时停止手动操作：

```powershell
python -B scripts/test_native_highlight.py --exe dist/LazyScholar.exe --focus-wait 60
python -B scripts/test_native_pdf_zoom.py --exe dist/LazyScholar.exe --during-load --focus-wait 60
```

划线、局部擦除及 PDF 缩放做过真实 Windows 鼠标验收；回收／恢复有隔离后端与浏览器回归，语言切换有真实本地模型队列验收。重新打包后仍应复查；浏览器脚本创建选区不能冒充原生输入。QA 输出、私人笔记、模型权重、回收站记录和交接快照不随源码上传。

## 项目与许可证

为 DEV Hacktoberfest 2026 第三周 **Slow and Steady** 而作：做一个小而私密的学习助手，而不是云端知识平台。

应用代码使用 [MIT 许可证](LICENSE)。llama.cpp、模型权重和捆绑的第三方组件分别遵循其各自许可证。
