#!/usr/bin/env python3
"""日记知识库 AI 对话客户端

通过 OpenAI 兼容 API（DeepSeek / OpenAI / 本地模型等）+ Function Calling，
让任何 AI 都能访问 Obsidian 日记知识库，实现个性化对话。

工具定义从 diary 模板自动生成，无需启动 api_server.py。

用法:
  python chat_client.py                     # 使用 config.yaml 中的 AI 配置
  python chat_client.py --model deepseek-chat
  python chat_client.py --provider openai --model gpt-4o
  python chat_client.py --no-stream         # 关闭流式输出
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# 复用 api_server.py 中的 vault 读取逻辑
sys.path.insert(0, str(Path(__file__).parent))
from api_server import (
    load_config, load_vault_path, load_template,
    load_entries, apply_filters, get_folder,
)

try:
    from openai import OpenAI, Stream
except ImportError:
    print("[ERROR] 请安装 openai SDK: pip install openai", file=sys.stderr)
    sys.exit(1)


# ─── 维度名 → 英文 Tool Name 映射 ─────────────────────────────────────────────────
# OpenAI function name 只允许 [a-zA-Z0-9_-]，中文维度名需要转换

_DIM_TO_EN = {
    "属性": "attributes",
    "事件": "events",
    "关系": "relations",
    "成就": "achievements",
    "情感": "emotions",
    "人物": "persons",
    "习惯": "habits",
    "目标": "goals",
}


def _dim_tool_name(dim_name: str, idx: int) -> str:
    return f"query_{_DIM_TO_EN.get(dim_name, f'dim{idx}')}"


# ─── 工具定义生成（根据模板自动生成） ───────────────────────────────────────────────

def build_tools(template: dict) -> tuple[list[dict], dict[str, str]]:
    """
    生成 OpenAI function calling 格式的工具列表（小说角色扮演语境）。

    返回:
        tools:    工具定义列表，直接传给 client.chat.completions.create(tools=...)
        name_map: {tool_name → dim_name} 映射，用于执行时定位维度
    """
    dim_names = [d["name"] for d in template["dimensions"]]
    name_map: dict[str, str] = {}

    # ── 固定工具（小说语境描述） ───────────────────────────────────────────────────
    tools: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "vault_summary",
                "description": (
                    "了解故事全貌：各维度档案数量及样本。"
                    "开场前必须调用，以掌握整个故事的规模和维度。"
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_entries",
                "description": "在故事档案中搜索关键词，用于回溯情节、核实细节或定位特定场景。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "q": {"type": "string", "description": "搜索关键词（必填）"},
                        "dimension": {
                            "type": "string",
                            "description": f"限定维度（可选）: {' | '.join(dim_names)}",
                        },
                        "date_from": {"type": "string", "description": "故事内起始时间"},
                        "date_to":   {"type": "string", "description": "故事内截止时间"},
                        "limit":     {"type": "integer", "description": "最多返回条数，默认 10"},
                    },
                    "required": ["q"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_person",
                "description": (
                    "查询角色完整档案：性格描述、与主角关系、好感度及关联剧情事件。"
                    "扮演某个配角开口说话前必须调用，确保性格和语气一致。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "角色姓名"},
                    },
                    "required": ["name"],
                },
            },
        },
    ]

    # ── 动态维度工具（根据模板字段生成参数） ────────────────────────────────────────
    for idx, dim in enumerate(template["dimensions"]):
        tool_name = _dim_tool_name(dim["name"], idx)
        name_map[tool_name] = dim["name"]
        tools.append(_build_dim_tool(tool_name, dim))

    return tools, name_map


def _field_to_props(fields: list[dict]) -> dict:
    """将模板字段定义转为 JSON Schema properties。"""
    props: dict = {
        "q":     {"type": "string",  "description": "全文关键词搜索（可选）"},
        "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
    }
    for field in fields:
        fname, ftype = field["name"], field["type"]
        if ftype == "relation":
            continue
        elif ftype == "number":
            rng = f"[{field.get('min', '?')}-{field.get('max', '?')}]"
            props[f"min_{fname}"] = {"type": "number", "description": f"{fname} 最小值 {rng}"}
            props[f"max_{fname}"] = {"type": "number", "description": f"{fname} 最大值 {rng}"}
        elif ftype == "date":
            props[f"{fname}_from"] = {"type": "string", "description": f"{fname} 起始 YYYY-MM-DD"}
            props[f"{fname}_to"]   = {"type": "string", "description": f"{fname} 截止 YYYY-MM-DD"}
        elif ftype in ("select", "multiselect"):
            opts = field.get("options", [])
            desc = " | ".join(opts) if opts else fname
            if field.get("free"):
                desc += "（支持自定义）"
            props[fname] = {"type": "string", "description": desc}
        elif ftype == "boolean":
            props[fname] = {"type": "boolean", "description": fname}
    return props


_DIM_DESC = {
    "事件": "获取故事事件列表，按故事内时间顺序排列。推进剧情前调用，依次展开每个事件。",
    "人物": "获取所有角色档案列表，用于了解故事人物全貌。",
    "属性": "查询主角的性格特征、当前状态和喜好，用于准确描写主角内心和行为。",
    "成就": "查询主角经历的重要里程碑和成就，回顾关键成长节点时调用。",
    "关系": "查询角色关系网络，了解人物之间的纽带和矛盾。",
    "情感": "查询主角的情感记录，用于描写情绪变化时参考。",
}


def _build_dim_tool(tool_name: str, dim_def: dict) -> dict:
    desc = _DIM_DESC.get(
        dim_def["name"],
        f"查询【{dim_def['name']}】档案，所有参数均可选，可自由组合过滤。",
    )
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": _field_to_props(dim_def["fields"]),
                "required": [],
            },
        },
    }


# ─── 工具执行器 ───────────────────────────────────────────────────────────────────

class VaultExecutor:
    """将 AI 的 tool call 路由到对应的 vault 查询函数。"""

    def __init__(self, vault_path, config: dict, template: dict, name_map: dict[str, str]):
        self.vault_path = vault_path
        self.config = config
        self.dim_map = {d["name"]: d for d in template["dimensions"]}
        self.name_map = name_map
        self.relation_dim = next((d for d in template["dimensions"] if d["name"] == "关系"), None)
        self.event_dim    = next((d for d in template["dimensions"] if d["name"] == "事件"), None)

    def run(self, tool_name: str, args: dict) -> str:
        try:
            if tool_name == "vault_summary":
                result = self._summary()
            elif tool_name == "search_entries":
                result = self._search(args)
            elif tool_name == "get_person":
                result = self._person(args.get("name", ""))
            elif tool_name in self.name_map:
                result = self._query_dim(self.name_map[tool_name], args)
            else:
                result = {"error": f"未知工具: {tool_name}"}
        except Exception as e:
            result = {"error": str(e)}

        return json.dumps(result, ensure_ascii=False, default=str)

    def _summary(self) -> dict:
        dims = {}
        for dim_name, dim_def in self.dim_map.items():
            entries = load_entries(self.vault_path, get_folder(dim_name, self.config))
            title_keys = ["标题", "姓名", "属性名"]
            samples = [
                str(next((e[k] for k in title_keys if k in e), e.get("_file", "")))
                for e in entries[:3]
            ]
            dims[dim_name] = {"count": len(entries), "samples": samples}
        return {"dimensions": dims, "total": sum(v["count"] for v in dims.values())}

    def _search(self, args: dict) -> list:
        args = dict(args)  # 避免修改原始 dict
        dim_name  = args.pop("dimension", None)
        limit     = int(args.pop("limit", 10))
        q         = args.pop("q", "")
        date_from = args.pop("date_from", None)
        date_to   = args.pop("date_to", None)

        if dim_name and dim_name in self.dim_map:
            entries = load_entries(self.vault_path, get_folder(dim_name, self.config))
        else:
            entries = []
            for dn in self.dim_map:
                for e in load_entries(self.vault_path, get_folder(dn, self.config)):
                    e["_dimension"] = dn
                    entries.append(e)

        q_lower = q.lower()
        results = []
        for entry in entries:
            d = str(entry.get("日期", ""))
            if (date_from and d and d < date_from) or (date_to and d and d > date_to):
                continue
            if q_lower in " ".join(str(v) for v in entry.values()).lower():
                results.append(entry)
            if len(results) >= limit:
                break
        return results

    def _person(self, name: str) -> dict:
        profile = None
        if self.relation_dim:
            folder = get_folder(self.relation_dim["name"], self.config)
            for e in load_entries(self.vault_path, folder):
                pname = str(e.get("姓名", ""))
                if pname == name or name in pname:
                    profile = e
                    break

        related = []
        if self.event_dim:
            folder = get_folder(self.event_dim["name"], self.config)
            for e in load_entries(self.vault_path, folder):
                linked = e.get("关联人物", [])
                if isinstance(linked, str):
                    linked = [linked]
                if name in linked or any(name in str(p) for p in linked):
                    related.append(e)

        return {"profile": profile, "related_events": related}

    def _query_dim(self, dim_name: str, args: dict) -> list:
        args = dict(args)
        args.setdefault("limit", 10)
        dim_def = self.dim_map[dim_name]
        folder  = get_folder(dim_name, self.config)
        entries = load_entries(self.vault_path, folder)
        return apply_filters(entries, dim_def["fields"], args)


# ─── 系统提示词 ───────────────────────────────────────────────────────────────────

def build_system_prompt(template: dict) -> str:
    dim_names = "、".join(d["name"] for d in template["dimensions"])
    return f"""\
你是这部小说的沉浸式叙事引擎，故事档案包含以下维度：{dim_names}。
用户将以主人公"我"的视角参与故事，你负责扮演所有配角、描绘场景、按时间线推进完整剧情。

## 你的三重身份

**叙述者**：用第二人称（"你"）描写主角所见所感。每个场景先交代环境与氛围，再呈现事件，结尾留下钩子或悬念。

**配角扮演者**：扮演故事中的每一个配角。开口前必须调用 get_person 确认其性格、与主角的关系和好感度，确保语气、措辞与档案一致。配角说话用引号，绝不替主角开口。

**剧情引导者**：以事件档案为主线，按故事内时间顺序一幕一幕推进。每幕结束时根据剧情走向给出 2~3 个行动选项，让用户感受到选择的重量，但主线结果遵循档案。

## 工具使用节奏

- **开场**：先调用 vault_summary 掌握故事规模 → 再调用 query_events 获取全部事件列表 → 以第一个事件为起点，写出沉浸式开场白
- **推进新场景**：从事件列表中取下一个事件，必要时再次查询补充细节
- **登场新角色**：调用 get_person 获取档案再让其开口
- **用户提问时**：按需调用 search_entries 回溯情节，给出准确回顾

## 叙事风格

- 场景描写：环境 → 感官细节 → 情绪氛围，每层 1~2 句，避免流水账
- 对话节奏：配角先说，给用户留反应空间，不要连续推进两个配角的台词
- 情绪张力：参考事件的情感分（若有）决定叙述基调，高分场景写得温暖，低分场景写得紧张或沉重

## 特殊指令（用户输入时响应）

| 指令 | 你的行动 |
|------|---------|
| 跳过 | 用一段话摘要当前场景，直接跳至下一个事件 |
| 回顾 | 调用 query_events 汇总已经历的事件，列出时间线 |
| 认识XXX | 调用 get_person，以旁白口吻介绍该角色 |
| 重来 | 重新叙述当前场景，换一种开场方式 |

## 禁止事项

- 不在叙述中途解释工具调用过程，工具调用对用户不可见
- 不编造档案中没有的角色、事件或关系
- 不越权替主角做决定，选择权永远交给用户

---

现在请开始：调用工具了解故事全貌，然后以沉浸式开场白引入第一幕。
"""


# ─── 对话循环 ─────────────────────────────────────────────────────────────────────

def chat_loop(
    client: OpenAI,
    model: str,
    tools: list[dict],
    executor: VaultExecutor,
    system_prompt: str,
    stream: bool = True,
) -> None:
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    print("━" * 52)
    print(f"  📖 小说沉浸模式   模型: {model}")
    print("  特殊指令：跳过 / 回顾 / 认识XXX / 重来")
    print("  输入 quit 或按 Ctrl+C 退出故事")
    print("━" * 52 + "\n")

    while True:
        try:
            user_input = input("▶ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[故事暂停]")
            break

        if user_input.lower() in ("quit", "exit", "q", "退出"):
            print("[故事暂停]")
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        # 处理可能的多轮工具调用，用 used_tools 标记本轮是否经历过工具调用
        used_tools = False
        while True:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    stream=False,  # 工具调用阶段必须 non-stream，才能拿到结构化 tool_calls
                )
            except Exception as e:
                print(f"\n[API 错误] {e}\n")
                messages.pop()
                break

            msg = resp.choices[0].message

            if msg.tool_calls:
                used_tools = True
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    print(f"  [查档] {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
                    result = executor.run(tc.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            else:
                # 最终文本回复
                print()
                if stream and not used_tools:
                    # 纯聊天轮次（无工具调用）：用流式请求获得打字机效果
                    stream_resp = client.chat.completions.create(
                        model=model, messages=messages, stream=True,
                    )
                    chunks = []
                    for chunk in stream_resp:
                        delta = chunk.choices[0].delta.content or ""
                        print(delta, end="", flush=True)
                        chunks.append(delta)
                    content = "".join(chunks)
                else:
                    # 工具调用后：msg.content 已是完整回复，直接输出，不重复请求
                    content = msg.content or ""
                    print(content)

                print("\n")
                messages.append({"role": "assistant", "content": content})
                break


# ─── 入口 ─────────────────────────────────────────────────────────────────────────

_DEFAULT_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai":   "https://api.openai.com/v1",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="日记知识库 AI 对话客户端（DeepSeek / OpenAI / 本地模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python chat_client.py                        # 使用 config.yaml 的 AI 配置
  python chat_client.py --model deepseek-chat
  python chat_client.py --provider openai --model gpt-4o
  python chat_client.py --base-url http://localhost:11434/v1 --model llama3  # Ollama
""",
    )
    parser.add_argument("--provider", help="AI 提供商，覆盖 config.yaml 中的 api.provider")
    parser.add_argument("--model",    help="模型名称，覆盖 config.yaml 中的 api.model")
    parser.add_argument("--api-key",  help="API Key，覆盖 config.yaml 中的 api.api_key")
    parser.add_argument("--base-url", help="API Base URL，覆盖 config.yaml 中的 api.base_url")
    parser.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    args = parser.parse_args()

    try:
        config    = load_config()
        vault     = load_vault_path(config)
        template  = load_template(config)
    except Exception as e:
        print(f"[ERROR] 初始化失败: {e}", file=sys.stderr)
        sys.exit(1)

    api_cfg  = config.get("api", {})
    provider = args.provider or api_cfg.get("provider", "deepseek")
    model    = args.model    or api_cfg.get("model", "deepseek-chat")
    api_key  = args.api_key  or api_cfg.get("api_key", "")
    base_url = (
        args.base_url
        or (api_cfg.get("base_url") or "").rstrip("/")
        or _DEFAULT_URLS.get(provider, "https://api.deepseek.com")
    )

    if not api_key or api_key.startswith("sk-xxx"):
        print(f"[ERROR] 请在 config.yaml 的 api.api_key 填写有效的 {provider} API Key", file=sys.stderr)
        sys.exit(1)

    client   = OpenAI(api_key=api_key, base_url=base_url)
    tools, name_map = build_tools(template)
    executor = VaultExecutor(vault, config, template, name_map)
    system_prompt = build_system_prompt(template)

    chat_loop(client, model, tools, executor, system_prompt, stream=not args.no_stream)


if __name__ == "__main__":
    main()
