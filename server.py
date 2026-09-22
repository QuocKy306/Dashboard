from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import json
import mimetypes
import re
import os
import threading
import libsql_client

ROOT = Path(__file__).resolve().parent
HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', 8000))

# Turso (libSQL) connection info - lấy từ biến môi trường, KHÔNG hard-code ở đây
TURSO_URL = os.environ.get('TURSO_DATABASE_URL')
TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN')

DEFAULT_LIST_NAME = 'Từ vựng mẫu'

SEED_VOCAB = [
    ('noon', 'giữa trưa'),
    ('workshop', 'phân xưởng / xưởng làm việc'),
    ('cafeteria', 'quán ăn tự phục vụ'),
    ('rental', 'sự cho thuê'),
    ('conference', 'hội nghị / hội thảo'),
    ("o'clock", 'giờ đúng'),
    ('luggage', 'hành lý'),
    ('enclose', 'bao quanh / vây quanh'),
    ('journey', 'chuyến đi / hành trình'),
    ('repair', 'sửa chữa'),
    ('cashier', 'thu ngân'),
    ('exchange', 'trao đổi'),
    ('schedule', 'lịch trình'),
    ('arrival', 'sự đến nơi'),
    ('departure', 'sự khởi hành'),
    ('passenger', 'hành khách'),
]

# ------------------------------------------------------------------ Turso DB
# Wrapper nhỏ để phần code còn lại vẫn dùng được API kiểu sqlite3
# (con.execute(...).fetchone()/.fetchall(), cur.lastrowid, cur.rowcount...)
_client_lock = threading.Lock()
_client = None


def _get_client():
    global _client
    if _client is None:
        if not TURSO_URL:
            raise RuntimeError(
                'Thiếu biến môi trường TURSO_DATABASE_URL. '
                'Hãy đặt TURSO_DATABASE_URL và TURSO_AUTH_TOKEN trước khi chạy server.'
            )
        _client = libsql_client.create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)
    return _client


class Cursor:
    def __init__(self, result_set):
        self._rs = result_set

    def fetchone(self):
        return self._rs.rows[0] if self._rs.rows else None

    def fetchall(self):
        return self._rs.rows

    @property
    def rowcount(self):
        return self._rs.rows_affected

    @property
    def lastrowid(self):
        return self._rs.last_insert_rowid


class Connection:
    def execute(self, sql, params=()):
        with _client_lock:
            rs = _get_client().execute(sql, tuple(params) if params else None)
        return Cursor(rs)

    def executemany(self, sql, seq_of_params):
        with _client_lock:
            client = _get_client()
            for params in seq_of_params:
                client.execute(sql, tuple(params))

    def commit(self):
        pass  # mỗi execute() trên Turso đã tự commit ngay

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def db_connect():
    return Connection()


def column_exists(con, table, column):
    rows = con.execute(f'PRAGMA table_info({table})').fetchall()
    return any(r['name'] == column for r in rows)


def init_db():
    with db_connect() as con:
        con.execute('''
            CREATE TABLE IF NOT EXISTS lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                is_learned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS vocabulary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                english TEXT NOT NULL COLLATE NOCASE,
                vietnamese TEXT NOT NULL,
                list_id INTEGER NOT NULL DEFAULT 1 REFERENCES lists(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(english, vietnamese, list_id)
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')

        if not column_exists(con, 'vocabulary', 'list_id'):
            con.execute('ALTER TABLE vocabulary ADD COLUMN list_id INTEGER NOT NULL DEFAULT 1')

        default_row = con.execute(
            'SELECT id FROM lists WHERE name = ?', (DEFAULT_LIST_NAME,)
        ).fetchone()
        if default_row:
            default_list_id = default_row['id']
        else:
            cur = con.execute('INSERT INTO lists (name) VALUES (?)', (DEFAULT_LIST_NAME,))
            default_list_id = cur.lastrowid

        seeded = con.execute("SELECT value FROM app_meta WHERE key='seeded'").fetchone()
        if not seeded:
            con.executemany(
                'INSERT OR IGNORE INTO vocabulary (english, vietnamese, list_id) VALUES (?, ?, ?)',
                [(e, v, default_list_id) for e, v in SEED_VOCAB],
            )
            con.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES('seeded', '1')")
        con.commit()


def list_to_dict(row, total):
    return {
        'id': row['id'],
        'name': row['name'],
        'is_learned': bool(row['is_learned']),
        'created_at': row['created_at'],
        'total': total,
    }


class AppHandler(BaseHTTPRequestHandler):
    server_version = 'EnglishMatch/1.1'

    def log_message(self, fmt, *args):
        print(f'[{self.log_date_time_string()}] {fmt % args}')

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length) if length else b'{}'
            return json.loads(raw.decode('utf-8'))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/lists':
            with db_connect() as con:
                rows = con.execute('SELECT * FROM lists ORDER BY id ASC').fetchall()
                counts = {
                    r['list_id']: r['c']
                    for r in con.execute(
                        'SELECT list_id, COUNT(*) AS c FROM vocabulary GROUP BY list_id'
                    ).fetchall()
                }
            self.send_json({'items': [list_to_dict(r, counts.get(r['id'], 0)) for r in rows]})
            return

        if parsed.path == '/api/vocabulary':
            query = parse_qs(parsed.query)
            search = (query.get('search', [''])[0] or '').strip()
            list_id_param = (query.get('list_id', [''])[0] or '').strip()
            with db_connect() as con:
                clauses = []
                params = []
                if list_id_param:
                    clauses.append('list_id = ?')
                    params.append(list_id_param)
                if search:
                    like = f'%{search}%'
                    clauses.append('(english LIKE ? OR vietnamese LIKE ?)')
                    params.extend([like, like])
                where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
                order = 'ORDER BY id DESC' if search else 'ORDER BY id ASC'
                rows = con.execute(
                    f'SELECT id, english, vietnamese, list_id, created_at FROM vocabulary {where} {order}',
                    params,
                ).fetchall()
            self.send_json({'items': [r.asdict() for r in rows], 'total': len(rows)})
            return

        if parsed.path == '/api/stats':
            with db_connect() as con:
                total = con.execute('SELECT COUNT(*) AS c FROM vocabulary').fetchone()['c']
                total_lists = con.execute('SELECT COUNT(*) AS c FROM lists').fetchone()['c']
                learned_lists = con.execute(
                    'SELECT COUNT(*) AS c FROM lists WHERE is_learned = 1'
                ).fetchone()['c']
            self.send_json({
                'total_vocabulary': total,
                'total_lists': total_lists,
                'learned_lists': learned_lists,
            })
            return

        self.serve_static(parsed.path)

    # --------------------------------------------------------------- POST
    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/lists':
            data = self.read_json()
            if data is None:
                self.send_json({'error': 'Dữ liệu JSON không hợp lệ.'}, 400)
                return
            name = re.sub(r'\s+', ' ', str(data.get('name', '')).strip())
            if not name:
                self.send_json({'error': 'Vui lòng nhập tên danh sách.'}, 400)
                return
            if len(name) > 80:
                self.send_json({'error': 'Tên danh sách quá dài.'}, 400)
                return
            try:
                with db_connect() as con:
                    cur = con.execute('INSERT INTO lists (name) VALUES (?)', (name,))
                    con.commit()
                    row = con.execute('SELECT * FROM lists WHERE id=?', (cur.lastrowid,)).fetchone()
                self.send_json({'item': list_to_dict(row, 0)}, 201)
            except libsql_client.LibsqlError as e:
                if 'UNIQUE' in str(e):
                    self.send_json({'error': 'Tên danh sách này đã tồn tại.'}, 409)
                else:
                    self.send_json({'error': 'Lỗi cơ sở dữ liệu.'}, 500)
            return

        if parsed.path == '/api/vocabulary':
            data = self.read_json()
            if data is None:
                self.send_json({'error': 'Dữ liệu JSON không hợp lệ.'}, 400)
                return

            english = re.sub(r'\s+', ' ', str(data.get('english', '')).strip())
            vietnamese = re.sub(r'\s+', ' ', str(data.get('vietnamese', '')).strip())
            list_id = data.get('list_id')
            if not english or not vietnamese:
                self.send_json({'error': 'Vui lòng nhập đủ từ tiếng Anh và nghĩa tiếng Việt.'}, 400)
                return
            if len(english) > 120 or len(vietnamese) > 220:
                self.send_json({'error': 'Từ hoặc nghĩa quá dài.'}, 400)
                return
            if not list_id:
                self.send_json({'error': 'Vui lòng chọn danh sách (thư mục).'}, 400)
                return

            try:
                with db_connect() as con:
                    list_row = con.execute('SELECT id FROM lists WHERE id=?', (list_id,)).fetchone()
                    if not list_row:
                        self.send_json({'error': 'Danh sách không tồn tại.'}, 404)
                        return
                    cur = con.execute(
                        'INSERT INTO vocabulary (english, vietnamese, list_id) VALUES (?, ?, ?)',
                        (english, vietnamese, list_id),
                    )
                    con.commit()
                    row = con.execute(
                        'SELECT id, english, vietnamese, list_id, created_at FROM vocabulary WHERE id=?',
                        (cur.lastrowid,),
                    ).fetchone()
                self.send_json({'item': row.asdict()}, 201)
            except libsql_client.LibsqlError as e:
                if 'UNIQUE' in str(e):
                    self.send_json({'error': 'Cặp từ này đã có trong danh sách.'}, 409)
                else:
                    self.send_json({'error': 'Lỗi cơ sở dữ liệu.'}, 500)
            return

        self.send_json({'error': 'Không tìm thấy API.'}, 404)

    # -------------------------------------------------------------- PATCH
    def do_PATCH(self):
        parsed = urlparse(self.path)
        match = re.fullmatch(r'/api/lists/(\d+)', parsed.path)
        if not match:
            self.send_json({'error': 'Không tìm thấy API.'}, 404)
            return

        data = self.read_json()
        if data is None:
            self.send_json({'error': 'Dữ liệu JSON không hợp lệ.'}, 400)
            return

        list_id = int(match.group(1))
        fields = []
        params = []
        if 'is_learned' in data:
            fields.append('is_learned = ?')
            params.append(1 if data.get('is_learned') else 0)
        if 'name' in data:
            name = re.sub(r'\s+', ' ', str(data.get('name', '')).strip())
            if not name:
                self.send_json({'error': 'Tên danh sách không hợp lệ.'}, 400)
                return
            fields.append('name = ?')
            params.append(name)

        if not fields:
            self.send_json({'error': 'Không có gì để cập nhật.'}, 400)
            return

        params.append(list_id)
        try:
            with db_connect() as con:
                cur = con.execute(f"UPDATE lists SET {', '.join(fields)} WHERE id=?", params)
                con.commit()
                if cur.rowcount == 0:
                    self.send_json({'error': 'Không tìm thấy danh sách.'}, 404)
                    return
                row = con.execute('SELECT * FROM lists WHERE id=?', (list_id,)).fetchone()
                total = con.execute(
                    'SELECT COUNT(*) AS c FROM vocabulary WHERE list_id=?', (list_id,)
                ).fetchone()['c']
            self.send_json({'item': list_to_dict(row, total)})
        except libsql_client.LibsqlError as e:
            if 'UNIQUE' in str(e):
                self.send_json({'error': 'Tên danh sách này đã tồn tại.'}, 409)
            else:
                self.send_json({'error': 'Lỗi cơ sở dữ liệu.'}, 500)

    # ------------------------------------------------------------- DELETE
    def do_DELETE(self):
        parsed = urlparse(self.path)

        list_match = re.fullmatch(r'/api/lists/(\d+)', parsed.path)
        if list_match:
            list_id = int(list_match.group(1))
            with db_connect() as con:
                remaining = con.execute('SELECT COUNT(*) AS c FROM lists').fetchone()['c']
                if remaining <= 1:
                    self.send_json({'error': 'Phải còn lại ít nhất một danh sách.'}, 400)
                    return
                cur = con.execute('DELETE FROM lists WHERE id=?', (list_id,))
                con.commit()
            if cur.rowcount == 0:
                self.send_json({'error': 'Không tìm thấy danh sách.'}, 404)
            else:
                self.send_json({'ok': True})
            return

        vocab_match = re.fullmatch(r'/api/vocabulary/(\d+)', parsed.path)
        if vocab_match:
            item_id = int(vocab_match.group(1))
            with db_connect() as con:
                cur = con.execute('DELETE FROM vocabulary WHERE id=?', (item_id,))
                con.commit()
            if cur.rowcount == 0:
                self.send_json({'error': 'Không tìm thấy từ vựng.'}, 404)
            else:
                self.send_json({'ok': True})
            return

        self.send_json({'error': 'Không tìm thấy API.'}, 404)

    # ------------------------------------------------------------ static
    def serve_static(self, path):
        if path in ('', '/'):
            file_path = ROOT / 'index.html'
        else:
            safe = Path(path.lstrip('/'))
            if '..' in safe.parts:
                self.send_error(403)
                return
            file_path = ROOT / safe

        if not file_path.exists() or not file_path.is_file():
            file_path = ROOT / 'index.html'

        data = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header('Content-Type', (content_type or 'application/octet-stream') + ('; charset=utf-8' if (content_type or '').startswith('text/') else ''))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    init_db()
    port = PORT
    server = None
    for attempt in range(10):
        try:
            server = ThreadingHTTPServer((HOST, port), AppHandler)
            break
        except OSError as e:
            if getattr(e, 'errno', None) == 48 or 'Address already in use' in str(e):
                print(f'Cổng {port} đang bận, thử cổng {port + 1}...')
                port += 1
            else:
                raise
    if server is None:
        print('Không tìm được cổng trống. Hãy đóng bớt chương trình khác rồi thử lại.')
        return

    print(f'English Matching Game đang chạy tại: http://{HOST}:{port}')
    print('Nhấn Ctrl+C để dừng.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nĐã dừng máy chủ.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
