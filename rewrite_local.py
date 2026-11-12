#!/usr/bin/env python3
"""本地小模型定版（Qwen2.5-3B-Instruct 4bit，MLX）。opt-in：VOICE_REWRITE_BACKEND=local-qwen。

輸出一律經過 rewrite_cloud.validate_and_repair_rewrite（與雲端同一道守門）：
模型改了字、刪了不該刪的，就拋 RewriteRejected，由呼叫端退回規則版。
模型本身不會被信任；預設 temperature=0，失敗就走規則，不會卡住貼上。
"""

from __future__ import annotations

import functools
from pathlib import Path

from rewrite_cloud import (
    DEFAULT_MAX_TERMS,
    RewriteRejected,
    TermRule,
    canonical_draft,
    select_terms,
    validate_and_repair_rewrite,
)

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "models" / "qwen25-3b-instruct-mlx-4bit"

SYSTEM_PROMPT = (
    "你是語音輸入的定版器。只做三件事：補全形標點、修正明顯的同音錯字、刪除贅字。"
    "不要改寫、不要增加或刪除實質內容、不要回答問題、不要解釋。只輸出修正後的文字。"
)


@functools.cache
def _load(model_dir: str):
    """第一次用到才載入；同一路徑之後重用。"""
    from mlx_lm import load

    return load(model_dir)


def _build_prompt(tokenizer, draft: str, terms: list[TermRule]) -> str:
    system = SYSTEM_PROMPT
    declared = [rule.text for rule in terms if rule.text]
    if declared:
        system += "\n詞庫正字（若遇到近音字請用這些寫法）：" + "、".join(declared)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_draft(draft)},
    ]
    return tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)


def generate_text(draft: str, terms: list[TermRule], model_dir: str = str(DEFAULT_MODEL_DIR)) -> str:
    """實際跑模型。測試時可以替換這個函式，不必載入真模型。"""
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    model, tokenizer = _load(model_dir)
    prompt = _build_prompt(tokenizer, draft, terms)
    limit = max(64, min(512, len(draft) * 3))
    return generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=limit,
        sampler=make_sampler(temp=0.0),
        verbose=False,
    ).strip()


def local_rewrite(
    draft: str,
    terms: list[TermRule] | tuple[TermRule, ...] = (),
    *,
    allowed_deletions: set[str] | frozenset[str] = frozenset(),
    model_dir: str | None = None,
) -> tuple[str, str, int]:
    """回傳 (定版文字, 守門理由, 改動字數)。守門不過就拋 RewriteRejected（帶模型原文）。"""
    # 與雲端相同：只挑跟這句話有關的詞，prompt 與守門用同一份子集
    active = select_terms(draft, None, list(terms), DEFAULT_MAX_TERMS)
    candidate = generate_text(draft, active, model_dir or str(DEFAULT_MODEL_DIR))
    if not candidate:
        raise RewriteRejected("本地模型回空字串", model_text="")
    ok, reason, changed, final_text = validate_and_repair_rewrite(
        draft,
        candidate,
        allowed_deletions=allowed_deletions,
        declared_terms=active,
    )
    if not ok:
        raise RewriteRejected(reason, model_text=candidate)
    return final_text, reason, changed
