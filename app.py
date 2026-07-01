# =============================================================================
# 台股掃描系統 — Flask API  v7.0
# 架構設計：
#   - 管理員：可手動觸發掃描、調整參數、管理試用帳號
#   - 會員/試用：只能查看最新掃描結果
#   - scanner_core 只在「真正開始掃描」時才 import（不在啟動時載入）
#   - 掃描由管理員按按鈕觸發，不自動排程（避免 Render 免費方案冷啟動問題）
#   - 每次掃描有 SCAN_TIMEOUT_SEC 看門狗，卡住會自動放棄並報錯
# =============================================================================
import os, json, secrets, threading, time, traceback, sqlite3, hashlib
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, render_template, request,
                   Response, make_response, redirect, url_for)
import pytz

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

TW_TZ = pytz.timezone('Asia/Taipei')
def _now_tw():
    return datetime.now(TW_TZ)

# =============================================================================
# ★ 密碼設定（環境變數優先）
#   ADMIN_PASSWORD  — 管理員密碼（可執行掃描、管理帳號）
#   FULL_PASSWORDS  — 正式會員密碼，逗號分隔，有效 180 天
# =============================================================================
ADMIN_PASSWORD    = os.environ.get('ADMIN_PASSWORD',   'admin_bob0309')
_DEFAULT_FULL_PWS = ['stock2024vip', 'bob0309']

def _load_full_passwords():
    env = os.environ.get('FULL_PASSWORDS', '')
    return set(p.strip() for p in env.split(',') if p.strip()) or set(_DEFAULT_FULL_PWS)

# =============================================================================
# SQLite — 試用帳號 + Sessions
# =============================================================================
_DB_PATH = Path(os.environ.get('DATA_DIR', '/tmp')) / 'scanner_auth.db'
_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trial_accounts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            label      TEXT NOT NULL,
            password   TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at    TEXT,
            is_active  INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            pw_type    TEXT NOT NULL,
            account_id INTEGER,
            expires_at TEXT NOT NULL
        );
        """)

_init_db()

# =============================================================================
# Session 管理
# =============================================================================
def _issue_token(pw_type, expires_iso, account_id=None):
    token = secrets.token_urlsafe(32)
    with _db_lock:
        with _db() as c:
            c.execute('INSERT INTO sessions(token,pw_type,account_id,expires_at) VALUES(?,?,?,?)',
                      (token, pw_type, account_id, expires_iso))
    return token

def _validate_token(token):
    if not token:
        return None
    with _db_lock:
        with _db() as c:
            row = c.execute('SELECT pw_type,account_id,expires_at FROM sessions WHERE token=?',
                            (token,)).fetchone()
    if not row:
        return None
    exp = datetime.fromisoformat(row['expires_at'])
    if exp.tzinfo is None:
        exp = TW_TZ.localize(exp)
    if _now_tw() > exp:
        with _db_lock:
            with _db() as c:
                c.execute('DELETE FROM sessions WHERE token=?', (token,))
        return None
    return dict(row)

def _token_from_req():
    t = request.cookies.get('auth_token', '')
    if not t:
        ah = request.headers.get('Authorization', '')
        if ah.startswith('Bearer '):
            t = ah[7:]
    return t

def _session():
    return _validate_token(_token_from_req())

def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        s = _session()
        if not s:
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登入', 'code': 'UNAUTHORIZED'}), 401
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        s = _session()
        if not s or s['pw_type'] != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'error': '需要管理員權限'}), 403
            return redirect(url_for('login_page'))
        return f(*a, **kw)
    return d

# =============================================================================
# Auth Routes
# =============================================================================
@app.route('/login')
def login_page():
    if _session():
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    pw = (request.get_json(silent=True) or {}).get('password', '').strip()
    if not pw:
        return jsonify({'error': '請輸入密碼'}), 400
    now = _now_tw()

    if pw == ADMIN_PASSWORD:
        exp = (now + timedelta(hours=12)).isoformat()
        tok = _issue_token('admin', exp)
        exp_s = datetime.fromisoformat(exp).strftime('%Y-%m-%d %H:%M')
        resp = make_response(jsonify({'ok':True,'type':'admin','expires':exp_s,
                                      'message':f'管理員登入，有效至 {exp_s}'}))
        resp.set_cookie('auth_token', tok, max_age=12*3600,
                        httponly=True, samesite='Lax', secure=request.is_secure)
        return resp

    if pw in _load_full_passwords():
        exp = (now + timedelta(days=180)).isoformat()
        tok = _issue_token('full', exp)
        exp_s = datetime.fromisoformat(exp).strftime('%Y-%m-%d %H:%M')
        resp = make_response(jsonify({'ok':True,'type':'full','expires':exp_s,
                                      'message':f'正式帳號，有效至 {exp_s}'}))
        resp.set_cookie('auth_token', tok, max_age=180*86400,
                        httponly=True, samesite='Lax', secure=request.is_secure)
        return resp

    with _db_lock:
        with _db() as c:
            acc = c.execute('SELECT * FROM trial_accounts WHERE password=? AND is_active=1',
                            (pw,)).fetchone()
    if acc:
        exp = datetime.fromisoformat(acc['expires_at'])
        if exp.tzinfo is None:
            exp = TW_TZ.localize(exp)
        if now > exp:
            time.sleep(1)
            return jsonify({'error': f'此試用帳號已於 {exp.strftime("%Y-%m-%d %H:%M")} 到期'}), 403
        if not acc['used_at']:
            with _db_lock:
                with _db() as c:
                    c.execute('UPDATE trial_accounts SET used_at=? WHERE id=?',
                              (now.isoformat(), acc['id']))
        tok = _issue_token('trial', acc['expires_at'], account_id=acc['id'])
        exp_s = exp.strftime('%Y-%m-%d %H:%M')
        days_left = max(1, (exp - now).days + 1)
        resp = make_response(jsonify({'ok':True,'type':'trial','expires':exp_s,
                                      'message':f'試用帳號「{acc["label"]}」，有效至 {exp_s}'}))
        resp.set_cookie('auth_token', tok, max_age=days_left*86400,
                        httponly=True, samesite='Lax', secure=request.is_secure)
        return resp

    time.sleep(1)
    return jsonify({'error': '密碼錯誤'}), 401

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    tok = _token_from_req()
    with _db_lock:
        with _db() as c:
            c.execute('DELETE FROM sessions WHERE token=?', (tok,))
    resp = make_response(jsonify({'ok': True}))
    resp.delete_cookie('auth_token')
    return resp

@app.route('/api/auth/me')
def api_me():
    s = _session()
    if not s:
        return jsonify({'logged_in': False}), 401
    exp_s = datetime.fromisoformat(s['expires_at']).strftime('%Y-%m-%d %H:%M')
    labels = {'trial':'試用帳號','full':'正式帳號（180天）','admin':'管理員'}
    return jsonify({'logged_in':True,'type':s['pw_type'],
                    'expires':exp_s,'label':labels.get(s['pw_type'], s['pw_type'])})

# =============================================================================
# 管理員後台 API（試用帳號管理）
# =============================================================================
@app.route('/admin')
@admin_required
def admin_page():
    return render_template('admin.html')

@app.route('/api/admin/trials', methods=['GET'])
@admin_required
def admin_list_trials():
    with _db_lock:
        with _db() as c:
            rows = c.execute('SELECT * FROM trial_accounts ORDER BY created_at DESC').fetchall()
    now = _now_tw()
    result = []
    for r in rows:
        exp = datetime.fromisoformat(r['expires_at'])
        if exp.tzinfo is None:
            exp = TW_TZ.localize(exp)
        result.append({
            'id': r['id'], 'label': r['label'], 'password': r['password'],
            'created_at': r['created_at'][:16].replace('T',' '),
            'expires_at': r['expires_at'][:16].replace('T',' '),
            'used_at': (r['used_at'] or '')[:16].replace('T',' '),
            'is_active': r['is_active'], 'expired': now > exp,
        })
    return jsonify(result)

@app.route('/api/admin/trials', methods=['POST'])
@admin_required
def admin_create_trial():
    d = request.get_json(silent=True) or {}
    label = d.get('label','').strip()
    pw    = d.get('password','').strip()
    days  = int(d.get('days', 1))
    if not label or not pw:
        return jsonify({'error': '請填寫備註與密碼'}), 400
    if not 1 <= days <= 365:
        return jsonify({'error': '天數需介於 1~365'}), 400
    now = _now_tw()
    exp = (now + timedelta(days=days)).isoformat()
    try:
        with _db_lock:
            with _db() as c:
                c.execute('INSERT INTO trial_accounts(label,password,created_at,expires_at) VALUES(?,?,?,?)',
                          (label, pw, now.isoformat(), exp))
        return jsonify({'ok': True, 'expires': exp[:16].replace('T',' ')})
    except sqlite3.IntegrityError:
        return jsonify({'error': '此密碼已存在'}), 409

@app.route('/api/admin/trials/<int:tid>', methods=['DELETE'])
@admin_required
def admin_delete_trial(tid):
    with _db_lock:
        with _db() as c:
            c.execute('DELETE FROM trial_accounts WHERE id=?', (tid,))
    return jsonify({'ok': True})

@app.route('/api/admin/trials/<int:tid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_trial(tid):
    with _db_lock:
        with _db() as c:
            row = c.execute('SELECT is_active FROM trial_accounts WHERE id=?', (tid,)).fetchone()
            if not row:
                return jsonify({'error': '找不到'}), 404
            nv = 0 if row['is_active'] else 1
            c.execute('UPDATE trial_accounts SET is_active=? WHERE id=?', (nv, tid))
    return jsonify({'ok': True, 'is_active': nv})

# =============================================================================
# 掃描引擎
#
# 設計原則（v7.0 打掉重練）：
#   1. scanner_core 的 import 完全移出啟動流程。
#      Flask 啟動時不做任何 import、不啟動任何背景執行緒。
#      只有管理員按下「開始掃描」才會真正 import + 執行。
#      這樣 Render 啟動時永遠不會卡住，登入頁面永遠能打開。
#
#   2. 「管理員觸發」模式，不自動排程。
#      自動排程在 Render 免費方案上問題太多（冷啟動、CPU 節流、
#      容器睡眠喚醒時序不穩）。改成管理員手動按按鈕，完全可控。
#      管理員可以設定「掃描完自動等 N 分鐘後再跑一次」（選填）。
#
#   3. 每次掃描有 SCAN_TIMEOUT_SEC 看門狗。
#      卡住超過這個時間，自動放棄本輪、顯示錯誤、解除 running 狀態，
#      讓管理員可以重新觸發。
#
#   4. 全體使用者共用同一份最新結果（_state），只讀不寫。
# =============================================================================

SCAN_TIMEOUT_SEC   = int(os.environ.get('SCAN_TIMEOUT_SEC',   '270'))
SCAN_INTERVAL_SEC  = int(os.environ.get('SCAN_INTERVAL_SEC',  '600'))  # 預設10分鐘

# 預設篩選參數（管理員可在 UI 上調整）
_DEFAULT_PARAMS = {
    'k_threshold':     int(os.environ.get('SCAN_K_THRESHOLD',      '30')),
    'yoy_min':         float(os.environ.get('SCAN_YOY_MIN',         '15')),
    'gross_margin_min':float(os.environ.get('SCAN_GROSS_MARGIN_MIN','25')),
    'op_margin_min':   float(os.environ.get('SCAN_OP_MARGIN_MIN',   '15')),
    'fin_block':       os.environ.get('SCAN_FIN_BLOCK','true').lower() != 'false',
    'interval_min':    int(os.environ.get('SCAN_INTERVAL_MIN', '10')),  # 循環間隔（分鐘）
}

# 全域掃描狀態（一把鎖保護）
_state_lock = threading.Lock()
_state = {
    'running':       False,
    'loop_active':   False,   # 是否在循環掃描模式
    'progress':      0,
    'log':           [],
    'last_log':      '',
    'error':         None,
    'last_scan':     None,
    'next_scan_eta': None,    # 下次掃描預計時間（ISO字串）
    'results':       None,
    'etf_results':   None,
    'regime':        None,
    'stats':         {},
    'params':        dict(_DEFAULT_PARAMS),
}

# 看門狗用的世代編號（每次新掃描 +1，讓舊的孤兒執行緒自動失效）
_gen = 0
_gen_lock = threading.Lock()

# 循環控制旗標（設定後讓 loop 執行緒退出等待、結束循環）
_loop_stop_event = threading.Event()

def _new_gen():
    global _gen
    with _gen_lock:
        _gen += 1
        return _gen

def _is_cur_gen(g):
    with _gen_lock:
        return _gen == g

def _log(msg, pct=None):
    with _state_lock:
        ts = _now_tw().strftime('%H:%M:%S')
        entry = f'[{ts}] {msg}'
        _state['log'].append(entry)
        if len(_state['log']) > 200:
            _state['log'] = _state['log'][-200:]
        _state['last_log'] = msg
        if pct is not None:
            _state['progress'] = min(int(pct), 99)

def _set_state(**kw):
    with _state_lock:
        _state.update(kw)

def _r2d(r):
    """result dict → JSON-safe dict"""
    out = {}
    for k, v in r.items():
        if hasattr(v, 'isoformat'):          out[k] = v.isoformat()
        elif hasattr(v, 'to_dict'):          out[k] = str(v)
        elif isinstance(v, float) and v!=v:  out[k] = None
        else:
            try:    json.dumps(v); out[k] = v
            except: out[k] = str(v)
    return out

# =============================================================================
# 核心掃描函式（在獨立 daemon 執行緒內跑）
# =============================================================================
def _run_scan(gen, params):
    try:
        # ── Step 0: import scanner_core（只在這裡 import，不在啟動時） ──────
        _log('載入掃描模組...', 2)
        try:
            import scanner_core as sc
        except Exception as e:
            raise RuntimeError(f'scanner_core 載入失敗：{e}')

        if not _is_cur_gen(gen): return

        # 套用參數
        sc.K_THRESHOLD       = params['k_threshold']
        sc.YOY_MIN_PCT       = params['yoy_min']
        sc.GROSS_MARGIN_MIN  = params['gross_margin_min']
        sc.OP_MARGIN_MIN     = params['op_margin_min']
        sc.FIN_BLOCK_ON_FAIL = params['fin_block']
        _log('掃描模組就緒', 5)

        # ── Step 1: 撿股讚基本面 ─────────────────────────────────────────────
        if not _is_cur_gen(gen): return
        _log('抓取撿股讚基本面資料...', 8)
        fund_df, html = sc.fetch_wespai_fundamental(force=True)
        if fund_df is None or fund_df.empty:
            raise RuntimeError('撿股讚基本面抓取失敗或空白，請確認 wespai.com 是否可連線')
        _log(f'撿股讚抓到 {len(fund_df)} 檔', 15)

        fund_set = sc.build_fundamental_filter(fund_df)
        stocks   = sc.build_yoy_stocks(fund_df, fund_set)
        _log(f'YOY≥{params["yoy_min"]:.0f}% 達標：{len(stocks)} 檔', 22)

        # ── Step 2: 月增率 ────────────────────────────────────────────────────
        if not _is_cur_gen(gen): return
        _log('抓取月增率 (MoM) 資料...', 25)
        mom_dict = sc.fetch_mom_data(list(stocks.keys()), wespai_html=html, force=True)
        _log(f'MoM：{len(mom_dict)} 檔', 30)

        # ── Step 3: 財務品質 ──────────────────────────────────────────────────
        if not _is_cur_gen(gen): return
        _log('抓取財務品質資料...', 33)
        df_fin   = sc.fetch_wespai_fin_quality(force=True)
        fin_dict = sc.fetch_fin_quality_batch(stocks, force=True, df_fin_wespai=df_fin)
        fin_set  = sc.build_fin_quality_filter(fund_set, fin_dict)
        scan_st  = ({c:n for c,n in stocks.items() if c in fin_set}
                    if params['fin_block'] else stocks)
        _log(f'財務篩選後：{len(scan_st)} 檔（財務達標 {len(fin_set)}/{len(stocks)}）', 40)

        # ── Step 4: 大盤狀態 ──────────────────────────────────────────────────
        if not _is_cur_gen(gen): return
        _log('計算大盤狀態...', 43)
        try:
            regime = sc.calc_market_regime(force=True)
        except Exception as e:
            regime = {}
            _log(f'大盤狀態計算失敗（跳過）：{e}', 43)

        # ── Step 5: ETF 掃描 ──────────────────────────────────────────────────
        if not _is_cur_gen(gen): return
        _log('掃描主動式 ETF...', 46)
        etf_results = []
        etf_fail = 0
        try:
            active_etfs   = sc.fetch_active_etf_list(force=True)
            active_params = sc.get_active_params(datetime.now(TW_TZ))
            for t, n in active_etfs.items():
                if not _is_cur_gen(gen): return
                try:
                    etf_results.append(_r2d(sc.analyze_etf(t, n, active_params)))
                except Exception as e:
                    etf_fail += 1
                    etf_results.append({'tid':t,'name':n,'error':str(e)})
        except Exception as e:
            _log(f'ETF 清單抓取失敗（跳過）：{e}', 48)
        _log(f'ETF 完成：{len(etf_results)} 檔' +
             (f'（{etf_fail} 失敗）' if etf_fail else ''), 50)

        # ── Step 6: 個股掃描 ──────────────────────────────────────────────────
        if not _is_cur_gen(gen): return
        total   = len(scan_st) or 1
        results = []
        fail_n  = 0
        _log(f'開始個股分析，共 {len(scan_st)} 檔...', 53)
        active_params_stock = sc.get_active_params(datetime.now(TW_TZ))

        for i, (tid, name) in enumerate(scan_st.items()):
            if not _is_cur_gen(gen): return
            try:
                r = sc.analyze_stock(
                    tid, name, sc._guess_mtype(tid),
                    entry_price=None,
                    active_params=active_params_stock,
                    mom_pct=sc.get_mom_pct(tid, fund_df, mom_dict),
                    fin_quality=fin_dict.get(tid),
                )
                results.append(_r2d(r))
            except Exception as e:
                fail_n += 1
                results.append({'tid':tid,'name':name,'error':str(e)})
                app.logger.warning(f'個股 {tid} 失敗：{e}')

            pct = 53 + int((i+1)/total * 44)
            if (i+1) % 5 == 0 or (i+1) == total:
                msg = f'個股進度 {i+1}/{total}'
                if fail_n:
                    msg += f'（{fail_n} 失敗跳過）'
                _log(msg, pct)

        if not _is_cur_gen(gen): return

        fp = sum(1 for c in stocks if fin_dict.get(c,{}).get('fin_pass') is True)
        ff = sum(1 for c in stocks if fin_dict.get(c,{}).get('fin_pass') is False)

        _set_state(
            running=False, progress=100, error=None,
            last_scan=_now_tw().isoformat(),
            results=results, etf_results=etf_results, regime=regime,
            stats={
                'total_wespai': len(fund_df),
                'yoy_pass':     len(stocks),
                'fin_pass':     fp,
                'fin_fail':     ff,
                'scan_count':   len(scan_st),
                'etf_count':    len(etf_results),
                'mom_count':    len(mom_dict),
                'fail_count':   fail_n,
            },
        )
        _log(f'✅ 掃描完成！{len(results)} 檔個股 + {len(etf_results)} 檔ETF'
             + (f'，{fail_n} 檔失敗已跳過' if fail_n else ''), 100)

    except Exception as e:
        if not _is_cur_gen(gen): return
        _set_state(running=False, progress=0, error=str(e))
        _log(f'❌ 掃描失敗：{e}')
        app.logger.error(traceback.format_exc())


def _run_with_watchdog(gen, params):
    """執行一次掃描（帶看門狗），阻塞直到完成或逾時。"""
    worker = threading.Thread(target=_run_scan, args=(gen, params), daemon=True)
    worker.start()
    worker.join(timeout=SCAN_TIMEOUT_SEC)
    if worker.is_alive() and _is_cur_gen(gen):
        _set_state(running=False, progress=0,
                   error=f'掃描逾時（超過 {SCAN_TIMEOUT_SEC} 秒），'
                         f'請確認 yfinance / 撿股讚 是否可連線')
        _log(f'⏱ 掃描逾時（>{SCAN_TIMEOUT_SEC}秒）')


def _loop_thread(params):
    """
    循環掃描主執行緒：
      1. 執行一次掃描（帶看門狗）
      2. 掃描完成後等待 interval_min 分鐘（期間可被 stop 打斷）
      3. 若未被中斷，重複步驟 1
    整個 loop 跑在獨立 daemon thread，管理員按「中斷」時
    _loop_stop_event 被 set，loop 退出。
    """
    _loop_stop_event.clear()
    interval_sec = int(params.get('interval_min', 10)) * 60

    while not _loop_stop_event.is_set():
        # ── 執行一輪掃描 ──────────────────────────────────────────────────────
        gen = _new_gen()
        _set_state(running=True, progress=2, error=None, last_log='準備中...')
        _run_with_watchdog(gen, params)

        # 掃描被外部中斷（世代失效）→ 退出 loop
        if not _is_cur_gen(gen):
            break

        # 掃描完成（不管成功還是逾時），檢查是否要繼續循環
        if _loop_stop_event.is_set():
            break

        # ── 等待下次掃描 ──────────────────────────────────────────────────────
        next_eta = (_now_tw() + timedelta(seconds=interval_sec)).isoformat()
        _set_state(loop_active=True, next_scan_eta=next_eta)
        _log(f'⏱ 等待 {params.get("interval_min",10)} 分鐘後自動重新掃描...')

        # 用 wait(timeout) 取代 sleep，這樣 stop 事件可以即時打斷等待
        _loop_stop_event.wait(timeout=interval_sec)

    # loop 結束（被中斷或 stop 觸發）
    _set_state(loop_active=False, next_scan_eta=None)
    _log('⏹ 循環掃描已停止')

# =============================================================================
# 掃描 Routes
# =============================================================================
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/scan/start', methods=['POST'])
@admin_required
def api_scan_start():
    """
    管理員：啟動循環掃描。
    - 解析新參數（含 interval_min 循環間隔）
    - 若目前有掃描/循環在跑，先強制中斷
    - 在新 daemon thread 啟動 _loop_thread
    """
    # 先停止任何現有循環
    _loop_stop_event.set()     # 打斷等待中的 loop
    _new_gen()                 # 讓正在跑的掃描 worker 失效
    time.sleep(0.1)            # 讓 loop 有機會感知到 stop
    _loop_stop_event.clear()   # 重置，給新 loop 用

    body = request.get_json(silent=True) or {}
    params = dict(_DEFAULT_PARAMS)
    for k in ('k_threshold','yoy_min','gross_margin_min','op_margin_min','fin_block','interval_min'):
        if body.get(k) is not None:
            if k == 'fin_block':
                params[k] = bool(body[k])
            elif k == 'k_threshold':
                params[k] = int(body[k])
            elif k == 'interval_min':
                params[k] = max(1, int(body[k]))   # 最少1分鐘
            else:
                params[k] = float(body[k])

    _set_state(running=False, loop_active=True, progress=0,
               error=None, log=[], last_log='啟動中...', params=dict(params))

    threading.Thread(target=_loop_thread, args=(params,), daemon=True).start()

    return jsonify({
        'ok': True,
        'message': (f'循環掃描已啟動 — K≤{params["k_threshold"]} / '
                    f'YOY≥{params["yoy_min"]}% / 每 {params["interval_min"]} 分鐘自動重掃')
    })


@app.route('/api/scan/stop', methods=['POST'])
@admin_required
def api_scan_stop():
    """管理員：中斷循環掃描（含正在執行中的這輪）"""
    _loop_stop_event.set()   # 打斷 loop 的 wait 或標記退出
    _new_gen()               # 讓正在跑的 worker 失效
    _set_state(running=False, loop_active=False, progress=0,
               error=None, next_scan_eta=None)
    _log('⏹ 循環掃描已由管理員中斷')
    return jsonify({'ok': True, 'message': '已中斷循環掃描'})

@app.route('/api/status')
@login_required
def api_status():
    with _state_lock:
        s = dict(_state)
    return jsonify({
        'running':       s['running'],
        'loop_active':   s['loop_active'],
        'next_scan_eta': s['next_scan_eta'],
        'progress':      s['progress'],
        'last_log':      s['last_log'],
        'error':         s['error'],
        'last_scan':     s['last_scan'],
        'stats':         s['stats'],
        'params':        s['params'],
        'log':           s['log'][-10:],
    })

@app.route('/api/results')
@login_required
def api_results():
    with _state_lock:
        s = dict(_state)
    if s['results'] is None:
        return jsonify({'error': '尚未掃描，請管理員執行掃描後再查看'}), 404
    return jsonify({
        'results':     s['results'],
        'etf_results': s['etf_results'],
        'regime':      s['regime'],
        'stats':       s['stats'],
        'last_scan':   s['last_scan'],
        'params':      s['params'],
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)