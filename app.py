# =============================================================================
# Flask Web API for 台股掃描系統 v4.9  ── 含密碼驗證系統
# =============================================================================
import os, json, hashlib, secrets, threading, time, traceback, queue
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, jsonify, render_template, request,
                   Response, make_response, redirect, url_for)
import pytz

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# =============================================================================
# ★ 密碼設定區  ── 請在此修改你的密碼
#    也可以用環境變數覆蓋（部署到 Render 時建議用環境變數）
#    環境變數：TRIAL_PASSWORDS  和  FULL_PASSWORDS
#    格式：逗號分隔，例如  TRIAL_PASSWORDS=trial2024,demo123
# =============================================================================
_DEFAULT_TRIAL_PASSWORDS = ['trial2024', 'demo888']   # ← 試用密碼（1天）
_DEFAULT_FULL_PASSWORDS  = ['stock2024vip', 'bob0309']  # ← 正式密碼（180天）

def _load_passwords():
    trial_env = os.environ.get('TRIAL_PASSWORDS', '')
    full_env  = os.environ.get('FULL_PASSWORDS', '')
    trial = [p.strip() for p in trial_env.split(',') if p.strip()] or _DEFAULT_TRIAL_PASSWORDS
    full  = [p.strip() for p in full_env.split(',')  if p.strip()] or _DEFAULT_FULL_PASSWORDS
    return set(trial), set(full)

# =============================================================================
# Session / Token 管理（存在記憶體，服務重啟會清掉，建議改 SQLite 做持久化）
# =============================================================================
_sessions: dict[str, dict] = {}   # token -> {expires, type}
_sessions_lock = threading.Lock()

TW_TZ = pytz.timezone('Asia/Taipei')


def _now_tw() -> datetime:
    return datetime.now(TW_TZ)


def _make_token() -> str:
    return secrets.token_urlsafe(32)


def _issue_token(pw_type: str) -> str:
    """簽發新 token，trial=1天，full=180天"""
    token = _make_token()
    days  = 1 if pw_type == 'trial' else 180
    expires = _now_tw() + timedelta(days=days)
    with _sessions_lock:
        _sessions[token] = {'expires': expires.isoformat(), 'type': pw_type}
    return token


def _validate_token(token: str) -> dict | None:
    """驗證 token；有效回傳 session dict，否則回傳 None"""
    if not token:
        return None
    with _sessions_lock:
        s = _sessions.get(token)
    if not s:
        return None
    expires = datetime.fromisoformat(s['expires'])
    if expires.tzinfo is None:
        expires = TW_TZ.localize(expires)
    if _now_tw() > expires:
        with _sessions_lock:
            _sessions.pop(token, None)
        return None
    return s


def _get_token_from_request() -> str:
    """從 Cookie 或 Authorization header 取 token"""
    token = request.cookies.get('auth_token', '')
    if not token:
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]
    return token


def login_required(f):
    """Decorator：需要有效 token，否則 API 回 401，網頁導向登入"""
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
# ── Auth Routes ───────────────────────────────────────────────────────────────
# =============================================================================
@app.route('/login')
def login_page():
    token = _get_token_from_request()
    if _validate_token(token):
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
        pw_type = 'full'
    elif pw in trial_pws:
        pw_type = 'trial'
    else:
        time.sleep(1)   # 防暴力破解
        return jsonify({'error': '密碼錯誤'}), 401

    token = _issue_token(pw_type)
    s = _sessions[token]
    days  = 1 if pw_type == 'trial' else 180
    exp_str = datetime.fromisoformat(s['expires']).strftime('%Y-%m-%d %H:%M')

    resp = make_response(jsonify({
        'ok':      True,
        'type':    pw_type,
        'expires': exp_str,
        'message': f'{"試用帳號" if pw_type == "trial" else "正式帳號"}，有效至 {exp_str}',
    }))
    resp.set_cookie(
        'auth_token', token,
        max_age=days * 86400,
        httponly=True,
        samesite='Lax',
        secure=request.is_secure,   # HTTPS 時才設 secure
    )
    return resp


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    token = _get_token_from_request()
    with _sessions_lock:
        _sessions.pop(token, None)
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
# ── 掃描狀態 ──────────────────────────────────────────────────────────────────
# =============================================================================
_scan_lock   = threading.Lock()
_scan_status = {
    'running': False, 'last_scan': None, 'results': None,
    'etf_results': None, 'regime': None, 'stats': {}, 'log': [], 'error': None,
}
_log_queue = queue.Queue()


def _add_log(msg: str):
    ts = _now_tw().strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    _scan_status['log'].append(entry)
    if len(_scan_status['log']) > 200:
        _scan_status['log'] = _scan_status['log'][-200:]
    _log_queue.put(entry)


_core = None
_core_lock = threading.Lock()

def get_core():
    global _core
    with _core_lock:
        if _core is None:
            _add_log('載入核心模組...')
            import scanner_core as sc
            _core = sc
            _add_log('核心模組載入完成')
        return _core


def _result_to_dict(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if hasattr(v, 'isoformat'):
            out[k] = v.isoformat()
        elif hasattr(v, 'to_dict'):
            out[k] = str(v)
        elif isinstance(v, float) and v != v:
            out[k] = None
        else:
            try:
                json.dumps(v); out[k] = v
            except Exception:
                out[k] = str(v)
    return out


def _run_scan_thread(params: dict):
    sc = get_core()
    sc.K_THRESHOLD       = params.get('k_threshold', 30)
    sc.YOY_MIN_PCT       = params.get('yoy_min', 15.0)
    sc.GROSS_MARGIN_MIN  = params.get('gross_margin_min', 25.0)
    sc.OP_MARGIN_MIN     = params.get('op_margin_min', 15.0)
    sc.FIN_BLOCK_ON_FAIL = params.get('fin_block', True)

    try:
        _add_log('抓取撿股讚基本面資料...')
        fund_df, html = sc.fetch_wespai_fundamental(force=True)
        if fund_df.empty:
            _scan_status['error'] = '無法取得撿股讚資料，請稍後重試'
            _scan_status['running'] = False
            return

        _add_log(f'撿股讚抓到 {len(fund_df)} 檔')
        fund_set = sc.build_fundamental_filter(fund_df)
        stocks   = sc.build_yoy_stocks(fund_df, fund_set)
        codes    = list(stocks.keys())
        _add_log(f'YOY≥{sc.YOY_MIN_PCT:.0f}% 達標：{len(stocks)} 檔')

        _add_log('抓取月增率資料...')
        mom_dict = sc.fetch_mom_data(codes, wespai_html=html, force=True)
        _add_log(f'MoM資料：{len(mom_dict)} 檔')

        _add_log('抓取財務品質資料 (撿股讚75684/75686)...')
        df_fin_wespai    = sc.fetch_wespai_fin_quality(force=True)
        fin_quality_dict = sc.fetch_fin_quality_batch(stocks, force=True,
                                                      df_fin_wespai=df_fin_wespai)
        fund_set_fin = sc.build_fin_quality_filter(fund_set, fin_quality_dict)
        scan_stocks  = {c: n for c, n in stocks.items() if c in fund_set_fin} \
                       if sc.FIN_BLOCK_ON_FAIL else stocks
        _add_log(f'財務篩選後：{len(scan_stocks)} 檔')

        _add_log('計算大盤狀態...')
        regime = sc.calc_market_regime(force=True)

        _add_log('掃描主動式ETF...')
        active_etfs   = sc.fetch_active_etf_list(force=True)
        active_params = sc.get_active_params(datetime.now(TW_TZ))
        etf_results   = []
        for tid, name in active_etfs.items():
            r = sc.analyze_etf(tid, name, active_params)
            etf_results.append(_result_to_dict(r))

        _add_log(f'開始個股掃描共 {len(scan_stocks)} 檔...')
        results = []
        total   = len(scan_stocks)
        for i, (tid, name) in enumerate(scan_stocks.items()):
            mom_pct     = sc.get_mom_pct(tid, fund_df, mom_dict)
            fin_quality = fin_quality_dict.get(tid)
            r = sc.analyze_stock(tid, name, sc._guess_mtype(tid),
                                 entry_price=None, active_params=active_params,
                                 mom_pct=mom_pct, fin_quality=fin_quality)
            results.append(_result_to_dict(r))
            if (i + 1) % 10 == 0 or (i + 1) == total:
                _add_log(f'進度 {i+1}/{total}')

        fin_pass = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('fin_pass') is True)
        fin_fail = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('fin_pass') is False)

        _scan_status.update({
            'running': False, 'last_scan': _now_tw().isoformat(),
            'results': results, 'etf_results': etf_results, 'regime': regime,
            'stats': {
                'total_wespai': len(fund_df), 'yoy_pass': len(stocks),
                'fin_pass': fin_pass, 'fin_fail': fin_fail,
                'scan_count': len(scan_stocks), 'etf_count': len(etf_results),
                'mom_count': len(mom_dict),
            },
            'error': None,
        })
        _add_log(f'掃描完成！共 {len(results)} 檔個股 + {len(etf_results)} 檔ETF')

    except Exception as e:
        _scan_status['error']   = str(e)
        _scan_status['running'] = False
        _add_log(f'錯誤：{e}')
        app.logger.error(traceback.format_exc())


# =============================================================================
# ── Scanner Routes（全部需要登入）─────────────────────────────────────────────
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
        _scan_status['running'] = True
        _scan_status['error']   = None
        _scan_status['log']     = []
    t = threading.Thread(target=_run_scan_thread, args=(params,), daemon=True)
    t.start()
    return jsonify({'status': 'started'})


@app.route('/api/status')
@login_required
def api_status():
    s = _scan_status
    return jsonify({
        'running': s['running'], 'last_scan': s['last_scan'],
        'stats': s['stats'], 'error': s['error'], 'log': s['log'][-50:],
    })


@app.route('/api/results')
@login_required
def api_results():
    s = _scan_status
    if s['results'] is None:
        return jsonify({'error': '尚未掃描'}), 404
    return jsonify({
        'results': s['results'], 'etf_results': s['etf_results'],
        'regime': s['regime'], 'stats': s['stats'], 'last_scan': s['last_scan'],
    })


@app.route('/api/log-stream')
@login_required
def api_log_stream():
    def generate():
        while True:
            try:
                msg = _log_queue.get(timeout=30)
                yield f'data: {json.dumps(msg)}\n\n'
            except queue.Empty:
                yield 'data: ping\n\n'
            if not _scan_status['running']:
                yield f'data: {json.dumps("__done__")}\n\n'
                break
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
