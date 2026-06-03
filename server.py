#!/usr/bin/env python3
"""
Server gia phả họ Nguyễn — Duy Tiên · Hà Nam
Chạy: python3 server.py
Truy cập: http://localhost:8000
"""
import json, os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE   = os.path.dirname(os.path.abspath(__file__))
DATA   = os.path.join(BASE, 'data.json')
PORT   = int(os.environ.get('PORT', 8000))

# ── Khởi tạo data.json nếu chưa có ──────────────────────────────────────────
def init_data():
    if not os.path.exists(DATA):
        # Tạo data.json từ dữ liệu gốc (hardcoded 84 thành viên)
        default = {"members": [], "requests": [], "version": 1}
        with open(DATA, 'w', encoding='utf-8') as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print(f"[OK] Tạo {DATA}")
    else:
        print(f"[OK] Dữ liệu: {DATA}")

def read_data():
    with open(DATA, 'r', encoding='utf-8') as f:
        return json.load(f)

def write_data(data):
    with open(DATA, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        mime = {
            '.html': 'text/html; charset=utf-8',
            '.js':   'application/javascript; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.css':  'text/css; charset=utf-8',
            '.svg':  'image/svg+xml',
            '.png':  'image/png',
            '.ico':  'image/x-icon',
        }.get(ext, 'application/octet-stream')
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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
            if os.path.isfile(filepath):
                try:
                    self.send_file(filepath)
                except Exception as e:
                    self.send_json(500, {'error': str(e)})
            else:
                self.send_json(404, {'error': f'Not found: {p}'})

    def do_POST(self):
        p = urlparse(self.path).path.rstrip('/')
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        d = read_data()

        # ── Lưu toàn bộ danh sách thành viên ──
        if p == '/api/members':
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

        # ── Gửi yêu cầu đăng ký ──
        elif p == '/api/requests':
            import time, random, string
            req = body
            req['id'] = 'req-' + str(int(time.time())) + '-' + ''.join(random.choices(string.ascii_lowercase, k=4))
            req['status'] = 'pending'
            req['ts'] = time.strftime('%H:%M, %d/%m/%Y')
            d.setdefault('requests', []).append(req)
            write_data(d)
            self.send_json(200, {'ok': True, 'id': req['id']})

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
        p = urlparse(self.path).path.rstrip('/')
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        d = read_data()

        # ── Sửa một thành viên ──
        if p.startswith('/api/members/'):
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
        p = urlparse(self.path).path.rstrip('/')
        d = read_data()

        # ── Xoá một thành viên ──
        if p.startswith('/api/members/'):
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
