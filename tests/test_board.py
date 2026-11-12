#!/usr/bin/env python3
"""board.py 的测试：用真实文件、真实进程和真实 HTTP 连接，不打开浏览器。运行：
    python3 tests/test_board.py
"""
import hashlib
import http.client
import json
import os
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, '..', 'harness-board', 'scripts'))
sys.path.insert(0, SCRIPTS)

import board  # noqa: E402

BOARD = os.path.join(SCRIPTS, 'board.py')


def rec_human(text='你好'):
    return {'type': 'user', 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': text},
            'timestamp': '2026-11-06T10:00:00.000Z', 'version': '2.1.318'}


def rec_api():
    return {'type': 'api-request', 'querySource': 'repl_main_thread', 'timestamp': '2026-11-06T10:00:01.000Z'}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def wait_until(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return fn()


class Sandbox:
    """一个假的 Claude 配置目录。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, 'claude')
        self.state = os.path.join(self.tmp, 'state')
        os.makedirs(os.path.join(self.config, 'projects', 'proj'))
        os.makedirs(os.path.join(self.config, 'sessions'))
        os.makedirs(self.state)

    def path(self, sid='s1', proj='proj'):
        os.makedirs(os.path.join(self.config, 'projects', proj), exist_ok=True)
        return os.path.join(self.config, 'projects', proj, sid + '.jsonl')

    def append(self, recs, sid='s1', proj='proj'):
        with open(self.path(sid, proj), 'a', encoding='utf-8') as f:
            for r in recs:
                f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + '\n')

    def registry(self, pid, **kw):
        data = dict({'pid': pid, 'sessionId': 's1', 'cwd': '/w', 'status': 'idle', 'kind': 'interactive', 'name': 'n'}, **kw)
        p = os.path.join(self.config, 'sessions', f'{pid}.json')
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, p)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SSE:
    """真实的 SSE 客户端，把事件放进队列。"""

    def __init__(self, port, token):
        self.q = queue.Queue()
        self.conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        self.conn.request('GET', f'/events?t={token}', headers={'Host': f'127.0.0.1:{port}'})
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        ev, data = None, None
        try:
            while True:
                line = self.resp.fp.readline()
                if not line:
                    return
                line = line.decode().rstrip('\n')
                if line.startswith('event: '):
                    ev = line[7:]
                elif line.startswith('data: '):
                    data = json.loads(line[6:])
                elif line == '' and ev:
                    self.q.put((ev, data))
                    ev, data = None, None
        except Exception:
            return

    def next(self, timeout=5):
        return self.q.get(timeout=timeout)

    def until(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            try:
                ev, d = self.next(max(0.01, end - time.time()))
            except queue.Empty:
                break
            if pred(ev, d):
                return ev, d
        raise AssertionError('等待超时，没等到期望的事件')

    def close(self):
        """真正断开连接：响应对象会一直占着 socket，只关 conn 不会断。"""
        try:
            self.resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass


def get(port, path, host=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    c.putrequest('GET', path, skip_host=True)
    c.putheader('Host', host or f'127.0.0.1:{port}')
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, r, body


class 定位(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.cleanup()

    def test_找不到记录文件返回None(self):
        self.assertIsNone(board.find_transcript(self.s.config, 'nope'))

    def test_同一个id命中多个目录时取修改时间最新的(self):
        self.s.append([rec_human()], proj='old')
        os.utime(self.s.path(proj='old'), (1000, 1000))
        self.s.append([rec_human()], proj='new')
        self.assertEqual(board.find_transcript(self.s.config, 's1'), self.s.path(proj='new'))

    def test_配置目录优先用环境变量(self):
        old = os.environ.get('CLAUDE_CONFIG_DIR')
        os.environ['CLAUDE_CONFIG_DIR'] = '/x/y'
        try:
            self.assertEqual(board.config_dir(), '/x/y')
        finally:
            if old is None:
                del os.environ['CLAUDE_CONFIG_DIR']
            else:
                os.environ['CLAUDE_CONFIG_DIR'] = old

    def test_只列出活着的会话(self):
        self.s.registry(os.getpid(), sessionId='alive')
        p = subprocess.Popen(['true'])
        p.wait()
        self.s.registry(p.pid, sessionId='dead')
        with open(os.path.join(self.s.config, 'sessions', '123.abc.key'), 'w') as f:
            f.write('secret')
        with open(os.path.join(self.s.config, 'sessions', '999.json'), 'w') as f:
            f.write('{bad')
        got = board.list_sessions(self.s.config)
        self.assertEqual([x['sessionId'] for x in got], ['alive'])

    def test_没有登记目录时返回空(self):
        shutil.rmtree(os.path.join(self.s.config, 'sessions'))
        self.assertEqual(board.list_sessions(self.s.config), [])

    def test_当前目录只有一个会话时直接选(self):
        ss = [{'sessionId': 'a', 'cwd': '/w'}, {'sessionId': 'b', 'cwd': '/other'}]
        self.assertEqual(board.choose_session(ss, '/w')['sessionId'], 'a')

    def test_当前目录有多个会话时不猜(self):
        ss = [{'sessionId': 'a', 'cwd': '/w'}, {'sessionId': 'b', 'cwd': '/w'}]
        self.assertIsNone(board.choose_session(ss, '/w'))

    def test_当前目录没有会话但全局只有一个时直接选(self):
        self.assertEqual(board.choose_session([{'sessionId': 'a', 'cwd': '/w'}], '/x')['sessionId'], 'a')

    def test_当前目录没有会话且全局有多个时不猜(self):
        ss = [{'sessionId': 'a', 'cwd': '/w'}, {'sessionId': 'b', 'cwd': '/v'}]
        self.assertIsNone(board.choose_session(ss, '/x'))


class 退出规则(unittest.TestCase):
    def test_有页面连接时不退出(self):
        self.assertFalse(board.should_exit(now=10000, last_activity=0, clients=1, idle_limit=1800))

    def test_没有连接但记录还在变时不退出(self):
        self.assertFalse(board.should_exit(now=1000, last_activity=900, clients=0, idle_limit=1800))

    def test_没有连接且超时才退出(self):
        self.assertTrue(board.should_exit(now=10000, last_activity=0, clients=0, idle_limit=1800))


class 服务(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.svc = None
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        if self.svc:
            self.svc.stop()
        self.s.cleanup()

    def start(self, sid='s1', pid=None, **kw):
        self.svc = board.Service(self.s.config, sid, claude_pid=pid, **kw)
        self.svc.start()
        return self.svc

    def sse(self):
        c = SSE(self.svc.port, self.svc.token)
        self.clients.append(c)
        return c

    def t(self):
        return f'?t={self.svc.token}'

    def test_只监听本机(self):
        self.start()
        self.assertEqual(self.svc.address()[0], '127.0.0.1')
        self.assertNotEqual(self.svc.address()[1], 0)

    def test_没有token返回403(self):
        self.start()
        for path in ('/', '/events', '/health'):
            self.assertEqual(get(self.svc.port, path)[0], 403, path)

    def test_错的token返回403(self):
        self.start()
        self.assertEqual(get(self.svc.port, '/health?t=wrong')[0], 403)

    def test_Host不对返回403(self):
        self.start()
        self.assertEqual(get(self.svc.port, '/health' + self.t(), host='evil.example')[0], 403)
        self.assertEqual(get(self.svc.port, '/health' + self.t(), host=f'localhost.evil.example:{self.svc.port}')[0], 403)

    def test_没有写接口(self):
        self.start()
        c = http.client.HTTPConnection('127.0.0.1', self.svc.port, timeout=5)
        c.request('POST', '/health' + self.t(), body='x')
        self.assertIn(c.getresponse().status, (403, 405, 501))
        c.close()

    def test_健康检查不含会话内容(self):
        self.s.append([rec_human('机密内容')])
        self.start()
        status, _, body = get(self.svc.port, '/health' + self.t())
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertTrue(d['ok'])
        self.assertEqual(d['pid'], os.getpid())
        self.assertNotIn('机密内容', body.decode())

    def test_页面设CSP且不加载外部资源(self):
        self.start()
        status, r, body = get(self.svc.port, '/' + self.t())
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", r.getheader('Content-Security-Policy'))
        self.assertEqual(r.getheader('Referrer-Policy'), 'no-referrer')
        html = body.decode()
        self.assertIn('EventSource', html)
        # 内嵌字体的授权说明里本来就有网址；要查的是有没有去外部取资源
        import re
        self.assertIsNone(re.search(r'''(?:src|href)\s*=\s*["']?\s*(?:https?:)?//''', html, re.I))
        self.assertIsNone(re.search(r'''url\(\s*["']?\s*(?:https?:)?//''', html, re.I))
        self.assertIsNone(re.search(r'''@import''', html))

    def test_页面允许内嵌字体(self):
        self.start()
        _, r, _ = get(self.svc.port, '/' + self.t())
        csp = r.getheader('Content-Security-Policy')
        self.assertIn('font-src data:', csp)
        self.assertNotIn('font-src *', csp)

    def test_快照带实时字段(self):
        self.s.append([rec_human(), rec_api()])
        self.start()
        ev, d = self.sse().next()
        self.assertEqual(ev, 'snapshot')
        L = d['live']
        self.assertIsNotNone(L['turn']['start_ts'])
        self.assertEqual(L['split']['phase'], 'model')
        self.assertEqual(L['tools'], [])

    def test_等待开始时刻随增量推送(self):
        self.s.registry(os.getpid(), status='busy')
        self.s.append([rec_human(), rec_api()])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        self.s.registry(os.getpid(), status='waiting', waitingFor='permission prompt')
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['live']['wait']['kind'] == 'permission')
        self.assertIsNotNone(d['state']['live']['wait']['since'])
        self.assertEqual(d['state']['live']['split']['phase'], 'wait')

    def test_状态不变时不反复推送(self):
        self.s.registry(os.getpid(), status='busy')
        self.s.append([rec_human(), rec_api()])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        with self.assertRaises(queue.Empty):
            c.next(timeout=0.8)

    def test_连接时先发快照(self):
        self.s.append([rec_human(), rec_api()])
        self.start()
        ev, d = self.sse().next()
        self.assertEqual(ev, 'snapshot')
        self.assertEqual(d['counters']['rounds'], 1)
        self.assertEqual(d['nodes']['model']['state'], 'active')
        self.assertTrue(d['log'])
        self.assertEqual(len(d['blocks']), 8)

    def test_增量带八大块(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        c.next()
        self.s.append([rec_api()])
        _, d = c.until(lambda e, x: e == 'delta' and x['transitions'])
        model = next(b for b in d['state']['blocks'] if b['id'] == 'model')
        self.assertEqual(model['state'], 'active')

    def test_启动回放不产生增量(self):
        self.s.append([rec_human(), rec_api()])
        self.start()
        c = self.sse()
        c.next()
        with self.assertRaises(queue.Empty):
            c.next(timeout=0.4)

    def test_新记录推送增量(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        c.next()
        self.s.append([rec_api()])
        _, d = c.until(lambda e, x: e == 'delta' and x['transitions'])
        self.assertEqual(d['transitions'][0]['node'], 'model')
        self.assertEqual(d['transitions'][0]['state'], 'active')
        self.assertEqual(d['state']['nodes']['model']['state'], 'active')
        self.assertTrue(d['log'])

    def test_增量延迟不超过1秒(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        c.next()
        t0 = time.time()
        self.s.append([rec_api()])
        c.until(lambda e, x: e == 'delta' and x['transitions'])
        self.assertLess(time.time() - t0, 1.0)

    def test_半行等写完才处理(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        c.next()
        line = json.dumps(rec_api())
        with open(self.s.path(), 'a') as f:
            f.write(line[:10])
        with self.assertRaises(queue.Empty):
            c.next(timeout=0.4)
        with open(self.s.path(), 'a') as f:
            f.write(line[10:] + '\n')
        c.until(lambda e, x: e == 'delta' and x['transitions'])

    def test_坏行不影响后面的记录(self):
        self.start()
        c = self.sse()
        c.next()
        self.s.append(['not json', rec_human()])
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['counters']['rounds'] == 1)
        self.assertEqual(d['state']['session']['bad_lines'], 1)

    def test_记录文件还不存在时等待(self):
        self.start()
        c = self.sse()
        ev, d = c.next()
        self.assertEqual(ev, 'snapshot')
        self.assertEqual(d['counters']['rounds'], 0)
        self.assertFalse(d['session']['transcript_found'])
        self.s.append([rec_human()])
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['counters']['rounds'] == 1)
        self.assertTrue(d['state']['session']['transcript_found'])

    def test_等批准时推送(self):
        self.s.registry(os.getpid(), status='busy')
        self.s.append([rec_human(), rec_api()])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        self.s.registry(os.getpid(), status='waiting', waitingFor='permission prompt')
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['nodes']['permission']['state'] == 'waiting')
        self.assertEqual(d['state']['nodes']['permission']['label'], '等你批准')

    def test_等人1秒内可见(self):
        self.s.registry(os.getpid(), status='busy')
        self.s.append([rec_human(), rec_api()])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        t0 = time.time()
        self.s.registry(os.getpid(), status='waiting', waitingFor='input needed')
        c.until(lambda e, x: e == 'delta' and x['state']['nodes']['tool']['state'] == 'waiting')
        self.assertLess(time.time() - t0, 1.0)

    def test_读不到进程文件时标不可用(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        wait_until(lambda: self.svc.snapshot()['session']['proc_available'] is False)
        self.assertFalse(self.svc.snapshot()['session']['proc_available'])
        c.close()

    def test_启动时按会话id找到进程(self):
        self.s.registry(os.getpid(), sessionId='s1', status='busy')
        self.start()
        self.assertTrue(wait_until(lambda: self.svc.snapshot()['session']['proc_status'] == 'busy'))

    def test_子agent的记录和结束(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        c.next()
        base = self.s.path()[:-6] + '/subagents'
        os.makedirs(base)
        with open(base + '/agent-a1.meta.json', 'w') as f:
            json.dump({'agentType': 'Explore', 'description': '查配置', 'toolUseId': 'tA'}, f)
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['nodes']['agent']['state'] == 'active')
        self.assertEqual(d['state']['agents'][0]['description'], '查配置')
        with open(base + '/agent-a1.jsonl', 'w') as f:
            f.write(json.dumps({'type': 'api-request', 'querySource': 'agent:builtin:Explore'}) + '\n')
        c.until(lambda e, x: e == 'delta' and x['state']['agents'][0]['step'] == '调用模型')
        with open(base + '/agent-a1.meta.json', 'w') as f:
            json.dump({'agentType': 'Explore', 'description': '查配置', 'toolUseId': 'tA', 'finished': True}, f)
        c.until(lambda e, x: e == 'delta' and x['state']['nodes']['agent']['state'] == 'done')

    def test_启动时读入已有的子agent(self):
        self.s.append([rec_human()])
        base = self.s.path()[:-6] + '/subagents'
        os.makedirs(base)
        with open(base + '/agent-a1.meta.json', 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'x', 'toolUseId': 'tA', 'finished': True}, f)
        with open(base + '/agent-a1.jsonl', 'w') as f:
            f.write(json.dumps({'type': 'api-request', 'querySource': 'agent:builtin:Explore'}) + '\n')
        self.start()
        self.assertEqual(self.svc.snapshot()['nodes']['agent']['state'], 'done')

    def test_同一进程换会话时跟到新文件(self):
        self.s.registry(os.getpid(), sessionId='s1')
        self.s.append([rec_human('旧')])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        self.s.append([rec_human('新一'), rec_human('新二')], sid='s2')
        self.s.registry(os.getpid(), sessionId='s2')
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['session']['session_id'] == 's2')
        self.assertEqual(d['state']['counters']['rounds'], 2)
        self.assertEqual(d['log'][-1]['step'], '会话已切换')
        self.s.append([rec_human('新三')], sid='s2')
        c.until(lambda e, x: e == 'delta' and x['state']['counters']['rounds'] == 3)

    def test_换回旧会话时接着读旧文件(self):
        self.s.registry(os.getpid(), sessionId='s1')
        self.s.append([rec_human('一')])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        self.s.registry(os.getpid(), sessionId='s2')
        c.until(lambda e, x: e == 'delta' and x['state']['session']['session_id'] == 's2')
        self.s.registry(os.getpid(), sessionId='s1')
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['session']['session_id'] == 's1')
        self.assertEqual(d['state']['counters']['rounds'], 1)
        self.s.append([rec_human('二')])
        c.until(lambda e, x: e == 'delta' and x['state']['counters']['rounds'] == 2)

    def test_换到还没有记录文件的会话时等待(self):
        self.s.registry(os.getpid(), sessionId='s1')
        self.s.append([rec_human()])
        self.start(pid=os.getpid())
        c = self.sse()
        c.next()
        self.s.registry(os.getpid(), sessionId='s2')
        _, d = c.until(lambda e, x: e == 'delta' and x['state']['session']['session_id'] == 's2')
        self.assertFalse(d['state']['session']['transcript_found'])
        self.s.append([rec_human()], sid='s2')
        c.until(lambda e, x: e == 'delta' and x['state']['counters']['rounds'] == 1)

    def test_进程退出后通知页面并结束服务(self):
        p = subprocess.Popen(['sleep', '60'])
        try:
            self.s.registry(p.pid)
            self.s.append([rec_human()])
            self.start(pid=p.pid, exit_delay=0.3)
            c = self.sse()
            c.next()
            p.kill()
            p.wait()
            c.until(lambda e, x: e == 'delta' and x['state']['session']['proc_status'] == 'exited')
            self.assertTrue(self.svc.stopped.wait(5))
        finally:
            p.kill()

    def test_没有连接且超时后退出(self):
        self.s.append([rec_human()])
        self.start(idle_limit=0.3)
        self.assertTrue(self.svc.stopped.wait(5))

    def test_有连接时不会超时退出(self):
        self.s.append([rec_human()])
        self.start(idle_limit=0.3)
        self.sse().next()
        self.assertFalse(self.svc.stopped.wait(1.0))

    def test_不修改会话文件和配置(self):
        self.s.append([rec_human(), rec_api()])
        settings = os.path.join(self.s.config, 'settings.json')
        with open(settings, 'w') as f:
            f.write('{}')
        self.s.registry(os.getpid())
        def digest():
            out = {}
            for r, _, fs in os.walk(self.s.config):
                for n in fs:
                    p = os.path.join(r, n)
                    with open(p, 'rb') as f:
                        out[p] = (hashlib.sha256(f.read()).hexdigest(), os.stat(p).st_mtime_ns)
            return out
        before = digest()
        self.start(pid=os.getpid())
        self.sse().next()
        time.sleep(0.5)
        self.svc.stop()
        self.assertEqual(digest(), before)

    def test_断线重连时再发快照(self):
        self.s.append([rec_human()])
        self.start()
        self.sse().next()
        ev, _ = self.sse().next()
        self.assertEqual(ev, 'snapshot')

    def test_客户端断开后计数减少(self):
        self.s.append([rec_human()])
        self.start()
        c = self.sse()
        c.next()
        self.assertTrue(wait_until(lambda: self.svc.client_count() == 1))
        c.close()
        self.s.append([rec_api()])
        self.assertTrue(wait_until(lambda: self.svc.client_count() == 0, timeout=20))

    # ---------- 15 号件（第 7 步独立审查）的回归测试 ----------

    def test_R1_最后一句和finished同一轮写下时子agent判结束(self):
        self.s.append([rec_human()])
        self.start()
        base = self.s.path()[:-6] + '/subagents'
        os.makedirs(base)
        meta = base + '/agent-a1.meta.json'
        with open(meta, 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'd', 'toolUseId': 'tA'}, f)
        with open(base + '/agent-a1.jsonl', 'a') as f:
            f.write(json.dumps({'type': 'api-request', 'querySource': 'agent:builtin:Explore', 'timestamp': '2026-11-06T10:00:02.000Z'}) + '\n')
        self.assertTrue(wait_until(lambda: self.svc.snapshot()['agents'] and self.svc.snapshot()['agents'][0]['step'] == '调用模型'))
        with self.svc._lock:  # 两次写入落在同一个轮询周期里
            with open(base + '/agent-a1.jsonl', 'a') as f:
                f.write(json.dumps({'type': 'assistant', 'requestId': 'ra', 'timestamp': board.time.strftime('%Y-%m-%dT%H:%M:%S.000Z', board.time.gmtime()),
                                    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'x'}]}}) + '\n')
            with open(meta, 'w') as f:
                json.dump({'agentType': 'Explore', 'description': 'd', 'toolUseId': 'tA', 'finished': True}, f)
        self.assertTrue(wait_until(lambda: self.svc.snapshot()['agents'][0]['state'] == 'done'))
        time.sleep(0.8)
        self.assertEqual(self.svc.snapshot()['agents'][0]['state'], 'done')
        self.assertEqual(self.svc.snapshot()['nodes']['agent']['state'], 'done')

    def test_R3_换到还没有记录文件的会话时旧子agent不混进来(self):
        self.s.registry(os.getpid(), sessionId='s1')
        self.s.append([rec_human()])
        base = self.s.path()[:-6] + '/subagents'
        os.makedirs(base)
        with open(base + '/agent-old.jsonl', 'w') as f:
            f.write(json.dumps({'type': 'api-request', 'querySource': 'agent:builtin:Explore'}) + '\n')
        self.start(pid=os.getpid())
        self.s.registry(os.getpid(), sessionId='s2')
        self.assertTrue(wait_until(lambda: self.svc.snapshot()['session']['session_id'] == 's2'))
        with open(base + '/agent-old.jsonl', 'a') as f:
            f.write(json.dumps({'type': 'api-request', 'querySource': 'agent:builtin:Explore'}) + '\n')
        self.s.append([rec_human()], sid='s2')
        self.assertTrue(wait_until(lambda: self.svc.snapshot()['counters']['rounds'] == 1))
        time.sleep(0.6)
        self.assertEqual(self.svc.snapshot()['agents'], [])

    def test_R5_记录文件变短后整份重建计数不翻倍(self):
        self.s.append([rec_human('一'), rec_human('二')])
        self.start()
        self.assertEqual(self.svc.snapshot()['counters']['rounds'], 2)
        with open(self.s.path(), 'w') as f:
            f.write(json.dumps(rec_human('一')) + '\n')
        # 等服务读到「变短」这一刻，再追加；否则长度又回到原来，读取器察觉不到
        self.assertTrue(wait_until(lambda: self.svc.snapshot()['counters']['rounds'] == 1))
        self.s.append([rec_human('二')])
        time.sleep(0.8)
        self.assertEqual(self.svc.snapshot()['counters']['rounds'], 2)

    def test_R6_算快照出错时连接计数不泄漏(self):
        self.s.append([rec_human()])
        self.start()
        real = self.svc.snapshot

        def boom(*a, **k):
            raise TypeError('模拟字段形状不对')
        self.svc.snapshot = boom
        try:
            conn = http.client.HTTPConnection('127.0.0.1', self.svc.port, timeout=5)
            conn.request('GET', f'/events?t={self.svc.token}', headers={'Host': f'127.0.0.1:{self.svc.port}'})
            conn.getresponse().read()
            conn.close()
        finally:
            self.svc.snapshot = real
        time.sleep(0.3)
        self.assertEqual(self.svc.client_count(), 0)

    def test_R17_请求有读超时(self):
        self.start()
        self.assertEqual(self.svc._handler().timeout, 10)


class 命令行(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        # 0.2.0 起终端提示分中英文，这组测试按中文断言，固定成中文
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=self.s.config, XDG_CACHE_HOME=self.s.state, HARNESS_BOARD_LANG='zh')
        self.env.pop('CLAUDE_CODE_SESSION_ID', None)
        self.pids = []

    def tearDown(self):
        for f in os.listdir(os.path.join(self.s.state, 'harness-board')) if os.path.isdir(os.path.join(self.s.state, 'harness-board')) else []:
            if f.endswith('.json'):
                try:
                    os.kill(load_json(os.path.join(self.s.state, 'harness-board', f))['pid'], signal.SIGTERM)
                except Exception:
                    pass
        self.s.cleanup()

    def run_board(self, *args, env=None, timeout=20, input=None):
        return subprocess.run([sys.executable, BOARD, *args], capture_output=True, text=True, timeout=timeout,
                              env=env or self.env, input=input if input is not None else '', cwd=self.s.tmp)

    def state_files(self):
        d = os.path.join(self.s.state, 'harness-board')
        return [os.path.join(d, f) for f in os.listdir(d) if f.endswith('.json')] if os.path.isdir(d) else []

    def state(self):
        files = self.state_files()
        self.assertEqual(len(files), 1)
        return load_json(files[0])

    def test_启动复用停止(self):
        self.s.append([rec_human()])
        r = self.run_board('--session', 's1', '--no-open', 'start')
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.state()
        self.assertIn(f"http://127.0.0.1:{st['port']}/?t={st['token']}", r.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(self.state_files()[0]).st_mode), 0o600)
        self.assertEqual(get(st['port'], f"/health?t={st['token']}")[0], 200)
        r2 = self.run_board('--session', 's1', '--no-open', 'start')
        self.assertEqual(self.state()['port'], st['port'])
        self.assertIn('复用', r2.stdout)
        r3 = self.run_board('--session', 's1', 'status')
        self.assertIn('运行中', r3.stdout)
        r4 = self.run_board('--session', 's1', 'stop')
        self.assertEqual(r4.returncode, 0)
        self.assertTrue(wait_until(lambda: not self.state_files()))
        with self.assertRaises(OSError):
            get(st['port'], f"/health?t={st['token']}")

    def test_代码更新后重启而不是复用旧服务(self):
        self.s.append([rec_human()])
        self.run_board('--session', 's1', '--no-open', 'start')
        path = self.state_files()[0]
        old = load_json(path)
        stale = dict(old, build='旧版本')
        with open(path, 'w') as f:
            json.dump(stale, f)
        r = self.run_board('--session', 's1', '--no-open', 'start')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('复用', r.stdout)
        new = self.state()
        self.assertNotEqual(new['pid'], old['pid'])
        self.assertTrue(wait_until(lambda: not board._pid_alive(old['pid'])))

    def test_R15_运行时不在包目录写缓存文件(self):
        d = tempfile.mkdtemp()
        try:
            shutil.copytree(SCRIPTS, os.path.join(d, 'scripts'), ignore=shutil.ignore_patterns('__pycache__'))
            r = subprocess.run([sys.executable, os.path.join(d, 'scripts', 'board.py'), '--help'], capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(d, 'scripts', '__pycache__')))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_R16_浏览器打开成功时不输出令牌(self):
        import contextlib
        import io
        from unittest import mock
        orig = board._open_browser
        board._open_browser = lambda url: True
        try:
            with mock.patch.dict(os.environ, {'HARNESS_BOARD_LANG': 'zh'}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    board._announce('http://127.0.0.1:1234/?t=secret', 'started', False)
                self.assertNotIn('secret', out.getvalue())
                self.assertIn('已在浏览器打开', out.getvalue())
                board._open_browser = lambda url: False
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    board._announce('http://127.0.0.1:1234/?t=secret', 'started', False)
                self.assertIn('?t=secret', out.getvalue())  # 打不开时要给网址，才能手动打开
        finally:
            board._open_browser = orig

    def test_不带子命令等于启动(self):
        self.s.append([rec_human()])
        r = self.run_board('--session', 's1', '--no-open')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(get(self.state()['port'], f"/health?t={self.state()['token']}")[0], 200)

    def test_服务脱离父进程继续运行(self):
        self.s.append([rec_human()])
        self.run_board('--session', 's1', '--no-open', 'start')
        time.sleep(0.5)
        st = self.state()
        self.assertEqual(get(st['port'], f"/health?t={st['token']}")[0], 200)
        self.assertNotEqual(st['pid'], os.getpid())
        self.assertEqual(os.getsid(st['pid']), st['pid'])

    def test_新会话记录文件还不存在时也能启动(self):
        r = self.run_board('--session', 'fresh', '--no-open', 'start')
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.state()
        self.assertEqual(get(st['port'], f"/health?t={st['token']}")[0], 200)

    def test_用环境变量作会话id(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='s9')
        r = self.run_board('--no-open', 'start', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state()['session_id'], 's9')

    def test_没有会话id时报错(self):
        r = self.run_board('--no-open', 'start')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('会话', r.stdout + r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_未知子命令报错(self):
        r = self.run_board('--session', 's1', 'bogus')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('Traceback', r.stderr)

    def test_没有在运行时stop也正常(self):
        r = self.run_board('--session', 's1', 'stop')
        self.assertEqual(r.returncode, 0)
        self.assertIn('没有', r.stdout)

    def test_用进程号找到同一个服务(self):
        self.s.registry(os.getpid(), sessionId='s1')
        self.s.append([rec_human()])
        self.run_board('--session', 's1', '--no-open', 'start')
        port = self.state()['port']
        self.s.registry(os.getpid(), sessionId='s2')
        r = self.run_board('--session', 's2', '--no-open', 'start')
        self.assertIn('复用', r.stdout)
        self.assertEqual(self.state()['port'], port)

    def test_pick只有一个会话时直接打开(self):
        self.s.registry(os.getpid(), sessionId='s1', cwd=self.s.tmp)
        r = self.run_board('--no-open', 'pick')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state()['session_id'], 's1')
        self.assertEqual(self.state()['claude_pid'], os.getpid())

    def test_pick有多个时非交互列出后退出(self):
        p = subprocess.Popen(['sleep', '30'])
        try:
            self.s.registry(os.getpid(), sessionId='s1', cwd='/w1')
            self.s.registry(p.pid, sessionId='s2', cwd='/w2')
            r = self.run_board('--no-open', 'pick', input='')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('/w1', r.stdout)
            self.assertIn('/w2', r.stdout)
            r2 = self.run_board('--no-open', 'pick', '2')
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertEqual(len(self.state_files()), 1)
        finally:
            p.kill()
            p.wait()

    def test_pick没有运行中的会话时说明(self):
        r = self.run_board('--no-open', 'pick')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('没有', r.stdout)
        self.assertNotIn('Traceback', r.stderr)

    def test_doctor输出统计不含内容(self):
        self.s.append([rec_human('机密内容')])
        r = self.run_board('doctor')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('records', r.stdout)
        self.assertNotIn('机密内容', r.stdout)

    def test_大记录文件3秒内可用(self):
        line = json.dumps(rec_human('x' * 1000))
        with open(self.s.path(), 'w') as f:
            f.write((line + '\n') * 7000)
        t0 = time.time()
        r = self.run_board('--session', 's1', '--no-open', 'start')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t0, 3.0)
        st = self.state()
        self.assertEqual(get(st['port'], f"/health?t={st['token']}")[0], 200)


class 平台和语言(unittest.TestCase):
    """0.2.0：Windows 直接退出；终端提示按系统语言出中文或英文；页面标题和默认语言。"""

    def test_强制语言优先(self):
        mac = lambda: 'zh-Hans-CN'  # noqa: E731
        self.assertEqual(board.ui_lang({'HARNESS_BOARD_LANG': 'en', 'LANG': 'zh_CN.UTF-8'}, 'darwin', mac), 'en')
        self.assertEqual(board.ui_lang({'HARNESS_BOARD_LANG': ' ZH ', 'LANG': 'en_US.UTF-8'}, 'linux'), 'zh')
        self.assertEqual(board.ui_lang({'HARNESS_BOARD_LANG': 'fr', 'LANG': 'zh_CN.UTF-8'}, 'linux'), 'zh')  # 不认的值不算

    def test_macOS看系统界面语言而不是LANG(self):
        self.assertEqual(board.ui_lang({'LANG': 'en_US.UTF-8'}, 'darwin', lambda: 'zh-Hans-US'), 'zh')
        self.assertEqual(board.ui_lang({'LANG': 'zh_CN.UTF-8'}, 'darwin', lambda: 'en-US'), 'en')
        self.assertEqual(board.ui_lang({'LANG': 'zh_CN.UTF-8'}, 'darwin', lambda: None), 'zh')  # 读不到再看 LANG

    def test_其他系统按LC_ALL_LC_MESSAGES_LANG的顺序(self):
        never = lambda: self.fail('不是 macOS 不该读界面语言')  # noqa: E731
        self.assertEqual(board.ui_lang({'LC_ALL': 'en_US.UTF-8', 'LANG': 'zh_CN.UTF-8'}, 'linux', never), 'en')
        self.assertEqual(board.ui_lang({'LC_MESSAGES': 'zh_TW.UTF-8', 'LANG': 'en_US.UTF-8'}, 'linux', never), 'zh')
        self.assertEqual(board.ui_lang({'LANG': 'zh_CN.UTF-8'}, 'linux', never), 'zh')
        self.assertEqual(board.ui_lang({'LANG': 'C.UTF-8'}, 'linux', never), 'en')
        self.assertEqual(board.ui_lang({}, 'linux', never), 'en')

    def test_读macOS偏好文件(self):
        import plistlib
        from unittest import mock
        home = tempfile.mkdtemp()
        try:
            pref = os.path.join(home, 'Library', 'Preferences')
            os.makedirs(pref)
            path = os.path.join(pref, '.GlobalPreferences.plist')
            with open(path, 'wb') as f:
                plistlib.dump({'AppleLanguages': ['zh-Hans-CN', 'en-US']}, f, fmt=plistlib.FMT_BINARY)
            with mock.patch.dict(os.environ, {'HOME': home}):
                self.assertEqual(board._mac_ui_lang(), 'zh-Hans-CN')
                with open(path, 'wb') as f:
                    f.write(b'not a plist')
                self.assertIsNone(board._mac_ui_lang())
                os.remove(path)
                self.assertIsNone(board._mac_ui_lang())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_每条提示都有中英两份且占位符一致(self):
        import string
        fields = lambda s: sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)  # noqa: E731
        for key, (zh, en) in board.MSG.items():
            self.assertTrue(zh and en, key)
            self.assertEqual(fields(zh), fields(en), key)
            self.assertIsNone(re.search(r'[一-鿿]', en), ('英文里混了中文', key))

    def test_源码里没有写死的中文提示(self):
        with open(BOARD, encoding='utf-8') as f:
            lines = f.read().splitlines()
        bad = [(i, l) for i, l in enumerate(lines, 1)
               if re.search(r'\b(print|input)\(|help=|description=', l) and re.search(r'[一-鿿]', l)]
        self.assertEqual(bad, [])

    def test_按语言出提示(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {'HARNESS_BOARD_LANG': 'en'}):
            self.assertEqual(board.T('stopped'), 'Board stopped.')
            self.assertEqual(board.T('running', url='U'), 'Board running: U')
        with mock.patch.dict(os.environ, {'HARNESS_BOARD_LANG': 'zh'}):
            self.assertEqual(board.T('stopped'), '看板已停止。')

    def test_命令行英文提示(self):
        s = Sandbox()
        try:
            env = dict(os.environ, CLAUDE_CONFIG_DIR=s.config, XDG_CACHE_HOME=s.state, HARNESS_BOARD_LANG='en')
            env.pop('CLAUDE_CODE_SESSION_ID', None)
            r = subprocess.run([sys.executable, BOARD, '--session', 's1', 'status'], capture_output=True, text=True, timeout=20, env=env)
            self.assertEqual(r.stdout.strip(), 'No board is running for this session.')
            r = subprocess.run([sys.executable, BOARD, 'status'], capture_output=True, text=True, timeout=20, env=env)
            self.assertIn("Can't find the current session id", r.stderr)
            r = subprocess.run([sys.executable, BOARD, '--help'], capture_output=True, text=True, timeout=20, env=env)
            self.assertIsNone(re.search(r'[一-鿿]', r.stdout), r.stdout)
        finally:
            s.cleanup()

    def test_不是POSIX时直接退出且不碰任何进程(self):
        import contextlib
        import io
        from unittest import mock
        boom = mock.Mock(side_effect=AssertionError('不该查进程'))
        with mock.patch.object(board, 'platform_ok', return_value=False), \
                mock.patch.object(board, '_pid_alive', boom), mock.patch.object(board, 'list_sessions', boom), \
                mock.patch.dict(os.environ, {'HARNESS_BOARD_LANG': 'en'}):
            for argv in (['--session', 's1', 'status'], ['pick'], ['--session', 's1', 'stop'], ['--serve', '--session', 's1']):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(board.main(argv), 2, argv)
                self.assertIn('macOS and Linux only', err.getvalue())
        boom.assert_not_called()
        self.assertTrue(board.platform_ok())  # 跑测试的这台是 macOS 或 Linux

    def test_没有写权限时说清原因不甩报错(self):
        """Claude Code 沙箱挡住写 ~/.cache/harness-board 时（0.2.0 驱动终端实测）：一句话说明，退出码 3。"""
        import contextlib
        import io
        from unittest import mock
        s = Sandbox()
        try:
            denied = PermissionError(1, 'Operation not permitted', '/denied/harness-board/x.json.err')
            with mock.patch.object(board, 'state_dir', side_effect=denied), \
                    mock.patch.dict(os.environ, {'HARNESS_BOARD_LANG': 'en', 'CLAUDE_CONFIG_DIR': s.config}):
                for argv in (['--session', 's1', '--no-open', 'start'], ['--session', 's1', 'stop'], ['--session', 's1', 'status']):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(board.main(argv), 3, argv)
                    self.assertIn('/denied/harness-board/x.json.err', err.getvalue())
                    self.assertIn('pick', err.getvalue())
                    self.assertNotIn('Traceback', err.getvalue())
        finally:
            s.cleanup()

    def test_页面标题和默认语言(self):
        with open(board.ASSET, encoding='utf-8') as f:
            html = f.read()
        self.assertEqual(re.findall(r'<title>(.*?)</title>', html), ['harness-board'])
        i = html.index('I18N.lang=(()=>')
        line = html[i:html.index('\n', i)]
        self.assertIn('navigator.language', line)
        self.assertLess(line.index("localStorage.getItem('hb-lang')"), line.index('navigator.language'))  # 用户切过的优先

    def test_包里带许可证和字体授权说明(self):
        pkg = os.path.dirname(SCRIPTS)
        with open(os.path.join(pkg, 'LICENSE'), encoding='utf-8') as f:
            lic = f.read()
        self.assertIn('MIT License', lic)
        self.assertIn('SIL Open Font License', lic)
        with open(os.path.join(pkg, 'assets', '字体授权', 'README.md'), encoding='utf-8') as f:
            self.assertIn('Font licenses (English)', f.read())


if __name__ == '__main__':
    unittest.main(verbosity=1)
