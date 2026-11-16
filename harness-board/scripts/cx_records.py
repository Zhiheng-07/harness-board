#!/usr/bin/env python3
"""Codex 记录解析（规则见 20 号件，D-119～D-122、D-125）。

Codex 的 rollout 记录（~/.codex/sessions/年/月/日/rollout-<时间>-<会话 id>.jsonl）翻成和 Claude Code 一样的节点状态，
状态机、快照、八大块都沿用 cc_records.Board，这里只重写「一条记录怎么变成事件」。只读取，不写任何文件。
记录格式是 Codex 的内部格式，每次升级都可能变：看不懂就计数，不崩溃。只用 Python 标准库，语法兼容 Python 3.9。
"""
import collections
import glob
import json
import os
import re
import shutil
import subprocess
import time

from cc_records import Board, LineReader, _int, _k, _s, _summary, _ts

# 卡片上的名字（D-125）：内部名太长也看不懂；抽屉和事件里保留原名
FRIENDLY = {'exec_command': '命令', 'write_stdin': '命令', 'apply_patch': '改文件', 'spawn_agent': '派子 agent', 'view_image': '看图片'}
CATEGORY = {'exec_command': 'Shell', 'write_stdin': 'Shell', 'apply_patch': '文件', 'view_image': '文件', 'web__run': '网络', 'web_search': '网络'}
AGENT_TOOLS = frozenset(('spawn_agent', 'send_message', 'wait_agent', 'followup_task', 'interrupt_agent', 'list_agents'))
# 回合来源（task_started 的 turn_trigger）
TRIGGER_PROMPT = frozenset(('user', 'composer', 'exec', 'edit_user_message', 'plan_implementation', 'plan_feedback'))
TRIGGER_SOURCE = {'queue': '你排队的输入', 'automation_heartbeat_scheduled': '自动化', 'send_user_message_async_question': '你的回答',
                  'capacity_retry_manual': '自动继续', 'resume_interrupted_task': '自动继续', 'app_tool_create_thread': '新建对话'}
# 读数「模式」：批准方式的中文简称（D-125）；页面按语言选，这里给原名
HIDDEN_EVENTS = frozenset(('thread_settings_applied', 'thread_goal_updated'))
HIDDEN_ITEMS = frozenset(('CollabAgentToolCall', 'FunctionCallOutput', 'EnteredReviewMode', 'ExitedReviewMode'))
INNER_ITEMS = frozenset(('CommandExecution', 'McpToolCall', 'FileChange', 'WebSearch', 'ImageView', 'DynamicToolCall'))
# Codex 界面「Context N% left」的口径：扣掉 12000 的基线（观察，20 号件 §五）
CTX_BASELINE = 12000
_TOOL_RE = re.compile(r'tools\.([A-Za-z_][\w]*)\s*\(')
_CMD_RE = re.compile(r'\bcmd\s*:\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`([^`]*)`)')


def host_of(originator):
    """会话来源 → 页面「去哪里处理」的说法（D-116）：终端、桌面应用，其他不提地点。"""
    if originator == 'codex-tui':
        return 'terminal'
    if originator in ('Codex Desktop', 'codex_work_desktop'):
        return 'desktop'
    return None


def model_name(mid):
    """Codex 的模型名照它界面的写法：gpt-6.1-sol → GPT-6.1-Sol。认不出就原样。"""
    if not isinstance(mid, str) or not mid:
        return None
    if mid.lower().startswith('gpt-'):
        return 'GPT-' + '-'.join(p[:1].upper() + p[1:] for p in mid[4:].split('-'))
    return mid


def context_left(used, window):
    """剩余上下文的百分比（和 Codex 界面同一口径）；算不了返回 None。"""
    if not isinstance(used, int) or not isinstance(window, int) or window <= CTX_BASELINE:
        return None
    left = 1 - max(0, used - CTX_BASELINE) / (window - CTX_BASELINE)
    return max(0, min(100, round(left * 100)))


def _text_items(content):
    if isinstance(content, list):
        return ''.join(c.get('text') or '' for c in content if isinstance(c, dict) and isinstance(c.get('text'), str))
    return content if isinstance(content, str) else ''


def _out_text(out):
    """工具结果的文字（字符串，或 input_text 块的列表）。"""
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return '\n'.join(c.get('text') or '' for c in out if isinstance(c, dict) and isinstance(c.get('text'), str))
    return ''


def parse_script(src):
    """从脚本文字里认出调了哪些工具、第一条命令（D-121：跑的时候只能从文字认）。"""
    src = src if isinstance(src, str) else ''
    names = _TOOL_RE.findall(src)
    m = _CMD_RE.search(src)
    cmd = None
    if m:
        cmd = next(g for g in m.groups() if g is not None)
        try:
            cmd = json.loads('"%s"' % cmd.replace('"', '\\"')) if m.group(1) is None else json.loads('"%s"' % cmd)
        except ValueError:
            pass
    return names, cmd, 'require_escalated' in src


class CodexBoard(Board):
    """一个 Codex 会话的看板状态。和 Board 共用节点、计数、快照；产品是 codex。"""

    product = 'codex'

    def _reset_session(self):
        super()._reset_session()
        self.originator = None
        self.parent_id = None
        self.window = None
        self.ctx_used = None
        self.policy = {'approval': None, 'sandbox': None, 'reviewer': None}
        self._calls = {}          # call_id → 这次调用的信息
        self._cells = {}          # 脚本的 cell id → call_id（「Script running with cell ID」之后用 wait 续等）
        self._turn_trigger = None
        self._turn_ts = None
        self._turn_pending = False   # task_started 之后还没等到输入
        self._turn_input = False
        self._pre_inject = []       # task_started 之后、输入之前写下的系统注入（开回合会清节点，开完再补）
        self._compact_turn = False
        self._asking = False
        self._reviewing = None    # 正在审核的自动审核会话 id
        self._guess_wait = False
        self._resp = set()
        self._bg_done = 0

    # ---------- 对外接口 ----------

    def _main_record(self, o, ts):
        t = o.get('type')
        p = o.get('payload') if isinstance(o.get('payload'), dict) else {}
        if t == 'session_meta':
            self._meta(p)
        elif t == 'turn_context':
            self._turn_context(p)
        elif t == 'event_msg':
            self._event(p, ts)
        elif t == 'response_item':
            self._response(p, ts)
        elif t == 'token_usage_record':
            self._usage_record(p, ts)
        elif t == 'compacted':
            self._compacted(ts)
        elif t == 'world_state':
            self._world(p)
        elif t not in ('inter_agent_communication_metadata', 'retained_context'):
            self._new_type(str(t) if t else '(无类型)')

    def feed_proc(self, reg):
        """Codex 没有进程状态文件：只区分进程在不在（reg 为 None 时等于读不到）。"""
        return

    def proc_exited(self):
        self.proc_status = 'exited'
        self._set_wait(None, None)
        self._add_log('已关闭', 'Codex 已关闭这个会话', time.time())

    def command_running(self, ts):
        """服务端看到 Codex 进程下出现了命令子进程：申请出沙箱的那一步已经批准、在跑了（等批准时没有子进程，19 号件 §三）。"""
        if self._guess_wait:
            self._end_guess_wait(ts, approved=True)

    def guess_waiting(self):
        return bool(self._guess_wait)

    def guess_command(self):
        """推测在等批准的那条命令的原文（服务端拿它去对 Codex 的子进程）。"""
        info = self._calls.get(self._guess_wait) if self._guess_wait else None
        return info.get('cmd') if info else None

    def _tool_label(self):
        return '执行中'

    # ---------- 会话信息 ----------

    def _meta(self, p):
        self.originator = _s(p.get('originator')) or self.originator
        self.version = _s(p.get('cli_version')) or self.version
        self.parent_id = _s(p.get('parent_thread_id')) or self.parent_id
        self.cwd = _s(p.get('cwd')) or getattr(self, 'cwd', None)
        bi = p.get('base_instructions')
        if isinstance(bi, dict) and isinstance(bi.get('text'), str):
            self._cx['prompt_size'] = len(bi['text'])

    def _turn_context(self, p):
        if isinstance(p.get('model'), str):
            self._md['model'] = p['model']
        eff = p.get('effort')
        if isinstance(eff, str):
            self._md['effort'] = eff
        sp = p.get('sandbox_policy')
        self.policy = {'approval': _s(p.get('approval_policy')), 'sandbox': _s(sp.get('type')) if isinstance(sp, dict) else None,
                       'reviewer': _s(p.get('approvals_reviewer'))}
        self.mode = self.policy['approval'] or self.mode

    def _world(self, p):
        st = p.get('state') if isinstance(p.get('state'), dict) else {}
        am = st.get('agents_md')
        if isinstance(am, dict) and isinstance(am.get('text'), str):
            self._mem['files']['AGENTS.md'] = {'type': 'AgentsMd', 'size': len(am['text'])}
        sk = st.get('host_skills')
        if isinstance(sk, dict) and isinstance(sk.get('entries'), dict):
            cloud = st.get('cloud_skills') if isinstance(st.get('cloud_skills'), dict) else {}
            self._cx['skills'] = len(sk['entries']) + (len(cloud['entries']) if isinstance(cloud.get('entries'), dict) else 0)
        env = st.get('environments')
        if isinstance(env, dict):
            loc = (env.get('environments') or {}).get('local') if isinstance(env.get('environments'), dict) else None
            if isinstance(loc, dict):
                self._cx['env'] = ' · '.join(x for x in (loc.get('shell') if isinstance(loc.get('shell'), str) else '',) if x) or None

    # ---------- 回合 ----------

    def _event(self, p, ts):
        k = p.get('type')
        if k == 'task_started':
            self._task_started(p, ts)
        elif k == 'task_complete':
            self._task_complete(p, ts)
        elif k == 'turn_aborted':
            self._end_guess_wait(ts)
            self._end_ask(ts)
            self._interrupt(bool(self._inflight), ts)
        elif k == 'token_count':
            info = p.get('info') if isinstance(p.get('info'), dict) else {}
            last = info.get('last_token_usage') if isinstance(info.get('last_token_usage'), dict) else {}
            if _int(last.get('total_tokens')):
                self.ctx_used = last['total_tokens']
                self.c['context_tokens'] = self.ctx_used
            if _int(info.get('model_context_window')):
                self.window = info['model_context_window']
        elif k == 'item_completed':
            self._item(p.get('item') if isinstance(p.get('item'), dict) else {}, p, ts)
        elif k not in HIDDEN_EVENTS:
            self._new_type('event/' + str(k))

    def _task_started(self, p, ts):
        if _int(p.get('model_context_window')):
            self.window = p['model_context_window']
        att = p.get('turn_attribution') if isinstance(p.get('turn_attribution'), dict) else {}
        trig = att.get('turn_trigger')
        self._turn_trigger, self._turn_ts, self._turn_input, self._compact_turn = trig, ts, False, False
        self._turn_pending = True
        self._pre_inject = []
        if trig in TRIGGER_PROMPT or trig == 'queue' or trig is None:
            # 等随后的 UserMessage 再开回合（内容和来源要看它）；没有来源的旧版本和手动压缩也先等
            return
        if trig == 'guardian_review':
            return
        source = TRIGGER_SOURCE.get(trig, str(trig))
        self._open_turn(source, source, source, ts)
        self._turn_input, self._turn_pending = True, False
        self._add_log('收到消息', source, ts)
        self._flush_inject()
        self._model_start(ts)

    def _end_ask(self, ts):
        """回合结束或中断时，没人回答的提问也不再算等你。"""
        if self._asking:
            self._asking = False
            self._wait_ended()
            self._set_wait(None, ts)

    def _task_complete(self, p, ts):
        self._end_guess_wait(ts)
        self._end_ask(ts)
        if self._turn_pending and not self._turn_input and not self._compact_turn and self._turn_ts is not None:
            # 只有 task_started、没有输入就结束了（读到一半的记录、没见过的来源）：补开回合，用时才有起点
            self._open_turn('未知', '未知', '未知', self._turn_ts)
        self._turn_pending = False
        self._flush_inject() if self._turn_active() else None
        self._pre_inject = []
        ms = p.get('duration_ms')
        self._ss['turn_ms'] = ms if isinstance(ms, (int, float)) else None
        self._api_pending = False
        self._sync_loop()
        if self._turn_active():
            self._turn_finished(self._ss['turn_ms'], ts)
        err = p.get('error')
        if isinstance(err, dict) or (isinstance(err, str) and err):
            info = err.get('codex_error_info') if isinstance(err, dict) else None
            self._set('model', 'error', None, ts)
            self._add_log('模型调用', '出错' + ('（%s）' % info if isinstance(info, str) else ''), ts)
        self._turn_trigger = None

    def _user_message(self, item, ts):
        text = _text_items(item.get('content'))
        s = text.lstrip()
        if s.startswith('<send_user_message_question_reply>'):
            if self._asking:
                self._asking = False
                self._wait_ended()
                self._set_wait(None, ts)
            self._add_log('你的回答', '', ts)
            if self._turn_active():
                self._model_start(ts)
            return
        if (self._turn_pending and not self._turn_input) or not self._turn_active():
            start = self._turn_ts if self._turn_pending and self._turn_ts is not None else ts
            self._turn_input, self._turn_pending = True, False
            if s.startswith('<heartbeat>'):
                self._open_turn('自动化', '定时自动化', '自动化', start)
                self._add_log('收到消息', '自动化', ts)
            elif s.startswith('<') and not s.startswith('<image'):
                tag = re.match(r'<([\w-]+)', s)
                self._open_turn('系统消息', tag.group(1) if tag else '系统消息', '系统消息', start)
                self._add_log('收到消息', tag.group(1) if tag else '', ts)
            else:
                self._prompt(text, start)
                if self._turn_trigger == 'queue':
                    self._in['source'] = '你排队的输入'
            self._flush_inject()
            self._model_start(ts)
            return
        if s.startswith('<'):
            tag = re.match(r'<([\w-]+)', s)
            self._add_log('系统消息', tag.group(1) if tag else '', ts)
            return
        # 回合中途你又发了一句（插话）
        self._in['interjected'] += 1
        self._add_log('插话', _summary(text), ts)
        self._model_start(ts)

    def _model_start(self, ts):
        """模型调用开始：记录里没有，按「该轮到模型了」推（D-121）。"""
        if not self._turn_active() or self._api_pending or self._inflight or self._asking:
            return
        self._api_pending = True
        self._api_ts = ts
        self._sync_loop()
        self._set('model', 'active', None, ts)
        self._add_log('模型调用', '第 %d 次' % self.c['loop'], ts)

    def _usage_record(self, p, ts):
        rid = p.get('response_id')
        if not isinstance(rid, str) or rid in self._resp:
            return
        self._resp.add(rid)
        self.c['model_calls'] += 1
        if not self._turn_active():
            return
        self._turn_reqs.add(rid)
        self._api_pending = False
        self._sync_loop()
        u = p.get('usage') if isinstance(p.get('usage'), dict) else {}
        inp, cached = _int(u.get('input_tokens')), _int(u.get('cached_input_tokens'))
        self._md['usage'] = {'input_tokens': max(0, inp - cached), 'cache_read_input_tokens': cached,
                             'cache_creation_input_tokens': _int(u.get('cache_write_input_tokens')), 'output_tokens': _int(u.get('output_tokens'))}

    def _compacted(self, ts):
        manual = self._turn_pending and not self._turn_input
        pre = self.ctx_used
        if manual:
            # 手动 /compact：Codex 记成一轮，但没有输入；和 Claude Code 一样不算回合，只亮压缩（20 号件 §3.5）
            self._compact_turn = True
            self._set('compact', 'active', None, self._turn_ts or ts)
        trigger = 'manual' if manual else 'auto'
        self._ss['compacts'][trigger] += 1
        self._ss['last_compact_raw'] = {'pre': pre, 'post': None, 'ms': None, 'trigger': trigger, 'ts': ts}
        self._set('compact', 'done', None, ts)
        self._add_log('压缩', '手动' if manual else '自动', ts)

    # ---------- 工具 ----------

    def _response(self, p, ts):
        k = p.get('type')
        if k in ('custom_tool_call', 'function_call'):
            self._call(p, ts)
        elif k in ('custom_tool_call_output', 'function_call_output'):
            self._call_output(p, ts)
        elif k == 'message':
            self._message(p, ts)
        elif k == 'reasoning':
            pass  # item_completed 的 Reasoning 已计数
        elif k not in ('additional_tools', 'agent_message', 'web_search_call', 'compaction', 'configuration_update'):
            self._new_type('response/' + str(k))

    def _call(self, p, ts):
        cid, name = p.get('call_id'), str(p.get('name') or '')
        if not cid:
            return
        args = p.get('input') if p.get('type') == 'custom_tool_call' else p.get('arguments')
        if name == 'wait':
            # 续等一段还在跑的脚本：不是新工具
            try:
                cell = str(json.loads(args).get('cell_id'))
            except (ValueError, AttributeError, TypeError):
                cell = None
            self._calls[cid] = {'kind': 'wait', 'cell': cell}
            return
        if name in ('request_user_input_async', 'request_user_input'):
            self._calls[cid] = {'kind': 'ask', 'sync': name == 'request_user_input'}
            if self.nodes['model']['state'] == 'active':
                self._set('model', 'done', None, ts)
            # 提问是这次模型回复的一部分：回复已经结束（用量记录随后到，照样计数）
            self._api_pending = False
            self._sync_loop()
            self._asking = True
            self._set('tool', 'waiting', '等你回答', ts)
            self._set_wait('input', ts)
            self._add_log('等你回答', '', ts)
            return
        if name == 'exec' and p.get('type') == 'custom_tool_call':
            names, cmd, escalated = parse_script(args)
            raw = names[0] if names else 'exec'
            target = cmd or ('等待命令输出' if raw == 'write_stdin' else ', '.join(dict.fromkeys(names))[:120])
        else:
            raw, target, escalated = name, '', False
            try:
                a = json.loads(args) if isinstance(args, str) else (args if isinstance(args, dict) else {})
                target = next((v for v in a.values() if isinstance(v, str) and v), '') if isinstance(a, dict) else ''
            except ValueError:
                pass
        friendly = 'MCP' if raw.startswith('mcp__') else FRIENDLY.get(raw, raw)
        cat = 'MCP' if raw.startswith('mcp__') else CATEGORY.get(raw, '其他')
        if raw.startswith('mcp__'):
            parts = raw.split('__')
            target = (' · '.join(parts[1:3]) if len(parts) >= 3 else raw) + (' ' + target if target else '')
        self._calls[cid] = {'kind': 'tool', 'raw': raw, 'inner': [], 'escalated': escalated, 'ts': ts,
                            'cmd': cmd if name == 'exec' and p.get('type') == 'custom_tool_call' else None,
                            'script': name == 'exec' and p.get('type') == 'custom_tool_call'}
        block = {'id': cid, 'name': friendly, 'input': {'command': target} if target else {}}
        self._tool_started(block, ts)
        r = self._tools.get(cid)
        if r is not None:
            r['cat'] = cat
            r['raw'] = raw
        self._tl['cats']['其他'] -= 1
        self._tl['cats'][cat] += 1
        self._lv['cats_turn']['其他'] -= 1
        self._lv['cats_turn'][cat] += 1
        if cat == 'MCP':
            self._lv['mcp_calls'] += 1
            self._lv['mcp_turn'] += 1
        if raw == 'update_plan':
            self.stats['plan_updates'] += 1
        if escalated and self.policy['approval'] != 'never' and self.policy['sandbox'] != 'danger-full-access':
            if self.policy['reviewer'] == 'auto_review':
                self._calls[cid]['review'] = True
            else:
                # 申请出沙箱：记录里没有批准，只能推测在等你批准（D-121）
                self._guess_wait = cid
                self._set('permission', 'waiting', '可能在等你批准', ts)
                self._set_wait('permission', ts)
                self._add_log('等你批准', '可能 · ' + _summary(target), ts)

    def _end_guess_wait(self, ts, approved=None):
        cid = self._guess_wait
        if not cid:
            return
        self._guess_wait = False
        self._set_wait(None, ts)
        if approved:
            self._perm_result('user', '你批准了', ts)
            if cid in self._calls:
                self._calls[cid]['perm_done'] = '你批准了'
        elif self.nodes['permission']['state'] == 'waiting':
            self._set('permission', 'idle', None, ts)

    def _perm_result(self, key, text, ts):
        self._set('permission', 'rejected' if key.endswith('reject') else 'passed', text, ts)
        self._tl['perm'] = text
        self._lv['perm_counts'][key] += 1
        self._lv['perm_turn'][key] += 1
        self._lv['perm_last'] = text

    def _call_output(self, p, ts):
        cid = p.get('call_id')
        info = self._calls.get(cid)
        if info is None:
            return
        out = _out_text(p.get('output'))
        if info['kind'] == 'wait':
            self._calls.pop(cid, None)
            owner = self._cells.get(info.get('cell'))
            if owner and not out.startswith('Script running'):
                self._cells.pop(info.get('cell'), None)
                self._finish(owner, out, ts)
            if self._turn_active():
                self._model_start(ts)
            return
        if info['kind'] == 'ask':
            self._calls.pop(cid, None)
            if info.get('sync'):
                self._asking = False
                self._wait_ended()
                self._set_wait(None, ts)
            if self._turn_active() and not self._asking:
                self._model_start(ts)
            return
        m = re.match(r'Script running with cell ID (\S+)', out)
        if m:
            # 脚本还在跑，之后用 wait 续等
            self._cells[m.group(1)] = cid
            self._model_start_after_yield(ts)
            return
        self._finish(cid, out, ts)
        if self._turn_active() and not out.startswith('aborted by user'):
            # 被拒或被你取消的结果后面紧跟中断，不再推模型调用
            self._model_start(ts)

    def _model_start_after_yield(self, ts):
        """脚本先返回、命令还在跑：模型会马上再调一次（wait）。工具仍算在途，模型这次调用不单独显示。"""
        return

    def _finish(self, cid, out, ts):
        info = self._calls.pop(cid, None)
        if info is None or cid not in self._inflight:
            return
        if self._guess_wait == cid:
            self._end_guess_wait(ts, approved=bool(info['inner']))
        if out.startswith('aborted by user'):
            if info['escalated'] and not info['inner']:
                # 批准窗口里选「否」和按 Esc 是同一个键（19 号件 §三）：申请过出沙箱、什么都没执行，算你拒绝了
                self._tool_finished(cid, False, {'decision': 'reject', 'source': 'user_reject'}, ts)
            else:
                self._inflight.discard(cid)
                self.c['inflight'] = len(self._inflight)
                self._tool_stopped(cid, ts)
            return
        failed = out.startswith('Script failed') or 'failed' in info['inner']
        declined = 'declined' in info['inner']
        review = info.get('review_result')
        if review == 'deny' or declined:
            self._tool_finished(cid, False, {'decision': 'reject', 'source': 'user_reject'}, ts)
            if review == 'deny':
                # _tool_finished 按「你拒绝了」记的，改成自动审核的说法
                self._relabel('user_reject', 'review_reject', '自动审核拒绝', cid)
            return
        self._tool_finished(cid, failed, None, ts)
        r = self._tools.get(cid)
        if info.get('perm_done'):
            # 批准在推测结束时已经计过
            if r is not None:
                r['perm'] = info['perm_done']
            return
        if review == 'allow':
            # 审核结束时已经计过一次
            if r is not None:
                r['perm'] = '自动审核放行'
            return
        key, text = self._perm_of(info)
        if key:
            self._perm_result(key, text, ts)
            if r is not None:
                r['perm'] = text

    def _relabel(self, old, new, text, cid):
        L = self._lv
        for c in (L['perm_counts'], L['perm_turn']):
            c[old] -= 1
            if c[old] <= 0:
                del c[old]
            c[new] += 1
        L['perm_last'] = self._tl['perm'] = text
        self.nodes['permission']['label'] = text
        r = self._tools.get(cid)
        if r is not None:
            r['perm'] = text

    def _perm_of(self, info):
        if not info.get('script'):
            return None, None  # 权限文字只给脚本里的命令；sleep、spawn_agent 这类不经过放行
        if info['escalated'] and info['inner']:
            # 申请出沙箱、命令跑了：批准交给你的是你批准了；交给自动审核的，没看到审核记录（Codex 有时直接放行，实测）也记自动审核放行
            return ('review', '自动审核放行') if self.policy['reviewer'] == 'auto_review' else ('user', '你批准了')
        if self.policy['approval'] == 'never' or self.policy['sandbox'] == 'danger-full-access':
            return 'mode', '模式放行'
        if self.policy['sandbox'] in ('read-only', 'workspace-write'):
            return 'sandbox', '沙箱内运行'
        return None, None

    def _current_cell(self):
        for cid in reversed(list(self._inflight)):
            if cid in self._calls and self._calls[cid].get('kind') == 'tool':
                return cid
        return None

    def _item(self, item, p, ts):
        t = item.get('type')
        if t == 'UserMessage':
            self._user_message(item, ts)
        elif t == 'AgentMessage':
            self._add_log('输出文字', '最终回答' if item.get('phase') == 'final_answer' else '', ts)
        elif t == 'Reasoning':
            self._md['think_blocks'] += 1
            self._add_log('思考', '', ts)
        elif t in INNER_ITEMS:
            self._inner(t, item, ts)
        elif t == 'Extension':
            if item.get('kind') == 'web.search':
                self._tl['cats']['网络'] += 1
        elif t == 'SubAgentActivity':
            self._subagent(item, ts)
        elif t == 'ContextCompaction':
            pass  # 和 compacted 一起处理
        elif t == 'Plan':
            self.stats['plan_updates'] += 1
        elif t not in HIDDEN_ITEMS:
            self._new_type('item/' + str(t))

    def _inner(self, t, item, ts):
        status = item.get('status')
        cid = self._current_cell()
        if t == 'CommandExecution':
            for pc in item.get('parsed_cmd') or ():
                path = pc.get('path') if isinstance(pc, dict) else None
                if isinstance(path, str) and (os.path.basename(path) == 'AGENTS.md' or '/.codex/memories/' in path):
                    self._mem['reads'] += 1
                    self._set('memory', 'done', None, ts)
        elif t == 'FileChange':
            ch = item.get('changes')
            paths = list(ch.keys()) if isinstance(ch, dict) else [c.get('path') for c in ch if isinstance(c, dict)] if isinstance(ch, list) else []
            if any(isinstance(x, str) and (os.path.basename(x) == 'AGENTS.md' or '/.codex/memories/' in x) for x in paths):
                self._mem['writes'] += 1
                self._set('memory', 'done', None, ts)
        if cid is None:
            if t == 'CommandExecution' and not self._turn_active():
                # 被中断后还在后台跑的命令，跑完补一条（19 号件 §三）
                self._bg_done += 1
                self._add_log('后台命令结束', '', ts)
            return
        info = self._calls[cid]
        if self._guess_wait == cid:
            self._end_guess_wait(ts, approved=True)
        info['inner'].append(status if isinstance(status, str) else 'completed')
        r = self._tools.get(cid)
        if r is not None and t == 'CommandExecution':
            cmd = item.get('command')
            text = cmd[-1] if isinstance(cmd, list) and cmd and isinstance(cmd[-1], str) else None
            if text and info['raw'] != 'write_stdin':
                r['target'] = _summary(text)

    def _message(self, p, ts):
        md = p.get('internal_chat_message_metadata_passthrough')
        kinds = md.get('content_item_kinds') if isinstance(md, dict) else None
        if not isinstance(kinds, list) or p.get('role') == 'assistant':
            return
        content = p.get('content') if isinstance(p.get('content'), list) else []
        items = []
        for i, k in enumerate(kinds):
            if not isinstance(k, str) or k.startswith('user.'):
                continue
            body = content[i] if i < len(content) and isinstance(content[i], dict) else {}
            items.append((k, len(body['text']) if isinstance(body.get('text'), str) else 0))
        if not items:
            return
        if self._turn_pending and not self._turn_input:
            # Codex 先写系统注入、后写你的输入：开回合时节点会被清，先记下，开完再补
            self._pre_inject.append((items, ts))
            return
        self._inject(items, ts)

    def _inject(self, items, ts):
        for k, size in items:
            item = self.context.setdefault(k, {'kind': 'context', 'count': 0, 'size': 0, 'ts': None})
            item['count'] += 1
            item['size'] = size
            item['ts'] = ts
            self._cx['round_ctx'] += 1
            if k == 'memories.instructions':
                self._mem['files']['Codex 记忆'] = {'type': 'CodexMemory', 'size': size}
                self._set('memory', 'done', None, ts)
            elif k == 'agents_md.instructions':
                self._set('memory', 'done', None, ts)
            elif k == 'skills.selected_skill_instructions':
                self._lv['skills_turn'] += 1
                self._lv['skills_total'] += 1
        self._set('context', 'done', None, ts)
        self._add_log('上下文注入', '、'.join(k for k, _ in items)[:120], ts)

    def _flush_inject(self):
        pend, self._pre_inject = self._pre_inject, []
        for items, ts in pend:
            self._inject(items, ts)

    # ---------- 子 agent 和自动审核 ----------

    def _subagent(self, item, ts):
        aid = _s(item.get('agent_thread_id'))
        kind = item.get('kind')
        if not aid:
            return
        if kind == 'started':
            a = self._ensure_agent(aid, {'agentType': _s(item.get('agent_path')), 'description': _s(item.get('model'))}, ts)
            a['state'] = 'running'
            self._refresh_agent_node(ts)
        elif kind == 'completed':
            self._ensure_agent(aid, None, ts)
            self._agent_finished(aid, 'completed', ts)
        elif kind == 'interrupted':
            self._ensure_agent(aid, None, ts)
            self._agent_finished(aid, 'stopped', ts)

    def feed_child(self, child_id, rows, kind):
        """子会话的记录：kind 为 'guardian'（自动审核）或 'agent'（子 agent）。"""
        for o in rows:
            if o is LineReader.BAD_LINE or not isinstance(o, dict):
                continue
            try:
                if kind == 'guardian':
                    self._guardian(child_id, o, _ts(o))
                else:
                    self._agent_step(child_id, o, _ts(o))
            except Exception:
                self.parse_errors += 1

    def _guardian(self, gid, o, ts):
        p = o.get('payload') if isinstance(o.get('payload'), dict) else {}
        if o.get('type') == 'session_meta' or (o.get('type') == 'event_msg' and p.get('type') == 'task_started'):
            if self._reviewing != gid:
                self._reviewing = gid
                self._set('permission', 'active', '自动审核中', ts)
                self._set_wait('review', ts)
                self._add_log('自动审核', '开始', ts)
        elif o.get('type') == 'event_msg' and p.get('type') == 'task_complete':
            try:
                verdict = json.loads(p.get('last_agent_message') or '{}').get('outcome')
            except (ValueError, AttributeError):
                verdict = None
            # 同一个自动审核会话会被后面的出沙箱命令接着用（实测：一份记录里多次开始、结束），每次结束都单独记
            if self._reviewing != gid:
                return
            self._reviewing = None
            self._set_wait(None, ts)
            cid = self._current_cell()
            if cid:
                self._calls[cid]['review_result'] = 'deny' if verdict == 'deny' else 'allow'
            if verdict == 'deny':
                # 计数在工具结束时（_finish）记
                self._set('permission', 'rejected', '自动审核拒绝', ts)
            else:
                self._perm_result('review', '自动审核放行', ts)
            self._add_log('自动审核', '拒绝' if verdict == 'deny' else '放行', ts)

    def _agent_step(self, aid, o, ts):
        a = self.agents.get(aid)
        if a is None:
            return
        p = o.get('payload') if isinstance(o.get('payload'), dict) else {}
        if o.get('type') == 'response_item' and p.get('type') in ('custom_tool_call', 'function_call'):
            if p.get('name') == 'exec':
                names, cmd, _ = parse_script(p.get('input'))
                raw = names[0] if names else 'exec'
                a['step'] = _summary(((FRIENDLY.get(raw, raw)) + ' ' + (cmd or '')).strip())
            else:
                a['step'] = _summary(str(p.get('name') or ''))
        elif o.get('type') == 'event_msg' and p.get('type') == 'item_completed' and isinstance(p.get('item'), dict):
            if p['item'].get('type') == 'AgentMessage':
                a['step'] = '输出文字'

    # ---------- 快照 ----------

    def _live(self):
        v = super()._live()
        v['product'] = 'codex'
        v['host'] = host_of(self.originator)
        v['model']['name'] = model_name(self._md['model'])
        v['model']['thinking'] = None
        v['context'] = {'used': self.ctx_used, 'window': self.window, 'left_pct': context_left(self.ctx_used, self.window)}
        v['policy'] = dict(self.policy)
        if self._lv['wait_kind'] == 'permission' and self._guess_wait:
            v['wait']['guess'] = True
        v['hooks'] = None
        v['tasks'] = []
        v['checkpoints'] = None
        v['background_done'] = self._bg_done
        return v

    def _drawer(self):
        d = super()._drawer()
        files = self._mem['files']

        def one(key):
            f = files.get(key)
            return {'size': f['size'], 'count': 1} if f else None
        d['memory']['agents_md'] = one('AGENTS.md')
        d['memory']['codex_memory'] = one('Codex 记忆')
        d['perm']['sandbox'] = self.policy['sandbox']
        d['perm']['reviewer'] = self.policy['reviewer']
        d['agents']['background_done'] = self._bg_done
        return d

    def _phase(self):
        # 自动审核的时间算工具段，不算等你（D-125）
        if self._lv['wait_kind'] == 'review':
            if self._lv['turn_start'] is None or self.nodes['turn']['state'] in ('done', 'interrupted'):
                return None
            return 'tools'
        return super()._phase()


# ---------- 找记录、找进程 ----------

def codex_home():
    return os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')


def find_rollout(home, sid):
    """会话 id → 它的 rollout 记录（只找没压缩的 .jsonl，正在写的都是这种）。"""
    if not sid:
        return None
    hits = glob.glob(os.path.join(glob.escape(home), 'sessions', '*', '*', '*', 'rollout-*-' + glob.escape(sid) + '.jsonl'))
    return max(hits, key=os.path.getmtime) if hits else None


def first_record(path):
    try:
        with open(path, 'rb') as f:
            line = f.readline()
        o = json.loads(line)
        return o.get('payload') if isinstance(o, dict) and isinstance(o.get('payload'), dict) else {}
    except (OSError, ValueError):
        return {}


def _lsof(args):
    exe = shutil.which('lsof') or '/usr/sbin/lsof'
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout


def lock_holders(home, sid):
    """占着这个会话锁文件的进程（Codex 退出后锁文件不删，只是没人占，19 号件 §四）。读不到返回 None。"""
    path = os.path.join(home, 'thread-writer-locks', sid + '.lock')
    if not os.path.exists(path):
        return set()
    out = _lsof(['-t', path])
    if out is None:
        return None
    return {int(x) for x in out.split() if x.isdigit()}


def open_rollouts(pid):
    """一个 Codex 进程开着的 rollout 记录（终端里 /new 换会话时用）。"""
    out = _lsof(['-p', str(pid), '-Fn'])
    if not out:
        return []
    return [ln[1:] for ln in out.splitlines() if ln.startswith('n') and '/sessions/' in ln and ln.endswith('.jsonl') and '/rollout-' in ln]


def running_sessions(home):
    """正在运行的 Codex 会话：锁文件有进程占着的主会话。"""
    lock_dir = os.path.join(home, 'thread-writer-locks')
    locks = glob.glob(os.path.join(glob.escape(lock_dir), '*.lock'))
    locks = [p for p in locks if not os.path.basename(p).startswith('.')]
    if not locks:
        return []
    out = _lsof(['-F', 'pn'] + locks)
    if not out:
        return []
    held, pid = {}, None
    for ln in out.splitlines():
        if ln.startswith('p'):
            pid = int(ln[1:]) if ln[1:].isdigit() else None
        elif ln.startswith('n') and pid is not None:
            held[os.path.basename(ln[1:])[:-5]] = pid
    res = []
    for sid, pid in held.items():
        path = find_rollout(home, sid)
        if not path:
            continue
        m = first_record(path)
        if isinstance(m.get('source'), dict):
            continue
        res.append({'sessionId': sid, 'pid': pid, 'cwd': m.get('cwd'), 'name': None, 'kind': 'codex', 'status': '',
                    'startedAt': os.path.getmtime(path), 'product': 'codex'})
    return res


def codex_doctor(home):
    """把解析跑遍本机全部 Codex 记录，只返回统计（20 号件 §八）。压缩的 .zst 用本机的 zstd 解压，没装就跳过。"""
    r = collections.Counter(files=0, records=0, bad_lines=0, crashes=0, prompts=0, non_human_prompts=0,
                            inflight_underflow=0, stuck_after_turn=0, split_negative=0, skipped_zst=0)
    new_types = set()
    zstd = shutil.which('zstd')
    paths = glob.glob(os.path.join(glob.escape(home), 'sessions', '**', '*.jsonl*'), recursive=True)
    paths += glob.glob(os.path.join(glob.escape(home), 'archived_sessions', '*.jsonl*'))
    for path in paths:
        if path.endswith('.zst'):
            if not zstd:
                r['skipped_zst'] += 1
                continue
            try:
                data = subprocess.run([zstd, '-dc', path], capture_output=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                r['skipped_zst'] += 1
                continue
        elif path.endswith('.jsonl'):
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except OSError:
                continue
        else:
            continue
        rows = []
        for ln in data.split(b'\n'):
            if not ln.strip():
                continue
            try:
                o = json.loads(ln)
                rows.append(o if isinstance(o, dict) else LineReader.BAD_LINE)
            except ValueError:
                rows.append(LineReader.BAD_LINE)
        meta = rows[0].get('payload') if rows and isinstance(rows[0], dict) and isinstance(rows[0].get('payload'), dict) else {}
        if isinstance(meta.get('source'), dict):
            continue  # 子 agent 和自动审核的记录挂在主会话下读
        r['files'] += 1
        b = CodexBoard()
        for o in rows:
            if o is LineReader.BAD_LINE:
                r['bad_lines'] += 1
                continue
            r['records'] += 1
            b.feed_record(o)
            b.drain()
        try:
            json.dumps(b.snapshot())
        except Exception:
            r['crashes'] += 1
        r['crashes'] += b.parse_errors + b.stats['snapshot_errors']
        r['split_negative'] += any(v < 0 for v in b._lv['split'].values())
        for k in ('prompts', 'non_human_prompts', 'inflight_underflow', 'stuck_after_turn'):
            r[k] += b.stats[k]
        new_types |= b.new_types
    out = dict(r)
    out['new_types'] = sorted(new_types)
    return out
