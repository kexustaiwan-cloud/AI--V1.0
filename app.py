# =============================================================================
# Flask Web API for 台股掃描系統 v4.9  ── 含密碼驗證系統 v2
# =============================================================================
import os, json, secrets, threading, time, traceback, queue, sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, render_template, request,
                   Response, make_response, redirect, url_for)
import pytz

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

TW_TZ = pytz.timezone('Asia/Taipei')
def _now_tw() -> datetime:
    return datetime.now(TW_TZ)

# =============================================================================
# ★ 密碼設定區
#   Render 環境變數：TRIAL_PASSWORDS=pw1,pw2  FULL_PASSWORDS=pw1,pw2
# =============================================================================
_DEFAULT_TRIAL_PASSWORDS = ['trial2024', 'demo888']   # 試用密碼（只能用1天，用完不能再用）
_DEFAULT_FULL_PASSWORDS  = ['stock2024vip', 'bob0309'] # 正式密碼（180天）

def _load_passwords():
    trial = [p.strip() for p in os.environ.get('TRIAL_PASSWORDS','').split(',') if p.strip()] \
            or _DEFAULT_TRIAL_PASSWORDS
    full  = [p.strip() for p in os.environ.get('FULL_PASSWORDS','').split(',')  if p.strip()] \
            or _DEFAULT_FULL_PASSWORDS
    return set(trial), set(full)

# =============================================================================
# SQLite — 持久化儲存：試用密碼首次使用時間 + session tokens
# 資料庫放在 /data（Render Disk）或本機 /tmp
# =============================================================================
_DB_PATH = Path(os.environ.get('DATA_DIR', '/tmp')) / 'scanner_auth.db'

def _get_db():
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS trial_first_use (
            password_hash TEXT PRIMARY KEY,
            first_used_at TEXT NOT NULL   -- ISO8601 台北時間
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            pw_type TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """)

_init_db()
_db_lock = threading.Lock()

def _pw_hash(pw: str) -> str:
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()

# ── 試用密碼：記錄首次使用，到期後永久封鎖 ───────────────────────────────────
def _trial_check_and_register(pw: str) -> tuple[bool, str]:
    """
    回傳 (allowed: bool, expires_iso: str)
    - 第一次用：寫入首次使用時間，有效期 = 首次使用 + 24小時
    - 之後再用：檢查是否還在24小時內
    - 超過24小時：永久拒絕，回傳 allowed=False
    """
    h = _pw_hash(pw)
    now = _now_tw()
    with _db_lock:
        with _get_db() as db:
            row = db.execute(
                'SELECT first_used_at FROM trial_first_use WHERE password_hash=?', (h,)
            ).fetchone()

            if row is None:
                # 第一次使用，寫入
                db.execute(
                    'INSERT INTO trial_first_use(password_hash, first_used_at) VALUES(?,?)',
                    (h, now.isoformat())
                )
                expires = now + timedelta(hours=24)
                return True, expires.isoformat()
            else:
                first_used = datetime.fromisoformat(row['first_used_at'])
                if first_used.tzinfo is None:
                    first_used = TW_TZ.localize(first_used)
                expires = first_used + timedelta(hours=24)
                if now <= expires:
                    return True, expires.isoformat()
                else:
                    return False, expires.isoformat()   # 已過期，永久拒絕

# ── Session token 管理 ────────────────────────────────────────────────────────
def _issue_token(pw_type: str, expires_iso: str) -> str:
    token = secrets.token_urlsafe(32)
    with _db_lock:
        with _get_db() as db:
            db.execute(
                'INSERT INTO sessions(token, pw_type, expires_at) VALUES(?,?,?)',
                (token, pw_type, expires_iso)
            )
    return token

def _validate_token(token: str) -> dict | None:
    if not token:
        return None
    with _db_lock:
        with _get_db() as db:
            row = db.execute(
                'SELECT pw_type, expires_at FROM sessions WHERE token=?', (token,)
            ).fetchone()
    if not row:
        return None
    expires = datetime.fromisoformat(row['expires_at'])
    if expires.tzinfo is None:
        expires = TW_TZ.localize(expires)
    if _now_tw() > expires:
        with _db_lock:
            with _get_db() as db:
                db.execute('DELETE FROM sessions WHERE token=?', (token,))
        return None
    return {'type': row['pw_type'], 'expires': row['expires_at']}

def _get_token_from_request() -> str:
    token = request.cookies.get('auth_token', '')
    if not token:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
    return token

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _get_token_from_request()
        session = _validate_token(token)
        if not session:
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登入或 session 已過期', 'code': 'UNAUTHORIZED'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

# =============================================================================
# ── Auth Routes
# =============================================================================
@app.route('/login')
def login_page():
    if _validate_token(_get_token_from_request()):
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    pw = data.get('password', '').strip()
    if not pw:
        return jsonify({'error': '請輸入密碼'}), 400

    trial_pws, full_pws = _load_passwords()

    if pw in full_pws:
        pw_type    = 'full'
        expires    = (_now_tw() + timedelta(days=180)).isoformat()
        allowed    = True
        deny_msg   = ''
    elif pw in trial_pws:
        pw_type    = 'trial'
        allowed, expires = _trial_check_and_register(pw)
        if not allowed:
            exp_str = datetime.fromisoformat(expires).strftime('%Y-%m-%d %H:%M')
            time.sleep(1)
            return jsonify({'error': f'試用密碼已於 {exp_str} 過期，無法再次使用'}), 403
    else:
        time.sleep(1)
        return jsonify({'error': '密碼錯誤'}), 401

    token   = _issue_token(pw_type, expires)
    exp_str = datetime.fromisoformat(expires).strftime('%Y-%m-%d %H:%M')
    days    = 1 if pw_type == 'trial' else 180

    resp = make_response(jsonify({
        'ok':      True,
        'type':    pw_type,
        'expires': exp_str,
        'message': f'{"試用帳號" if pw_type == "trial" else "正式帳號"}，有效至 {exp_str}',
    }))
    resp.set_cookie('auth_token', token,
                    max_age=days * 86400,
                    httponly=True, samesite='Lax',
                    secure=request.is_secure)
    return resp

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    token = _get_token_from_request()
    with _db_lock:
        with _get_db() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (token,))
    resp = make_response(jsonify({'ok': True}))
    resp.delete_cookie('auth_token')
    return resp

@app.route('/api/auth/me')
def api_me():
    token = _get_token_from_request()
    s = _validate_token(token)
    if not s:
        return jsonify({'logged_in': False}), 401
    exp_str = datetime.fromisoformat(s['expires']).strftime('%Y-%m-%d %H:%M')
    return jsonify({
        'logged_in': True,
        'type':      s['type'],
        'expires':   exp_str,
        'label':     '試用帳號（1天）' if s['type'] == 'trial' else '正式帳號（180天）',
    })

# =============================================================================
# ── 掃描狀態
# =============================================================================
_scan_lock   = threading.Lock()
_scan_status = {
    'running': False, 'last_scan': None, 'results': None,
    'etf_results': None, 'regime': None, 'stats': {}, 'log': [], 'error': None,
    'progress': 0,   # 0~100
}
_log_queue = queue.Queue()

def _add_log(msg: str, progress: int = None):
    ts = _now_tw().strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    _scan_status['log'].append(entry)
    if len(_scan_status['log']) > 200:
        _scan_status['log'] = _scan_status['log'][-200:]
    if progress is not None:
        _scan_status['progress'] = min(progress, 99)
    _log_queue.put(entry)

_core = None
_core_lock = threading.Lock()

def get_core():
    global _core
    with _core_lock:
        if _core is None:
            _add_log('載入核心模組...', 2)
            import scanner_core as sc
            _core = sc
            _add_log('核心模組載入完成', 5)
        return _core

def _result_to_dict(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if hasattr(v, 'isoformat'):   out[k] = v.isoformat()
        elif hasattr(v, 'to_dict'):   out[k] = str(v)
        elif isinstance(v, float) and v != v: out[k] = None
        else:
            try:    json.dumps(v); out[k] = v
            except: out[k] = str(v)
    return out

def _run_scan_thread(params: dict):
    sc = get_core()
    sc.K_THRESHOLD       = params.get('k_threshold', 30)
    sc.YOY_MIN_PCT       = params.get('yoy_min', 15.0)
    sc.GROSS_MARGIN_MIN  = params.get('gross_margin_min', 25.0)
    sc.OP_MARGIN_MIN     = params.get('op_margin_min', 15.0)
    sc.FIN_BLOCK_ON_FAIL = params.get('fin_block', True)

    try:
        _add_log('抓取撿股讚基本面資料...', 8)
        fund_df, html = sc.fetch_wespai_fundamental(force=True)
        if fund_df.empty:
            _scan_status['error'] = '無法取得撿股讚資料，請稍後重試'
            _scan_status['running'] = False
            return

        _add_log(f'撿股讚抓到 {len(fund_df)} 檔', 18)
        fund_set = sc.build_fundamental_filter(fund_df)
        stocks   = sc.build_yoy_stocks(fund_df, fund_set)
        codes    = list(stocks.keys())
        _add_log(f'YOY≥{sc.YOY_MIN_PCT:.0f}% 達標：{len(stocks)} 檔', 25)

        _add_log('抓取月增率資料...', 30)
        mom_dict = sc.fetch_mom_data(codes, wespai_html=html, force=True)
        _add_log(f'MoM資料：{len(mom_dict)} 檔', 38)

        _add_log('抓取財務品質資料...', 42)
        df_fin_wespai    = sc.fetch_wespai_fin_quality(force=True)
        fin_quality_dict = sc.fetch_fin_quality_batch(stocks, force=True,
                                                      df_fin_wespai=df_fin_wespai)
        fund_set_fin = sc.build_fin_quality_filter(fund_set, fin_quality_dict)
        scan_stocks  = {c: n for c, n in stocks.items() if c in fund_set_fin} \
                       if sc.FIN_BLOCK_ON_FAIL else stocks
        _add_log(f'財務篩選後：{len(scan_stocks)} 檔', 50)

        _add_log('計算大盤狀態...', 53)
        regime = sc.calc_market_regime(force=True)

        _add_log('掃描主動式ETF...', 57)
        active_etfs   = sc.fetch_active_etf_list(force=True)
        active_params = sc.get_active_params(datetime.now(TW_TZ))
        etf_results   = []
        for tid, name in active_etfs.items():
            r = sc.analyze_etf(tid, name, active_params)
            etf_results.append(_result_to_dict(r))
        _add_log(f'ETF掃描完成：{len(etf_results)} 檔', 62)

        _add_log(f'開始個股掃描共 {len(scan_stocks)} 檔...', 65)
        results = []
        total   = len(scan_stocks)
        for i, (tid, name) in enumerate(scan_stocks.items()):
            mom_pct     = sc.get_mom_pct(tid, fund_df, mom_dict)
            fin_quality = fin_quality_dict.get(tid)
            r = sc.analyze_stock(tid, name, sc._guess_mtype(tid),
                                 entry_price=None, active_params=active_params,
                                 mom_pct=mom_pct, fin_quality=fin_quality)
            results.append(_result_to_dict(r))
            pct = 65 + int((i + 1) / total * 32)
            if (i + 1) % 5 == 0 or (i + 1) == total:
                _add_log(f'個股進度 {i+1}/{total}', pct)

        fin_pass = sum(1 for c in stocks if fin_quality_dict.get(c,{}).get('fin_pass') is True)
        fin_fail = sum(1 for c in stocks if fin_quality_dict.get(c,{}).get('fin_pass') is False)

        _scan_status.update({
            'running': False, 'last_scan': _now_tw().isoformat(),
            'results': results, 'etf_results': etf_results, 'regime': regime,
            'stats': {
                'total_wespai': len(fund_df), 'yoy_pass': len(stocks),
                'fin_pass': fin_pass, 'fin_fail': fin_fail,
                'scan_count': len(scan_stocks), 'etf_count': len(etf_results),
                'mom_count': len(mom_dict),
            },
            'error': None, 'progress': 100,
        })
        _add_log(f'✅ 掃描完成！{len(results)} 檔個股 + {len(etf_results)} 檔ETF', 100)

    except Exception as e:
        _scan_status['error']    = str(e)
        _scan_status['running']  = False
        _scan_status['progress'] = 0
        _add_log(f'❌ 錯誤：{e}')
        app.logger.error(traceback.format_exc())

# =============================================================================
# ── Scanner Routes
# =============================================================================
@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/scan', methods=['POST'])
@login_required
def api_scan():
    with _scan_lock:
        if _scan_status['running']:
            return jsonify({'error': '掃描中，請稍候'}), 429
        params = request.get_json(silent=True) or {}
        _scan_status['running']  = True
        _scan_status['error']    = None
        _scan_status['log']      = []
        _scan_status['progress'] = 2
    t = threading.Thread(target=_run_scan_thread, args=(params,), daemon=True)
    t.start()
    return jsonify({'status': 'started'})

@app.route('/api/status')
@login_required
def api_status():
    s = _scan_status
    return jsonify({
        'running':  s['running'],  'last_scan': s['last_scan'],
        'stats':    s['stats'],    'error':     s['error'],
        'progress': s['progress'], 'log':       s['log'][-5:],
    })

@app.route('/api/results')
@login_required
def api_results():
    s = _scan_status
    if s['results'] is None:
        return jsonify({'error': '尚未掃描'}), 404
    return jsonify({
        'results': s['results'], 'etf_results': s['etf_results'],
        'regime':  s['regime'],  'stats':        s['stats'],
        'last_scan': s['last_scan'],
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
