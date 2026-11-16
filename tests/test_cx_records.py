#!/usr/bin/env python3
"""Codex 记录解析的测试（20 号件 §八）。样本全是合成的，不放真实记录。
在仓根运行：python3 tests/test_cx_records.py"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, '..', 'harness-board', 'scripts')))

import board  # noqa: E402
from cc_records import LineReader  # noqa: E402
from cx_records import CodexBoard, context_left, host_of, model_name, parse_script  # noqa: E402

T0 = 1_800_000_000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z')


class S:
    """按时间顺序造 Codex 记录。"""

    def __init__(self, originator='codex-tui', approval='on-request', sandbox='workspace-write', reviewer='user'):
        self.t = T0
        self.rows = []
        self.n = 0
        self.add('session_meta', {'id': 'sess-1', 'originator': originator, 'cli_version': '0.166.0', 'source': 'cli',
                                  'base_instructions': {'text': 'x' * 500}})
        self.policy = (approval, sandbox, reviewer)

    def add(self, typ, payload, dt=0.1):
        self.t += dt
        self.rows.append({'timestamp': iso(self.t), 'type': typ, 'payload': payload})
        return self

    def ev(self, kind, dt=0.1, **kw):
        return self.add('event_msg', dict(type=kind, **kw), dt)

    def item(self, item, dt=0.1):
        return self.ev('item_completed', dt, item=item)

    def turn(self, text='修一下登录', trigger='user', attribution=True):
        a, s, r = self.policy
        if attribution:
            self.ev('task_started', turn_id='t%d' % self.n, started_at=int(self.t), model_context_window=828400,
                    turn_attribution={'turn_trigger': trigger})
        else:
            self.ev('task_started', turn_id='t%d' % self.n, model_context_window=828400)
        self.add('turn_context', {'model': 'gpt-6.1-sol', 'effort': 'high', 'approval_policy': a, 'sandbox_policy': {'type': s},
                                  'approvals_reviewer': r})
        if text is not None:
            self.item({'type': 'UserMessage', 'content': [{'type': 'text', 'text': text}]})
        return self

    def usage(self, inp=20000, cached=10000, out=100):
        self.n += 1
        self.add('token_usage_record', {'response_id': 'resp_%d' % self.n,
                                        'usage': {'input_tokens': inp, 'cached_input_tokens': cached, 'output_tokens': out}})
        return self.ev('token_count', info={'last_token_usage': {'total_tokens': inp + out}, 'model_context_window': 828400})

    def script(self, cid, src):
        return self.add('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': cid, 'input': src})

    def fn(self, cid, name, args='{}'):
        return self.add('response_item', {'type': 'function_call', 'name': name, 'call_id': cid, 'arguments': args})

    def out(self, cid, text, dt=0.1, custom=True):
        return self.add('response_item', {'type': 'custom_tool_call_output' if custom else 'function_call_output', 'call_id': cid,
                                          'output': [{'type': 'input_text', 'text': text}] if custom else text}, dt)

    def cmd(self, command, status='completed', dt=0.1):
        return self.item({'type': 'CommandExecution', 'command': ['/bin/zsh', '-lc', command], 'status': status,
                          'exit_code': 0 if status == 'completed' else 1}, dt)

    def done(self, dt=0.2, **kw):
        return self.ev('task_complete', dt, turn_id='t', duration_ms=int((self.t + dt - T0) * 1000), **kw)

    def board(self, children=None):
        b = CodexBoard('sess-1')
        for r in self.rows:
            b.feed_record(r)
        return b


def snap(b):
    return b.snapshot()


def node(b, n):
    return b.nodes[n]['state'], b.nodes[n]['label']


class 基本(unittest.TestCase):
    def test_纯问答(self):
        s = S().turn().item({'type': 'Reasoning'}).item({'type': 'AgentMessage', 'phase': 'final_answer'}).usage().done()
        b = s.board()
        self.assertEqual(node(b, 'prompt')[0], 'done')
        self.assertEqual(node(b, 'turn')[0], 'done')
        self.assertEqual(b.c['rounds'], 1)
        self.assertEqual(b.c['loop'], 1)
        self.assertEqual(b._md['think_blocks'], 1)
        L = snap(b)['live']
        self.assertEqual(L['product'], 'codex')
        self.assertEqual(L['host'], 'terminal')
        self.assertEqual(L['model']['name'], 'GPT-6.1-Sol')
        self.assertEqual(snap(b)['session']['version'], '0.166.0')
        self.assertEqual(snap(b)['session']['mode'], 'on-request')
        self.assertEqual(L['context']['window'], 828400)
        self.assertEqual(L['context']['left_pct'], context_left(20100, 828400))
        self.assertIsNone(L['hooks'])

    def test_模型调用中途是进行中(self):
        b = S().turn().board()
        self.assertEqual(node(b, 'model')[0], 'active')
        self.assertEqual(b.c['loop'], 1)

    def test_单条命令(self):
        s = S().turn().script('c1', 'const r = await tools.exec_command({cmd:"ls -la src"}); text(r);')
        b = s.board()
        self.assertEqual(node(b, 'tool'), ('active', '执行中'))
        self.assertEqual(node(b, 'model')[0], 'done')
        t = snap(b)['live']['tools'][-1]
        self.assertEqual((t['name'], t['raw'], t['cat'], t['target'], t['state']), ('命令', 'exec_command', 'Shell', 'ls -la src', 'running'))
        s.cmd('ls -la src').out('c1', 'Script completed\nWall time 0.1 seconds').usage().done()
        b = s.board()
        t = snap(b)['live']['tools'][-1]
        self.assertEqual((t['state'], t['perm']), ('done', '沙箱内运行'))
        self.assertEqual(snap(b)['live']['perm']['counts'], {'sandbox': 1})
        self.assertEqual(node(b, 'turn')[0], 'done')

    def test_工具结果之后推模型调用(self):
        s = S().turn().script('c1', 'tools.exec_command({cmd:"pwd"})').usage().cmd('pwd').out('c1', 'Script completed')
        b = s.board()
        self.assertEqual(node(b, 'model')[0], 'active')
        self.assertEqual(b.c['loop'], 2)

    def test_命令出错(self):
        b = S().turn().script('c1', 'tools.exec_command({cmd:"false"})').cmd('false', 'failed').out('c1', 'Script completed').usage().done().board()
        t = snap(b)['live']['tools'][-1]
        self.assertEqual(t['state'], 'error')
        self.assertEqual(b._lv['errors_turn'], 1)

    def test_长命令先返回再续等(self):
        s = (S().turn().script('c1', 'tools.exec_command({cmd:"sleep 15", yield_time_ms:1000})')
             .out('c1', 'Script running with cell ID 3\nWall time 1.0 seconds').usage()
             .fn('w1', 'wait', '{"cell_id":"3"}'))
        b = s.board()
        self.assertEqual(snap(b)['live']['tools'][-1]['state'], 'running')
        self.assertEqual(b.c['inflight'], 1)
        s.cmd('sleep 15', dt=14).out('w1', 'Script completed', custom=False).usage().done()
        b = s.board()
        self.assertEqual(snap(b)['live']['tools'][-1]['state'], 'done')
        self.assertEqual(len(snap(b)['live']['tools']), 1, 'wait 不算新工具')

    def test_续等命令输出的脚本(self):
        b = S().turn().script('c2', 'text(await tools.write_stdin({session_id:5, chars:""}))').board()
        t = snap(b)['live']['tools'][-1]
        self.assertEqual((t['name'], t['raw'], t['target']), ('命令', 'write_stdin', '等待命令输出'))

    def test_改文件和MCP的名字(self):
        b = (S().turn().script('c1', 'await tools.apply_patch("*** Begin Patch")')
             .script('c2', 'await tools.mcp__github__search_issues({query:"x"})').board())
        ts = {t['raw']: t for t in snap(b)['live']['tools']}
        self.assertEqual((ts['apply_patch']['name'], ts['apply_patch']['cat']), ('改文件', '文件'))
        self.assertEqual((ts['mcp__github__search_issues']['name'], ts['mcp__github__search_issues']['cat']), ('MCP', 'MCP'))
        self.assertEqual(snap(b)['live']['mcp']['calls'], 1)

    def test_其他工具用原名不带权限文字(self):
        b = S().turn().fn('f1', 'sleep', '{"duration_ms":1000}').item({'type': 'Extension', 'kind': 'clock.sleep'}).out('f1', '{}', custom=False).board()
        t = snap(b)['live']['tools'][-1]
        self.assertEqual((t['name'], t['state'], t['perm']), ('sleep', 'done', None))


class 批准(unittest.TestCase):
    ESC = 'await tools.exec_command({cmd:"rm -rf /tmp/x", sandbox_permissions:"require_escalated"})'

    def test_申请出沙箱推测在等你批准(self):
        b = S().turn().script('c1', self.ESC).board()
        self.assertEqual(node(b, 'permission'), ('waiting', '可能在等你批准'))
        L = snap(b)['live']
        self.assertEqual(L['wait']['kind'], 'permission')
        self.assertTrue(L['wait']['guess'])

    def test_批准后命令跑完(self):
        b = S().turn().script('c1', self.ESC).cmd('rm -rf /tmp/x', dt=20).out('c1', 'Script completed').usage().done().board()
        self.assertEqual(node(b, 'permission'), ('passed', '你批准了'))
        self.assertEqual(snap(b)['live']['perm']['counts'], {'user': 1})
        self.assertIsNone(snap(b)['live']['wait']['kind'])

    def test_命令子进程出现就结束推测(self):
        b = S().turn().script('c1', self.ESC).board()
        b.command_running(T0 + 9)
        self.assertEqual(node(b, 'permission'), ('passed', '你批准了'))
        self.assertFalse(b.guess_waiting())

    def test_批准窗口里拒绝(self):
        b = (S().turn().script('c1', self.ESC).out('c1', 'aborted by user after 3.0s', dt=3)
             .add('response_item', {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': '<turn_aborted>'}],
                                    'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['generic.turn_aborted']}})
             .ev('turn_aborted', reason='interrupted', duration_ms=3200).board())
        t = snap(b)['live']['tools'][-1]
        self.assertEqual((t['state'], t['perm']), ('rejected', '你拒绝了'))
        self.assertEqual(node(b, 'turn')[0], 'interrupted')
        self.assertEqual(node(b, 'model')[0], 'done', '被拒之后不再推模型调用')
        self.assertEqual(snap(b)['live']['perm']['counts'], {'user_reject': 1})

    def test_运行中按Esc之后后台命令跑完(self):
        s = (S().turn().script('c1', 'tools.exec_command({cmd:"sleep 30"})').out('c1', 'aborted by user after 5.0s', dt=5)
             .ev('turn_aborted', reason='interrupted').cmd('sleep 30', dt=20))
        b = s.board()
        self.assertEqual(snap(b)['live']['tools'][-1]['state'], 'interrupted')
        self.assertEqual(node(b, 'turn')[0], 'interrupted')
        self.assertEqual(snap(b)['live']['background_done'], 1)
        self.assertIn('后台命令结束', [e['step'] for e in b._log])

    def test_完全放行时不推测(self):
        b = S(approval='never', sandbox='danger-full-access').turn().script('c1', self.ESC).board()
        self.assertEqual(node(b, 'permission')[0], 'idle')
        b2 = S(approval='never', sandbox='danger-full-access').turn().script('c1', 'tools.exec_command({cmd:"ls"})').cmd('ls').out('c1', 'Script completed').board()
        self.assertEqual(snap(b2)['live']['tools'][-1]['perm'], '模式放行')

    def _review(self, verdict):
        s = S(reviewer='auto_review').turn().script('c1', self.ESC)
        b = s.board()
        self.assertNotEqual(node(b, 'permission')[0], 'waiting', '交给自动审核时不推测在等你')
        b.feed_child('g1', [{'timestamp': iso(T0 + 5), 'type': 'session_meta', 'payload': {'id': 'g1', 'parent_thread_id': 'sess-1'}}], 'guardian')
        self.assertEqual(node(b, 'permission'), ('active', '自动审核中'))
        self.assertEqual(snap(b)['live']['wait']['kind'], 'review')
        self.assertEqual(b._phase(), 'tools', '自动审核算工具段（D-125）')
        b.feed_child('g1', [{'timestamp': iso(T0 + 12), 'type': 'event_msg',
                             'payload': {'type': 'task_complete', 'last_agent_message': json.dumps({'outcome': verdict})}}], 'guardian')
        return s, b

    def test_自动审核放行(self):
        s, b = self._review('allow')
        self.assertEqual(node(b, 'permission'), ('passed', '自动审核放行'))
        for r in S(reviewer='auto_review').rows:
            pass
        rows = [{'timestamp': iso(T0 + 13), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'status': 'completed', 'command': ['zsh', '-lc', 'rm']}}},
                {'timestamp': iso(T0 + 13.1), 'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': 'Script completed'}}]
        for r in rows:
            b.feed_record(r)
        self.assertEqual(snap(b)['live']['perm']['counts'], {'review': 1}, '只计一次')
        self.assertEqual(snap(b)['live']['tools'][-1]['perm'], '自动审核放行')

    def test_同一个审核会话第二次审核(self):
        # 实测：后面的出沙箱命令接着用同一个自动审核会话，一份记录里多次开始、结束
        s, b = self._review('allow')
        for r in [{'timestamp': iso(T0 + 13), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'status': 'completed', 'command': ['zsh', '-lc', 'rm']}}},
                  {'timestamp': iso(T0 + 13.1), 'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': 'Script completed'}},
                  {'timestamp': iso(T0 + 14), 'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'c2', 'input': self.ESC}}]:
            b.feed_record(r)
        b.feed_child('g1', [{'timestamp': iso(T0 + 15), 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'g2'}}], 'guardian')
        self.assertEqual(node(b, 'permission'), ('active', '自动审核中'))
        b.feed_child('g1', [{'timestamp': iso(T0 + 17), 'type': 'event_msg',
                             'payload': {'type': 'task_complete', 'last_agent_message': json.dumps({'outcome': 'deny'})}}], 'guardian')
        self.assertEqual(node(b, 'permission'), ('rejected', '自动审核拒绝'))

    def test_自动审核没留记录也记放行(self):
        # 实测：交给自动审核的出沙箱命令，Codex 有时直接放行、不起审核会话
        b = S(reviewer='auto_review').turn().script('c1', self.ESC).cmd('rm -rf /tmp/x').out('c1', 'Script completed').board()
        self.assertEqual(snap(b)['live']['tools'][-1]['perm'], '自动审核放行')
        self.assertEqual(snap(b)['live']['perm']['counts'], {'review': 1})

    def test_自动审核拒绝(self):
        s, b = self._review('deny')
        self.assertEqual(node(b, 'permission'), ('rejected', '自动审核拒绝'))
        b.feed_record({'timestamp': iso(T0 + 13), 'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': 'Script failed: rejected'}})
        self.assertEqual(snap(b)['live']['perm']['counts'], {'review_reject': 1})
        self.assertEqual(snap(b)['live']['tools'][-1]['state'], 'rejected')


class 回合(unittest.TestCase):
    def test_异步提问到回答(self):
        s = S().turn().fn('q1', 'request_user_input_async', '{"questions":[]}').usage().out('q1', '{"accepted":true}', custom=False)
        b = s.board()
        self.assertEqual(snap(b)['live']['wait']['kind'], 'input')
        self.assertEqual(node(b, 'tool'), ('waiting', '等你回答'))
        s.item({'type': 'UserMessage', 'content': [{'type': 'text', 'text': '<send_user_message_question_reply>\n[{"answer":"A"}]'}]}, dt=8)
        b = s.board()
        self.assertIsNone(snap(b)['live']['wait']['kind'])
        self.assertEqual(b.c['rounds'], 1, '回答不算新输入')
        self.assertEqual(node(b, 'model')[0], 'active')

    def test_手动压缩不算回合(self):
        s = S().turn().usage().done()
        s.ev('task_started', turn_id='tc', model_context_window=828400).add('compacted', {'window_number': 1}, 13).item({'type': 'ContextCompaction'}).done()
        b = s.board()
        self.assertEqual(b.c['rounds'], 1)
        L = snap(b)['live']
        self.assertEqual((L['compact']['count'], L['compact']['manual']), (1, 1))
        self.assertEqual(node(b, 'compact')[0], 'done')
        self.assertEqual(b.stats['stuck_after_turn'], 0)

    def test_回合里的自动压缩(self):
        b = S().turn().usage().add('compacted', {'window_number': 1}).usage().done().board()
        self.assertEqual(snap(b)['live']['compact']['auto'], 1)

    def test_模型调用出错(self):
        b = S().turn().done(error={'message': 'overloaded', 'codex_error_info': 'server_overloaded'}).board()
        self.assertEqual(node(b, 'model')[0], 'error')
        self.assertEqual(node(b, 'turn')[0], 'done')

    def test_没有来源的旧版本(self):
        b = S().turn(attribution=False).usage().done().board()
        self.assertEqual(b.c['rounds'], 1)
        self.assertEqual(b._in['source'], '你输入')

    def test_自动化回合不算轮次(self):
        b = S().turn(text='<heartbeat>tick</heartbeat>', trigger='automation_heartbeat_scheduled').usage().done().board()
        self.assertEqual(b.c['rounds'], 0)
        self.assertEqual(b._in['source'], '自动化')
        self.assertEqual(node(b, 'turn')[0], 'done')

    def test_排队的输入(self):
        b = S().turn(trigger='queue').board()
        self.assertEqual((b.c['rounds'], b._in['source']), (1, '你排队的输入'))

    def test_插话(self):
        b = S().turn().usage().item({'type': 'UserMessage', 'content': [{'type': 'text', 'text': '顺便看下测试'}]}).board()
        self.assertEqual(b._in['interjected'], 1)
        self.assertEqual(b.c['rounds'], 1)

    def test_第二轮重置节点(self):
        b = S().turn().script('c1', 'tools.exec_command({cmd:"ls"})').cmd('ls').out('c1', 'Script completed').usage().done().turn('再来').board()
        self.assertEqual(b.c['rounds'], 2)
        self.assertEqual(node(b, 'tool')[0], 'idle')
        self.assertEqual(node(b, 'turn')[0], 'idle')

    def test_系统注入算上下文和记忆(self):
        # 真实顺序：task_started、turn_context、系统注入、你的输入（19 号件 §三）
        s = S().turn(text=None).add('response_item', {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'm' * 300}, {'type': 'input_text', 'text': 'p'}],
                                      'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['memories.instructions', 'permissions.instructions']}})
        s.add('world_state', {'state': {'agents_md': {'text': 'a' * 120}, 'host_skills': {'entries': {'a': {}, 'b': {}}}}})
        b = s.item({'type': 'UserMessage', 'content': [{'type': 'text', 'text': '看一下'}]}).board()
        self.assertEqual(b._cx['round_ctx'], 2)
        self.assertEqual(node(b, 'context')[0], 'done')
        self.assertEqual(node(b, 'memory')[0], 'done')
        L = snap(b)['live']
        self.assertEqual(L['memory']['by_type'], {'CodexMemory': 1, 'AgentsMd': 1})
        self.assertEqual(L['skills']['available'], 2)

    def test_用了skill(self):
        b = S().turn().add('response_item', {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 's'}],
                                             'internal_chat_message_metadata_passthrough': {'content_item_kinds': ['skills.selected_skill_instructions']}}).board()
        self.assertEqual(snap(b)['live']['skills']['turn'], 1)


class 子agent(unittest.TestCase):
    def test_起止和被停掉(self):
        s = S().turn().fn('a1', 'spawn_agent', '{"task_name":"reader"}').item(
            {'type': 'SubAgentActivity', 'kind': 'started', 'agent_thread_id': 'ag1', 'agent_path': '/root/reader', 'model': 'gpt-6.1-sol'}).out('a1', '{}', custom=False)
        b = s.board()
        self.assertEqual(node(b, 'agent')[0], 'active')
        self.assertEqual(snap(b)['agents'][0]['type'], '/root/reader')
        b.feed_child('ag1', [{'timestamp': iso(T0 + 3), 'type': 'response_item',
                              'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'x', 'input': 'tools.exec_command({cmd:"cat a.md"})'}}], 'agent')
        self.assertEqual(snap(b)['agents'][0]['step'], '命令 cat a.md')
        s.item({'type': 'SubAgentActivity', 'kind': 'completed', 'agent_thread_id': 'ag1'})
        self.assertEqual(node(s.board(), 'agent')[0], 'done')
        s2 = S().turn().item({'type': 'SubAgentActivity', 'kind': 'started', 'agent_thread_id': 'ag2'}).item(
            {'type': 'SubAgentActivity', 'kind': 'interrupted', 'agent_thread_id': 'ag2'})
        b2 = s2.board()
        self.assertEqual(snap(b2)['agents'][0]['step'], '被你停止')
        self.assertEqual(node(b2, 'agent')[0], 'done')


class 宽容(unittest.TestCase):
    def test_坏行和没见过的类型(self):
        s = S().turn().add('mystery', {}).ev('brand_new').item({'type': 'Hologram'})
        b = CodexBoard('sess-1')
        for r in s.rows:
            b.feed_record(r)
        b.feed_line(LineReader.BAD_LINE)
        self.assertEqual(b.bad_lines, 1)
        self.assertEqual(b.new_types, {'mystery', 'event/brand_new', 'item/Hologram'})
        json.dumps(b.snapshot())

    def test_字段形状不对不崩溃(self):
        b = CodexBoard('sess-1')
        for r in ({'type': 'session_meta', 'payload': 'oops'}, {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': 5}},
                  {'type': 'turn_context', 'payload': {'sandbox_policy': 'x', 'model': 3}}, {'type': 'token_usage_record', 'payload': {'usage': 'x'}},
                  {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'c', 'input': None}}):
            b.feed_record(r)
        json.dumps(b.snapshot())
        self.assertEqual(b.parse_errors + b.stats['snapshot_errors'], 0)


class 小函数(unittest.TestCase):
    def test_来源(self):
        self.assertEqual(host_of('codex-tui'), 'terminal')
        self.assertEqual(host_of('Codex Desktop'), 'desktop')
        self.assertEqual(host_of('codex_work_desktop'), 'desktop')
        self.assertIsNone(host_of('codex_exec'))
        b = S(originator='Codex Desktop').turn().board()
        self.assertEqual(snap(b)['live']['host'], 'desktop')

    def test_模型名(self):
        self.assertEqual(model_name('gpt-6.1-sol'), 'GPT-6.1-Sol')
        self.assertEqual(model_name('codex-auto-review'), 'codex-auto-review')

    def test_上下文百分比(self):
        self.assertEqual(context_left(28814, 828400), 98)   # 实测 Codex 界面「98% left」
        self.assertIsNone(context_left(None, 828400))
        self.assertEqual(context_left(5000, 828400), 100)

    def test_认脚本(self):
        names, cmd, esc = parse_script('const r = await tools.exec_command({cmd:"echo \\"hi\\" > a.txt", sandbox_permissions:"require_escalated"}); tools.wait()')
        self.assertEqual((names, cmd, esc), (['exec_command', 'wait'], 'echo "hi" > a.txt', True))
        self.assertEqual(parse_script("tools.exec_command({cmd:'ls -la'})")[1], 'ls -la')
        self.assertEqual(parse_script(None), ([], None, False))


class 认会话(unittest.TestCase):
    def test_顺序(self):
        cfg = '/nonexistent-claude'
        env = {'CODEX_THREAD_ID': 'cx-1', 'CLAUDE_CODE_SESSION_ID': 'cc-1'}
        self.assertEqual(board.resolve_session('', cfg, env), ('cx-1', 'codex'), 'Codex 的优先：从 Claude Code 里起的 Codex 会继承 Claude 的变量')
        self.assertEqual(board.resolve_session(None, cfg, {'CLAUDE_CODE_SESSION_ID': 'cc-1'}), ('cc-1', 'claude'))
        self.assertEqual(board.resolve_session('', cfg, {}), (None, None))
        self.assertEqual(board.resolve_session('cc-9', cfg, env), ('cc-9', 'claude'), '显式给的、哪边都找不到：按 Claude Code')
        self.assertEqual(board.resolve_session('cx-1', cfg, env), ('cx-1', 'codex'))


if __name__ == '__main__':
    unittest.main()
