# =============================================================================
# 🔍 台股 60分K KD + 均線掃描系統  v4.9（撿股讚三頁財務整合版）
# =============================================================================
# ★ v4.9 財務品質資料來源升級（三頁撿股讚 → yfinance備援）：
#
#   資料來源優先順序：
#   [1] 撿股讚 p/75684「阿段投資心法」
#       → (季)營業毛利率Q1/Q2、(季)營業利益率Q1/Q2
#   [2] 撿股讚 p/75686「阿段+營收成」
#       → 同上 + (月)營收成長率、近期月增率
#   [3] 撿股讚 p/75683「戴維斯雙擊」（原有，YOY+MoM）
#       → YOY年增率M1/M2、MoM月增率
#   [4] yfinance quarterly_income_stmt（備援，無撿股讚資料時才用）
#   [5] FinMind API（TOKEN填入後可用，最精確）
#
#   v4.9 核心修改：
#   - 新增 fetch_wespai_fin_quality() 同時抓 p/75684 + p/75686
#   - 解析毛利率、營業利益率欄位，直接作為財務品質判斷依據
#   - yfinance 降為最後備援，避免抓錯資料（如威潤案例）
#   - 新增 VOLUME_MIN_LOTS 流動性過濾（日均量<500張不列入）
#   - MoM 阻擋條件升級：單月<-15% OR 連續2個月月減才擋
#   - 出場機制 v4.8 保留：60分K死叉需日K>60 + 豁免機制
#   ★ Bug fix：fetch_fin_quality_batch 新增 df_fin_wespai 參數
# =============================================================================

import yfinance as yf
import pandas as pd
import numpy as np
import json, os, time, warnings, logging, urllib.request
import pytz, re
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings('ignore')
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# ── 基本設定 ─────────────────────────────────────────────────────────────────
INTERVAL_MIN       = 15
K_THRESHOLD        = 30
FINMIND_TOKEN      = ''          # FinMind API Token（建議填入以取得更多財務資料）

# ── ☯ 卦象參數 ───────────────────────────────────────────────────────────────
CHANNEL_NO_ENTRY_PCT  = 80
CHANNEL_WARN_PCT      = 65
MA_SLOPE_BARS         = 7
A_CLASS_REQUIRE_60MA  = True
K_HIGH_NO_ENTRY       = 70

# ── ★ v4.4 出場/停損參數 ────────────────────────────────────────────────────
STOP_LOSS_PCT         = 0.07
TAKE_PROFIT_TRIGGER   = 0.15
TRAILING_STOP_PCT     = 0.08
EXIT_BELOW_MA42       = True
KD_EXIT_K_HIGH        = 75

# ── ☯ 地澤臨警戒 ─────────────────────────────────────────────────────────────
AUGUST_WARN_START     = (7, 15)
AUGUST_WARN_END       = (9, 30)
AUGUST_STOP_LOSS_PCT  = 0.05
AUGUST_K_THRESHOLD    = 20

# ── ☯ 坤卦互卦警示 ───────────────────────────────────────────────────────────
REGIME_OVERHEAT_SCORE = 85

# ── ★ v4.3 量增參數 ──────────────────────────────────────────────────────────
VOLUME_MA5_RATIO      = 2.0
VOLUME_CONSEC_DAYS    = 3
VOLUME_CONSEC_RATIO   = 1.2

# ── ★ v4.5 月營收月增率（MoM）門檻設定 ──────────────────────────────────────
MOM_A_THRESHOLD      =  0.0
MOM_B_THRESHOLD      = -3.0
MOM_C_THRESHOLD      = -7.0
MOM_C_POSITION_RATIO = 0.7
MOM_CACHE_MIN        = 120

# ── ★ v4.7 財務品質篩選參數 ──────────────────────────────────────────────────
GROSS_MARGIN_MIN       = 25.0   # 近一季毛利率門檻 (%)
OP_MARGIN_MIN          = 15.0   # 近一季營業利益率門檻 (%)
INTEREST_COVER_MIN     = 15.0   # 利息保障倍數門檻（倍）
GROSS_MARGIN_TOLERANCE = 1.0    # 毛利率趨勢容許誤差 (%)（允許小幅下滑）
OP_LEVERAGE_TOLERANCE  = 2.0    # 營業槓桿容許誤差 (%)
FIN_QUALITY_CACHE_MIN  = 360    # 財務品質快取分鐘（6小時）
FIN_QUALITY_THREADS    = 8      # 平行抓取執行緒數
FIN_BLOCK_ON_FAIL      = True   # True=不達標擋掉 / False=僅標記不擋

# ── 族群優先排序 ─────────────────────────────────────────────────────────────
PRIORITY_INDUSTRIES = [
    'AI', '伺服器', '散熱', '液冷', '機殼',
    'IC設計', '半導體', '晶圓', '封裝', '測試', '設備', '儀器',
    '連接器', 'PCB', '電路板', '被動元件', '電感', '電容',
    '網通', '交換器', '路由', '資安',
    '感測器', '影像', '光學', '鏡頭', 'CMOS',
    '電動車', '儲能', '電池', '充電',
]

# ── 基本面過濾 ───────────────────────────────────────────────────────────────
YOY_MIN_PCT   = 15.0
YOY_CACHE_MIN = 60

# ── ★ v4.6 主動式ETF 設定 ────────────────────────────────────────────────────
ETF_60M_K_BUY    = 30
ETF_DAY_K_STRONG = 25
ETF_CACHE_MIN    = 360

ACTIVE_ETF_BUILTIN = {
    '00905':  '統一台灣動能',
    '00933':  '國泰台灣領袖50',
    '00951':  '台新臺灣IC設計',
    '00952':  '凱基台灣Smart',
    '00953':  '群益半導體收益',
    '00954':  '統一MSCI台灣',
    '00955':  '富蘭克林台灣',
    '00956':  '元大台灣價值成長',
    '00980A': '主動野村臺灣優選',
    '00981A': '主動統一台股增長',
    '00982A': '主動群益台灣強棒',
    '00983A': '主動台新臺灣永續',
    '00984A': '主動凱基臺灣精選',
    '00985A': '主動野村台灣50',
    '00986A': '主動元大AI新優選',
    '00987A': '主動富邦台灣成長',
    '00988A': '主動兆豐台灣藍籌',
    '00989A': '主動永豐台灣優選',
    '00990A': '主動元大AI新經濟',
    '00991A': '主動復華未來50',
    '00992A': '主動群益科技創新',
    '00993A': '主動中信台灣趨勢',
    '00994A': '主動台新AI優選',
    '00995A': '主動安聯台灣科技',
    '00996A': '主動國泰台灣優選',
    '00997A': '主動統一台灣50',
    '00998A': '主動野村台灣關鍵',
    '00999A': '主動富蘭克林科技',
    '01000A': '主動凱基台灣成長',
    '0052':   '富邦科技',
    '00631L': '富邦台灣加權正二',
    '00670L': '富邦台灣加權正二(元大)',
}

EXCLUDE_INDUSTRIES = [
    '營建', '建設', '建築',
    '生技', '生物科技', '製藥', '醫藥',
    '金融', '銀行', '保險', '證券', '期貨',
    'ETF', '指數', '基金',
    '航運', '航空',
    '紡織', '食品',
]


# =============================================================================
# ★ v4.5 月增率分級函式
# =============================================================================
def classify_mom(mom_pct) -> str:
    if mom_pct is None or (isinstance(mom_pct, float) and pd.isna(mom_pct)):
        return 'NA'
    if mom_pct >= MOM_A_THRESHOLD:
        return 'A'
    if mom_pct >= MOM_B_THRESHOLD:
        return 'B'
    if mom_pct >= MOM_C_THRESHOLD:
        return 'C'
    return 'W'


def mom_label(mom_pct, mom_grade) -> str:
    if mom_grade == 'NA':
        return '📊?MoM:N/A'
    icons = {'A': '📊✅', 'B': '📊~', 'C': '📊⚠', 'W': '📊🔴'}
    icon = icons.get(mom_grade, '📊?')
    return f'{icon}MoM:{mom_pct:+.1f}%'


def mom_allows_entry(mom_grade: str) -> tuple:
    if mom_grade == 'W':
        return False, f'📊月增率衰退>{abs(MOM_C_THRESHOLD):.0f}%，列觀察股不進場'
    return True, ''


# =============================================================================
# ★ v4.5 MoM 資料抓取（三層備援）
# =============================================================================
_mom_cache = {'data': None, 'last_update': 0}


def fetch_mom_finmind(codes: list) -> dict:
    if not FINMIND_TOKEN:
        return {}
    now = datetime.now()
    start = (now - timedelta(days=60)).strftime('%Y-%m-%d')
    result = {}
    batch = codes[:200]
    print(f'  [MoM-FinMind] 📡 嘗試抓取 {len(batch)} 檔月增率...')
    for code in batch:
        try:
            url = (f'https://api.finmindtrade.com/api/v4/data'
                   f'?dataset=TaiwanStockMonthRevenue&data_id={code}'
                   f'&start_date={start}&token={FINMIND_TOKEN}')
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = json.loads(r.read())
            if raw.get('status') != 200 or not raw.get('data'):
                continue
            rows = sorted(raw['data'], key=lambda x: x.get('date', ''), reverse=True)
            if rows:
                mom = rows[0].get('revenue_month_MoM')
                if mom is not None:
                    result[str(code).strip()] = float(mom)
            time.sleep(0.05)
        except Exception:
            continue
    print(f'  [MoM-FinMind] ✅ 取得 {len(result)} 檔月增率')
    return result


def _load_local_mom_csv() -> dict:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'revenue_mom.csv')
    if not os.path.exists(p):
        return {}
    try:
        df = pd.read_csv(p, dtype={'code': str})
        df['code'] = df['code'].str.strip()
        df['mom_pct'] = pd.to_numeric(df['mom_pct'], errors='coerce')
        result = {r['code']: r['mom_pct']
                  for _, r in df.iterrows()
                  if not pd.isna(r['mom_pct'])}
        print(f'  [MoM-CSV] ✅ 載入 {len(result)} 筆月增率')
        return result
    except Exception as e:
        print(f'  [MoM-CSV] ❌ {e}')
        return {}


def _parse_wespai_mom(html: str) -> dict:
    result = {}
    table_pat = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
    row_pat   = re.compile(r'<tr[^>]*>(.*?)</tr>',       re.DOTALL | re.IGNORECASE)
    cell_pat  = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)
    tag_pat   = re.compile(r'<[^>]+>')
    clean     = lambda s: tag_pat.sub('', s).strip()

    for tbl in table_pat.findall(html):
        rows = row_pat.findall(tbl)
        if len(rows) < 3:
            continue
        headers = [clean(c) for c in cell_pat.findall(rows[0])]

        def find_col(kws):
            for kw in kws:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return None

        col_code = find_col(['代號', '股票代號', 'code'])
        col_mom  = find_col(['月增', 'MoM', 'mom', '月增率'])
        if col_code is None or col_mom is None:
            continue

        for row in rows[1:]:
            cells = [clean(c) for c in cell_pat.findall(row)]
            needed = max(col_code, col_mom)
            if len(cells) <= needed:
                continue
            try:
                code = cells[col_code].strip()
                if not re.match(r'^\d{4,6}$', code):
                    continue
                mom_s = cells[col_mom].replace('%', '').replace(',', '').strip()
                mom_v = float(mom_s)
                result[code] = mom_v
            except Exception:
                continue
        if result:
            break
    return result


def fetch_mom_data(codes: list, wespai_html: str = None, force=False) -> dict:
    now, cache = time.time(), _mom_cache
    if (not force and cache['data'] is not None
            and now - cache['last_update'] < MOM_CACHE_MIN * 60):
        return cache['data']

    result = {}

    if wespai_html:
        result = _parse_wespai_mom(wespai_html)
        if result:
            print(f'  [MoM] ✅ 從撿股讚解析到 {len(result)} 檔月增率')

    if FINMIND_TOKEN:
        missing = [c for c in codes if c not in result]
        if missing:
            fm_data = fetch_mom_finmind(missing)
            result.update(fm_data)

    if not result:
        result = _load_local_mom_csv()

    if not result:
        print('  [MoM] ℹ️  無月增率資料，MoM 欄位將顯示 N/A（不影響進場）')

    cache['data'], cache['last_update'] = result, now
    return result


# =============================================================================
# ☯ v4.4 警戒狀態判斷
# =============================================================================
def is_august_warning_period(dt=None) -> bool:
    if dt is None:
        dt = datetime.now(pytz.timezone('Asia/Taipei'))
    m, d = dt.month, dt.day
    sm, sd = AUGUST_WARN_START
    em, ed = AUGUST_WARN_END
    start_ok = (m > sm) or (m == sm and d >= sd)
    end_ok   = (m < em) or (m == em and d <= ed)
    return start_ok and end_ok


def get_active_params(dt=None):
    if is_august_warning_period(dt):
        return {
            'stop_loss_pct': AUGUST_STOP_LOSS_PCT,
            'k_threshold':   AUGUST_K_THRESHOLD,
            'warning':       True,
            'warning_msg':   '☯【臨卦警戒】至于八月有凶｜停損收緊至5%，K門檻提高至20，謹慎操作',
        }
    return {
        'stop_loss_pct': STOP_LOSS_PCT,
        'k_threshold':   K_THRESHOLD,
        'warning':       False,
        'warning_msg':   '',
    }


# =============================================================================
# ★ v4.7 財務品質篩選（毛利率 / 營業利益率 / 利息保障倍數）
# =============================================================================
_fin_cache = {}   # code -> (timestamp, result_dict)


def _to_float(v):
    """安全轉換 float，失敗回 None"""
    try:
        f = float(v)
        return f if pd.notna(f) else None
    except Exception:
        return None


def _yf_row(df, *keys):
    """從 DataFrame index 中嘗試多個 key，回傳第一個命中的 row"""
    for k in keys:
        if k in df.index:
            return df.loc[k]
    return None


def _fetch_quarters_yfinance(code: str, mtype: str) -> list:
    """
    從 yfinance 抓取季度損益表，回傳最多 4 筆季度資料 list（最新在前）。
    每筆包含：period, revenue, gross_profit, op_income, interest_expense, ebit
    """
    suffixes = ['.TW', '.TWO'] if mtype != 'TWO' else ['.TWO', '.TW']
    for sfx in suffixes:
        try:
            t = yf.Ticker(code + sfx)
            qf = None
            for attr in ['quarterly_income_stmt', 'quarterly_financials']:
                try:
                    candidate = getattr(t, attr, None)
                    if candidate is not None and not candidate.empty:
                        qf = candidate
                        break
                except Exception:
                    pass
            if qf is None or qf.empty or qf.shape[1] < 1:
                continue

            qf = qf.sort_index(axis=1, ascending=False)

            quarters = []
            for col in qf.columns[:4]:
                rev_row  = _yf_row(qf,
                    'Total Revenue', 'Operating Revenue', 'Revenue',
                    'Net Revenue', 'TotalRevenue')
                gp_row   = _yf_row(qf, 'Gross Profit', 'GrossProfit')
                op_row   = _yf_row(qf,
                    'Operating Income', 'Operating Income Loss',
                    'Operating Profit', 'OperatingIncome', 'EBIT')
                ie_row   = _yf_row(qf,
                    'Interest Expense', 'Interest Expense Non Operating',
                    'InterestExpense', 'Net Interest Income',
                    'Interest And Debt Expense')
                ebit_row = _yf_row(qf, 'EBIT', 'Pretax Income',
                    'Income Before Tax', 'EarningsBeforeInterestAndTaxes')

                rev  = _to_float(rev_row[col])  if rev_row  is not None else None
                gp   = _to_float(gp_row[col])   if gp_row   is not None else None
                op   = _to_float(op_row[col])   if op_row   is not None else None
                ie   = _to_float(ie_row[col])   if ie_row   is not None else None
                ebit = _to_float(ebit_row[col]) if ebit_row is not None else op

                if ie is not None:
                    ie = abs(ie)

                quarters.append({
                    'period':           str(col)[:10],
                    'revenue':          rev,
                    'gross_profit':     gp,
                    'op_income':        op,
                    'interest_expense': ie,
                    'ebit':             ebit if ebit is not None else op,
                })

            if quarters and any(q.get('revenue') is not None for q in quarters):
                return quarters
        except Exception:
            continue
    return []


def _fetch_quarters_finmind(code: str) -> list:
    """從 FinMind TaiwanStockFinancialStatements 抓取季度損益資料。"""
    if not FINMIND_TOKEN:
        return []
    start = (datetime.now() - timedelta(days=450)).strftime('%Y-%m-%d')
    url = (f'https://api.finmindtrade.com/api/v4/data'
           f'?dataset=TaiwanStockFinancialStatements&data_id={code}'
           f'&start_date={start}&token={FINMIND_TOKEN}')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read())
        if raw.get('status') != 200 or not raw.get('data'):
            return []
        df = pd.DataFrame(raw['data'])
        if 'type' not in df.columns or 'value' not in df.columns:
            return []

        date_col = 'date' if 'date' in df.columns else df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        df = df.dropna(subset=[date_col]).sort_values(date_col, ascending=False)

        TYPE_MAP = {
            'Revenue': 'revenue', 'Revenues': 'revenue',
            'GrossProfit': 'gross_profit', 'Gross Profit': 'gross_profit',
            'OperatingIncome': 'op_income', 'OperatingIncomeLoss': 'op_income',
            'Operating Income': 'op_income',
            'InterestExpense': 'interest_expense',
            'InterestExpenseNonOperating': 'interest_expense',
            'Interest Expense': 'interest_expense',
            'EBIT': 'ebit', 'EarningsBeforeInterestAndTaxes': 'ebit',
        }

        quarters = {}
        for _, row in df.iterrows():
            period = str(row[date_col])[:7]
            mapped = TYPE_MAP.get(str(row.get('type', '')))
            if mapped is None:
                continue
            if period not in quarters:
                quarters[period] = {'period': period}
            try:
                quarters[period][mapped] = float(row['value'])
            except Exception:
                pass

        result = sorted(quarters.values(), key=lambda x: x['period'], reverse=True)
        for q in result:
            if 'ebit' not in q:
                q['ebit'] = q.get('op_income')
        return result[:4]
    except Exception:
        return []


def eval_fin_quality(quarters: list) -> dict:
    """
    根據季度資料計算財務品質指標。
    - 有資料且不達標 → fin_pass=False
    - 有資料且達標   → fin_pass=True
    - 無資料         → available=False, fin_pass=None（放行）
    """
    result = {
        'available':            False,
        'gross_margin_q1':      None,
        'gross_margin_q2':      None,
        'gross_margin_ok':      None,
        'op_margin_q1':         None,
        'op_margin_ok':         None,
        'op_leverage_ok':       None,
        'rev_growth':           None,
        'op_growth':            None,
        'interest_coverage':    None,
        'interest_coverage_ok': None,
        'fin_pass':             None,
        'fin_fail_reasons':     [],
        'source':               'none',
    }

    if not quarters:
        return result

    q1 = quarters[0]
    q2 = quarters[1] if len(quarters) >= 2 else {}

    if q1.get('revenue') is None:
        return result

    result['available'] = True
    checks = []

    rev1 = q1.get('revenue')
    gp1  = q1.get('gross_profit')
    op1  = q1.get('op_income')
    ie1  = q1.get('interest_expense')
    eb1  = q1.get('ebit') or op1

    rev2 = q2.get('revenue')     if q2 else None
    gp2  = q2.get('gross_profit') if q2 else None
    op2  = q2.get('op_income')   if q2 else None

    # ── 條件一：毛利率 ≥ 35%（連續兩季持平或走揚）───────────────────────────
    if rev1 and rev1 > 0 and gp1 is not None:
        gm1 = gp1 / rev1 * 100
        result['gross_margin_q1'] = round(gm1, 1)

        gm2 = None
        if rev2 and rev2 > 0 and gp2 is not None:
            gm2 = gp2 / rev2 * 100
            result['gross_margin_q2'] = round(gm2, 1)

        threshold_ok = gm1 >= GROSS_MARGIN_MIN
        trend_ok     = (gm2 is None) or (gm1 >= gm2 - GROSS_MARGIN_TOLERANCE)
        gm_ok = threshold_ok and trend_ok
        result['gross_margin_ok'] = gm_ok
        checks.append(gm_ok)

        if not threshold_ok:
            result['fin_fail_reasons'].append(f'毛利{gm1:.1f}%<{GROSS_MARGIN_MIN:.0f}%')
        elif not trend_ok:
            result['fin_fail_reasons'].append(f'毛利下滑({gm1:.1f}%←{gm2:.1f}%)')

    # ── 條件二A：營業利益率 ≥ 15% ────────────────────────────────────────────
    if rev1 and rev1 > 0 and op1 is not None:
        om1 = op1 / rev1 * 100
        result['op_margin_q1'] = round(om1, 1)
        om_ok = om1 >= OP_MARGIN_MIN
        result['op_margin_ok'] = om_ok
        checks.append(om_ok)
        if not om_ok:
            result['fin_fail_reasons'].append(f'營業利{om1:.1f}%<{OP_MARGIN_MIN:.0f}%')

        # ── 條件二B：營業利益增幅 > 營收增幅（營業槓桿）─────────────────────
        if rev2 and rev2 != 0 and op2 is not None and op2 != 0:
            rev_growth = (rev1 - rev2) / abs(rev2) * 100
            op_growth  = (op1  - op2)  / abs(op2)  * 100
            result['rev_growth'] = round(rev_growth, 1)
            result['op_growth']  = round(op_growth, 1)
            lev_ok = op_growth >= rev_growth - OP_LEVERAGE_TOLERANCE
            result['op_leverage_ok'] = lev_ok
            checks.append(lev_ok)
            if not lev_ok:
                result['fin_fail_reasons'].append(
                    f'營業利增({op_growth:.1f}%)≤營收增({rev_growth:.1f}%)')

    # ── 條件三：利息保障倍數 > 20 倍 ─────────────────────────────────────────
    if ie1 is not None and ie1 > 100:
        if eb1 is not None:
            ic = eb1 / ie1
            result['interest_coverage'] = round(ic, 1)
            ic_ok = ic > INTEREST_COVER_MIN
            result['interest_coverage_ok'] = ic_ok
            checks.append(ic_ok)
            if not ic_ok:
                result['fin_fail_reasons'].append(
                    f'利息保障{ic:.1f}x<{INTEREST_COVER_MIN:.0f}x')
    else:
        # 無或極小利息支出 → 視為無限倍 → 自動通過
        result['interest_coverage']    = None
        result['interest_coverage_ok'] = True

    # ── 綜合判斷 ──────────────────────────────────────────────────────────────
    if checks:
        result['fin_pass'] = all(checks)
    else:
        result['fin_pass'] = None   # 沒有任何有效 check → 放行

    return result


def fetch_fin_quality_single(code: str, mtype: str, force: bool = False,
                             df_fin_wespai: pd.DataFrame = None) -> dict:
    """
    抓取並評估單一股票財務品質（含快取）。
    v4.9 優先順序：撿股讚財務頁 → FinMind → yfinance
    """
    now    = time.time()
    cached = _fin_cache.get(code)
    if not force and cached and now - cached[0] < FIN_QUALITY_CACHE_MIN * 60:
        return cached[1]

    result = None
    source = 'none'

    # ── [1] 撿股讚財務頁（最可靠，直接抓整理好的季度比率）─────────────────
    if df_fin_wespai is not None and not df_fin_wespai.empty:
        r = get_wespai_fin_quality(code, df_fin_wespai)
        if r.get('available'):
            result = r
            source = 'wespai'

    # ── [2] FinMind（需 TOKEN）────────────────────────────────────────────────
    if result is None and FINMIND_TOKEN:
        quarters = _fetch_quarters_finmind(code)
        if quarters:
            result = eval_fin_quality(quarters)
            source = 'finmind'

    # ── [3] yfinance（最後備援）──────────────────────────────────────────────
    if result is None:
        quarters = _fetch_quarters_yfinance(code, mtype)
        if quarters:
            result = eval_fin_quality(quarters)
            source = 'yfinance'

    if result is None:
        result = eval_fin_quality([])

    result['source']  = source
    _fin_cache[code]  = (now, result)
    return result


# =============================================================================
# ★ v4.9 Bug Fix：fetch_fin_quality_batch 加入 df_fin_wespai 參數
# =============================================================================
def fetch_fin_quality_batch(stock_dict: dict, force: bool = False,
                            df_fin_wespai: pd.DataFrame = None) -> dict:
    """批量抓取所有股票的財務品質（多執行緒）。"""
    codes   = list(stock_dict.keys())
    results = {}
    now     = time.time()

    to_fetch = []
    for code in codes:
        cached = _fin_cache.get(code)
        if not force and cached and now - cached[0] < FIN_QUALITY_CACHE_MIN * 60:
            results[code] = cached[1]
        else:
            to_fetch.append(code)

    if not to_fetch:
        print(f'  [財務品質] ✅ 全部 {len(results)} 檔命中快取')
        return results

    print(f'  [財務品質] 📡 批量抓取 {len(to_fetch)} 檔財務資料（{FIN_QUALITY_THREADS}執行緒）...')

    def _worker(code):
        mtype = _guess_mtype(code)
        return code, fetch_fin_quality_single(code, mtype, force=force,
                                              df_fin_wespai=df_fin_wespai)

    done = 0
    with ThreadPoolExecutor(max_workers=FIN_QUALITY_THREADS) as ex:
        futures = {ex.submit(_worker, c): c for c in to_fetch}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                c, r = fut.result()
                results[c] = r
                _fin_cache[c] = (now, r)
            except Exception:
                empty = eval_fin_quality([])
                results[code] = empty
                _fin_cache[code] = (now, empty)
            done += 1
            if done % 10 == 0 or done == len(to_fetch):
                print(f'  [財務品質] 進度 {done}/{len(to_fetch)}...', end='\r')

    print()
    available = sum(1 for r in results.values() if r.get('available'))
    passed    = sum(1 for r in results.values() if r.get('fin_pass') is True)
    failed    = sum(1 for r in results.values() if r.get('fin_pass') is False)
    no_data   = sum(1 for r in results.values() if not r.get('available'))
    print(f'  [財務品質] ✅ 有資料:{available}檔  達標:{passed}  不達標:{failed}  無資料(放行):{no_data}')
    return results


def build_fin_quality_filter(fund_set: set, fin_quality_dict: dict) -> set:
    """
    從 YOY 達標的 fund_set 中，進一步過濾財務品質不達標者。
    FIN_BLOCK_ON_FAIL=False 時直接回傳原 fund_set（僅標記）。
    """
    if not FIN_BLOCK_ON_FAIL:
        return fund_set

    passed = set()
    for code in fund_set:
        fq = fin_quality_dict.get(code)
        if fq is None or not fq.get('available'):
            passed.add(code)              # 無資料 → 放行
        elif fq.get('fin_pass') is not False:
            passed.add(code)              # True 或 None → 放行
        # fin_pass is False → 排除
    return passed


# =============================================================================
# ★ v4.6 主動式ETF 清單抓取與分析
# =============================================================================
_etf_cache = {'data': None, 'last_update': 0}


def _is_active_etf_code(code: str) -> bool:
    return bool(re.search(r'[A-Za-z]', code)) and code.startswith('00')


def fetch_active_etf_list(force=False) -> dict:
    now, cache = time.time(), _etf_cache
    if not force and cache['data'] is not None and now - cache['last_update'] < ETF_CACHE_MIN * 60:
        return cache['data']

    result = dict(ACTIVE_ETF_BUILTIN)
    try:
        twse_url = 'https://www.twse.com.tw/zh/ETF/fund/TWETFall'
        req = urllib.request.Request(
            twse_url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = json.loads(r.read())
        if raw.get('stat') == 'OK':
            for row in raw.get('data', []):
                if not row: continue
                code = str(row[0]).strip()
                name = str(row[1]).strip() if len(row) > 1 else ''
                if _is_active_etf_code(code) and code not in result:
                    result[code] = name
            print(f'  [主動ETF] ✅ TWSE補充後共 {len(result)} 檔')
    except Exception as e:
        print(f'  [主動ETF] ℹ️  TWSE抓取失敗({e})，使用內建清單 {len(result)} 檔')

    cache['data'], cache['last_update'] = result, now
    return result


def analyze_etf(tid: str, name: str, active_params: dict) -> dict:
    mtype = 'TW'
    result = {
        'tid': tid, 'name': name, 'mtype': mtype, 'is_etf': True,
        'price': None, 'k60': None, 'k_day': None,
        'ma20': None, 'ma42': None,
        'ma20_rising': None, 'ma42_rising': None,
        'above_ma20': None, 'above_ma42': None,
        'etf_signal': None, 'etf_signal_label': '',
        'vol_surge': None, 'exit_info': None, 'error': None,
        'mom_grade': 'NA', 'mom_pct': None,
        'entry_type': None, 'entry_blocked': False, 'entry_block_reason': '',
        'channel': None,
    }

    df_day = fetch_day(tid, mtype)
    if df_day.empty:
        for sfx in ['.TW', '.TWO']:
            try:
                df_day = yf.Ticker(tid + sfx).history(
                    period='200d', interval='1d', auto_adjust=True, prepost=False)
                if df_day is not None and not df_day.empty and len(df_day) >= 10:
                    df_day = df_day[OHLCV].dropna(subset=['Open', 'High', 'Low', 'Close'])
                    df_day = _normalize_index(df_day)
                    break
            except Exception:
                continue
    if df_day is None or df_day.empty:
        result['error'] = '無日K資料'; return result

    close = df_day['Close']
    price = float(close.iloc[-1])
    result['price'] = price

    k_day_s, _ = calc_kd(df_day)
    if k_day_s is not None:
        result['k_day'] = round(float(k_day_s.iloc[-1]), 1)

    for n in [20, 42]:
        ma = calc_ma(close, n)
        if ma is not None and not pd.isna(ma.iloc[-1]):
            v = float(ma.iloc[-1])
            result[f'ma{n}']        = round(v, 2)
            result[f'above_ma{n}']  = price > v
            result[f'ma{n}_rising'] = ma_slope_rising(ma)

    df_60m, _ = fetch_60m(tid, mtype)
    if not df_60m.empty and len(df_60m) >= 6:
        k_s, _ = calc_kd(df_60m)
        if k_s is not None:
            result['k60'] = round(float(k_s.iloc[-1]), 1)
        result['channel']   = detect_channel(df_60m, price)
        result['vol_surge'] = detect_volume_surge_v43(
            df_60m, result['k60'] or 50, result['channel'])

    result['exit_info'] = calc_exit_signals(
        df_day, df_60m if not df_60m.empty else None, price, None, active_params)

    k_day = result['k_day']
    k60   = result['k60']

    if k_day is not None and k_day <= ETF_DAY_K_STRONG:
        result['etf_signal']       = 'strong_buy'
        result['etf_signal_label'] = f'🟢強烈買進 日K={k_day:.0f}≤{ETF_DAY_K_STRONG}'
        result['entry_type']       = 'ETF-S'
    elif k60 is not None and k60 <= ETF_60M_K_BUY:
        result['etf_signal']       = 'buy'
        result['etf_signal_label'] = f'🟡買進 60分K={k60:.0f}≤{ETF_60M_K_BUY}'
        result['entry_type']       = 'ETF-B'
    else:
        k_ref = k60 if k60 is not None else k_day
        result['etf_signal']       = 'watch'
        result['etf_signal_label'] = f'─ 觀望 K={k_ref:.0f}' if k_ref else '─ 觀望'
        result['entry_type']       = None

    return result


# =============================================================================
# 撿股讚資料抓取（v4.9：三頁整合）
# =============================================================================
_fundamental_cache  = {'data': None, 'html': None, 'last_update': 0}
_fin_wespai_cache   = {'data': None, 'last_update': 0}   # 財務品質快取（75684+75686）

# 三個撿股讚頁面 URL
WESPAI_URL          = 'https://stock.wespai.com/p/75683'   # 戴維斯雙擊：YOY/MoM
WESPAI_FIN_URLS     = [
    'https://stock.wespai.com/p/75684',   # 阿段投資心法：毛利率、營業利益率
    'https://stock.wespai.com/p/75686',   # 阿段+營收成：毛利率、營業利益率、營收成長
]
WESPAI_FIN_CACHE_MIN = 120               # 財務頁快取分鐘

WESPAI_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'),
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer':         'https://stock.wespai.com/',
    'Connection':      'keep-alive',
}


def _parse_wespai_html(html: str) -> pd.DataFrame:
    records   = []
    table_pat = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
    row_pat   = re.compile(r'<tr[^>]*>(.*?)</tr>',       re.DOTALL | re.IGNORECASE)
    cell_pat  = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)
    tag_pat   = re.compile(r'<[^>]+>')
    clean     = lambda s: tag_pat.sub('', s).strip()

    for tbl in table_pat.findall(html):
        rows = row_pat.findall(tbl)
        if len(rows) < 3:
            continue
        headers = [clean(c) for c in cell_pat.findall(rows[0])] or \
                  [clean(c) for c in cell_pat.findall(rows[1])]

        def find_col(kws):
            for kw in kws:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return None

        col_code = find_col(['代號', '股票代號', 'code'])
        col_name = find_col(['名稱', '公司', 'name'])
        col_ind  = find_col(['產業', '類股', '行業', 'industry'])
        if col_code is None:
            continue

        yoy_cols = [i for i, h in enumerate(headers)
                    if any(k in h for k in ['年增', 'YOY', 'yoy'])]
        col_yoy1 = yoy_cols[0] if len(yoy_cols) >= 1 else None
        col_yoy2 = yoy_cols[1] if len(yoy_cols) >= 2 else None
        col_mom  = find_col(['月增', 'MoM', 'mom', '月增率'])

        for row in rows[1:]:
            cells = [clean(c) for c in cell_pat.findall(row)]
            max_needed = max(filter(lambda x: x is not None,
                             [col_code, col_name or 0, col_ind or 0,
                              col_yoy1 or 0, col_yoy2 or 0, col_mom or 0]))
            if len(cells) <= max_needed:
                continue
            try:
                code = cells[col_code].strip()
                if not re.match(r'^\d{4,6}$', code):
                    continue
                name = cells[col_name].strip() if col_name is not None else ''
                ind  = cells[col_ind].strip()  if col_ind  is not None else ''

                def parse_pct(s):
                    try:
                        return float(s.replace('%', '').replace(',', '').strip())
                    except Exception:
                        return None

                yoy1 = parse_pct(cells[col_yoy1]) if col_yoy1 is not None else None
                yoy2 = parse_pct(cells[col_yoy2]) if col_yoy2 is not None else None
                mom  = parse_pct(cells[col_mom])  if col_mom  is not None else None

                records.append({
                    'code': code, 'name': name, 'industry': ind,
                    'yoy_m1': yoy1, 'yoy_m2': yoy2, 'mom': mom,
                })
            except Exception:
                continue
        if records:
            break
    return pd.DataFrame(records) if records else pd.DataFrame()


def fetch_wespai_fundamental(force=False) -> tuple:
    now, cache = time.time(), _fundamental_cache
    if not force and cache['data'] is not None and now - cache['last_update'] < YOY_CACHE_MIN * 60:
        return cache['data'], cache.get('html', '')

    print('  [撿股讚/75683] 📡 抓取 YOY/MoM 資料...')
    df, html = pd.DataFrame(), ''
    try:
        req = urllib.request.Request(WESPAI_URL, headers=WESPAI_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        import gzip
        try:
            html = gzip.decompress(raw).decode('utf-8', errors='ignore')
        except Exception:
            html = raw.decode('utf-8', errors='ignore')
        df = _parse_wespai_html(html)
        if df.empty:
            print('  [撿股讚] ⚠️  解析為空，嘗試備援 CSV...')
    except Exception as e:
        print(f'  [撿股讚] ⚠️  抓取失敗（{e}），嘗試備援 CSV...')
    if df.empty:
        df = _load_local_fundamental_csv()
    if df.empty:
        print('  [撿股讚] ❌ 無法取得股票清單')
    else:
        print(f'  [撿股讚] ✅ 取得 {len(df)} 檔')
    cache['data'], cache['html'], cache['last_update'] = df, html, now
    return df, html


# =============================================================================
# ★ v4.9 撿股讚財務品質頁（75684 + 75686）解析
# =============================================================================
def _fetch_url_html(url: str, timeout: int = 20) -> str:
    """抓取單一 URL，回傳解壓後 HTML 字串；失敗回 ''"""
    import gzip as _gz
    try:
        req = urllib.request.Request(url, headers=WESPAI_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        try:
            return _gz.decompress(raw).decode('utf-8', errors='ignore')
        except Exception:
            return raw.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f'  [財務頁] ⚠️  抓取失敗 {url}: {e}')
        return ''


def _parse_wespai_fin_html(html: str) -> pd.DataFrame:
    """
    解析撿股讚財務品質頁（p/75684、p/75686）。
    目標欄位：
      代號、名稱、產業
      (季)營業毛利率   ← 最新季 Q1
      (季-1)營業毛利率 ← 前一季 Q2（趨勢判斷用）
      (季)營業利益率
      (季-1)營業利益率
      (季)營業利益成長率 或 (季)營收成長率（備用）
    欄位名稱因頁面不同略有差異，用關鍵字模糊比對。
    """
    records   = []
    table_pat = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
    row_pat   = re.compile(r'<tr[^>]*>(.*?)</tr>',       re.DOTALL | re.IGNORECASE)
    cell_pat  = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)
    tag_pat   = re.compile(r'<[^>]+>')
    clean     = lambda s: tag_pat.sub('', s).strip()

    for tbl in table_pat.findall(html):
        rows = row_pat.findall(tbl)
        if len(rows) < 3:
            continue
        headers = [clean(c) for c in cell_pat.findall(rows[0])] or \
                  [clean(c) for c in cell_pat.findall(rows[1])]

        def find_col(kws):
            for kw in kws:
                for i, h in enumerate(headers):
                    if kw in h:
                        return i
            return None

        col_code = find_col(['代號', '股票代號', 'code'])
        col_name = find_col(['名稱', '公司', 'name'])
        col_ind  = find_col(['產業', '類股', '行業'])
        if col_code is None:
            continue

        # 毛利率：優先找「毛利率」，其次「毛利」
        gm_cols  = [i for i, h in enumerate(headers) if '毛利率' in h or ('毛利' in h and '率' in h)]
        # 備用：有些頁面寫「毛利」沒有「率」字
        if not gm_cols:
            gm_cols = [i for i, h in enumerate(headers) if '毛利' in h]
        col_gm1  = gm_cols[0] if len(gm_cols) >= 1 else None
        col_gm2  = gm_cols[1] if len(gm_cols) >= 2 else None

        # 營業利益率
        op_cols  = [i for i, h in enumerate(headers)
                    if ('營業利益率' in h) or ('營業利益' in h and '率' in h)
                    or ('營益率' in h)]
        col_op1  = op_cols[0] if len(op_cols) >= 1 else None
        col_op2  = op_cols[1] if len(op_cols) >= 2 else None

        # 營收成長率（p/75686 有）
        rev_gr_cols = [i for i, h in enumerate(headers)
                       if '營收成長' in h or '營業收入成長' in h or '收入成長' in h]
        col_rev_gr  = rev_gr_cols[0] if rev_gr_cols else None

        if col_gm1 is None and col_op1 is None:
            continue   # 這張表沒有財務欄位，跳過

        for row in rows[1:]:
            cells = [clean(c) for c in cell_pat.findall(row)]
            all_cols = [x for x in [col_code, col_name or 0, col_ind or 0,
                                     col_gm1 or 0, col_gm2 or 0,
                                     col_op1 or 0, col_op2 or 0,
                                     col_rev_gr or 0] if x is not None]
            if not all_cols or len(cells) <= max(all_cols):
                continue
            try:
                code = cells[col_code].strip()
                if not re.match(r'^\d{4,6}$', code):
                    continue

                def parse_pct(s):
                    try:
                        return float(str(s).replace('%', '').replace(',', '').strip())
                    except Exception:
                        return None

                name   = cells[col_name].strip() if col_name is not None else ''
                ind    = cells[col_ind].strip()  if col_ind  is not None else ''
                gm1    = parse_pct(cells[col_gm1])    if col_gm1    is not None else None
                gm2    = parse_pct(cells[col_gm2])    if col_gm2    is not None else None
                op1    = parse_pct(cells[col_op1])    if col_op1    is not None else None
                op2    = parse_pct(cells[col_op2])    if col_op2    is not None else None
                rev_gr = parse_pct(cells[col_rev_gr]) if col_rev_gr is not None else None

                records.append({
                    'code':        code,
                    'name':        name,
                    'industry':    ind,
                    'gm_q1':       gm1,    # 近季毛利率 %
                    'gm_q2':       gm2,    # 前季毛利率 %
                    'op_margin_q1': op1,   # 近季營業利益率 %
                    'op_margin_q2': op2,   # 前季營業利益率 %
                    'rev_growth':   rev_gr, # 營收成長率 %（可能為 None）
                })
            except Exception:
                continue
        if records:
            break
    return pd.DataFrame(records) if records else pd.DataFrame()


def fetch_wespai_fin_quality(force: bool = False) -> pd.DataFrame:
    """
    抓取 p/75684 + p/75686，合併成財務品質 DataFrame。
    欄位：code, name, industry, gm_q1, gm_q2, op_margin_q1, op_margin_q2, rev_growth
    快取 WESPAI_FIN_CACHE_MIN 分鐘。
    """
    now, cache = time.time(), _fin_wespai_cache
    if not force and cache['data'] is not None \
            and now - cache['last_update'] < WESPAI_FIN_CACHE_MIN * 60:
        return cache['data']

    combined = pd.DataFrame()
    for url in WESPAI_FIN_URLS:
        page_name = url.split('/')[-1]
        print(f'  [撿股讚/{page_name}] 📡 抓取財務品質資料...')
        html = _fetch_url_html(url)
        if not html:
            continue
        df_page = _parse_wespai_fin_html(html)
        if df_page.empty:
            print(f'  [撿股讚/{page_name}] ⚠️  解析為空')
            continue
        print(f'  [撿股讚/{page_name}] ✅ 解析到 {len(df_page)} 筆')
        if combined.empty:
            combined = df_page
        else:
            # 合併：以 code 為鍵，用後頁補齊前頁缺漏的欄位
            fin_cols = ['gm_q1', 'gm_q2', 'op_margin_q1', 'op_margin_q2', 'rev_growth']
            combined = combined.set_index('code')
            df_page2 = df_page.set_index('code')
            for col in fin_cols:
                if col in df_page2.columns:
                    if col not in combined.columns:
                        combined[col] = df_page2[col]
                    else:
                        # 以前頁為主，後頁補 NaN
                        mask = combined[col].isna() & df_page2[col].notna()
                        combined.loc[mask, col] = df_page2.loc[mask, col]
            combined = combined.reset_index()

    if combined.empty:
        print('  [撿股讚財務] ⚠️  兩個財務頁均無資料，財務篩選將依賴 yfinance 備援')

    cache['data'], cache['last_update'] = combined, now
    return combined


def get_wespai_fin_quality(code: str, df_fin: pd.DataFrame) -> dict:
    """
    從撿股讚財務 DataFrame 取出單一股票的財務品質評估結果。
    格式與 eval_fin_quality() 回傳的 dict 相容。
    """
    empty = {
        'available': False, 'source': 'none',
        'gross_margin_q1': None, 'gross_margin_q2': None, 'gross_margin_ok': None,
        'op_margin_q1': None, 'op_margin_ok': None, 'op_leverage_ok': None,
        'rev_growth': None, 'op_growth': None,
        'interest_coverage': None, 'interest_coverage_ok': True,
        'fin_pass': None, 'fin_fail_reasons': [],
    }
    if df_fin is None or df_fin.empty:
        return empty
    rows = df_fin[df_fin['code'] == code]
    if rows.empty:
        return empty

    row = rows.iloc[0]
    gm1  = _to_float(row.get('gm_q1'))
    gm2  = _to_float(row.get('gm_q2'))
    op1  = _to_float(row.get('op_margin_q1'))
    op2  = _to_float(row.get('op_margin_q2'))
    rev_gr = _to_float(row.get('rev_growth'))

    if gm1 is None and op1 is None:
        return empty   # 無有效財務資料

    result = dict(empty)
    result['available'] = True
    result['source']    = 'wespai'
    checks = []

    # ── 毛利率條件 ────────────────────────────────────────────────────────────
    if gm1 is not None:
        result['gross_margin_q1'] = round(gm1, 1)
        if gm2 is not None:
            result['gross_margin_q2'] = round(gm2, 1)
        threshold_ok = gm1 >= GROSS_MARGIN_MIN
        trend_ok     = (gm2 is None) or (gm1 >= gm2 - GROSS_MARGIN_TOLERANCE)
        gm_ok        = threshold_ok and trend_ok
        result['gross_margin_ok'] = gm_ok
        checks.append(gm_ok)
        if not threshold_ok:
            result['fin_fail_reasons'].append(f'毛利{gm1:.1f}%<{GROSS_MARGIN_MIN:.0f}%')
        elif not trend_ok:
            result['fin_fail_reasons'].append(f'毛利下滑({gm1:.1f}%←{gm2:.1f}%)')

    # ── 營業利益率條件 ────────────────────────────────────────────────────────
    if op1 is not None:
        result['op_margin_q1'] = round(op1, 1)
        om_ok = op1 >= OP_MARGIN_MIN
        result['op_margin_ok'] = om_ok
        checks.append(om_ok)
        if not om_ok:
            result['fin_fail_reasons'].append(f'營業利{op1:.1f}%<{OP_MARGIN_MIN:.0f}%')

        # 營業利益率趨勢（op1 >= op2 - 容差，表示維持或成長）
        if op2 is not None:
            result['op_margin_q2'] = round(op2, 1)
            lev_ok = op1 >= op2 - OP_LEVERAGE_TOLERANCE
            result['op_leverage_ok'] = lev_ok
            checks.append(lev_ok)
            if not lev_ok:
                result['fin_fail_reasons'].append(
                    f'營業利率下滑({op1:.1f}%←{op2:.1f}%)')

    # ── 利息保障：撿股讚頁無此欄位 → 自動通過（不降評）────────────────────
    result['interest_coverage']    = None
    result['interest_coverage_ok'] = True

    # ── 綜合 ─────────────────────────────────────────────────────────────────
    result['fin_pass'] = all(checks) if checks else None
    return result


def _load_local_fundamental_csv() -> pd.DataFrame:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fundamental_yoy.csv')
    if not os.path.exists(p):
        print(f'  [備援CSV] ℹ️  找不到 {p}')
        return pd.DataFrame()
    try:
        df = pd.read_csv(p, dtype={'code': str})
        df['code'] = df['code'].str.strip()
        for c in ['yoy_m1', 'yoy_m2', 'mom']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        if 'mom' not in df.columns:
            df['mom'] = None
        print(f'  [備援CSV] ✅ 載入 {len(df)} 筆')
        return df
    except Exception as e:
        print(f'  [備援CSV] ❌ {e}')
        return pd.DataFrame()


def build_stock_list(df_fund: pd.DataFrame) -> dict:
    if df_fund.empty:
        return {}
    return {str(r['code']).strip(): str(r['name']).strip()
            for _, r in df_fund.iterrows()
            if str(r.get('code', '')).strip() and str(r.get('name', '')).strip()}


def build_fundamental_filter(df_fund: pd.DataFrame) -> set:
    """第一道過濾：排除產業 + YOY≥門檻"""
    if df_fund.empty:
        return set()
    passed = set()
    for _, row in df_fund.iterrows():
        code = str(row.get('code', '')).strip()
        if not code:
            continue
        if any(kw in str(row.get('industry', '')) for kw in EXCLUDE_INDUSTRIES):
            continue
        y1, y2 = row.get('yoy_m1'), row.get('yoy_m2')
        y1_ok = y1 is not None and not pd.isna(y1) and y1 >= YOY_MIN_PCT
        y2_ok = y2 is not None and not pd.isna(y2) and y2 >= YOY_MIN_PCT
        if y1_ok and (y2_ok or pd.isna(y2) or y2 is None):
            passed.add(code)
    return passed


def build_yoy_stocks(df_fund: pd.DataFrame, fund_set: set) -> dict:
    return {c: n for c, n in build_stock_list(df_fund).items() if c in fund_set}


def get_industry(tid: str, fund_df: pd.DataFrame) -> str:
    if fund_df is None or fund_df.empty:
        return ''
    row = fund_df[fund_df['code'] == tid]
    if row.empty:
        return ''
    return str(row.iloc[0].get('industry', ''))


def get_mom_pct(tid: str, fund_df: pd.DataFrame, mom_dict: dict):
    if tid in mom_dict:
        v = mom_dict[tid]
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            return float(v)
    if fund_df is not None and not fund_df.empty:
        row = fund_df[fund_df['code'] == tid]
        if not row.empty:
            v = row.iloc[0].get('mom')
            if v is not None and not (isinstance(v, float) and pd.isna(v)):
                return float(v)
    return None


def priority_score(industry: str) -> int:
    if any(kw in industry for kw in PRIORITY_INDUSTRIES):
        return 0
    return 1


# =============================================================================
# 市場風向儀
# =============================================================================
_regime_cache = {'score': None, 'level': None, 'detail': {}, 'last_update': 0}
REGIME_CACHE_SEC = 900


def _fetch_index(symbol, years=30):
    try:
        df = yf.Ticker(symbol).history(
            period=f'{min(years, 30)}y', interval='1d', auto_adjust=True)
        if df is None or df.empty or len(df) < 100:
            return pd.DataFrame()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
    except Exception:
        return pd.DataFrame()


def _score_ma_trend(close):
    if len(close) < 500:
        return 15, 'MA資料不足，給中性分'
    ma200 = close.rolling(200).mean()
    ma500 = close.rolling(500).mean()
    last, m200, m500 = float(close.iloc[-1]), float(ma200.iloc[-1]), float(ma500.iloc[-1])
    slope  = (float(ma200.iloc[-1]) - float(ma200.iloc[-20])) / float(ma200.iloc[-20]) * 100
    pct200 = (last - m200) / m200 * 100
    pct500 = (last - m500) / m500 * 100
    if last > m200 and slope > 0.5:
        return min(30, 20 + int(pct200 * 0.5)), f'站上MA200({pct200:+.1f}%) MA200上升中 ✅'
    elif last > m200:
        return min(22, 13 + int(pct200 * 0.3)), f'站上MA200({pct200:+.1f}%) 但MA200走平 ⚠️'
    elif last > m500:
        return max(5, 10 + int(pct200 * 0.2)), f'跌破MA200({pct200:+.1f}%) 站上MA500({pct500:+.1f}%) 🔶'
    else:
        return max(0, 3 + int(pct500 * 0.1)), f'跌破MA200({pct200:+.1f}%) 跌破MA500({pct500:+.1f}%) 🔴'


def _score_drawdown(close):
    if len(close) < 50:
        return 12, '資料不足'
    peak = float(close.tail(min(len(close), 252 * 3)).max())
    dd   = (float(close.iloc[-1]) - peak) / peak * 100
    if   dd >= -5:  return 22, f'距高點{dd:.1f}% 🟢強勢'
    elif dd >= -15: return 17, f'距高點{dd:.1f}% 🟡正常回檔'
    elif dd >= -25: return 10, f'距高點{dd:.1f}% 🟠中度修正'
    elif dd >= -40: return 4,  f'距高點{dd:.1f}% 🔴深度修正'
    else:           return 1,  f'距高點{dd:.1f}% ⛔崩盤區'


def _score_momentum(close):
    if len(close) < 252:
        return 10, '資料不足'
    r12   = (float(close.iloc[-1]) / float(close.iloc[-252]) - 1) * 100
    r3    = (float(close.iloc[-1]) / float(close.iloc[-63])  - 1) * 100
    r1    = (float(close.iloc[-1]) / float(close.iloc[-21])  - 1) * 100
    combo = r12 * 0.5 + r3 * 0.35 + r1 * 0.15
    if   combo >= 20:  return 20, f'12M:{r12:+.1f}% 3M:{r3:+.1f}% 1M:{r1:+.1f}% 強勢多頭 🚀'
    elif combo >= 10:  return 17, f'12M:{r12:+.1f}% 3M:{r3:+.1f}% 1M:{r1:+.1f}% 多頭動能 ✅'
    elif combo >= 0:   return 12, f'12M:{r12:+.1f}% 3M:{r3:+.1f}% 1M:{r1:+.1f}% 動能中性 ➡️'
    elif combo >= -10: return 6,  f'12M:{r12:+.1f}% 3M:{r3:+.1f}% 1M:{r1:+.1f}% 動能偏弱 ⚠️'
    else:              return 1,  f'12M:{r12:+.1f}% 3M:{r3:+.1f}% 1M:{r1:+.1f}% 空頭動能 🔴'


def _score_volatility(close):
    if len(close) < 252:
        return 7, '資料不足'
    ret      = close.pct_change().dropna()
    vol20    = float(ret.tail(20).std()      * np.sqrt(252) * 100)
    vol_long = float(ret.tail(252 * 3).std() * np.sqrt(252) * 100) if len(ret) >= 252 * 3 else vol20
    ratio    = vol20 / (vol_long + 1e-6)
    if   ratio <= 0.6: return 15, f'近期波動{vol20:.1f}%(歷史{vol_long:.1f}%) 極度穩定 ✅'
    elif ratio <= 1.0: return 12, f'近期波動{vol20:.1f}%(歷史{vol_long:.1f}%) 正常水準 ✅'
    elif ratio <= 1.5: return 7,  f'近期波動{vol20:.1f}%(歷史{vol_long:.1f}%) 偏高不安 ⚠️'
    elif ratio <= 2.5: return 3,  f'近期波動{vol20:.1f}%(歷史{vol_long:.1f}%) 恐慌氛圍 🔴'
    else:              return 1,  f'近期波動{vol20:.1f}%(歷史{vol_long:.1f}%) 極度恐慌 ⛔'


def _score_us_market(sp500_close):
    if sp500_close is None or len(sp500_close) < 200:
        return 5, 'S&P500資料不足，給中性分'
    last  = float(sp500_close.iloc[-1])
    ma200 = float(sp500_close.rolling(200).mean().iloc[-1])
    pct   = (last - ma200) / ma200 * 100
    if   pct >= 5:  return 10, f'S&P500強勢站上MA200({pct:+.1f}%) 🟢'
    elif pct >= 0:  return 7,  f'S&P500站上MA200({pct:+.1f}%) ✅'
    elif pct >= -5: return 4,  f'S&P500略跌破MA200({pct:+.1f}%) ⚠️'
    else:           return 1,  f'S&P500跌破MA200({pct:+.1f}%) 🔴'


REGIMES = [
    (80, 5, '💚', '強勢多頭',  '最大倉位 80~100%', '全線多頭，第一/二梯隊積極布局，留意過熱訊號'),
    (63, 4, '🟢', '健康多頭',  '標準倉位 60~80%',  '趨勢向上，逢回分批進場，第一梯隊優先'),
    (45, 3, '🟡', '中性震盪',  '輕倉 30~50%',      '觀望為主，僅第一梯隊試單，嚴設停損'),
    (28, 2, '🟠', '空頭趨勢',  '防禦倉位 10~20%',  '以空手為主，僅極度超賣反彈可小試'),
    (0,  1, '🔴', '熊市/崩盤', '清空倉位 0~10%',   '嚴禁做多，空手觀望，保護資本優先'),
]


def _score_to_regime(score):
    for thr, level, icon, name, pos, strat in REGIMES:
        if score >= thr:
            return {'level': level, 'icon': icon, 'name': name,
                    'position': pos, 'strategy': strat}
    return {'level': 1, 'icon': '🔴', 'name': '熊市/崩盤',
            'position': '清空倉位 0~10%', 'strategy': '嚴禁做多，空手觀望'}


def calc_market_regime(force=False):
    now = time.time()
    if not force and _regime_cache['score'] is not None \
            and now - _regime_cache['last_update'] < REGIME_CACHE_SEC:
        return _regime_cache
    print('  [風向儀] 📡 下載大盤長期資料（首次需要約10秒）...')
    tw_df = _fetch_index('^TWII', 30)
    sp_df = _fetch_index('^GSPC', 30)
    if tw_df.empty:
        print('  [風向儀] ⚠️  無法取得台股大盤資料')
        return {}
    tw_close = tw_df['Close']
    sp_close = sp_df['Close'] if not sp_df.empty else None
    s_ma,   d_ma  = _score_ma_trend(tw_close)
    s_dd,   d_dd  = _score_drawdown(tw_close)
    s_mom2, d_mom = _score_momentum(tw_close)
    s_vol,  d_vol = _score_volatility(tw_close)
    s_us,   d_us  = _score_us_market(sp_close)
    total = s_ma + s_dd + s_mom2 + s_vol + s_us
    warn_2022, warn_msg = False, ''
    if sp_close is not None and len(sp_close) >= 252 and len(tw_close) >= 252:
        sp_r = (float(sp_close.iloc[-1]) / float(sp_close.iloc[-252]) - 1) * 100
        tw_r = (float(tw_close.iloc[-1]) / float(tw_close.iloc[-252]) - 1) * 100
        tw_d = (float(tw_close.iloc[-1]) / float(tw_close.tail(252 * 3).max()) - 1) * 100
        if sp_r < -10 and tw_r < -10 and tw_d < -15:
            warn_2022 = True
            warn_msg  = f'台股年跌{tw_r:.1f}% / 美股年跌{sp_r:.1f}% / 大盤回撤{tw_d:.1f}%'
    regime   = _score_to_regime(total)
    overheat = total >= REGIME_OVERHEAT_SCORE
    result = {
        'score': total, 'level': regime['level'],
        'icon': regime['icon'], 'name': regime['name'],
        'position': regime['position'], 'strategy': regime['strategy'],
        'warn_2022': warn_2022, 'warn_msg': warn_msg, 'overheat': overheat,
        'detail': {
            'MA趨勢(30)':   (s_ma,   d_ma),
            '回撤深度(25)': (s_dd,   d_dd),
            '動能強度(20)': (s_mom2, d_mom),
            '波動率(15)':   (s_vol,  d_vol),
            '美股方向(10)': (s_us,   d_us),
        },
        'tw_last':     float(tw_close.iloc[-1]),
        'last_update': now,
    }
    _regime_cache.update(result)
    return result


def print_regime_banner(regime):
    if not regime or regime.get('score') is None:
        return
    W     = 122
    score = regime['score']
    filled  = int(score / 100 * 40)
    bar_str = f'[{"█"*filled}{"░"*(40-filled)}] {score}/100'
    print(f'\n╔{"═"*W}╗')
    print(f'║  {"🌐 大盤風向儀（Market Regime Detector）":<{W-2}}║')
    print(f'╠{"═"*W}╣')
    print(f'║  {regime["icon"]} 當前市場狀態：{regime["name"]:<10} 健康分數：{bar_str}'
          + ' ' * max(0, W - 58 - len(regime["name"])) + '║')
    print(f'║  💼 建議倉位：{regime["position"]:<22} 📋 策略：{regime["strategy"]}'
          + ' ' * max(0, W - 22 - len(regime["position"]) - len(regime["strategy"]) - 14) + '║')
    if regime.get('overheat'):
        oh_msg = (f'  ☯【坤卦警示·龍戰于野】大盤分數{score}>={REGIME_OVERHEAT_SCORE}，'
                  f'強勢極盛暗藏轉折｜建議倉位降至60%以下，避免追高')
        print(f'╠{"═"*W}╣')
        print(f'║{oh_msg}' + ' ' * max(0, W - len(oh_msg)) + '║')
    print(f'╠{"─"*W}╣')
    print(f'║  {"指標項目":<14} {"得分":>6}  {"說明":<{W-26}}║')
    print(f'╠{"─"*W}╣')
    for label, (s, d) in regime['detail'].items():
        ms = int(label.split("(")[1].replace(")", "")) if "(" in label else 10
        bi = "█" * int(s / ms * 12) + "░" * (12 - int(s / ms * 12))
        print(f'║  {label:<14} {s:>4}/{ms:<3} [{bi}]  {d}'
              + ' ' * max(0, W - 26 - len(d)) + '║')
    if regime.get('warn_2022'):
        print(f'╠{"═"*W}╣')
        print(f'║  🚨🚨 【2022型態警告】{regime["warn_msg"]}'
              + ' ' * max(0, W - 12 - len(regime["warn_msg"])) + '║')
        print(f'║  ⛔  強烈建議：立即減倉至10%以下，停止做多！' + ' ' * (W - 23) + '║')
    print(f'╠{"═"*W}╣')
    level = regime['level']
    if level >= 4:   adj = '📈 進場調整：第一/二梯隊均可積極布局，三批進場策略正常執行'
    elif level == 3: adj = '⚖️  進場調整：僅執行第一梯隊（三線全上），第二梯隊降為試單'
    elif level == 2: adj = '🛡️  進場調整：第一梯隊只做第一批試單(1/3)，第二梯隊跳過'
    else:            adj = '🚫 進場調整：暫停所有做多，現有倉位評估停損，現金為王！'
    print(f'║  {adj}' + ' ' * max(0, W - 2 - len(adj)) + '║')
    print(f'╚{"═"*W}╝')


# =============================================================================
# 技術指標計算
# =============================================================================
OHLCV = ['Open', 'High', 'Low', 'Close', 'Volume']


def calc_ma(series, n):
    if len(series) < n:
        return None
    return series.rolling(n, min_periods=n).mean()


def calc_kd(df, n=9):
    if df is None or len(df) < n:
        return None, None
    lo  = df['Low'].rolling(n, min_periods=n).min()
    hi  = df['High'].rolling(n, min_periods=n).max()
    rsv = (df['Close'] - lo) / (hi - lo + 1e-9) * 100
    k_vals, d_vals, kp, dp = [], [], 50.0, 50.0
    for v in rsv:
        k = kp * 2 / 3 + (v if not pd.isna(v) else kp) / 3
        d = dp * 2 / 3 + k / 3
        k_vals.append(k); d_vals.append(d)
        kp, dp = k, d
    return pd.Series(k_vals, index=df.index), pd.Series(d_vals, index=df.index)


def calc_macd(close, fast=12, slow=26, sig=9):
    if len(close) < slow + sig:
        return None, None, None
    ema_f  = close.ewm(span=fast, adjust=False).mean()
    ema_s  = close.ewm(span=slow, adjust=False).mean()
    dif    = ema_f - ema_s
    signal = dif.ewm(span=sig, adjust=False).mean()
    return dif, signal, dif - signal


def ma_slope_rising(ma_series, bars=MA_SLOPE_BARS):
    if ma_series is None or len(ma_series) < bars + 1:
        return None
    recent = float(ma_series.iloc[-1])
    past   = float(ma_series.iloc[-1 - bars])
    if past == 0:
        return None
    return recent > past


def price_crossed_above(close, ma):
    if len(close) < 2 or ma is None or len(ma) < 2:
        return False
    pc, pm = float(close.iloc[-2]), float(ma.iloc[-2])
    cc, cm = float(close.iloc[-1]), float(ma.iloc[-1])
    if pd.isna(pm) or pd.isna(cm):
        return False
    return pc < pm and cc >= cm


def price_crossed_below(close, ma):
    if len(close) < 2 or ma is None or len(ma) < 2:
        return False
    pc, pm = float(close.iloc[-2]), float(ma.iloc[-2])
    cc, cm = float(close.iloc[-1]), float(ma.iloc[-1])
    if pd.isna(pm) or pd.isna(cm):
        return False
    return pc > pm and cc <= cm


# =============================================================================
# ★ v4.4 出場機制
# =============================================================================
def calc_exit_signals(df_day, df_60m, price, entry_price=None, active_params=None):
    if active_params is None:
        active_params = get_active_params()
    sl_pct = active_params['stop_loss_pct']
    result = {
        'exit_ma42': False, 'exit_death_cross': False, 'exit_kd_high': False,
        'stop_loss_price': None, 'take_profit_price': None, 'trailing_stop': None,
        'exit_reasons': [], 'exit_urgency': 'none',
    }
    close = df_day['Close']
    if entry_price and entry_price > 0:
        result['stop_loss_price']   = round(entry_price * (1 - sl_pct), 2)
        result['take_profit_price'] = round(entry_price * (1 + TAKE_PROFIT_TRIGGER), 2)
        result['trailing_stop']     = round(price * (1 - TRAILING_STOP_PCT), 2)
        if price <= result['stop_loss_price']:
            result['exit_reasons'].append(
                f'🛑 固定停損觸發！現價{price:.2f}≤停損{result["stop_loss_price"]:.2f}'
                f'（-{sl_pct*100:.0f}%）')
            result['exit_urgency'] = 'urgent'
    ma20 = calc_ma(close, 20)
    ma42 = calc_ma(close, 42)
    if ma20 is not None and ma42 is not None and len(ma20) >= 2 and len(ma42) >= 2:
        if EXIT_BELOW_MA42 and float(close.iloc[-1]) < float(ma42.iloc[-1]):
            result['exit_ma42'] = True
            result['exit_reasons'].append(
                f'⚠️  收盤({price:.2f})跌破42MA({float(ma42.iloc[-1]):.2f})，注意出場')
            if result['exit_urgency'] == 'none':
                result['exit_urgency'] = 'warn'
        prev_m20, prev_m42 = float(ma20.iloc[-2]), float(ma42.iloc[-2])
        curr_m20, curr_m42 = float(ma20.iloc[-1]), float(ma42.iloc[-1])
        if not pd.isna(prev_m20) and not pd.isna(prev_m42) \
                and prev_m20 >= prev_m42 and curr_m20 < curr_m42:
            result['exit_death_cross'] = True
            result['exit_reasons'].append(
                f'🔴 20MA死叉42MA！({curr_m20:.2f}<{curr_m42:.2f})  建議出場')
            result['exit_urgency'] = 'urgent'
    if df_60m is not None and len(df_60m) >= 9:
        k_s, d_s = calc_kd(df_60m)
        if k_s is not None and len(k_s) >= 2:
            k_curr, d_curr = float(k_s.iloc[-1]), float(d_s.iloc[-1])
            k_prev, d_prev = float(k_s.iloc[-2]), float(d_s.iloc[-2])
            if k_curr > KD_EXIT_K_HIGH and k_prev >= d_prev and k_curr < d_curr:
                result['exit_kd_high'] = True
                result['exit_reasons'].append(
                    f'🔴 60分K高位死叉！K={k_curr:.0f}>{KD_EXIT_K_HIGH}，KD死叉，建議出場')
                result['exit_urgency'] = 'urgent'
    return result


# =============================================================================
# ★ 倉位建議計算
# =============================================================================
def calc_position_advice(entry_type: str, regime: dict, active_params: dict,
                         vol_level: str = 'none', mom_grade: str = 'NA') -> str:
    if regime is None:
        return '─'
    level    = regime.get('level', 1)
    is_warn  = active_params.get('warning', False)
    overheat = regime.get('overheat', False)
    base = {'A': 30, 'B': 20, 'C': 15, 'D': 15}.get(entry_type, 10)
    if vol_level == 'best':
        base = min(base + 10, 40)
    if mom_grade == 'A':
        base = min(base + 5, 45)
    elif mom_grade == 'C':
        base = int(base * MOM_C_POSITION_RATIO)
    regime_coeff = {5: 1.0, 4: 0.8, 3: 0.5, 2: 0.3, 1: 0.0}.get(level, 0.5)
    pos = int(base * regime_coeff)
    if overheat: pos = int(pos * 0.7)
    if is_warn:  pos = int(pos * 0.6)
    pos = max(0, min(pos, 45))
    tags = []
    if is_warn:          tags.append('⚠️臨卦')
    if overheat:         tags.append('🔥過熱')
    if mom_grade == 'A': tags.append('📊+')
    if mom_grade == 'C': tags.append('📊⚠降倉')
    tag_s = ' ' + ' '.join(tags) if tags else ''
    return f'{pos}%{tag_s}'


# =============================================================================
# 量增偵測（v4.3）
# =============================================================================
def detect_volume_surge_v43(df_60m, k_val, channel=None):
    empty = {'surge': False, 'level': 'none',
             'ratio_ma5': None, 'curr_vol': None, 'vol_ma5': None,
             'consec_days': 0, 'desc': ''}
    if df_60m is None or len(df_60m) < 6:
        return empty
    vol       = df_60m['Volume'].values
    prev_vols = vol[-6:-1]
    vol_ma5   = float(np.mean(prev_vols)) if len(prev_vols) >= 3 else None
    curr_vol  = float(vol[-1])
    if vol_ma5 is None or vol_ma5 <= 0 or curr_vol <= 0:
        return empty
    ratio_ma5 = curr_vol / vol_ma5
    consec = 0
    for i in range(1, VOLUME_CONSEC_DAYS + 2):
        if i >= len(vol): break
        ref_mean = float(np.mean(vol[max(0, -(i+6)):-(i)])) if i+1 <= len(vol) else vol_ma5
        if ref_mean <= 0: break
        if float(vol[-i]) > ref_mean * VOLUME_CONSEC_RATIO:
            consec += 1
        else:
            break
    result = {
        'surge': False, 'level': 'none',
        'ratio_ma5': round(ratio_ma5, 2),
        'curr_vol': curr_vol, 'vol_ma5': vol_ma5,
        'consec_days': consec, 'desc': ''
    }
    if k_val > K_HIGH_NO_ENTRY:
        result['desc'] = f'K={k_val:.0f}>{K_HIGH_NO_ENTRY}高檔量增，疑似出貨陷阱，忽略'
        return result
    if ratio_ma5 < VOLUME_MA5_RATIO:
        if consec >= VOLUME_CONSEC_DAYS:
            result['level'] = 'consec'
            result['desc']  = (f'📶 連續{consec}日量增（>5日均量×{VOLUME_CONSEC_RATIO}）'
                               f'，法人代理訊號，注意追蹤')
        return result
    result['surge'] = True
    c_s     = f'{curr_vol/1000:.0f}張'
    m_s     = f'{vol_ma5/1000:.0f}張'
    ch_high = (channel is not None and channel.get('valid') and
               channel.get('pos_pct', 0) > CHANNEL_WARN_PCT and
               channel.get('confidence', 'low') in ('high', 'medium'))
    consec_tag = f' 🔁連續{consec}日' if consec >= VOLUME_CONSEC_DAYS else ''
    if k_val <= K_THRESHOLD and not ch_high:
        result['level'] = 'best'
        result['desc']  = (f'🚀 主力發動！{ratio_ma5:.1f}x均量（{c_s} vs 均{m_s}）'
                           f'K={k_val:.0f}超賣{consec_tag}，黃金進場！')
    elif k_val <= K_THRESHOLD and ch_high:
        result['level'] = 'watch'
        result['desc']  = f'⚡ 量增{ratio_ma5:.1f}x均量+K超賣，☯通道高位降觀察{consec_tag}'
    else:
        result['level'] = 'watch'
        result['desc']  = (f'⚡ {ratio_ma5:.1f}x均量（{c_s} vs 均{m_s}）'
                           f'K={k_val:.0f}{consec_tag}，主力可能發動留意')
    return result


# =============================================================================
# 通道位置
# =============================================================================
CHANNEL_BARS        = 200
CHANNEL_PIVOT_W     = 5
CHANNEL_MIN_PIVOTS  = 2
CHANNEL_TOUCH_PCT   = 2.5
CHANNEL_BOUNCE_PCT  = 2.0
CHANNEL_BOUNCE_BARS = 8
CHANNEL_MAX_WIDTH   = 40.0


def _channel_allows_entry(channel):
    if not channel or not channel.get('valid'):
        return True, ''
    pos  = channel.get('pos_pct', 50)
    conf = channel.get('confidence', 'low')
    if pos > CHANNEL_NO_ENTRY_PCT and conf in ('high', 'medium'):
        return True, f'☯通道{pos:.0f}%高位注意（>{CHANNEL_NO_ENTRY_PCT}%）降倉留意'
    return True, ''


def detect_channel(df_60m, price):
    empty = {'valid': False, 'direction': '', 'dir_label': '',
             'upper': None, 'lower': None, 'width_pct': None,
             'pos_pct': None, 'upper_dist': None, 'lower_dist': None,
             'upper_tests': 0, 'lower_tests': 0, 'confidence': 'low',
             'alert': '', 'alert_detail': ''}
    if len(df_60m) < 40:
        return empty
    lb = min(CHANNEL_BARS, len(df_60m))
    df = df_60m.tail(lb).copy().reset_index(drop=True)
    n, w = len(df), CHANNEL_PIVOT_W
    hi, lo, cl = df['High'].values, df['Low'].values, df['Close'].values
    pivot_hi, pivot_lo = [], []
    for i in range(w, n - w):
        if hi[i] == max(hi[i-w:i+w+1]): pivot_hi.append(i)
        if lo[i] == min(lo[i-w:i+w+1]): pivot_lo.append(i)
    if len(pivot_hi) < CHANNEL_MIN_PIVOTS or len(pivot_lo) < CHANNEL_MIN_PIVOTS:
        return empty

    def fit_line(idxs, vals):
        x = np.array(idxs, dtype=float)
        y = np.array([vals[i] for i in idxs], dtype=float)
        if len(x) < 2:
            return 0.0, float(np.mean(y)), 0.0
        coef = np.polyfit(x, y, 1)
        sl2, ic = float(coef[0]), float(coef[1])
        yh   = sl2 * x + ic
        ss_r = float(np.sum((y - yh) ** 2))
        ss_t = float(np.sum((y - np.mean(y)) ** 2))
        return sl2, ic, 1.0 - ss_r / ss_t if ss_t > 1e-9 else 1.0

    up_sl, up_ic, r2_up = fit_line(pivot_hi, hi)
    lo_sl, lo_ic, r2_lo = fit_line(pivot_lo, lo)
    cur_x          = float(n - 1)
    upper, lower   = up_sl * cur_x + up_ic, lo_sl * cur_x + lo_ic
    if upper <= lower: return empty
    width_pct = (upper - lower) / max(price, 1e-9) * 100
    if width_pct < 1.5 or width_pct > CHANNEL_MAX_WIDTH: return empty

    touch, bounce, bbars = CHANNEL_TOUCH_PCT / 100, CHANNEL_BOUNCE_PCT / 100, CHANNEL_BOUNCE_BARS
    uv = lv = 0
    for idx in pivot_hi:
        rail = up_sl * idx + up_ic
        if rail <= 0: continue
        if abs(hi[idx] - rail) / rail <= touch:
            ei = min(idx + bbars, n - 1)
            if ei > idx and (hi[idx] - float(np.min(cl[idx+1:ei+1]))) / hi[idx] >= bounce:
                uv += 1
    for idx in pivot_lo:
        rail = lo_sl * idx + lo_ic
        if rail <= 0: continue
        if abs(lo[idx] - rail) / rail <= touch:
            ei = min(idx + bbars, n - 1)
            if ei > idx and (float(np.max(cl[idx+1:ei+1])) - lo[idx]) / lo[idx] >= bounce:
                lv += 1

    if   uv >= 1 and lv >= 1:  confidence, conf_label = 'high',   f'⭐⭐ 雙側驗證（上{uv}次↓下{lv}次↑）'
    elif uv >= 2 or  lv >= 2:  confidence, conf_label = 'high',   f'⭐⭐ 強側驗證（上{uv}次↓下{lv}次↑）'
    elif uv >= 1 or  lv >= 1:  confidence, conf_label = 'medium', f'⭐ 單側驗證（上{uv}次↓下{lv}次↑）'
    elif r2_up >= 0.5 or r2_lo >= 0.5:
                               confidence, conf_label = 'low',    f'△ 未驗證（R²={max(r2_up,r2_lo):.2f}）'
    else:
        return empty

    pos_pct    = (price - lower) / (upper - lower) * 100
    upper_dist = (upper - price) / price * 100
    lower_dist = (price - lower) / price * 100
    if pos_pct > 115 or pos_pct < -15: return empty

    sp_bar = ((up_sl + lo_sl) / 2) / max(price, 1) * 100
    if   sp_bar > 0.05:  direction, dir_label = 'up',   '⬆️上升通道'
    elif sp_bar < -0.05: direction, dir_label = 'down', '⬇️下降通道'
    else:                direction, dir_label = 'flat', '↔️橫盤通道'

    ci = '⭐⭐' if confidence == 'high' else ('⭐' if confidence == 'medium' else '△')
    alert, alert_detail = '', ''
    if pos_pct >= 80:
        alert        = '🔴賣出區間'
        alert_detail = f'{dir_label} {ci} | 位置:{pos_pct:.0f}%  上軌:{upper:.1f}({upper_dist:+.1f}%)  {conf_label}'
    elif pos_pct <= 20:
        alert        = '🟢買入區間'
        alert_detail = f'{dir_label} {ci} | 位置:{pos_pct:.0f}%  下軌:{lower:.1f}({lower_dist:+.1f}%)  {conf_label}'
    elif pos_pct >= 65:
        alert        = '🟡接近上軌'
        alert_detail = f'{dir_label} {ci} | 位置:{pos_pct:.0f}%  上軌:{upper:.1f}({upper_dist:+.1f}%)  {conf_label}'
    elif pos_pct <= 35:
        alert        = '🔵接近下軌'
        alert_detail = f'{dir_label} {ci} | 位置:{pos_pct:.0f}%  下軌:{lower:.1f}({lower_dist:+.1f}%)  {conf_label}'
    elif confidence == 'high':
        alert_detail = f'{dir_label} {ci} | 位置:{pos_pct:.0f}%  上軌:{upper:.1f} 下軌:{lower:.1f}  {conf_label}'

    return {
        'valid': True, 'direction': direction, 'dir_label': dir_label,
        'upper': round(upper, 1), 'lower': round(lower, 1),
        'width_pct': round(width_pct, 1), 'pos_pct': round(pos_pct, 1),
        'upper_dist': round(upper_dist, 1), 'lower_dist': round(lower_dist, 1),
        'upper_tests': uv, 'lower_tests': lv,
        'confidence': confidence, 'conf_label': conf_label,
        'alert': alert, 'alert_detail': alert_detail,
    }


# =============================================================================
# 行情資料抓取
# =============================================================================
def _cols_ok(df, cols):
    return df is not None and not df.empty and set(cols).issubset(df.columns)


def _normalize_index(df):
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert('Asia/Taipei').tz_localize(None)
    return df


def _filter_tw_market_hours(df):
    if df.empty: return df
    h, m = df.index.hour, df.index.minute
    return df[((h > 9) | ((h == 9) & (m >= 0))) &
              ((h < 13) | ((h == 13) & (m <= 30)))]


def _agg_tw_60m(df_sub):
    df = _filter_tw_market_hours(df_sub.copy())
    if df.empty: return pd.DataFrame()
    gk = df.index.normalize() + pd.to_timedelta(df.index.hour, unit='h')
    return df.groupby(gk).agg(
        Open=('Open', 'first'), High=('High', 'max'),
        Low=('Low', 'min'),     Close=('Close', 'last'),
        Volume=('Volume', 'sum')).sort_index()


def fetch_60m(tid, mtype, verbose=False):
    _tp  = pytz.timezone('Asia/Taipei')
    _now = datetime.now(_tp)
    _s   = (_now - timedelta(days=90)).strftime('%Y-%m-%d')
    _e   = (_now + timedelta(days=1)).strftime('%Y-%m-%d')
    base_ids = [tid] + ([tid.zfill(4)] if len(tid) < 4 else [])
    order    = ['.TWO', '.TW'] if mtype == 'TWO' else ['.TW', '.TWO']
    seen, suffixes = set(), []
    for b in base_ids:
        for s in order:
            sym = b + s
            if sym not in seen:
                seen.add(sym); suffixes.append(sym)

    def _get(sym, interval, start=None, end=None, period=None):
        try:
            kw = dict(interval=interval, auto_adjust=True, prepost=False)
            if period: kw['period'] = period
            else:      kw['start'] = start; kw['end'] = end
            df = yf.Ticker(sym).history(**kw)
            if df is None or df.empty: return pd.DataFrame()
            if not _cols_ok(df, ['Open', 'High', 'Low', 'Close']): return pd.DataFrame()
            if 'Volume' not in df.columns: df['Volume'] = 0
            df = df[OHLCV].dropna(subset=['Open', 'High', 'Low', 'Close'])
            df['Volume'] = df['Volume'].fillna(0)
            return _filter_tw_market_hours(_normalize_index(df))
        except Exception:
            return pd.DataFrame()

    for sym in suffixes:
        df = _get(sym, '60m', start=_s, end=_e)
        if len(df) >= 50: return df, '60m'
    fallback = {}
    for sym in suffixes:
        df = _get(sym, '60m', period='60d')
        if len(df) >= 9: fallback[sym] = df
    for iv, pd_ in [('30m', '60d'), ('15m', '60d'), ('5m', '60d')]:
        for sym in suffixes:
            ds = _get(sym, iv, period=pd_)
            if len(ds) < 4: continue
            d60 = _agg_tw_60m(ds)
            if len(d60) >= 50: return d60, '60m'
            if len(d60) >= 9 and sym not in fallback:
                fallback[sym] = d60
    if fallback:
        best = max(fallback, key=lambda s: len(fallback[s]))
        return fallback[best], '60m'
    for iv, pd_, lb in [('30m', '60d', '30m'), ('15m', '60d', '15m'),
                         ('30m', '5d', '30m'),  ('15m', '5d', '15m')]:
        for sym in suffixes:
            df = _get(sym, iv, period=pd_)
            if len(df) >= 5: return df, lb
    return pd.DataFrame(), None


def _fetch_finmind_day(tid):
    if not FINMIND_TOKEN: return pd.DataFrame()
    start = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d')
    url   = (f'https://api.finmindtrade.com/api/v4/data'
             f'?dataset=TaiwanStockPrice&data_id={tid}'
             f'&start_date={start}&token={FINMIND_TOKEN}')
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
                timeout=12) as r:
            raw = json.loads(r.read())
        if raw.get('status') != 200 or not raw.get('data'): return pd.DataFrame()
        df = pd.DataFrame(raw['data']).rename(columns={
            'date': 'Date', 'open': 'Open', 'max': 'High',
            'min': 'Low', 'close': 'Close', 'Trading_Volume': 'Volume'})
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index('Date').sort_index()
        for c in OHLCV:
            if c not in df.columns: df[c] = 0
        df = df[OHLCV].apply(pd.to_numeric, errors='coerce').dropna(
            subset=['Open', 'High', 'Low', 'Close'])
        df['Volume'] = df['Volume'].fillna(0)
        return df
    except Exception:
        return pd.DataFrame()


def fetch_day(tid, mtype):
    base_ids = [tid] + ([tid.zfill(4)] if len(tid) < 4 else [])
    order    = ['.TWO', '.TW'] if mtype == 'TWO' else ['.TW', '.TWO']
    seen, suf = set(), []
    for b in base_ids:
        for s in order:
            sym = b + s
            if sym not in seen: seen.add(sym); suf.append(sym)
    for sym in suf:
        try:
            df = yf.Ticker(sym).history(period='200d', interval='1d',
                                         auto_adjust=True, prepost=False)
            if df is None or df.empty: continue
            if 'Volume' not in df.columns: df['Volume'] = 0
            df = df[OHLCV].dropna(subset=['Open', 'High', 'Low', 'Close'])
            df = _normalize_index(df)
            if len(df) >= 30: return df
        except Exception:
            continue
    for fm in ([tid] + ([tid.zfill(4)] if len(tid) < 4 else [])):
        d = _fetch_finmind_day(fm)
        if not d.empty and len(d) >= 30: return d
    return pd.DataFrame()


# =============================================================================
# 單一股票分析（v4.7）
# =============================================================================
def _guess_mtype(t):
    TW_WHITELIST = {'7720', '7522', '7536', '7715', '7716', '7702'}
    if t in TW_WHITELIST or len(t) != 4: return 'TW'
    p = t[0]
    if p in ('7', '8'): return 'TWO'
    if p == '4' and int(t) >= 4000: return 'TWO'
    if p == '6' and int(t) >= 6200: return 'TWO'
    if p == '5' and int(t) >= 5000: return 'TWO'
    if p == '3' and int(t) >= 3200: return 'TWO'
    return 'TW'


def analyze_stock(tid, name, mtype,
                  entry_price=None, active_params=None,
                  mom_pct=None, fin_quality=None):
    if active_params is None:
        active_params = get_active_params()
    k_thr     = active_params['k_threshold']
    mom_grade = classify_mom(mom_pct)

    result = {
        "tid": tid, "name": name, "mtype": mtype,
        "price": None,
        "k60": None, "k_label": None,
        "ma20": None, "ma42": None, "ma60": None,
        "above_ma20": None, "above_ma42": None, "above_ma60": None,
        "ma20_rising": None, "ma42_rising": None, "ma60_rising": None,
        "cross_above_20": False, "cross_below_20": False,
        "dif": None, "macd_signal": None, "dif_above_signal": None,
        "entry_type": None,
        "entry_blocked": False, "entry_block_reason": "",
        "exit_signal": False, "exit_info": None,
        "mom_pct": mom_pct, "mom_grade": mom_grade,
        "fin_quality": fin_quality or {},
        "error": None, "channel": None, "vol_surge": None,
    }

    df_day = fetch_day(tid, mtype)
    if df_day.empty:
        result["error"] = "無日K資料"; return result
    close = df_day["Close"]
    price = float(close.iloc[-1])
    result["price"] = price

    ma_series = {}
    for n in [20, 42, 60]:
        ma = calc_ma(close, n)
        if ma is not None and not pd.isna(ma.iloc[-1]):
            v = float(ma.iloc[-1])
            result[f"ma{n}"]       = round(v, 2)
            result[f"above_ma{n}"] = price > v
            ma_series[n]           = ma
    for n in [20, 42, 60]:
        if n in ma_series:
            result[f"ma{n}_rising"] = ma_slope_rising(ma_series[n])

    if 20 in ma_series:
        result["cross_above_20"] = price_crossed_above(close, ma_series[20])
        result["cross_below_20"] = price_crossed_below(close, ma_series[20])
    if 42 in ma_series:
        result["cross_above_42"] = price_crossed_above(close, ma_series[42])

    dif_s, sig_s, _ = calc_macd(close)
    if dif_s is not None:
        dif_v = float(dif_s.iloc[-1])
        sig_v = float(sig_s.iloc[-1])
        result["dif"]              = round(dif_v, 4)
        result["macd_signal"]      = round(sig_v, 4)
        result["dif_above_signal"] = dif_v > sig_v

    df_60m, k_label = fetch_60m(tid, mtype)
    if df_60m.empty or len(df_60m) < 6:
        result["error"] = "無60分K"; return result
    k_series, _ = calc_kd(df_60m)
    if k_series is None:
        result["error"] = "KD計算失敗"; return result
    k_val = float(k_series.iloc[-1])
    result["k60"]     = round(k_val, 1)
    result["k_label"] = k_label

    channel = detect_channel(df_60m, price)
    result["channel"]   = channel
    result["vol_surge"] = detect_volume_surge_v43(df_60m, k_val, channel)

    exit_info = calc_exit_signals(df_day, df_60m, price, entry_price, active_params)
    result["exit_info"] = exit_info

    # ── 進場條件 ──────────────────────────────────────────────────────────────
    k_low  = k_val <= k_thr
    m20r   = result["ma20_rising"]
    m42r   = result["ma42_rising"]
    m60r   = result["ma60_rising"]
    a42    = result["above_ma42"] is True
    a60    = result["above_ma60"] is True
    c20    = result["cross_above_20"]
    dif_ok = result["dif_above_signal"] is True
    b20    = result["cross_below_20"]

    if m42r is True and m20r is True and b20:
        result["exit_signal"] = True

    ch_ok, ch_block_reason = _channel_allows_entry(channel)
    if not ch_ok:
        result["entry_blocked"]      = True
        result["entry_block_reason"] = ch_block_reason
        return result

    mom_ok, mom_block_reason = mom_allows_entry(mom_grade)
    if not mom_ok:
        result["entry_blocked"]      = True
        result["entry_block_reason"] = mom_block_reason
        result["mom_blocked"]        = True
        return result

    # ── ★ v4.7 財務品質阻擋 ──────────────────────────────────────────────────
    if FIN_BLOCK_ON_FAIL and fin_quality and fin_quality.get('available'):
        if fin_quality.get('fin_pass') is False:
            reasons = '、'.join(fin_quality.get('fin_fail_reasons', ['財務不達標']))
            result["entry_blocked"]      = True
            result["entry_block_reason"] = f'📋財務品質不達標：{reasons}'
            result["fin_blocked"]        = True
            return result

    a60_ok = (not A_CLASS_REQUIRE_60MA) or (a60 and m60r is True)
    if k_low and m42r is True and m20r is True and a42 and dif_ok and a60_ok:
        result["entry_type"] = "A"
    elif m42r is True and m20r is not True and c20:
        result["entry_type"] = "B"
    elif k_low and a42 and not result["above_ma20"]:
        result["entry_type"] = "C"
    elif k_low and a42 and dif_ok:
        result["entry_type"] = "D"

    return result


# =============================================================================
# 分類與排序
# =============================================================================
def _classify(r):
    if r.get('error'):           return 'error'
    if r.get('is_etf'):
        sig = r.get('etf_signal')
        if sig == 'strong_buy':  return 'etf_strong'
        if sig == 'buy':         return 'etf_buy'
        return 'etf_watch'
    if r.get('entry_blocked'):
        if r.get('fin_blocked'): return 'fin_watch'
        if r.get('mom_blocked'): return 'mom_watch'
        return 'blocked'
    if r.get('exit_signal'):     return 'exit'
    et = r.get('entry_type')
    if et in ('A', 'B', 'C', 'D'): return et
    return 'skip'


def _sort_key(r, fund_df):
    ind       = get_industry(r['tid'], fund_df)
    prio      = priority_score(ind)
    k         = r.get('k60') or 99
    vs        = r.get('vol_surge') or {}
    has_vol   = 0 if (vs.get('level') in ('best', 'consec') or
                      vs.get('consec_days', 0) >= VOLUME_CONSEC_DAYS) else 1
    mom_order = {'A': 0, 'B': 1, 'NA': 2, 'C': 3}.get(r.get('mom_grade', 'NA'), 2)
    return (prio, mom_order, has_vol, k)


# =============================================================================
# 輸出（v4.7：加入財務品質標籤）
# =============================================================================
def print_scan_result(results, scan_count, tw_now, regime=None,
                      fund_set=None, fund_df=None, active_params=None,
                      etf_results=None, fin_quality_dict=None):
    if active_params  is None: active_params  = get_active_params(tw_now)
    if fin_quality_dict is None: fin_quality_dict = {}

    W   = 148
    SEP = '═' * W
    sep = '─' * W

    def _yoy_info(tid):
        if fund_df is None or fund_df.empty: return None, None, ''
        row = fund_df[fund_df['code'] == tid]
        if row.empty: return None, None, ''
        r = row.iloc[0]
        return r.get('yoy_m1'), r.get('yoy_m2'), str(r.get('industry', ''))

    def _yoy_tag(tid):
        y1, y2, _ = _yoy_info(tid)
        if y1 is None: return ''
        y1_s = f'{y1:+.1f}%'
        y2_s = f'{y2:+.1f}%' if y2 is not None and not pd.isna(y2) else 'N/A'
        icon = '📈' if (fund_set and tid in fund_set) else '📉'
        return f' | {icon}YOY:{y1_s}/{y2_s}'

    def _ind_tag(tid):
        _, _, ind = _yoy_info(tid)
        if not ind: return ''
        p = '🏆' if priority_score(ind) == 0 else '  '
        return f' [{p}{ind[:8]}]'

    def _vol_tag(r):
        vs  = r.get('vol_surge') or {}
        lvl = vs.get('level', 'none')
        if lvl == 'best':
            ratio  = vs.get('ratio_ma5', 0)
            consec = vs.get('consec_days', 0)
            ct     = f'🔁{consec}日' if consec >= VOLUME_CONSEC_DAYS else ''
            return f' | 🚀{ratio:.1f}x均量{ct}'
        elif lvl == 'watch':
            return f' | ⚡{vs.get("ratio_ma5", 0):.1f}x均量'
        elif lvl == 'consec':
            return f' | 📶連續{vs.get("consec_days", 0)}日量增'
        return ''

    def _mom_tag(r):
        grade = r.get('mom_grade', 'NA')
        pct   = r.get('mom_pct')
        return ' | ' + mom_label(pct, grade)

    def _fin_tag(r):
        fq = r.get('fin_quality') or {}
        if not fq.get('available'):
            return ' | 📋─'
        gm  = fq.get('gross_margin_q1')
        om  = fq.get('op_margin_q1')
        ic  = fq.get('interest_coverage')
        fp  = fq.get('fin_pass')

        if ic is None:
            ic_s = '∞'
        elif ic == float('inf'):
            ic_s = '∞'
        else:
            ic_s = f'{ic:.0f}x'

        if fp is True:
            gm_s = f'{gm:.0f}%' if gm is not None else '─'
            om_s = f'{om:.0f}%' if om is not None else '─'
            return f' | 📋✅毛利{gm_s} 營益{om_s} 保障{ic_s}'
        elif fp is False:
            reasons = '|'.join(fq.get('fin_fail_reasons', ['不達標']))
            return f' | 📋❌{reasons}'
        else:
            gm_s = f'{gm:.0f}%' if gm is not None else '─'
            om_s = f'{om:.0f}%' if om is not None else '─'
            return f' | 📋?毛利{gm_s} 營益{om_s} 保障{ic_s}'

    def channel_note(r):
        ch = r.get('channel')
        if not ch or not ch.get('valid'): return ''
        alert = ch.get('alert', '')
        if not alert: return ''
        ci = '⭐⭐' if ch.get('confidence') == 'high' else ('⭐' if ch.get('confidence') == 'medium' else '△')
        return f' | 通道{ci}:{alert}({ch["pos_pct"]:.0f}%)'

    fmt = lambda v, d=2: f'{v:.{d}f}' if v is not None else 'N/A'

    def print_etf_row(r, icon):
        p     = r.get('price', 0)
        k60   = r.get('k60');  k_day = r.get('k_day')
        m20   = fmt(r.get('ma20')); m42 = fmt(r.get('ma42'))
        m20r  = '↑' if r.get('ma20_rising') else ('↓' if r.get('ma20_rising') is False else '─')
        m42r  = '↑' if r.get('ma42_rising') else ('↓' if r.get('ma42_rising') is False else '─')
        a20_s = '✅>20MA' if r.get('above_ma20') else '❌<20MA'
        a42_s = '✅>42MA' if r.get('above_ma42') else '❌<42MA'
        sig   = r.get('etf_signal_label', '')
        ch_n  = channel_note(r); vt = _vol_tag(r)
        ei    = r.get('exit_info') or {}
        warn  = ''
        if ei.get('exit_ma42'):        warn = '  ⚠️跌破42MA'
        if ei.get('exit_death_cross'): warn = '  🔴20MA死叉42MA'
        k60_s = f'60分K={k60:.0f}' if k60 is not None else ''
        kd_s  = f'日K={k_day:.0f}' if k_day is not None else ''
        print(f'║  {icon} {r["tid"]:<8}{r["name"]:<12}  現價:{p:>8.2f}  '
              f'{kd_s}  {k60_s}  20MA:{m20}({m20r})  42MA:{m42}({m42r})'
              f'  {a20_s} {a42_s}  {sig}{warn}{ch_n}{vt}')

    def print_entry_row(r, label):
        p    = r.get('price', 0)
        k    = r.get('k60')
        k_s  = f'{k:.1f}' if k is not None else 'N/A'
        m20  = fmt(r.get('ma20')); m42 = fmt(r.get('ma42')); m60 = fmt(r.get('ma60'))
        m20r = '↑' if r.get('ma20_rising') else ('↓' if r.get('ma20_rising') is False else '─')
        m42r = '↑' if r.get('ma42_rising') else ('↓' if r.get('ma42_rising') is False else '─')
        m60r = '↑' if r.get('ma60_rising') else ('↓' if r.get('ma60_rising') is False else '─')
        a60  = '✅60' if r.get('above_ma60') else ''
        dif  = 'DIF✅' if r.get('dif_above_signal') else ('DIF❌' if r.get('dif_above_signal') is False else '')
        c_n  = '↑突破' if r.get('cross_above_20') else ''
        ch_n = channel_note(r); vt = _vol_tag(r); mt = _mom_tag(r)
        ft   = _fin_tag(r); it = _ind_tag(r['tid']); yn = _yoy_tag(r['tid'])
        et   = r.get('entry_type', '─')
        vs   = (r.get('vol_surge') or {}).get('level', 'none')
        mg   = r.get('mom_grade', 'NA')
        pos  = calc_position_advice(et, regime, active_params, vs, mg)
        sl   = (r.get('exit_info') or {}).get('stop_loss_price')
        sl_s = f' 停損:{sl:.2f}' if sl else ''
        print(f'║  {label} {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
              f'K={k_s}  20MA:{m20}({m20r})  42MA:{m42}({m42r})  60MA:{m60}({m60r})'
              f'  {a60} {dif}{c_n}  建議倉:{pos}{sl_s}{ch_n}{vt}{mt}{ft}{it}{yn}')

    def print_exit_row(r):
        p  = r.get('price', 0); k = r.get('k60')
        ei = r.get('exit_info') or {}
        sl = ei.get('stop_loss_price')
        reasons = '  '.join(ei.get('exit_reasons', []))[:60]
        mt = _mom_tag(r); ft = _fin_tag(r)
        print(f'║  🔴 {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
              f'K={f"{k:.1f}" if k else "N/A"}  {reasons}{mt}{ft}')
        if sl:
            print(f'║       停損:{sl:.2f}  移動停利:{ei.get("trailing_stop","─")}')

    def print_ch_sell_row(r):
        ch   = r['channel']; pos = ch['pos_pct']
        ci   = '⭐⭐' if ch.get('confidence') == 'high' else ('⭐' if ch.get('confidence') == 'medium' else '△')
        p    = r.get('price', 0); k = r.get('k60')
        icon = '🔴' if pos >= 80 else '🟡'
        tag  = '賣出區間' if pos >= 80 else '接近上軌'
        detail = (f'{ch["dir_label"]}  位置:{pos:.0f}%  '
                  f'上軌:{ch["upper"]:.1f}({ch["upper_dist"]:+.1f}%)  '
                  f'下軌:{ch["lower"]:.1f}  {ci}(上{ch["upper_tests"]}↓下{ch["lower_tests"]}↑)')
        mt = _mom_tag(r); ft = _fin_tag(r)
        print(f'║  {icon} {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
              f'K={f"{k:.0f}" if k else "?"}  {tag}  {detail}{mt}{ft}')

    # ── ETF / 個股分組 ────────────────────────────────────────────────────────
    etf_list   = etf_results or []
    etf_strong = sorted([r for r in etf_list if r.get('etf_signal') == 'strong_buy'],
                        key=lambda r: r.get('k_day') or 99)
    etf_buy    = sorted([r for r in etf_list if r.get('etf_signal') == 'buy'],
                        key=lambda r: r.get('k60') or 99)
    etf_watch  = [r for r in etf_list if r.get('etf_signal') == 'watch']

    cls_map    = {r['tid']: _classify(r) for r in results}
    grp_A      = [r for r in results if cls_map[r['tid']] == 'A']
    grp_B      = [r for r in results if cls_map[r['tid']] == 'B']
    grp_C      = [r for r in results if cls_map[r['tid']] == 'C']
    grp_D      = [r for r in results if cls_map[r['tid']] == 'D']
    grp_exit   = [r for r in results if cls_map[r['tid']] == 'exit']
    grp_mom_w  = [r for r in results if cls_map[r['tid']] == 'mom_watch']
    grp_fin_w  = [r for r in results if cls_map[r['tid']] == 'fin_watch']
    errors     = [r for r in results if cls_map[r['tid']] == 'error']

    urgent_exit = [r for r in results
                   if (r.get('exit_info') or {}).get('exit_urgency') == 'urgent'
                   and not r.get('error')]

    valid_ch  = [r for r in results
                 if r.get('channel') and r['channel'].get('valid') and not r.get('error')]
    ch_sell   = [r for r in valid_ch
                 if r['channel']['alert'] in ('🔴賣出區間', '🟡接近上軌')
                 and (r.get('exit_info') or {}).get('exit_kd_high', False)]

    vol_best   = [r for r in results if (r.get('vol_surge') or {}).get('level') == 'best'  and not r.get('error')]
    vol_watch  = [r for r in results if (r.get('vol_surge') or {}).get('level') == 'watch' and not r.get('error')]
    vol_consec = [r for r in results if (r.get('vol_surge') or {}).get('level') == 'consec' and not r.get('error')]

    # ── 表頭 ─────────────────────────────────────────────────────────────────
    print(f'╔{SEP}╗')
    title = (f'  🔍 台股掃描 第{scan_count}次 | {tw_now.strftime("%Y-%m-%d %H:%M:%S")}'
             f' | ☯ v4.9 主動ETF+YOY+財務品質版')
    print(f'║{title}' + ' ' * max(0, W - len(title)) + '║')

    if active_params.get('warning'):
        warn_msg = active_params['warning_msg']
        print(f'╠{"═"*W}╣')
        print(f'║  {warn_msg}' + ' ' * max(0, W - 2 - len(warn_msg)) + '║')

    if regime:
        rl = (f'  大盤：{regime.get("name","")}{regime.get("icon","")}  '
              f'分數:{regime.get("score","")}/100  建議倉位:{regime.get("position","")}')
        if regime.get('overheat'):
            rl += '  ☯龍戰于野·過熱警示'
        print(f'║{rl}' + ' ' * max(0, W - len(rl)) + '║')

    k_thr  = active_params['k_threshold']
    sl_pct = active_params['stop_loss_pct']
    fin_mode = f'財務篩選:{"擋掉" if FIN_BLOCK_ON_FAIL else "標記"}｜毛利≥{GROSS_MARGIN_MIN:.0f}%|營益≥{OP_MARGIN_MIN:.0f}%|保障>{INTEREST_COVER_MIN:.0f}x'
    fs = (f'  📊 個股{len(fund_set) if fund_set else 0}檔(YOY≥{YOY_MIN_PCT:.0f}%)  '
          f'主動ETF:{len(etf_list)}檔  K門檻≤{k_thr}  停損{sl_pct*100:.0f}%  '
          f'ETF:日K≤{ETF_DAY_K_STRONG}🟢/60分K≤{ETF_60M_K_BUY}🟡  '
          f'MoM：✅≥0% ~<-3% ⚠<-7% 🔴觀察  {fin_mode}')
    print(f'║{fs}' + ' ' * max(0, W - len(fs)) + '║')
    cond = ('  個股進場：A=42↑20↑60↑+>60MA+MACD+K<門檻  B=42↑突破20  C=>42MA<20MA+K<門檻  '
            'D=>42MA+DIF+K<門檻  ETF不看財報純K值  📋財務品質：✅達標 ❌擋掉 ─無資料放行')
    print(f'║{cond}' + ' ' * max(0, W - len(cond)) + '║')
    print(f'╠{SEP}╣')

    # ── ★ 主動式ETF ───────────────────────────────────────────────────────────
    if etf_strong or etf_buy:
        hdr = (f'║  📡【主動式ETF掃描】共{len(etf_list)}檔  '
               f'🟢強烈買進:{len(etf_strong)}檔(日K≤{ETF_DAY_K_STRONG})  '
               f'🟡買進:{len(etf_buy)}檔(60分K≤{ETF_60M_K_BUY})  '
               f'─觀望:{len(etf_watch)}檔')
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        if etf_strong:
            print(f'║  ── 🟢 強烈買進（日K≤{ETF_DAY_K_STRONG}）───────────────────────────────────')
            for r in etf_strong: print_etf_row(r, '🟢')
        if etf_buy:
            print(f'║  ── 🟡 買進（60分K≤{ETF_60M_K_BUY}）────────────────────────────────────────')
            for r in etf_buy: print_etf_row(r, '🟡')
        print(f'╠{SEP}╣')
    elif etf_list:
        hdr = (f'║  📡【主動式ETF掃描】共{len(etf_list)}檔  '
               f'目前全部觀望（K值偏高）  日K≤{ETF_DAY_K_STRONG}出🟢訊號  60分K≤{ETF_60M_K_BUY}出🟡訊號')
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        watch_sorted = sorted(etf_watch, key=lambda r: min(r.get('k_day') or 99, r.get('k60') or 99))[:5]
        if watch_sorted:
            print(f'╠{sep}╣')
            print(f'║  ── 最接近買進門檻 TOP5 ──────────────────────────────────────────────')
            for r in watch_sorted: print_etf_row(r, '─')
        print(f'╠{SEP}╣')

    # ── 緊急出場 ─────────────────────────────────────────────────────────────
    if urgent_exit:
        urgent_s = sorted(urgent_exit, key=lambda r: r.get('price') or 0, reverse=True)
        hdr = f'║  🛑🛑【緊急出場警示】— {len(urgent_exit)} 檔  ← 立即處理！'
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        for r in urgent_s:
            ei      = r.get('exit_info') or {}
            p       = r.get('price', 0); k = r.get('k60')
            reasons = ' / '.join(ei.get('exit_reasons', []))[:70]
            sl      = ei.get('stop_loss_price')
            sl_s    = f'  停損:{sl:.2f}' if sl else ''
            mt      = _mom_tag(r); ft = _fin_tag(r)
            print(f'║  🛑 {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
                  f'K={f"{k:.0f}" if k else "?"}  {reasons}{sl_s}{mt}{ft}')
        print(f'╠{SEP}╣')

    # ── A / B / C / D ────────────────────────────────────────────────────────
    for grp, label, desc in [
        (grp_A, '⭐⭐⭐', f'A類：42↑20↑60↑+>60MA+DIF+K≤{k_thr}（☯三線）'),
        (grp_B, '⭐⭐ ', 'B類：42↑+突破20MA'),
        (grp_C, '⭐⭐ ', f'C類：>42MA<20MA+K≤{k_thr}'),
        (grp_D, '⭐⭐ ', f'D類：>42MA+DIF+K≤{k_thr}'),
    ]:
        if grp:
            s = sorted(grp, key=lambda r: _sort_key(r, fund_df))
            print(f'║  {label} 【{desc}】  — {len(grp)} 檔'
                  + ' ' * max(0, W - len(desc) - len(str(len(grp))) - 12) + '║')
            print(f'╠{sep}╣')
            for r in s: print_entry_row(r, label)
            print(f'╠{SEP}╣')
        else:
            print(f'║  {label} 【{desc}】本輪無' + ' ' * max(0, W - len(desc) - 14) + '║')
            print(f'╠{SEP}╣')

    # ── ★ v4.7 財務品質未達標觀察區 ──────────────────────────────────────────
    if grp_fin_w:
        grp_fin_s = sorted(grp_fin_w, key=lambda r: _sort_key(r, fund_df))
        hdr = f'║  📋【財務品質未達標觀察】YOY達標但財務篩選擋掉  — {len(grp_fin_w)} 檔'
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        for r in grp_fin_s:
            fq  = r.get('fin_quality') or {}
            p   = r.get('price', 0); k = r.get('k60')
            reasons = '、'.join(fq.get('fin_fail_reasons', []))
            mt  = _mom_tag(r); it = _ind_tag(r['tid']); yn = _yoy_tag(r['tid'])
            print(f'║  📋 {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
                  f'K={f"{k:.1f}" if k else "N/A"}  ❌{reasons}{mt}{it}{yn}')
        print(f'╠{SEP}╣')

    # ── V-A 黃金量增 ─────────────────────────────────────────────────────────
    if vol_best:
        vol_best_s = sorted(vol_best, key=lambda r: (
            priority_score(get_industry(r['tid'], fund_df)),
            -(r.get('vol_surge') or {}).get('ratio_ma5', 0)))
        hdr = (f'║  🚀🚀【V-A 主力量增】>{VOLUME_MA5_RATIO}x5日均量+K≤{k_thr}'
               f'  — {len(vol_best)} 檔  🏆優先族群排前')
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        for r in vol_best_s:
            vs     = r.get('vol_surge') or {}
            p      = r.get('price', 0); k = r.get('k60')
            et     = r.get('entry_type') or '─'
            m42r_s = '42↑' if r.get('ma42_rising') else '42↓'
            m20r_s = '20↑' if r.get('ma20_rising') else '20↓'
            dif_s4 = 'DIF✅' if r.get('dif_above_signal') else 'DIF❌'
            ch_n   = channel_note(r); it = _ind_tag(r['tid'])
            yn     = _yoy_tag(r['tid']); mt = _mom_tag(r); ft = _fin_tag(r)
            c_s    = f'{vs.get("curr_vol", 0)/1000:.0f}張'
            m_s    = f'{vs.get("vol_ma5", 0)/1000:.0f}張'
            ct     = f' 🔁{vs.get("consec_days", 0)}日' if vs.get('consec_days', 0) >= VOLUME_CONSEC_DAYS else ''
            mg     = r.get('mom_grade', 'NA')
            pos    = calc_position_advice(et, regime, active_params, 'best', mg)
            ei     = r.get('exit_info') or {}
            sl     = ei.get('stop_loss_price')
            sl_s   = f'  停損:{sl:.2f}' if sl else ''
            print(f'║  🚀 {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
                  f'K={f"{k:.0f}" if k else "?"}  {vs.get("ratio_ma5", 0):.1f}x均量（{c_s} vs 均{m_s}）{ct}  '
                  f'{m42r_s}{m20r_s} {dif_s4}  倉:{pos}{sl_s}  技術:{et}{ch_n}{mt}{ft}{it}{yn}')
        print(f'╠{SEP}╣')

    # ── 連續量增 ─────────────────────────────────────────────────────────────
    if vol_consec:
        vol_consec_s = sorted(vol_consec, key=lambda r: (
            priority_score(get_industry(r['tid'], fund_df)),
            -(r.get('vol_surge') or {}).get('consec_days', 0)))
        hdr = (f'║  📶【連續量增 法人代理 TOP10】連續≥{VOLUME_CONSEC_DAYS}日量>{VOLUME_CONSEC_RATIO}x均量'
               f'  — 共{len(vol_consec)}檔，顯示量能最大前10')
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        for r in vol_consec_s[:10]:
            vs     = r.get('vol_surge') or {}
            p      = r.get('price', 0); k = r.get('k60')
            et     = r.get('entry_type') or '─'
            m42r_s = '42↑' if r.get('ma42_rising') else '42↓'
            m20r_s = '20↑' if r.get('ma20_rising') else '20↓'
            ch_n   = channel_note(r); it = _ind_tag(r['tid'])
            yn     = _yoy_tag(r['tid']); mt = _mom_tag(r); ft = _fin_tag(r)
            c_s    = f'{vs.get("curr_vol", 0)/1000:.0f}張'
            m_s    = f'{vs.get("vol_ma5", 0)/1000:.0f}張'
            ratio  = vs.get('ratio_ma5', 0)
            print(f'║  📶 {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
                  f'K={f"{k:.0f}" if k else "?"}  連續{vs.get("consec_days", 0)}日量增  '
                  f'{ratio:.1f}x均量（{c_s} vs 均{m_s}）  '
                  f'{m42r_s}{m20r_s}  技術:{et}{ch_n}{mt}{ft}{it}{yn}')
        print(f'╠{SEP}╣')

    # ── V-B 觀察 ─────────────────────────────────────────────────────────────
    if vol_watch:
        vol_watch_s = sorted(vol_watch, key=lambda r: (
            priority_score(get_industry(r['tid'], fund_df)),
            -(r.get('vol_surge') or {}).get('ratio_ma5', 0)))
        hdr = f'║  ⚡ 【V-B 量增留意】>{VOLUME_MA5_RATIO}x均量  — {len(vol_watch)} 檔'
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        for r in vol_watch_s:
            vs     = r.get('vol_surge') or {}
            p      = r.get('price', 0); k = r.get('k60')
            et     = r.get('entry_type') or '─'
            m42r_s = '42↑' if r.get('ma42_rising') else '42↓'
            m20r_s = '20↑' if r.get('ma20_rising') else '20↓'
            ch_n   = channel_note(r); it = _ind_tag(r['tid'])
            yn     = _yoy_tag(r['tid']); mt = _mom_tag(r); ft = _fin_tag(r)
            print(f'║  ⚡ {r["tid"]:<7}{r["name"]:<8}  現價:{p:>8.2f}  '
                  f'K={f"{k:.0f}" if k else "?"}  {vs.get("ratio_ma5", 0):.1f}x均量  '
                  f'{m42r_s}{m20r_s}  技術:{et}{ch_n}{mt}{ft}{it}{yn}')
        print(f'╠{SEP}╣')

    # ── 資料異常 ─────────────────────────────────────────────────────────────
    if errors:
        err_str = '  '.join(f'{r["tid"]}({r["error"]})' for r in errors)
        print(f'║  ⚠️  【資料異常】{len(errors)}檔：{err_str[:W-20]}'
              + ' ' * max(0, W - 20 - min(len(err_str), W - 20)) + '║')
        print(f'╠{SEP}╣')

    # ── 出場訊號 ─────────────────────────────────────────────────────────────
    if grp_exit:
        grp_exit_s = sorted(grp_exit, key=lambda r: r.get('price') or 0, reverse=True)
        print(f'║  🔴 【出場訊號】跌破20MA（42↑+20↑）  — {len(grp_exit)} 檔'
              + ' ' * max(0, W - 36 - len(str(len(grp_exit)))) + '║')
        print(f'╠{sep}╣')
        for r in grp_exit_s: print_exit_row(r)
        print(f'╠{SEP}╣')

    # ── 通道賣出 ─────────────────────────────────────────────────────────────
    if ch_sell:
        ch_sell_s = sorted(ch_sell, key=lambda r: -(r['channel']['pos_pct'] or 0))
        ch_high   = [r for r in ch_sell_s if r['channel'].get('confidence') == 'high']
        ch_med    = [r for r in ch_sell_s if r['channel'].get('confidence') == 'medium']
        ch_low    = [r for r in ch_sell_s if r['channel'].get('confidence') == 'low']
        hdr = (f'║  📐【通道賣出區間+KD死叉】靠近上軌且60分KD死叉 — {len(ch_sell)}檔  '
               f'(⭐⭐:{len(ch_high)}  ⭐:{len(ch_med)}  △:{len(ch_low)})')
        print(hdr + ' ' * max(0, W - len(hdr) + 2) + '║')
        print(f'╠{sep}╣')
        for r in ch_sell_s: print_ch_sell_row(r)
        print(f'╠{SEP}╣')

    # ── 說明 ─────────────────────────────────────────────────────────────────
    note = (f'  📌 A=42↑20↑60↑+DIF+K≤{active_params["k_threshold"]}  '
            f'B=42↑突破20  C=>42<20+K≤{active_params["k_threshold"]}  '
            f'D=>42+DIF+K≤{active_params["k_threshold"]}  '
            f'MoM:✅≥0% ~<-3% ⚠<-7% 🔴觀察  停損{active_params["stop_loss_pct"]*100:.0f}%  '
            f'📋毛利≥{GROSS_MARGIN_MIN:.0f}% 營益≥{OP_MARGIN_MIN:.0f}% 保障>{INTEREST_COVER_MIN:.0f}x')
    print(f'║{note}' + ' ' * max(0, W - len(note)) + '║')
    print(f'╚{SEP}╝')


# =============================================================================
# 以下是 v4.9 文件2 截斷處的補完內容
# 接在 analyze_stock(..., fin_quality= 之後
# =============================================================================

# ── run_once() 中 個股掃描 迴圈的補完 ─────────────────────────────────────────
#
#         r = analyze_stock(tid, name, _guess_mtype(tid),
#                           entry_price=None,
#                           active_params=active_params,
#                           mom_pct=mom_pct,
#                           fin_quality=fin_quality)        # ← 這行接上去
#         results.append(r)
#     print(' ' * 60, end='\r')
#
#     print_scan_result(results, scan_count, tw_now, regime,
#                       fund_set=fund_set, fund_df=fund_df,
#                       active_params=active_params,
#                       etf_results=etf_results,
#                       fin_quality_dict=fin_quality_dict)
#     return results, etf_results
#
# 下面是完整的 main() 以及 __main__ 入口
# =============================================================================

def run_once(stocks, scan_count, tw_tz, regime=None,
             fund_df=None, fund_set=None, mom_dict=None,
             active_etfs=None, fin_quality_dict=None):
    tw_now        = datetime.now(tw_tz)
    active_params = get_active_params(tw_now)
    if mom_dict         is None: mom_dict         = {}
    if active_etfs      is None: active_etfs      = {}
    if fin_quality_dict is None: fin_quality_dict = {}

    print(f'\n🔄 第 {scan_count} 次掃描中... ({tw_now.strftime("%H:%M:%S")})')
    if active_params['warning']:
        print(f'\n  {active_params["warning_msg"]}')

    if regime is None:
        regime = calc_market_regime()
    print_regime_banner(regime)

    # ── 主動式ETF ──────────────────────────────────────────────────────────────
    etf_results = []
    etf_total   = len(active_etfs)
    if etf_total:
        print(f'  [ETF] 掃描 {etf_total} 檔主動式ETF...')
        for i, (tid, name) in enumerate(active_etfs.items()):
            print(f'  [ETF {i+1}/{etf_total}] {tid} {name}...', end='\r')
            r = analyze_etf(tid, name, active_params)
            etf_results.append(r)
        ok  = sum(1 for r in etf_results if not r.get('error'))
        sig = sum(1 for r in etf_results if r.get('etf_signal') in ('strong_buy', 'buy'))
        print(f'  [ETF] ✅ 完成 {ok}/{etf_total} 檔，{sig} 檔有訊號          ')

    # ── 個股掃描 ──────────────────────────────────────────────────────────────
    results = []
    total   = len(stocks)
    for i, (tid, name) in enumerate(stocks.items()):
        print(f'  [{i+1}/{total}] {tid} {name}...', end='\r')
        mom_pct     = get_mom_pct(tid, fund_df, mom_dict)
        fin_quality = fin_quality_dict.get(tid)
        r = analyze_stock(tid, name, _guess_mtype(tid),
                          entry_price=None,
                          active_params=active_params,
                          mom_pct=mom_pct,
                          fin_quality=fin_quality)
        results.append(r)
    print(' ' * 60, end='\r')

    print_scan_result(results, scan_count, tw_now, regime,
                      fund_set=fund_set, fund_df=fund_df,
                      active_params=active_params,
                      etf_results=etf_results,
                      fin_quality_dict=fin_quality_dict)
    return results, etf_results


def main(interval=INTERVAL_MIN, k=K_THRESHOLD, once=False, yoy_min=YOY_MIN_PCT):
    global K_THRESHOLD, YOY_MIN_PCT
    K_THRESHOLD = k
    YOY_MIN_PCT = yoy_min

    tw_tz  = pytz.timezone('Asia/Taipei')
    tw_now = datetime.now(tw_tz)
    active_params = get_active_params(tw_now)

    print('=' * 72)
    print(f'🚀 台股掃描系統 v4.9 ★撿股讚三頁財務整合版')
    print(f'📡 主動ETF：日K≤{ETF_DAY_K_STRONG}強烈買進  60分K≤{ETF_60M_K_BUY}買進  純技術不看財報')
    print(f'★ YOY門檻：{yoy_min}%（個股）  MoM分級：✅≥0% | ~<-3% | ⚠<-7% | 🔴觀察')
    print(f'★ 財務品質：毛利≥{GROSS_MARGIN_MIN:.0f}%(連兩季)  營益≥{OP_MARGIN_MIN:.0f}%(趨勢不滑)  保障>{INTEREST_COVER_MIN:.0f}x')
    print(f'  來源：撿股讚75684/75686 → FinMind → yfinance  無資料→放行  模式：{"擋掉" if FIN_BLOCK_ON_FAIL else "標記"}')
    print(f'🚪 出場v4.8：60分K死叉需日K>60｜MoM≥0%+通道低位豁免｜≥2訊號才緊急出場')
    print(f'☯ 勿逐:{CHANNEL_NO_ENTRY_PCT}%  大壯利貞:A類需60MA={A_CLASS_REQUIRE_60MA}')
    print(f'★ 量增:>{VOLUME_MA5_RATIO}x5日均量  📶法人代理:連續{VOLUME_CONSEC_DAYS}日>{VOLUME_CONSEC_RATIO}x')
    print(f'★ 出場：停損{STOP_LOSS_PCT*100:.0f}%｜停利觸發+{TAKE_PROFIT_TRIGGER*100:.0f}%｜移動回撤-{TRAILING_STOP_PCT*100:.0f}%')
    print(f'☯ 臨卦警戒期：每年{AUGUST_WARN_START[0]}/{AUGUST_WARN_START[1]}~{AUGUST_WARN_END[0]}/{AUGUST_WARN_END[1]}')
    if active_params['warning']:
        print(f'\n  ⚠️  {active_params["warning_msg"]}')
    print('=' * 72)

    regime        = calc_market_regime(force=True)
    fund_df, html = fetch_wespai_fundamental(force=True)

    if fund_df.empty:
        print('\n❌ 無法取得股票清單。'); return

    fund_set    = build_fundamental_filter(fund_df)
    stocks      = build_yoy_stocks(fund_df, fund_set)
    codes       = list(stocks.keys())
    mom_dict    = fetch_mom_data(codes, wespai_html=html, force=True)
    active_etfs = fetch_active_etf_list(force=True)

    # ── ★ v4.9 財務品質：先抓撿股讚財務頁(75684+75686)，再批量評估 ──────────
    df_fin_wespai    = fetch_wespai_fin_quality(force=True)
    fin_quality_dict = fetch_fin_quality_batch(stocks, force=True,
                                               df_fin_wespai=df_fin_wespai)
    fund_set_fin     = build_fin_quality_filter(fund_set, fin_quality_dict)
    stocks_fin       = {c: n for c, n in stocks.items() if c in fund_set_fin}

    print(f'✅ 撿股讚抓到：{len(fund_df)} 檔')
    print(f'⚡ YOY≥{yoy_min}%達標：{len(stocks)} 檔')
    fin_pass = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('fin_pass') is True)
    fin_fail = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('fin_pass') is False)
    fin_na   = len(stocks) - fin_pass - fin_fail
    wespai_n = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('source') == 'wespai')
    print(f'📋 財務品質：達標{fin_pass}檔  不達標{fin_fail}檔  無資料放行{fin_na}檔  '
          f'（撿股讚來源:{wespai_n}檔）  → 最終掃描:{len(stocks_fin) if FIN_BLOCK_ON_FAIL else len(stocks)}檔')
    print(f'📊 MoM月增率：{sum(1 for c in codes if c in mom_dict)}/{len(codes)} 檔有資料')
    print(f'📡 主動式ETF：{len(active_etfs)} 檔')
    scan_stocks = stocks_fin if FIN_BLOCK_ON_FAIL else stocks
    print(f'💡 預估時間：約 {(len(scan_stocks)+len(active_etfs))*4//60} 分 '
          f'{(len(scan_stocks)+len(active_etfs))*4%60} 秒/輪')

    scan_count = 0
    while True:
        scan_count += 1
        regime        = calc_market_regime()
        fund_df, html = fetch_wespai_fundamental()
        if not fund_df.empty:
            fund_set         = build_fundamental_filter(fund_df)
            stocks           = build_yoy_stocks(fund_df, fund_set)
            codes            = list(stocks.keys())
            mom_dict         = fetch_mom_data(codes, wespai_html=html)
            df_fin_wespai    = fetch_wespai_fin_quality()
            fin_quality_dict = fetch_fin_quality_batch(stocks,
                                                       df_fin_wespai=df_fin_wespai)
            fund_set_fin     = build_fin_quality_filter(fund_set, fin_quality_dict)
            scan_stocks      = {c: n for c, n in stocks.items() if c in fund_set_fin} \
                               if FIN_BLOCK_ON_FAIL else stocks
        active_etfs = fetch_active_etf_list()

        run_once(scan_stocks, scan_count, tw_tz, regime,
                 fund_df=fund_df, fund_set=fund_set,
                 mom_dict=mom_dict,
                 active_etfs=active_etfs,
                 fin_quality_dict=fin_quality_dict)
        if once: break
        next_t = datetime.now(tw_tz) + timedelta(minutes=interval)
        print(f'\n⏳ 下次掃描：{next_t.strftime("%H:%M:%S")}（{interval}分鐘後）| Ctrl+C 停止')
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print(f'\n👋 手動停止，共完成 {scan_count} 次掃描。')
            break


if __name__ == '__main__':
    import sys
    _in_colab = 'google.colab' in sys.modules or 'ipykernel' in sys.modules
    if _in_colab:
        main(interval=15, k=30, yoy_min=15.0, once=False)
    else:
        import argparse
        parser = argparse.ArgumentParser(description='台股掃描 v4.9 撿股讚三頁財務整合版')
        parser.add_argument('--interval',        default=INTERVAL_MIN,        type=int)
        parser.add_argument('--k',               default=K_THRESHOLD,         type=int)
        parser.add_argument('--yoy-min',         default=YOY_MIN_PCT,         type=float)
        parser.add_argument('--vol-ratio',       default=VOLUME_MA5_RATIO,    type=float)
        parser.add_argument('--stop-loss',       default=STOP_LOSS_PCT,       type=float)
        parser.add_argument('--take-profit',     default=TAKE_PROFIT_TRIGGER, type=float)
        parser.add_argument('--etf-day-k',       default=ETF_DAY_K_STRONG,    type=int)
        parser.add_argument('--etf-60m-k',       default=ETF_60M_K_BUY,       type=int)
        parser.add_argument('--gross-margin',    default=GROSS_MARGIN_MIN,    type=float)
        parser.add_argument('--op-margin',       default=OP_MARGIN_MIN,       type=float)
        parser.add_argument('--interest-cover',  default=INTEREST_COVER_MIN,  type=float)
        parser.add_argument('--no-fin-block',    action='store_true',
                            help='僅標記財務品質，不擋掉不達標個股')
        parser.add_argument('--once',            action='store_true')
        args = parser.parse_args()

        VOLUME_MA5_RATIO    = args.vol_ratio
        STOP_LOSS_PCT       = args.stop_loss
        TAKE_PROFIT_TRIGGER = args.take_profit
        ETF_DAY_K_STRONG    = args.etf_day_k
        ETF_60M_K_BUY       = args.etf_60m_k
        GROSS_MARGIN_MIN    = args.gross_margin
        OP_MARGIN_MIN       = args.op_margin
        INTEREST_COVER_MIN  = args.interest_cover
        FIN_BLOCK_ON_FAIL   = not args.no_fin_block

        main(interval=args.interval, k=args.k,
             yoy_min=args.yoy_min, once=args.once)