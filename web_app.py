#!/usr/bin/env python3
"""小说知识库 Web UI

三功能本地部署页面：
  1. 配置 API Key 和文件根目录
  2. 上传小说（≤13000字）并 AI 解析
  3. 选择目录开始沉浸式角色扮演聊天

启动：
  python web_app.py
  python web_app.py --port 7860

访问：http://localhost:7860
"""

import asyncio
import json
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
CONFIG_PATH = BASE_DIR / "config.yaml"

sys.path.insert(0, str(BASE_DIR))

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

app = FastAPI(title="小说知识库 Web UI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── 在线 chat 会话（内存） ────────────────────────────────────────────────────────
_sessions: dict[str, dict] = {}

_DEFAULT_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
}

# ── Config helpers ────────────────────────────────────────────────────────────────

def _read_cfg() -> dict:
    if not CONFIG_PATH.exists():
        return {"api": {}, "obsidian_vault_path": "", "diary_folder": "小说"}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_cfg(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


# ── API: /api/config ──────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_get_config():
    cfg = _read_cfg()
    api = cfg.get("api", {})
    raw = api.get("api_key", "")
    masked = (raw[:8] + "..." + raw[-4:]) if len(raw) > 12 else ("*" * len(raw))
    return {
        "vault_path":    cfg.get("obsidian_vault_path", ""),
        "diary_folder":  cfg.get("diary_folder", "小说"),
        "provider":      api.get("provider", "deepseek"),
        "model":         api.get("model", "deepseek-chat"),
        "api_key_masked": masked,
        "has_key":       bool(raw and not raw.startswith("sk-xxx")),
    }


@app.post("/api/config")
async def api_save_config(request: Request):
    body = await request.json()
    cfg = _read_cfg()

    if "vault_path" in body:
        cfg["obsidian_vault_path"] = body["vault_path"]
    if "diary_folder" in body:
        cfg["diary_folder"] = body["diary_folder"]

    cfg.setdefault("output_folders", {"属性": "属性", "事件": "事件", "关系": "关系", "成就": "成就"})
    cfg.setdefault("template_path", "templates/game_life.json")
    cfg.setdefault("processed_log", "processed_diaries.txt")

    api = cfg.get("api", {})
    if body.get("provider"):
        api["provider"] = body["provider"]
    if body.get("model"):
        api["model"] = body["model"]
    raw_key = body.get("api_key", "").strip()
    if raw_key and raw_key not in ("sk-xxx...", ""):
        api["api_key"] = raw_key
    cfg["api"] = api

    _write_cfg(cfg)
    _sessions.clear()
    return {"ok": True}


# ── API: /api/novel ───────────────────────────────────────────────────────────────

@app.get("/api/novel/list")
def api_novel_list():
    cfg = _read_cfg()
    d = Path(cfg.get("obsidian_vault_path", "")) / cfg.get("diary_folder", "小说")
    if not d.exists():
        return []
    return [f.stem for f in sorted(d.glob("*.md"))]


@app.post("/api/novel/parse")
async def api_parse_novel(
    novel_name: str = Form(...),
    protagonist: str = Form("我"),
    plot_count:  int = Form(10),
    content:     str = Form(...),
):
    if len(content) > 13000:
        raise HTTPException(400, f"超出 13000 字上限（当前 {len(content)} 字）")

    cfg = _read_cfg()
    vault = Path(cfg.get("obsidian_vault_path", ""))
    if not vault.exists():
        raise HTTPException(400, f"Vault 路径不存在：{vault}，请先完成配置")

    src_dir = vault / cfg.get("diary_folder", "小说")
    src_dir.mkdir(parents=True, exist_ok=True)
    novel_file = src_dir / f"{novel_name}.md"
    novel_file.write_text(content, encoding="utf-8")

    cmd = [
        sys.executable, str(BASE_DIR / "main_novel.py"),
        "--file", str(novel_file),
        "--protagonist", protagonist,
        "--plot-count", str(plot_count),
        "--force",
    ]

    async def stream_events():
        def evt(d: dict) -> str:
            return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"

        yield evt({"type": "log", "text": f"✎ 已保存 {novel_file.name}（{len(content)} 字）"})
        yield evt({"type": "log", "text": f"主人公：{protagonist}　情节数量：{plot_count}"})
        yield evt({"type": "log", "text": "─" * 48})

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE_DIR),
        )
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                yield evt({"type": "log", "text": line})

        await proc.wait()
        if proc.returncode == 0:
            yield evt({"type": "done", "text": "✓ 解析完成，可前往「开始聊天」体验剧情"})
        else:
            yield evt({"type": "error", "text": f"✗ 解析失败（退出码 {proc.returncode}）"})

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── API: /api/chat ────────────────────────────────────────────────────────────────

def _init_session(sid: str, vault_dir: str) -> dict:
    from api_server import load_template
    from chat_client_novel import build_tools, build_system_prompt, VaultExecutor

    cfg = _read_cfg()
    vault_path = Path(vault_dir)
    if not vault_path.exists():
        raise ValueError(f"目录不存在：{vault_dir}")

    template = load_template(cfg)
    tools, name_map = build_tools(template)
    executor = VaultExecutor(vault_path, cfg, template, name_map)
    system_prompt = build_system_prompt(template)

    _sessions[sid] = {
        "vault_dir": vault_dir,
        "messages":  [{"role": "system", "content": system_prompt}],
        "tools":     tools,
        "executor":  executor,
    }
    return _sessions[sid]


@app.post("/api/chat/reset")
async def api_chat_reset(request: Request):
    body = await request.json()
    sid = body.get("session_id", "default")
    vault_dir = body.get("vault_dir", "")
    _sessions.pop(sid, None)
    if vault_dir:
        try:
            _init_session(sid, vault_dir)
        except Exception as e:
            raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/chat/message")
async def api_chat_message(
    message:    str = Form(...),
    vault_dir:  str = Form(...),
    session_id: str = Form("default"),
):
    if OpenAI is None:
        raise HTTPException(500, "请安装 openai：pip install openai")

    cfg = _read_cfg()
    api = cfg.get("api", {})
    api_key  = api.get("api_key", "")
    provider = api.get("provider", "deepseek")
    model    = api.get("model", "deepseek-chat")
    base_url = (api.get("base_url") or "").rstrip("/") or _DEFAULT_URLS.get(provider, "https://api.deepseek.com")

    if not api_key or api_key.startswith("sk-xxx"):
        raise HTTPException(400, "API Key 未配置，请先在「配置」页面设置")

    session = _sessions.get(session_id)
    if not session or session.get("vault_dir") != vault_dir:
        try:
            session = _init_session(session_id, vault_dir)
        except Exception as e:
            raise HTTPException(400, str(e))

    session["messages"].append({"role": "user", "content": message})
    client = OpenAI(api_key=api_key, base_url=base_url)

    async def stream_events():
        def evt(d: dict) -> str:
            return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"

        used_tools = False
        while True:
            try:
                resp = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=model,
                    messages=session["messages"],
                    tools=session["tools"],
                    tool_choice="auto",
                    stream=False,
                )
            except Exception as e:
                session["messages"].pop()
                yield evt({"type": "error", "text": str(e)})
                return

            msg = resp.choices[0].message

            if msg.tool_calls:
                used_tools = True
                session["messages"].append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    yield evt({"type": "tool", "name": tc.function.name, "args": args})
                    result = await asyncio.to_thread(session["executor"].run, tc.function.name, args)
                    session["messages"].append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result}
                    )
            else:
                content = msg.content or ""
                session["messages"].append({"role": "assistant", "content": content})
                yield evt({"type": "content", "text": content})
                yield evt({"type": "done"})
                return

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Main page ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = WEB_DIR / "index.html"
    if not html.exists():
        return HTMLResponse("<h1>web/index.html not found — run setup first</h1>", status_code=404)
    return HTMLResponse(html.read_text(encoding="utf-8"))


# ── Entry ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import uvicorn

    p = argparse.ArgumentParser(description="小说知识库 Web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7860)
    a = p.parse_args()

    WEB_DIR.mkdir(exist_ok=True)
    print(f"🌐  http://{a.host}:{a.port}")
    uvicorn.run(app, host=a.host, port=a.port)
