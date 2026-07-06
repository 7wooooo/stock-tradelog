#!/usr/bin/env python3
# 주식 매매일지 로컬 서버
# - index.html 을 http://localhost 로 띄우고
# - 시세(네이버 금융) / 환율 은 이 PC에서 대신 받아와 /api 로 넘겨줌 (CORS 문제 없음)
import datetime
import http.server, socketserver, urllib.request, urllib.parse, json, os, re, subprocess, threading, webbrowser

PORT = 8770
DIR = os.path.dirname(os.path.abspath(__file__))


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read()


def run_git(args):
    return subprocess.run(
        ['git'] + args,
        cwd=DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )


def git_update(commit_message=None):
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    date = stamp[:10]
    run_git(['add', 'data.json', 'index.html', 'server.py'])

    staged = run_git(['diff', '--cached', '--quiet'])
    changed = staged.returncode != 0
    message = (commit_message or '').strip()
    if not message:
        message = f'update stock tradelog {stamp}' if changed else f'retrigger pages {date}'

    commit_args = ['commit', '-m', message]
    if not changed:
        commit_args.insert(1, '--allow-empty')
    commit = run_git(commit_args)
    if commit.returncode != 0:
        return {
            'ok': False,
            'step': 'commit',
            'message': message,
            'stdout': commit.stdout,
            'stderr': commit.stderr,
        }

    push = run_git(['push', 'origin', 'main'])
    if push.returncode != 0:
        return {
            'ok': False,
            'step': 'push',
            'message': message,
            'stdout': push.stdout,
            'stderr': push.stderr,
        }

    return {
        'ok': True,
        'changed': changed,
        'message': message,
        'commit': commit.stdout.strip(),
        'push': push.stdout.strip() or push.stderr.strip(),
    }


def normalize_lookup_item(item):
    path = (item.get('url') or '').strip()
    code = (item.get('code') or '').strip()
    type_code = (item.get('typeCode') or '').strip().upper()
    ticker = code
    if path.startswith('/domestic/stock/') and code:
        ticker = f'{code}.KQ' if type_code == 'KOSDAQ' else f'{code}.KS'
    return {
        'ticker': ticker,
        'path': path,
        'name': item.get('name'),
        'typeCode': type_code,
    }


def lookup_by_name(name):
    url = 'https://ac.stock.naver.com/ac?q=' + urllib.parse.quote(name) + '&target=stock,ipo,index,marketindicator'
    data = json.loads(fetch(url))
    items = data.get('items') or []
    if not items:
        return None
    return normalize_lookup_item(items[0])


def polling_url_from_path(path):
    clean = (path or '').strip()
    if not clean.startswith('/'):
        return None
    if clean.endswith('/total'):
        clean = clean[:-6]
    return 'https://polling.finance.naver.com/api/realtime' + clean

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIR, **k)

    def log_message(self, *a):  # 조용히
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == '/api/save':
            try:
                n = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(n)
                json.loads(body)  # 유효성 검사
                with open(os.path.join(DIR, 'data.json'), 'wb') as f:
                    f.write(body)
                return self._json({'ok': True})
            except Exception as e:
                return self._json({'error': str(e)}, 500)
        if p.path == '/api/git-update':
            try:
                n = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(n) or b'{}')
                if 'state' in payload:
                    body = json.dumps(payload['state'], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                    json.loads(body)
                    with open(os.path.join(DIR, 'data.json'), 'wb') as f:
                        f.write(body)
                result = git_update(payload.get('message'))
                return self._json(result, 200 if result.get('ok') else 500)
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)}, 500)
        return self._json({'error': 'not found'}, 404)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)

        if p.path == '/api/load':
            fp = os.path.join(DIR, 'data.json')
            if os.path.exists(fp):
                with open(fp, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._json({})

        if p.path == '/api/fx':
            for url in ('https://api.frankfurter.app/latest?from=USD&to=KRW',
                        'https://open.er-api.com/v6/latest/USD'):
                try:
                    d = json.loads(fetch(url))
                    return self._json({'rate': d['rates']['KRW']})
                except Exception:
                    continue
            return self._json({'error': 'fx failed'}, 502)

        if p.path == '/api/resolve':
            q = urllib.parse.parse_qs(p.query)
            name = (q.get('name', [''])[0]).strip()
            if not name:
                return self._json({'error': 'no name'}, 400)
            try:
                found = lookup_by_name(name)
                if not found:
                    return self._json({'error': 'not found'}, 404)
                return self._json(found)
            except Exception as e:
                return self._json({'error': str(e)}, 502)

        if p.path == '/api/price':
            q = urllib.parse.parse_qs(p.query)
            t = (q.get('ticker', [''])[0]).strip()
            path = (q.get('path', [''])[0]).strip()
            if not t and not path:
                return self._json({'error': 'no ticker'}, 400)
            try:
                if path:
                    url = polling_url_from_path(path)
                    if not url:
                        return self._json({'error': 'bad path'}, 400)
                elif re.search(r'\.(KS|KQ)$', t, re.I):
                    code = re.sub(r'\.(KS|KQ)$', '', t, flags=re.I)
                    url = 'https://polling.finance.naver.com/api/realtime/domestic/stock/' + urllib.parse.quote(code)
                else:
                    url = 'https://polling.finance.naver.com/api/realtime/worldstock/stock/' + urllib.parse.quote(t)
                d = json.loads(fetch(url))
                datas = d.get('datas') or []
                if not datas:
                    return self._json({'error': 'not found'}, 404)
                row = datas[0]
                raw = row.get('closePriceRaw')
                if raw is None:
                    raw = str(row.get('closePrice', '')).replace(',', '')
                price = float(raw)
                return self._json({'price': price, 'name': row.get('stockName')})
            except Exception as e:
                return self._json({'error': str(e)}, 502)

        return super().do_GET()

def main():
    os.chdir(DIR)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    host = '127.0.0.1'
    port = PORT
    try:
        httpd = socketserver.ThreadingTCPServer((host, port), Handler)
    except OSError as e:
        if getattr(e, 'errno', None) == 48:
            print(f'포트 {port}이 이미 사용 중입니다.')
            print(f'기존 서버를 종료한 뒤 다시 실행하세요: lsof -ti tcp:{port} | xargs kill')
            return
        raise
    with httpd:
        url = f'http://localhost:{port}/index.html'
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        print('=' * 48)
        print('  주식 매매일지 실행 중')
        print('  브라우저 주소:', url)
        print('  종료: 이 창에서  Ctrl + C')
        print('=' * 48)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\n종료합니다.')

if __name__ == '__main__':
    main()
