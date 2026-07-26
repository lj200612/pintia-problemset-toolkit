# 第三方组件与内容说明

本文件记录项目直接依赖和嵌入素材的来源与许可证。它不是法律意见，也不能替代各组件随附的完整许可证。

## Python 直接依赖

| 组件 | 用途 | 许可证 | 项目地址 |
| --- | --- | --- | --- |
| Requests | HTTPS 客户端 | Apache License 2.0 | [psf/requests](https://github.com/psf/requests) |
| Flask | 本地 WebUI 服务 | BSD 3-Clause License | [pallets/flask](https://github.com/pallets/flask) |
| Playwright for Python（可选） | 浏览器辅助登录 | Apache License 2.0 | [microsoft/playwright-python](https://github.com/microsoft/playwright-python) |

安装时还会解析这些项目的传递依赖。实际安装版本及许可证以 Python 环境中的包元数据和发行包为准；发布者应在发版前运行依赖清单与许可证审计。

## WebUI 内嵌 SVG 图标

WebUI 使用从 `lucide-static` 1.27.0 重新导入并内嵌的 SVG 路径。Lucide
主体使用 ISC License，其中部分图标派生自 Feather Icons，并同时保留
Feather 的 MIT License 与 Cole Bemis 版权声明。完整许可证文本见
[`licenses/LUCIDE.txt`](licenses/LUCIDE.txt)。

本项目使用的图标为：Search、RefreshCw、Sun、Moon、LogOut、Send、Save、
Download、Layers、X、Book、Menu、Zap、Key、Globe、Plus、ChevronDown、
Ellipsis 和 Maximize。`pta/web/index.html` 中的内部 symbol ID 仅为现有 UI
接口名，不表示图标由本项目原创。

## PTA/Pintia 内容与标识

PTA、拼题 A、Pintia 的名称、标识、页面、题面、测试数据、平台响应和用户提交不属于本项目的 MIT License 授权范围。仓库和发行包不得包含抓取的题面、真实答案、课程截图或平台前端副本。

本项目未获 PTA/Pintia 或相关运营方授权、认可或背书。使用者需要自行确认平台、学校、课程和题集组织方的规则；没有公开可验证的 API 文档或条款不能解释为允许自动化使用。
