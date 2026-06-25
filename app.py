# =============================================================================
# Flask Web API for 台股掃描系統 v4.9
# =============================================================================
from flask import Flask, jsonify, render_template, request, Response
import threading, time, json, traceback, queue, sys, io
from contextlib import redirect_stdout
import pytz
from datetime import datetime

app = Flask(__name__)

# ── 全域掃描狀態 ──────────────────────────────────────────────────────────────
_scan_lock   = threading.Lock()
_scan_status = {
    'running':    False,
    'last_scan':  None,
    'results':    None,
    'etf_results': None,
    'regime':     None,
    'fund_df':    None,
    'stats':      {},
    'log':        [],
    'error':      None,
}
_log_queue = queue.Queue()


def _add_log(msg: str):
    ts = datetime.now(pytz.timezone('Asia/Taipei')).strftime('%H:%M:%S')
    entry = f'[{ts}] {msg}'
    _scan_status['log'].append(entry)
    if len(_scan_status['log']) > 200:
        _scan_status['log'] = _scan_status['log'][-200:]
    _log_queue.put(entry)


# ── 動態載入 scanner_core（避免啟動時就執行） ─────────────────────────────────
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
    """把 analyze_stock 的 result 轉成 JSON-safe dict"""
    out = {}
    for k, v in r.items():
        if hasattr(v, 'isoformat'):
            out[k] = v.isoformat()
        elif hasattr(v, 'to_dict'):
            out[k] = str(v)
        elif isinstance(v, float) and (v != v):   # NaN
            out[k] = None
        else:
            try:
                json.dumps(v)
                out[k] = v
            except Exception:
                out[k] = str(v)
    return out


def _run_scan_thread(params: dict):
    sc = get_core()
    tw_tz = pytz.timezone('Asia/Taipei')

    # 覆寫參數
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
        active_etfs = sc.fetch_active_etf_list(force=True)
        etf_results = []
        active_params = sc.get_active_params(datetime.now(tw_tz))
        for tid, name in active_etfs.items():
            r = sc.analyze_etf(tid, name, active_params)
            etf_results.append(_result_to_dict(r))

        _add_log(f'開始個股掃描共 {len(scan_stocks)} 檔...')
        results = []
        total = len(scan_stocks)
        for i, (tid, name) in enumerate(scan_stocks.items()):
            mom_pct     = sc.get_mom_pct(tid, fund_df, mom_dict)
            fin_quality = fin_quality_dict.get(tid)
            r = sc.analyze_stock(tid, name, sc._guess_mtype(tid),
                                 entry_price=None,
                                 active_params=active_params,
                                 mom_pct=mom_pct,
                                 fin_quality=fin_quality)
            results.append(_result_to_dict(r))
            if (i + 1) % 10 == 0 or (i + 1) == total:
                _add_log(f'進度 {i+1}/{total}')

        fin_pass = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('fin_pass') is True)
        fin_fail = sum(1 for c in stocks if fin_quality_dict.get(c, {}).get('fin_pass') is False)

        _scan_status.update({
            'running':     False,
            'last_scan':   datetime.now(tw_tz).isoformat(),
            'results':     results,
            'etf_results': etf_results,
            'regime':      regime,
            'stats': {
                'total_wespai': len(fund_df),
                'yoy_pass':     len(stocks),
                'fin_pass':     fin_pass,
                'fin_fail':     fin_fail,
                'scan_count':   len(scan_stocks),
                'etf_count':    len(etf_results),
                'mom_count':    len(mom_dict),
            },
            'error': None,
        })
        _add_log(f'掃描完成！共 {len(results)} 檔個股 + {len(etf_results)} 檔ETF')

    except Exception as e:
        tb = traceback.format_exc()
        _scan_status['error']   = str(e)
        _scan_status['running'] = False
        _add_log(f'錯誤：{e}')
        app.logger.error(tb)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/scan', methods=['POST'])
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
def api_status():
    s = _scan_status
    return jsonify({
        'running':   s['running'],
        'last_scan': s['last_scan'],
        'stats':     s['stats'],
        'error':     s['error'],
        'log':       s['log'][-50:],
    })


@app.route('/api/results')
def api_results():
    s = _scan_status
    if s['results'] is None:
        return jsonify({'error': '尚未掃描'}), 404
    return jsonify({
        'results':     s['results'],
        'etf_results': s['etf_results'],
        'regime':      s['regime'],
        'stats':       s['stats'],
        'last_scan':   s['last_scan'],
    })


@app.route('/api/log-stream')
def api_log_stream():
    """SSE endpoint for real-time log streaming"""
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
