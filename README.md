# AI 智能日记拆分工具（Obsidian 版）

将 Obsidian 日记自动拆分为结构化 Markdown 条目（事件 / 关系 / 属性 / 成就等），并生成带 YAML frontmatter 的文件，可直接配合 Obsidian Dataview 插件使用。

## 功能特点

- **自动化**：扫描日记文件夹，一键触发 AI 拆分
- **自定义模板**：通过 JSON 文件定义拆分维度和字段
- **隐私优先**：数据全部本地存储，仅调用 AI API 时发送日记文本
- **双链支持**：`relation` 类型字段自动转换为 Obsidian `[[双链]]` 格式
- **防重复处理**：记录已处理文件，避免重复消耗 API

## 目录结构

```
diary_splitter/
├── main.py               # 主脚本
├── config.example.yaml   # 配置模板（复制为 config.yaml 后填写）
├── requirements.txt      # Python 依赖
└── templates/
    ├── basic.json        # 基础日记模板（事件 / 人物 / 情感）
    └── game_life.json    # 游戏人生模板（属性 / 事件 / 关系 / 成就）
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，填写：
- `obsidian_vault_path`：你的 Obsidian 库绝对路径
- `api.api_key`：你的 DeepSeek 或 OpenAI API Key

### 3. 在 Obsidian 写日记

在 vault 的 `日记/` 文件夹下创建 `YYYY-MM-DD.md` 格式的日记文件。

### 4. 运行

```bash
# 处理所有新日记
python main.py

# 仅扫描，不调用 API（测试用）
python main.py --dry-run

# 处理指定文件
python main.py --file "日记/2026-02-21.md"

# 强制重新处理所有日记
python main.py --force
```

## 生成结果示例

一篇日记会在 vault 中按维度生成多个文件：

**`事件/2026-02-21-春节聚餐.md`**
```markdown
---
标题: 春节聚餐
日期: 2026-02-21
情感分: 4
类型:
  - 家庭
  - 节日
关键词:
  - 过年
  - 老家
是否核心: true
关联人物: [[妈妈]], [[爸爸]]
---
妈妈做了鱼，爸爸做了羊肉...
```

## 自定义模板

模板为 JSON 格式，支持以下字段类型：

| 类型 | 说明 |
|------|------|
| `text` | 文本 |
| `number` | 数字（可设 min/max） |
| `date` | 日期（YYYY-MM-DD） |
| `select` | 单选（需提供 options） |
| `multiselect` | 多选（free: true 允许自定义值） |
| `boolean` | 布尔值 |
| `relation` | 关联其他维度，生成 Obsidian 双链 |

模板放在 `templates/` 目录下，在 `config.yaml` 中通过 `template_path` 指定。

## 支持的 AI 提供商

| provider | model 示例 |
|----------|-----------|
| `deepseek` | `deepseek-chat` |
| `openai` | `gpt-4o`, `gpt-4-turbo` |

设置 `base_url` 可接入代理或兼容 OpenAI 协议的自托管模型。

## 注意事项

- `config.yaml` 已加入 `.gitignore`，不会被提交，请妥善保管 API Key
- `processed_diaries.txt` 记录已处理文件，删除可重置处理状态
