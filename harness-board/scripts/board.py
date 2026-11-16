#!/usr/bin/env python3
"""harness-board 的命令行入口和本地服务。

只读取 Claude Code、Codex 的会话记录和进程信息，不写回，不改配置。
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

from cc_records import Board, LineReader, _ts, doctor  # noqa: E402
from cx_records import (CodexBoard, codex_doctor, codex_home, find_rollout, first_record, host_of,  # noqa: E402
                        lock_holders, open_rollouts, running_sessions)

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
    'no_sessions': ('没有运行中的 Claude Code 或 Codex 会话。', 'No Claude Code or Codex session is running.'),
    'bad_index': ('序号 {index} 不存在。', 'There is no session number {index}.'),
    'untitled': ('(未命名)', '(untitled)'),
    'pick_prompt': ('选一个序号：', 'Pick a number: '),
    'pick_hint': ('运行 board.py pick <序号> 选择一个会话。', 'Run board.py pick <number> to choose a session.'),
    'no_session_id': ('拿不到当前会话的 id。请在 Claude Code 里运行 /harness-board、在 Codex 里运行 $harness-board，或用 board.py pick 选择会话。',
                      "Can't find the current session id. Run /harness-board in Claude Code or $harness-board in Codex, "
                      'or choose a session with board.py pick.'),
    # Codex 的沙箱不让开本地端口、写缓存（19 号件 §五）：这句话主要是给 Codex 里的模型看的，它会申请在沙箱外重跑
    'codex_sandbox': ('Codex 的沙箱不让看板开本地端口、写缓存目录。请在沙箱外重跑同一条命令（申请提权，sandbox_permissions: require_escalated）。',
                      "Codex's sandbox blocks the board from listening on a local port and writing its cache. "
                      'Rerun the same command outside the sandbox (request escalated permissions: sandbox_permissions require_escalated).'),
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
    'help_session': ('会话 id，默认读环境变量 CODEX_THREAD_ID，再读 CLAUDE_CODE_SESSION_ID', 'session id; defaults to $CODEX_THREAD_ID, then $CLAUDE_CODE_SESSION_ID'),
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
    names = (os.path.abspath(__file__), os.path.join(HERE, 'cc_records.py'), os.path.join(HERE, 'cx_records.py'))
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


SHELLS = frozenset(('zsh', 'bash', 'sh', 'fish', 'dash', 'ksh', 'tcsh', 'csh'))


def _command_child(pid, command):
    """Codex 进程下有没有正在跑这条命令的 shell。等你批准时没有，批准后命令跑起来才有（19 号件 §三）。
    要对上命令原文：Codex 还会起一闪而过的 shell 做环境快照，只看「有没有子 shell」会把它当成命令开跑（实测）。"""
    if not command:
        return False
    want = ' '.join(command.split())[:80]
    try:
        out = subprocess.run(['ps', '-A', '-ww', '-o', 'ppid=,args='], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for ln in out.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2 and parts[0] == str(pid):
            args = parts[1]
            exe = os.path.basename(args.split(None, 1)[0]).lstrip('-')
            if exe in SHELLS and want in ' '.join(args.split()):
                return True
    return False


def _codex_parent():
    """沿本进程的父进程往上找 Codex（找不到锁文件的占用者时用）。"""
    pid = os.getppid()
    for _ in range(6):
        try:
            out = subprocess.run(['ps', '-o', 'ppid=,comm=', '-p', str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        ppid, comm = (out.split(None, 1) + [''])[:2]
        if os.path.basename(comm.strip()) == 'codex':
            return pid
        if not ppid.isdigit() or int(ppid) <= 1:
            return None
        pid = int(ppid)
    return None


class CodexService(Service):
    """跟读一个 Codex 会话（20 号件）：主记录、它的子 agent 和自动审核会话、进程和锁文件。"""

    def __init__(self, home, session_id, codex_pid=None, state_path=None, idle_limit=IDLE_LIMIT, exit_delay=2.0):
        super().__init__(home, session_id, claude_pid=codex_pid, state_path=state_path, idle_limit=idle_limit, exit_delay=exit_delay)
        self.home = home
        self.board = CodexBoard(session_id)
        self._children = {}
        self._seen = set()
        self._next_child = 0.0
        self._next_lock = 0.0
        self._next_switch = 0.0
        self._next_title = 0.0
        self._next_child_proc = 0.0
        self._title_mtime = None

    def snapshot(self, include_log=True):
        s = super().snapshot(include_log=include_log)
        s['session']['product'] = 'codex'
        s['session']['cwd'] = getattr(self.board, 'cwd', None)
        return s

    # ---------- 读取 ----------

    def _collect_session(self, sid):
        self._children, self._seen = {}, set()
        path = find_rollout(self.home, sid)
        if not path:
            return None, [], {}
        reader = LineReader(path)
        return reader, reader.read(), {}

    def _child_of(self, path):
        """一个 rollout 是不是本会话的子会话：返回 (会话 id, 'guardian' 或 'agent')，不是返回 None；还读不到第一行返回 False。"""
        m = first_record(path)
        if not m:
            return False
        src = m.get('source')
        spawn = src.get('subagent', {}).get('thread_spawn', {}) if isinstance(src, dict) and isinstance(src.get('subagent'), dict) else {}
        parent = m.get('parent_thread_id') or (spawn.get('parent_thread_id') if isinstance(spawn, dict) else None)
        if parent != self.session_id or not m.get('id'):
            return None
        guardian = m.get('thread_source') == 'guardian_review' or 'guardian' in json.dumps(src)
        return m['id'], 'guardian' if guardian else 'agent'

    def _scan_children(self, initial=False):
        """找新出现的子会话，读出已有记录：[(会话 id, 种类, 记录)]。"""
        if initial and self._reader:
            since = os.path.getmtime(self._reader.path) - 7 * 86400
            paths = [p for p in glob.glob(os.path.join(glob.escape(self.home), 'sessions', '*', '*', '*', 'rollout-*.jsonl'))
                     if self._mtime_s(p) and self._mtime_s(p) >= since]
        else:
            days = {time.strftime('%Y/%m/%d', time.localtime(time.time() - d * 86400)) for d in (0, 1)}
            paths = [p for d in days for p in glob.glob(os.path.join(glob.escape(self.home), 'sessions', d, 'rollout-*.jsonl'))]
        out = []
        for p in paths:
            if p in self._seen or (self._reader and p == self._reader.path):
                continue
            hit = self._child_of(p)
            if hit is False:
                continue
            self._seen.add(p)
            if hit:
                reader = LineReader(p)
                self._children[p] = (hit[0], hit[1], reader)
                out.append((hit[0], hit[1], reader.read()))
        return out

    def _replay_merged(self, rows, kids, log=True):
        """主记录和子会话的记录按时间合并后回放：自动审核要落在它审的那一步上。"""
        b = self.board
        was = b._replaying, b._log_on
        b._replaying, b._log_on = True, log
        try:
            pending = sorted(((_ts(r) or 0.0, n, cid, kind, r) for cid, kind, rs in kids for n, r in enumerate(rs) if isinstance(r, dict)),
                             key=lambda x: (x[0], x[1]))
            i = 0
            for r in rows:
                t = _ts(r) if isinstance(r, dict) else None
                while i < len(pending) and t is not None and pending[i][0] <= t:
                    b.feed_child(pending[i][2], [pending[i][4]], pending[i][3])
                    i += 1
                b.feed_line(r)
            for x in pending[i:]:
                b.feed_child(x[2], [x[4]], x[3])
        finally:
            b._replaying, b._log_on = was

    def _load_initial(self):
        with self._lock:
            self._reader, rows, _ = self._collect_session(self.session_id)
            self._replay_merged(rows, self._scan_children(initial=True))
            self._load_title()
            self.board.drain()

    def _rebuild(self):
        b = self.board
        b._reset_session()
        self._reader, rows, _ = self._collect_session(self.session_id)
        self._replay_merged(rows, self._scan_children(initial=True), log=False)
        self._load_title(force=True)
        self._last_activity = time.time()

    def _switch(self, sid):
        b = self.board
        self.session_id = b.session_id = sid
        b.prev_title = b.custom_title or b.ai_title
        b._reset_session()
        b._log.clear()
        b._pending_log = []
        self._reader, rows, _ = self._collect_session(sid)
        self._replay_merged(rows, self._scan_children(initial=True), log=False)
        b._add_log('会话已切换', sid[:8], time.time())
        self._load_title(force=True)
        self._last_activity = time.time()
        self._write_state()

    def _find_main(self):
        if self._reader:
            return
        path = find_rollout(self.home, self.session_id)
        if path:
            self._reader = LineReader(path)
            self._last_activity = time.time()

    def _poll_agents(self):
        now = time.time()
        if now >= self._next_child:
            self._next_child = now + 1.0
            for cid, kind, rows in self._scan_children():
                if rows:
                    self._last_activity = now
                    self.board.feed_child(cid, rows, kind)
        for cid, kind, reader in list(self._children.values()):
            rows = reader.read()
            if rows:
                self._last_activity = now
                self.board.feed_child(cid, rows, kind)

    def _new_main(self, paths):
        """同一进程开着的记录里，比当前会话更晚在写的主会话（不是子会话）：返回它的 id，没有返回 None。"""
        cur = self._mtime_s(self._reader.path) or 0
        best = None
        for p in paths:
            # 只跳过子会话：_seen 里也有扫子会话时看过的普通主会话，新会话的记录就在里面（实测）
            if p == self._reader.path or p in self._children:
                continue
            m = first_record(p)
            if not m or isinstance(m.get('source'), dict) or not m.get('id'):
                continue
            t = self._mtime_s(p) or 0
            if t > cur + 1 and (best is None or t > best[0]):
                best = (t, m['id'])
        return best[1] if best else None

    def _load_title(self, force=False):
        """会话标题：Codex 写在 session_index.jsonl（id、thread_name），只在文件变了时重读。"""
        path = os.path.join(self.home, 'session_index.jsonl')
        m = self._mtime(path)
        if m is None or (m == self._title_mtime and not force):
            return
        self._title_mtime = m
        name = None
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for ln in f:
                    if self.session_id in ln:
                        try:
                            o = json.loads(ln)
                        except ValueError:
                            continue
                        if o.get('id') == self.session_id and isinstance(o.get('thread_name'), str):
                            name = o['thread_name']
        except OSError:
            return
        if name:
            self.board.custom_title = name

    # ---------- 进程 ----------

    def _poll_proc(self):
        if self._exited:
            return
        now = time.time()
        b = self.board
        if self.claude_pid is None:
            if now >= self._next_lock:
                self._next_lock = now + 2.0
                held = lock_holders(self.home, self.session_id)
                if held:
                    self.claude_pid = min(held)
                    self._write_state()
            if self.claude_pid is None:
                b.proc_available = False
                return
        b.proc_available = True
        alive = _pid_alive(self.claude_pid)
        if alive and now >= self._next_lock:
            self._next_lock = now + 2.0
            held = lock_holders(self.home, self.session_id)
            if held is not None and self.claude_pid not in held:
                if held:
                    self.claude_pid = min(held)
                    self._write_state()
                elif self._reader:
                    alive = False   # 锁没人占了：会话已关闭（锁文件本身不删，19 号件 §四）
        if not alive:
            self._exited = True
            b.proc_exited()
            self._exit_at = now + self.exit_delay
            return
        if b.guess_waiting() and now >= self._next_child_proc:
            self._next_child_proc = now + 0.5
            if _command_child(self.claude_pid, b.guess_command()):
                b.command_running(now)
        if now >= self._next_switch and host_of(b.originator) == 'terminal' and self._reader:
            # 终端里 /new、/resume 换会话：同一个进程开着的主会话记录里，最近在写的那个就是当前会话
            self._next_switch = now + 2.0
            sid = self._new_main(open_rollouts(self.claude_pid))
            if sid:
                self._switch(sid)
        if now >= self._next_title:
            self._next_title = now + 5.0
            self._load_title()


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


def cmd_start(session_id, claude_pid, no_open, product='claude'):
    if product == 'codex':
        # 桌面端所有对话共用一个 app-server 进程：Codex 的状态文件按会话 id 分，不按进程
        claude_pid_key = None
    else:
        claude_pid_key = claude_pid
    path, st = _find_state(session_id, claude_pid_key)
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
    path = os.path.join(state_dir(), _key(session_id, claude_pid_key) + '.json')
    args = [sys.executable, os.path.abspath(__file__), '--serve', '--session', session_id, '--state', path, '--product', product]
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
    sessions = list_sessions(config) + running_sessions(codex_home())
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
    return cmd_start(chosen['sessionId'], chosen['pid'], no_open, chosen.get('product') or 'claude')


def cmd_doctor(config):
    r = doctor(os.path.join(config, 'projects'))
    home = codex_home()
    if os.path.isdir(os.path.join(home, 'sessions')):
        r['codex'] = codex_doctor(home)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    broken = any(x['crashes'] or x['non_human_prompts'] or x['inflight_underflow'] or x['stuck_after_turn']
                 for x in (r, r.get('codex') or r))
    return 1 if broken else 0


def cmd_serve(config, session_id, claude_pid, state_path, product='claude'):
    if product == 'codex':
        svc = CodexService(codex_home(), session_id, codex_pid=claude_pid, state_path=state_path)
    else:
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
    p.add_argument('--product', choices=['claude', 'codex'], default='claude', help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    config = config_dir()
    if a.serve:
        return cmd_serve(config, a.session, a.claude_pid, a.state, a.product)
    if os.environ.get('CODEX_SANDBOX') and a.command in ('start', 'pick'):
        # 在 Codex 的沙箱里：开不了端口、写不了缓存，起服务注定失败（19 号件 §五）
        print(T('codex_sandbox'), file=sys.stderr)
        return 3
    try:
        return _run(a, config)
    except PermissionError as e:
        # Claude Code 的沙箱对 skill 里的 ! 命令也生效，只许写工作目录，写 ~/.cache/harness-board 会被挡（0.2.0 实测）。
        # 不甩出整段报错，说清原因和绕开的办法；在 Codex 里（有 CODEX_THREAD_ID）说 Codex 沙箱的办法
        if os.environ.get('CODEX_THREAD_ID'):
            print(T('codex_sandbox'), file=sys.stderr)
        else:
            print(T('no_permission', path=e.filename or '', board=os.path.abspath(__file__)), file=sys.stderr)
        return 3


def resolve_session(arg, config, env=None):
    """当前会话：(会话 id, 'claude' 或 'codex')（20 号件 §2.1）。
    --session 不为空就用它，按记录在哪边定产品；否则先看 CODEX_THREAD_ID（从 Claude Code 里起的 Codex 会继承 Claude 的变量），
    再看 CLAUDE_CODE_SESSION_ID。Claude Code 的 SKILL.md 写的是 --session=${CLAUDE_SESSION_ID}，在 Codex 里它是空的。"""
    env = os.environ if env is None else env
    sid = (arg or '').strip()
    if sid:
        if find_transcript(config, sid):
            return sid, 'claude'
        if find_rollout(codex_home(), sid):
            return sid, 'codex'
        return sid, 'codex' if env.get('CODEX_THREAD_ID') == sid else 'claude'
    if env.get('CODEX_THREAD_ID'):
        return env['CODEX_THREAD_ID'], 'codex'
    if env.get('CLAUDE_CODE_SESSION_ID'):
        return env['CLAUDE_CODE_SESSION_ID'], 'claude'
    return None, None


def _run(a, config):
    if a.command == 'doctor':
        return cmd_doctor(config)
    if a.command == 'pick':
        return cmd_pick(config, a.no_open, a.index)
    sid, product = resolve_session(a.session, config)
    if not sid:
        print(T('no_session_id'), file=sys.stderr)
        return 2
    if product == 'codex':
        held = lock_holders(codex_home(), sid)
        pid = min(held) if held else _codex_parent()
        if a.command == 'stop':
            return cmd_stop(sid, None)
        if a.command == 'status':
            return cmd_status(sid, None)
        return cmd_start(sid, pid, a.no_open, 'codex')
    hit = next((s for s in list_sessions(config) if s.get('sessionId') == sid), None)
    pid = hit['pid'] if hit else None
    if a.command == 'stop':
        return cmd_stop(sid, pid)
    if a.command == 'status':
        return cmd_status(sid, pid)
    return cmd_start(sid, pid, a.no_open)


if __name__ == '__main__':
    sys.exit(main())
