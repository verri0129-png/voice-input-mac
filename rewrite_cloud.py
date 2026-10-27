#!/usr/bin/env python3
"""Typeless-style cloud rewrite for local speech drafts.

The local speech recognizer stays on-device.  Only the short text draft,
a small amount of text around the cursor, and an optional personal term
list are sent to Vertex AI for correction.  Audio is never uploaded.

The validator is intentionally strict: cloud output may only change
characters in place, remove known filler material, add punctuation, or use
a declared dictionary variant.  Anything else falls back to deterministic
local punctuation handling.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_CONTEXT_CHARS = 100
DEFAULT_SELECTED_CHARS = 300
DEFAULT_VISIBLE_CHARS = 1000
DEFAULT_FULL_FIELD_CHARS = 1200
DEFAULT_MAX_TERMS = 60
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_OUTPUT_TOKENS = 1024
MAX_CHANGED_RATIO = 0.25
# 用後學習的門檻：中文逐字修正最多 2 個字；英文專有名詞允許整詞替換
# （含長度改變，例如 Sony → Sonia），但一次只能換一個詞、且不得超長。
MAX_LEARN_CHANGED_CHARS = 2
MAX_LEARN_WORD_CHARS = 16

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


@dataclass(frozen=True)
class RewriteConfig:
    """Cloud-rewrite settings.

    project may be empty: in that case ADC quota_project_id is used.
    """

    backend: str = "cloud"
    model: str = "gemini-3.8-flash"
    location: str = "global"
    project: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    # 思考階段會吃掉輸出預算並把單次呼叫拖到 10s 以上（實測：thoughts ~980/1024）。
    # None = 不送 thinkingConfig；0 = 盡量關掉（這顆模型不一定聽話，所以還要看 finishReason）。
    thinking_budget: int | None = 0
    adc_path: str = ""
    terms_file: str = "rewrite_terms.json"
    max_terms: int = DEFAULT_MAX_TERMS
    # Priority PayGo：加兩個 header 走優先池（單價較高，沒有最低消費、隨時可關）。
    # 回應的 usageMetadata.trafficType 會是 ON_DEMAND_PRIORITY；被降級則是 ON_DEMAND。
    priority: bool = False


@dataclass(frozen=True)
class TermRule:
    text: str
    variants: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class RewriteResult:
    text: str
    backend: str
    changed_chars: int = 0
    checked_chars: int = 0
    milliseconds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reason: str = ""
    traffic_type: str = ""


def _content_chars(value: str) -> str:
    """Keep text that carries meaning: punctuation, spaces and controls removed."""
    out: list[str] = []
    for char in value:
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("C"):
            continue
        if char == "�":
            continue
        out.append(char)
    return "".join(out)


PROTECTED_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._\-/][A-Za-z0-9]+)*")


def _protected_spans(value: str) -> list[str]:
    """Identifiers, model numbers, dates, URLs and other ASCII tokens, in order."""
    return PROTECTED_TOKEN_RE.findall(value)


WORD_FRAGMENT_RE = re.compile(r"^[A-Za-z]+(?:['\-][A-Za-z]+)*$")


def _is_word_swap(source: str, revised: str) -> bool:
    """True when a manual edit replaced one English word (or a fragment of it).

    Users fix English names by hand far more often than Chinese characters
    (Sony → Sonia, Sketcher → Skecher), and those edits change the token and
    its length.  Only letters are allowed here: digits, symbols and mixed
    content still have to keep their exact shape.
    """
    return bool(WORD_FRAGMENT_RE.match(source)) and bool(WORD_FRAGMENT_RE.match(revised))


def _word_bounds(text: str, start: int, end: int, low: int, high: int) -> tuple[int, int]:
    """Widen [start, end) to the whole ASCII word it belongs to, within [low, high)."""
    while start > low and text[start - 1].isascii() and text[start - 1].isalpha():
        start -= 1
    while end < high and text[end].isascii() and text[end].isalpha():
        end += 1
    return start, end


def _collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")).strip()


def _strip_invisible(value: str) -> str:
    return "".join(
        char
        for char in value
        if char == "\n" or (not unicodedata.category(char).startswith("C") and char != "�")
    )


def normalize_edit_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _collapse_whitespace(_strip_invisible(value))


def _insertion_index(field: str, before: str, after: str) -> int | None:
    positions: list[int] = []
    if before:
        start = 0
        while True:
            found = field.find(before, start)
            if found < 0:
                break
            position = found + len(before)
            if not after or field.startswith(after, position):
                positions.append(position)
            start = found + 1
    elif after:
        start = 0
        while True:
            found = field.find(after, start)
            if found < 0:
                break
            positions.append(found)
            start = found + 1
    positions = sorted(set(positions))
    return positions[0] if len(positions) == 1 else None


def _insertion_block(original: str, current: str, inserted: str) -> tuple[int, int] | None:
    """Find where the pasted block sits in `current` by comparing both snapshots.

    The cursor context is often too short to be a unique anchor (a single
    Chinese character repeats constantly), so locate the paste structurally:
    strip the common prefix/suffix, and require what is left in `current` to be
    the pasted text plus at most a small manual edit.

    Returns (start, end) offsets into `current`, or None when the shape does not
    match (field changed elsewhere, several insertions, mismatched payload).
    """
    limit = min(len(original), len(current))
    prefix = 0
    while prefix < limit and original[prefix] == current[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and original[len(original) - 1 - suffix] == current[len(current) - 1 - suffix]
    ):
        suffix += 1
    # The paste must be the only difference between the two snapshots.
    if original[prefix: len(original) - suffix]:
        return None
    start, end = prefix, len(current) - suffix
    if end <= start:
        return None
    tolerance = max(4, len(inserted) // 4)
    if abs((end - start) - len(inserted)) > tolerance:
        return None
    return start, end


class _LearnSkip(Exception):
    """Internal signal: why one manual edit could not become a term rule."""


def _snapshot_shape(original: str, current: str) -> str:
    limit = min(len(original), len(current))
    prefix = 0
    while prefix < limit and original[prefix] == current[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and original[len(original) - 1 - suffix] == current[len(current) - 1 - suffix]
    ):
        suffix += 1
    return (
        f"prefix={prefix} suffix={suffix} "
        f"原欄多出={len(original) - prefix - suffix}字 "
        f"現欄多出={len(current) - prefix - suffix}字"
    )


def infer_term_candidates(payload: dict) -> list[TermRule]:
    """Infer one context-bound correction from a later manual edit.

    Every rejection explains itself on stderr, and the app logs that line:
    a term that never appeared can then be traced to the guard that stopped
    it instead of being guessed at.
    """
    try:
        return _infer_term_candidates(payload)
    except _LearnSkip as skip:
        shape = " ".join(
            f"{name}={len(normalize_edit_text(payload.get(key)))}字"
            for name, key in (
                ("original", "original_field"),
                ("current", "current_field"),
                ("inserted", "inserted_text"),
                ("before", "text_before"),
                ("after", "text_after"),
            )
        )
        print(f"[learn] 略過：{skip}（{shape}）", file=sys.stderr)
        return []


def _infer_term_candidates(payload: dict) -> list[TermRule]:
    original = normalize_edit_text(payload.get("original_field"))
    current = normalize_edit_text(payload.get("current_field"))
    inserted = normalize_edit_text(payload.get("inserted_text"))
    before = normalize_edit_text(payload.get("text_before"))
    after = normalize_edit_text(payload.get("text_after"))
    if not current or not inserted or original == current:
        raise _LearnSkip("欄位或注入文字是空的，或者欄位跟貼上時一模一樣（沒有手動修改）")
    span = _insertion_block(original, current, inserted)
    if span is not None:
        index = span[0]
    else:
        index = _insertion_index(original, before, after)
        if index is None:
            raise _LearnSkip(
                "找不到貼上的那一段：欄位快照 diff 對不上，游標前後文也不能當備援"
                f"（{_snapshot_shape(original, current)}）"
            )
    insert_end = index + len(inserted)
    expected = original[:index] + inserted + original[index:]
    if expected == current:
        return []

    matcher = difflib.SequenceMatcher(None, expected, current, autojunk=False)
    candidates: list[TermRule] = []
    char_changed = 0
    word_changed = 0
    word_swaps = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if i2 <= index or i1 >= insert_end:
            continue
        if tag == "equal":
            continue
        if i1 < index or i2 > insert_end:
            return []
        source = expected[i1:i2]
        revised = current[j1:j2]
        if tag == "insert":
            if _content_chars(revised):
                return []
            continue
        if tag == "delete":
            if _content_chars(source):
                return []
            continue
        word_swap = _is_word_swap(source, revised)
        if (i2 - i1) != (j2 - j1) and not word_swap:
            return []
        if _content_chars(source) == _content_chars(revised):
            continue
        if _protected_spans(source) != _protected_spans(revised) and not word_swap:
            return []
        if word_swap:
            word_swaps += 1
            word_changed += max(i2 - i1, j2 - j1)
            source_start, source_end = _word_bounds(expected, i1, i2, index, insert_end)
            revised_start, revised_end = _word_bounds(current, j1, j2, 0, len(current))
            start = max(index, source_start - 2)
            end = min(insert_end, source_end + 2)
            source_phrase = expected[start:end]
            revised_phrase = current[
                max(0, revised_start - (source_start - start)) : revised_end + (end - source_end)
            ]
        else:
            char_changed += max(i2 - i1, j2 - j1)
            start = max(index, i1 - 2)
            end = min(insert_end, i2 + 2)
            source_phrase = expected[start:end]
            revised_phrase = current[(start + j1 - i1) : (end + j2 - i2)]
        if (
            len(source_phrase) < 2
            or len(source_phrase) > 16
            or source_phrase == revised_phrase
        ):
            return []
        candidates.append(TermRule(text=revised_phrase, variants=(source_phrase,)))
    if not candidates:
        return []
    if char_changed > MAX_LEARN_CHANGED_CHARS:
        return []
    if word_swaps > 1 or word_changed > MAX_LEARN_WORD_CHARS:
        return []
    return candidates


def _read_terms_document(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "terms": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"個人詞庫無法讀取：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
        raise RuntimeError("個人詞庫格式錯誤")
    return data


def merge_term_candidates(path: str | os.PathLike[str], candidates: list[TermRule]) -> dict:
    terms_path = Path(path)
    document = _read_terms_document(terms_path)
    rows = document.get("terms", [])
    added = existing = conflicts = 0
    by_text: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("text"), str):
            by_text[row["text"]] = row
    for candidate in candidates:
        if len(candidate.text) < 2:
            continue
        for variant in candidate.variants:
            if not variant or variant == candidate.text:
                continue
            conflict = any(
                row_text != candidate.text and variant in _term_variant_list(row)
                for row_text, row in by_text.items()
            )
            if conflict:
                conflicts += 1
                continue
            row = by_text.get(candidate.text)
            if row is None:
                row = {"text": candidate.text, "match": []}
                rows.append(row)
                by_text[candidate.text] = row
            variants = _term_variant_list(row)
            if variant in variants:
                existing += 1
                continue
            variants.append(variant)
            row["match"] = variants
            added += 1
    if added:
        terms_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(terms_path.parent),
            delete=False,
        ) as handle:
            json.dump(
                {"version": 1, "terms": rows},
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
            temporary = handle.name
        os.replace(temporary, terms_path)
        os.chmod(terms_path, 0o600)
    return {"added": added, "existing": existing, "conflicts": conflicts}


def _term_variant_list(row: dict) -> list[str]:
    raw = row.get("match", row.get("variants", []))
    if isinstance(raw, str):
        raw = [raw]
    variants: list[str] = []
    for value in raw if isinstance(raw, list) else []:
        text = str(value).strip()
        if text and text not in variants:
            variants.append(text)
    return variants


def learn_insertion_edits(payload: dict, terms_path: str | os.PathLike[str]) -> dict:
    candidates = infer_term_candidates(payload if isinstance(payload, dict) else {})
    merged = merge_term_candidates(terms_path, candidates)
    return {
        "candidates": len(candidates),
        "learned": merged["added"],
        "existing": merged["existing"],
        "conflicts": merged["conflicts"],
    }


def canonical_draft(value: str) -> str:
    return _collapse_whitespace(_strip_invisible(value or ""))


def load_term_rules(path: str | os.PathLike[str]) -> list[TermRule]:
    """Load an optional user-managed personal term file.

    Supported shapes:
      {"version": 1, "terms": [{"text": "...", "match": ["..."], "note": "..."}]}
      {"terms": ["...", "..."]}
      ["...", "..."]
    """
    candidate = Path(path)
    if not candidate.exists():
        return []
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        items: list = data
    elif isinstance(data, dict):
        items = data.get("terms", [])
    else:
        return []
    rules: list[TermRule] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            text, variants, note = item.strip(), (), ""
        elif isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            raw_variants = item.get("match", item.get("variants", []))
            if isinstance(raw_variants, str):
                raw_variants = [raw_variants]
            variants = tuple(sorted({str(x).strip() for x in raw_variants if str(x).strip()}))
            note = str(item.get("note", ""))
        else:
            continue
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        rules.append(TermRule(text=text, variants=variants, note=note))
    return rules


def _context_haystack(context: dict | None) -> str:
    if not context:
        return ""
    parts: list[str] = []
    for key in (
        "app_name",
        "window_title",
        "web_url",
        "selected_text",
        "text_before",
        "text_after",
        "visible_text",
    ):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def select_terms(draft: str, context: dict | None, rules: list[TermRule], limit: int) -> list[TermRule]:
    """Pick dictionary entries that could plausibly matter for this utterance."""
    if limit <= 0:
        return []
    haystack = "\n".join([draft or "", _context_haystack(context)])
    haystack_chars = set(haystack)
    scored: list[tuple[int, int, TermRule]] = []
    for order, rule in enumerate(rules):
        score = 0
        for variant in rule.variants:
            if variant and variant in haystack:
                score += 100 + len(variant)
        if rule.text in haystack:
            score += 20 + len(rule.text)
        else:
            overlap = len(set(rule.text) & haystack_chars)
            if overlap >= max(2, len(rule.text) // 2):
                score += overlap
        if score > 0:
            scored.append((score, order, rule))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [rule for _, _, rule in scored[:limit]]


def sanitize_context(context: dict | None) -> dict:
    """Keep only bounded context fields the prompt is allowed to see."""
    if not isinstance(context, dict):
        return {}
    def text(key: str, limit: int) -> str:
        value = context.get(key)
        if not isinstance(value, str):
            return ""
        return _collapse_whitespace(_strip_invisible(value))[:limit]

    cleaned: dict[str, object] = {
        "app_name": text("app_name", 120),
        "bundle_id": text("bundle_id", 160),
        "window_title": text("window_title", 240),
        "web_url": _redact_url(text("web_url", 500)),
        "field_role": text("field_role", 64),
        "selected_text": text("selected_text", DEFAULT_SELECTED_CHARS),
        "text_before": text("text_before", DEFAULT_CONTEXT_CHARS),
        "text_after": text("text_after", DEFAULT_CONTEXT_CHARS),
        "visible_text": text("visible_text", DEFAULT_VISIBLE_CHARS),
        # Typeless 的 `full_field_content`：整個輸入框內容。同音字靠它才分得出來。
        "full_field_text": text("full_field_text", DEFAULT_FULL_FIELD_CHARS),
    }
    if context.get("blocked") is True:
        reason = context.get("reason")
        blocked: dict[str, object] = {"blocked": True}
        if isinstance(reason, str) and reason.strip():
            blocked["reason"] = reason.strip()[:160]
        return blocked
    return {key: value for key, value in cleaned.items() if value not in ("", None, False)}


def _redact_url(value: str) -> str:
    if not value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return value
    redacted_netloc = parts.netloc.rsplit("@", 1)[-1]
    redacted = parts._replace(netloc=redacted_netloc, query="", fragment="")
    return urllib.parse.urlunsplit(redacted)


def _clean_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def build_prompt(draft: str, context: dict, terms: list[TermRule]) -> tuple[str, str]:
    clean_context = sanitize_context(context)
    lines = [
        "你是語音辨識逐字稿的修正器。輸入是一段剛說完的話，可能含有同音錯字。",
        "只能做以下修正：",
        "1. 把錯誤的同音字換成正確的字。",
        "2. 當聲音吻合時，使用下面的專有名詞正確寫法。",
        "3. 刪除沒有意義的口頭重複與語助詞。",
        "4. 依語意補上正確的中文標點。",
        "嚴格禁止改寫句子、調整語序、翻譯、摘要、舉例、補充背景、",
        "增刪數字、英文代號、日期、網址，也不要把上下文裡的字塞進逐字稿。",
        "英文廠商名、英文人名與英文代號一律原樣輸出，即使你覺得拼法有誤"
        "（例如 Sketcher 不可以寫成 Skechers、nvy 不可以寫成 NVY）。",
        "不確定就保留原文用字。只輸出修正後的整段文字，不要解釋或加引號。",
    ]
    if _clean_text(clean_context.get("app_name")):
        lines.append(f"當前 App：{_clean_text(clean_context['app_name'])}")
    if _clean_text(clean_context.get("window_title")):
        lines.append(f"視窗標題：{_clean_text(clean_context['window_title'])}")
    full_field = _clean_text(clean_context.get("full_field_text"))
    if full_field:
        lines.append(
            "輸入框現有內容（使用者原本打的字；只用來判斷同音字與領域用詞，"
            f"絕對不要複製、改寫或刪掉它）：\n{full_field}"
        )
    if _clean_text(clean_context.get("selected_text")):
        lines.append(f"已選取文字：{_clean_text(clean_context['selected_text'])}")
    before = _clean_text(clean_context.get("text_before"))
    after = _clean_text(clean_context.get("text_after"))
    if before or after:
        lines.append(
            "游標前後文字（只用來判斷同音字，不要複製進輸出）："
            f"前「{before}」／後「{after}」"
        )
    if _clean_text(clean_context.get("visible_text")):
        lines.append(
            "可見文字片段（只用來判斷領域用詞，不要複製進輸出）："
            f"{_clean_text(clean_context['visible_text'])}"
        )
    if terms:
        lines.append("專有名詞正確寫法（聲音吻合時使用，不要硬塞無關詞）：")
        for rule in terms:
            if rule.variants:
                lines.append(f"- {rule.text}（也可能聽成{'、'.join(rule.variants)}）")
            else:
                lines.append(f"- {rule.text}")
    return "\n".join(lines), canonical_draft(draft)


def _default_adc_path(config: RewriteConfig) -> Path:
    if config.adc_path:
        return Path(config.adc_path).expanduser()
    override = os.environ.get("VOICE_GOOGLE_ADC", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config/gcloud/application_default_credentials.json"


def mint_access_token(config: RewriteConfig) -> str:
    """Mint a short-lived Vertex access token from user OAuth ADC.

    The token is cached only in process memory and never written to disk.
    """
    now = time.time()
    cache_key = str(_default_adc_path(config))
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > now + 60:
        return cached[0]
    try:
        adc = json.loads(_default_adc_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"找不到可用的 Google ADC 憑證：{exc}") from exc
    if adc.get("type") != "authorized_user":
        raise RuntimeError("只支援使用者 OAuth 的 application_default_credentials.json")
    body = urllib.parse.urlencode(
        {
            "client_id": adc["client_id"],
            "client_secret": adc["client_secret"],
            "refresh_token": adc["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - surface as a rewrite fallback reason
        raise RuntimeError(f"換取 Vertex 存取憑證失敗：{type(exc).__name__}") from exc
    token = str(payload.get("access_token", ""))
    if not token:
        raise RuntimeError("換取 Vertex 存取憑證失敗：回應沒有 access_token")
    try:
        lifetime = float(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        lifetime = 3600.0
    _TOKEN_CACHE[cache_key] = (token, now + max(300.0, lifetime - 60.0))
    return token


def _project_id(config: RewriteConfig) -> str:
    if config.project:
        return config.project
    try:
        adc = json.loads(_default_adc_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"找不到可用的 Google ADC 憑證：{exc}") from exc
    project = str(adc.get("quota_project_id", "") or "")
    if not project:
        raise RuntimeError("ADC 沒有 quota_project_id，請設定 VOICE_CLOUD_REWRITE_PROJECT")
    return project


def _post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 - error detail is best-effort
            detail = ""
        raise RuntimeError(f"Vertex HTTP {exc.code}：{detail}") from exc
    except Exception as exc:  # noqa: BLE001 - surface as a rewrite fallback reason
        raise RuntimeError(f"Vertex 呼叫失敗：{type(exc).__name__}") from exc


def _response_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Vertex 沒有回傳候選文字")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    chunks = [
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("text") and part.get("thought") is not True
    ]
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("Vertex 候選文字為空")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    return text


def _finish_reason(payload: dict) -> str:
    """First candidate's finishReason ("STOP", "MAX_TOKENS", ...); "" when absent."""
    if not isinstance(payload, dict):
        return ""
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    return str(candidates[0].get("finishReason") or "").upper()


def _declared_replacements(terms: list[TermRule]) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    for rule in terms:
        for variant in rule.variants:
            if variant and variant != rule.text:
                replacements.append((variant, rule.text))
    replacements.sort(key=lambda item: -len(item[0]))
    return replacements


def _describe_protected_change(draft: str, candidate: str) -> str:
    """Name the first identifier/number the cloud output disturbed."""
    before = _protected_spans(canonical_draft(draft))
    after = _protected_spans(canonical_draft(candidate))
    for original_token, revised_token in zip(before, after):
        if original_token != revised_token:
            return f"{original_token[:20]} → {revised_token[:20]}"
    if len(before) > len(after):
        return f"少了 {before[len(after)][:20]}"
    if len(after) > len(before):
        return f"多了 {after[len(before)][:20]}"
    return ""


def validate_and_repair_rewrite(
    draft: str,
    candidate: str,
    *,
    allowed_deletions: set[str] | frozenset[str] = frozenset(),
    declared_terms: list[TermRule] | tuple[TermRule, ...] = (),
) -> tuple[bool, str, int, str]:
    """Validate, but put identifiers back instead of dropping the whole rewrite.

    Dropping a 100-character rewrite because the model "corrected" Sketcher to
    Skechers costs the user several seconds and gains nothing.  Stricter still:
    when the token sequences cannot be aligned, reject as before.

    Returns (ok, reason, changed_chars, text_to_use).
    """
    repaired = candidate
    fixes: list[tuple[str, str]] = []
    if _protected_spans(canonical_draft(draft)) != _protected_spans(canonical_draft(candidate)):
        repaired, maybe_fixes = _repair_protected_tokens(canonical_draft(draft), candidate)
        if maybe_fixes is None:
            detail = _describe_protected_change(draft, candidate)
            return False, f"英文代號或數字被改動且無法對齊（{detail}）", 0, candidate
        fixes = maybe_fixes
    ok, reason, changed = validate_rewrite(
        draft,
        repaired,
        allowed_deletions=allowed_deletions,
        declared_terms=declared_terms,
    )
    if ok and fixes:
        detail = "、".join(f"{current}→{original}" for current, original in fixes[:3])
        reason = f"{reason}；代號已還原（{detail}）"
    return ok, reason, changed, repaired


def _repair_protected_tokens(draft: str, candidate: str) -> tuple[str, list[tuple[str, str]] | None]:
    """Restore each identifier/number to the way the draft said it.

    Returns (repaired_text, fixes); fixes is None when the two texts do not
    carry the same tokens in the same order, i.e. the model restructured the
    sentence and the rewrite must be rejected.
    """
    original_tokens = _protected_spans(draft)
    matches = list(PROTECTED_TOKEN_RE.finditer(candidate))
    if len(original_tokens) != len(matches):
        return candidate, None
    pieces: list[str] = []
    fixes: list[tuple[str, str]] = []
    cursor = 0
    for match, original_token in zip(matches, original_tokens):
        pieces.append(candidate[cursor:match.start()])
        token = match.group(0)
        if token == original_token:
            pieces.append(token)
        else:
            pieces.append(original_token)
            fixes.append((token, original_token))
        cursor = match.end()
    pieces.append(candidate[cursor:])
    return "".join(pieces), fixes


def validate_rewrite(
    draft: str,
    candidate: str,
    *,
    allowed_deletions: set[str] | frozenset[str] = frozenset(),
    declared_terms: list[TermRule] | tuple[TermRule, ...] = (),
) -> tuple[bool, str, int]:
    """Return (ok, reason, changed_chars)."""
    original = _content_chars(canonical_draft(draft))
    revised = _content_chars(canonical_draft(candidate))
    if not revised:
        return False, "雲端輸出為空", 0
    if _protected_spans(canonical_draft(draft)) != _protected_spans(canonical_draft(candidate)):
        detail = _describe_protected_change(draft, candidate)
        reason = f"英文代號或數字被改動（{detail}）" if detail else "英文代號或數字被改動"
        return False, reason, 0
    if original == revised:
        return True, "pass（只動標點或空白）", 0

    working = original
    for source, target in _declared_replacements(list(declared_terms)):
        if source and source in working:
            working = working.replace(source, target)
    if working == revised:
        changed = sum(1 for a, b in zip(original, revised) if a != b)
        return True, f"pass（字典強制替換，換字 {changed}/{len(original)}）", changed

    matcher = difflib.SequenceMatcher(None, working, revised, autojunk=False)
    changed = 0
    allowed = set(allowed_deletions)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            inserted = revised[j1:j2]
            if inserted:
                return False, f"雲端新增了原文沒有的字「{inserted[:12]}」", 0
            continue
        if tag == "delete":
            deleted = working[i1:i2]
            if deleted and deleted not in allowed:
                return False, f"雲端刪掉了非贅字「{deleted[:12]}」", 0
            continue
        # replace:同音修正通常是一對一換字，長度必須相同，避免整句重寫。
        if (i2 - i1) != (j2 - j1):
            return False, "雲端改寫改變了字數", 0
        changed += max(i2 - i1, j2 - j1)
    if changed == 0:
        return True, "pass（只刪贅字）", 0
    ratio = changed / max(1, len(working))
    if ratio > MAX_CHANGED_RATIO:
        return False, f"改動比例過高 {ratio:.0%}（上限 {MAX_CHANGED_RATIO:.0%}）", 0
    return True, f"pass（換字 {changed}/{len(working)}）", changed


def rewrite_draft(
    draft: str,
    context: dict | None = None,
    terms: list[TermRule] | None = None,
    config: RewriteConfig | None = None,
    *,
    transport=None,
    allowed_deletions: set[str] | frozenset[str] = frozenset(),
) -> RewriteResult:
    active = config or RewriteConfig()
    started = time.perf_counter()
    clean_context = sanitize_context(context)
    active_terms = select_terms(draft, clean_context, terms or [], active.max_terms)
    system, user = build_prompt(draft, clean_context, active_terms)
    project = _project_id(active)
    token = mint_access_token(active)
    url = (
        f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/"
        f"{active.location}/publishers/google/models/"
        f"{urllib.parse.quote(active.model, safe='')}:generateContent"
    )
    generation: dict = {
        "temperature": 0,
        "maxOutputTokens": max(256, active.max_output_tokens),
    }
    if active.thinking_budget is not None:
        generation["thinkingConfig"] = {"thinkingBudget": int(active.thinking_budget)}
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": generation,
    }
    poster = transport or _post_json
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if active.priority:
        # 「只用 Priority PayGo」模式（沒有 PT 配額）：兩個 header 都要送。
        headers["X-Vertex-AI-LLM-Request-Type"] = "shared"
        headers["X-Vertex-AI-LLM-Shared-Request-Type"] = "priority"
    response = poster(
        url,
        headers,
        payload,
        active.timeout_seconds,
    )
    # 被 MAX_TOKENS 砍掉的輸出是半句話。以前完全沒看 finishReason，半句話會直接
    # 進守門、甚至被當成結果貼出去；現在一律視為失敗，交給呼叫端退回規則版。
    finish_reason = _finish_reason(response)
    if finish_reason == "MAX_TOKENS":
        usage = response.get("usageMetadata") if isinstance(response, dict) else None
        thoughts = int((usage or {}).get("thoughtsTokenCount", 0) or 0)
        raise RuntimeError(
            f"輸出被截斷（finish=MAX_TOKENS，思考吃掉 {thoughts} token；"
            f"把 VOICE_CLOUD_MAX_OUTPUT_TOKENS 調高或關掉思考）"
        )
    text = _response_text(response)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    usage = response.get("usageMetadata") if isinstance(response, dict) else None
    input_tokens = int((usage or {}).get("promptTokenCount", 0) or 0)
    output_tokens = int((usage or {}).get("candidatesTokenCount", 0) or 0)
    traffic_type = str((usage or {}).get("trafficType", "") or "")
    ok, reason, changed, final_text = validate_and_repair_rewrite(
        draft,
        text,
        allowed_deletions=allowed_deletions,
        declared_terms=active_terms,
    )
    checked = len(_content_chars(canonical_draft(draft)))
    if not ok:
        raise RuntimeError(reason)
    return RewriteResult(
        text=final_text,
        backend=f"cloud:{active.model}",
        changed_chars=changed,
        checked_chars=checked,
        milliseconds=elapsed_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reason=reason,
        traffic_type=traffic_type,
    )


__all__ = [
    "DEFAULT_CONTEXT_CHARS",
    "DEFAULT_MAX_TERMS",
    "DEFAULT_SELECTED_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_VISIBLE_CHARS",
    "RewriteConfig",
    "RewriteResult",
    "TermRule",
    "build_prompt",
    "canonical_draft",
    "infer_term_candidates",
    "learn_insertion_edits",
    "load_term_rules",
    "merge_term_candidates",
    "mint_access_token",
    "rewrite_draft",
    "sanitize_context",
    "select_terms",
    "validate_rewrite",
]
