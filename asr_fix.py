"""辨識結果的修正，做法來自 EasyType 拆解（2026-11-14）；起點是櫃檯小妹 voice-bridge/duplex/asrfix.py 的規則，之後各自維護。

1. 假字：辨識模型會從安靜或雜音生出「謝謝收看」。兩份清單：ALWAYS 不管音量都刪；QUIET_ONLY 也是真的短話，
   只有整句就是那個詞、而且音量一直很小時才刪。
2. 看前後文才改的修字表：聽錯的詞只在前後文對得上（且沒有反例詞）時才換。「全線通車」不動，「設定一下全線」變權限。
   也收 OpenCC s2twp 的台灣用語誤轉（权限→許可權、账号→賬號）；會改掉意思的換詞（文件→檔案）在轉換前就擋下。
規則加在 ASR_RULES：(聽成, 改成, 要有任一詞才改（空＝一律改）, 有任一詞就不改)。
"""
from __future__ import annotations

import re

# 0.05 秒小段 RMS 的最大值低於這個（約 -30.5 dBFS）＝整段都很小聲；與櫃檯小妹的 QUIET_PEAK=0.03 相同
QUIET_LEVEL_DB = -30.5

PHANTOM_ALWAYS = ("謝謝收看", "感謝收看", "謝謝觀看", "感謝觀看", "請不吝點贊訂閱", "不吝點贊", "訂閱我的頻道",
                  "字幕由", "字幕志願者", "中文字幕", "Thanks for watching", "Thank you for watching",
                  "Subtitles by", "Amara.org")
PHANTOM_QUIET_ONLY = ("謝謝", "謝謝大家", "謝謝你", "好", "嗯", "喔", "啊", "再見", "拜拜", "你好", "Thank you", "Bye",
                      "Thanks")

ASR_RULES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("許可權", "權限", (), ()),  # OpenCC s2twp 把 权限 轉成 許可權；台灣講權限
    ("型別", "類型", (), ("TypeScript", "type")),  # s2twp 把 类型 轉成 型別
    ("賬", "帳", (), ()),  # OpenCC 把 账 轉成 賬（賬號、轉賬）；台灣用帳
    ("全線", "權限", ("設定", "帳號", "管理", "admin", "權"), ("捷運", "高鐵", "通車", "台鐵", "火車", "開放")),
    ("連線值", "連線池", ("資料庫", "DB", "database", "設太小", "設太大"), ()),
    ("美洲", "每週", ("報表", "週報", "DAU"), ("出差", "北美", "南美", "市場")),
    ("火藥", "活躍", ("用戶", "使用者", "會員"), ("爆炸", "炸藥")),
    ("Chrome", "cron", ("排程", "crontab", "定時"), ("瀏覽器", "分頁", "網頁")),
)

# OpenCC s2twp 換詞時會改掉意思的詞：轉換前先遮起來，轉完放回。文件≠檔案（document／file）；
# 「文件夾→資料夾」是對的，不遮。轉完已分不出原本講哪個，所以只能在轉換前處理。
OPENCC_KEEP = re.compile(r"文件(?![夹夾])")
_KEEP_MARK = "\ue000"  # 私用區字元，OpenCC 不會動它


def opencc_convert(convert, text: str) -> str:
    """用 convert（OpenCC 的 convert）轉換，但 OPENCC_KEEP 的詞保持原樣。"""
    kept = OPENCC_KEEP.findall(text)
    out = convert(OPENCC_KEEP.sub(_KEEP_MARK, text))
    for word in kept:
        out = out.replace(_KEEP_MARK, word, 1)
    return out


_PUNCT = re.compile(r"[\s，,。.！!？?、；;：:～~…「」『』\"'（）()]+")


def drop_phantom(text: str, quiet: bool | None = None) -> str:
    """去掉假字後的文字；只剩假字就回 ""。quiet：整段是否都很小聲（None＝不知道 → 不套 QUIET_ONLY）。"""
    bare = _PUNCT.sub("", text)
    rest = bare
    for p in sorted(PHANTOM_ALWAYS, key=len, reverse=True):
        rest = re.sub(re.escape(_PUNCT.sub("", p)), "", rest, flags=re.I)
    if bare and not rest:  # 全是假字才算編的（「找中文字幕的電影」是真的請求）
        return ""
    if quiet and any(bare.lower() == p.lower() for p in PHANTOM_QUIET_ONLY):
        return ""
    return text


def fix_context(text: str) -> str:
    """套 ASR_RULES：要有的詞對得上、不能有的詞都沒有，才換。"""
    low = text.lower()
    for heard, to, need, block in ASR_RULES:
        if heard.lower() not in low:
            continue
        if need and not any(n.lower() in low for n in need):
            continue
        if any(b.lower() in low for b in block):
            continue
        text = re.sub(re.escape(heard), to, text, flags=re.I)
        low = text.lower()
    return text
