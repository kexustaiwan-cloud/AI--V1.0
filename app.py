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
# Scanner Engine — v6.0：固定參數、背景自動定時掃描、結果廣播給所有人
#
#   設計理念：
#   - 不再由「使用者按按鈕」觸發掃描，改成伺服器背景執行緒，用固定參數
#     每隔 SCAN_INTERVAL_SEC（預設900秒=15分鐘）自動掃描一次。
#   - 全部使用者共用同一份「最新掃描結果」（_latest 全域變數），
#     登入後只是「讀」這份結果，不會觸發任何運算，所以可以無限多人
#     同時登入查看，完全不會互相卡住、也不會把外部資料源(yfinance/
#     撿股讚)打爆，因為不管多少人看，對外部API的請求頻率永遠固定
#     是「每15分鐘1次」。
#   - 想調整篩選參數（K值門檻、YOY下限...），改下面 SCAN_PARAMS 或
#     用 Render 環境變數覆蓋，存檔重新部署即可套用到下一次自動掃描，
#     使用者端不能也不需要自己調整。
# =============================================================================
SCAN_INTERVAL_SEC = int(os.environ.get('SCAN_INTERVAL_SEC', '900'))   # 15分鐘
# ★ 修正：單次掃描的最長容許時間（秒）。
#   必須小於 render.yaml 的 gunicorn --timeout（300秒），
#   讓我們自己的看門狗先發現「卡住」並回收，而不是被 gunicorn 從外部強制砍掉 worker
#   （那樣會連同尚未卡住的部分一起中斷，且下一輪還是會用同一顆有問題的股票再卡一次）。
SCAN_TIMEOUT_SEC = int(os.environ.get('SCAN_TIMEOUT_SEC', '240'))
SCAN_PARAMS = {
    'k_threshold':      int(os.environ.get('SCAN_K_THRESHOLD', '30')),
    'yoy_min':           float(os.environ.get('SCAN_YOY_MIN', '15')),
    'gross_margin_min':  float(os.environ.get('SCAN_GROSS_MARGIN_MIN', '25')),
    'op_margin_min':     float(os.environ.get('SCAN_OP_MARGIN_MIN', '15')),
    'fin_block':         os.environ.get('SCAN_FIN_BLOCK', 'true').lower() != 'false',
}

_latest_lock = threading.Lock()
_latest = {
    'running': False, 'last_scan': None, 'next_scan_eta': None,
    'results': None, 'etf_results': None, 'regime': None, 'stats': {},
    'log': [], 'error': None, 'progress': 0,
    'params': dict(SCAN_PARAMS),
}

def _add_log(msg: str, progress: int = None):
    with _latest_lock:
        ts    = _now_tw().strftime('%H:%M:%S')
        _latest['log'].append(f'[{ts}] {msg}')
        if len(_latest['log']) > 200:
            _latest['log'] = _latest['log'][-200:]
        if progress is not None:
            _latest['progress'] = min(progress, 99)

_core = None
_core_lock = threading.Lock()

def get_core():
    global _core
    with _core_lock:
        if _core is None:
            import scanner_core as sc
            _core = sc
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

_cancel_event = threading.Event()   # 設定後，正在跑的掃描會在下一個檢查點中止

_scan_generation = 0
_generation_lock = threading.Lock()

def _new_generation():
    global _scan_generation
    with _generation_lock:
        _scan_generation += 1
        return _scan_generation

def _is_current_generation(gen):
    with _generation_lock:
        return gen == _scan_generation

class ScanCancelled(Exception):
    pass

def _check_cancel(gen=None):
    if _cancel_event.is_set():
        raise ScanCancelled('管理員已中斷掃描')
    if gen is not None and not _is_current_generation(gen):
        raise ScanCancelled('已被新的掃描取代')

def _run_one_scan(gen=None):
    """執行一次完整掃描，更新全域共用結果（所有人看到的都是同一份）
    gen: 這次掃描的世代編號；只有仍是「目前世代」時才允許寫入全域狀態 _latest。
    """
    if gen is None:
        gen = _new_generation()
    _cancel_event.clear()   # 每次開始都先清掉舊的取消旗標
    with _latest_lock:
        _latest['running'] = True
        _latest['error'] = None
        _latest['log'] = []
        _latest['progress'] = 2
    try:
        sc = get_core()
        sc.K_THRESHOLD       = SCAN_PARAMS['k_threshold']
        sc.YOY_MIN_PCT       = SCAN_PARAMS['yoy_min']
        sc.GROSS_MARGIN_MIN  = SCAN_PARAMS['gross_margin_min']
        sc.OP_MARGIN_MIN     = SCAN_PARAMS['op_margin_min']
        sc.FIN_BLOCK_ON_FAIL = SCAN_PARAMS['fin_block']

        _check_cancel(gen)
        _add_log('抓取撿股讚基本面資料...', 8)
        fund_df, html = sc.fetch_wespai_fundamental(force=True)
        if fund_df.empty:
            if _is_current_generation(gen):
                with _latest_lock:
                    _latest['error'] = '無法取得撿股讚資料，下次排程會重試'
                    _latest['running'] = False
            return
        _check_cancel(gen)
        _add_log(f'撿股讚抓到 {len(fund_df)} 檔', 18)
        fund_set = sc.build_fundamental_filter(fund_df)
        stocks   = sc.build_yoy_stocks(fund_df, fund_set)
        _add_log(f'YOY≥{sc.YOY_MIN_PCT:.0f}% 達標：{len(stocks)} 檔', 25)
        _check_cancel(gen)
        _add_log('抓取月增率資料...', 30)
        mom_dict = sc.fetch_mom_data(list(stocks.keys()), wespai_html=html, force=True)
        _add_log(f'MoM資料：{len(mom_dict)} 檔', 38)
        _check_cancel(gen)
        _add_log('抓取財務品質資料...', 42)
        df_fin  = sc.fetch_wespai_fin_quality(force=True)
        fin_dict= sc.fetch_fin_quality_batch(stocks, force=True, df_fin_wespai=df_fin)
        fin_set = sc.build_fin_quality_filter(fund_set, fin_dict)
        scan_st = {c:n for c,n in stocks.items() if c in fin_set} \
                  if sc.FIN_BLOCK_ON_FAIL else stocks
        _add_log(f'財務篩選後：{len(scan_st)} 檔', 50)
        _check_cancel(gen)
        _add_log('計算大盤狀態...', 53)
        regime  = sc.calc_market_regime(force=True)
        _check_cancel(gen)
        _add_log('掃描主動式ETF...', 57)
        active_etfs   = sc.fetch_active_etf_list(force=True)
        active_params = sc.get_active_params(datetime.now(TW_TZ))
        etf_results   = [_result_to_dict(sc.analyze_etf(t,n,active_params))
                         for t,n in active_etfs.items()]
        _add_log(f'ETF完成：{len(etf_results)} 檔', 62)
        _check_cancel(gen)
        _add_log(f'個股掃描共 {len(scan_st)} 檔...', 65)
        results = []; total = len(scan_st) or 1
        for i,(tid,name) in enumerate(scan_st.items()):
            _check_cancel(gen)
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
        now = _now_tw()
        if not _is_current_generation(gen):
            return
        with _latest_lock:
            _latest.update({
                'running': False, 'last_scan': now.isoformat(),
                'next_scan_eta': (now + timedelta(seconds=SCAN_INTERVAL_SEC)).isoformat(),
                'results': results, 'etf_results': etf_results, 'regime': regime,
                'stats': {'total_wespai':len(fund_df),'yoy_pass':len(stocks),
                          'fin_pass':fp,'fin_fail':ff,'scan_count':len(scan_st),
                          'etf_count':len(etf_results),'mom_count':len(mom_dict)},
                'error': None, 'progress': 100,
            })
        _add_log(f'✅ 掃描完成！{len(results)} 檔個股 + {len(etf_results)} 檔ETF', 100)
    except ScanCancelled as e:
        if not _is_current_generation(gen):
            return
        with _latest_lock:
            _latest['running'] = False
            _latest['progress'] = 0
            _latest['error'] = None   # 取消是正常操作，不顯示為錯誤
        _add_log(f'⏹ {e}')
    except Exception as e:
        if not _is_current_generation(gen):
            return
        with _latest_lock:
            _latest['error']   = str(e)
            _latest['running'] = False
            _latest['progress']= 0
        _add_log(f'❌ 錯誤：{e}')
        app.logger.error(traceback.format_exc())

def _scheduler_loop():
    """背景排程：開機先跑一次，之後每 SCAN_INTERVAL_SEC 秒重複執行。

    ★ 核心修正（這是這次「卡在2%超過30分鐘」的真正原因）：
    舊版直接在這個迴圈裡『同步呼叫』_run_one_scan()。如果 _run_one_scan()
    內部任何一步卡住不返回（例如網路請求沒有逾時、對方伺服器不回應），
    這個 while 迴圈就永遠停在 _run_one_scan() 那一行，
    不會執行到下面的 time.sleep()，也就是說――
    自動排程只要卡住『一次』，之後所有排定的掃描就永遠不會再啟動，
    不會自己恢復，只能等使用者發現並手動介入。
    這跟「以前用手動按鈕」最大的差別在於：手動模式下，
    使用者按下按鈕後如果沒反應，會立刻知道要重新整理頁面重試；
    但自動排程沒有人在盯著，卡住可以無聲無息地持續下去。

    修正方式：用一條獨立的執行緒真正去跑 _run_one_scan()，
    主迴圈只 join() 最多 SCAN_TIMEOUT_SEC 秒。時間到了不管那條執行緒有沒有
    真的結束，主迴圈都會記錄逾時、把狀態重置、並繼續進行下一輪排程；
    同時把世代編號往前推進一格，讓萬一之後才回來的舊執行緒自動失效，
    不會再污染新一輪的結果（見 _run_one_scan 內的世代檢查）。
    """
    while True:
        gen = _new_generation()
        worker = threading.Thread(target=_run_one_scan, args=(gen,), daemon=True)
        worker.start()
        worker.join(timeout=SCAN_TIMEOUT_SEC)

        if worker.is_alive():
            # 逾時：這次掃描被視為卡住，放棄等待，讓舊執行緒變成孤兒（它之後
            # 就算真的返回，也會因為世代編號過期而被 _run_one_scan 自動忽略）。
            app.logger.error(f'掃描逾時（超過 {SCAN_TIMEOUT_SEC} 秒未完成），已放棄本輪並繼續排程')
            with _latest_lock:
                _latest['running']  = False
                _latest['progress'] = 0
                _latest['error']    = f'掃描逾時（超過 {SCAN_TIMEOUT_SEC} 秒），已自動略過本輪，下一輪會重試'
            _add_log(f'⏱️ 掃描逾時（>{SCAN_TIMEOUT_SEC}秒），已略過本輪')

        with _latest_lock:
            eta = (_now_tw() + timedelta(seconds=SCAN_INTERVAL_SEC)).isoformat()
            _latest['next_scan_eta'] = eta
        time.sleep(SCAN_INTERVAL_SEC)

_scheduler_started = False
_scheduler_lock = threading.Lock()

def _ensure_scheduler_started():
    global _scheduler_started
    with _scheduler_lock:
        if not _scheduler_started:
            _scheduler_started = True
            threading.Thread(target=_scheduler_loop, daemon=True).start()

# Render 用 gunicorn 啟動，匯入 app.py 模組時就把背景排程啟動起來
# （搭配 render.yaml 固定 1 個 worker，確保只會啟動一份排程，不會重複掃描）
_ensure_scheduler_started()

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/api/scan/manual', methods=['POST'])
@login_required
def api_scan_manual():
    """管理員：套用新篩選條件並立刻掃描；若掃描正在執行中，先中斷再重啟"""
    s = _get_session()
    if not s or s['pw_type'] != 'admin':
        return jsonify({'error': '需要管理員權限'}), 403

    body = request.get_json(silent=True) or {}
    if body.get('k_threshold')      is not None: SCAN_PARAMS['k_threshold']      = int(body['k_threshold'])
    if body.get('yoy_min')          is not None: SCAN_PARAMS['yoy_min']          = float(body['yoy_min'])
    if body.get('gross_margin_min') is not None: SCAN_PARAMS['gross_margin_min'] = float(body['gross_margin_min'])
    if body.get('op_margin_min')    is not None: SCAN_PARAMS['op_margin_min']    = float(body['op_margin_min'])
    if body.get('fin_block')        is not None: SCAN_PARAMS['fin_block']        = bool(body['fin_block'])
    with _latest_lock:
        _latest['params'] = dict(SCAN_PARAMS)
        is_running = _latest['running']

    if is_running:
        _cancel_event.set()
        _add_log('⏹ 管理員中斷目前掃描，套用新條件後重新啟動...')

    gen = _new_generation()
    threading.Thread(target=_run_one_scan, args=(gen,), daemon=True).start()
    action = '已中斷舊掃描，' if is_running else ''
    return jsonify({'ok': True,
                    'message': f'{action}套用新條件並重新掃描 — K≤{SCAN_PARAMS["k_threshold"]} / YOY≥{SCAN_PARAMS["yoy_min"]}%'})

@app.route('/api/status')
@login_required
def api_status():
    with _latest_lock:
        s = dict(_latest)
    return jsonify({
        'running': s['running'], 'last_scan': s['last_scan'],
        'next_scan_eta': s['next_scan_eta'],
        'scan_interval_sec': SCAN_INTERVAL_SEC,
        'stats': s['stats'], 'error': s['error'],
        'progress': s['progress'], 'log': s['log'][-5:],
        'params': s['params'],
    })

@app.route('/api/results')
@login_required
def api_results():
    with _latest_lock:
        s = dict(_latest)
    if s['results'] is None:
        return jsonify({'error': '系統正在準備第一次掃描，請稍候片刻'}), 404
    return jsonify({'results':s['results'],'etf_results':s['etf_results'],
                    'regime':s['regime'],'stats':s['stats'],
                    'last_scan':s['last_scan'], 'next_scan_eta':s['next_scan_eta'],
                    'params': s['params']})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
