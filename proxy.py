#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
茫茫股海 — 本機即時報價代理（Gemini 架構 + 鉅亨目標價回退）

使用方式：
  1) 將此 proxy.py 與 stock_app.html 放在同一個資料夾
  2) 執行： python proxy.py        （Windows 若不行就用 python3 proxy.py）
  3) 瀏覽器打開： http://localhost:8787

目標價來源：先試 MoneyDJ（依代號、Big5）；該頁需登入時自動回退鉅亨（公開）。
"""
import sys, os, json, re, html as htmllib, urllib.parse, urllib.request, urllib.error, http.cookiejar
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading, time

# ---- 線上人數追蹤（記憶體內；服務休眠歸零屬正常） ----
ONLINE = {}
ONLINE_LOCK = threading.Lock()
ONLINE_TTL = 35   # 秒；超過此時間沒心跳即視為離線

def beat(cid):
    now = time.time()
    with ONLINE_LOCK:
        if cid:
            ONLINE[cid] = now
        for k in [k for k, v in ONLINE.items() if now - v > ONLINE_TTL]:
            del ONLINE[k]
        return len(ONLINE)

PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8787))
HERE = os.path.dirname(os.path.abspath(__file__))

# 部署成公開代理時，/fetch 只允許這些財經網域，避免被當成開放跳板濫用
ALLOW_HOSTS = ("twse.com.tw", "tpex.org.tw", "finance.yahoo.com", "yahoo.com",
               "finmindtrade.com", "cnyes.com")

def _host_allowed(target):
    try:
        h = (urllib.parse.urlparse(target).hostname or "").lower()
    except Exception:
        return False
    return any(h == d or h.endswith("." + d) for d in ALLOW_HOSTS)

# 偽裝成標準瀏覽器標頭，避免被阻擋
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

def find_html():
    """找出要服務的 App 檔案：優先 stock_app.html，否則資料夾內第一個 .html"""
    pref = os.path.join(HERE, "stock_app.html")
    if os.path.exists(pref):
        return pref
    for fn in sorted(os.listdir(HERE)):
        if fn.lower().endswith(".html"):
            return os.path.join(HERE, fn)
    return None

def _clean(x):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", x)).strip()

# ===== 目標價來源：Yahoo Finance 官方 quoteSummary API（分析師目標價共識）=====
def _yahoo_session():
    """建立帶 cookie 的 opener 並取得 crumb（Yahoo 近年 quoteSummary 多需 crumb 驗證）。"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb = ""
    try:
        opener.open(urllib.request.Request("https://fc.yahoo.com", headers=HDRS), timeout=10)
    except Exception:
        pass
    for u in ("https://query1.finance.yahoo.com/v1/test/getcrumb",
              "https://query2.finance.yahoo.com/v1/test/getcrumb"):
        try:
            r = opener.open(urllib.request.Request(u, headers=HDRS), timeout=10)
            crumb = r.read().decode("utf-8", "replace").strip()
            if crumb and "<" not in crumb:
                break
        except Exception:
            continue
    return opener, crumb

_REC_MAP = {"strong_buy": "強力買進", "buy": "買進", "hold": "中立",
            "sell": "賣出", "underperform": "劣於大盤", "none": "--"}

def _scrape_yahoo(code):
    """打 Yahoo Finance quoteSummary 的 financialData 模組，取分析師均標/最高/最低。
    依序試 .TW（上市）、.TWO（上櫃）。回傳均標、最高、最低三筆。"""
    code = str(code).strip()
    for suffix in (".TW", ".TWO"):
        sym = code + suffix
        opener, crumb = _yahoo_session()
        data = None
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            url = ("https://" + host + "/v10/finance/quoteSummary/"
                   + urllib.parse.quote(sym) + "?modules=financialData")
            if crumb:
                url += "&crumb=" + urllib.parse.quote(crumb)
            try:
                req = urllib.request.Request(url, headers=HDRS)
                with opener.open(req, timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                print(f"[-] Yahoo API {sym}@{host} 失敗: {e}")
                continue
        if data is None:
            continue
        try:
            fd = data["quoteSummary"]["result"][0]["financialData"]
        except Exception:
            continue

        def val(k):
            v = fd.get(k)
            return v.get("raw") if isinstance(v, dict) else v

        mean, high, low = val("targetMeanPrice"), val("targetHighPrice"), val("targetLowPrice")
        n = val("numberOfAnalystOpinions")
        rtxt = _REC_MAP.get(str(fd.get("recommendationKey", "")).lower(), "--")
        today = datetime.now().strftime("%m/%d")
        nlabel = f"（{int(n)} 位分析師）" if n else ""
        out = []
        if mean and float(mean) > 0:
            out.append({"date": today, "firm": "分析師均標" + nlabel, "rating": rtxt, "price": float(mean)})
        if high and float(high) > 0:
            out.append({"date": today, "firm": "最高目標", "rating": rtxt, "price": float(high)})
        if low and float(low) > 0:
            out.append({"date": today, "firm": "最低目標", "rating": rtxt, "price": float(low)})
        if out:
            return out
    return []

def _scrape_cnyes(code):
    """鉅亨（湯森路透）外資評等表，公開免登入。限制：市場整表＋分頁，只涵蓋近期被評等個股。"""
    code = str(code).strip()
    urls = [
        "https://www.cnyes.com/archive/twstock/board/ratediff.aspx?gt=qfii&gp=rate",
        "https://www.cnyes.com/twstock/board/ratediff.aspx?gt=qfii&gp=rate",
    ]
    out, seen = [], set()
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=HDRS)
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", "replace")
        except Exception:
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S | re.I):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            if len(cells) < 9:
                continue
            c = [_clean(x) for x in cells]
            date, codecell, firm = c[0], c[1], c[2]
            newrating, newtarget, price = c[5], c[7], c[8]
            m = re.match(r"(\d{4,6})", codecell)
            if not m or m.group(1) != code:
                continue
            try:
                tgt = float(newtarget.replace(",", ""))
            except Exception:
                continue
            if tgt <= 0:
                continue
            key = date + firm + newtarget
            if key in seen:
                continue
            seen.add(key)
            d = re.sub(r"\D", "", date)
            mmdd = (d[4:6] + "/" + d[6:8]) if len(d) == 8 else date
            out.append({"date": mmdd, "firm": firm or "外資", "rating": newrating, "price": tgt})
        if out:
            break
    out.sort(key=lambda e: e["date"], reverse=True)
    return out[:8]

def _diag_try(url, opener=None, timeout=12):
    o = opener or urllib.request.build_opener()
    try:
        req = urllib.request.Request(url, headers=HDRS)
        with o.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return {"status": getattr(r, "status", 200), "len": len(body), "body": body}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "HTTPError"}
    except Exception as e:
        return {"error": str(e)}

def diag(code):
    """一次測試所有候選目標價來源，回報真實狀態與資料樣本（給除錯用）。"""
    code = str(code).strip()
    res = {}
    opener, crumb = _yahoo_session()
    res["crumb"] = {"got": bool(crumb), "len": len(crumb)}

    # 1) Yahoo chart（連通性測試，不需 crumb）
    r = _diag_try("https://query1.finance.yahoo.com/v8/finance/chart/%s.TW" % code, opener)
    res["yahoo_chart"] = {"status": r.get("status"), "error": r.get("error")}

    # 2) Yahoo quoteSummary financialData（分析師目標價）
    u = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/%s.TW?modules=financialData" % code
    if crumb:
        u += "&crumb=" + urllib.parse.quote(crumb)
    r = _diag_try(u, opener)
    info = {"status": r.get("status"), "error": r.get("error")}
    if r.get("body"):
        try:
            fd = json.loads(r["body"])["quoteSummary"]["result"][0]["financialData"]
            tm = fd.get("targetMeanPrice")
            info["targetMeanPrice"] = tm.get("raw") if isinstance(tm, dict) else tm
            info["numAnalysts"] = (fd.get("numberOfAnalystOpinions") or {}).get("raw") if isinstance(fd.get("numberOfAnalystOpinions"), dict) else fd.get("numberOfAnalystOpinions")
        except Exception as ex:
            info["parse_error"] = str(ex)[:100]
            info["body_head"] = r["body"][:160]
    res["yahoo_quoteSummary"] = info

    # 3) Yahoo quote v7
    u = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%s.TW" % code
    if crumb:
        u += "&crumb=" + urllib.parse.quote(crumb)
    r = _diag_try(u, opener)
    res["yahoo_quote_v7"] = {"status": r.get("status"), "error": r.get("error")}

    # 4) cnyes 外資評等整表
    r = _diag_try("https://www.cnyes.com/archive/twstock/board/ratediff.aspx?gt=qfii&gp=rate")
    c = {"status": r.get("status"), "error": r.get("error")}
    if r.get("body"):
        c["len"] = r.get("len")
        c["code_found"] = (code in r["body"])
    res["cnyes"] = c

    return res

_mis_opener_cache = None
def _mis_opener():
    """證交所 MIS 需要先取得 session cookie；快取 opener 重複使用。"""
    global _mis_opener_cache
    if _mis_opener_cache is not None:
        return _mis_opener_cache
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        op.open(urllib.request.Request("https://mis.twse.com.tw/stock/index.jsp", headers=HDRS), timeout=8).read()
    except Exception:
        pass
    _mis_opener_cache = op
    return op

def _mis_quotes(codes, out):
    """證交所官方即時批次（最接近實際成交價）。寫入 out[code]={price,prevClose}。"""
    op = _mis_opener()
    hdr = dict(HDRS); hdr["Referer"] = "https://mis.twse.com.tw/stock/index.jsp"
    for prefix in ("tse", "otc"):
        remaining = [c for c in codes if c not in out]
        if not remaining:
            break
        for i in range(0, len(remaining), 60):
            chunk = remaining[i:i + 60]
            ex = "|".join(prefix + "_" + c + ".tw" for c in chunk)
            url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch="
                   + urllib.parse.quote(ex) + "&json=1&delay=0&_=" + str(int(time.time() * 1000)))
            try:
                with op.open(urllib.request.Request(url, headers=hdr), timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:
                print(f"[-] MIS 批次失敗: {e}")
                continue
            for a in (data.get("msgArray") or []):
                code = a.get("c")
                if not code or code in out:
                    continue
                z = a.get("z"); y = a.get("y"); b = a.get("b")
                price = None
                if z not in (None, "", "-"):
                    try: price = float(z)
                    except Exception: price = None
                if price is None and b:
                    try: price = float(str(b).split("_")[0])
                    except Exception: price = None
                prev = None
                if y not in (None, "", "-"):
                    try: prev = float(y)
                    except Exception: prev = None
                if price is not None:
                    out[code] = {"price": price, "prevClose": prev}

def scrape_quotes(codes):
    """一次查多檔即時價＋昨收。證交所官方即時優先，Yahoo 補抓未取得者。"""
    out = {}
    codes = [c for c in codes if c]
    if not codes:
        return out
    try:
        _mis_quotes(codes, out)        # 1) 證交所官方即時（最準）
    except Exception as e:
        print(f"[-] MIS 整體失敗: {e}")
    missing = [c for c in codes if c not in out]
    if missing:
        _yahoo_quotes(missing, out)    # 2) Yahoo 補抓
    return out

def _yahoo_quotes(codes, out):
    """Yahoo v7 quote 批次補抓（MIS 未取得者）。"""
    opener, crumb = _yahoo_session()

    def query(symbols):
        results = []
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + urllib.parse.quote(",".join(chunk))
            if crumb:
                url += "&crumb=" + urllib.parse.quote(crumb)
            try:
                with opener.open(urllib.request.Request(url, headers=HDRS), timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                results += ((data.get("quoteResponse") or {}).get("result") or [])
            except Exception as e:
                print(f"[-] Yahoo quotes 批次失敗: {e}")
        return results

    def absorb(results):
        for it in results:
            code = it.get("symbol", "").split(".")[0]
            price = it.get("regularMarketPrice")
            prev = it.get("regularMarketPreviousClose")
            if price is not None and code not in out:
                out[code] = {"price": price, "prevClose": prev}

    absorb(query([c + ".TW" for c in codes]))
    miss = [c for c in codes if c not in out]
    if miss:
        absorb(query([c + ".TWO" for c in miss]))

def scrape_fundamentals(code):
    """Yahoo quoteSummary 抓本益比、股價淨值比、殖利率、EPS（公開、即時）。"""
    code = str(code).strip()
    for suffix in (".TW", ".TWO"):
        sym = code + suffix
        opener, crumb = _yahoo_session()
        data = None
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            url = ("https://" + host + "/v10/finance/quoteSummary/"
                   + urllib.parse.quote(sym)
                   + "?modules=summaryDetail,defaultKeyStatistics,financialData")
            if crumb:
                url += "&crumb=" + urllib.parse.quote(crumb)
            try:
                with opener.open(urllib.request.Request(url, headers=HDRS), timeout=12) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                break
            except Exception as e:
                print(f"[-] Yahoo fundamentals {sym}@{host} 失敗: {e}")
                continue
        if data is None:
            continue
        try:
            res = data["quoteSummary"]["result"][0]
        except Exception:
            continue
        sd = res.get("summaryDetail") or {}
        dks = res.get("defaultKeyStatistics") or {}

        def rawv(d, k):
            v = d.get(k)
            return v.get("raw") if isinstance(v, dict) else v

        pe = rawv(sd, "trailingPE")
        pb = rawv(dks, "priceToBook")
        eps = rawv(dks, "trailingEps")
        dy = rawv(sd, "dividendYield")
        if dy is None:
            dy = rawv(sd, "trailingAnnualDividendYield")
        yld = (dy * 100) if (dy is not None and dy < 1) else dy
        out = {}
        if pe is not None and pe > 0:
            out["pe"] = round(float(pe), 2)
        if pb is not None and pb > 0:
            out["pb"] = round(float(pb), 2)
        if eps is not None:
            out["eps"] = round(float(eps), 2)
        if yld is not None and yld > 0:
            out["yield"] = round(float(yld), 2)
        if out:
            return out
    return {}

def scrape_targets(code):
    """先試 Yahoo 股市（公開、依代號、結構穩定），無資料時回退鉅亨 cnyes（公開）。"""
    code = str(code).strip()
    out = _scrape_yahoo(code)
    if out:
        return out
    return _scrape_cnyes(code)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 保持終端機乾淨
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # 1) 代理路由：/fetch?url=<目標網址>
        if parsed.path == "/fetch":
            # 防止 parse_qs 把自帶多個 & 參數的目標 API 網址截斷
            target = None
            if "url=" in self.path:
                _, _, potential_url = self.path.partition("url=")
                target = urllib.parse.unquote(potential_url)
            if not target:
                qs = urllib.parse.parse_qs(parsed.query)
                target = qs.get("url", [None])[0]
            if not target:
                self._send(400, b'{"error":"missing url"}', "application/json")
                return
            if not _host_allowed(target):
                self._send(403, b'{"error":"host not allowed"}', "application/json")
                return
            try:
                req = urllib.request.Request(target, headers=HDRS)
                with urllib.request.urlopen(req, timeout=12) as r:
                    body = r.read()
                    ctype = r.headers.get("Content-Type", "application/json")
                self._send(200, body, ctype)
            except Exception as e:
                msg = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(502, msg, "application/json")
            return

        # 2) 健康檢查
        if parsed.path == "/ping":
            self._send(200, b'{"ok":true}', "application/json")
            return

        # 2a) 線上人數心跳 /beat?id=xxx
        if parsed.path == "/beat":
            qs = urllib.parse.parse_qs(parsed.query)
            cid = qs.get("id", [""])[0]
            n = beat(cid)
            self._send(200, json.dumps({"online": n}).encode("utf-8"), "application/json")
            return

        # 2c) 診斷：一次測試所有目標價來源 /diag?code=2330
        if parsed.path == "/diag":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", ["2330"])[0]
            try:
                body = json.dumps(diag(code), ensure_ascii=False, indent=2).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
            return

        # 2b) 法人/外資目標價介面
        if parsed.path == "/targets":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            if not code:
                self._send(400, b'{"error":"missing code"}', "application/json")
                return
            try:
                data = scrape_targets(code)
                body = json.dumps({"code": code, "targets": data}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                msg = json.dumps({"error": str(e), "targets": []}).encode("utf-8")
                self._send(502, msg, "application/json")
            return

        # 2d) 基本面（PE/PB/殖利率/EPS）/fundamentals?code=2330
        if parsed.path == "/fundamentals":
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            if not code:
                self._send(400, b'{"error":"missing code"}', "application/json")
                return
            try:
                data = scrape_fundamentals(code)
                body = json.dumps({"code": code, "fundamentals": data}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                msg = json.dumps({"error": str(e), "fundamentals": {}}).encode("utf-8")
                self._send(502, msg, "application/json")
            return

        # 2f) 批次報價 /quotes?codes=2330,2317,2454
        if parsed.path == "/quotes":
            qs = urllib.parse.parse_qs(parsed.query)
            raw = qs.get("codes", [""])[0]
            codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
            try:
                data = scrape_quotes(codes)
                body = json.dumps({"quotes": data}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(502, json.dumps({"error": str(e), "quotes": {}}).encode("utf-8"), "application/json")
            return

        # 3) 服務 App HTML 首頁
        if parsed.path in ("/", "/index.html", "/stock_app.html"):
            html = find_html()
            if not html:
                self._send(404, "找不到 stock_app.html，請放在同一資料夾。".encode("utf-8"), "text/plain; charset=utf-8")
                return
            with open(html, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

def lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None

def main():
    html = find_html()
    ip = lan_ip()
    print("=" * 56)
    print("  茫茫股海 — 即時報價代理")
    print("=" * 56)
    if html:
        print("  App 檔案識別成功：", os.path.basename(html))
    else:
        print("  ⚠ 警告：找不到 stock_app.html（請放同資料夾）")
    print("  本機請打開：    http://localhost:%d" % PORT)
    if ip:
        print("  區網手機請打開：http://%s:%d" % (ip, PORT))
    print("  外地手機：搭配 Tailscale / ngrok，詳見對話說明。")
    print("=" * 56)
    print("  ⚠ 已開放區網連線（0.0.0.0），請只在信任的網路使用。")
    print("=" * 56)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止運作。")

if __name__ == "__main__":
    main()
