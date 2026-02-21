#!/usr/bin/env python3
"""Obsidian 日记知识库 MCP Server

通过 FastMCP 暴露 6 个查询工具，让 AI 可以查询用户个人日记知识库。

运行方式：
  python mcp_server.py          # stdio 模式（供 Claude Desktop / Cursor 配置）
  python mcp_server.py --port 8000  # SSE HTTP 模式（调试用）

Claude Desktop 配置示例（~/Library/Application Support/Claude/claude_desktop_config.json）：
{
  "mcpServers": {
    "diary": {
      "command": "python",
      "args": ["/path/to/diary_splitter/mcp_server.py"]
    }
  }
}

示例系统提示词（给 AI 用）：
---
你拥有访问用户个人日记知识库的能力，可通过以下工具了解用户信息：

工具说明：
- search_entries(query, dimension?, date_from?, date_to?)：关键词搜索
- get_person(name)：查询某个人物的关系和历史互动
- get_events(...)：查询事件，支持日期/类型/情感分过滤
- get_attributes(attribute_type?)：查询用户的性格、状态、喜好
- get_achievements(category?, min_importance?)：查询成就记录
- vault_summary()：获取知识库整体统计

使用原则：
1. 当用户提及自己的经历、感受或问"我喜欢什么/我认识谁"时，先调用工具再回答
2. 不要主动透露所有信息，按需查询，让对话自然
3. 结合查询结果给出更个性化、有温度的回应
---
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import yaml
from mcp.server.fastmcp import FastMCP


# ─── 配置 ────────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"

# 维度名 → vault 文件夹名的映射（与 config.yaml 中 output_folders 保持一致）
DIMENSION_FOLDERS = {
    "属性": "属性",
    "事件": "事件",
    "关系": "关系",
    "成就": "成就",
}


def load_vault_path() -> Path:
    """从 config.yaml 读取 vault 路径，不校验 API key。"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件未找到: {CONFIG_PATH}")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 也允许 output_folders 覆盖维度→文件夹映射
    folders = config.get("output_folders", {})
    for dim in DIMENSION_FOLDERS:
        if dim in folders:
            DIMENSION_FOLDERS[dim] = folders[dim]

    raw_path = config.get("obsidian_vault_path", "")
    vault_path = Path(raw_path)
    if not vault_path.exists():
        raise FileNotFoundError(
            f"Obsidian vault 路径不存在: {vault_path}\n"
            f"请检查 {CONFIG_PATH} 中的 obsidian_vault_path 配置。"
        )
    return vault_path


# ─── Vault Reader ────────────────────────────────────────────────────────────────

def _strip_wikilinks(value):
    """将 [[xxx]] 转为 xxx（去掉 Obsidian 双链标记）。"""
    if isinstance(value, str):
        return re.sub(r"\[\[(.+?)\]\]", r"\1", value)
    if isinstance(value, list):
        return [re.sub(r"\[\[(.+?)\]\]", r"\1", str(v)) for v in value]
    return value


def _parse_md_file(file_path: Path) -> dict:
    """解析单个 .md 文件，返回 frontmatter 字段 + 正文（存入"描述"键）。"""
    content = file_path.read_text(encoding="utf-8")

    # 按 --- 分割 frontmatter
    parts = content.split("---", 2)
    if len(parts) >= 3:
        frontmatter_raw = parts[1]
        body = parts[2].strip()
    else:
        frontmatter_raw = ""
        body = content.strip()

    try:
        data = yaml.safe_load(frontmatter_raw) or {}
    except yaml.YAMLError:
        data = {}

    if not isinstance(data, dict):
        data = {}

    # 清理所有字段中的 wikilinks
    cleaned: dict = {}
    for k, v in data.items():
        cleaned[k] = _strip_wikilinks(v)

    # 正文放入"描述"（若 frontmatter 中已有描述则以正文覆盖，保持完整文本）
    if body:
        cleaned["描述"] = body

    return cleaned


def load_vault_entries(vault_path: Path, dimension: str) -> list[dict]:
    """读取某维度文件夹下所有 .md 文件，返回条目 dict 列表。

    每条记录额外携带 _file（文件名）和 _dimension（维度名）字段。
    """
    folder_name = DIMENSION_FOLDERS.get(dimension, dimension)
    folder = vault_path / folder_name
    if not folder.exists():
        return []

    entries = []
    for md_file in sorted(folder.glob("*.md")):
        try:
            data = _parse_md_file(md_file)
            data["_file"] = md_file.name
            data["_dimension"] = dimension
            entries.append(data)
        except Exception:
            continue
    return entries


def load_all_entries(vault_path: Path) -> list[dict]:
    """加载所有维度的条目。"""
    all_entries: list[dict] = []
    for dim in DIMENSION_FOLDERS:
        all_entries.extend(load_vault_entries(vault_path, dim))
    return all_entries


# ─── 日期过滤辅助 ─────────────────────────────────────────────────────────────────

def _match_date(
    entry_date: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> bool:
    """检查条目日期是否在 [date_from, date_to] 范围内（均为 YYYY-MM-DD 字符串）。
    若条目无日期字段，视为通过过滤。
    """
    if not entry_date:
        return True
    d = str(entry_date)
    if date_from and d < date_from:
        return False
    if date_to and d > date_to:
        return False
    return True


# ─── MCP Server ──────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "diary-kb",
    instructions=(
        "Obsidian 个人日记知识库查询服务。"
        "通过各工具查询用户的属性、事件、人物关系和成就记录，"
        "结合查询结果给出个性化回应。"
    ),
)

# 启动时尝试加载 vault 路径，失败时记录错误信息，在工具调用时再抛出
VAULT_PATH: Optional[Path] = None
_vault_error: str = ""

try:
    VAULT_PATH = load_vault_path()
except Exception as _e:
    _vault_error = str(_e)


def _get_vault() -> Path:
    if VAULT_PATH is None:
        raise RuntimeError(f"Vault 路径加载失败: {_vault_error}")
    return VAULT_PATH


# ─── 6 个 MCP 工具 ────────────────────────────────────────────────────────────────

@mcp.tool()
def search_entries(
    query: str,
    dimension: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """全文搜索个人知识库条目。

    在所有字段的文本中匹配关键词，支持按维度和日期范围过滤。

    Args:
        query: 搜索关键词
        dimension: 限定维度，可选值：属性、事件、关系、成就。不填则搜索全部
        date_from: 起始日期 YYYY-MM-DD（仅对有日期字段的条目有效）
        date_to: 结束日期 YYYY-MM-DD
        limit: 最多返回条数，默认 20
    """
    vault = _get_vault()

    entries = (
        load_vault_entries(vault, dimension)
        if dimension
        else load_all_entries(vault)
    )

    query_lower = query.lower()
    results: list[dict] = []
    for entry in entries:
        entry_date = entry.get("日期") or entry.get("date")
        if not _match_date(entry_date, date_from, date_to):
            continue
        text = " ".join(str(v) for v in entry.values()).lower()
        if query_lower in text:
            results.append(entry)
        if len(results) >= limit:
            break

    return results


@mcp.tool()
def get_person(name: str) -> dict:
    """查询某个人物的关系信息及与其关联的历史事件。

    Args:
        name: 人物姓名，如"爸爸"、"小明"
    """
    vault = _get_vault()

    # 在关系维度查找人物信息
    relations = load_vault_entries(vault, "关系")
    person: Optional[dict] = None
    for r in relations:
        person_name = str(r.get("姓名", ""))
        if person_name == name or name in person_name:
            person = r
            break

    # 在事件维度查找关联事件
    events = load_vault_entries(vault, "事件")
    related_events: list[dict] = []
    for e in events:
        linked = e.get("关联人物", [])
        if isinstance(linked, list):
            if name in linked or any(name in str(p) for p in linked):
                related_events.append(e)
        elif isinstance(linked, str) and name in linked:
            related_events.append(e)

    return {
        "person": person,
        "related_events": related_events,
    }


@mcp.tool()
def get_events(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    event_type: Optional[str] = None,
    min_emotion: Optional[int] = None,
    core_only: bool = False,
    limit: int = 20,
) -> list[dict]:
    """查询并过滤事件列表。

    Args:
        date_from: 起始日期 YYYY-MM-DD
        date_to: 结束日期 YYYY-MM-DD
        event_type: 事件类型，可选：家庭、工作、情感、学习、娱乐
        min_emotion: 最低情感分（1-5），例如传入 4 可筛选开心或重要的事件
        core_only: 是否只返回"是否核心=true"的核心事件
        limit: 最多返回条数，默认 20
    """
    vault = _get_vault()
    events = load_vault_entries(vault, "事件")

    results: list[dict] = []
    for e in events:
        # 日期过滤
        if not _match_date(e.get("日期"), date_from, date_to):
            continue

        # 情感分过滤
        if min_emotion is not None:
            score = e.get("情感分")
            try:
                if score is None or float(score) < min_emotion:
                    continue
            except (TypeError, ValueError):
                continue

        # 事件类型过滤
        if event_type:
            types = e.get("类型", [])
            if isinstance(types, str):
                types = [types]
            if event_type not in types:
                continue

        # 核心事件过滤
        if core_only:
            is_core = e.get("是否核心")
            if is_core not in (True, "true", "True", 1):
                continue

        results.append(e)
        if len(results) >= limit:
            break

    return results


@mcp.tool()
def get_attributes(attribute_type: Optional[str] = None) -> list[dict]:
    """查询用户的个人属性（性格、状态、喜好等）。

    Args:
        attribute_type: 属性类型过滤，可选：性格、状态、喜好。不填则返回全部属性
    """
    vault = _get_vault()
    attrs = load_vault_entries(vault, "属性")

    if not attribute_type:
        return attrs

    return [a for a in attrs if a.get("属性类型") == attribute_type]


@mcp.tool()
def get_achievements(
    category: Optional[str] = None,
    min_importance: Optional[int] = None,
    limit: int = 20,
) -> list[dict]:
    """查询用户的成就记录。

    Args:
        category: 成就类别，可选：工作、学习、生活、社交、健康
        min_importance: 最低重要性分值（1-5），只返回重要性达到该阈值的成就
        limit: 最多返回条数，默认 20
    """
    vault = _get_vault()
    achievements = load_vault_entries(vault, "成就")

    results: list[dict] = []
    for a in achievements:
        if category and a.get("类别") != category:
            continue
        if min_importance is not None:
            imp = a.get("重要性")
            try:
                if imp is None or float(imp) < min_importance:
                    continue
            except (TypeError, ValueError):
                continue
        results.append(a)
        if len(results) >= limit:
            break

    return results


@mcp.tool()
def vault_summary() -> dict:
    """获取个人知识库的整体统计概览。

    返回各维度的条目总数及前几条条目标题示例，方便 AI 快速了解知识库规模。
    """
    vault = _get_vault()
    summary: dict = {}

    for dim in DIMENSION_FOLDERS:
        entries = load_vault_entries(vault, dim)
        samples: list[str] = []
        for e in entries[:3]:
            title = (
                e.get("标题")
                or e.get("姓名")
                or e.get("属性名")
                or e.get("_file", "")
            )
            samples.append(str(title))
        summary[dim] = {
            "count": len(entries),
            "samples": samples,
        }

    return {
        "vault_path": str(vault),
        "dimensions": summary,
        "total_entries": sum(v["count"] for v in summary.values()),
    }


# ─── 入口 ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Obsidian 日记知识库 MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python mcp_server.py              # stdio 模式（Claude Desktop / Cursor）
  python mcp_server.py --port 8000  # SSE HTTP 模式（浏览器调试）
""",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="启用 SSE HTTP 模式并监听指定端口（不填则使用 stdio 模式）",
    )
    args = parser.parse_args()

    if VAULT_PATH is None:
        print(f"[WARNING] {_vault_error}", file=sys.stderr)
        print("[WARNING] MCP Server 将启动，但工具调用会返回错误，请检查 config.yaml。", file=sys.stderr)

    if args.port:
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
