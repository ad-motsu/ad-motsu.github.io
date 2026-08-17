#!/usr/bin/env python3
"""
コミケ公式サイトから、指定した回の関連ページを取得して日付候補を抜き出す。

    python3 tools/check.py 110

やること:
  1. URLの規則からページ候補を組み立てて取得する
  2. ISO-2022-JP を UTF-8 に直す（公式サイトの文字コード）
  3. 全角数字を半角にそろえて、日付らしい箇所を前後の文脈ごと抜き出す
  4. out/ に本文を保存し、日付候補を一覧で表示する

やらないこと:
  どれが何の締切かの判断。そこは人間（またはClaude Code）がやる。
  このスクリプトは「探す手間」だけを消すもので、読み取りは代行しない。
"""

import argparse
import datetime
import html as htmlmod
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://www.comiket.co.jp"
UA = "Mozilla/5.0 (compatible; personal-memo-checker/1.0)"
OUT = Path(__file__).parent / "out"

# 回ごとに変わるページ。{n} に回数が入る
PER_EVENT = [
    ("申込書セット案内", "appset", "/info-c/C{n}/C{n}Appset.html"),
    ("申込書セットPDF", "appset_pdf", "/info-c/C{n}/C{n}Appset.pdf"),
    ("ジャンルコード一覧", "genre", "/info-c/C{n}/C{n}genre.html"),
    ("サークル受付の説明", "registration", "/info-c/C{n}/C{n}CircleRegistration.html"),
    ("搬入・搬出の手引き", "carrying", "/info-c/C{n}/C{n}HandbookOfCarrying-c/"),
]

# 回に関係なく同じURLのページ
FIXED = [
    ("申込サークルサポート", "support", "/info-c/index.html"),
    ("オンライン申込サポート", "e_application", "/info-c/e_application/e_application.html"),
    ("Webカタログ サークル側について", "cdbms", "/cdbms/"),
]

# 日付らしい箇所の近くにあると意味を持つ言葉
KEYWORDS = ["締切", "〆切", "消印", "申込", "受付", "期間", "開催",
            "発表", "提出", "搬入", "登録", "公開", "発送"]

DATE_RE = re.compile(
    r"(?:(?P<y>\d{4})年)?\s*"
    r"(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日"
    r"\s*(?:[（(](?P<w>[日月火水木金土])[)）])?"
    r"(?:\s*(?:(?P<hh_j>\d{1,2})時\s*(?:(?P<mm_j>\d{1,2})分)?|(?P<hh_c>\d{1,2}):(?P<mm_c>\d{1,2})(?::(?P<ss_c>\d{1,2}))?)(?:頃)?)?"
)

RANGE_CONT_RE = re.compile(
    r"^\s*[〜~–―ー-]\s*"
    r"(?P<d>\d{1,2})日"
    r"\s*(?:[（(](?P<w>[日月火水木金土])[)）])?"
    r"(?:\s*(?:(?P<hh_j>\d{1,2})時\s*(?:(?P<mm_j>\d{1,2})分)?|(?P<hh_c>\d{1,2}):(?P<mm_c>\d{1,2})(?::(?P<ss_c>\d{1,2}))?)(?:頃)?)?"
)


def fetch(url):
    """取得して (ステータス, 本文, バイト数) を返す。本文はHTMLのみ。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, "", 0
    except Exception as e:
        return f"ERR:{e}", "", 0

    if "pdf" in ctype.lower() or url.endswith(".pdf"):
        return 200, "", len(raw)

    # 文字コードの当たりをつける。公式は ISO-2022-JP が多い
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    cands = ([m.group(1)] if m else []) + ["iso-2022-jp", "utf-8", "cp932", "euc-jp"]
    for enc in cands:
        try:
            return 200, raw.decode(enc), len(raw)
        except (UnicodeDecodeError, LookupError):
            continue
    return 200, raw.decode("utf-8", errors="replace"), len(raw)


def strip_html(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = htmlmod.unescape(html)
    return re.sub(r"[ \t\u3000]+", " ", html)


def parse_candidate(y, m, d, hh_j, mm_j, hh_c, mm_c, default_year, month_inherited, text, start, end, flat):
    try:
        m_val = int(m)
        d_val = int(d)
    except (TypeError, ValueError):
        return None

    hh_str = hh_j or hh_c
    mm_str = mm_j or mm_c
    time_given = hh_str is not None
    hh_val = int(hh_str) if time_given else 23
    mm_val = int(mm_str) if (time_given and mm_str) else (0 if time_given else 59)
    ss_val = 0 if time_given else 59

    if not (1 <= m_val <= 12 and 1 <= d_val <= 31 and 0 <= hh_val <= 23 and 0 <= mm_val <= 59 and 0 <= ss_val <= 59):
        return None

    year_val = y or default_year
    iso = None
    if year_val:
        try:
            dt = datetime.datetime(
                int(year_val), m_val, d_val, hh_val, mm_val, ss_val,
                tzinfo=datetime.timezone(datetime.timedelta(hours=9))
            )
            iso = dt.isoformat()
        except (ValueError, OverflowError):
            return None

    ctx = flat[max(0, start - 45): end + 45].strip()
    hits = [k for k in KEYWORDS if k in ctx]
    if not hits:
        return None  # 日付だけあって文脈が無いものは捨てる

    return {
        "text": text.strip(),
        "at": iso,
        "year_guessed": y is None,
        "month_inherited": month_inherited,
        "time_given": time_given,
        "keywords": hits,
        "context": ctx,
    }


def find_dates(text, default_year=None):
    """日付候補を、前後の文脈つきで拾う。"""
    flat = unicodedata.normalize("NFKC", text)
    flat = re.sub(r"\s+", " ", flat)
    found = []
    for mt in DATE_RE.finditer(flat):
        g = mt.groupdict()
        cand = parse_candidate(
            g["y"], g["m"], g["d"], g["hh_j"], g["mm_j"], g["hh_c"], g["mm_c"],
            default_year, False, mt.group(0), mt.start(), mt.end(), flat
        )
        if cand:
            found.append(cand)

        # 終了日で「月」が省略された期間表現（例: 12月29日〜31日）を拾う
        cont_m = RANGE_CONT_RE.match(flat[mt.end():])
        if cont_m:
            cg = cont_m.groupdict()
            cont_start = mt.end()
            cont_end = mt.end() + cont_m.end()
            cont_cand = parse_candidate(
                g["y"], g["m"], cg["d"], cg["hh_j"], cg["mm_j"], cg["hh_c"], cg["mm_c"],
                default_year, True, cont_m.group(0), cont_start, cont_end, flat
            )
            if cont_cand:
                found.append(cont_cand)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("event", help="回数。例: 110")
    ap.add_argument("--year", help="年が書かれていない日付に補う年。例: 2027")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    n = args.event
    targets = [(lbl, sg, BASE + p.format(n=n)) for lbl, sg, p in PER_EVENT] + \
              [(lbl, sg, BASE + p) for lbl, sg, p in FIXED]

    report = []
    errors = 0
    for label, slug, url in targets:
        status, body, size = fetch(url)
        mark = "OK " if status == 200 else "-- "
        print(f"{mark}{status:<6} {label}")
        print(f"          {url}")

        entry = {"label": label, "url": url, "status": status, "dates": []}

        if status != 200:
            if isinstance(status, str) and status.startswith("ERR:"):
                errors += 1
                print("          （取得に失敗しました。通信環境を確認してください）\n")
            else:
                print("          （未公開か、URLの規則から外れています）\n")
            report.append(entry)
            continue

        if not body:
            print(f"          PDF {size:,} バイト。中身はブラウザかClaude Codeで開いてください\n")
            report.append(entry)
            continue

        text = strip_html(body)
        path = OUT / f"C{n}_{slug}.txt"
        path.write_text(text, encoding="utf-8")

        dates = find_dates(text, args.year)
        entry["dates"] = dates
        entry["saved"] = str(path)

        if dates:
            print(f"          日付候補 {len(dates)}件 → {path.name}")
            for d in dates[:6]:
                flag = ""
                if d.get("month_inherited"):
                    flag += " ※月を引き継ぎ"
                if d["year_guessed"] and d["at"]:
                    flag += " ※年を補完"
                elif not d["at"]:
                    flag += " ※年不明"
                print(f"            {d['text']:<22} [{'/'.join(d['keywords'])}]{flag}")
            if len(dates) > 6:
                print(f"            ... 他 {len(dates)-6}件")
        else:
            print(f"          日付候補なし → {path.name}")
        print()
        report.append(entry)

    (OUT / f"C{n}_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"詳細: {OUT / f'C{n}_report.json'}")
    print("\n次にやること: tools/PROMPT.md の内容を Claude Code に貼ってください。")
    if errors:
        print(f"※ {errors}件が通信エラーで取得できていません。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
