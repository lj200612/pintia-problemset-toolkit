# PTA 本地题集工具

一个在本机运行的 PTA/Pintia 题集辅助工具，提供命令行和 WebUI，可查看题集、抓取题面、管理本地答案、提交代码并轮询判题结果。

> [!IMPORTANT]
> 本项目是非官方社区项目，与 PTA、拼题 A、Pintia、浙江大学及相关运营方没有隶属、合作、认可或授权关系。相关名称与商标归各自权利人所有。
>
> 仅在你有权使用的账号、题集和内容上使用本工具，并遵守平台规则、课程要求及适用法律。请勿用于代做、绕过考试限制、批量滥用接口或获取未获授权的内容。

本项目没有取得官方 API 授权，当前也没有核实平台是否许可这类自动化访问。未找到公开条款或文档不代表允许使用；运行前请自行向题集所属组织和平台确认规则。遇到 `429` 或其他限流响应时应停止或降低请求频率，不能把客户端观察到的限额当作使用许可。

当前版本为 `0.1.0` Alpha：适合在本地测试，接口兼容性和配置格式仍可能变化。

## 功能与边界

- 查看当前账号加入的题集、题目目录和答题状态。
- 从 `answers/` 按题目 ID 匹配本地源码。
- 默认只提交“本地有答案且尚未通过”的题目；支持预演和二次确认。
- 提交后轮询判题结果。
- 将有权访问的题面保存为本地 Markdown；题面文件不会纳入发行包。
- 提供本地 WebUI，用于浏览题目、编辑答案和提交。
- 可选 Playwright 辅助登录；滑块等人机验证始终由用户手动完成。

本项目不实现交卷、删除、重判、重置、调分等管理操作，也不应加入这类能力。

## 运行要求

- Python 3.10–3.13
- Windows、macOS 或 Linux
- 一个有效且有权使用的 PTA/Pintia 账号

Python 3.8 和 3.9 已停止上游安全支持，本项目不再支持这些版本。

## 安装

先创建独立虚拟环境。

Windows PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

项目不会在启动时自动安装依赖。依赖安装必须由用户在虚拟环境中显式执行。

仓库同时提交 `uv.lock`。需要完全复现维护者解析出的依赖版本时，可安装
[uv](https://docs.astral.sh/uv/) 后执行：

```bash
uv sync --locked                 # 基础 CLI / WebUI
uv sync --locked --extra browser # 加入浏览器辅助登录
```

### 可选：浏览器辅助登录

手动粘贴 Cookie 和命令行功能不需要 Playwright。若要使用 WebUI 的浏览器辅助登录：

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

开发者也可使用可编辑安装；安装后会提供 `pta-submit`、`pta-fetch`、`pta-list` 和 `pta-web` 命令：

```bash
python -m pip install -e .
python -m pip install -e ".[browser]"
```

## 配置

复制示例文件，然后编辑本地的 `config.json`。

Windows PowerShell：

```powershell
Copy-Item config.example.json config.json
```

macOS / Linux：

```bash
cp config.example.json config.json
```

必须配置的字段：

- `problemSetId`：题集 URL 中的 ID，例如 `https://pintia.cn/problem-sets/{problemSetId}/...`。
- `cookie`：登录后的完整 Cookie 字符串。建议保持为空，改用 `PTA_COOKIE` 环境变量。

临时设置 Cookie：

Windows PowerShell：

```powershell
$env:PTA_COOKIE = '在此粘贴 Cookie'
```

macOS / Linux：

```bash
export PTA_COOKIE='在此粘贴 Cookie'
```

`PTA_COOKIE` 的优先级高于 `config.json`。关闭当前终端后，以上临时环境变量通常会失效。不要把真实 Cookie 写进 shell 历史、截图、Issue、日志或 Git。

为保持原有的直连行为，工具默认不继承 `HTTP_PROXY` / `HTTPS_PROXY` 等系统代理。
确实需要通过自己信任的系统代理访问时，可显式设置 `PTA_USE_SYSTEM_PROXY=1`；代理将能看到连接元数据，并可能在受信任证书配置下处理 HTTPS 流量。

### 获取 Cookie

1. 在浏览器中登录 [Pintia](https://pintia.cn) 并进入你有权访问的题集。
2. 打开开发者工具的 Network（网络）面板并刷新页面。
3. 选择一个发往 `pintia.cn` 的 API 请求。
4. 从 Request Headers 复制完整 `Cookie` 值。

Cookie 等同于登录会话。泄漏后应立即在平台退出相关会话或修改凭据，并删除本地副本。

## WebUI

```bash
python pta_web.py
# 或安装项目后：
pta-web
```

启动器默认只监听 `127.0.0.1`，并为本次进程生成随机的本地 API 访问令牌。后端把令牌嵌入本次返回的首页，前端调用本地 API 时必须携带该令牌。令牌会在进程退出后失效。它只能降低误访问和跨站请求风险，不能抵御已经控制本机账号的恶意程序。

不要把 WebUI 暴露到局域网或公网，不要放到反向代理后，也不要从页面源码、开发者工具或 API 请求中复制并分享访问令牌。WebUI 使用明文 HTTP 是因为它仅设计用于本机回环地址。

登录方式：

1. 浏览器辅助登录：本地页面把账号和密码交给本地后端，再由临时 Chromium 窗口填入；账号和密码不写入配置文件，但登录成功后的 Cookie 会写入 `config.json`。
2. 手动粘贴 Cookie：校验成功后，Cookie 会写入 `config.json`。

WebUI 的“退出登录”会清空配置文件中的 Cookie，但不会删除答案、题面、CLI 日志或浏览器 `localStorage` 中的编辑草稿。完整数据流和删除方法见 [PRIVACY.md](PRIVACY.md)。

## 命令行

### 查找题集

`pta-list` 只需要 Cookie，不要求预先填写 `problemSetId`。

```bash
python pta_list.py
python pta_list.py --all
python pta_list.py --available
python pta_list.py --me
```

### 准备答案

在 `answers/` 中放置源码，文件名必须是题目 ID，扩展名决定默认编译器。

```text
answers/
  {题目-id}.c
  {题目-id}.cpp
```

- `CODE_COMPLETION`：通常只提交题目要求补全的部分，不要自行加入 `main`。
- `PROGRAMMING`：提交完整程序；编译器映射可在 `compilerByExtension` 中调整。

### 查看与提交

```bash
# 只查看，不提交
python pta_submit.py --list

# 预演，不提交
python pta_submit.py --dry-run

# 正式提交；默认再次确认
python pta_submit.py

# 仅提交指定题目
python pta_submit.py --only {题目-id},{另一题目-id}

# 跳过确认 / 包含已通过题 / 不轮询
python pta_submit.py --yes
python pta_submit.py --include-accepted
python pta_submit.py --no-poll
```

| 参数 | 说明 |
| --- | --- |
| `--config <path>` | 配置文件路径，默认 `config.json` |
| `--list` | 只列出题目与本地答案覆盖情况 |
| `--dry-run` | 显示计划，不提交 |
| `-y, --yes` | 跳过二次确认 |
| `--force` | 在题集状态异常时仍尝试提交；使用者自行承担风险 |
| `--no-poll` | 提交后不轮询判题 |
| `--include-accepted` | 重新提交已通过题目 |
| `--only id1,id2` | 仅处理指定题目 ID |

CLI 的正式提交会写入 `logs/submit-*.jsonl`，记录题目 ID、标题、编译器、提交 ID、状态、分数和脱敏错误元数据。`--list` 和 `--dry-run` 不创建提交日志。WebUI 的提交与结果事件写入 `logs/submit-web-YYYYMMDD.jsonl`，记录题集/题目 ID、提交 ID、状态和稳定错误类别。两类日志都不记录 Cookie、源代码或远端错误响应正文，但仍属于个人学习记录。

### 抓取题面

```bash
python pta_fetch.py --list
python pta_fetch.py
python pta_fetch.py --overwrite
python pta_fetch.py --type CODE_COMPLETION
python pta_fetch.py --type PROGRAMMING
python pta_fetch.py --only {题目-id}
python pta_fetch.py --combined
```

题面默认写入 `problems/`。其中可能包含受版权或课程规则约束的内容，只能保存你有权访问和留存的材料；不要随源码仓库再发布。

## 数据与网络

默认情况下，工具会连接 `pintia.cn` 和 `passport.pintia.cn`，发送完成登录校验、读取题集和提交所需的数据。浏览器辅助登录还会加载 Pintia 登录页及该页面自身引用的资源。项目不包含遥测、广告、第三方分析或运行时字体 CDN 请求。

`baseUrl` 和 `extraHeaders` 是高级配置。修改它们会改变请求目的地或内容；不要使用来源不明的配置文件，因为 Cookie 可能随请求发送到配置的服务。

详见 [PRIVACY.md](PRIVACY.md) 和 [SECURITY.md](SECURITY.md)。

## 项目结构

```text
pta_submit.py             # 提交 CLI
pta_fetch.py              # 题面抓取 CLI
pta_list.py               # 题集发现 CLI
pta_web.py                # 本地 WebUI 启动器
pta/                      # 客户端、配置、提交与 Web 后端
  web/                    # 随 Python 包发布的 WebUI 静态资源
answers/                  # 本地答案；发行时为空
problems/                 # 本地题面；发行时为空
tests/                    # 自动化测试
scripts/check_release.py  # 发布边界与凭据检查
config.example.json       # 无凭据的配置示例
```

## 参与贡献与安全报告

- 贡献流程和内容边界：[CONTRIBUTING.md](CONTRIBUTING.md)
- 安全漏洞私下报告：[SECURITY.md](SECURITY.md)
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- 第三方组件说明：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- 版本变更：[CHANGELOG.md](CHANGELOG.md)

不要在公开 Issue 中发布真实 Cookie、账号、题目内容、学生信息或可直接利用的漏洞细节。

## 许可证

项目代码以 [MIT License](LICENSE) 许可。题面、用户答案、平台返回数据、PTA/Pintia 名称与商标不因 MIT License 而获得重新授权。
