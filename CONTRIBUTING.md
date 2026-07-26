# 贡献指南

感谢你帮助改进项目。提交代码前请先阅读 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)、[SECURITY.md](SECURITY.md) 和以下内容边界。

## 内容与授权边界

贡献不得包含：

- 真实 Cookie、密码、API 令牌、私钥或其他凭据；
- 真实姓名、学号、手机号、邮箱、课程名、头像或可识别截图；
- 从题集抓取的题面、测试数据、课程资料或他人答案；
- 真实题集 ID、题目 ID、提交 ID或本机用户路径；
- 未获许可的商标、图标、字体、图片、前端 bundle 或反编译内容；
- 交卷、删除、重判、重置、调分、绕过访问控制或规避人机验证的能力；
- 与项目实际使用无关的管理端接口清单或探测结果。

请使用 `example.invalid`、明显合成的 ID 和自己编写的最小样例。贡献者必须有权按项目许可证提交所贡献的代码与素材。

## 开发环境

项目支持 Python 3.10–3.13。建议创建虚拟环境后安装开发依赖：

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m playwright install chromium  # 仅测试浏览器辅助登录时需要
```

虚拟环境激活命令见 [README.md](README.md)。不要在测试中访问真实 PTA/Pintia 服务；使用 mock、fake transport 或 `example.invalid`。

使用 uv 的贡献者可以运行 `uv sync --locked --extra browser --extra dev`；修改
`pyproject.toml` 中的依赖后必须同步更新并提交 `uv.lock`。

## 修改流程

1. 为一个独立问题创建分支。
2. 先添加能够暴露问题的测试，再实现最小修复。
3. 保持兼容 Python 3.10，并为公开函数提供清晰命名、类型提示和错误信息。
4. 若修改配置、数据流、网络目的地、日志或命令参数，同步更新 README、隐私说明和变更日志。
5. 在提交前运行验证。

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest
python -m compileall -q pta pta_submit.py pta_fetch.py pta_list.py pta_web.py scripts
python scripts/check_release.py
python -m build
python -m twine check dist/*
```

发布边界检查默认同时审查 Git 索引中的完整候选树及其工作区文件；首次提交前可用 `--candidate` 或 `--candidates-from` 检查尚未跟踪的显式文件清单。守卫会拒绝未列入白名单的路径、二进制、异常编码、链接、超限文件和常见凭据，但仍需人工检查完整历史、内容授权和许可证来源。

## Pull Request 要求

PR 描述应说明问题、方案、风险和验证结果。尽量保持改动聚焦，并确认：

- 没有真实账号数据、题面、答案、截图或生成物；
- 新增网络请求是完成已有用户功能所必需的，且目的地和数据流已记录；
- 没有扩大到管理、绕过或高风险自动化能力；
- 新依赖有明确用途、受维护、许可证兼容并设置合理版本范围；
- 测试不依赖真实平台状态；
- 面向用户的行为变化已加入 `CHANGELOG.md`。

维护者可能要求拆分过大的 PR、补充测试或更换来源不明的素材。

## 报告问题

普通 Bug 可以使用 Issue 模板。安全问题必须按 [SECURITY.md](SECURITY.md) 私下报告。

诊断信息在上传前应脱敏；不要依赖事后编辑，因为通知邮件、缓存和 Git 历史可能保留原始内容。

## 许可证

提交贡献即表示你有权提供该贡献，并同意其按本项目的 [MIT License](LICENSE) 发布。第三方内容仍受其原许可证约束，必须在 `THIRD_PARTY_NOTICES.md` 中准确记录。
