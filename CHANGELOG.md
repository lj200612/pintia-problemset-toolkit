# 变更日志

本项目的重要变化记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- 本地 CLI 与 WebUI 的首个 Alpha 发布候选。
- 安全策略、隐私数据流、贡献指南、行为准则和第三方说明。
- Python 包元数据、命令行安装入口、CI 和发布边界检查。
- WebUI 回环地址限制与每次启动生成的本地 API 访问令牌。
- Ruff 格式/Lint、Mypy 类型检查和对应的 CI 质量门禁。

### Changed

- 支持范围调整为 Python 3.10–3.13。
- 依赖升级到当前安全基线，并设置主版本上限。
- Playwright 改为可选的 `browser` extra。
- 依赖安装改为显式用户操作，启动器不再自动调用 `pip`。
- 示例配置中的 Cookie 和题集 ID 改为空占位。
- WebUI 静态资源不再依赖运行时字体 CDN。
- WebUI 图标改用已核对的 `lucide-static` 路径并随包保留许可证文本。

### Security

- 限制 WebUI 的 Host、Origin 和本地 API 访问令牌边界。
- 限制 WebUI 保存答案时的题目 ID、扩展名和目标路径。
- 隔离 WebUI 应用实例与认证配置之间的缓存，并限制缓存生命周期。
- 校验平台返回的题目 ID，并确保抓取输出始终位于指定目录内。
- CLI 与 WebUI 持久日志不再保存远端错误正文。
- 增加严格发布白名单、大小/编码/二进制/链接边界和常见凭据检查。

项目尚无已发布的稳定版本。
