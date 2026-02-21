#!/usr/bin/env python3
"""AI 智能日记拆分工具

将 Obsidian 库中的日记文件自动拆分为结构化 Markdown 条目。
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
import yaml


# ─── 日志 ──────────────────────────────────────────────────────────────────────

def setup_logging(log_file: Optional[str] = None) -> None:
    logger = logging.getLogger("diary_splitter")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)


def get_logger() -> logging.Logger:
    return logging.getLogger("diary_splitter")


# ─── 配置 ──────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config.yaml") -> dict:
    path = Path(config_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    if not path.exists():
        raise FileNotFoundError(f"配置文件未找到: {path}")
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    required_keys = ["obsidian_vault_path", "diary_folder", "template_path", "api"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"配置文件缺少必要字段: {key}")

    vault_path = Path(config["obsidian_vault_path"])
    if not vault_path.exists():
        raise FileNotFoundError(f"Obsidian 库路径不存在: {vault_path}")

    api_key = config["api"].get("api_key", "")
    if not api_key or api_key.startswith("sk-xxx"):
        raise ValueError("请在 config.yaml 中配置有效的 api.api_key")


# ─── 模板 ──────────────────────────────────────────────────────────────────────

def load_template(config: dict) -> dict:
    """按优先级加载模板：vault 内 → 脚本目录 templates/"""
    vault_path = Path(config["obsidian_vault_path"])
    template_rel = config["template_path"]

    candidates = [
        vault_path / template_rel,
        Path(__file__).parent / template_rel,
        Path(__file__).parent / "templates" / Path(template_rel).name,
    ]
    for p in candidates:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return json.load(f)

    raise FileNotFoundError(
        f"模板文件未找到，已尝试路径: {[str(c) for c in candidates]}"
    )


# ─── 处理记录 ──────────────────────────────────────────────────────────────────

def _log_path(config: dict) -> Path:
    rel = config.get("processed_log", "processed_diaries.txt")
    p = Path(rel)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    return p


def load_processed(config: dict) -> set:
    p = _log_path(config)
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def record_processed(config: dict, entry: str) -> None:
    p = _log_path(config)
    with open(p, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def make_log_key(diary_path: Path) -> str:
    return f"{diary_path.name}:{int(diary_path.stat().st_mtime)}"


# ─── 日记扫描 ──────────────────────────────────────────────────────────────────

def scan_new_diaries(config: dict, processed: set) -> list:
    vault_path = Path(config["obsidian_vault_path"])
    diary_folder = vault_path / config["diary_folder"]

    if not diary_folder.exists():
        diary_folder.mkdir(parents=True, exist_ok=True)
        get_logger().info(f"已创建日记文件夹: {diary_folder}")
        return []

    new_files = []
    for md_file in sorted(diary_folder.glob("*.md")):
        if make_log_key(md_file) not in processed:
            new_files.append(md_file)
    return new_files


# ─── 提示词 ────────────────────────────────────────────────────────────────────

def build_prompt(diary_text: str, template: dict) -> str:
    template_desc = json.dumps(template, ensure_ascii=False, indent=2)
    return f"""你是一个专业的日记分析助手。请根据以下模板，将用户的日记内容拆分为结构化数据。

## 模板定义
{template_desc}

## 输出要求
1. 严格按照模板中的维度(dimensions)和字段(fields)进行拆分。
2. 输出必须是合法的 JSON 对象，顶层 key 为维度名称，value 为该维度条目的数组。
3. required=true 的字段必须填写。
4. select/multiselect 类型从 options 中选择（free=true 时可追加自定义值）。
5. boolean 类型用 true/false，date 类型格式 YYYY-MM-DD，number 类型为数字。
6. 若日记中没有对应维度的信息，返回空数组 []。
7. 仅输出 JSON，不要有任何解释或 markdown 代码块包裹。

## 日记内容
{diary_text}

## JSON 输出："""


# ─── API 调用 ──────────────────────────────────────────────────────────────────

_DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
}


def call_api(prompt: str, config: dict, max_retries: int = 3) -> str:
    api_cfg = config["api"]
    provider = api_cfg.get("provider", "deepseek")
    base_url = (api_cfg.get("base_url") or "").rstrip("/") or _DEFAULT_BASE_URLS.get(provider, "https://api.deepseek.com")
    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": api_cfg.get("model", "deepseek-chat"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }

    logger = get_logger()
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.HTTPError as e:
            logger.warning(f"API HTTP 错误（第 {attempt} 次）: {e} - {resp.text[:200]}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"API 请求失败（第 {attempt} 次）: {e}")

        if attempt < max_retries:
            wait = 2 ** attempt
            logger.info(f"等待 {wait}s 后重试...")
            time.sleep(wait)

    raise RuntimeError(f"API 调用失败，已重试 {max_retries} 次")


# ─── JSON 解析 ─────────────────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    raw = raw.strip()
    # 去除 markdown 代码块包裹
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)
    raw = raw.strip()
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError(f"期望 JSON 对象，实际得到: {type(result).__name__}")
        return result
    except json.JSONDecodeError as e:
        raise ValueError(f"无法解析 AI 返回的 JSON: {e}\n原始内容（前500字）:\n{raw[:500]}")


# ─── Markdown 生成 ─────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\n\r\t]', "", name)
    name = name.strip(". ")
    return name or "未命名"


def _output_folder(dimension_name: str, config: dict, vault_path: Path) -> Path:
    mapping = config.get("output_folders") or {}
    folder_name = mapping.get(dimension_name, dimension_name)
    folder = vault_path / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _format_yaml_value(value: Any, field_type: str) -> str:
    """将 Python 值格式化为 YAML frontmatter 行的值部分。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = "\n".join(f"  - {v}" for v in value)
        return f"\n{items}"
    if isinstance(value, str):
        # 需要引号的情况
        if any(c in value for c in [':', '#', '[', ']', '{', '}', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`', '"', "'"]):
            escaped = value.replace('"', '\\"')
            return f'"{escaped}"'
        if value.lower() in ("true", "false", "null", "yes", "no"):
            return f'"{value}"'
        if "\n" in value:
            indented = "\n".join(f"  {line}" for line in value.split("\n"))
            return f"|\n{indented}"
    return str(value)


def generate_entry_file(
    entry: dict,
    dimension: dict,
    diary_date: str,
    config: dict,
    vault_path: Path,
    seq: int,
) -> Path:
    field_map = {f["name"]: f for f in dimension["fields"]}

    # 文件名
    title = (
        entry.get("标题")
        or entry.get("事件标题")
        or entry.get("姓名")
        or entry.get("属性名")
        or ""
    )
    if title:
        filename = f"{diary_date}-{sanitize_filename(str(title))}.md"
    else:
        filename = f"{diary_date}-{seq}.md"

    output_dir = _output_folder(dimension["name"], config, vault_path)

    # 唯一性保证
    file_path = output_dir / filename
    counter = 1
    while file_path.exists():
        stem = Path(filename).stem
        file_path = output_dir / f"{stem}-{counter}.md"
        counter += 1

    # 构建 frontmatter
    frontmatter_lines = ["---"]
    body_text = ""

    for field_name, value in entry.items():
        if value is None or value == "" or value == []:
            continue

        field_def = field_map.get(field_name, {})
        field_type = field_def.get("type", "text")

        # 描述字段放入正文
        if field_name == "描述" and isinstance(value, str):
            body_text = value
            continue

        # relation 类型转双链
        if field_type == "relation":
            if isinstance(value, list):
                linked = [f"[[{v}]]" for v in value]
                formatted = _format_yaml_value(linked, field_type)
            else:
                formatted = f"[[{value}]]"
        else:
            formatted = _format_yaml_value(value, field_type)

        frontmatter_lines.append(f"{field_name}: {formatted}")

    frontmatter_lines.append("---")

    content = "\n".join(frontmatter_lines) + "\n"
    if body_text:
        content += f"\n{body_text}\n"

    # 原子写入
    tmp = file_path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(file_path)

    return file_path


# ─── 处理单篇日记 ──────────────────────────────────────────────────────────────

def process_diary(
    diary_path: Path,
    config: dict,
    template: dict,
    dry_run: bool = False,
) -> bool:
    logger = get_logger()
    logger.info(f"处理日记: {diary_path.name}")

    diary_text = diary_path.read_text(encoding="utf-8").strip()
    if not diary_text:
        logger.warning(f"日记内容为空，跳过: {diary_path.name}")
        return True

    # 从文件名提取日期，否则用今天
    stem = diary_path.stem
    diary_date = stem if re.match(r"\d{4}-\d{2}-\d{2}", stem) else datetime.now().strftime("%Y-%m-%d")

    prompt = build_prompt(diary_text, template)

    if dry_run:
        logger.info("[DRY RUN] 跳过 API 调用，提示词已生成")
        return True

    try:
        raw = call_api(prompt, config)
        structured = extract_json(raw)
    except Exception as e:
        logger.error(f"AI 拆分失败: {e}")
        return False

    vault_path = Path(config["obsidian_vault_path"])
    dim_map = {d["name"]: d for d in template["dimensions"]}
    generated_count = 0

    for dim_name, entries in structured.items():
        if not isinstance(entries, list) or not entries:
            continue
        dimension = dim_map.get(dim_name)
        if not dimension:
            logger.warning(f"模板中未定义维度 '{dim_name}'，跳过")
            continue

        for seq, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            try:
                fp = generate_entry_file(entry, dimension, diary_date, config, vault_path, seq)
                logger.info(f"  ✓ {fp.relative_to(vault_path)}")
                generated_count += 1
            except Exception as e:
                logger.error(f"  ✗ 生成条目失败 [{dim_name}#{seq}]: {e}")

    logger.info(f"共生成 {generated_count} 个条目")
    return True


# ─── 入口 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI 智能日记拆分工具 - 将 Obsidian 日记自动拆分为结构化条目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py                    # 处理所有新日记
  python main.py --dry-run          # 仅扫描，不调用 API
  python main.py --force            # 强制重新处理所有日记
  python main.py --file 日记/2026-02-21.md  # 处理单个文件（相对 vault）
  python main.py --config my.yaml   # 使用自定义配置文件
""",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认: config.yaml）")
    parser.add_argument("--dry-run", action="store_true", help="仅扫描文件，不调用 API 也不生成文件")
    parser.add_argument("--force", action="store_true", help="忽略处理记录，强制重新处理所有日记")
    parser.add_argument("--file", metavar="PATH", help="仅处理指定的单个日记文件（相对 vault 或绝对路径）")
    args = parser.parse_args()

    # 加载配置
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"[ERROR] 配置加载失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 初始化日志
    log_file = config.get("log_file")
    if log_file:
        log_file_path = Path(__file__).parent / log_file
        setup_logging(str(log_file_path))
    else:
        setup_logging()

    logger = get_logger()
    logger.info("=== AI 智能日记拆分工具启动 ===")

    # 加载模板
    try:
        template = load_template(config)
        logger.info(f"已加载模板: {template['name']}")
    except Exception as e:
        logger.error(f"模板加载失败: {e}")
        sys.exit(1)

    # 确定待处理文件列表
    processed = set() if args.force else load_processed(config)
    vault_path = Path(config["obsidian_vault_path"])

    if args.file:
        file_path = Path(args.file)
        if not file_path.is_absolute():
            file_path = vault_path / args.file
        if not file_path.exists():
            logger.error(f"指定文件不存在: {file_path}")
            sys.exit(1)
        diary_files = [file_path]
    else:
        diary_files = scan_new_diaries(config, processed)

    if not diary_files:
        logger.info("没有需要处理的新日记。")
        return

    logger.info(f"找到 {len(diary_files)} 篇待处理日记")

    # 逐篇处理
    success, fail = 0, 0
    for diary_path in diary_files:
        ok = process_diary(diary_path, config, template, dry_run=args.dry_run)
        if ok:
            if not args.dry_run:
                record_processed(config, make_log_key(diary_path))
            success += 1
        else:
            fail += 1

    logger.info(f"=== 完成: 成功 {success} 篇，失败 {fail} 篇 ===")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
