#!/usr/bin/env python3
"""harness-board 的命令行入口和本地服务。

只读取 Claude Code 的会话记录和进程登记文件，不写回，不改配置。
服务只监听 127.0.0.1，随机端口加随机 token，没有写接口。只用 Python 标准库，语法兼容 Python 3.9。
"""
import argparse
import collections
import glob
import hmac
import http.client
import http.server
import json
import os
import plistlib
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

# 不在包目录里写 __pycache__：包装在 ~/.claude/skills/ 下时那就是写进了配置目录（15 号件 R-15）
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cc_records import Board, LineReader, doctor  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET = os.path.join(os.path.dirname(HERE), 'assets', 'board.html')
IDLE_LIMIT = 30 * 60
MAIN_POLL = 0.1
SLOW_POLL = 0.25
HEARTBEAT = 5.0
MSG_BUFFER = 1000


# ---------- 终端提示的语言（0.2.0） ----------
# 每条提示一份中文、一份英文。语言按这个顺序定：环境变量 HARNESS_BOARD_LANG（zh 或 en）；
# macOS 上系统设置里的首选语言（终端的 LANG 常被设成 en_US，和界面语言不一致）；LC_ALL、LC_MESSAGES、LANG；都没有就英文。
MSG = {
    'started': ('已启动', 'started'),
    'reused': ('复用现有服务', 'reused (already running)'),
    'announce_url': ('看板{verb}：{url}', 'Board {verb}: {url}'),
    'announce_opened': ('看板{verb}，已在浏览器打开。', 'Board {verb}, opened in your browser.'),
    'announce_no_browser': ('看板{verb}，但没能打开浏览器，请手动打开：{url}',
                            "Board {verb}, but the browser couldn't be opened. Open this address manually: {url}"),
    'start_failed': ('看板启动失败，服务没有响应。', 'The board failed to start: the service did not respond.'),
    'none_running': ('这个会话没有在运行的看板。', 'No board is running for this session.'),
    'stopped': ('看板已停止。', 'Board stopped.'),
    'running': ('看板运行中：{url}', 'Board running: {url}'),
    'no_sessions': ('没有运行中的 Claude Code 会话。', 'No Claude Code session is running.'),
    'bad_index': ('序号 {index} 不存在。', 'There is no session number {index}.'),
    'untitled': ('(未命名)', '(untitled)'),
    'pick_prompt': ('选一个序号：', 'Pick a number: '),
    'pick_hint': ('运行 board.py pick <序号> 选择一个会话。', 'Run board.py pick <number> to choose a session.'),
    'no_session_id': ('拿不到当前会话的 id。请在 Claude Code 会话里运行 /harness-board，或用 board.py pick 选择会话。',
                      "Can't find the current session id. Run /harness-board inside a Claude Code session, "
                      'or choose a session with board.py pick.'),
    'unsupported': ('harness-board 只支持 macOS 和 Linux，不支持这个系统（{name}）。',
                    'harness-board supports macOS and Linux only, not this system ({name}).'),
    'no_permission': ('没有权限访问 {path}。如果开着 Claude Code 的沙箱，它会挡住看板：'
                      '请在另一个终端里运行 python3 {board} pick，或者看说明里的「排错」。',
                      "No permission to access {path}. If Claude Code's sandbox is on, it blocks the board: "
                      'run python3 {board} pick in another terminal, or see Troubleshooting in the README.'),
    'request_error': ('请求处理出错：{name}', 'Request handling error: {name}'),
    'tail_error': ('跟读出错：{name}', 'Error while reading records: {name}'),
    'desc': ('harness-board：实时显示 Claude Code 会话走到哪一步',
             'harness-board: a live view of where the current Claude Code session is'),
    'help_index': ('pick 的序号', 'session number for pick'),
    'help_session': ('会话 id，默认读环境变量 CLAUDE_CODE_SESSION_ID', 'session id; defaults to $CLAUDE_CODE_SESSION_ID'),
    'help_no_open': ('只输出地址，不打开浏览器', 'print the address instead of opening the browser'),
}


def _mac_ui_lang():
    """macOS 系统设置里的首选语言（如 zh-Hans-US）。读不到返回 None。只读用户的全局偏好文件，不起子进程。"""
    try:
        with open(os.path.expanduser('~/Library/Preferences/.GlobalPreferences.plist'), 'rb') as f:
            langs = plistlib.load(f).get('AppleLanguages')
        return langs[0] if isinstance(langs, list) and langs and isinstance(langs[0], str) else None
    except Exception:
        return None


def ui_lang(env=None, platform=None, mac_lang=_mac_ui_lang):
    """终端提示用中文还是英文：返回 'zh' 或 'en'。"""
    env = os.environ if env is None else env
    forced = (env.get('HARNESS_BOARD_LANG') or '').strip().lower()
    if forced in ('zh', 'en'):
        return forced
    if (platform or sys.platform) == 'darwin':
        ui = mac_lang()
        if ui:
            return 'zh' if ui.lower().startswith('zh') else 'en'
    for k in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        v = env.get(k)
        if v:
            return 'zh' if v.lower().startswith('zh') else 'en'
    return 'en'


def T(key, **kw):
    zh, en = MSG[key]
    return (zh if ui_lang() == 'zh' else en).format(**kw)


def platform_ok():
    """只支持 POSIX（macOS、Linux）。Windows 上 os.kill(pid, 0) 等于给进程发 Ctrl+C，会打断正在运行的 Claude Code。"""
    return os.name == 'posix'


def _build():
    """代码版本标记。代码更新后，start 会重启旧服务而不是复用。"""
    names = (os.path.abspath(__file__), os.path.join(HERE, 'cc_records.py'))
    return str(max(os.stat(n).st_mtime_ns for n in names))


def config_dir():
    return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')


def state_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    d = os.path.join(base, 'harness-board')
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def find_transcript(config, sid):
    hits = glob.glob(os.path.join(glob.escape(config), 'projects', '*', glob.escape(sid) + '.jsonl'))
    return max(hits, key=os.path.getmtime) if hits else None


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            o = json.load(f)
        return o if isinstance(o, dict) else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, OverflowError):
        return False
    return True


def list_sessions(config):
    """列出运行中的 Claude Code 进程。只读 <pid>.json，同目录的 .key 文件不碰。"""
    out = []
    for path in glob.glob(os.path.join(glob.escape(config), 'sessions', '*.json')):
        r = _read_json(path)
        if r and isinstance(r.get('pid'), int) and r.get('sessionId') and _pid_alive(r['pid']):
            out.append(r)
    return out


def choose_session(sessions, cwd):
    """只有一个明确的候选时才自动选，否则返回 None，绝不猜。"""
    interactive = [s for s in sessions if s.get('kind') == 'interactive']
    pool = interactive or sessions
    here = [s for s in pool if s.get('cwd') == cwd]
    if len(here) == 1:
        return here[0]
    if not here and len(pool) == 1:
        return pool[0]
    return None


def should_exit(now, last_activity, clients, idle_limit):
    return clients == 0 and now - last_activity > idle_limit


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # 和跟读出错一样只记异常类型，不打整段堆栈（堆栈里可能带会话内容）
        print(T('request_error', name=sys.exc_info()[0].__name__), file=sys.stderr)


class Service:
    """跟读一个 Claude Code 进程的当前会话，并通过 SSE 推给本机页面。"""

    def __init__(self, config, session_id, claude_pid=None, state_path=None, idle_limit=IDLE_LIMIT, exit_delay=2.0):
        self.config = config
        self.session_id = session_id
        self.claude_pid = claude_pid
        self.state_path = state_path
        self.idle_limit = idle_limit
        self.exit_delay = exit_delay
        self.token = secrets.token_urlsafe(24)
        self.board = Board(session_id)
        self.stopped = threading.Event()
        self._halt = threading.Event()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._msgs = collections.deque(maxlen=MSG_BUFFER)
        self._seq = 0
        self._last_state = None
        self._clients = 0
        self._reader = None
        self._agents = {}
        self._meta_mtime = {}
        self._cwd = None
        self._last_activity = time.time()
        self._exit_at = None
        self._exited = False
        self._next_scan = 0.0
        self._httpd = None

    # ---------- 生命周期 ----------

    def start(self):
        self._httpd = _Server(('127.0.0.1', 0), self._handler())
        self.port = self._httpd.server_address[1]
        self._load_initial()
        self._last_state = json.dumps(self.snapshot(include_log=False), sort_keys=True)
        threading.Thread(target=self._httpd.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        self._write_state()

    def stop(self):
        if self._halt.is_set():
            self.stopped.wait(5)
            return
        self._halt.set()
        with self._cond:
            self._cond.notify_all()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._remove_state()
        self.stopped.set()

    def address(self):
        return self._httpd.server_address

    def client_count(self):
        with self._lock:
            return self._clients

    def url(self):
        return f'http://127.0.0.1:{self.port}/?t={self.token}'

    def snapshot(self, include_log=True):
        with self._lock:
            s = self.board.snapshot(include_log=include_log)
            s['session']['transcript_found'] = self._reader is not None
            s['session']['transcript_path'] = self._reader.path if self._reader else None
            s['session']['cwd'] = self._cwd
            s['session']['claude_pid'] = self.claude_pid
            s['seq'] = self._seq
            return s

    def _write_state(self):
        if not self.state_path:
            return
        data = {'pid': os.getpid(), 'claude_pid': self.claude_pid, 'session_id': self.session_id, 'port': self.port,
                'token': self.token, 'url': self.url(), 'build': _build()}
        tmp = f'{self.state_path}.{os.getpid()}.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, self.state_path)

    def _remove_state(self):
        if not self.state_path:
            return
        cur = _read_json(self.state_path)
        if cur and cur.get('pid') == os.getpid():
            try:
                os.remove(self.state_path)
            except OSError:
                pass

    # ---------- 读取 ----------

    def _collect_session(self, sid):
        """读出一个会话已有的全部内容，用于建立状态。"""
        # 先清掉旧会话的子 agent 读取器：新会话还没有记录文件时也要清，否则旧的记录会混进新会话（15 号件 R-3）
        self._agents, self._meta_mtime = {}, {}
        path = find_transcript(self.config, sid)
        if not path:
            return None, [], {}
        reader = LineReader(path)
        rows = reader.read()
        agents = {}
        for p in glob.glob(os.path.join(glob.escape(path[:-6]), 'subagents', '**', '*.jsonl'), recursive=True):
            aid = os.path.basename(p)[:-6]
            self._agents[aid] = LineReader(p)
            agents.setdefault(aid, {})['records'] = self._agents[aid].read()
        for p in glob.glob(os.path.join(glob.escape(path[:-6]), 'subagents', '**', '*.meta.json'), recursive=True):
            aid = os.path.basename(p)[:-10]
            self._meta_mtime[p] = self._mtime(p)
            agents.setdefault(aid, {})['meta'] = _read_json(p) or {}
            agents[aid]['meta_ts'] = self._mtime_s(p)
        return reader, rows, agents

    def _load_initial(self):
        with self._lock:
            self._reader, rows, agents = self._collect_session(self.session_id)
            self.board.replay(rows, agents=agents)
            self.board.drain()

    @staticmethod
    def _mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _mtime_s(path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def _rebuild(self):
        """记录文件变短（被改写或截断）：整份重读重建，不把重读出的记录当新增，计数不会翻倍（15 号件 R-5）。"""
        self._reader, rows, agents = self._collect_session(self.session_id)
        self.board.rebuild(rows, agents=agents)
        self._last_activity = time.time()

    def _read_main(self):
        if not self._reader:
            return
        resets = self._reader.resets
        rows = self._reader.read()
        if self._reader.resets != resets:
            self._rebuild()
            return
        if rows:
            self._last_activity = time.time()
        for r in rows:
            self.board.feed_line(r)

    def _find_main(self):
        if self._reader:
            return
        path = find_transcript(self.config, self.session_id)
        if path:
            self._reader = LineReader(path)
            self._last_activity = time.time()

    def _poll_agents(self):
        if not self._reader:
            return
        base = glob.escape(self._reader.path[:-6])
        # 先把子 agent 的记录读完，再看 meta 有没有判结束：最后一句话和 finished 常在同一个轮询周期里写下，
        # 反过来读会先判结束、再被随后读到的最后一句改回运行中（15 号件 R-1）
        for p in glob.glob(os.path.join(base, 'subagents', '**', '*.jsonl'), recursive=True):
            aid = os.path.basename(p)[:-6]
            if aid not in self._agents:
                self._agents[aid] = LineReader(p)
        for aid, reader in self._agents.items():
            resets = reader.resets
            rows = reader.read()
            if reader.resets != resets:
                self._rebuild()
                return
            if rows:
                self._last_activity = time.time()
            for r in rows:
                self.board.feed_line(r, agent=aid)
        for p in glob.glob(os.path.join(base, 'subagents', '**', '*.meta.json'), recursive=True):
            m = self._mtime(p)
            if m != self._meta_mtime.get(p):
                meta = _read_json(p)
                if meta is not None:
                    self._meta_mtime[p] = m
                    self.board.feed_meta(os.path.basename(p)[:-10], meta, ts=m / 1e9 if m else None)
                    self._last_activity = time.time()

    def _switch(self, sid):
        self.session_id = sid
        self._reader, rows, agents = self._collect_session(sid)
        self.board.switch_session(sid, rows, agents=agents)
        self._last_activity = time.time()
        self._write_state()

    def _poll_proc(self):
        if self._exited:
            return
        if self.claude_pid is None:
            now = time.time()
            if now >= self._next_scan:
                self._next_scan = now + 1.0
                hit = next((s for s in list_sessions(self.config) if s.get('sessionId') == self.session_id), None)
                if hit:
                    self.claude_pid = hit['pid']
                    self._write_state()
            if self.claude_pid is None:
                self.board.feed_proc(None)
                return
        if not _pid_alive(self.claude_pid):
            self._exited = True
            self.board.proc_exited()
            self._exit_at = time.time() + self.exit_delay
            return
        reg = _read_json(os.path.join(self.config, 'sessions', f'{self.claude_pid}.json'))
        if reg is None:
            self.board.feed_proc(None)
            return
        self._cwd = reg.get('cwd')
        sid = reg.get('sessionId')
        if sid and sid != self.session_id:
            self._switch(sid)
        if reg.get('status') != self.board.proc_status:
            self._last_activity = time.time()
        self.board.feed_proc(reg)

    # ---------- 主循环 ----------

    def _publish(self):
        # 先算快照再取走变化：快照出错时变化不会丢（15 号件 R-6）
        state = self.snapshot(include_log=False)
        d = self.board.drain()
        state.pop('seq', None)
        key = json.dumps(state, sort_keys=True)
        if not d['transitions'] and not d['log'] and key == self._last_state:
            return
        self._last_state = key
        self._seq += 1
        self._msgs.append((self._seq, json.dumps({'seq': self._seq, 'transitions': d['transitions'], 'log': d['log'], 'state': state})))
        self._cond.notify_all()

    def _loop(self):
        next_slow = 0.0
        while not self._halt.is_set():
            now = time.time()
            try:
                with self._lock:
                    self._read_main()
                    if now >= next_slow:
                        next_slow = now + SLOW_POLL
                        self._find_main()
                        self._poll_proc()
                        self._poll_agents()
                    self._publish()
            except Exception as e:  # 单次出错不让服务停下
                print(T('tail_error', name=type(e).__name__), file=sys.stderr)
            if (self._exit_at and now >= self._exit_at) or should_exit(now, self._last_activity, self.client_count(), self.idle_limit):
                threading.Thread(target=self.stop, daemon=True).start()
                return
            self._halt.wait(MAIN_POLL)

    # ---------- HTTP ----------

    def _handler(service):  # noqa: N805
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            # 读请求的超时：连上不发请求的连接 10 秒后断开，不一直占着线程（15 号件 R-17）
            timeout = 10
            server_version = 'harness-board'
            sys_version = ''

            def log_message(self, *a):
                pass

            def _deny(self):
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True

            def _allowed(self, query):
                token = parse_qs(query).get('t', [''])[0]
                host_ok = self.headers.get('Host') == f'127.0.0.1:{service.port}'
                return host_ok and hmac.compare_digest(token.encode(), service.token.encode())

            def do_GET(self):
                u = urlparse(self.path)
                if not self._allowed(u.query):
                    return self._deny()
                if u.path == '/':
                    return self._page()
                if u.path == '/health':
                    return self._json({'ok': True, 'pid': os.getpid(), 'session': service.session_id, 'clients': service.client_count()})
                if u.path == '/events':
                    return self._events()
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def _json(self, data):
                b = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b)

            def _page(self):
                with open(ASSET, 'rb') as f:
                    b = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(b)))
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; font-src data:; base-uri 'none'; form-action 'none'")
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b)

            def _send_event(self, name, seq, body):
                self.wfile.write(f'id: {seq}\nevent: {name}\ndata: {body}\n\n'.encode())
                self.wfile.flush()

            def _events(self):
                self.close_connection = True
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                with service._cond:
                    snap = service.snapshot()
                    seen = snap['seq']
                    # 快照算好了才加连接计数：算快照出错时不会多算一个、让服务永远不空闲退出（15 号件 R-6）
                    service._clients += 1
                try:
                    self._send_event('snapshot', seen, json.dumps(snap))
                    last_beat = time.time()
                    while not service._halt.is_set():
                        with service._cond:
                            service._cond.wait(timeout=1.0)
                            pending = [m for m in service._msgs if m[0] > seen]
                            gap = bool(pending) and pending[0][0] != seen + 1
                            snap = service.snapshot() if gap else None
                        if snap is not None:
                            seen = snap['seq']
                            self._send_event('snapshot', seen, json.dumps(snap))
                            continue
                        for seq, body in pending:
                            self._send_event('delta', seq, body)
                            seen = seq
                        if time.time() - last_beat >= HEARTBEAT:
                            self.wfile.write(b': hb\n\n')
                            self.wfile.flush()
                            last_beat = time.time()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with service._cond:
                        service._clients -= 1
                        service._last_activity = time.time()

        return Handler


# ---------- 命令行 ----------

def _key(session_id, claude_pid):
    return f'p{claude_pid}' if claude_pid else 's' + ''.join(c for c in session_id if c.isalnum() or c == '-')


def _health(st):
    try:
        c = http.client.HTTPConnection('127.0.0.1', st['port'], timeout=1)
        c.request('GET', f"/health?t={st['token']}", headers={'Host': f"127.0.0.1:{st['port']}"})
        r = c.getresponse()
        ok = r.status == 200 and json.loads(r.read()).get('pid') == st.get('pid')
        c.close()
        return ok
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return False


def _find_state(session_id, claude_pid):
    """找到管这个会话的服务：先按进程，再按会话 id。"""
    d = state_dir()
    path = os.path.join(d, _key(session_id, claude_pid) + '.json')
    st = _read_json(path)
    if st:
        return path, st
    for p in glob.glob(os.path.join(glob.escape(d), '*.json')):
        st = _read_json(p)
        if st and (st.get('session_id') == session_id or (claude_pid and st.get('claude_pid') == claude_pid)):
            return p, st
    return path, None


def _open_browser(url):
    try:
        return subprocess.run(['open', url], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _announce(url, verb, no_open):
    """verb 是 MSG 里的键：'started' 或 'reused'。"""
    verb = T(verb)
    if no_open:
        print(T('announce_url', verb=verb, url=url))
    elif _open_browser(url):
        # 打开成功就不再输出带令牌的网址：这段输出会进会话上下文和会话记录（15 号件 R-16）
        print(T('announce_opened', verb=verb))
    else:
        print(T('announce_no_browser', verb=verb, url=url))


def _terminate(pid):
    os.kill(pid, signal.SIGTERM)
    end = time.time() + 3
    while time.time() < end and _pid_alive(pid):
        time.sleep(0.02)


def cmd_start(session_id, claude_pid, no_open):
    path, st = _find_state(session_id, claude_pid)
    if st and _health(st):
        if st.get('build') == _build():
            _announce(st['url'], 'reused', no_open)
            return 0
        _terminate(st['pid'])
    if st:
        try:
            os.remove(path)
        except OSError:
            pass
    path = os.path.join(state_dir(), _key(session_id, claude_pid) + '.json')
    args = [sys.executable, os.path.abspath(__file__), '--serve', '--session', session_id, '--state', path]
    if claude_pid:
        args += ['--claude-pid', str(claude_pid)]
    with open(path + '.err', 'w') as err:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True, close_fds=True, cwd=state_dir())
    end = time.time() + 10
    while time.time() < end:
        st = _read_json(path)
        if st and _health(st):
            _announce(st['url'], 'started', no_open)
            return 0
        time.sleep(0.02)
    print(T('start_failed'), file=sys.stderr)
    return 1


def cmd_stop(session_id, claude_pid):
    path, st = _find_state(session_id, claude_pid)
    if not st or not _health(st):
        if st:
            try:
                os.remove(path)
            except OSError:
                pass
        print(T('none_running'))
        return 0
    _terminate(st['pid'])
    print(T('stopped'))
    return 0


def cmd_status(session_id, claude_pid):
    _, st = _find_state(session_id, claude_pid)
    if st and _health(st):
        print(T('running', url=st['url']))
    else:
        print(T('none_running'))
    return 0


def cmd_pick(config, no_open, index):
    sessions = list_sessions(config)
    if not sessions:
        print(T('no_sessions'))
        return 1
    cwd = os.getcwd()
    sessions.sort(key=lambda s: (s.get('cwd') != cwd, s.get('kind') != 'interactive', -(s.get('startedAt') or 0)))
    chosen = None
    if index is not None:
        if 1 <= index <= len(sessions):
            chosen = sessions[index - 1]
        else:
            print(T('bad_index', index=index))
    elif len(sessions) == 1 or choose_session(sessions, cwd):
        chosen = choose_session(sessions, cwd) or sessions[0]
    if not chosen:
        for i, s in enumerate(sessions, 1):
            print(f"{i}. {s.get('name') or T('untitled')}  {s.get('cwd') or ''}  {s.get('status') or ''}  {s.get('kind') or ''}")
        if sys.stdin.isatty():
            try:
                index = int(input(T('pick_prompt')))
                chosen = sessions[index - 1] if 1 <= index <= len(sessions) else None
            except (ValueError, EOFError):
                chosen = None
        if not chosen:
            print(T('pick_hint'))
            return 1
    return cmd_start(chosen['sessionId'], chosen['pid'], no_open)


def cmd_doctor(config):
    r = doctor(os.path.join(config, 'projects'))
    print(json.dumps(r, ensure_ascii=False, indent=1))
    broken = r['crashes'] or r['non_human_prompts'] or r['inflight_underflow'] or r['stuck_after_turn']
    return 1 if broken else 0


def cmd_serve(config, session_id, claude_pid, state_path):
    svc = Service(config, session_id, claude_pid=claude_pid, state_path=state_path)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=svc.stop, daemon=True).start())
    signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=svc.stop, daemon=True).start())
    svc.start()
    svc.stopped.wait()
    if state_path:
        try:
            os.remove(state_path + '.err')
        except OSError:
            pass
    return 0


def main(argv=None):
    if not platform_ok():
        print(T('unsupported', name=sys.platform), file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog='board.py', description=T('desc'))
    p.add_argument('command', nargs='?', default='start', choices=['start', 'stop', 'status', 'pick', 'doctor'])
    p.add_argument('index', nargs='?', type=int, help=T('help_index'))
    p.add_argument('--session', help=T('help_session'))
    p.add_argument('--no-open', action='store_true', help=T('help_no_open'))
    p.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--state', help=argparse.SUPPRESS)
    p.add_argument('--claude-pid', type=int, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    config = config_dir()
    if a.serve:
        return cmd_serve(config, a.session, a.claude_pid, a.state)
    try:
        return _run(a, config)
    except PermissionError as e:
        # Claude Code 的沙箱对 skill 里的 ! 命令也生效，只许写工作目录，写 ~/.cache/harness-board 会被挡（0.2.0 实测）。
        # 不甩出整段报错，说清原因和绕开的办法
        print(T('no_permission', path=e.filename or '', board=os.path.abspath(__file__)), file=sys.stderr)
        return 3


def _run(a, config):
    if a.command == 'doctor':
        return cmd_doctor(config)
    if a.command == 'pick':
        return cmd_pick(config, a.no_open, a.index)
    sid = a.session or os.environ.get('CLAUDE_CODE_SESSION_ID')
    if not sid:
        print(T('no_session_id'), file=sys.stderr)
        return 2
    hit = next((s for s in list_sessions(config) if s.get('sessionId') == sid), None)
    pid = hit['pid'] if hit else None
    if a.command == 'stop':
        return cmd_stop(sid, pid)
    if a.command == 'status':
        return cmd_status(sid, pid)
    return cmd_start(sid, pid, a.no_open)


if __name__ == '__main__':
    sys.exit(main())
