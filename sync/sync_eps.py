"""
sync/sync_eps.py — 季 EPS 同步（官方來源，免 token）

兩種資料來源 / 模式：
  1. 最新一季（預設）— TWSE/TPEX OpenAPI「綜合損益彙總表」
     - 上市：https://openapi.twse.com.tw/v1/opendata/t187ap14_L
     - 上櫃：https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O
     - 乾淨 JSON，含 基本每股盈餘 + 產業別 → 同時補全 stock_info 產業別
     - 只給「最新一季」，適合季報後排程（一年 4 次）

  2. 歷史回補（--backfill）— 公開資訊觀測站 MOPS ajax_t163sb04
     - https://mopsov.twse.com.tw/mops/web/ajax_t163sb04
     - 可帶 year(民國)+season 查任意歷史季，逐季回傳全市場綜合損益表
     - 用於一次回補多年歷史 EPS

同步時機（EPS 為季資料，不需每日）：
  - 台股財報公告期限：Q1→5/15、Q2→8/14、Q3→11/14、Q4(年報)→隔年3/31
  - 建議在各季報公告後各跑一次（一年 4 次）即可。

副作用：
  - 會一併 upsert stock_info（公司名稱/市場別，OpenAPI 模式另含產業別），
    確保 stock_eps 的 FK 成立。

使用方式：
  python sync/sync_eps.py                                  # 同步最新一季（OpenAPI）
  python sync/sync_eps.py --backfill --from-year 2021 --to-year 2025   # 回補歷史
  python sync/sync_eps.py --backfill --years 5             # 回補近 5 年
"""
from __future__ import annotations

import argparse
import io
import sys
import os
import time
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault(
    "STREAMLIT_SECRETS_TOML",
    str(_PROJECT_ROOT / ".streamlit" / "secrets.toml"),
)

import streamlit as st   # noqa: E402
import db                # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TWSE_EPS_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap14_L"
TPEX_EPS_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ------------------------------------------------------------------
# 解析輔助
# ------------------------------------------------------------------

def _get(row: dict, *keys, default=None):
    """依序嘗試多個 key（兩端點欄位名不同），回傳第一個存在且非空的值。"""
    for k in keys:
        if k in row and str(row[k]).strip() != "":
            return row[k]
    return default


def _to_float(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").strip())
        return v
    except (ValueError, TypeError, AttributeError):
        return None


def _roc_to_ad_year(roc) -> int | None:
    try:
        return int(str(roc).strip()) + 1911
    except (ValueError, TypeError):
        return None


def _parse_roc_date(roc_str) -> date | None:
    """民國日期 1150614 → date(2026,6,14)。"""
    try:
        s = str(roc_str).strip()
        if len(s) == 7:
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
    except (ValueError, TypeError):
        pass
    return None


# ------------------------------------------------------------------
# 抓取
# ------------------------------------------------------------------

def _fetch(url: str, market: str, suffix: str) -> list[dict]:
    """抓單一端點並正規化為統一結構。market=上市/上櫃，suffix=.TW/.TWO。"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        rows = r.json()
    except Exception as e:
        log.warning(f"{market} EPS API 失敗：{e}")
        return []

    out: list[dict] = []
    for row in rows:
        raw_code = _get(row, "公司代號", "SecuritiesCompanyCode")
        if not raw_code:
            continue
        year = _roc_to_ad_year(_get(row, "年度", "Year"))
        quarter = _to_float(_get(row, "季別"))
        eps = _to_float(_get(row, "基本每股盈餘（元）", "基本每股盈餘", "基本每股盈餘(元)"))
        if year is None or quarter is None:
            continue
        out.append({
            "code":           f"{str(raw_code).strip()}{suffix}",
            "name":           str(_get(row, "公司名稱", "CompanyName", default="")).strip(),
            "market":         market,
            "industry":       str(_get(row, "產業別", default="")).strip() or None,
            "fiscal_year":    year,
            "fiscal_quarter": int(quarter),
            "eps":            eps,
            "announced_date": _parse_roc_date(_get(row, "出表日期", "Date")),
        })
    log.info(f"{market} EPS：取得 {len(out)} 筆（最新一季）")
    return out


def fetch_all_eps() -> list[dict]:
    data = _fetch(TWSE_EPS_URL, "上市", ".TW")
    time.sleep(1.0)
    data += _fetch(TPEX_EPS_URL, "上櫃", ".TWO")
    return data


# ------------------------------------------------------------------
# 歷史回補：MOPS ajax_t163sb04（可帶 year/season 查任意歷史季）
# ------------------------------------------------------------------

MOPS_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04"
MOPS_DELAY = 3.0  # MOPS 對頻繁請求較敏感，拉長間隔

# TYPEK → (市場別, 代碼後綴)
MOPS_MARKETS = {"sii": ("上市", ".TW"), "otc": ("上櫃", ".TWO")}


def _norm_code(raw) -> str | None:
    """把表格中的代號（可能是 2330.0 或 '2330'）正規化為純代號字串。"""
    try:
        return str(int(float(str(raw).strip())))
    except (ValueError, TypeError):
        return None


def _fetch_mops_quarter(roc_year: int, season: int, typek: str) -> list[dict]:
    """抓 MOPS 單一季別、單一市場的全市場綜合損益表，解析出 (code, name, eps)。"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    market, suffix = MOPS_MARKETS[typek]
    payload = {
        "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
        "TYPEK": typek, "year": str(roc_year), "season": f"{season:02d}",
    }
    headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        r = requests.post(MOPS_URL, data=payload, headers=headers, timeout=30, verify=False)
        tables = pd.read_html(io.StringIO(r.text))
    except ValueError:
        # 無表格（該季尚未公告或無資料）
        return []
    except Exception as e:
        log.warning(f"MOPS {roc_year}Q{season} {market} 失敗：{e}")
        return []

    out: list[dict] = []
    for t in tables:
        cols = [str(c) for c in t.columns]
        code_cols = [c for c in cols if "代號" in c]
        eps_cols = [c for c in cols if "每股盈餘" in c]
        if not (code_cols and eps_cols):
            continue
        name_cols = [c for c in cols if "名稱" in c]
        code_col, eps_col = code_cols[0], eps_cols[0]
        name_col = name_cols[0] if name_cols else None
        for _, row in t.iterrows():
            code = _norm_code(row[code_col])
            eps = _to_float(row[eps_col])
            if code is None or eps is None:
                continue
            out.append({
                "code":           f"{code}{suffix}",
                "name":           str(row[name_col]).strip() if name_col else code,
                "market":         market,
                "industry":       None,  # MOPS 表以產業分組，逐列無產業欄；留待 OpenAPI 補
                "fiscal_year":    roc_year + 1911,
                "fiscal_quarter": season,
                "eps":            eps,
                "announced_date": None,
            })
    return out


def backfill_eps(from_year: int, to_year: int) -> list[dict]:
    """回補 [from_year, to_year]（西元）各季、上市+上櫃的 EPS。"""
    data: list[dict] = []
    for ad_year in range(from_year, to_year + 1):
        roc_year = ad_year - 1911
        for season in (1, 2, 3, 4):
            for typek in MOPS_MARKETS:
                rows = _fetch_mops_quarter(roc_year, season, typek)
                if rows:
                    log.info(f"MOPS {ad_year} Q{season} {MOPS_MARKETS[typek][0]}：{len(rows)} 筆")
                    data += rows
                time.sleep(MOPS_DELAY)
    return data


# ------------------------------------------------------------------
# 寫入
# ------------------------------------------------------------------

def _upsert_stock_info(rows: list[dict]):
    """確保所有公司存在 stock_info（FK 前提），並補全名稱/產業別。"""
    n = 0
    for r in rows:
        db.execute(
            """
            INSERT INTO stock_info (stock_code, stock_name, market_type, industry_type)
            VALUES (:code, :name, :market, :industry)
            ON DUPLICATE KEY UPDATE
                stock_name    = VALUES(stock_name),
                market_type   = VALUES(market_type),
                industry_type = COALESCE(VALUES(industry_type), industry_type)
            """,
            {"code": r["code"], "name": r["name"] or r["code"],
             "market": r["market"], "industry": r["industry"]},
        )
        n += 1
    log.info(f"stock_info upsert：{n} 筆")


def _upsert_eps(rows: list[dict]):
    n = 0
    for r in rows:
        if r["eps"] is None:
            continue
        db.execute(
            """
            INSERT INTO stock_eps
                (stock_code, fiscal_year, fiscal_quarter, eps, announced_date)
            VALUES (:code, :fy, :fq, :eps, :ad)
            ON DUPLICATE KEY UPDATE
                eps            = VALUES(eps),
                announced_date = VALUES(announced_date)
            """,
            {"code": r["code"], "fy": r["fiscal_year"], "fq": r["fiscal_quarter"],
             "eps": r["eps"], "ad": str(r["announced_date"]) if r["announced_date"] else None},
        )
        n += 1
    log.info(f"stock_eps upsert：{n} 筆")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="季 EPS 同步（官方來源）")
    parser.add_argument("--backfill", action="store_true",
                        help="歷史回補模式（MOPS），預設為最新一季（OpenAPI）")
    parser.add_argument("--from-year", type=int, default=None, help="回補起始西元年")
    parser.add_argument("--to-year", type=int, default=None, help="回補結束西元年")
    parser.add_argument("--years", type=int, default=None,
                        help="回補近 N 年（與 --from/--to 二擇一）")
    args = parser.parse_args()

    if args.backfill:
        this_year = date.today().year
        if args.years:
            from_year, to_year = this_year - args.years + 1, this_year
        elif args.from_year:
            from_year, to_year = args.from_year, args.to_year or this_year
        else:
            log.error("--backfill 需指定 --years N 或 --from-year YYYY")
            return
        log.info(f"=== EPS 歷史回補（MOPS）：{from_year}~{to_year} ===")
        rows = backfill_eps(from_year, to_year)
    else:
        log.info("=== 季 EPS 同步（OpenAPI，最新一季）===")
        rows = fetch_all_eps()

    if not rows:
        log.warning("未取得任何 EPS 資料。")
        return

    seasons = sorted({(r["fiscal_year"], r["fiscal_quarter"]) for r in rows})
    log.info(f"本次資料涵蓋季別：{seasons[:8]}{' …' if len(seasons) > 8 else ''}")

    _upsert_stock_info(rows)
    _upsert_eps(rows)
    log.info("✅ EPS 同步完畢")


if __name__ == "__main__":
    main()
