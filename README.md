# 小说知识库 · Novel Knowledge Base

> 把小说变成可互动的沉浸式体验 — 上传、AI 解析、以主角视角经历故事

---

## 是什么

小说知识库是一套本地部署的 AI 工具链，完成两件事：

1. **结构化解析**：把小说文本（最多 20000 字/篇）交给 AI，自动提取事件、人物、关系、属性、成就，存为带 YAML frontmatter 的 Markdown 档案
2. **沉浸式体验**：AI 扮演叙事引擎，让你以主角视角一幕幕"经历"故事，配角开口前会查阅档案确保性格一致

所有数据留在本地，支持 DeepSeek / OpenAI / 任意 OpenAI 兼容接口。

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动 Web UI
python web_app.py

# 3. 打开浏览器
open http://127.0.0.1:7860
```

首次打开后在「⚙️ 配置」页填入 API Key，然后前往「📚 上传解析」粘贴小说内容即可。

详细步骤见 [入门教程.md](入门教程.md)。

---

## 功能一览

| 功能 | 入口 | 说明 |
|------|------|------|
| Web UI | `python web_app.py` | 三标签页：配置 / 上传解析 / 沉浸聊天 |
| 小说解析 CLI | `python main_novel.py` | 命令行批量或单文件解析 |
| 日记解析 CLI | `python main.py` | 按日期拆分个人日记 |
| 沉浸聊天 CLI | `python chat_client_novel.py` | 终端版角色扮演 |
| REST API | `python api_server.py` | 动态生成查询端点，含 Swagger UI |
| MCP Server | `python mcp_server.py` | Claude Desktop 工具集成 |

---

## 目录结构

```
diary_splitter/
├── web_app.py            # Web UI 后端（FastAPI，端口 7860）
├── web/index.html        # 前端 SPA（纯 HTML/JS，无外部依赖）
├── main_novel.py         # 小说解析器
├── main.py               # 日记解析器
├── chat_client_novel.py  # 小说沉浸聊天客户端
├── chat_client.py        # 日记问答聊天客户端
├── api_server.py         # REST API 服务
├── mcp_server.py         # MCP Server（Claude Desktop）
├── config.yaml           # 统一配置文件
├── templates/
│   └── game_life.json    # 默认解析模板（4维度：事件/属性/关系/成就）
├── data/                 # 解析数据（自动创建）
│   └── {小说名}/
│       ├── src/          # 原始文本
│       ├── 事件/         # 事件档案
│       ├── 属性/         # 属性档案
│       ├── 关系/         # 人物关系档案
│       └── 成就/         # 成就档案
├── 入门教程.md           # 零基础用户快速上手（10分钟）
├── 使用指南.md           # 完整功能使用说明
├── 产品文档.md           # 产品设计与功能详述
└── 技术文档.md           # 架构与开发者参考
```

---

## 技术栈

- **后端**：Python 3.10+，FastAPI，uvicorn
- **AI 接入**：OpenAI SDK（兼容 DeepSeek / OpenAI / Ollama）
- **MCP**：FastMCP（Claude Desktop 工具协议）
- **数据格式**：YAML frontmatter + Markdown 正文
- **前端**：原生 HTML/CSS/JS，零外部依赖

---

## 文档

| 文档 | 面向 | 内容 |
|------|------|------|
| [入门教程.md](入门教程.md) | 零基础用户 | 10 分钟完成第一次体验 |
| [使用指南.md](使用指南.md) | 进阶用户 | 所有功能完整说明 |
| [产品文档.md](产品文档.md) | 产品 / 运营 | 功能设计、定位、用户场景 |
| [技术文档.md](技术文档.md) | 开发者 | 架构、API、数据格式、扩展指南 |

---

## License

MIT
