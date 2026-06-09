#!/usr/bin/env python3
"""
Server gia phả họ Nguyễn — Duy Tiên · Hà Nam
Chạy: python3 server.py
Truy cập: http://localhost:8000

Lưu trữ dữ liệu: GitHub API (persistent) + bộ nhớ cache (nhanh)
"""
import json, os, base64, threading, hashlib, hmac, secrets, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'data.json')
PORT = int(os.environ.get('PORT', 8000))

# ── Xác thực admin ────────────────────────────────────────────────────────────
# Mật khẩu lưu dưới dạng SHA-256 hash — KHÔNG bao giờ lưu plaintext
# Thay đổi bằng: python3 -c "import hashlib; print(hashlib.sha256(b'MatKhauMoi').hexdigest())"
ADMIN_USER   = os.environ.get('ADMIN_USER', 'Administrator')
# Mặc định hash của 'Giapha@hoNguyen243' — đặt biến môi trường ADMIN_HASH trên server
ADMIN_HASH   = os.environ.get(
    'ADMIN_HASH',
    hashlib.sha256(b'Giapha@hoNguyen243').hexdigest()
)
TOKEN_TTL    = 8 * 3600          # token hết hạn sau 8 giờ
_tokens: dict = {}               # {token: expire_ts}
_tokens_lock = threading.Lock()

# Rate limiting: tối đa 5 lần thử sai / 10 phút / IP
_fail_log: dict = {}             # {ip: [ts, ts, ...]}
_fail_lock = threading.Lock()
MAX_FAILS = 5
FAIL_WINDOW = 600  # giây

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _fail_lock:
        hits = [t for t in _fail_log.get(ip, []) if now - t < FAIL_WINDOW]
        _fail_log[ip] = hits
        return len(hits) >= MAX_FAILS

def _record_fail(ip: str):
    now = time.time()
    with _fail_lock:
        _fail_log.setdefault(ip, []).append(now)

def _issue_token() -> str:
    token = secrets.token_hex(32)
    with _tokens_lock:
        _tokens[token] = time.time() + TOKEN_TTL
    return token

def _validate_token(token: str) -> bool:
    if not token:
        return False
    with _tokens_lock:
        exp = _tokens.get(token)
        if exp and time.time() < exp:
            return True
        _tokens.pop(token, None)
        return False

def _check_auth(handler) -> bool:
    """Kiểm tra header Authorization: Bearer <token>. Trả False và gửi 401 nếu không hợp lệ."""
    auth = handler.headers.get('Authorization', '')
    token = auth.removeprefix('Bearer ').strip()
    if _validate_token(token):
        return True
    handler.send_json(401, {'error': 'Chưa xác thực hoặc phiên đã hết hạn'})
    return False

# Các endpoint chỉ đọc — không cần token
_PUBLIC_GET = {'/api/status', '/api/members', '/api/gallery'}

# ── GitHub config (đặt biến môi trường trên Render) ──────────────────────────
GH_TOKEN = os.environ.get('GH_TOKEN', '')          # Personal Access Token
GH_REPO  = os.environ.get('GH_REPO',  'nguyenhahlqn/giapha')
GH_FILE  = os.environ.get('GH_FILE',  'data.json')
GH_API   = f'https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE}'

# ── Bộ nhớ cache (tránh đọc GitHub liên tục) ─────────────────────────────────
_cache      = None
_cache_lock = threading.Lock()

def _gh_headers():
    h = {'Accept': 'application/vnd.github.v3+json',
         'Content-Type': 'application/json'}
    if GH_TOKEN:
        h['Authorization'] = f'token {GH_TOKEN}'
    return h

def _fetch_from_github():
    """Đọc data.json từ GitHub API, trả về (data_dict, file_sha)."""
    try:
        req = Request(GH_API, headers=_gh_headers())
        with urlopen(req, timeout=10) as r:
            meta = json.loads(r.read())
        content = base64.b64decode(meta['content']).decode('utf-8')
        return json.loads(content), meta['sha']
    except Exception as e:
        print(f'[GitHub] Không đọc được: {e}')
        return None, None

def _push_to_github(data, sha):
    """Ghi data.json lên GitHub API."""
    if not GH_TOKEN:
        print('[GitHub] Không có GH_TOKEN — bỏ qua sync')
        return False
    try:
        content_b64 = base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        ).decode('ascii')
        body = json.dumps({
            'message': f'[auto] Cập nhật data.json (v{data.get("version",1)})',
            'content': content_b64,
            'sha': sha
        }).encode('utf-8')
        req = Request(GH_API, data=body, headers=_gh_headers(), method='PUT')
        with urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        new_sha = resp['content']['sha']
        print(f'[GitHub] ✓ Đã sync — SHA: {new_sha[:8]}')
        return new_sha
    except HTTPError as e:
        print(f'[GitHub] Lỗi {e.code}: {e.read().decode()}')
        return False
    except Exception as e:
        print(f'[GitHub] Lỗi push: {e}')
        return False

def _load_admin_hash():
    """Nếu mật khẩu đã được đổi và lưu trong data.json thì dùng nó."""
    global ADMIN_HASH
    try:
        if os.path.exists(DATA):
            with open(DATA, 'r', encoding='utf-8') as f:
                d = json.load(f)
            saved = d.get('config', {}).get('admin_hash')
            if saved:
                ADMIN_HASH = saved
                print('[Auth] Dùng mật khẩu đã được đổi từ data.json')
    except Exception:
        pass

def init_data():
    """Khởi động: ưu tiên đọc từ GitHub, fallback về file local."""
    global _cache
    _load_admin_hash()
    with _cache_lock:
        if GH_TOKEN:
            print('[GitHub] Đang tải dữ liệu từ GitHub...')
            data, sha = _fetch_from_github()
            if data:
                _cache = {'data': data, 'sha': sha}
                # Cập nhật file local để đồng bộ
                with open(DATA, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                # Cập nhật ADMIN_HASH nếu đã được đổi
                saved_hash = data.get('config', {}).get('admin_hash')
                if saved_hash:
                    global ADMIN_HASH
                    ADMIN_HASH = saved_hash
                print(f'[GitHub] ✓ {len(data.get("members",[]))} thành viên, SHA: {sha[:8]}')
                return
        # Fallback: đọc từ file local
        if os.path.exists(DATA):
            with open(DATA, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _cache = {'data': data, 'sha': None}
            print(f'[Local] ✓ {len(data.get("members",[]))} thành viên')
        else:
            data = {"members": [], "requests": [], "version": 1}
            _cache = {'data': data, 'sha': None}
            print('[Local] Tạo data.json mới')

def read_data():
    """Đọc từ cache (nhanh)."""
    with _cache_lock:
        return json.loads(json.dumps(_cache['data']))  # deep copy

def write_data(data):
    """Ghi vào cache + file local + GitHub (async)."""
    global _cache
    with _cache_lock:
        old_sha = _cache.get('sha')
        _cache = {'data': data, 'sha': old_sha}
    # Ghi file local ngay lập tức
    with open(DATA, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Đẩy lên GitHub ở background thread
    def _sync():
        with _cache_lock:
            sha = _cache.get('sha')
        new_sha = _push_to_github(data, sha)
        if new_sha:
            with _cache_lock:
                _cache['sha'] = new_sha
    threading.Thread(target=_sync, daemon=True).start()

# ── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def _cors_origin(self):
        origin = self.headers.get('Origin', '')
        allowed = os.environ.get('ALLOWED_ORIGIN', 'https://honguyenhanam.id.vn')
        # Cho phép localhost trong dev
        if origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1'):
            return origin
        return allowed if origin == allowed else allowed

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', self._cors_origin())
        self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(body)

    def _is_social_crawler(self):
        ua = self.headers.get('User-Agent', '')
        crawlers = ['facebookexternalhit', 'Facebot', 'Twitterbot', 'LinkedInBot',
                    'WhatsApp', 'Slackbot', 'TelegramBot', 'Zalo', 'Googlebot', 'Bingbot']
        return any(c.lower() in ua.lower() for c in crawlers)

    def send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        mime = {
            '.html': 'text/html; charset=utf-8',
            '.js':   'application/javascript; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.css':  'text/css; charset=utf-8',
            '.svg':  'image/svg+xml',
            '.png':  'image/png',
            '.jpg':  'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
            '.ico':  'image/x-icon',
        }.get(ext, 'application/octet-stream')
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(body))
        if ext in ('.jpg', '.jpeg', '.png', '.webp', '.svg', '.ico'):
            self.send_header('Cache-Control', 'public, max-age=86400')
        else:
            self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Robots-Tag', 'index, follow')
        # Cho phép social crawlers đọc nội dung (bypass CORS cho crawler)
        if self._is_social_crawler():
            self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', self._cors_origin())
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Vary', 'Origin')
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path.rstrip('/')

        # ── API endpoints ──
        if p == '/api/members':
            d = read_data()
            self.send_json(200, {'members': d.get('members', []), 'count': len(d.get('members', []))})

        elif p == '/api/requests':
            d = read_data()
            self.send_json(200, {'requests': d.get('requests', [])})

        elif p == '/api/gallery':
            d = read_data()
            self.send_json(200, {'gallery': d.get('gallery', [])})

        elif p == '/api/status':
            d = read_data()
            m = d.get('members', [])
            r = d.get('requests', [])
            self.send_json(200, {
                'members': len(m),
                'pendingRequests': len([x for x in r if x.get('status') == 'pending']),
                'version': d.get('version', 1)
            })

        # ── Static files ──
        else:
            if p == '' or p == '/':
                p = '/ho-nguyen-hero.html'
            filepath = os.path.join(BASE, p.lstrip('/'))
            # Log social crawler access để debug
            if self._is_social_crawler():
                ua = self.headers.get('User-Agent', '')[:60]
                print(f'[Crawler] {ua} → {p}')
            if os.path.isfile(filepath):
                try:
                    self.send_file(filepath)
                except Exception as e:
                    self.send_json(500, {'error': str(e)})
            else:
                self.send_json(404, {'error': f'Not found: {p}'})

    def do_POST(self):
        global ADMIN_HASH
        p = urlparse(self.path).path.rstrip('/')
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        # ── Đăng nhập (public — không cần token) ──
        if p == '/api/login':
            ip = self.client_address[0]
            if _is_rate_limited(ip):
                self.send_json(429, {'error': 'Quá nhiều lần thử sai. Vui lòng đợi 10 phút.'})
                return
            u = body.get('username', '')
            pw = body.get('password', '')
            pw_hash = hashlib.sha256(pw.encode()).hexdigest()
            if hmac.compare_digest(u, ADMIN_USER) and hmac.compare_digest(pw_hash, ADMIN_HASH):
                token = _issue_token()
                self.send_json(200, {'ok': True, 'token': token, 'ttl': TOKEN_TTL})
            else:
                _record_fail(ip)
                time.sleep(0.5)  # thêm độ trễ chống brute-force
                self.send_json(401, {'error': 'Tài khoản hoặc mật khẩu không đúng'})
            return

        # ── Đổi mật khẩu (yêu cầu token + mật khẩu cũ) ──
        if p == '/api/change-password':
            if not _check_auth(self): return
            old_pw  = body.get('oldPassword', '')
            new_pw  = body.get('newPassword', '')
            old_hash = hashlib.sha256(old_pw.encode()).hexdigest()
            # Xác minh mật khẩu hiện tại
            if not hmac.compare_digest(old_hash, ADMIN_HASH):
                ip = self.client_address[0]
                _record_fail(ip)
                time.sleep(0.5)
                self.send_json(401, {'error': 'Mật khẩu hiện tại không đúng'})
                return
            # Kiểm tra độ mạnh tối thiểu
            if len(new_pw) < 10:
                self.send_json(400, {'error': 'Mật khẩu mới phải có ít nhất 10 ký tự'})
                return
            new_hash = hashlib.sha256(new_pw.encode()).hexdigest()
            # Lưu hash mới vào data.json (bền vững qua restart)
            d2 = read_data()
            d2.setdefault('config', {})['admin_hash'] = new_hash
            write_data(d2)
            # Cập nhật biến runtime
            ADMIN_HASH = new_hash
            # Thu hồi tất cả token cũ (bắt buộc đăng nhập lại)
            with _tokens_lock:
                _tokens.clear()
            print('[Auth] Mật khẩu đã được thay đổi — tất cả phiên đã bị thu hồi')
            self.send_json(200, {'ok': True})
            return

        # ── Yêu cầu đăng ký (public — người dùng gửi) ──
        if p == '/api/requests':
            import random, string
            req = body
            req['id'] = 'req-' + str(int(time.time())) + '-' + ''.join(random.choices(string.ascii_lowercase, k=4))
            req['status'] = 'pending'
            req['ts'] = time.strftime('%H:%M, %d/%m/%Y')
            d2 = read_data()
            d2.setdefault('requests', []).append(req)
            write_data(d2)
            self.send_json(200, {'ok': True, 'id': req['id']})
            return

        # ── Tất cả endpoints còn lại yêu cầu token ──
        if not _check_auth(self):
            return

        d = read_data()

        # ── Gallery: lưu toàn bộ ──
        if p == '/api/gallery':
            items = body.get('gallery', body if isinstance(body, list) else [])
            d['gallery'] = items
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True, 'count': len(items)})

        # ── Gallery: thêm một item ──
        elif p == '/api/gallery/add':
            items = d.get('gallery', [])
            new_id = body.get('id') or f"g{int(max((int(x['id'][1:]) for x in items if x.get('id','').startswith('g') and x['id'][1:].isdigit()), default=0)) + 1}"
            body['id'] = str(new_id)
            items.append(body)
            d['gallery'] = items
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True, 'item': body})

        # ── Lưu toàn bộ danh sách thành viên ──
        elif p == '/api/members':
            members = body.get('members', body if isinstance(body, list) else [])
            d['members'] = members
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True, 'count': len(members), 'version': d['version']})

        # ── Thêm một thành viên ──
        elif p == '/api/members/add':
            member = body
            if not member.get('id'):
                ids = [int(m['id']) for m in d['members'] if str(m.get('id','')).isdigit()]
                member['id'] = str(max(ids) + 1 if ids else 1)
            d['members'].append(member)
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True, 'member': member, 'version': d['version']})

        # ── Phê duyệt yêu cầu ──
        elif p.startswith('/api/requests/') and p.endswith('/approve'):
            rid = p.split('/')[-2]
            reqs = d.get('requests', [])
            req = next((r for r in reqs if r['id'] == rid), None)
            if not req:
                self.send_json(404, {'error': 'Không tìm thấy yêu cầu'}); return
            req['status'] = 'approved'
            if req.get('type') == 'add':
                ids = [int(m['id']) for m in d['members'] if str(m.get('id','')).isdigit()]
                new_id = str(max(ids) + 1 if ids else 1)
                new_member = {
                    'id': new_id, 'n': req.get('name',''),
                    'g': int(req.get('generation', 1)),
                    'sx': 'f' if req.get('gender') == 'female' else 'm',
                    'par': req.get('fatherId') or None,
                    'born': req.get('birthYear',''), 'died': req.get('deathYear',''),
                    'note': req.get('note',''), 'al':'', 'role':'', 'ori':'', 'occ':'', 'br':''
                }
                d['members'].append(new_member)
                d['version'] = d.get('version', 1) + 1
                write_data(d)
                self.send_json(200, {'ok': True, 'newMember': new_member, 'version': d['version']})
            elif req.get('type') == 'edit':
                target_id = req.get('editTargetId')
                for m in d['members']:
                    if str(m.get('id')) == str(target_id):
                        if req.get('name'): m['n'] = req['name']
                        if req.get('birthYear'): m['born'] = req['birthYear']
                        if req.get('deathYear'): m['died'] = req['deathYear']
                        if req.get('gender'): m['sx'] = 'f' if req['gender']=='female' else 'm'
                        if req.get('origin'): m['ori'] = req['origin']
                        if req.get('occupation'): m['occ'] = req['occupation']
                        if req.get('note'): m['note'] = (m.get('note','') + '\n' + req['note']).strip()
                        break
                d['version'] = d.get('version', 1) + 1
                write_data(d)
                self.send_json(200, {'ok': True, 'version': d['version']})
            else:
                write_data(d)
                self.send_json(200, {'ok': True})

        # ── Từ chối yêu cầu ──
        elif p.startswith('/api/requests/') and p.endswith('/reject'):
            rid = p.split('/')[-2]
            for r in d.get('requests', []):
                if r['id'] == rid:
                    r['status'] = 'rejected'; break
            write_data(d)
            self.send_json(200, {'ok': True})

        else:
            self.send_json(404, {'error': f'API không tồn tại: {p}'})

    def do_PUT(self):
        if not _check_auth(self): return
        p = urlparse(self.path).path.rstrip('/')
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        d = read_data()

        # ── Sửa một gallery item ──
        if p.startswith('/api/gallery/'):
            gid = p.split('/')[-1]
            items = d.get('gallery', [])
            found = False
            for i, item in enumerate(items):
                if str(item.get('id')) == str(gid):
                    items[i] = {**item, **body}; found = True; break
            if not found:
                self.send_json(404, {'error': f'Không tìm thấy gallery ID {gid}'}); return
            d['gallery'] = items
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True})

        # ── Sửa một thành viên ──
        elif p.startswith('/api/members/'):
            mid = p.split('/')[-1]
            found = False
            for i, m in enumerate(d['members']):
                if str(m.get('id')) == str(mid):
                    d['members'][i] = {**m, **body}
                    found = True; break
            if not found:
                self.send_json(404, {'error': f'Không tìm thấy ID {mid}'}); return
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True, 'version': d['version']})
        else:
            self.send_json(404, {'error': 'API không tồn tại'})

    def do_DELETE(self):
        if not _check_auth(self): return
        p = urlparse(self.path).path.rstrip('/')
        d = read_data()

        # ── Xoá một gallery item ──
        if p.startswith('/api/gallery/'):
            gid = p.split('/')[-1]
            before = len(d.get('gallery', []))
            d['gallery'] = [x for x in d.get('gallery', []) if str(x.get('id')) != str(gid)]
            if len(d['gallery']) == before:
                self.send_json(404, {'error': f'Không tìm thấy gallery ID {gid}'}); return
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True})

        # ── Xoá một thành viên ──
        elif p.startswith('/api/members/'):
            mid = p.split('/')[-1]
            before = len(d['members'])
            d['members'] = [m for m in d['members'] if str(m.get('id')) != str(mid)]
            if len(d['members']) == before:
                self.send_json(404, {'error': f'Không tìm thấy ID {mid}'}); return
            d['version'] = d.get('version', 1) + 1
            write_data(d)
            self.send_json(200, {'ok': True, 'version': d['version']})
        else:
            self.send_json(404, {'error': 'API không tồn tại'})


if __name__ == '__main__':
    init_data()
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f"""
╔══════════════════════════════════════════╗
║   Gia phả Họ Nguyễn — Server đang chạy  ║
╠══════════════════════════════════════════╣
║  http://localhost:{PORT}                    ║
║  http://localhost:{PORT}/ho-nguyen-tree.html║
╚══════════════════════════════════════════╝
  Nhấn Ctrl+C để dừng.
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer đã dừng.")
