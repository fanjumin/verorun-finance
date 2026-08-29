# Agent Matrix 工具集 — 开发状态

> 最后更新：2026-07-15 | 系统版本：0.14.0 | AI Advisor：1.1.0

## 架构概览

```
用户输入（自然语言）
    ↓
口令控制台 (admin/templates/partials/ai_chat.html)
    ↓
Agent Matrix 意图识别 (agent_matrix/intent.py)
    ↓
工具调用 (agent_matrix/tools.py)
    ├── generate_ppt       → .pptx    下载按钮
    ├── generate_image     → .png     内联图片预览
    ├── generate_markdown  → .md      右侧样式化预览面板
    └── generate_docx      → .docx    下载按钮
```

## 核心文件清单

| 文件 | 说明 |
|------|------|
| [agent_matrix/tools.py](file:///f:/Sites/VeroRun/agent_matrix/tools.py) | 所有工具的 schema + executor（~750 行） |
| [agent_matrix/routes.py](file:///f:/Sites/VeroRun/agent_matrix/routes.py) | API 路由：chat/tool、md-preview、media/download |
| [agent_matrix/engine.py](file:///f:/Sites/VeroRun/agent_matrix/engine.py) | AI 引擎（DashScope / OpenAI / DeepSeek / OpenRouter） |
| [agent_matrix/intent.py](file:///f:/Sites/VeroRun/agent_matrix/intent.py) | 意图识别 |
| [agent_matrix/orchestrator.py](file:///f:/Sites/VeroRun/agent_matrix/orchestrator.py) | 编排器（子 Agent 调度） |
| [agent_matrix/agent_runner.py](file:///f:/Sites/VeroRun/agent_matrix/agent_runner.py) | Agent 运行时 |
| [agent_matrix/audio.py](file:///f:/Sites/VeroRun/agent_matrix/audio.py) | 语音接口（ASR + TTS 抽象类，暂未实现） |
| [admin/templates/partials/ai_chat.html](file:///f:/Sites/VeroRun/admin/templates/partials/ai_chat.html) | 口令控制台前端（JS 动态渲染） |

## 工具详情

### generate_ppt
- **schema**: topic(pages(3-20), style)
- **executor**: AI 生成大纲 → python-pptx 渲染暗色主题 16:9
- **返回**: 下载链接

### generate_image
- **schema**: prompt(style: realistic/anime/3d/painting/line-art, count: 1-4, size)
- **executor**: 硅基流动 API (siliconflow_api_key) + 保存到 media/temp
- **返回**: 行内图片预览 + 下载按钮
- **API Key**: system_config.siliconflow_api_key

### generate_markdown
- **schema**: topic(outline, style: technical/professional/creative/simple/list)
- **executor**: 硅基流动/阿里云 API → 保存 .md 文件
- **返回**: 文本摘要 + 右侧样式化预览面板 (markdown → fenced_code/tables/nl2br)
- **注意**: 需要服务器安装 `python3 -m pip install markdown`

### generate_docx
- **schema**: topic(style: professional/formal/creative/simple/report, sections: 3-12)
- **executor**: AI 生成 Markdown → python-docx 渲染
- **返回**: 下载链接
- **注意**: 需要服务器安装 `python3 -m pip install python-docx`

## 插件状态

| 插件 | 状态 | 说明 |
|------|------|------|
| AI Chatbot (chatbot) | ✅ 启用 v1.1.0 | 独立 DB (chatbot.db)，与主库解耦 |

## 提供商配置

| 提供商 | 用途 | API Key 配置 |
|--------|------|-------------|
| 硅基流动 | **默认**（境内） | system_config.siliconflow_api_key |
| 阿里云 DashScope | 兜底（硅基无 Key 时） | system_config.dashscope_text_key |
| OpenRouter | 境外路由 | system_config.openrouter_api_key |

## 已知问题

1. `intent.py` 中的 `AIEngine` 创建使用硬编码 `{'provider': 'dashscope', 'model_name': 'qwen-turbo'}` — 应复用 Master Agent 配置
2. .docx 预览暂不支持（仅下载），后续可增加 docx → HTML 转换
3. 语音接口 `audio.py` 仅占位，未实现具体功能
4. 口令控制台右侧预览面板对图片/PPT/docx 暂不支持 — 目前仅 .md

## 提交历史 (相关)

```
32fa469 feat: add generate_docx tool + voice interface stubs (audio.py)
7632a87 feat: add generate_markdown tool + md preview panel in command console
0c6d3ea feat: add generate_image tool with SiliconFlow API support
6304097 refactor: merge PPT generation into Agent Matrix tools, disable ai_tools plugin
0902dd2 chore: bump AI Advisor to v1.1.0
4e0eeab fix(chatbot): change handoff keywords from Chinese to English for i18n consistency
347789b fix(chatbot): migrate_from_main() should not overwrite existing config values
7f23c88 chore: bump system to v0.14.0 and AI Advisor to v1.0.0
2913fb4 fix(chatbot): decouple from main DB agent_matrix, add identifier column
aa1af7a fix(chatbot): i18n hardcoded strings and avatar border-radius
```
