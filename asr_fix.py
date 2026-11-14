"""辨識結果的修正，做法來自 EasyType 拆解（2026-11-14）；起點是櫃檯小妹 voice-bridge/duplex/asrfix.py 的規則，之後各自維護。

1. 假字：辨識模型會從安靜或雜音生出「謝謝收看」。兩份清單：ALWAYS 不管音量都刪；QUIET_ONLY 也是真的短話，
   只有整句就是那個詞、而且音量一直很小時才刪。
2. 看前後文才改的修字表：聽錯的詞只在前後文對得上（且沒有反例詞）時才換。「全線通車」不動，「設定一下全線」變權限。
   也收 OpenCC s2twp 的台灣用語誤轉（权限→許可權、账号→賬號）；會改掉意思的換詞（文件→檔案、通過→透過）在轉換前就擋下。
規則加在 ASR_RULES：(聽成, 改成, 要有任一詞才改（空＝一律改）, 有任一詞就不改)。
"""
from __future__ import annotations

import difflib
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
    # 2026-11-14 從 .run.log 的 [raw] 整理（1056 句實跑命中）。OpenCC 把已是繁體的字再轉一次：
    ("隻要", "只要", (), ("一隻", "兩隻", "三隻", "幾隻", "這隻", "那隻", "每隻")),
    ("隻有", "只有", (), ("一隻", "兩隻", "三隻", "幾隻", "這隻", "那隻", "每隻")),
    ("隻能", "只能", (), ("一隻", "兩隻", "三隻", "幾隻", "這隻", "那隻", "每隻")),
    ("隻適合", "只適合", (), ()),
    ("查檢視", "查查看", (), ()),
    ("遊標", "游標", (), ()),
    ("剋制", "克制", (), ()),
    # 同音或近音的聽錯：need＝使用者講正字時旁邊出現過的詞，block＝原詞才是對的時會出現的詞
    ("認識", "潤飾", ("標點", "LLM", "模型", "校對", "叫對", "贅詞", "千問", "Gemini", "錯字", "repo"),
     ("不認識", "認識你", "認識我", "認識他", "認識的人", "朋友")),
    ("report", "repo", ("github", "抓下來", "拉進來", "同一個", "clone", "push", "commit"),
     ("報告", "QA", "週報", "summary", "inspection")),
    ("cloud", "Claude", ("訂閱", "opus", "sonnet", "sonet", "haiku", "fable", "session", "外殼", "桌面app", ".md",
                         "remote control"),
     ("雲端", "cloudflare", "cloudfair", "icloud", "google cloud", "azure", "aws", "上cloud", "上 cloud",
      "cloud code")),  # cloud code 留給詞庫的 cloud code→Claude Code
    ("黨名", "檔名", ("唸", "念", "檔案", "路徑", "資料夾", "bit", "fp16"), ("政黨", "國民黨", "民進黨", "共產黨", "選舉", "黨員")),
    ("粵文", "越文", ("越南", "中文", "翻譯", "翻成", "HTML"), ("粵語", "廣東", "香港")),
    ("閱文", "越文", ("越南", "ASR", "翻譯", "翻成"), ("小說", "閱文集團")),
    ("插畫", "插話", ("語音", "講話", "說話", "打斷", "停掉", "停止"), ("插畫家", "繪", "圖", "風格")),
    ("城市", "程式", ("跑", "寫", "執行", "指令碼", "腳本", "bug"), ("台北", "臺北", "胡志明", "國家", "市長", "旅遊", "住")),
    ("卡密", "commit", ("推", "push", "main", "分支"), ()),
    ("分值", "分支", ("推", "main", "merge", "衝突", "repo"), ("分數", "評分")),
    ("電瓶", "電平", ("輸入", "麥克風", "音量", "跳動"), ("機車", "汽車", "充電", "沒電")),
    ("catch", "cache", ("prompt", "快取", "命中"), ("try", "exception", "error")),
    ("回復", "回覆", ("郵件", "信", "mail", "訊息", "對話", "分鐘"), ("回復原狀", "回復正常", "回復健康")),
    ("最遲", "贅詞", ("移除", "刪", "語助詞", "潤飾"), ("期限", "之前", "以前")),
    ("頭號", "逗號", ("標點", "句號", "問號", "打"), ("頭號人物", "頭號敵人")),
    ("生辰", "生成", ("語音", "幾句", "聲音", "模型"), ("八字", "生日")),
    ("物化", "霧化", ("效果", "玻璃", "glass"), ("物化女性",)),
)

# OpenCC s2twp 換詞時會改掉意思的詞：轉換前先遮起來，轉完放回。文件≠檔案（document／file）；
# 「文件夾→資料夾」是對的，不遮。轉完已分不出原本講哪個，所以只能在轉換前處理。
# 綁定→繫結、對話框→對話方塊、字體→字型、通過→透過 同理（台灣口語照講，官方用語另當別論）。
# (簡繁都比對的樣式, 放回的繁體寫法)
OPENCC_KEEP = (
    (r"文件(?![夹夾])", "文件"),
    (r"[绑綁]定", "綁定"),
    (r"[对對][话話]框", "對話框"),
    (r"字[体體]", "字體"),
    (r"通[过過]", "通過"),
)
_KEEP_RE = re.compile("|".join(f"({pat})" for pat, _ in OPENCC_KEEP))
_KEEP_MARK = "\ue000"  # 私用區字元，OpenCC 不會動它


def opencc_convert(convert, text: str) -> str:
    """用 convert（OpenCC 的 convert）轉換，但 OPENCC_KEEP 的詞換成固定的繁體寫法、不給 OpenCC 換詞。"""
    kept = [OPENCC_KEEP[m.lastindex - 1][1] for m in _KEEP_RE.finditer(text)]
    out = convert(_KEEP_RE.sub(_KEEP_MARK, text))
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


# ── 聲音裁判 ─────────────────────────────────────────────────────────────────
# 文字規則只能猜；同一段音檔只放候選詞當熱詞再聽一次，聽出候選詞才採用。
# 2026-11-14 M2 實測（144 段合成語音）：拿掉前後文條件強制觸發 24 段反例，聲音擋下 22 段；第二輪約 0.17 秒。
# OpenCC 誤轉不是聽錯，聲音分不出來，不送裁判。
# 同音字也不送：2026-11-14 壓力測試裡寄給我／記給我、客訴、上限都被裁判改壞。可試的只剩韻母或音節不同的（認識→潤飾、report→repo）
JUDGE_SKIP = frozenset({"許可權", "型別", "賬", "隻要", "隻有", "隻能", "隻適合", "查檢視", "遊標", "剋制",
                        "cloud", "黨名", "粵文", "閱文", "插畫", "城市", "卡密", "分值", "電瓶", "catch", "回復",
                        "最遲", "頭號", "生辰", "物化"})
JUDGE_MAX_TERMS = 8


def judge_candidates(text: str, term_rules=()) -> list[str]:
    """第一輪文字裡出現「聽成」的詞 → 回傳要送去給聲音確認的正字（不看 need 條件，由聲音決定）。
    來源：ASR_RULES 與個人詞庫的變體（rule.variants → rule.text）。"""
    low = text.lower()
    cands: list[str] = []
    for heard, to, _need, block in ASR_RULES:
        # 不看 need（缺前後文正是要問聲音的情況），但 block 詞在就代表原詞是對的（北美洲、全線通車）
        if heard not in JUDGE_SKIP and heard.lower() in low and not any(b.lower() in low for b in block):
            cands.append(to)
    for rule in term_rules or ():
        target = getattr(rule, "text", "")
        for variant in getattr(rule, "variants", ()) or ():
            if variant and variant != target and variant in text:
                cands.append(target)
                break
    return list(dict.fromkeys(c for c in cands if c and c not in text))[:JUDGE_MAX_TERMS]


JUDGE_MIN_SIMILAR = 0.6  # 第二輪應是同一句話只改幾個字；差太多＝照抄熱詞或聽成別的，不採用


def audio_judge(text: str, cands: list[str], rerun) -> str:
    """rerun(cands) 用同一段音檔、只放 cands 當熱詞再辨識一次。聽出任一候選詞、且整句和第一輪大致相同，才採用第二輪。
    rerun 失敗（例外或空字串）就維持第一輪。"""
    if not cands:
        return text
    try:
        second = rerun(cands) or ""
    except Exception:  # noqa: BLE001 — 裁判是加分項，失敗不能讓整句不見
        return text
    if not any(c in second for c in cands):
        return text
    a, b = _PUNCT.sub("", text), _PUNCT.sub("", second)
    if difflib.SequenceMatcher(None, a, b).ratio() < JUDGE_MIN_SIMILAR:
        return text
    return second
