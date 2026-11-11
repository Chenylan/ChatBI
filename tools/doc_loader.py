"""Markdown 语料加载：解析 YAML front-matter，返回 LangChain Document。

约定（knowledge/ 目录）：
- 每个进向量库的文档必须带 YAML front-matter，至少包含 `category` 字段；
- front-matter 会被从正文剥离（不参与向量化），字段写入 Document.metadata；
- `source` 固定为文件绝对路径。eval/eval_retrieval.py 依赖 Path(source).stem 做
  表名映射（如 /.../knowledge/transactions.md -> transactions），勿改动 source 字段。
"""
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from langchain_core.documents import Document


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """解析文件开头的 YAML front-matter，返回 (meta, body)。无 front-matter 时返回 ({}, 原文)。"""
    text = text.lstrip("﻿")  # 去掉可能的 BOM
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    if len(lines) < 3:
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def load_markdown_docs(docs_dir: str) -> List[Document]:
    """加载 docs_dir 下所有 .md，剥离 front-matter 并写入 metadata。

    缺少 `category` 或正文为空的文件会被跳过并打印警告——这是进入向量库的「过滤器」：
    只有带合法 front-matter 的知识文档才会被索引。
    """
    docs: List[Document] = []
    skipped: List[Tuple[str, str]] = []

    for path in sorted(Path(docs_dir).rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)

        if not meta.get("category"):
            skipped.append((str(path), "缺少 front-matter 或 category 字段"))
            continue
        if not body.strip():
            skipped.append((str(path), "正文为空"))
            continue

        meta.setdefault("title", path.stem)
        docs.append(Document(page_content=body, metadata={"source": str(path), **meta}))

    if skipped:
        print(f"[doc_loader] 跳过 {len(skipped)} 个文档（不会被索引）：")
        for p, reason in skipped:
            print(f"  - {p}：{reason}")

    return docs
