#!/usr/bin/env python3
"""cc_records.py 的样本测试，覆盖 05 号件 §六。

样本是按真实记录形态手写的合成记录，不含任何真实会话内容。运行：
    python3 tests/test_cc_records.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, '..', 'harness-board', 'scripts')))

from cc_records import Board, LineReader, doctor  # noqa: E402

_clock = [1000.0]


def iso():
    from datetime import datetime, timezone
    _clock[0] += 1
    return datetime.fromtimestamp(_clock[0], timezone.utc).isoformat().replace('+00:00', 'Z')


def human(text):
    return {'type': 'user', 'origin': {'kind': 'human'}, 'message': {'role': 'user', 'content': text}, 'timestamp': iso()}


def bare(text, **kw):
    """没有 origin 的 user 记录。"""
    return dict({'type': 'user', 'message': {'role': 'user', 'content': text}, 'timestamp': iso()}, **kw)


def notif(tool_use_id='', status='completed'):
    body = f'<task-notification>\n<task-id>t1</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n<status>{status}</status>\n<summary>完成</summary>\n</task-notification>'
    return {'type': 'user', 'origin': {'kind': 'task-notification'}, 'message': {'role': 'user', 'content': body}, 'timestamp': iso()}


def att(kind, **kw):
    return {'type': 'attachment', 'attachment': dict({'type': kind}, **kw), 'timestamp': iso()}


def api(source='repl_main_thread'):
    return {'type': 'api-request', 'querySource': source, 'version': '2.1.318', 'timestamp': iso()}


def text(req='r1', usage=None, **kw):
    m = {'role': 'assistant', 'content': [{'type': 'text', 'text': '回答'}]}
    if usage:
        m['usage'] = usage
    return dict({'type': 'assistant', 'requestId': req, 'message': m, 'timestamp': iso()}, **kw)


def think(req='r1'):
    return {'type': 'assistant', 'requestId': req, 'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': ''}]}, 'timestamp': iso()}


def use(tid='t1', name='Bash', inp=None, req='r1'):
    return {'type': 'assistant', 'requestId': req, 'timestamp': iso(),
            'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp or {'command': 'ls'}}]}}


def result(tid='t1', err=False, decision='accept', source='config', reason='classifier', code=None, tur=None):
    r = {'type': 'user', 'timestamp': iso(),
         'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'is_error': err, 'content': '工具结果正文'}]}}
    if decision:
        pd = {'decision': decision, 'source': source}
        if reason:
            pd['reasonType'] = reason
        if code:
            pd['reasonCode'] = code
        r['permissionDecision'] = pd
    if tur is not None:
        r['toolUseResult'] = tur
    return r


def turn_end(ms=1000):
    return {'type': 'system', 'subtype': 'turn_duration', 'durationMs': ms, 'timestamp': iso()}


def boundary(trigger='auto'):
    return {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': iso(),
            'compactMetadata': {'trigger': trigger, 'preTokens': 50000, 'postTokens': 12000, 'durationMs': 20000}}


def queued(mode, body):
    return att('queued_command', commandMode=mode, prompt=body)


def feed(b, *recs):
    for r in recs:
        b.feed_record(r)


def st(b, node):
    return b.snapshot()['nodes'][node]['state']


def lb(b, node):
    return b.snapshot()['nodes'][node].get('label')


def cnt(b, key):
    return b.snapshot()['counters'][key]


class 主流程(unittest.TestCase):
    def test_纯问答(self):
        b = Board()
        feed(b, human('你好'))
        self.assertEqual(st(b, 'prompt'), 'done')
        self.assertEqual(cnt(b, 'rounds'), 1)
        feed(b, att('environment'))
        self.assertEqual(st(b, 'context'), 'done')
        feed(b, api())
        self.assertEqual(st(b, 'model'), 'active')
        feed(b, text(), turn_end())
        self.assertEqual(st(b, 'model'), 'done')
        self.assertEqual(st(b, 'turn'), 'done')
        self.assertEqual(st(b, 'tool'), 'idle')

    def test_单工具循环(self):
        b = Board()
        feed(b, human('跑测试'), api(), use())
        self.assertEqual(st(b, 'model'), 'done')
        self.assertEqual(st(b, 'tool'), 'active')
        self.assertEqual(cnt(b, 'inflight'), 1)
        feed(b, result())
        self.assertEqual(st(b, 'tool'), 'done')
        self.assertEqual(st(b, 'permission'), 'passed')
        self.assertEqual(lb(b, 'permission'), '分类器放行')
        feed(b, api())
        self.assertEqual(st(b, 'model'), 'active')
        self.assertEqual(cnt(b, 'loop'), 2)
        feed(b, text(req='r2'), turn_end())
        self.assertEqual(st(b, 'turn'), 'done')
        self.assertEqual(cnt(b, 'tool_calls'), 1)
        self.assertEqual(cnt(b, 'model_calls'), 2)

    def test_并行工具结果乱序到达(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), use('b'))
        self.assertEqual(cnt(b, 'inflight'), 2)
        feed(b, result('b'))
        self.assertEqual(st(b, 'tool'), 'active')
        feed(b, result('a'))
        self.assertEqual(st(b, 'tool'), 'done')
        self.assertEqual(cnt(b, 'inflight'), 0)

    def test_工具出错(self):
        b = Board()
        feed(b, human('x'), api(), use(), result(err=True))
        self.assertEqual(st(b, 'tool'), 'error')
        self.assertEqual(st(b, 'permission'), 'passed')

    def test_批次里有一个出错就算出错(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), use('b'), result('a', err=True), result('b'))
        self.assertEqual(st(b, 'tool'), 'error')

    def test_新一批工具不受上一批出错影响(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), result('a', err=True), api(), use('b', req='r2'), result('b'))
        self.assertEqual(st(b, 'tool'), 'done')

    def test_用户拒绝不算出错(self):
        b = Board()
        feed(b, human('x'), api(), use(), result(err=True, decision='reject', source='user_reject'))
        self.assertEqual(st(b, 'tool'), 'rejected')
        self.assertEqual(st(b, 'permission'), 'rejected')
        self.assertEqual(lb(b, 'permission'), '你拒绝了')

    def test_规则拒绝不算出错(self):
        b = Board()
        feed(b, human('x'), api(), use(), result(err=True, decision='reject', source='config'))
        self.assertEqual(st(b, 'tool'), 'rejected')
        self.assertEqual(lb(b, 'permission'), '规则拒绝')

    def test_被环境拦下的调用算出错(self):
        b = Board()
        feed(b, human('x'), api(), use(), result(err=True, decision=None))
        self.assertEqual(st(b, 'tool'), 'error')

    def test_用户批准的来源文字(self):
        b = Board()
        feed(b, human('x'), api(), use(), result(source='user_temporary'))
        self.assertEqual(lb(b, 'permission'), '你批准了')

    def test_API错误(self):
        b = Board()
        feed(b, human('x'), api(), text(isApiErrorMessage=True))
        self.assertEqual(st(b, 'model'), 'error')
        feed(b, turn_end())
        self.assertEqual(st(b, 'model'), 'error')
        self.assertEqual(st(b, 'turn'), 'done')

    def test_新prompt重置主流程(self):
        b = Board()
        feed(b, human('一'), att('environment'), api(), use(), result(), text(req='r2'), turn_end())
        feed(b, human('二'))
        self.assertEqual(st(b, 'tool'), 'idle')
        self.assertEqual(st(b, 'context'), 'idle')
        self.assertEqual(st(b, 'turn'), 'idle')
        self.assertEqual(cnt(b, 'rounds'), 2)
        self.assertEqual(cnt(b, 'loop'), 0)

    def test_新prompt清空残留的在途工具(self):
        b = Board()
        feed(b, human('一'), api(), use())
        feed(b, human('二'))
        self.assertEqual(cnt(b, 'inflight'), 0)

    def test_后台通知之后有模型调用才开回合且不加轮次(self):
        b = Board()
        feed(b, human('一'), api(), text(), turn_end(), notif(), api())
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(st(b, 'turn'), 'idle')
        self.assertEqual(lb(b, 'prompt'), '后台通知')
        live = b.snapshot()['live']['turn']
        self.assertIsNotNone(live['start_ts'])
        self.assertIsNone(live['end_ts'])
        self.assertEqual(b._in['source'], '后台通知')

    def test_回合结束后才到的后台通知不开回合(self):
        # 真实记录：回合结束标记之后才补写通知，之后没有模型调用（13 号件 §三）
        b = Board()
        feed(b, human('一'), api(), text(), turn_end(), notif())
        live = b.snapshot()['live']['turn']
        self.assertIsNotNone(live['end_ts'])
        self.assertEqual(st(b, 'turn'), 'done')
        self.assertEqual(b._in['source'], '你输入')
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_后台通知之后接你的输入按输入开回合(self):
        b = Board()
        feed(b, human('一'), api(), text(), turn_end(), notif(), human('二'), api())
        self.assertEqual(cnt(b, 'rounds'), 2)
        self.assertEqual(b._in['source'], '你输入')
        self.assertIsNone(b.snapshot()['live']['turn']['end_ts'])

    def test_通知晚于已开始的模型调用写下时当场开回合(self):
        # 真实记录：回合结束后模型调用先写，通知晚几十毫秒才写；下一次模型调用不能清掉在途工具
        b = Board()
        feed(b, human('一'), api(), text(), turn_end(), api(), notif())
        self.assertEqual(cnt(b, 'loop'), 1)
        self.assertEqual(st(b, 'model'), 'active')
        feed(b, use(tid='tX', req='r2'), api(), result(tid='tX'))
        self.assertEqual(b.stats['inflight_underflow'], 0)
        self.assertEqual(lb(b, 'prompt'), '后台通知')
        self.assertEqual(cnt(b, 'inflight'), 0)
        self.assertEqual(cnt(b, 'loop'), 2)

    def test_重试的请求不算新的一次调用(self):
        b = Board()
        feed(b, human('一'), api(), api())
        self.assertEqual(cnt(b, 'loop'), 1)
        feed(b, text(req='rA'), api(), text(req='rB'), turn_end())
        self.assertEqual(cnt(b, 'loop'), 2)
        self.assertEqual(b.stats['api_retries'], 1)

    def test_没有api_request的调用在回复时补算(self):
        b = Board()
        feed(b, human('一'), api(), text(req='rA'), use(tid='t1', req='rA'), result(tid='t1'))
        feed(b, text(req='rB'))  # 第二次调用没有 api-request
        self.assertEqual(cnt(b, 'loop'), 2)
        self.assertEqual(st(b, 'model'), 'active')

    def test_prompt记录晚于模型调用写下时那次调用归新回合(self):
        b = Board()
        feed(b, human('一'), api(), text(), turn_end())
        feed(b, api(), human('二'), text(req='r2'))
        self.assertEqual(cnt(b, 'rounds'), 2)
        self.assertEqual(cnt(b, 'loop'), 1)
        self.assertEqual(b._in['source'], '你输入')

    def test_没有开回合记录的模型调用开自动继续回合(self):
        b = Board()
        feed(b, human('一'), api(), text(), turn_end())
        e = api()
        feed(b, e, text(req='r2'))
        from cc_records import _ts
        self.assertEqual(b._in['source'], '自动继续')
        self.assertEqual(live(b)['turn']['start_ts'], _ts(e))
        self.assertIsNone(live(b)['turn']['end_ts'])
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(cnt(b, 'loop'), 1)

    def test_运行中按Esc的工具算被中断不算出错(self):
        b = Board()
        feed(b, human('一'), api(), use(tid='tz'))
        r = result(tid='tz', err=True)
        r.pop('permissionDecision', None)
        r['toolUseResult'] = 'User rejected tool use'
        feed(b, r, bare('[Request interrupted by user for tool use]'))
        t = [x for x in live(b)['tools'] if x['id'] == 'tz'][0]
        self.assertEqual(t['state'], 'interrupted')
        self.assertEqual(st(b, 'tool'), 'interrupted')
        self.assertEqual(live(b)['drawer']['tools']['errors_turn'], 0)
        self.assertTrue(any('（被中断）' in e['summary'] for e in b.snapshot()['log'] if e['step'] == '工具结束'))

    def test_等批准时点不算你拒绝了(self):
        b = Board()
        feed(b, human('一'), api(), use(tid='ty'))
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        r = result(tid='ty', err=True)
        r.pop('permissionDecision', None)
        r['toolUseResult'] = 'User rejected tool use'
        feed(b, r)
        t = [x for x in live(b)['tools'] if x['id'] == 'ty'][0]
        self.assertEqual(t['state'], 'rejected')

    def test_中断回合的用时取实际经过的时间(self):
        from cc_records import _ts
        b = Board()
        p = human('一')
        feed(b, p, api(), use())
        intr = bare('[Request interrupted by user for tool use]')
        feed(b, intr, turn_end(999))
        self.assertAlmostEqual(live(b)['turn']['ms'], (_ts(intr) - _ts(p)) * 1000)

    def test_本轮计数在新回合清零(self):
        b = Board()
        feed(b, human('一'), api(), use(tid='m1', name='mcp__x__y'), result(tid='m1', err=True), turn_end())
        d = live(b)['drawer']
        self.assertEqual(d['mcp']['turn'], 1)
        self.assertEqual(d['tools']['errors_turn'], 1)
        self.assertEqual(d['tools']['cats_turn'], {'MCP': 1})
        feed(b, human('二'))
        d = live(b)['drawer']
        self.assertEqual((d['mcp']['turn'], d['mcp']['total'], d['tools']['errors_turn']), (0, 1, 0))

    def test_回合进行中的后台通知照旧开新回合(self):
        b = Board()
        feed(b, human('一'), api(), notif())
        self.assertEqual(lb(b, 'prompt'), '后台通知')
        # 刚发出、还没回复的那次请求带进新回合（D-111）
        self.assertEqual(cnt(b, 'loop'), 1)

    def test_子agent交回报告开一个回合(self):
        b = Board()
        feed(b, human('一'), api(), text(), turn_end())
        feed(b, {'type': 'user', 'isMeta': True, 'timestamp': iso(),
                 'origin': {'kind': 'peer', 'handback': True, 'name': 'general-purpose', 'from': 'a1'},
                 'message': {'role': 'user', 'content': 'Another Claude session sent a message: …'}})
        self.assertEqual(b._in['source'], '子 agent 交回')
        self.assertEqual(b._in['summary'], 'general-purpose')
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertIsNone(b.snapshot()['live']['turn']['end_ts'])
        self.assertNotIn('origin/peer', b.snapshot()['session']['new_types'])
        feed(b, api(), text(req='r2'), turn_end(), notif())
        self.assertIsNotNone(b.snapshot()['live']['turn']['end_ts'])

    def test_其他会话发来的消息和自动继续(self):
        b = Board()
        feed(b, {'type': 'user', 'timestamp': iso(), 'origin': {'kind': 'peer', 'from': 'x'}, 'message': {'role': 'user', 'content': 'hi'}})
        self.assertEqual(b._in['source'], '其他会话')
        feed(b, api(), text(), turn_end(), {'type': 'user', 'timestamp': iso(), 'origin': {'kind': 'auto-continuation'}, 'message': {'role': 'user', 'content': 'go'}})
        self.assertEqual(b._in['source'], '自动继续')
        self.assertIsNone(b.snapshot()['live']['turn']['end_ts'])

    def test_token按requestId去重(self):
        b = Board()
        u = {'input_tokens': 10, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 30, 'output_tokens': 5}
        feed(b, human('x'), api(), text(req='r1', usage=u), text(req='r1', usage=u))
        self.assertEqual(cnt(b, 'context_tokens'), 60)
        self.assertEqual(cnt(b, 'model_calls'), 1)


class 等人(unittest.TestCase):
    def test_等批准(self):
        b = Board()
        b.feed_proc({'status': 'busy'})
        feed(b, human('x'), api())
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        self.assertEqual(st(b, 'model'), 'done')
        self.assertEqual(st(b, 'permission'), 'waiting')
        self.assertEqual(lb(b, 'permission'), '等你批准')
        b.feed_proc({'status': 'busy'})
        self.assertEqual(st(b, 'permission'), 'idle')
        feed(b, use(), result(source='user_temporary'))
        self.assertEqual(st(b, 'permission'), 'passed')
        self.assertEqual(st(b, 'tool'), 'done')

    def test_等回答(self):
        b = Board()
        b.feed_proc({'status': 'busy'})
        feed(b, human('x'), api())
        b.feed_proc({'status': 'waiting', 'waitingFor': 'input needed'})
        self.assertEqual(st(b, 'model'), 'done')
        self.assertEqual(st(b, 'tool'), 'waiting')
        self.assertEqual(lb(b, 'tool'), '等你回答')
        b.feed_proc({'status': 'busy'})
        self.assertEqual(st(b, 'tool'), 'active')
        feed(b, use(name='AskUserQuestion'), result(source='user_temporary'))
        self.assertEqual(st(b, 'tool'), 'done')

    def test_等待事件的日志带当前时间(self):
        import time
        b = Board()
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        last = b.snapshot()['log'][-1]
        self.assertEqual(last['step'], '等你批准')
        self.assertAlmostEqual(last['ts'], time.time(), delta=5)

    def test_其他等待只更新状态条(self):
        b = Board()
        feed(b, human('x'), api())
        b.feed_proc({'status': 'waiting', 'waitingFor': 'dialog open'})
        self.assertEqual(st(b, 'model'), 'active')
        self.assertEqual(st(b, 'tool'), 'idle')
        self.assertEqual(b.snapshot()['session']['waiting_for'], 'dialog open')

    def test_等批准时被中断(self):
        b = Board()
        b.feed_proc({'status': 'busy'})
        feed(b, human('x'), api())
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        feed(b, bare('[Request interrupted by user]'))
        self.assertEqual(st(b, 'permission'), 'idle')
        self.assertEqual(st(b, 'turn'), 'interrupted')

    def test_能读到进程状态时工具显示执行中(self):
        b = Board()
        b.feed_proc({'status': 'busy'})
        feed(b, human('x'), api(), use())
        self.assertEqual(lb(b, 'tool'), '执行中')

    def test_读不到进程状态时工具显示执行中或等待批准(self):
        b = Board()
        b.feed_proc(None)
        feed(b, human('x'), api(), use())
        self.assertEqual(lb(b, 'tool'), '执行中或等待批准')
        self.assertFalse(b.snapshot()['session']['proc_available'])

    def test_进程状态显示后台命令和没见过的值(self):
        b = Board()
        b.feed_proc({'status': 'shell'})
        self.assertEqual(b.snapshot()['session']['proc_status'], 'shell')
        b.feed_proc({'status': 'somethingnew'})
        self.assertEqual(b.snapshot()['session']['proc_status'], 'somethingnew')

    def test_进程退出(self):
        b = Board()
        b.feed_proc({'status': 'idle'})
        b.proc_exited()
        self.assertEqual(b.snapshot()['session']['proc_status'], 'exited')


class 子agent(unittest.TestCase):
    def meta(self, **kw):
        return dict({'agentType': 'Explore', 'description': '查找配置', 'toolUseId': 'tA'}, **kw)

    def test_事件归属到子agent不点亮主流程(self):
        b = Board()
        feed(b, human('x'), api(), text(), turn_end())
        b.feed_meta('a1', self.meta())
        self.assertEqual(st(b, 'agent'), 'active')
        b.feed_record(bare('任务描述'), agent='a1')
        b.feed_record(api('agent:builtin:Explore'), agent='a1')
        b.feed_record(use('z', 'Read', {'file_path': '/x'}), agent='a1')
        self.assertEqual(st(b, 'turn'), 'done')
        self.assertEqual(st(b, 'tool'), 'idle')
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(cnt(b, 'tool_calls'), 0)
        card = b.snapshot()['agents'][0]
        self.assertEqual(card['type'], 'Explore')
        self.assertEqual(card['description'], '查找配置')
        self.assertIn('Read', card['step'])
        self.assertEqual(cnt(b, 'agents'), 1)

    def test_后台子agent跨回合(self):
        b = Board()
        feed(b, human('x'), api(), use('tA', 'Agent'), result('tA'))
        b.feed_meta('a1', self.meta())
        feed(b, text(req='r2'), turn_end())
        self.assertEqual(st(b, 'agent'), 'active')
        feed(b, human('下一个'))
        self.assertEqual(st(b, 'agent'), 'active')
        feed(b, notif('tA'))
        self.assertEqual(st(b, 'agent'), 'done')

    def test_只有finished也能结束(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        b.feed_meta('a1', self.meta(finished=True))
        self.assertEqual(st(b, 'agent'), 'done')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'done')

    def test_只有通知也能结束(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        feed(b, notif('tA'))
        self.assertEqual(st(b, 'agent'), 'done')

    def test_排队通知也能结束且不重置主流程(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        feed(b, human('x'), att('environment'), api())
        feed(b, queued('task-notification', '<task-notification><tool-use-id>tA</tool-use-id><status>completed</status></task-notification>'))
        self.assertEqual(st(b, 'agent'), 'done')
        self.assertEqual(st(b, 'model'), 'active')
        self.assertEqual(st(b, 'context'), 'done')

    def test_失败的通知显示出错(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        feed(b, notif('tA', 'failed'))
        self.assertEqual(st(b, 'agent'), 'error')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'failed')

    def test_还有子agent在跑时节点保持进行中(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        b.feed_meta('a2', self.meta(toolUseId='tB'))
        feed(b, notif('tA'))
        self.assertEqual(st(b, 'agent'), 'active')
        feed(b, notif('tB'))
        self.assertEqual(st(b, 'agent'), 'done')

    def test_重复结束信号不报错(self):
        b = Board()
        b.feed_meta('a1', self.meta(finished=True))
        feed(b, notif('tA'), notif('tA'))
        self.assertEqual(st(b, 'agent'), 'done')

    def test_已结束的子agent又有新记录回到运行中(self):
        b = Board()
        b.feed_meta('a1', self.meta(finished=True))
        b.feed_record(api('agent:builtin:Explore'), agent='a1')
        self.assertEqual(st(b, 'agent'), 'active')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'running')

    def test_通知之后才读到的较早子agent记录不改回运行中(self):
        # 服务端先读主记录再读子 agent 记录：子 agent 最后几条记录早于完成通知，却在通知之后才喂进来
        b = Board()
        b.feed_meta('a1', self.meta())
        early_api, early_text = api('agent:builtin:general-purpose'), text(req='ra')
        feed(b, human('x'), api(), notif(tool_use_id='tA'))
        b.feed_record(early_api, agent='a1')
        b.feed_record(early_text, agent='a1')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'done')
        self.assertEqual(st(b, 'agent'), 'done')
        # 晚于结束时刻的新记录仍然改回运行中
        b.feed_record(api('agent:builtin:general-purpose'), agent='a1')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'running')

    def test_被你停掉的子agent不再显示运行中(self):
        # 后台子 agent 被 ctrl+x ctrl+k 停掉：meta 写 stoppedByUser，没有 finished，也没有通知（2.1.322 实测）
        b = Board()
        b.feed_meta('a1', self.meta())
        b.feed_record(api('agent:builtin:general-purpose'), agent='a1')
        b.feed_meta('a1', self.meta(stoppedByUser=True))
        a = b.snapshot()['agents'][0]
        self.assertEqual(a['state'], 'done')
        self.assertEqual(a['step'], '被你停止')
        self.assertEqual(st(b, 'agent'), 'done')
        self.assertTrue(any(e['summary'] == '被你停止' for e in b.snapshot()['log'] if e['step'] == '子 agent'))

    def test_停掉后台子agent的系统记录(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        b.feed_record(api('agent:builtin:general-purpose'), agent='a1')
        feed(b, {'type': 'system', 'subtype': 'agents_killed', 'timestamp': iso()})
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'done')
        self.assertEqual(b.snapshot()['agents'][0]['step'], '被你停止')
        self.assertNotIn('system/agents_killed', b.snapshot()['session']['new_types'])

    def test_meta判结束后晚读到的较早记录不改回运行中(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        early = api('agent:builtin:general-purpose')
        last = text(req='ra')
        b.feed_record(last, agent='a1')
        b.feed_meta('a1', self.meta(finished=True))
        b.feed_record(early, agent='a1')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'done')

    def test_meta先判结束再来通知之后较早记录也不改回运行中(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        early = api('agent:builtin:general-purpose')
        b.feed_meta('a1', self.meta(finished=True))
        feed(b, human('x'), api(), notif(tool_use_id='tA'))
        b.feed_record(early, agent='a1')
        self.assertEqual(b.snapshot()['agents'][0]['state'], 'done')

    def test_和子agent无关的通知只进日志(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        feed(b, notif('别的工具'))
        self.assertEqual(st(b, 'agent'), 'active')

    def test_中断不改变子agent(self):
        b = Board()
        b.feed_meta('a1', self.meta())
        feed(b, human('x'), api(), bare('[Request interrupted by user]'))
        self.assertEqual(st(b, 'agent'), 'active')


class 不算prompt的记录(unittest.TestCase):
    def assert_不重置(self, rec):
        b = Board()
        feed(b, human('x'), att('environment'), api())
        feed(b, rec)
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(st(b, 'context'), 'done')
        self.assertEqual(st(b, 'model'), 'active')

    def test_本地命令输出(self):
        self.assert_不重置(bare('<local-command-stdout>ok</local-command-stdout>'))

    def test_本地命令(self):
        self.assert_不重置(bare('<command-name>/rename</command-name>\n<command-message>rename</command-message>'))

    def test_shell模式输入(self):
        self.assert_不重置(bare('<bash-input>ls</bash-input>'))

    def test_shell模式输出(self):
        self.assert_不重置(bare('<bash-stdout>a</bash-stdout>'))

    def test_shell模式错误(self):
        self.assert_不重置(bare('<bash-stderr>e</bash-stderr>'))

    def test_isMeta(self):
        self.assert_不重置(bare('<system-reminder>提醒</system-reminder>', isMeta=True))

    def test_压缩摘要(self):
        self.assert_不重置(bare('This session is being continued', isCompactSummary=True))

    def test_没见过的标签(self):
        self.assert_不重置(bare('<brand-new>x</brand-new>'))

    def test_命令输出不单独记事件(self):
        b = Board()
        feed(b, human('x'), api(), text(), turn_end())
        feed(b, bare('<local-command-caveat>说明</local-command-caveat>', isMeta=True),
             bare('<command-name>/exit</command-name>\n<command-message>exit</command-message>'),
             bare('<local-command-stdout>See ya!</local-command-stdout>'),
             bare('<bash-input>ls -la</bash-input>'), bare('<bash-stdout>a</bash-stdout>'), bare('<bash-stderr>e</bash-stderr>'))
        cmds = [e['summary'] for e in b.snapshot()['log'] if e['step'] == '命令']
        self.assertEqual(cmds, ['/exit', '! ls -la'])

    def test_出现过origin后没有origin的文字(self):
        self.assert_不重置(bare('某种新的合成文字'))

    def test_没见过的origin种类(self):
        rec = human('x')
        rec['origin'] = {'kind': 'something-new'}
        self.assert_不重置(rec)

    def test_用户以斜杠开头的文字算prompt(self):
        b = Board()
        feed(b, human('/查找 这个问题'))
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_粘贴内容算prompt(self):
        b = Board()
        feed(b, human('<pasted_content id="1">x</pasted_content>'))
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_看板自己的调用算prompt摘要用命令名(self):
        b = Board()
        feed(b, human('<command-message>harness-board</command-message>\n<command-name>/harness-board</command-name>'))
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(b.snapshot()['log'][-1]['summary'], '/harness-board')
        feed(b, bare('Base directory for this skill', isMeta=True))
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(cnt(b, 'tool_calls'), 0)


class 中途消息(unittest.TestCase):
    def test_任务中途插话只进日志(self):
        b = Board()
        feed(b, human('x'), att('environment'), api())
        feed(b, queued('prompt', '顺便再看一下'))
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(st(b, 'model'), 'active')
        last = b.snapshot()['log'][-1]
        self.assertEqual(last['step'], '插话')
        self.assertIn('顺便', last['summary'])

    def test_任务中途通知只进日志(self):
        b = Board()
        feed(b, human('x'), api())
        feed(b, queued('task-notification', '<task-notification><status>completed</status></task-notification>'))
        self.assertEqual(st(b, 'model'), 'active')
        self.assertEqual(b.snapshot()['log'][-1]['step'], '通知')


class 压缩(unittest.TestCase):
    def test_手动压缩(self):
        b = Board()
        feed(b, human('x'), api(), text(), turn_end())
        feed(b, bare('/compact'))
        self.assertEqual(st(b, 'compact'), 'active')
        self.assertEqual(cnt(b, 'rounds'), 1)
        feed(b, boundary('manual'))
        self.assertEqual(st(b, 'compact'), 'done')
        self.assertEqual(lb(b, 'compact'), '50000 → 12000 token')
        feed(b, bare('summary', isCompactSummary=True), bare('<command-name>/compact</command-name>'))
        self.assertEqual(st(b, 'compact'), 'done')
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_带参数的手动压缩(self):
        b = Board()
        feed(b, human('x'), bare('/compact 保留测试结果'))
        self.assertEqual(st(b, 'compact'), 'active')
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_自动压缩只有完成时才亮(self):
        b = Board()
        feed(b, human('x'), api())
        self.assertEqual(st(b, 'compact'), 'idle')
        feed(b, boundary())
        self.assertEqual(st(b, 'compact'), 'done')


class 上下文(unittest.TestCase):
    def test_同一批几十条attachment只点亮一次(self):
        b = Board()
        feed(b, human('x'))
        b.drain()
        feed(b, *[att('environment') for _ in range(34)])
        changes = [t for t in b.drain()['transitions'] if t['node'] == 'context']
        self.assertEqual(len(changes), 1)

    def test_提醒类不点亮(self):
        b = Board()
        feed(b, human('x'), att('total_tokens_reminder'))
        self.assertEqual(st(b, 'context'), 'idle')
        self.assertEqual(b.snapshot()['context']['total_tokens_reminder']['kind'], 'reminder')

    def test_提醒类不进日志(self):
        b = Board()
        feed(b, human('x'))
        n = len(b.snapshot()['log'])
        feed(b, att('total_tokens_reminder'))
        self.assertEqual(len(b.snapshot()['log']), n)

    def test_prompt_snapshot不点亮但记入清单(self):
        b = Board()
        feed(b, human('x'), att('prompt_snapshot', content='x' * 100))
        self.assertEqual(st(b, 'context'), 'idle')
        self.assertGreater(b.snapshot()['context']['prompt_snapshot']['size'], 100)

    def test_清单累计次数和大小(self):
        b = Board()
        feed(b, att('instructions', content='abc'), att('instructions', content='abcdef'))
        item = b.snapshot()['context']['instructions']
        self.assertEqual(item['count'], 2)
        self.assertEqual(item['kind'], 'context')
        self.assertGreater(item['size'], 6)

    def test_清单不含内容(self):
        b = Board()
        feed(b, att('instructions', content='绝密内容'))
        self.assertNotIn('绝密内容', json.dumps(b.snapshot(), ensure_ascii=False))

    def test_skill_mention是已知类型(self):
        b = Board()
        feed(b, human('x'), att('skill_mention'))
        self.assertEqual(st(b, 'context'), 'done')
        self.assertEqual(b.snapshot()['session']['new_types'], [])

    def test_没见过的attachment点亮并计新类型(self):
        b = Board()
        feed(b, human('x'), att('brand_new_kind'))
        self.assertEqual(st(b, 'context'), 'done')
        self.assertIn('attachment/brand_new_kind', b.snapshot()['session']['new_types'])


class 中断(unittest.TestCase):
    def test_工具运行时中断(self):
        b = Board()
        feed(b, human('x'), api(), use(), result(err=True, decision=None), bare('[Request interrupted by user for tool use]'))
        self.assertEqual(st(b, 'tool'), 'interrupted')
        self.assertEqual(st(b, 'turn'), 'interrupted')
        self.assertEqual(cnt(b, 'inflight'), 0)

    def test_模型输出时中断(self):
        b = Board()
        feed(b, human('x'), api(), bare('[Request interrupted by user]'))
        self.assertEqual(st(b, 'model'), 'interrupted')
        self.assertEqual(st(b, 'turn'), 'interrupted')

    def test_后到的turn_duration不覆盖中断(self):
        b = Board()
        feed(b, human('x'), api(), bare('[Request interrupted by user]'), turn_end())
        self.assertEqual(st(b, 'turn'), 'interrupted')

    def test_下一轮不受上次中断影响(self):
        b = Board()
        feed(b, human('x'), api(), bare('[Request interrupted by user]'))
        feed(b, human('y'), api(), text(), turn_end())
        self.assertEqual(st(b, 'turn'), 'done')


class 换会话(unittest.TestCase):
    def test_换会话时重建并清空旧日志(self):
        b = Board()
        feed(b, human('旧会话'), att('instructions'), api(), use(), result())
        n = 0  # 事件栏只留新会话的事件（D-110）
        b.switch_session('id2', [human('新会话的历史'), api(), text(), turn_end()])
        s = b.snapshot()
        self.assertEqual(s['session']['session_id'], 'id2')
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertEqual(cnt(b, 'tool_calls'), 0)
        self.assertEqual(st(b, 'turn'), 'done')
        self.assertNotIn('instructions', s['context'])
        self.assertEqual(len(s['log']), n + 1)
        self.assertEqual(s['log'][-1]['step'], '会话已切换')

    def test_换到还没有记录的新会话(self):
        b = Board()
        feed(b, human('旧'), api())
        b.switch_session('id2', [])
        self.assertEqual(st(b, 'model'), 'idle')
        self.assertEqual(cnt(b, 'rounds'), 0)

    def test_换会话后子agent清空(self):
        b = Board()
        b.feed_meta('a1', {'agentType': 'Explore', 'description': 'x', 'toolUseId': 'tA'})
        b.switch_session('id2', [])
        self.assertEqual(st(b, 'agent'), 'idle')
        self.assertEqual(b.snapshot()['agents'], [])


class 退化和容错(unittest.TestCase):
    def test_没有origin时走前缀退路(self):
        b = Board()
        feed(b, bare('你好'))
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertTrue(b.snapshot()['session']['degraded']['prompt'])

    def test_退路下命令算prompt(self):
        b = Board()
        feed(b, bare('<command-message>x</command-message>'))
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_退路下通知不加轮次(self):
        b = Board()
        feed(b, bare('<task-notification><status>completed</status></task-notification>'), api())
        self.assertEqual(cnt(b, 'rounds'), 0)
        self.assertEqual(lb(b, 'prompt'), '后台通知')

    def test_退路下shell记录不算prompt(self):
        b = Board()
        feed(b, bare('<bash-input>ls</bash-input>'))
        self.assertEqual(cnt(b, 'rounds'), 0)

    def test_有origin时不标退化(self):
        b = Board()
        feed(b, human('x'))
        self.assertFalse(b.snapshot()['session']['degraded']['prompt'])

    def test_无头会话不写origin但不算退化(self):
        """claude -p 的记录 entrypoint 是 sdk-cli，用户消息本来就没有 origin（10 号件 §九）。"""
        b = Board()
        feed(b, bare('你好', entrypoint='sdk-cli'))
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertFalse(b.snapshot()['session']['degraded']['prompt'])
        self.assertEqual(b.stats['non_human_prompts'], 0)
        feed(b, bare('<bash-input>ls</bash-input>', entrypoint='sdk-cli'))
        self.assertEqual(cnt(b, 'rounds'), 1)

    def test_无头会话的斜杠命令也算你输入的(self):
        """0.2.0：claude -p '/某个 skill' 写成 <command-message>…，没有 origin。原来按退化处理、计入误判（doctor 报 2 条）。"""
        b = Board()
        feed(b, bare('<command-message>harness-board</command-message>\n<command-name>/harness-board</command-name>', entrypoint='sdk-cli'))
        self.assertEqual(cnt(b, 'rounds'), 1)
        self.assertFalse(b.snapshot()['session']['degraded']['prompt'])
        self.assertEqual(b.stats['non_human_prompts'], 0)
        # 交互会话（cli）没有 origin 时照旧算退化
        b2 = Board()
        feed(b2, bare('<command-message>x</command-message>'))
        self.assertTrue(b2.snapshot()['session']['degraded']['prompt'])
        self.assertEqual(b2.stats['non_human_prompts'], 1)

    def test_坏行不崩溃(self):
        b = Board()
        b.feed_bad_line()
        self.assertEqual(b.snapshot()['session']['bad_lines'], 1)

    def test_没见过的类型计数并按类型去重(self):
        b = Board()
        feed(b, {'type': 'weird'}, {'type': 'weird'}, {'type': 'other'})
        self.assertEqual(sorted(b.snapshot()['session']['new_types']), ['other', 'weird'])

    def test_没见过的system子类型计入新类型(self):
        b = Board()
        feed(b, {'type': 'system', 'subtype': 'brand_new'})
        self.assertIn('system/brand_new', b.snapshot()['session']['new_types'])

    def test_已知不显示的类型不计数(self):
        b = Board()
        feed(b, {'type': 'atis-latch'}, {'type': 'frame-link'}, {'type': 'system', 'subtype': 'away_summary'})
        self.assertEqual(b.snapshot()['session']['new_types'], [])

    def test_字段缺失不崩溃(self):
        b = Board()
        for r in ({}, {'type': 'user'}, {'type': 'user', 'message': None}, {'type': 'assistant', 'message': {'content': 'x'}},
                  {'type': 'attachment'}, {'type': 'attachment', 'attachment': None}, {'type': 'user', 'message': {'content': [None, 1]}},
                  {'type': 'system'}, {'type': 'api-request'}, {'type': 'user', 'origin': 'x', 'message': {'content': 'a'}}):
            b.feed_record(r)

    def test_没有api_request的旧记录由assistant推断(self):
        b = Board()
        feed(b, human('x'), think('r1'))
        self.assertEqual(st(b, 'model'), 'active')
        self.assertEqual(lb(b, 'model'), '推断')
        self.assertTrue(b.snapshot()['session']['degraded']['model'])
        self.assertEqual(cnt(b, 'loop'), 1)
        feed(b, text('r1'))
        self.assertEqual(cnt(b, 'loop'), 1)

    def test_有api_request时不推断(self):
        b = Board()
        feed(b, human('x'), api(), think('r1'))
        self.assertIsNone(lb(b, 'model'))
        self.assertFalse(b.snapshot()['session']['degraded']['model'])


class 会话信息和摘要(unittest.TestCase):
    def test_标题优先用customTitle(self):
        b = Board()
        feed(b, {'type': 'ai-title', 'aiTitle': 'AI 标题'})
        self.assertEqual(b.snapshot()['session']['title'], 'AI 标题')
        feed(b, {'type': 'custom-title', 'customTitle': '我的标题'}, {'type': 'ai-title', 'aiTitle': 'AI 新标题'})
        self.assertEqual(b.snapshot()['session']['title'], '我的标题')

    def test_版本和权限模式(self):
        b = Board()
        feed(b, api(), {'type': 'permission-mode', 'permissionMode': 'auto'})
        s = b.snapshot()['session']
        self.assertEqual(s['version'], '2.1.318')
        self.assertEqual(s['mode'], 'auto')

    def test_摘要脱敏(self):
        for secret in ('curl -H "Authorization: Bearer abcdef123456"', 'export TOKEN=abcdef123456', 'x --password=abcdef123456', 'key sk-abcdef123456'):
            b = Board()
            feed(b, human('x'), api(), use(inp={'command': secret}))
            self.assertNotIn('abcdef123456', b.snapshot()['log'][-1]['summary'], secret)

    def test_prompt摘要也脱敏(self):
        b = Board()
        feed(b, human('用 sk-abcdef123456 试试'))
        self.assertNotIn('abcdef123456', b.snapshot()['log'][-1]['summary'])

    def test_摘要截断到120字符(self):
        b = Board()
        feed(b, human('长' * 300))
        self.assertLessEqual(len(b.snapshot()['log'][-1]['summary']), 121)

    def test_不显示工具结果和思考内容(self):
        b = Board()
        rec = think()
        rec['message']['content'][0]['thinking'] = '思考全文'
        feed(b, human('x'), api(), rec, use(), result())
        dump = json.dumps(b.snapshot(), ensure_ascii=False)
        self.assertNotIn('工具结果正文', dump)
        self.assertNotIn('思考全文', dump)

    def test_工具摘要用文件路径(self):
        b = Board()
        feed(b, human('x'), api(), use(name='Read', inp={'file_path': '/a/b.py'}))
        self.assertIn('/a/b.py', b.snapshot()['log'][-1]['summary'])

    def test_最后记录时间(self):
        b = Board()
        r = human('x')
        feed(b, r)
        self.assertIsNotNone(b.snapshot()['session']['last_record_ts'])


def block(b, bid):
    return next(x for x in b.snapshot()['blocks'] if x['id'] == bid)


def row(b, bid, label):
    return next(r for r in block(b, bid)['rows'] if r['label'] == label)


def val(b, bid, label):
    return row(b, bid, label)['value']


def instructions(*files):
    return att('instructions', files=[{'path': p, 'type': t, 'content': 'x' * n} for p, t, n in files])


def hook(event='SessionStart', exit_code=0, kind='hook_success', name=None):
    return att(kind, hookEvent=event, hookName=name or event, exitCode=exit_code, content='y' * 10)


class 八大块(unittest.TestCase):
    def test_固定八大块且每块四到八行(self):
        b = Board()
        s = b.snapshot()['blocks']
        self.assertEqual([x['id'] for x in s], ['input', 'context', 'memory', 'hooks', 'model', 'tools', 'agents', 'session'])
        for x in s:
            self.assertTrue(4 <= len(x['rows']) <= 8, x['id'])
            self.assertTrue(x['name'])

    def test_块的状态跟着节点走(self):
        b = Board()
        self.assertEqual(block(b, 'model')['state'], 'idle')
        feed(b, human('x'), api())
        self.assertEqual(block(b, 'input')['state'], 'done')
        self.assertEqual(block(b, 'model')['state'], 'active')
        feed(b, use())
        self.assertEqual(block(b, 'tools')['state'], 'active')

    def test_等批准时工具块显示等待(self):
        b = Board()
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        self.assertEqual(block(b, 'tools')['state'], 'waiting')
        r = row(b, 'tools', '权限')
        self.assertEqual(r['value'], '等你批准')
        self.assertEqual(r['state'], 'waiting')

    def test_等回答时当前工具一行显示等你回答(self):
        b = Board()
        b.feed_proc({'status': 'waiting', 'waitingFor': 'input needed'})
        self.assertEqual(val(b, 'tools', '当前'), '等你回答')
        self.assertEqual(block(b, 'tools')['state'], 'waiting')

    # INPUT
    def test_输入来源和内容(self):
        b = Board()
        feed(b, human('修一下登录'))
        self.assertEqual(val(b, 'input', '来源'), '你输入')
        self.assertEqual(val(b, 'input', '内容'), '修一下登录')
        self.assertEqual(val(b, 'input', '轮次'), '第 1 轮')
        feed(b, notif())
        self.assertEqual(val(b, 'input', '来源'), '后台通知')

    def test_插话和通知计数(self):
        b = Board()
        feed(b, human('x'), queued('prompt', '插一句'), queued('task-notification', '<task-notification><status>completed</status></task-notification>'))
        self.assertEqual(val(b, 'input', '插话'), '1 条')
        self.assertEqual(val(b, 'input', '后台通知'), '1 条')
        feed(b, human('y'))
        self.assertEqual(val(b, 'input', '插话'), '0 条')
        self.assertEqual(val(b, 'input', '后台通知'), '1 条')

    # CONTEXT
    def test_上下文拼装(self):
        b = Board()
        feed(b, human('x'),
             att('prompt_snapshot', systemPrompt=['a' * 2000], tools=[{}, {}, {}]),
             att('skill_listing', skillCount=67, names=[]),
             att('mcp_instructions_delta', addedNames=['github', 'figma'], removedNames=[]),
             att('deferred_tools_delta', addedNames=['A', 'B', 'C'], removedNames=['C']),
             att('environment', snapshot={'isWorktree': True, 'isGitRepo': True}))
        self.assertEqual(val(b, 'context', 'System Prompt'), '2.0k 字符')
        self.assertEqual(val(b, 'context', '工具定义'), '3 个 · 延迟加载 2 个')
        self.assertEqual(val(b, 'context', 'Skills'), '67 个')
        self.assertEqual(val(b, 'context', 'MCP'), '2 个：figma、github')
        self.assertIn('worktree', val(b, 'context', '环境'))

    def test_本轮注入计数每轮归零(self):
        b = Board()
        feed(b, human('x'), att('environment'), att('date'), att('total_tokens_reminder', text='<total_tokens>5 tokens left</total_tokens>'))
        self.assertEqual(val(b, 'context', '本轮注入'), '2 条 · 提醒 1 条')
        feed(b, human('y'))
        self.assertEqual(val(b, 'context', '本轮注入'), '0 条 · 提醒 0 条')

    def test_MCP移除后减少(self):
        b = Board()
        feed(b, att('mcp_instructions_delta', addedNames=['a', 'b']), att('mcp_instructions_delta', removedNames=['a']))
        self.assertEqual(val(b, 'context', 'MCP'), '1 个：b')

    # MEMORY
    def test_记忆文件按类型显示大小(self):
        b = Board()
        feed(b, instructions(('/u/.claude/CLAUDE.md', 'User', 1500), ('/p/CLAUDE.md', 'Project', 6000), ('/m/MEMORY.md', 'AutoMem', 300)))
        self.assertEqual(val(b, 'memory', '用户 CLAUDE.md'), '1.5k 字符')
        self.assertEqual(val(b, 'memory', '项目 CLAUDE.md'), '6.0k 字符')
        self.assertEqual(val(b, 'memory', '自动记忆 MEMORY.md'), '300 字符')
        self.assertEqual(block(b, 'memory')['state'], 'done')

    def test_记忆内容不进快照(self):
        b = Board()
        feed(b, att('instructions', files=[{'path': '/a/CLAUDE.md', 'type': 'User', 'content': '绝密规则'}]))
        self.assertNotIn('绝密规则', json.dumps(b.snapshot(), ensure_ascii=False))

    def test_项目级多个文件合计(self):
        b = Board()
        feed(b, instructions(('/p/CLAUDE.md', 'Project', 1000), ('/p/AGENTS.md', 'Project', 1000)))
        self.assertEqual(val(b, 'memory', '项目 CLAUDE.md'), '2.0k 字符 · 2 个')

    def test_同一文件更新不重复计算(self):
        b = Board()
        feed(b, instructions(('/p/CLAUDE.md', 'Project', 1000)), instructions(('/p/CLAUDE.md', 'Project', 2000)))
        self.assertEqual(val(b, 'memory', '项目 CLAUDE.md'), '2.0k 字符')

    def test_没有记忆时显示未加载(self):
        self.assertEqual(val(Board(), 'memory', '用户 CLAUDE.md'), '未见')

    def test_子目录记忆和压缩带回(self):
        b = Board()
        feed(b, att('nested_memory', path='/p/sub/CLAUDE.md', content='x'), att('compact_file_reference', filename='a.py'))
        self.assertEqual(val(b, 'memory', '子目录记忆'), '1 个')
        self.assertEqual(val(b, 'memory', '压缩带回'), '1 个文件')

    def test_读写记忆文件计数(self):
        b = Board()
        feed(b, human('x'), api(),
             use('a', 'Read', {'file_path': '/h/.claude/projects/p/memory/x.md'}), result('a'),
             use('b', 'Edit', {'file_path': '/p/CLAUDE.md'}), result('b'),
             use('c', 'Read', {'file_path': '/p/main.py'}), result('c'))
        self.assertEqual(val(b, 'memory', '读写'), '读 1 · 写 1')
        self.assertEqual(block(b, 'memory')['state'], 'done')

    def test_新一轮记忆块回到未到但数据保留(self):
        b = Board()
        feed(b, instructions(('/u/CLAUDE.md', 'User', 100)), human('x'))
        self.assertEqual(block(b, 'memory')['state'], 'idle')
        self.assertEqual(val(b, 'memory', '用户 CLAUDE.md'), '100 字符')

    # HOOKS
    def test_Hook按事件计数(self):
        b = Board()
        feed(b, human('x'), hook('SessionStart'), hook('UserPromptSubmit'), hook('UserPromptSubmit'), hook('PreToolUse'), hook('PostToolUse'))
        self.assertEqual(val(b, 'hooks', 'SessionStart'), '1 次')
        self.assertEqual(val(b, 'hooks', 'UserPromptSubmit'), '2 次')
        self.assertEqual(val(b, 'hooks', 'PreToolUse'), '1 次')
        self.assertEqual(val(b, 'hooks', 'PostToolUse'), '1 次')
        self.assertEqual(block(b, 'hooks')['state'], 'done')

    def test_Hook只算成功记录不把注入重复计次(self):
        b = Board()
        feed(b, hook('SessionStart'), hook('SessionStart', kind='hook_additional_context', exit_code=None))
        self.assertEqual(val(b, 'hooks', 'SessionStart'), '1 次')
        self.assertEqual(val(b, 'hooks', '注入'), '1 条 · 10 字符')

    def test_Hook注入内容是列表时也算大小(self):
        b = Board()
        feed(b, att('hook_additional_context', hookEvent='SessionStart', content=['abc', 'defg']))
        self.assertEqual(val(b, 'hooks', '注入'), '1 条 · 7 字符')

    def test_Stop_Hook来自system记录(self):
        b = Board()
        feed(b, {'type': 'system', 'subtype': 'stop_hook_summary', 'hookCount': 2, 'hookErrors': [], 'preventedContinuation': False})
        self.assertEqual(val(b, 'hooks', 'Stop'), '2 次')
        self.assertEqual(block(b, 'hooks')['state'], 'done')

    def test_Hook出错(self):
        b = Board()
        feed(b, hook('PreToolUse', exit_code=2))
        self.assertEqual(val(b, 'hooks', '出错'), '1 次')
        self.assertEqual(block(b, 'hooks')['state'], 'error')

    def test_Hook出错不被后面的成功覆盖(self):
        b = Board()
        feed(b, hook('PreToolUse', exit_code=2), hook('PostToolUse'))
        self.assertEqual(block(b, 'hooks')['state'], 'error')

    def test_Hook阻塞错误记录(self):
        b = Board()
        feed(b, att('hook_blocking_error', hookEvent='PreToolUse', hookName='PreToolUse'))
        self.assertEqual(val(b, 'hooks', '出错'), '1 次')

    def test_没见过的hook记录不算新类型(self):
        b = Board()
        feed(b, att('hook_something_new', hookEvent='Notification'))
        self.assertEqual(b.snapshot()['session']['new_types'], [])

    def test_其他事件计入其他(self):
        b = Board()
        feed(b, hook('SubagentStop'))
        self.assertEqual(val(b, 'hooks', '其他事件'), '1 次')

    # MODEL
    def test_模型配置来自请求参数(self):
        b = Board()
        r = api()
        r['params'] = {'model': 'claude-opus-5-5', 'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'xhigh'}}
        feed(b, r)
        self.assertEqual(val(b, 'model', '模型'), 'claude-opus-5-5')
        self.assertEqual(val(b, 'model', '配置'), 'effort xhigh · thinking adaptive')

    def test_没有请求参数时模型来自响应且忽略合成消息(self):
        b = Board()
        t = text()
        t['message']['model'] = 'claude-haiku-5-5'
        feed(b, t)
        self.assertEqual(val(b, 'model', '模型'), 'claude-haiku-5-5')
        s = text(req='r2')
        s['message']['model'] = '<synthetic>'
        feed(b, s)
        self.assertEqual(val(b, 'model', '模型'), 'claude-haiku-5-5')

    def test_模型次数(self):
        b = Board()
        feed(b, human('x'), api(), text('r1'), api(), text('r2'))
        self.assertEqual(val(b, 'model', '次数'), '本轮第 2 次 · 共 2 次')

    def test_token和缓存命中(self):
        b = Board()
        u = {'input_tokens': 100, 'cache_creation_input_tokens': 200, 'cache_read_input_tokens': 700, 'output_tokens': 50}
        feed(b, text('r1', usage=u))
        self.assertEqual(val(b, 'model', 'Token'), '输入 100 · 输出 50')
        self.assertEqual(val(b, 'model', '缓存'), '写 200 · 读 700 · 命中 70%')

    def test_停止原因和思考块(self):
        b = Board()
        t = text()
        t['message']['stop_reason'] = 'end_turn'
        feed(b, think(), think(), t)
        self.assertEqual(val(b, 'model', '输出'), 'stop end_turn · 思考块 2 个')

    # TOOLS
    def test_当前工具(self):
        b = Board()
        feed(b, human('x'), api(), use(inp={'command': 'npm test'}))
        r = row(b, 'tools', '当前')
        self.assertEqual(r['value'], 'Bash npm test')
        self.assertEqual(r['state'], 'active')
        feed(b, result())
        self.assertEqual(val(b, 'tools', '当前'), '无')

    def test_工具分类统计(self):
        b = Board()
        feed(b, human('x'), api())
        for i, n in enumerate(['Bash', 'Read', 'Edit', 'Write', 'WebFetch', 'mcp__github__x', 'ToolSearch']):
            feed(b, use(str(i), n), result(str(i)))
        self.assertEqual(val(b, 'tools', '调用'), 'Shell 1 · 文件 3 · 网络 1 · MCP 1 · 其他 1')

    def test_本批(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), use('b'), use('c'), result('a'))
        self.assertEqual(val(b, 'tools', '本批'), '3 个 · 在途 2 个')

    def test_放行和模式(self):
        b = Board()
        feed(b, {'type': 'permission-mode', 'permissionMode': 'auto'}, human('x'), api(),
             use('a'), result('a'),
             use('b'), result('b', source='user_temporary'),
             use('c'), result('c', err=True, decision='reject', source='user_reject'))
        self.assertEqual(val(b, 'tools', '放行'), '配置 1 · 你批准 1 · 拒绝 1')
        self.assertEqual(val(b, 'tools', '权限'), 'auto 模式 · 你拒绝了')

    def test_工具出错计数(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), result('a', err=True), use('b'), result('b'))
        self.assertEqual(val(b, 'tools', '出错'), '1 次')

    # AGENTS
    def test_子agent汇总和运行中明细(self):
        b = Board()
        for i in range(5):
            b.feed_meta(f'a{i}', {'agentType': 'Explore', 'description': f'任务{i}', 'toolUseId': f't{i}'})
        b.feed_meta('a0', {'finished': True})
        self.assertEqual(val(b, 'agents', '子 agent'), '运行中 4 · 结束 1 · 失败 0')
        rows = [r['label'] for r in block(b, 'agents')['rows']]
        self.assertEqual(rows.count('运行中'), 3)
        self.assertEqual(val(b, 'agents', '还有'), '1 个在跑')

    def test_后台命令和通知统计(self):
        b = Board()
        feed(b, notif('x', 'completed'), notif('y', 'failed'))
        b.feed_proc({'status': 'shell'})
        self.assertEqual(val(b, 'agents', '后台命令'), '运行中')
        self.assertEqual(val(b, 'agents', '后台任务通知'), '完成 1 · 失败 1 · 停止 0')

    # SESSION
    def test_回合状态(self):
        b = Board()
        feed(b, human('x'), api())
        self.assertEqual(val(b, 'session', '回合'), '进行中')
        feed(b, text(), turn_end(12300))
        # 知道回合开始时刻时取实际经过的时间（样本时钟每条 1 秒，prompt 到结束 3 秒），不用 durationMs（D-110）
        self.assertEqual(val(b, 'session', '回合'), '结束 · 3.0 秒')
        feed(b, human('y'), bare('[Request interrupted by user]'))
        self.assertEqual(val(b, 'session', '回合'), '中断')

    def test_Claude_Code状态(self):
        b = Board()
        self.assertEqual(val(b, 'session', 'Claude Code'), '状态不可用')
        b.feed_proc({'status': 'busy'})
        self.assertEqual(val(b, 'session', 'Claude Code'), '运行中')
        b.feed_proc({'status': 'waiting', 'waitingFor': 'dialog open'})
        self.assertEqual(val(b, 'session', 'Claude Code'), '等你操作（dialog open）')
        b.feed_proc({'status': 'idle'})
        self.assertEqual(val(b, 'session', 'Claude Code'), '空闲')

    def test_上下文占用只显示已用不把任务预算当窗口(self):
        """tokens left 约 1500 万，是任务预算，不是上下文窗口，不能拿来算占用比例。"""
        b = Board()
        u = {'input_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 250000, 'output_tokens': 0}
        feed(b, text('r1', usage=u), att('total_tokens_reminder', text='<total_tokens>14982133 tokens left</total_tokens>'))
        self.assertEqual(val(b, 'session', '上下文占用'), '250.0k')

    def test_上下文占用还没有数据(self):
        self.assertEqual(val(Board(), 'session', '上下文占用'), '未见')

    def test_压缩统计(self):
        b = Board()
        feed(b, boundary('auto'), boundary('manual'))
        self.assertEqual(val(b, 'session', '压缩'), '2 次 · 自动 1 · 手动 1')
        self.assertEqual(val(b, 'session', '最近压缩'), '50.0k → 12.0k · 20 秒')

    def test_压缩进行中时块显示进行中(self):
        b = Board()
        feed(b, human('x'), text(), turn_end(), bare('/compact'))
        self.assertEqual(block(b, 'session')['state'], 'active')
        self.assertEqual(val(b, 'session', '最近压缩'), '进行中')

    def test_换会话时小项也重建(self):
        b = Board()
        feed(b, instructions(('/u/CLAUDE.md', 'User', 100)), hook('SessionStart'))
        b.switch_session('id2', [])
        self.assertEqual(val(b, 'memory', '用户 CLAUDE.md'), '未见')
        self.assertEqual(val(b, 'hooks', 'SessionStart'), '0 次')

    def test_字段形状不对不崩溃(self):
        b = Board()
        for r in (att('instructions', files='x'), att('instructions', files=[1, None]), att('skill_listing', skillCount='a'),
                  att('mcp_instructions_delta', addedNames='x'), att('prompt_snapshot', systemPrompt=3, tools='y'),
                  att('total_tokens_reminder', text=5), att('environment', snapshot='z'),
                  {'type': 'api-request', 'params': 'x'}, {'type': 'api-request', 'params': {'output_config': 1, 'thinking': 2}},
                  {'type': 'system', 'subtype': 'stop_hook_summary', 'hookCount': 'x', 'hookErrors': 3}, hook(None)):
            b.feed_record(r)
        self.assertEqual(b.parse_errors, 0)
        self.assertEqual(len(b.snapshot()['blocks']), 8)


class 变化流(unittest.TestCase):
    def test_短工具同时到达也按顺序产生变化(self):
        b = Board()
        feed(b, human('x'), api())
        b.drain()
        feed(b, use(), result())
        seq = [(t['node'], t['state']) for t in b.drain()['transitions']]
        self.assertLess(seq.index(('tool', 'active')), seq.index(('tool', 'done')))

    def test_drain后清空(self):
        b = Board()
        feed(b, human('x'))
        self.assertTrue(b.drain()['transitions'])
        d = b.drain()
        self.assertEqual(d['transitions'], [])
        self.assertEqual(d['log'], [])

    def test_状态没变不产生变化(self):
        b = Board()
        feed(b, human('x'), api())
        b.drain()
        feed(b, think(), think())
        self.assertEqual(b.drain()['transitions'], [])

    def test_启动回放不产生变化且日志有上限(self):
        b = Board()
        b.replay([human(f'第{i}条') for i in range(2000)])
        self.assertEqual(cnt(b, 'rounds'), 2000)
        self.assertEqual(b.drain()['transitions'], [])
        self.assertEqual(len(b.snapshot()['log']), 500)
        self.assertIn('第1999条', b.snapshot()['log'][-1]['summary'])

    def test_启动回放后上下文清单已建好(self):
        b = Board()
        b.replay([att('instructions'), human('x')])
        self.assertIn('instructions', b.snapshot()['context'])

    def test_启动回放时已结束的子agent不会因历史记录回到运行中(self):
        b = Board()
        meta = {'agentType': 'Explore', 'description': 'x', 'toolUseId': 'tA', 'finished': True}
        b.replay([human('x')], agents={'a1': {'meta': meta, 'records': [api('agent:builtin:Explore')]}})
        self.assertEqual(st(b, 'agent'), 'done')

    def test_启动回放时子agent只记一次结束(self):
        b = Board()
        meta = {'agentType': 'Explore', 'description': 'x', 'toolUseId': 'tA', 'finished': True}
        b.replay([], agents={'a1': {'meta': meta, 'records': [api('agent:builtin:Explore')]}})
        ends = [e for e in b.snapshot()['log'] if e['step'] == '子 agent' and e['summary'] == '结束']
        self.assertEqual(len(ends), 1)

    def test_启动回放时通知能结束子agent(self):
        b = Board()
        meta = {'agentType': 'Explore', 'description': 'x', 'toolUseId': 'tA'}
        # 按真实顺序生成：子 agent 的记录早于通知
        h, rec = human('x'), api('agent:builtin:Explore')
        b.replay([h, notif('tA', 'failed')], agents={'a1': {'meta': meta, 'records': [rec]}})
        self.assertEqual(st(b, 'agent'), 'error')

    def test_启动回放时还在跑的子agent保持运行中(self):
        b = Board()
        meta = {'agentType': 'Explore', 'description': 'x', 'toolUseId': 'tA'}
        b.replay([human('x')], agents={'a1': {'meta': meta, 'records': []}})
        self.assertEqual(st(b, 'agent'), 'active')

    def test_feed_line统一处理坏行(self):
        b = Board()
        b.feed_line(LineReader.BAD_LINE)
        b.feed_line(human('x'))
        self.assertEqual(b.snapshot()['session']['bad_lines'], 1)
        self.assertEqual(cnt(b, 'rounds'), 1)


class 跟读(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, 'x.jsonl')

    def tearDown(self):
        self.d.cleanup()

    def write(self, s, mode='a'):
        with open(self.path, mode) as f:
            f.write(s)

    def test_文件不存在时返回空(self):
        self.assertEqual(LineReader(self.path).read(), [])

    def test_文件出现后开始读(self):
        r = LineReader(self.path)
        self.assertEqual(r.read(), [])
        self.write('{"a":1}\n')
        self.assertEqual(r.read(), [{'a': 1}])

    def test_半行留到下次(self):
        r = LineReader(self.path)
        self.write('{"a":1}\n{"b"')
        self.assertEqual(r.read(), [{'a': 1}])
        self.assertEqual(r.read(), [])
        self.write(':2}\n')
        self.assertEqual(r.read(), [{'b': 2}])

    def test_文件变小时从头重读(self):
        r = LineReader(self.path)
        self.write('{"a":1}\n{"b":2}\n')
        self.assertEqual(len(r.read()), 2)
        self.write('{"c":3}\n', 'w')
        self.assertEqual(r.read(), [{'c': 3}])

    def test_坏行用BAD_LINE标记(self):
        r = LineReader(self.path)
        self.write('not json\n{"a":1}\n')
        got = r.read()
        self.assertIs(got[0], LineReader.BAD_LINE)
        self.assertEqual(got[1], {'a': 1})

    def test_不是对象的行也是坏行(self):
        r = LineReader(self.path)
        self.write('[1,2]\n')
        self.assertIs(r.read()[0], LineReader.BAD_LINE)

    def test_多字节字符跨越读取边界(self):
        r = LineReader(self.path)
        raw = '{"a":"中文"}\n'.encode('utf-8')
        with open(self.path, 'ab') as f:
            f.write(raw[:8])
        self.assertEqual(r.read(), [])
        with open(self.path, 'ab') as f:
            f.write(raw[8:])
        self.assertEqual(r.read(), [{'a': '中文'}])

    def test_内容里的Unicode换行符不会切断记录(self):
        r = LineReader(self.path)
        self.write(json.dumps({'a': '第一段 第二段\u0085第三段'}, ensure_ascii=False) + '\n')
        got = r.read()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['a'], '第一段 第二段\u0085第三段')

    def test_只读打开不修改文件(self):
        self.write('{"a":1}\n')
        before = os.stat(self.path).st_mtime_ns
        LineReader(self.path).read()
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)


class 全量不变量(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.root = self.d.name
        os.makedirs(os.path.join(self.root, 'proj', 's1', 'subagents'))

    def tearDown(self):
        self.d.cleanup()

    def write(self, rel, recs):
        with open(os.path.join(self.root, rel), 'w') as f:
            for r in recs:
                f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + '\n')

    def test_统计且不含内容(self):
        self.write('proj/s1.jsonl', [human('机密 prompt'), api(), use(), result(), text(), turn_end(), 'bad json'])
        r = doctor(self.root)
        self.assertEqual(r['files'], 1)
        self.assertEqual(r['crashes'], 0)
        self.assertEqual(r['bad_lines'], 1)
        self.assertNotIn('机密', json.dumps(r, ensure_ascii=False))

    def test_结果早于调用计入下溢(self):
        self.write('proj/s1.jsonl', [human('x'), result('没有调用过')])
        self.assertEqual(doctor(self.root)['inflight_underflow'], 1)

    def test_非human不算prompt(self):
        self.write('proj/s1.jsonl', [human('x'), bare('<bash-input>ls</bash-input>'), notif()])
        r = doctor(self.root)
        self.assertEqual(r['prompts'], 1)
        self.assertEqual(r['non_human_prompts'], 0)

    def test_回合结束后不应停在进行中(self):
        self.write('proj/s1.jsonl', [human('x'), api(), use(), turn_end()])
        self.assertEqual(doctor(self.root)['stuck_after_turn'], 0)

    def test_按版本输出新类型和覆盖率(self):
        self.write('proj/s1.jsonl', [human('x'), api(), {'type': 'weird', 'version': '2.1.318'}])
        r = doctor(self.root)
        self.assertIn('weird', r['by_version']['2.1.318']['new_types'])
        self.assertEqual(r['by_version']['2.1.318']['api_requests'], 1)
        self.assertEqual(r['by_version']['2.1.318']['origin_records'], 1)

    def test_包含子agent文件(self):
        self.write('proj/s1.jsonl', [human('x')])
        self.write('proj/s1/subagents/agent-a1.jsonl', [bare('任务'), api('agent:builtin:Explore')])
        r = doctor(self.root)
        self.assertEqual(r['files'], 2)
        self.assertEqual(r['prompts'], 1)


def live(b):
    return b.snapshot()['live']


def tool(b, tid):
    return next(t for t in live(b)['tools'] if t['id'] == tid)


class 实时数据(unittest.TestCase):
    """正式页面要的结构化字段（05 号件 §七）。"""

    def test_逐个工具的清单(self):
        b = Board()
        feed(b, human('x'), api(), use('a', 'Bash', {'command': 'npm test'}), use('b', 'Read', {'file_path': '/p/a.py'}))
        ts = live(b)['tools']
        self.assertEqual([t['id'] for t in ts], ['a', 'b'])
        self.assertEqual(tool(b, 'a')['name'], 'Bash')
        self.assertEqual(tool(b, 'a')['target'], 'npm test')
        self.assertEqual(tool(b, 'a')['cat'], 'Shell')
        self.assertEqual(tool(b, 'b')['cat'], '文件')
        self.assertEqual(tool(b, 'a')['state'], 'running')
        self.assertIsNotNone(tool(b, 'a')['t0'])
        self.assertIsNone(tool(b, 'a')['t1'])
        feed(b, result('b'))
        self.assertEqual(tool(b, 'b')['state'], 'done')
        self.assertIsNotNone(tool(b, 'b')['t1'])
        self.assertEqual(tool(b, 'b')['perm'], '分类器放行')
        self.assertEqual(tool(b, 'a')['state'], 'running')
        feed(b, result('a', err=True))
        self.assertEqual(tool(b, 'a')['state'], 'error')

    def test_工具被拒绝和中断(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), result('a', err=True, decision='reject', source='user_reject', reason=None))
        self.assertEqual(tool(b, 'a')['state'], 'rejected')
        self.assertEqual(tool(b, 'a')['perm'], '你拒绝了')
        feed(b, use('c'), bare('[Request interrupted by user for tool use]'))
        self.assertEqual(tool(b, 'c')['state'], 'interrupted')
        # 被中断的工具在事件里补一行「工具结束 X（被中断）」，排在「中断」前面（定稿 F2）
        log = [(e['step'], e['summary']) for e in b.snapshot()['log']][-2:]
        self.assertEqual(log, [('工具结束', tool(b, 'c')['name'] + '（被中断）'), ('中断', '用户中断')])

    def test_手动压缩开始写明手动(self):
        b = Board()
        feed(b, human('x'), bare('/compact'))
        self.assertEqual(st(b, 'compact'), 'active')
        self.assertEqual(b.snapshot()['log'][-1]['summary'], '开始压缩（手动）')

    def test_工具清单只留最近的(self):
        b = Board()
        feed(b, human('x'), api())
        for i in range(40):
            feed(b, use('t%d' % i), result('t%d' % i))
        ts = live(b)['tools']
        self.assertEqual(len(ts), 24)
        self.assertEqual(ts[-1]['id'], 't39')

    def test_权限原因细分(self):
        cases = [(dict(reason='classifier'), '分类器放行', 'classifier'),
                 (dict(reason='mode'), '模式放行', 'mode'),
                 (dict(reason='subcommandResults'), '命令放行', 'command'),
                 (dict(reason='rule'), '规则放行', 'rule'),
                 (dict(reason='other'), '配置放行', 'config'),
                 (dict(reason=None), '配置放行', 'config'),
                 (dict(source='user_temporary', reason=None, code='unknown'), '你批准了', 'user'),
                 (dict(decision='reject', source='user_reject', reason=None), '你拒绝了', 'user_reject'),
                 (dict(decision='reject', reason='subcommandResults', code='rule:flagSettings'), '规则拒绝', 'rule_reject'),
                 (dict(decision='reject', reason='asyncAgent', code='safetyCheck'), '安全检查拒绝', 'safety_reject')]
        for kw, text, key in cases:
            b = Board()
            feed(b, human('x'), api(), use('a'), result('a', err=kw.get('decision') == 'reject', **kw))
            self.assertEqual(lb(b, 'permission'), text, kw)
            self.assertEqual(live(b)['perm']['counts'][key], 1, kw)
            self.assertEqual(live(b)['perm']['last'], text)

    def test_没有模式记录时从附件推断(self):
        b = Board()
        feed(b, att('auto_mode'))
        self.assertEqual(b.snapshot()['session']['mode'], 'auto')
        feed(b, att('plan_mode'))
        self.assertEqual(b.snapshot()['session']['mode'], 'plan')
        feed(b, att('plan_mode_exit'))
        self.assertEqual(b.snapshot()['session']['mode'], 'auto')
        feed(b, att('auto_mode_exit'))
        self.assertIsNone(b.snapshot()['session']['mode'])
        # 有 permission-mode 记录时以它为准
        feed(b, att('auto_mode'), {'type': 'permission-mode', 'permissionMode': 'default'}, att('plan_mode'))
        self.assertEqual(b.snapshot()['session']['mode'], 'default')

    def test_还没有记录时版本号取进程登记文件(self):
        b = Board()
        b.feed_proc({'status': 'idle', 'version': '2.1.322'})
        self.assertEqual(b.snapshot()['session']['version'], '2.1.322')
        feed(b, api())  # 记录里的版本号（2.1.318）随后覆盖
        self.assertEqual(b.snapshot()['session']['version'], '2.1.318')

    def test_回合开始和结束时刻(self):
        from cc_records import _ts
        b = Board()
        p = human('x')
        feed(b, p)
        t = live(b)['turn']
        self.assertEqual(t['start_ts'], _ts(p))
        self.assertIsNone(t['end_ts'])
        e = turn_end(4200)
        feed(b, api(), text(), e)
        t = live(b)['turn']
        self.assertEqual(t['end_ts'], _ts(e))
        self.assertAlmostEqual(t['ms'], (_ts(e) - _ts(p)) * 1000)

    def test_本轮用时含等你的时间_不知道开始时刻时用durationMs(self):
        from cc_records import _ts
        b = Board()
        p = human('x')
        feed(b, p, api(), use())
        b.feed_proc({'status': 'waiting', 'waitingFor': 'input needed'})
        _clock[0] += 400  # 等你回答 400 秒
        b.feed_proc({'status': 'busy'})
        e = turn_end(60000)
        feed(b, result(), text(req='r2'), e)
        self.assertAlmostEqual(live(b)['turn']['ms'], (_ts(e) - _ts(p)) * 1000)
        self.assertGreater(live(b)['turn']['ms'], 400000)
        b2 = Board()
        feed(b2, turn_end(4200))
        self.assertEqual(live(b2)['turn']['ms'], 4200)

    def test_等待开始时刻(self):
        b = Board()
        b.feed_proc({'status': 'busy'})
        feed(b, human('x'), api())
        self.assertIsNone(live(b)['wait']['kind'])
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        w = live(b)['wait']
        self.assertEqual(w['kind'], 'permission')
        self.assertIsNotNone(w['since'])
        b.feed_proc({'status': 'waiting', 'waitingFor': 'dialog open'})
        self.assertEqual(live(b)['wait']['kind'], 'other')
        self.assertEqual(live(b)['wait']['text'], 'dialog open')
        b.feed_proc({'status': 'busy'})
        self.assertIsNone(live(b)['wait']['kind'])
        self.assertIsNone(live(b)['wait']['since'])

    def test_本轮计数每轮归零(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), att('hook_success', hookEvent='PreToolUse', toolUseID='a'), result('a'),
             att('hook_success', hookEvent='PostToolUse', toolUseID='a'), att('hook_additional_context', hookEvent='PreToolUse', content=['x']),
             {'type': 'system', 'subtype': 'stop_hook_summary', 'hookCount': 1, 'hookErrors': [], 'timestamp': iso()}, turn_end())
        self.assertEqual(live(b)['turn']['tool_calls'], 1)
        self.assertEqual(live(b)['hooks']['turn'], 3)
        self.assertEqual(live(b)['hooks']['by_event']['PreToolUse'], 1)
        feed(b, human('y'))
        self.assertEqual(live(b)['turn']['tool_calls'], 0)
        self.assertEqual(live(b)['hooks']['turn'], 0)
        self.assertEqual(live(b)['hooks']['total'], 3)

    def test_Skills_MCP_延迟加载工具(self):
        b = Board()
        feed(b, att('skill_listing', skillCount=12), att('mcp_instructions_delta', addedNames=['github', 'figma']),
             att('deferred_tools_delta', addedNames=['A', 'B', 'C']), human('x'), api(),
             use('a', 'Skill', {'skill': 'lark'}), use('b', 'mcp__github__search', {'query': 'q'}), use('c', 'ToolSearch', {'query': 'select:A'}),
             result('a'), result('b'), result('c'))
        L = live(b)
        self.assertEqual(L['skills'], {'available': 12, 'turn': 1, 'total': 1})
        self.assertEqual(L['mcp']['servers'], ['figma', 'github'])
        self.assertEqual(L['mcp']['calls'], 1)
        self.assertEqual(L['deferred'], {'listed': 3, 'searches_turn': 1, 'searches': 1})

    def test_记忆份数(self):
        b = Board()
        feed(b, att('instructions', files=[{'path': '/u/CLAUDE.md', 'type': 'User', 'content': 'a'}, {'path': '/p/CLAUDE.md', 'type': 'Project', 'content': 'bb'},
                                            {'path': '/m/MEMORY.md', 'type': 'AutoMem', 'content': 'ccc'}]))
        m = live(b)['memory']
        self.assertEqual(m['files'], 3)
        self.assertEqual(m['by_type'], {'User': 1, 'Project': 1, 'AutoMem': 1})

    def test_模型名和推理强度(self):
        from cc_records import model_name
        self.assertEqual(model_name('claude-opus-5-5'), 'Opus 5.5')
        self.assertEqual(model_name('claude-sonnet-4-5-20250929'), 'Sonnet 4.5')
        self.assertEqual(model_name('claude-haiku-5-5'), 'Haiku 5.5')
        self.assertEqual(model_name('claude-opus-4'), 'Opus 4')
        self.assertEqual(model_name('claude-3-5-sonnet-20241022'), 'Sonnet 3.5')
        self.assertEqual(model_name('gpt-x'), 'gpt-x')
        self.assertIsNone(model_name(None))
        b = Board()
        r = api()
        r['params'] = {'model': 'claude-opus-5-5', 'output_config': {'effort': 'xhigh'}, 'thinking': {'type': 'adaptive'}}
        feed(b, human('x'), r)
        m = live(b)['model']
        self.assertEqual((m['id'], m['name'], m['effort'], m['thinking']), ('claude-opus-5-5', 'Opus 5.5', 'xhigh', 'adaptive'))

    def test_思考段数和时长(self):
        b = Board()
        a1, a2 = think(), think(req='r2')
        a1['thinkingDurationMs'] = 3200
        a2['thinkingDurationMs'] = 800
        feed(b, human('x'), api(), a1, api(), a2)
        t = live(b)['turn']
        self.assertEqual(t['think_blocks'], 2)
        self.assertEqual(t['think_ms'], 4000)
        feed(b, human('y'))
        self.assertEqual(live(b)['turn']['think_ms'], 0)

    def test_任务清单(self):
        b = Board()
        feed(b, human('x'), api(), use('a', 'TaskCreate', {'subject': '定位原因', 'activeForm': '定位原因中', 'description': 'd'}),
             result('a', tur={'task': {'id': '1', 'subject': '定位原因', 'description': 'd'}}),
             use('b', 'TaskCreate', {'subject': '跑测试', 'activeForm': '跑测试中'}), result('b', tur={'task': {'id': '2', 'subject': '跑测试'}}))
        self.assertEqual([(x['id'], x['subject'], x['status']) for x in live(b)['tasks']], [('1', '定位原因', 'pending'), ('2', '跑测试', 'pending')])
        feed(b, use('c', 'TaskUpdate', {'taskId': '1', 'status': 'completed'}), result('c'),
             use('d', 'TaskUpdate', {'taskId': '2', 'status': 'in_progress'}), result('d'))
        ts = live(b)['tasks']
        self.assertEqual([x['status'] for x in ts], ['completed', 'in_progress'])
        self.assertEqual(ts[1]['active'], '跑测试中')
        feed(b, use('e', 'TaskUpdate', {'taskId': '2', 'status': 'deleted'}), result('e'))
        self.assertEqual([x['id'] for x in live(b)['tasks']], ['1'])
        feed(b, use('f', 'TaskUpdate', {'taskId': '1', 'status': 'pending'}), result('f', err=True, decision='reject', source='user_reject', reason=None))
        self.assertEqual(live(b)['tasks'][0]['status'], 'completed')

    def test_文件检查点(self):
        b = Board()
        snap = {'type': 'file-history-snapshot', 'messageId': 'm1', 'isSnapshotUpdate': False, 'snapshot': {'messageId': 'm1', 'timestamp': iso(), 'trackedFileBackups': {}}}
        delta = {'type': 'file-history-delta', 'messageId': 'm2', 'snapshotMessageId': 'm1', 'trackingPath': 'a.txt', 'timestamp': iso(), 'backup': {}}
        feed(b, snap, human('x'), delta, delta)
        c = live(b)['checkpoints']
        self.assertEqual((c['snapshots'], c['edits']), (1, 2))
        self.assertIsNotNone(c['last_ts'])
        self.assertEqual(b.snapshot()['session']['new_types'], [])

    def test_花在哪按轮累计(self):
        b = Board()
        feed(b, human('x'), api(), use('a'), result('a'), api(), text(req='r2'), turn_end())
        sp = live(b)['split']
        self.assertEqual((sp['model'], sp['tools'], sp['gap'], sp['wait']), (3, 1, 2, 0))
        self.assertIsNone(sp['phase'])
        feed(b, human('y'))
        sp = live(b)['split']
        self.assertEqual((sp['model'], sp['tools'], sp['gap']), (0, 0, 0))
        self.assertEqual(sp['phase'], 'gap')
        self.assertIsNotNone(sp['since'])

    def test_等待计入花在哪(self):
        b = Board()
        b.feed_proc({'status': 'busy'})
        feed(b, human('x'), api())
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        self.assertEqual(live(b)['split']['phase'], 'wait')
        b.feed_proc({'status': 'busy'})
        self.assertGreaterEqual(live(b)['split']['wait'], 0)
        self.assertNotEqual(live(b)['split']['phase'], 'wait')

    def test_重放后计时和清单都在(self):
        from cc_records import _ts
        b = Board()
        p = human('x')
        b.replay([p, api(), use('a'), result('a')])
        L = live(b)
        self.assertEqual(L['turn']['start_ts'], _ts(p))
        self.assertEqual(tool(b, 'a')['state'], 'done')
        self.assertEqual(L['split']['model'], 1)

    def test_换会话记住上一个标题(self):
        b = Board('s1')
        feed(b, {'type': 'custom-title', 'customTitle': '修复登录超时'}, human('x'))
        b.switch_session('s2', [human('y')])
        self.assertEqual(live(b)['prev_title'], '修复登录超时')
        self.assertEqual(b.snapshot()['session']['title'], None)


def now_iso(dt=0.0):
    """贴近当前时间的时间戳：要和进程状态的读取时刻（真实时间）比较的测试用。"""
    import time
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() + dt, timezone.utc).isoformat().replace('+00:00', 'Z')


def agent_text(req='ra'):
    return {'type': 'assistant', 'requestId': req, 'timestamp': iso(),
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '子 agent 的回复'}]}}


class 第七步审查修复(unittest.TestCase):
    """15 号件 R-1～R-17 的回归测试。"""

    def test_R1_meta带修改时刻时晚读到的最后一句不改回运行中(self):
        from cc_records import _ts
        b = Board()
        feed(b, human('x'))
        b.feed_meta('a1', {'agentType': 'Explore', 'toolUseId': 'tA'})
        b.feed_record(api('agent:builtin:Explore'), agent='a1')
        last = agent_text()
        # meta 先读到（修改时刻晚于最后一句），最后一句随后才读到
        b.feed_meta('a1', {'agentType': 'Explore', 'toolUseId': 'tA', 'finished': True}, ts=_ts(last) + 0.05)
        b.feed_record(last, agent='a1')
        self.assertEqual(b.agents['a1']['state'], 'done')
        self.assertEqual(st(b, 'agent'), 'done')

    def test_R1_结束之后真有新的调用仍回到运行中(self):
        from cc_records import _ts
        b = Board()
        feed(b, human('x'))
        last = agent_text()
        b.feed_record(last, agent='a1')
        b.feed_meta('a1', {'finished': True}, ts=_ts(last) + 0.05)
        b.feed_record(api('agent:builtin:Explore'), agent='a1')
        self.assertEqual(b.agents['a1']['state'], 'running')

    def test_R2_回放时通知之后又被继续用的子agent是运行中(self):
        b = Board()
        h, r1 = human('x'), api('agent:builtin:Explore')
        n = notif('tA', 'completed')
        r2 = api('agent:builtin:Explore')
        b.replay([h, n], agents={'a1': {'meta': {'agentType': 'Explore', 'toolUseId': 'tA'}, 'records': [r1, r2]}})
        self.assertEqual(b.agents['a1']['state'], 'running')
        self.assertEqual(st(b, 'agent'), 'active')

    def test_R2_回放时meta判结束的不纠正(self):
        b = Board()
        h, r1 = human('x'), api('agent:builtin:Explore')
        n = notif('tA', 'completed')
        r2 = api('agent:builtin:Explore')
        meta = {'agentType': 'Explore', 'toolUseId': 'tA', 'finished': True}
        b.replay([h, n], agents={'a1': {'meta': meta, 'records': [r1, r2], 'meta_ts': None}})
        self.assertEqual(b.agents['a1']['state'], 'done')

    def test_R4_工具在途时到的通知不清场(self):
        b = Board()
        feed(b, human('一'), api(), use('t1'), notif(), result('t1'))
        self.assertEqual(b.stats['inflight_underflow'], 0)
        self.assertEqual(live(b)['turn']['tool_calls'], 1)
        self.assertEqual(live(b)['drawer']['input']['source'], '后台通知')

    def test_R5_重建后计数不翻倍(self):
        b = Board()
        recs = [human('一'), api(), text('r1'), turn_end(), human('二')]
        feed(b, *recs)
        b.rebuild(recs)
        self.assertEqual(cnt(b, 'rounds'), 2)
        self.assertEqual(cnt(b, 'model_calls'), 1)

    def test_R5_读取器记下文件变小(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, 'a.jsonl')
        with open(p, 'w') as f:
            f.write(json.dumps(human('一')) + '\n' + json.dumps(human('二')) + '\n')
        r = LineReader(p)
        self.assertEqual(len(r.read()), 2)
        with open(p, 'w') as f:
            f.write(json.dumps(human('三')) + '\n')
        self.assertEqual(len(r.read()), 1)
        self.assertEqual(r.resets, 1)

    def test_R6_字段形状不对时快照照常(self):
        b = Board()
        feed(b, human('x'), {'type': 'permission-mode', 'permissionMode': ['x']},
             {'type': 'custom-title', 'customTitle': {'a': 1}}, {'type': 'ai-title', 'aiTitle': 3},
             att('instructions', files=[{'path': '/p/CLAUDE.md', 'type': ['User'], 'content': 'x'}]))
        b.feed_meta('a1', {'agentType': {'name': 'Explore'}, 'description': ['d'], 'toolUseId': 7})
        b.feed_record(api('agent:builtin:Explore'), agent='a1')
        b.feed_record(notif('', 'failed'))
        s = b.snapshot()
        json.dumps(s)
        self.assertIsNone(s['session']['mode'])
        self.assertIsNone(s['session']['title'])
        self.assertIsNone(s['agents'][0]['type'])
        self.assertEqual(s['session']['parse_errors'], 0)

    def test_R6_快照某块出错时沿用上一次并计数(self):
        b = Board()
        feed(b, human('x'))
        good = b.snapshot()['live']
        orig = b._live
        b._live = lambda: 1 / 0
        s = b.snapshot()
        self.assertEqual(s['live'], good)
        self.assertEqual(s['session']['parse_errors'], 1)
        self.assertEqual(b.snapshot()['session']['parse_errors'], 1)  # 一直出错时不累加
        b._live = orig
        self.assertEqual(b.snapshot()['session']['parse_errors'], 0)

    def test_R6_第一次就出错时给空的一块(self):
        b = Board()
        b._blocks = lambda: 1 / 0
        s = b.snapshot()
        self.assertEqual(s['blocks'], Board()._blocks())

    def test_R6_doctor查得到快照出错(self):
        import cc_records
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, 'p'))
        with open(os.path.join(d, 'p', 's.jsonl'), 'w') as f:
            f.write(json.dumps(human('x')) + '\n')
        orig = cc_records.Board._blocks
        cc_records.Board._blocks = lambda self: 1 / 0
        try:
            r = doctor(d)
        finally:
            cc_records.Board._blocks = orig
        self.assertGreater(r['crashes'], 0)

    def test_R7_回放时子agent启动取它自己第一条记录的时间(self):
        from cc_records import _ts
        b = Board()
        h = human('x')
        r1, r2 = api('agent:builtin:Explore'), api('agent:builtin:Explore')
        b.replay([h], agents={'a1': {'meta': {'agentType': 'Explore'}, 'records': [r1]},
                              'a2': {'meta': {'agentType': 'Explore'}, 'records': [r2]}})
        starts = [e['ts'] for e in b.snapshot()['log'] if e['step'] == '子 agent' and e['summary'] == '启动']
        self.assertEqual(starts, [_ts(r1), _ts(r2)])

    def test_R8_中断后到的回合用时不改结束时刻(self):
        from cc_records import _ts
        b = Board()
        intr = None
        feed(b, human('一'), api(), use())
        intr = bare('[Request interrupted by user for tool use]')
        feed(b, intr)
        _clock[0] += 30
        feed(b, turn_end(999))
        T = live(b)['turn']
        self.assertEqual(T['end_ts'], _ts(intr))
        self.assertAlmostEqual(T['ms'], (T['end_ts'] - T['start_ts']) * 1000)

    def _rejected(self, ts):
        r = result('t1', err=True, decision=None, tur='User rejected tool use')
        r['timestamp'] = ts
        return r

    def test_R9_等批准刚结束就到的拒绝仍算你拒绝了(self):
        b = Board()
        feed(b, human('一'), api(), use('t1'))
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        b.feed_proc({'status': 'busy'})  # 先读到「已不在等」
        feed(b, self._rejected(now_iso()))
        self.assertEqual(tool(b, 't1')['state'], 'rejected')
        self.assertEqual(live(b)['perm']['counts'], {'user_reject': 1})

    def test_R9_批准后过了几秒按Esc算中断(self):
        b = Board()
        feed(b, human('一'), api(), use('t1'))
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        b.feed_proc({'status': 'busy'})
        feed(b, self._rejected(now_iso(5)))
        self.assertEqual(tool(b, 't1')['state'], 'interrupted')
        self.assertEqual(live(b)['perm']['counts'], {})

    def test_R10_处理出错的记录条数进快照(self):
        b = Board()
        feed(b, human('x'), api())
        b.feed_record({'type': 'assistant', 'requestId': 'r1', 'timestamp': iso(),
                       'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': ['x'], 'name': 'Bash', 'input': {}}]}})
        self.assertEqual(b.snapshot()['session']['parse_errors'], b.parse_errors)
        self.assertGreater(b.parse_errors, 0)

    def test_R11_第一次调用进行中时累计不小于本轮(self):
        b = Board()
        feed(b, human('x'), api())
        m = live(b)['drawer']['model']
        self.assertEqual((m['calls_turn'], m['calls_total']), (1, 1))
        feed(b, text('r1'))
        m = live(b)['drawer']['model']
        self.assertEqual((m['calls_turn'], m['calls_total']), (1, 1))

    def test_R12_无头会话的请求点亮模型调用(self):
        b = Board()
        p = human('x')
        p['entrypoint'] = 'sdk-cli'
        feed(b, p, api('sdk'))
        self.assertEqual(st(b, 'model'), 'active')
        self.assertEqual(cnt(b, 'loop'), 1)

    def test_R12_交互会话的sdk请求不算主线程(self):
        b = Board()
        feed(b, human('x'), api('sdk'))
        self.assertEqual(st(b, 'model'), 'idle')

    def test_R13_等批准直接转成等回答时权限节点复位(self):
        b = Board()
        feed(b, human('x'), api(), use('t1'))
        b.feed_proc({'status': 'waiting', 'waitingFor': 'permission prompt'})
        b.feed_proc({'status': 'waiting', 'waitingFor': 'input needed'})
        self.assertNotEqual(st(b, 'permission'), 'waiting')
        self.assertEqual(st(b, 'tool'), 'waiting')
        self.assertEqual(live(b)['wait']['kind'], 'input')


class 抽屉数字(unittest.TestCase):
    """抽屉「这一站」的原始数字（D-111），按站逐字段核（T-1：变异检查发现这里几乎没测）。"""

    def test_模型这一站本轮和累计(self):
        b = Board()
        feed(b, human('一'), api(), text('r1'), api(), text('r2'), turn_end(), human('二'), api(), text('r3'))
        m = live(b)['drawer']['model']
        self.assertEqual((m['calls_turn'], m['calls_total']), (1, 3))

    def test_记忆这一站分类(self):
        b = Board()
        feed(b, human('x'), att('instructions', files=[
            {'path': '/u/CLAUDE.md', 'type': 'User', 'content': 'ab'},
            {'path': '/p/CLAUDE.md', 'type': 'Project', 'content': 'abc'},
            {'path': '/p/CLAUDE.local.md', 'type': 'Local', 'content': 'abcd'},
            {'path': '/m/MEMORY.md', 'type': 'AutoMem', 'content': 'a'},
            {'path': '/x/X.md', 'type': 'Managed', 'content': 'abcde'}]))
        m = live(b)['drawer']['memory']
        self.assertEqual(m['user'], {'size': 2, 'count': 1})
        self.assertEqual(m['project'], {'size': 7, 'count': 2})
        self.assertEqual(m['auto'], {'size': 1, 'count': 1})
        self.assertEqual(m['other'], {'size': 5, 'count': 1})

    def test_权限这一站本轮和本会话(self):
        b = Board()
        feed(b, human('一'), api(), use('a'), result('a', source='user_temporary', reason=None), turn_end(),
             human('二'), api(), use('b'), result('b', reason='classifier'))
        p = live(b)['drawer']['perm']
        self.assertEqual(p['turn'], {'classifier': 1})
        self.assertEqual(p['session'], {'user': 1, 'classifier': 1})

    def test_子agent这一站通知计数(self):
        b = Board()
        feed(b, human('x'), notif('', 'completed'), notif('', 'failed'), notif('', 'stopped'))
        self.assertEqual(live(b)['drawer']['agents']['notif'], {'completed': 1, 'failed': 1, 'stopped': 1})

    def test_输入这一站来源和轮次(self):
        b = Board()
        feed(b, human('一'), turn_end(), human('二'))
        i = live(b)['drawer']['input']
        self.assertEqual((i['source'], i['summary'], i['rounds']), ('你输入', '二', 2))


if __name__ == '__main__':
    unittest.main(verbosity=1)
