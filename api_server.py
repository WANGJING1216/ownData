#!/usr/bin/env python3
"""Obsidian 日记知识库 REST API Server

根据 diary template 自动生成查询端点与 AI 友好接口文档。

运行:
  python api_server.py                         # 默认 127.0.0.1:8000
  python api_server.py --port 3000             # 自定义端口
  python api_server.py --host 0.0.0.0          # 外部可访问

接口说明:
  /docs      Swagger UI（交互式调试）
  /ai-docs   AI 友好文档（可直接粘贴进系统提示词）
"""

import argparse
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Optional

import uvicorn
import yaml
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse


# ─── 配置与 Vault 读取 ────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件未找到: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_vault_path(config: dict) -> Path:
    vault = Path(config.get("obsidian_vault_path", ""))
    if not vault.exists():
        raise FileNotFoundError(
            f"Vault 路径不存在: {vault}\n请检查 {CONFIG_PATH} 中的 obsidian_vault_path"
        )
    return vault


def load_template(config: dict) -> dict:
    template_rel = config.get("template_path", "templates/game_life.json")
    candidates = [
        Path(config["obsidian_vault_path"]) / template_rel,
        Path(__file__).parent / template_rel,
        Path(__file__).parent / "templates" / Path(template_rel).name,
    ]
    for p in candidates:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(f"模板文件未找到，已尝试: {candidates}")


def get_folder(dim_name: str, config: dict) -> str:
    return config.get("output_folders", {}).get(dim_name, dim_name)


def _strip_wikilinks(value):
    if isinstance(value, str):
        return re.sub(r"\[\[(.+?)\]\]", r"\1", value)
    if isinstance(value, list):
        return [re.sub(r"\[\[(.+?)\]\]", r"\1", str(v)) for v in value]
    return value


def _parse_md(file_path: Path) -> dict:
    content = file_path.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    frontmatter_raw, body = (parts[1], parts[2].strip()) if len(parts) >= 3 else ("", content.strip())
    try:
        data = yaml.safe_load(frontmatter_raw) or {}
    except yaml.YAMLError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    cleaned = {k: _strip_wikilinks(v) for k, v in data.items()}
    if body:
        cleaned["描述"] = body
    return cleaned


def load_entries(vault_path: Path, folder_name: str) -> list[dict]:
    folder = vault_path / folder_name
    if not folder.exists():
        return []
    results = []
    for f in sorted(folder.glob("*.md")):
        try:
            d = _parse_md(f)
            d["_file"] = f.name
            results.append(d)
        except Exception:
            continue
    return results


# ─── 通用过滤引擎 ─────────────────────────────────────────────────────────────────

def apply_filters(entries: list[dict], fields: list[dict], kwargs: dict) -> list[dict]:
    """根据模板字段定义和 kwargs 过滤条目列表。"""
    q = (kwargs.get("q") or "").lower()
    limit = int(kwargs.get("limit") or 20)
    results = []

    for entry in entries:
        # 全文搜索
        if q and q not in " ".join(str(v) for v in entry.values()).lower():
            continue

        skip = False
        for field in fields:
            fname, ftype = field["name"], field["type"]
            if ftype == "relation":
                continue

            if ftype == "number":
                min_v, max_v = kwargs.get(f"min_{fname}"), kwargs.get(f"max_{fname}")
                if min_v is not None or max_v is not None:
                    try:
                        ev = float(entry[fname]) if fname in entry else None
                        if ev is None or (min_v is not None and ev < float(min_v)) or \
                                         (max_v is not None and ev > float(max_v)):
                            skip = True; break
                    except (TypeError, ValueError):
                        skip = True; break

            elif ftype == "date":
                from_v, to_v = kwargs.get(f"{fname}_from"), kwargs.get(f"{fname}_to")
                if from_v or to_v:
                    d = str(entry.get(fname, ""))
                    if (from_v and d < from_v) or (to_v and d > to_v):
                        skip = True; break

            elif ftype in ("select", "multiselect"):
                fv = kwargs.get(fname)
                if fv:
                    ev = entry.get(fname, [])
                    if isinstance(ev, str):
                        ev = [ev]
                    if str(fv) not in [str(x) for x in ev]:
                        skip = True; break

            elif ftype == "boolean":
                fv = kwargs.get(fname)
                if fv is not None:
                    ev = entry.get(fname)
                    normalized = ev in (True, "true", "True", 1, "1")
                    if bool(fv) != normalized:
                        skip = True; break

        if not skip:
            results.append(entry)
        if len(results) >= limit:
            break

    return results


# ─── 动态端点生成 ─────────────────────────────────────────────────────────────────

def _build_params(fields: list[dict]) -> list[inspect.Parameter]:
    """将模板字段定义转换为 FastAPI Query 参数列表（供 __signature__ 使用）。"""
    params = [
        inspect.Parameter("q", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=Query(None, description="全文关键词搜索"),
                          annotation=Optional[str]),
        inspect.Parameter("limit", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=Query(20, ge=1, le=500, description="最多返回条数"),
                          annotation=int),
    ]
    for field in fields:
        fname, ftype = field["name"], field["type"]
        if ftype == "relation":
            continue

        if ftype == "number":
            rng = f"[{field.get('min', '?')}-{field.get('max', '?')}]"
            params += [
                inspect.Parameter(f"min_{fname}", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=Query(None, description=f"{fname} 最小值 {rng}"),
                                  annotation=Optional[float]),
                inspect.Parameter(f"max_{fname}", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=Query(None, description=f"{fname} 最大值 {rng}"),
                                  annotation=Optional[float]),
            ]
        elif ftype == "date":
            params += [
                inspect.Parameter(f"{fname}_from", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=Query(None, description=f"{fname} 起始日期 YYYY-MM-DD"),
                                  annotation=Optional[str]),
                inspect.Parameter(f"{fname}_to", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  default=Query(None, description=f"{fname} 截止日期 YYYY-MM-DD"),
                                  annotation=Optional[str]),
            ]
        elif ftype in ("select", "multiselect"):
            opts = field.get("options", [])
            desc = f"{'|'.join(opts)}" + ("（支持自定义）" if field.get("free") else "")
            params.append(inspect.Parameter(fname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                            default=Query(None, description=desc),
                                            annotation=Optional[str]))
        elif ftype == "boolean":
            params.append(inspect.Parameter(fname, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                            default=Query(None, description="true / false"),
                                            annotation=Optional[bool]))
        # text 类型：通过 q 全文搜索即可，不单独生成参数

    return params


def make_handler(dim_def: dict, vault_path: Path, folder_name: str):
    """为一个维度创建带正确类型签名的 FastAPI handler。"""
    fields = dim_def["fields"]
    name = dim_def["name"]
    params = _build_params(fields)

    async def handler(**kwargs):
        entries = load_entries(vault_path, folder_name)
        return apply_filters(entries, fields, kwargs)

    handler.__signature__ = inspect.Signature(params)
    handler.__name__ = f"query_{name}"
    handler.__doc__ = f"查询【{name}】条目，所有参数均可选，可自由组合。"
    return handler


# ─── AI 友好文档生成 ──────────────────────────────────────────────────────────────

def build_ai_docs(template: dict, config: dict, base_url: str) -> str:
    dim_names = [d["name"] for d in template["dimensions"]]
    lines = [
        f"# {template.get('name', '日记知识库')} — API 接口文档",
        f"\nBase URL: {base_url}",
        "协议: HTTP GET，所有参数为 URL Query String，可自由组合。\n",
        "=" * 60,
    ]

    # ── 固定端点 ──
    lines += [
        "\n## GET /summary",
        "知识库整体概览：各维度条目数量及样本标题。",
        "参数: 无\n",

        f"## GET /search",
        "跨维度全文搜索。",
        f"  q         (string, 必填)  搜索关键词",
        f"  dimension (string, 可选)  限定维度: {' | '.join(dim_names)}",
        f"  date_from (string, 可选)  起始日期 YYYY-MM-DD",
        f"  date_to   (string, 可选)  截止日期 YYYY-MM-DD",
        f"  limit     (int,    默认20) 最多返回条数\n",

        "## GET /person/{{name}}",
        "查询人物档案及其关联历史事件。",
        "  name (路径参数) 人物姓名，如: 爸爸、妈妈\n",

        "## GET /ai-docs",
        "返回本文档（即当前内容）。",
        "  base_url (string, 可选) 自定义 Base URL\n",

        "=" * 60,
        "\n## 维度端点（根据模板自动生成）\n",
    ]

    # ── 动态维度端点 ──
    for dim in template["dimensions"]:
        dname = dim["name"]
        lines.append(f"### GET /{dname}")
        lines.append(f"查询【{dname}】维度的所有条目。")
        lines.append(f"  q     (string, 可选)  全文关键词搜索")
        lines.append(f"  limit (int,    默认20) 最多返回条数")

        for field in dim["fields"]:
            fname, ftype = field["name"], field["type"]
            if ftype == "relation":
                continue
            if ftype == "number":
                rng = f"[{field.get('min','?')}-{field.get('max','?')}]"
                lines.append(f"  min_{fname} (float, 可选)  {fname} 最小值 {rng}")
                lines.append(f"  max_{fname} (float, 可选)  {fname} 最大值 {rng}")
            elif ftype == "date":
                lines.append(f"  {fname}_from (string, 可选)  {fname} 起始 YYYY-MM-DD")
                lines.append(f"  {fname}_to   (string, 可选)  {fname} 截止 YYYY-MM-DD")
            elif ftype in ("select", "multiselect"):
                opts = field.get("options", [])
                opt_str = " | ".join(opts) if opts else "自由输入"
                if field.get("free"):
                    opt_str += "（支持自定义）"
                lines.append(f"  {fname} (string, 可选)  {opt_str}")
            elif ftype == "boolean":
                lines.append(f"  {fname} (bool, 可选)  true / false")

        lines.append("")

    # ── 使用示例 ──
    lines += [
        "=" * 60,
        "\n## 使用示例\n",
        f"  # 查询情感分 >= 4 的近期开心事件",
        f"  GET {base_url}/事件?min_情感分=4&limit=10\n",
        f"  # 只看核心事件",
        f"  GET {base_url}/事件?是否核心=true\n",
        f"  # 查看所有喜好类属性",
        f"  GET {base_url}/属性?属性类型=喜好\n",
        f"  # 搜索包含「旅行」的所有记录",
        f"  GET {base_url}/search?q=旅行\n",
        f"  # 查询爸爸的关系档案和关联事件",
        f"  GET {base_url}/person/爸爸\n",
        f"  # 查询重要性 >= 4 的工作成就",
        f"  GET {base_url}/成就?类别=工作&min_重要性=4\n",
        "=" * 60,
        "\n## AI 使用原则\n",
        "1. 用户提问前先调用相关端点获取真实数据，不要凭空猜测",
        "2. 多个过滤条件可以组合，例如同时指定日期范围和情感分",
        "3. 先调 /summary 了解知识库规模，再决定调哪个端点",
        "4. 如果查询结果为空，如实告知，不要编造信息",
    ]

    return "\n".join(lines)


# ─── 构建 FastAPI App ─────────────────────────────────────────────────────────────

def create_app(vault_path: Path, config: dict, template: dict) -> FastAPI:
    title = template.get("name", "日记知识库")
    app = FastAPI(
        title=f"{title} API",
        description="根据 Obsidian 日记模板自动生成的个人知识库查询接口",
        version="1.0.0",
    )

    dim_map = {d["name"]: d for d in template["dimensions"]}
    relation_dim = next((d for d in template["dimensions"] if d["name"] == "关系"), None)
    event_dim = next((d for d in template["dimensions"] if d["name"] == "事件"), None)
    dim_names = list(dim_map.keys())

    # ── /summary ──────────────────────────────────────────────────────────────────
    @app.get("/summary", summary="知识库概览", tags=["通用"])
    def summary():
        """返回各维度条目数量及前 3 条样本标题，无需参数。"""
        result = {}
        for dim in template["dimensions"]:
            folder = get_folder(dim["name"], config)
            entries = load_entries(vault_path, folder)
            title_keys = ["标题", "姓名", "属性名"]
            samples = [str(next((e[k] for k in title_keys if k in e), e.get("_file", "")))
                       for e in entries[:3]]
            result[dim["name"]] = {"count": len(entries), "samples": samples}
        return {
            "vault_path": str(vault_path),
            "dimensions": result,
            "total_entries": sum(v["count"] for v in result.values()),
        }

    # ── /search ───────────────────────────────────────────────────────────────────
    @app.get("/search", summary="全文搜索", tags=["通用"])
    def search(
        q: str = Query(..., description="搜索关键词"),
        dimension: Optional[str] = Query(None, description="限定维度: " + " | ".join(dim_names)),
        date_from: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD"),
        limit: int = Query(20, ge=1, le=500, description="最多返回条数"),
    ):
        """全文搜索所有维度或指定维度的条目。"""
        if dimension:
            if dimension not in dim_map:
                return {"error": f"未知维度: {dimension}，可选: {dim_names}"}
            entries = load_entries(vault_path, get_folder(dimension, config))
            for e in entries:
                e["_dimension"] = dimension
        else:
            entries = []
            for dim in template["dimensions"]:
                for e in load_entries(vault_path, get_folder(dim["name"], config)):
                    e["_dimension"] = dim["name"]
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

    # ── /person/{name} ────────────────────────────────────────────────────────────
    @app.get("/person/{name}", summary="人物详情", tags=["通用"])
    def person(name: str):
        """查询人物关系档案及该人物关联的历史事件。"""
        profile = None
        if relation_dim:
            for e in load_entries(vault_path, get_folder(relation_dim["name"], config)):
                pname = str(e.get("姓名", ""))
                if pname == name or name in pname:
                    profile = e
                    break

        related = []
        if event_dim:
            for e in load_entries(vault_path, get_folder(event_dim["name"], config)):
                linked = e.get("关联人物", [])
                if isinstance(linked, str):
                    linked = [linked]
                if name in linked or any(name in str(p) for p in linked):
                    related.append(e)

        return {"profile": profile, "related_events": related}

    # ── /ai-docs ──────────────────────────────────────────────────────────────────
    @app.get("/ai-docs", response_class=PlainTextResponse, summary="AI 友好接口文档", tags=["通用"])
    def ai_docs(
        base_url: str = Query("http://localhost:8000", description="API 服务的 Base URL")
    ):
        """
        返回适合直接粘贴进 AI 系统提示词的纯文本接口文档。
        文档内容根据当前加载的模板动态生成，反映实际可用的端点和参数。
        """
        return build_ai_docs(template, config, base_url)

    # ── 动态维度端点（根据模板自动生成） ──────────────────────────────────────────
    for dim in template["dimensions"]:
        dname = dim["name"]
        handler = make_handler(dim, vault_path, get_folder(dname, config))
        app.add_api_route(
            f"/{dname}",
            handler,
            methods=["GET"],
            summary=f"查询{dname}",
            tags=[dname],
        )

    return app


# ─── 入口 ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Obsidian 日记知识库 REST API Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python api_server.py                          # 默认 127.0.0.1:8000
  python api_server.py --port 3000              # 自定义端口
  python api_server.py --host 0.0.0.0 --port 8000  # 外部可访问

接口地址:
  http://HOST:PORT/docs      Swagger UI（交互式调试）
  http://HOST:PORT/ai-docs   AI 友好接口文档（直接粘贴进系统提示词）
""",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更时自动重启")
    args = parser.parse_args()

    try:
        config = load_config()
        vault_path = load_vault_path(config)
        template = load_template(config)
    except Exception as e:
        print(f"[ERROR] 初始化失败: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Vault:  {vault_path}")
    print(f"模板:   {template.get('name')}  维度: {[d['name'] for d in template['dimensions']]}")
    print(f"Swagger UI:  http://{args.host}:{args.port}/docs")
    print(f"AI 文档:     http://{args.host}:{args.port}/ai-docs")
    print()

    app = create_app(vault_path, config, template)
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
