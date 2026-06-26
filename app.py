# =============================================================================
# 台股掃描系統 v4.9 — Flask API  (密碼系統 v3：管理後台版)
# =============================================================================
import os, json, secrets, threading, time, traceback, queue, sqlite3, hashlib
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
# ★ 管理員密碼（只有你知道，用來進後台）
#   Render 環境變數：ADMIN_PASSWORD=你的後台密碼
#   正式帳號密碼：FULL_PASSWORDS=pw1,pw2（逗號分隔）
# =============================================================================
ADMIN_PASSWORD      = os.environ.get('ADMIN_PASSWORD', 'admin_bob0309')
_DEFAULT_FULL_PWS   = ['stock2024vip', 'bob0309']   # 正式帳號（180天）

def _load_full_passwords():
    env = os.environ.get('FULL_PASSWORDS', '')
    return set(p.strip() for p in env.split(',') if p.strip()) or set(_DEFAULT_FULL_PWS)

# =============================================================================
# SQLite
# =============================================================================
_DB_PATH = Path(os.environ.get('DATA_DIR', '/tmp')) / 'scanner_auth.db'
_db_lock = threading.Lock()

def _get_db():
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _get_db() as db:
        db.executescript("""
        -- 試用帳號池：由管理員建立，每個帳號有獨立到期時間
        CREATE TABLE IF NOT EXISTS trial_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            label       TEXT NOT NULL,          -- 備註（例如「朋友A」）
            password    TEXT NOT NULL UNIQUE,   -- 明文密碼（試用帳號不需高安全性）
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,          -- 到期時間（管理員設定）
            used_at     TEXT,                   -- 第一次登入時間
            is_active   INTEGER DEFAULT 1       -- 0=手動停用
        );
        -- Sessions
        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT PRIMARY KEY,
            pw_type     TEXT NOT NULL,          -- 'trial'|'full'|'admin'
            account_id  INTEGER,                -- trial 帳號的 id（full/admin 為 null）
            expires_at  TEXT NOT NULL
        );
        """)

_init_db()

def _pw_hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

# =============================================================================
# Session 管理
# =============================================================================
def _issue_token(pw_type: str, expires_iso: str, account_id=None) -> str:
    token = secrets.token_urlsafe(32)
    with _db_lock:
        with _get_db() as db:
            db.execute(
                'INSERT INTO sessions(token,pw_type,account_id,expires_at) VALUES(?,?,?,?)',
                (token, pw_type, account_id, expires_iso)
            )
    return token

def _validate_token(token: str) -> dict | None:
    if not token:
        return None
    with _db_lock:
        with _get_db() as db:
            row = db.execute(
                'SELECT pw_type, account_id, expires_at FROM sessions WHERE token=?', (token,)
            ).fetchone()
    if not row:
        return None
    exp = datetime.fromisoformat(row['expires_at'])
    if exp.tzinfo is None:
        exp = TW_TZ.localize(exp)
    if _now_tw() > exp:
        with _db_lock:
            with _get_db() as db:
                db.execute('DELETE FROM sessions WHERE token=?', (token,))
        return None
    return dict(row)

def _get_token_from_request() -> str:
    t = request.cookies.get('auth_token', '')
    if not t:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            t = auth[7:]
    return t

def _get_session() -> dict | None:
    return _validate_token(_get_token_from_request())

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        s = _get_session()
        if not s:
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登入或 session 已過期', 'code': 'UNAUTHORIZED'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        s = _get_session()
        if not s or s['pw_type'] != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'error': '需要管理員權限'}), 403
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

# =============================================================================
# Auth Routes
# =============================================================================
@app.route('/login')
def login_page():
    if _get_session():
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    pw   = data.get('password', '').strip()
    if not pw:
        return jsonify({'error': '請輸入密碼'}), 400

    now = _now_tw()

    # ── 管理員密碼 ──
    if pw == ADMIN_PASSWORD:
        expires = (now + timedelta(hours=8)).isoformat()
        token   = _issue_token('admin', expires)
        exp_str = datetime.fromisoformat(expires).strftime('%Y-%m-%d %H:%M')
        resp = make_response(jsonify({
            'ok': True, 'type': 'admin', 'expires': exp_str,
            'message': f'管理員登入，有效至 {exp_str}',
        }))
        resp.set_cookie('auth_token', token, max_age=8*3600,
                        httponly=True, samesite='Lax', secure=request.is_secure)
        return resp

    # ── 正式密碼 ──
    if pw in _load_full_passwords():
        expires = (now + timedelta(days=180)).isoformat()
        token   = _issue_token('full', expires)
        exp_str = datetime.fromisoformat(expires).strftime('%Y-%m-%d %H:%M')
        resp = make_response(jsonify({
            'ok': True, 'type': 'full', 'expires': exp_str,
            'message': f'正式帳號，有效至 {exp_str}',
        }))
        resp.set_cookie('auth_token', token, max_age=180*86400,
                        httponly=True, samesite='Lax', secure=request.is_secure)
        return resp

    # ── 試用帳號 ──
    with _db_lock:
        with _get_db() as db:
            acc = db.execute(
                'SELECT * FROM trial_accounts WHERE password=? AND is_active=1', (pw,)
            ).fetchone()

    if acc:
        exp = datetime.fromisoformat(acc['expires_at'])
        if exp.tzinfo is None:
            exp = TW_TZ.localize(exp)

        if now > exp:
            time.sleep(1)
            exp_str = exp.strftime('%Y-%m-%d %H:%M')
            return jsonify({'error': f'此試用帳號已於 {exp_str} 到期'}), 403

        # 記錄首次使用時間
        if not acc['used_at']:
            with _db_lock:
                with _get_db() as db:
                    db.execute(
                        'UPDATE trial_accounts SET used_at=? WHERE id=?',
                        (now.isoformat(), acc['id'])
                    )

        expires_iso = acc['expires_at']
        token   = _issue_token('trial', expires_iso, account_id=acc['id'])
        exp_str = exp.strftime('%Y-%m-%d %H:%M')
        days_left = max(1, (exp - now).days + 1)
        resp = make_response(jsonify({
            'ok': True, 'type': 'trial', 'expires': exp_str,
            'message': f'試用帳號「{acc["label"]}」，有效至 {exp_str}',
        }))
        resp.set_cookie('auth_token', token, max_age=days_left*86400,
                        httponly=True, samesite='Lax', secure=request.is_secure)
        return resp

    time.sleep(1)
    return jsonify({'error': '密碼錯誤'}), 401

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
    s = _get_session()
    if not s:
        return jsonify({'logged_in': False}), 401
    exp_str = datetime.fromisoformat(s['expires_at']).strftime('%Y-%m-%d %H:%M')
    type_label = {'trial': '試用帳號', 'full': '正式帳號（180天）', 'admin': '管理員'}
    return jsonify({
        'logged_in': True, 'type': s['pw_type'],
        'expires': exp_str, 'label': type_label.get(s['pw_type'], s['pw_type']),
    })

# =============================================================================
# 管理員後台 API
# =============================================================================
@app.route('/admin')
@admin_required
def admin_page():
    return render_template('admin.html')

@app.route('/api/admin/trials', methods=['GET'])
@admin_required
def admin_list_trials():
    with _db_lock:
        with _get_db() as db:
            rows = db.execute(
                'SELECT * FROM trial_accounts ORDER BY created_at DESC'
            ).fetchall()
    now = _now_tw()
    result = []
    for r in rows:
        exp = datetime.fromisoformat(r['expires_at'])
        if exp.tzinfo is None:
            exp = TW_TZ.localize(exp)
        result.append({
            'id':         r['id'],
            'label':      r['label'],
            'password':   r['password'],
            'created_at': r['created_at'][:16].replace('T', ' '),
            'expires_at': r['expires_at'][:16].replace('T', ' '),
            'used_at':    (r['used_at'] or '')[:16].replace('T', ' '),
            'is_active':  r['is_active'],
            'expired':    now > exp,
        })
    return jsonify(result)

@app.route('/api/admin/trials', methods=['POST'])
@admin_required
def admin_create_trial():
    data  = request.get_json(silent=True) or {}
    label = data.get('label', '').strip()
    pw    = data.get('password', '').strip()
    days  = int(data.get('days', 1))

    if not label or not pw:
        return jsonify({'error': '請填寫備註與密碼'}), 400
    if days < 1 or days > 365:
        return jsonify({'error': '天數需介於 1~365'}), 400

    now     = _now_tw()
    expires = (now + timedelta(days=days)).isoformat()

    try:
        with _db_lock:
            with _get_db() as db:
                db.execute(
                    'INSERT INTO trial_accounts(label,password,created_at,expires_at) VALUES(?,?,?,?)',
                    (label, pw, now.isoformat(), expires)
                )
        return jsonify({'ok': True, 'expires': expires[:16].replace('T', ' ')})
    except sqlite3.IntegrityError:
        return jsonify({'error': '此密碼已存在'}), 409

@app.route('/api/admin/trials/<int:tid>', methods=['DELETE'])
@admin_required
def admin_delete_trial(tid):
    with _db_lock:
        with _get_db() as db:
            db.execute('DELETE FROM trial_accounts WHERE id=?', (tid,))
    return jsonify({'ok': True})

@app.route('/api/admin/trials/<int:tid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_trial(tid):
    with _db_lock:
        with _get_db() as db:
            row = db.execute('SELECT is_active FROM trial_accounts WHERE id=?', (tid,)).fetchone()
            if not row:
                return jsonify({'error': '找不到此帳號'}), 404
            new_val = 0 if row['is_active'] else 1
            db.execute('UPDATE trial_accounts SET is_active=? WHERE id=?', (new_val, tid))
    return jsonify({'ok': True, 'is_active': new_val})

# =============================================================================
# Scanner Routes
# =============================================================================
_scan_lock   = threading.Lock()
_scan_status = {
    'running': False, 'last_scan': None, 'results': None,
    'etf_results': None, 'regime': None, 'stats': {},
    'log': [], 'error': None, 'progress': 0,
}
_log_queue = queue.Queue()

def _add_log(msg: str, progress: int = None):
    ts    = _now_tw().strftime('%H:%M:%S')
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
        if hasattr(v, 'isoformat'):         out[k] = v.isoformat()
        elif hasattr(v, 'to_dict'):         out[k] = str(v)
        elif isinstance(v, float) and v!=v: out[k] = None
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
            _scan_status['running'] = False; return
        _add_log(f'撿股讚抓到 {len(fund_df)} 檔', 18)
        fund_set = sc.build_fundamental_filter(fund_df)
        stocks   = sc.build_yoy_stocks(fund_df, fund_set)
        _add_log(f'YOY≥{sc.YOY_MIN_PCT:.0f}% 達標：{len(stocks)} 檔', 25)
        _add_log('抓取月增率資料...', 30)
        mom_dict = sc.fetch_mom_data(list(stocks.keys()), wespai_html=html, force=True)
        _add_log(f'MoM資料：{len(mom_dict)} 檔', 38)
        _add_log('抓取財務品質資料...', 42)
        df_fin  = sc.fetch_wespai_fin_quality(force=True)
        fin_dict= sc.fetch_fin_quality_batch(stocks, force=True, df_fin_wespai=df_fin)
        fin_set = sc.build_fin_quality_filter(fund_set, fin_dict)
        scan_st = {c:n for c,n in stocks.items() if c in fin_set} \
                  if sc.FIN_BLOCK_ON_FAIL else stocks
        _add_log(f'財務篩選後：{len(scan_st)} 檔', 50)
        _add_log('計算大盤狀態...', 53)
        regime  = sc.calc_market_regime(force=True)
        _add_log('掃描主動式ETF...', 57)
        active_etfs   = sc.fetch_active_etf_list(force=True)
        active_params = sc.get_active_params(datetime.now(TW_TZ))
        etf_results   = [_result_to_dict(sc.analyze_etf(t,n,active_params))
                         for t,n in active_etfs.items()]
        _add_log(f'ETF完成：{len(etf_results)} 檔', 62)
        _add_log(f'個股掃描共 {len(scan_st)} 檔...', 65)
        results = []; total = len(scan_st)
        for i,(tid,name) in enumerate(scan_st.items()):
            r = sc.analyze_stock(tid, name, sc._guess_mtype(tid),
                                 entry_price=None, active_params=active_params,
                                 mom_pct=sc.get_mom_pct(tid,fund_df,mom_dict),
                                 fin_quality=fin_dict.get(tid))
            results.append(_result_to_dict(r))
            pct = 65 + int((i+1)/total*32)
            if (i+1) % 5 == 0 or (i+1) == total:
                _add_log(f'個股進度 {i+1}/{total}', pct)
        fp = sum(1 for c in stocks if fin_dict.get(c,{}).get('fin_pass') is True)
        ff = sum(1 for c in stocks if fin_dict.get(c,{}).get('fin_pass') is False)
        _scan_status.update({
            'running': False, 'last_scan': _now_tw().isoformat(),
            'results': results, 'etf_results': etf_results, 'regime': regime,
            'stats': {'total_wespai':len(fund_df),'yoy_pass':len(stocks),
                      'fin_pass':fp,'fin_fail':ff,'scan_count':len(scan_st),
                      'etf_count':len(etf_results),'mom_count':len(mom_dict)},
            'error': None, 'progress': 100,
        })
        _add_log(f'✅ 掃描完成！{len(results)} 檔個股 + {len(etf_results)} 檔ETF', 100)
    except Exception as e:
        _scan_status['error']   = str(e)
        _scan_status['running'] = False
        _scan_status['progress']= 0
        _add_log(f'❌ 錯誤：{e}')
        app.logger.error(traceback.format_exc())

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
        _scan_status.update({'running':True,'error':None,'log':[],'progress':2})
    threading.Thread(target=_run_scan_thread, args=(params,), daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/status')
@login_required
def api_status():
    s = _scan_status
    return jsonify({'running':s['running'],'last_scan':s['last_scan'],
                    'stats':s['stats'],'error':s['error'],
                    'progress':s['progress'],'log':s['log'][-5:]})

@app.route('/api/results')
@login_required
def api_results():
    s = _scan_status
    if s['results'] is None:
        return jsonify({'error': '尚未掃描'}), 404
    return jsonify({'results':s['results'],'etf_results':s['etf_results'],
                    'regime':s['regime'],'stats':s['stats'],'last_scan':s['last_scan']})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
