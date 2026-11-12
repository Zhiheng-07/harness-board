#!/usr/bin/env python3
"""Claude Code 记录解析和状态机。

把会话记录和进程状态翻译成流程图上每个节点的状态。只读取，不写任何文件。
记录格式是 Claude Code 的内部格式，每次升级都可能变，所以这里对未知内容一律宽容：
看不懂就计数，不崩溃。只用 Python 标准库，语法兼容 Python 3.9。
"""
import collections
import glob
import json
import os
import re
import time
from datetime import datetime

NODES = ('prompt', 'context', 'memory', 'hooks', 'model', 'tool', 'permission', 'agent', 'compact', 'turn')

BLOCKS = (
    ('input', 'INPUT', '输入', 'main'),
    ('context', 'CONTEXT', '上下文拼装', 'main'),
    ('memory', 'MEMORY', '记忆', 'support'),
    ('hooks', 'HOOKS', '钩子', 'support'),
    ('model', 'MODEL', '模型调用', 'main'),
    ('tools', 'TOOLS', '工具与权限', 'exec'),
    ('agents', 'AGENTS', '子 agent 与后台', 'exec'),
    ('session', 'SESSION', '回合与压缩', 'main'),
)
MEMORY_NAMES = frozenset(('CLAUDE.md', 'CLAUDE.local.md', 'MEMORY.md', 'AGENTS.md'))
HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop')
HOOK_ERRORS = frozenset(('hook_blocking_error', 'hook_non_blocking_error', 'hook_error_during_execution', 'hook_cancelled'))
TOOL_CATEGORIES = {'Bash': 'Shell', 'PowerShell': 'Shell', 'Read': '文件', 'Edit': '文件', 'Write': '文件', 'NotebookEdit': '文件',
                   'Glob': '文件', 'Grep': '文件', 'WebFetch': '网络', 'WebSearch': '网络'}
PROC_TEXT = {'busy': '运行中', 'idle': '空闲', 'shell': '后台命令运行中', 'exited': '已退出'}

ATT_CONTEXT = frozenset((
    'instructions', 'skill_listing', 'environment', 'date', 'session_context', 'model', 'deferred_tools_delta',
    'deferred_tools_record', 'agent_listing_delta', 'mcp_instructions_delta', 'hook_additional_context', 'hook_success',
    'file', 'edited_text_file', 'compact_file_reference', 'invoked_skills', 'command_permissions', 'auto_mode',
    'auto_mode_exit', 'plan_mode', 'plan_mode_exit', 'plan_mode_reentry', 'plan_file_reference', 'credential_org',
    'remote_session_change', 'nested_memory', 'fork_briefing', 'session_cron_carry', 'read_truncation_notice',
    'skill_mention',
))
ATT_REMINDER = frozenset((
    'total_tokens_reminder', 'silent_turn_reminder', 'task_reminder', 'task_status', 'batching_reminder_sent',
    'bash_output_audience_note', 'thinking_drop',
))
ATT_LIST = frozenset(('prompt_snapshot',))

HIDDEN_TYPES = frozenset((
    'api-request-blob', 'api-request-shape', 'last-prompt', 'agent-name', 'mode', 'session-owner', 'atis-latch',
    'queue-operation', 'file-history-snapshot', 'file-history-delta', 'tools-used', 'cost-state', 'worktree-state',
    'relocated', 'continued-in', 'frame-link', 'pr-link', 'bridge-session', 'artifact-autoreact-ledger',
    'artifact-comment-monitor', 'history-suppression',
))
HIDDEN_SYSTEM = frozenset(('local_command', 'away_summary', 'informational'))

PERMISSION_TEXT = {('accept', 'config'): '配置放行', ('accept', 'user_temporary'): '你批准了', ('reject', 'user_reject'): '你拒绝了'}
# 放行原因（reasonType）：规则放行的 Bash 也写 subcommandResults，和只读命令分不开，统称「命令放行」（10 号件 §九）
PERM_REASONS = {'classifier': ('classifier', '分类器放行'), 'mode': ('mode', '模式放行'),
                'subcommandResults': ('command', '命令放行'), 'rule': ('rule', '规则放行')}
TOOL_LIMIT = 24
# 等批准结束后这么多秒内到的「User rejected tool use」仍算你拒绝了（进程文件 0.25 秒读一次，结果约 0.15 秒落盘）
PERM_REJECT_WINDOW = 1.0
# origin.kind → 「输入」的来源；peer 另按是否子 agent 交回细分（_message_turn）
ORIGIN_SOURCES = {'peer': '其他会话', 'coordinator': '协调者', 'auto-continuation': '自动继续'}
COMMAND_OUTPUT_TAGS = frozenset(('local-command-stdout', 'local-command-stderr', 'local-command-caveat', 'bash-stdout', 'bash-stderr'))
TASK_TOOLS = frozenset(('TaskCreate', 'TaskUpdate'))

_REDACT = (
    (re.compile(r'(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?\S+'), r'\1***'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+'), 'Bearer ***'),
    (re.compile(r'(?i)\b([A-Za-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key)[A-Za-z0-9_-]*\s*[=:]\s*)\S+'), r'\1***'),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{6,}'), 'sk-***'),
)
SUMMARY_LIMIT = 120


def _ts(o):
    t = o.get('timestamp')
    if not isinstance(t, str):
        return None
    try:
        return datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(b.get('text') or '' for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _summary(text):
    s = ' '.join(str(text).split())
    for pat, rep in _REDACT:
        s = pat.sub(rep, s)
    return s if len(s) <= SUMMARY_LIMIT else s[:SUMMARY_LIMIT] + '…'


def _dur(ms):
    """时长文字：1 小时以内「12.3 秒」，超过 1 小时「1h02m」（和页面的时间写法一致，D-110、D-111）。"""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    s = ms / 1000
    return '%dh%02dm' % (s // 3600, s % 3600 // 60) if s >= 3600 else '%.1f 秒' % s


def _k(n):
    return str(n) if n < 1000 else '%.1fk' % (n / 1000)


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _str_list(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _s(v):
    """只收非空字符串，别的形状一律当没有（字段形状变了不能让快照出错，15 号件 R-6）。"""
    return v if isinstance(v, str) and v else None


def _is_memory_path(p):
    return isinstance(p, str) and ('/memory/' in p or os.path.basename(p) in MEMORY_NAMES)


def _tool_arg(inp):
    if not isinstance(inp, dict):
        return ''
    for k in ('command', 'file_path', 'path', 'pattern', 'url', 'query', 'description', 'prompt'):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v
    for v in inp.values():
        if isinstance(v, str) and v:
            return v
    return ''


def _prompt_summary(text):
    m = re.search(r'<command-name>\s*(/[^<\s]+)', text)
    if m:
        return m.group(1)
    m = re.search(r'<command-message>\s*([^<\s]+)', text)
    if m:
        return '/' + m.group(1)
    return _summary(re.sub(r'</?pasted_content[^>]*>', '', text))


def _perm(pd):
    """权限判定 → （计数键，显示文字）。"""
    decision, source = pd.get('decision'), pd.get('source')
    rt, code = pd.get('reasonType'), pd.get('reasonCode')
    if decision == 'reject':
        if source == 'user_reject':
            return 'user_reject', '你拒绝了'
        if code == 'safetyCheck' or rt == 'safetyCheck':
            return 'safety_reject', '安全检查拒绝'
        return 'rule_reject', '规则拒绝'
    if source == 'user_temporary':
        return 'user', '你批准了'
    return PERM_REASONS.get(rt, ('config', '配置放行'))


def model_name(mid):
    """模型 id → 给人看的名字，比如 claude-opus-5-5 → Opus 5.5。认不出就原样返回。"""
    if not isinstance(mid, str) or not mid:
        return None
    m = re.match(r'^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$', mid)
    if m:
        return '%s %s%s' % (m.group(1).title(), m.group(2), '.' + m.group(3) if m.group(3) else '')
    m = re.match(r'^claude-(\d+)(?:-(\d))?-(opus|sonnet|haiku)\b', mid)
    if m:
        return '%s %s%s' % (m.group(3).title(), m.group(1), '.' + m.group(2) if m.group(2) else '')
    return mid


def _notif_fields(text):
    out = {}
    for k in ('tool-use-id', 'status'):
        m = re.search(r'<%s>\s*([^<]*?)\s*</%s>' % (k, k), text)
        if m:
            out[k] = m.group(1)
    return out


class Board:
    """一个 Claude Code 进程当前会话的看板状态。"""

    LOG_LIMIT = 500

    def __init__(self, session_id=None):
        self.session_id = session_id
        self._log = collections.deque(maxlen=self.LOG_LIMIT)
        self._pending_log = []
        self._transitions = []
        self._replaying = False
        self._log_on = True
        self._ctx_entry = None
        self.proc_available = None
        self.proc_status = None
        self.waiting_for = None
        self.parse_errors = 0
        self.stats = collections.Counter()
        self.prev_title = None
        self._last_good = {}
        self._snap_fail = set()
        self._reset_session()

    def _reset_session(self):
        self.nodes = {n: {'state': 'idle', 'label': None} for n in NODES}
        self.c = {'rounds': 0, 'loop': 0, 'model_calls': 0, 'tool_calls': 0, 'agents': 0, 'context_tokens': 0, 'inflight': 0}
        self.context = {}
        self.agents = collections.OrderedDict()
        self._agent_meta_finished = {}
        # 子 agent 由通知判为结束的时刻：服务端先读主记录再读子 agent 记录，早于这一刻的子 agent 记录不能把它改回运行中
        self._agent_end_ts = {}
        self._agent_last_ts = {}
        # 子 agent 最后一次模型调用或回复的时刻（回放后纠正用）
        self._agent_act_ts = {}
        self._inflight = set()
        self._batch_err = False
        self._batch_rej = False
        self._interrupted = False
        self._perm_wait_end = None
        self._tool_info = {}
        self._requests = set()
        # 本轮模型调用次数＝本轮收到的不同回复数，加上还在等回复的那一次请求（D-111）：
        # api-request 和回复之间没有编号可配对，重试表现为连续的 api-request，有的调用没有 api-request
        self._turn_reqs = set()
        self._api_pending = False
        self._api_ts = None
        self._orphan_api_ts = None
        self._last_cmd = None
        self._saw_origin = False
        self._saw_api = False
        self._headless = False
        self.custom_title = None
        self.ai_title = None
        self.version = None
        self.mode = None
        self._mode_hint = None
        self._mode_before_plan = None
        self.new_types = set()
        self.bad_lines = 0
        self.degraded = {'prompt': False, 'model': False}
        self.last_record_ts = None
        self._in = {'source': None, 'summary': None, 'interjected': 0, 'notifications': 0}
        # 回合结束后才到的后台通知：先记下，等真有模型调用再开回合（13 号件 §三）
        self._pending_turn = None
        self._cx = {'prompt_size': None, 'tools': None, 'skills': None, 'mcp': set(), 'deferred': set(), 'env': None, 'round_ctx': 0, 'round_rem': 0}
        self._mem = {'files': {}, 'nested': set(), 'compact_refs': 0, 'reads': 0, 'writes': 0}
        self._hk = {'events': collections.Counter(), 'ctx_count': 0, 'ctx_size': 0, 'errors': 0}
        self._md = {'model': None, 'effort': None, 'thinking': None, 'stop': None, 'think_blocks': 0, 'usage': None}
        self._tl = {'current': None, 'batch': 0, 'cats': collections.Counter(), 'ok_config': 0, 'ok_user': 0, 'rejects': 0, 'errors': 0, 'perm': None}
        self._notif = collections.Counter()
        self._last_agent = None
        self._ss = {'turn_ms': None, 'compacts': collections.Counter(), 'last_compact': None, 'last_compact_raw': None}
        # 正式页面要的结构化字段（snapshot 的 live）
        self._tools = collections.OrderedDict()
        self._tool_inputs = {}
        self._tasks = collections.OrderedDict()
        self._lv = {'turn_start': None, 'turn_end': None, 'turn_tools': 0, 'think_ms': 0,
                    'hooks_turn': collections.Counter(), 'hooks_total': 0,
                    'skills_turn': 0, 'skills_total': 0, 'mcp_calls': 0, 'search_turn': 0, 'search_total': 0,
                    'perm_counts': collections.Counter(), 'perm_last': None, 'wait_kind': None, 'wait_since': None,
                    'ckpt': {'snapshots': 0, 'edits': 0, 'last_ts': None},
                    'split': {'model': 0.0, 'tools': 0.0, 'wait': 0.0, 'gap': 0.0}, 'phase': None, 'since': None,
                    'cats_turn': collections.Counter(), 'errors_turn': 0, 'mcp_turn': 0, 'perm_turn': collections.Counter()}

    # ---------- 对外接口 ----------

    def feed_record(self, o, agent=None):
        try:
            if not isinstance(o, dict):
                return
            ts = _ts(o)
            if ts is not None:
                self.last_record_ts = ts
            if isinstance(o.get('version'), str):
                self.version = o['version']
            if o.get('entrypoint') == 'sdk-cli':
                self._headless = True
            if agent is None:
                self._main_record(o, ts)
                if ts is not None:
                    self._tick(ts)
            else:
                self._agent_record(agent, o, ts)
        except Exception:
            self.parse_errors += 1

    def feed_line(self, item, agent=None):
        if item is LineReader.BAD_LINE:
            self.feed_bad_line()
        else:
            self.feed_record(item, agent=agent)

    def feed_bad_line(self):
        self.bad_lines += 1

    def feed_meta(self, agent, meta, ts=None, first_ts=None):
        """ts 是 meta 文件的修改时刻（服务端给），first_ts 是这个子 agent 第一条记录的时刻（回放时给）。"""
        try:
            meta = meta if isinstance(meta, dict) else {}
            self._ensure_agent(agent, meta, first_ts)
            done = meta.get('finished') is True
            stopped = meta.get('stoppedByUser') is True
            if (done or stopped) and not self._agent_meta_finished.get(agent):
                self._agent_meta_finished[agent] = True
                # meta 本身没有时间：结束时刻取 meta 文件的修改时刻和这个子 agent 最后一条记录的时刻里较晚的那个。
                # 最后一句话常和 finished 在同一个轮询周期里写下，只看「已读到的最后一条」会让随后读到的那句把它改回运行中（15 号件 R-1）
                end = self._agent_last_ts.get(agent)
                if isinstance(ts, (int, float)):
                    end = ts if end is None else max(end, ts)
                self._agent_finished(agent, 'stopped' if stopped and not done else 'completed', end)
        except Exception:
            self.parse_errors += 1

    def feed_proc(self, reg):
        now = time.time()
        if reg is None:
            if self.proc_status == 'waiting':
                self._end_wait(self.waiting_for, now)
            self.proc_available = False
            self.proc_status = None
            self.waiting_for = None
            self._set_wait(None, now)
            self._tick(now)
            return
        self.proc_available = True
        if self.version is None and isinstance(reg.get('version'), str):
            # 新会话还没有记录时，版本号先取进程登记文件里的（D-110）
            self.version = reg['version']
        status, wf = reg.get('status'), reg.get('waitingFor')
        was_waiting = self.proc_status == 'waiting'
        old_wf = self.waiting_for
        self.proc_status = status
        self.waiting_for = wf if status == 'waiting' else None
        if status == 'waiting':
            if wf != old_wf or not was_waiting:
                if was_waiting:
                    # 两次读进程文件之间等待类型直接换了（批准完马上弹出提问）：先结束上一种，不让两种等待同时显示（15 号件 R-13）
                    self._end_wait(old_wf, now)
                self._wait_started(wf)
                self._set_wait({'permission prompt': 'permission', 'input needed': 'input'}.get(wf, 'other'), now)
        elif was_waiting:
            self._end_wait(old_wf, now)
            self._set_wait(None, now)
        self._tick(now)

    def proc_exited(self):
        self.proc_status = 'exited'
        self.waiting_for = None
        self._set_wait(None, time.time())
        self._add_log('已退出', 'Claude Code 已退出', time.time())

    def replay(self, records, agents=None, log=True):
        """一次读完已有记录建立状态，不产生变化流。"""
        was = self._replaying, self._log_on
        self._replaying, self._log_on = True, log
        try:
            agents = agents or {}
            for aid, a in agents.items():
                meta = dict(a.get('meta') or {})
                meta.pop('finished', None)
                meta.pop('stoppedByUser', None)
                recs = a.get('records') or ()
                first = next((t for t in (_ts(r) for r in recs if isinstance(r, dict)) if t is not None), None)
                self.feed_meta(aid, meta, first_ts=first)
                for r in recs:
                    self.feed_line(r, agent=aid)
            for r in records:
                self.feed_line(r)
            for aid, a in agents.items():
                self.feed_meta(aid, a.get('meta') or {}, ts=a.get('meta_ts'))
            # 回放把子 agent 的记录排在主记录前面：通知判结束之后又被继续用（SendMessage）的子 agent，
            # 它通知之后的记录早已读过、不会再把它改回运行中。meta 没判结束、通知之后还有模型调用的，改回运行中（15 号件 R-2）
            for aid in agents:
                a = self.agents.get(aid)
                end, act = self._agent_end_ts.get(aid), self._agent_act_ts.get(aid)
                if (a is not None and a['state'] != 'running' and not self._agent_meta_finished.get(aid)
                        and end is not None and act is not None and act > end):
                    self._agent_end_ts.pop(aid, None)
                    a['state'] = 'running'
                    self._refresh_agent_node(act)
        finally:
            self._replaying, self._log_on = was

    def switch_session(self, session_id, records, agents=None):
        self.session_id = session_id
        self.prev_title = self.custom_title or self.ai_title
        self._reset_session()
        # 事件栏只留新会话的事件（D-110）；新会话已有的记录照旧不进日志
        self._log.clear()
        self._pending_log = []
        self.replay(records, agents=agents, log=False)
        self._add_log('会话已切换', session_id[:8] if session_id else '', time.time())

    def rebuild(self, records, agents=None):
        """记录文件被改写变短后：按新内容整份重建状态，事件栏保留（15 号件 R-5）。"""
        self._reset_session()
        self.replay(records, agents=agents, log=False)

    def snapshot(self, include_log=True):
        blocks, live = self._safe('blocks', self._blocks), self._safe('live', self._live)
        s = {
            'nodes': {k: dict(v) for k, v in self.nodes.items()},
            'counters': dict(self.c),
            'context': {k: dict(v) for k, v in self.context.items()},
            'agents': [{k: v for k, v in a.items() if k != 'tool_use_id'} for a in self.agents.values()],
            'blocks': blocks,
            'session': {
                'session_id': self.session_id,
                'title': self.custom_title or self.ai_title,
                'version': self.version,
                'mode': self.mode or self._mode_hint,
                'proc_status': self.proc_status,
                'waiting_for': self.waiting_for,
                'proc_available': self.proc_available,
                'last_record_ts': self.last_record_ts,
                'new_types': sorted(self.new_types),
                'bad_lines': self.bad_lines,
                # 处理出错的记录条数（状态可能不完整），和坏行显示在同一处（15 号件 R-10）
                'parse_errors': self.parse_errors + len(self._snap_fail),
                'degraded': dict(self.degraded),
            },
            'live': live,
        }
        if include_log:
            s['log'] = list(self._log)
        return s

    def _safe(self, name, fn):
        """快照里的一块算出错时，沿用上一次算好的那块，并计入出错条数；看板照常推送，不冻住（15 号件 R-6）。"""
        try:
            v = fn()
            self._last_good[name] = v
            self._snap_fail.discard(name)
            return v
        except Exception:
            self.stats['snapshot_errors'] += 1
            self._snap_fail.add(name)
            if name not in self._last_good:
                self._last_good[name] = getattr(Board(self.session_id), '_' + name)()
            return self._last_good[name]

    def drain(self):
        """取走上次之后产生的节点变化和新日志。"""
        out = {'transitions': self._transitions, 'log': self._pending_log}
        self._transitions = []
        self._pending_log = []
        self._ctx_entry = None
        return out

    # ---------- 正式页面要的结构化字段 ----------

    def _live(self):
        L, md, cx, ss = self._lv, self._md, self._cx, self._ss
        mem_types = collections.Counter(f['type'] for f in self._mem['files'].values())
        sp = {k: round(v, 1) for k, v in L['split'].items()}
        sp.update(phase=L['phase'], since=L['since'])
        return {
            'turn': {'start_ts': L['turn_start'], 'end_ts': L['turn_end'], 'ms': ss['turn_ms'], 'loop': self.c['loop'],
                     'tool_calls': L['turn_tools'], 'think_blocks': md['think_blocks'], 'think_ms': L['think_ms'], 'injected': cx['round_ctx']},
            'wait': {'kind': L['wait_kind'], 'since': L['wait_since'], 'text': self.waiting_for},
            'tools': [dict(r) for r in self._tools.values()],
            'perm': {'last': L['perm_last'], 'counts': dict(L['perm_counts'])},
            'model': {'id': md['model'], 'name': model_name(md['model']), 'effort': md['effort'], 'thinking': md['thinking']},
            'memory': {'files': len(self._mem['files']), 'by_type': dict(mem_types), 'reads': self._mem['reads'], 'writes': self._mem['writes'],
                       'nested': len(self._mem['nested'])},
            'skills': {'available': cx['skills'], 'turn': L['skills_turn'], 'total': L['skills_total']},
            'mcp': {'servers': sorted(cx['mcp']), 'calls': L['mcp_calls']},
            'deferred': {'listed': len(cx['deferred']), 'searches_turn': L['search_turn'], 'searches': L['search_total']},
            'hooks': {'turn': sum(L['hooks_turn'].values()), 'by_event': dict(L['hooks_turn']), 'total': L['hooks_total'], 'errors': self._hk['errors']},
            'compact': {'count': sum(ss['compacts'].values()), 'auto': ss['compacts']['auto'], 'manual': ss['compacts']['manual'], 'last': ss['last_compact_raw']},
            'tasks': [dict(t) for t in self._tasks.values()],
            'checkpoints': dict(L['ckpt']),
            'split': sp,
            'prev_title': self.prev_title,
            'drawer': self._drawer(),
        }

    def _drawer(self):
        """抽屉「这一站」要的原始数字（D-111）：页面自己拼中英文，不再显示原型写死的合成值。"""
        L, cx, m, h, md, tl = self._lv, self._cx, self._mem, self._hk, self._md, self._tl

        def mem(types):
            fs = [f for f in m['files'].values() if f['type'] in types]
            return {'size': sum(f['size'] for f in fs), 'count': len(fs)} if fs else None
        u = md['usage'] or {}
        known = ('User', 'Project', 'Local', 'AutoMem')
        return {
            'input': {'source': self._in['source'], 'summary': self._in['summary'], 'rounds': self.c['rounds'],
                      'interjected': self._in['interjected'], 'notifications': self._in['notifications']},
            'context': {'prompt_size': cx['prompt_size'], 'tools': cx['tools'], 'deferred': len(cx['deferred']), 'skills': cx['skills'],
                        'mcp': sorted(cx['mcp']), 'env': cx.get('env_raw'), 'injected': cx['round_ctx'], 'reminders': cx['round_rem'],
                        'searches_turn': L['search_turn']},
            'memory': {'user': mem(('User',)), 'project': mem(('Project', 'Local')), 'auto': mem(('AutoMem',)),
                       'other': mem(tuple({f['type'] for f in m['files'].values()} - set(known))),
                       'nested': len(m['nested']), 'reads': m['reads'], 'writes': m['writes'], 'compact_refs': m['compact_refs'],
                       'state': self.nodes['memory']['state']},
            'hooks': {'by_event': dict(L['hooks_turn']), 'session': dict(h['events']), 'ctx_count': h['ctx_count'], 'ctx_size': h['ctx_size'],
                      'errors': h['errors']},
            'model': {'id': md['model'], 'effort': md['effort'], 'thinking': md['thinking'], 'stop': md['stop'],
                      'input': _int(u.get('input_tokens')), 'output': _int(u.get('output_tokens')),
                      'cache_write': _int(u.get('cache_creation_input_tokens')), 'cache_read': _int(u.get('cache_read_input_tokens')),
                      # 累计也算上正在等回复的那一次，不会出现「本轮第 1 次 · 共 0 次」（15 号件 R-11）
                      'has_usage': bool(md['usage']), 'calls_turn': self.c['loop'], 'calls_total': self.c['model_calls'] + (1 if self._api_pending else 0),
                      'retries': self.stats['api_retries']},
            'tools': {'cats_turn': dict(L['cats_turn']), 'errors_turn': L['errors_turn'], 'calls_turn': L['turn_tools'],
                      'inflight': self.c['inflight'], 'current': tl['current'] if self._inflight else None},
            'perm': {'mode': self.mode or self._mode_hint, 'turn': dict(L['perm_turn']), 'session': dict(L['perm_counts'])},
            'mcp': {'servers': sorted(cx['mcp']), 'turn': L['mcp_turn'], 'total': L['mcp_calls']},
            'agents': {'shell': self.proc_status == 'shell', 'notif': {k: self._notif[k] for k in ('completed', 'failed', 'stopped')},
                       'last': self._last_agent},
            'checkpoints': L['ckpt']['edits'],
        }

    def _phase(self):
        """本轮此刻在哪一段：模型、工具、等你、间隙；回合没开始或已结束时为 None。"""
        if self._lv['turn_start'] is None or self.nodes['turn']['state'] in ('done', 'interrupted'):
            return None
        if self._lv['wait_kind']:
            return 'wait'
        if self._inflight:
            return 'tools'
        if self.nodes['model']['state'] == 'active':
            return 'model'
        return 'gap'

    def _tick(self, now):
        """阶段切换时，把上一段的时长记进「花在哪」并开新的一段。
        同一段内不累加（否则每次读进程状态都会产生一次推送）；当前段的时长由页面按 since 自己算。"""
        if now is None:
            return
        L = self._lv
        new = self._phase()
        if new == L['phase']:
            return
        if L['phase'] and L['since'] is not None and now > L['since']:
            L['split'][L['phase']] += now - L['since']
        L['phase'] = new
        L['since'] = (max(now, L['since']) if L['since'] is not None else now) if new else None

    def _set_wait(self, kind, now):
        L = self._lv
        if kind != L['wait_kind']:
            L['wait_kind'], L['wait_since'] = kind, (now if kind else None)

    # ---------- 八大块 ----------

    def _blocks(self):
        rows = {'input': self._rows_input(), 'context': self._rows_context(), 'memory': self._rows_memory(), 'hooks': self._rows_hooks(),
                'model': self._rows_model(), 'tools': self._rows_tools(), 'agents': self._rows_agents(), 'session': self._rows_session()}
        return [{'id': b, 'name': n, 'zh': z, 'group': g, 'state': self._block_state(b), 'rows': rows[b]} for b, n, z, g in BLOCKS]

    def _block_state(self, b):
        n = self.nodes
        if b == 'input':
            return n['prompt']['state']
        if b == 'tools':
            return 'waiting' if n['permission']['state'] == 'waiting' else n['tool']['state']
        if b == 'agents':
            return n['agent']['state']
        if b == 'session':
            if n['compact']['state'] == 'active':
                return 'active'
            s = n['turn']['state']
            return 'active' if s == 'idle' and n['prompt']['state'] != 'idle' else s
        return n[b]['state']

    @staticmethod
    def _row(label, value, state=None):
        return {'label': label, 'value': value, 'state': state}

    def _rows_input(self):
        i, r = self._in, self._row
        return [r('来源', i['source'] or '未见'), r('内容', i['summary'] or '未见'), r('轮次', '第 %d 轮' % self.c['rounds']),
                r('插话', '%d 条' % i['interjected']), r('后台通知', '%d 条' % i['notifications'])]

    def _rows_context(self):
        c, r = self._cx, self._row
        tools = '未见' if c['tools'] is None else '%d 个' % c['tools']
        if c['deferred']:
            tools += ' · 延迟加载 %d 个' % len(c['deferred'])
        mcp = sorted(c['mcp'])
        return [r('System Prompt', '%s 字符' % _k(c['prompt_size']) if c['prompt_size'] else '未见'), r('工具定义', tools),
                r('Skills', '%d 个' % c['skills'] if c['skills'] is not None else '未见'),
                r('MCP', '%d 个：%s' % (len(mcp), '、'.join(mcp)) if mcp else '未见'), r('环境', c['env'] or '未见'),
                r('本轮注入', '%d 条 · 提醒 %d 条' % (c['round_ctx'], c['round_rem']))]

    def _mem_value(self, types):
        files = [f for f in self._mem['files'].values() if f['type'] in types]
        if not files:
            return '未见'
        text = '%s 字符' % _k(sum(f['size'] for f in files))
        return text + (' · %d 个' % len(files)) if len(files) > 1 else text

    def _rows_memory(self):
        m, r = self._mem, self._row
        rows = [r('用户 CLAUDE.md', self._mem_value(('User',))), r('项目 CLAUDE.md', self._mem_value(('Project', 'Local'))),
                r('自动记忆 MEMORY.md', self._mem_value(('AutoMem',))), r('子目录记忆', '%d 个' % len(m['nested'])),
                r('读写', '读 %d · 写 %d' % (m['reads'], m['writes'])), r('压缩带回', '%d 个文件' % m['compact_refs'])]
        others = [f for f in m['files'].values() if f['type'] not in ('User', 'Project', 'Local', 'AutoMem')]
        if others:
            rows.insert(3, r('其他记忆', '%s 字符 · %d 个' % (_k(sum(f['size'] for f in others)), len(others))))
        return rows[:8]

    def _rows_hooks(self):
        h, r = self._hk, self._row
        rows = [r(e, '%d 次' % h['events'][e]) for e in HOOK_EVENTS]
        other = sum(v for k, v in h['events'].items() if k not in HOOK_EVENTS)
        rows.append(r('其他事件', '%d 次' % other))
        rows.append(r('注入', '%d 条 · %s 字符' % (h['ctx_count'], _k(h['ctx_size']))))
        rows.append(r('出错', '%d 次' % h['errors'], 'error' if h['errors'] else None))
        return rows

    def _rows_model(self):
        m, r = self._md, self._row
        cfg = ' · '.join(x for x in ('effort ' + m['effort'] if m['effort'] else '', 'thinking ' + m['thinking'] if m['thinking'] else '') if x)
        u = m['usage'] or {}
        inp, cw, cr, out = (_int(u.get(k)) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'))
        total = inp + cw + cr
        tail = ['stop ' + m['stop']] if m['stop'] else []
        tail.append('思考块 %d 个' % m['think_blocks'])
        return [r('模型', m['model'] or '未见'), r('配置', cfg or '未见'),
                r('次数', '本轮第 %d 次 · 共 %d 次' % (self.c['loop'], self.c['model_calls'])),
                r('Token', '输入 %s · 输出 %s' % (_k(inp), _k(out)) if m['usage'] else '未见'),
                r('缓存', '写 %s · 读 %s · 命中 %d%%' % (_k(cw), _k(cr), round(cr * 100 / total)) if total else '未见'), r('输出', ' · '.join(tail))]

    def _rows_tools(self):
        t, r, n = self._tl, self._row, self.nodes
        if n['tool']['state'] == 'waiting':
            cur = r('当前', n['tool']['label'], 'waiting')
        elif self._inflight and t['current']:
            cur = r('当前', t['current'], 'active')
        else:
            cur = r('当前', '无')
        if n['permission']['state'] == 'waiting':
            perm = r('权限', n['permission']['label'], 'waiting')
        else:
            parts = [self.mode + ' 模式'] if self.mode else []
            if t['perm']:
                parts.append(t['perm'])
            perm = r('权限', ' · '.join(parts) or '未见')
        cats = ' · '.join('%s %d' % (k, t['cats'][k]) for k in ('Shell', '文件', '网络', 'MCP', '其他'))
        return [cur, perm, r('本批', '%d 个 · 在途 %d 个' % (t['batch'], self.c['inflight'])), r('调用', cats),
                r('放行', '配置 %d · 你批准 %d · 拒绝 %d' % (t['ok_config'], t['ok_user'], t['rejects'])),
                r('出错', '%d 次' % t['errors'], 'error' if t['errors'] else None)]

    def _rows_agents(self):
        r = self._row
        agents = list(self.agents.values())
        running = [a for a in agents if a['state'] == 'running']
        done = sum(a['state'] == 'done' for a in agents)
        failed = sum(a['state'] == 'failed' for a in agents)
        rows = [r('子 agent', '运行中 %d · 结束 %d · 失败 %d' % (len(running), done, failed))]
        for a in running[:3]:
            text = ' · '.join(x for x in (a['type'], a['description'], a['step']) if x)
            rows.append(r('运行中', _summary(text) or '未知', 'active'))
        if len(running) > 3:
            rows.append(r('还有', '%d 个在跑' % (len(running) - 3)))
        rows.append(r('后台命令', '运行中' if self.proc_status == 'shell' else '无', 'active' if self.proc_status == 'shell' else None))
        rows.append(r('后台任务通知', '完成 %d · 失败 %d · 停止 %d' % (self._notif['completed'], self._notif['failed'], self._notif['stopped'])))
        rows.append(r('最近结束', self._last_agent or '无'))
        return rows

    def _rows_session(self):
        s, r, n, c = self._ss, self._row, self.nodes, self._ss['compacts']
        turn = n['turn']['state']
        if turn == 'interrupted':
            t = r('回合', '中断', 'interrupted')
        elif turn == 'done':
            t = r('回合', '结束 · ' + _dur(s['turn_ms']) if _dur(s['turn_ms']) else '结束')
        elif n['prompt']['state'] != 'idle':
            t = r('回合', '进行中', 'active')
        else:
            t = r('回合', '未开始')
        if self.proc_status == 'waiting':
            proc = r('Claude Code', '等你操作（%s）' % self.waiting_for if self.waiting_for else '等你操作', 'waiting')
        elif not self.proc_status:
            proc = r('Claude Code', '状态不可用')
        else:
            proc = r('Claude Code', PROC_TEXT.get(self.proc_status, str(self.proc_status)))
        used = self.c['context_tokens']
        ctx = _k(used) if used else '未见'
        total = sum(c.values())
        if n['compact']['state'] == 'active':
            last = r('最近压缩', '进行中', 'active')
        else:
            last = r('最近压缩', s['last_compact'] or '无')
        return [t, proc, r('上下文占用', ctx), r('压缩', '%d 次 · 自动 %d · 手动 %d' % (total, c['auto'], c['manual'])), last]

    # ---------- 内部：基础操作 ----------

    def _set(self, node, state, label=None, ts=None, force=False):
        n = self.nodes[node]
        if n['state'] == state and n['label'] == label and not force:
            return False
        n['state'], n['label'] = state, label
        if not self._replaying:
            self._transitions.append({'node': node, 'state': state, 'label': label, 'ts': ts})
        return True

    def _add_log(self, step, summary, ts):
        self._ctx_entry = None
        if not self._log_on:
            return None
        entry = {'ts': ts, 'step': step, 'summary': summary}
        self._log.append(entry)
        if not self._replaying:
            self._pending_log.append(entry)
        return entry

    def _new_type(self, name):
        self.new_types.add(name)

    def _tool_label(self):
        return '执行中' if self.proc_available else '执行中或等待批准'

    # ---------- 内部：主文件记录 ----------

    def _main_record(self, o, ts):
        t = o.get('type')
        if t == 'user':
            self._user(o, ts)
        elif t == 'assistant':
            self._assistant(o, ts)
        elif t == 'attachment':
            self._attachment(o, ts)
        elif t == 'api-request':
            self._api_request(o, ts)
        elif t == 'system':
            self._system(o, ts)
        elif t == 'custom-title':
            self.custom_title = _s(o.get('customTitle')) or self.custom_title
        elif t == 'ai-title':
            self.ai_title = _s(o.get('aiTitle')) or self.ai_title
        elif t == 'permission-mode':
            self.mode = _s(o.get('permissionMode')) or self.mode
        elif t in ('file-history-snapshot', 'file-history-delta'):
            self._checkpoint(o, ts)
        elif t not in HIDDEN_TYPES:
            self._new_type(str(t) if t else '(无类型)')

    def _checkpoint(self, o, ts):
        """文件检查点：快照在每条 prompt 时写一次（只在交互会话里有），增量随改文件的工具结果写（10 号件 §九）。"""
        c = self._lv['ckpt']
        if o.get('type') == 'file-history-snapshot':
            if o.get('isSnapshotUpdate'):
                return
            c['snapshots'] += 1
            snap = o.get('snapshot')
            ts = ts or (_ts(snap) if isinstance(snap, dict) else None)
        else:
            c['edits'] += 1
        if ts is not None:
            c['last_ts'] = ts

    def _user(self, o, ts):
        msg = o.get('message')
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, list):
            results = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result']
            if results:
                pd = o.get('permissionDecision')
                tur = o.get('toolUseResult')
                stopped = isinstance(tur, str) and tur.startswith('User rejected tool use') and not isinstance(pd, dict)
                for b in results:
                    self._tool_finished(b.get('tool_use_id'), bool(b.get('is_error')), pd if isinstance(pd, dict) else None, ts,
                                        tur if isinstance(tur, dict) else None, stopped=stopped)
                return
        origin = o.get('origin')
        okind = origin.get('kind') if isinstance(origin, dict) else origin
        if okind in ORIGIN_SOURCES and not o.get('isCompactSummary'):
            # 别的会话发来的消息带 isMeta，但它确实开了一轮（之后有模型调用）
            self._saw_origin = True
            self._message_turn(okind, origin if isinstance(origin, dict) else {}, ts)
            return
        if o.get('isCompactSummary') or o.get('isMeta'):
            return
        text = _text_of(content)
        if origin is not None:
            self._saw_origin = True
            kind = origin.get('kind') if isinstance(origin, dict) else origin
            if kind == 'human':
                self._prompt(text, ts)
            elif kind == 'task-notification':
                self._notification(text, ts)
            else:
                self._new_type('origin/' + str(kind))
                self._add_log('未知记录', 'origin：' + str(kind), ts)
            return
        s = text.lstrip()
        if not s:
            return
        if s.startswith('[Request interrupted'):
            self._interrupt('for tool use' in s[:60], ts)
        elif re.match(r'/compact(\s|$)', s):
            self._set('compact', 'active', None, ts)
            self._add_log('压缩', '开始压缩（手动）', ts)
        elif s.startswith('<'):
            self._tagged_without_origin(s, ts)
        elif self._saw_origin:
            self._add_log('未知记录', '', ts)
        elif self._headless:
            # 无头会话（claude -p）本来就不写 origin，不算格式退化（10 号件 §九）
            self._prompt(s, ts)
        else:
            self.degraded['prompt'] = True
            self._prompt(s, ts, human=False)

    def _tagged_without_origin(self, s, ts):
        m = re.match(r'<([A-Za-z_-]+)', s)
        tag = m.group(1) if m else ''
        if self._headless and tag == 'command-message':
            # 无头会话（claude -p）本来就不写 origin：带斜杠命令的 prompt 也是你输入的，不算退化（05 号件 v9 §二）
            self._prompt(s, ts)
            return
        if not self._saw_origin:
            if tag in ('pasted_content', 'command-message'):
                self.degraded['prompt'] = True
                self._prompt(s, ts, human=False)
                return
            if tag == 'task-notification':
                self.degraded['prompt'] = True
                self._notification(s, ts)
                return
        if tag in COMMAND_OUTPUT_TAGS:
            # 本地命令和 ! 命令的输出，命令本身已经记过一条「命令」
            return
        cmd = re.search(r'<command-name>\s*(/[^<\s]+)', s)
        if cmd:
            label = cmd.group(1)
        elif tag == 'bash-input':
            m = re.search(r'<bash-input>([\s\S]*?)(?:</bash-input>|$)', s)
            label = '! ' + _summary(m.group(1)) if m and m.group(1).strip() else '!'
        else:
            label = tag
        self._last_cmd = (ts, label)
        self._add_log('命令', label, ts)

    def _reset_flow(self, ts):
        # 开回合的记录有时比本轮第一次模型调用晚几十毫秒写下（prompt、后台通知都见过）：
        # 刚发出、还没回复的那次请求属于新回合，开回合时带过去，不清掉（D-111）
        carry = (self._api_pending and self.nodes['model']['state'] == 'active' and ts is not None
                 and self._api_ts is not None and abs(ts - self._api_ts) < 5)
        for n in NODES:
            if n != 'agent' and n != 'prompt':
                self._set(n, 'idle', None, ts)
        self._inflight.clear()
        self.c['inflight'] = 0
        self.c['loop'] = 0
        self._turn_reqs = set()
        self._api_pending = False
        self._orphan_api_ts = None
        if carry:
            self._api_pending = True
            self.c['loop'] = 1
            self._set('model', 'active', None, ts)
        self._batch_err = self._batch_rej = False
        self._interrupted = False
        self._in['interjected'] = 0
        self._cx['round_ctx'] = self._cx['round_rem'] = 0
        self._md['think_blocks'] = 0
        self._tl['current'] = None
        self._tl['batch'] = 0
        self._ss['turn_ms'] = None
        for r in self._tools.values():
            if r['state'] == 'running':
                r['state'] = 'done'
        L = self._lv
        L.update(turn_start=ts, turn_end=None, turn_tools=0, think_ms=0, hooks_turn=collections.Counter(),
                 skills_turn=0, search_turn=0, split={'model': 0.0, 'tools': 0.0, 'wait': 0.0, 'gap': 0.0}, phase=None, since=None,
                 cats_turn=collections.Counter(), errors_turn=0, mcp_turn=0, perm_turn=collections.Counter())

    def _turn_active(self):
        return self._lv['turn_start'] is not None and self._lv['turn_end'] is None

    def _busy(self):
        """回合在进行，或模型、工具已经在跑（记录偶尔晚于模型调用写下）。"""
        return self._turn_active() or self.nodes['model']['state'] == 'active' or bool(self._inflight)

    def _open_turn(self, source, summary, label, ts):
        """不是你输入的回合（后台通知、别的会话发来的消息）：开回合，轮次不加（轮次只数你的输入）。"""
        self._pending_turn = None
        self._reset_flow(ts)
        self._set('prompt', 'done', label, ts, force=True)
        self._in['source'], self._in['summary'] = source, summary

    def _open_pending_turn(self, ts):
        """模型调用开始时：有回合结束后才到的后台通知，就在这时开回合；回合已在进行就并进去。"""
        p = self._pending_turn
        if p is None:
            return
        if self._busy():
            self._pending_turn = None
            return
        self._open_turn('后台通知', p, '后台通知', ts)

    def _message_turn(self, kind, origin, ts):
        """别的会话发来的消息（子 agent 交回报告、SendMessage）和自动继续：也是一个回合。"""
        if kind == 'peer':
            source = '子 agent 交回' if origin.get('handback') else '其他会话'
            name = origin.get('name') or origin.get('from')
            summary = _summary(name) if isinstance(name, str) and name else source
        else:
            source = ORIGIN_SOURCES[kind]
            summary = source
        self.stats['message_turns'] += 1
        if self._inflight:
            # 还有工具没结束（记录乱序）：不清场，只换来源
            self._in['source'], self._in['summary'] = source, summary
        else:
            self._open_turn(source, summary, source, ts)
        self._add_log('收到消息', source + ('' if summary == source else '：' + summary), ts)

    def _prompt(self, text, ts, human=True):
        self.stats['prompts'] += 1
        self._pending_turn = None
        if not human:
            self.stats['non_human_prompts'] += 1
        self._reset_flow(ts)
        self._set('prompt', 'done', None, ts, force=True)
        self.c['rounds'] += 1
        summary = _prompt_summary(text)
        self._in['source'], self._in['summary'] = '你输入', summary
        self._add_log('收到 prompt', summary, ts)

    def _notification(self, text, ts):
        f = _notif_fields(text)
        summary = f.get('status') or '通知'
        if self._inflight:
            # 还有工具没结束（记录乱序）：和别的会话发来的消息一样，不清场，只换来源（15 号件 R-4）
            self._pending_turn = None
            self._in['source'], self._in['summary'] = '后台通知', summary
        elif self._busy():
            # 回合还没收尾（旧格式可能没有结束标记），或通知晚于已开始的模型调用写下：照旧当场开新回合
            # （已开始的那次模型调用由 _reset_flow 带进新回合）
            self._open_turn('后台通知', summary, '后台通知', ts)
        else:
            # 回合已结束：Claude Code 有时在回合结束后才补写通知、之后不再调模型，先不开回合
            self._pending_turn = summary
        self._add_log('通知', f.get('status', ''), ts)
        self._count_notification(f)
        self._match_agent_end(f, ts)

    def _count_notification(self, fields):
        self._in['notifications'] += 1
        status = fields.get('status')
        if status:
            self._notif[status] += 1

    def _match_agent_end(self, fields, ts):
        tid = fields.get('tool-use-id')
        if not tid:
            return
        for aid, a in self.agents.items():
            if a.get('tool_use_id') == tid:
                self._agent_finished(aid, fields.get('status'), ts)
                return

    def _assistant(self, o, ts):
        msg = o.get('message')
        if not isinstance(msg, dict):
            return
        rid = o.get('requestId')
        if rid and rid not in self._requests:
            self._requests.add(rid)
            self.c['model_calls'] += 1
            self._open_pending_turn(ts)
            if not self._turn_active() and (self._orphan_api_ts is not None or self._saw_api or self._saw_origin):
                self._open_implicit_turn(ts)
            missing = not self._api_pending
            self._turn_reqs.add(rid)
            self._api_pending = False
            self._sync_loop()
            if missing:
                # 这次调用没有 api-request 记录（各版本都有，2.1.316 约四成回合）：收到回复时补算（D-111）
                if not self._saw_api:
                    self.degraded['model'] = True
                self._set('model', 'active', None if self._saw_api else '推断', ts)
                self._add_log('模型调用', '第 %d 次' % self.c['loop'] + ('' if self._saw_api else '（推断）'), ts)
        usage = msg.get('usage')
        if isinstance(usage, dict):
            total = sum(_int(usage.get(k)) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
            if total:
                self.c['context_tokens'] = total
                self._md['usage'] = usage
        model = msg.get('model')
        if isinstance(model, str) and model and not model.startswith('<'):
            self._md['model'] = model
        if isinstance(msg.get('stop_reason'), str):
            self._md['stop'] = msg['stop_reason']
        tms = o.get('thinkingDurationMs')
        if isinstance(tms, (int, float)) and not isinstance(tms, bool) and tms > 0:
            self._lv['think_ms'] += tms
        if o.get('isApiErrorMessage'):
            self._set('model', 'error', None, ts)
            self._add_log('模型调用', 'API 错误', ts)
            return
        content = msg.get('content')
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            kind = b.get('type')
            if kind == 'thinking':
                self._md['think_blocks'] += 1
                self._add_log('思考', '', ts)
            elif kind == 'text':
                self._add_log('输出文字', '', ts)
            elif kind == 'tool_use':
                self._tool_started(b, ts)

    def _tool_started(self, block, ts):
        tid, name = block.get('id'), str(block.get('name') or '')
        if self.nodes['model']['state'] == 'active':
            self._set('model', 'done', None, ts)
        if not self._inflight:
            self._batch_err = self._batch_rej = False
            self._tl['batch'] = 0
        self._inflight.add(tid)
        self.c['inflight'] = len(self._inflight)
        self.c['tool_calls'] += 1
        self._tool_info[tid] = (name, ts)
        self._set('tool', 'active', self._tool_label(), ts)
        summary = _summary((name + ' ' + _tool_arg(block.get('input'))).strip())
        self._tl['current'] = summary
        self._tl['batch'] += 1
        cat = 'MCP' if name.startswith('mcp__') else TOOL_CATEGORIES.get(name, '其他')
        self._tl['cats'][cat] += 1
        self._lv['cats_turn'][cat] += 1
        self._add_log('工具开始', summary, ts)
        inp = block.get('input')
        self._live_tool_started(tid, name, cat, inp, ts)
        path = inp.get('file_path') if isinstance(inp, dict) else None
        if _is_memory_path(path):
            self._mem['writes' if name in ('Edit', 'Write', 'NotebookEdit') else 'reads'] += 1
            self._set('memory', 'done', None, ts)
            self._add_log('记忆', ('写 ' if name in ('Edit', 'Write', 'NotebookEdit') else '读 ') + os.path.basename(path), ts)

    def _live_tool_started(self, tid, name, cat, inp, ts):
        L = self._lv
        L['turn_tools'] += 1
        if name == 'Skill':
            L['skills_turn'] += 1
            L['skills_total'] += 1
        elif name == 'ToolSearch':
            L['search_turn'] += 1
            L['search_total'] += 1
        elif cat == 'MCP':
            L['mcp_calls'] += 1
            L['mcp_turn'] += 1
        if name in TASK_TOOLS and isinstance(inp, dict):
            self._tool_inputs[tid] = (name, inp)
        self._tools.pop(tid, None)
        self._tools[tid] = {'id': tid, 'name': name, 'target': _summary(_tool_arg(inp)), 'cat': cat, 'state': 'running',
                            't0': ts, 't1': None, 'perm': None}
        while len(self._tools) > TOOL_LIMIT:
            self._tools.popitem(last=False)

    def _live_tool_finished(self, tid, state, perm_text, ts, tur):
        r = self._tools.get(tid)
        if r is not None:
            r['state'], r['t1'] = state, ts
            if perm_text:
                r['perm'] = perm_text
        name, inp = self._tool_inputs.pop(tid, (None, None))
        if name is None or state != 'done':
            return
        if name == 'TaskCreate':
            task = tur.get('task') if tur else None
            if isinstance(task, dict) and task.get('id') is not None:
                k = str(task['id'])
                self._tasks[k] = {'id': k, 'subject': task.get('subject') or inp.get('subject'), 'status': 'pending', 'active': inp.get('activeForm')}
        else:
            k = str(inp.get('taskId')) if inp.get('taskId') is not None else None
            t = self._tasks.get(k)
            if t is None:
                return
            status = inp.get('status')
            if status == 'deleted':
                self._tasks.pop(k, None)
                return
            if isinstance(status, str):
                t['status'] = status
            for f, key in (('subject', 'subject'), ('activeForm', 'active')):
                if isinstance(inp.get(f), str):
                    t[key] = inp[f]

    def _tool_finished(self, tid, is_error, pd, ts, tur=None, stopped=False):
        if tid in self._inflight:
            self._inflight.discard(tid)
        else:
            self.stats['inflight_underflow'] += 1
        self.c['inflight'] = len(self._inflight)
        if stopped:
            # 「User rejected tool use」且没有权限判定：等你批准时点了「不」算你拒绝了；
            # 否则是运行中按了 Esc，算被中断，不算出错（D-111）。两种情况的记录完全一样（都紧跟中断标记，
            # 2.1.322 驱动实测），只能看当时是否在等批准。进程文件每 0.25 秒才读一次，可能先读到「已不在等」：
            # 等批准结束不到 1 秒就到了这条结果，也算你拒绝了（15 号件 R-9）
            pe = self._perm_wait_end
            if self.nodes['permission']['state'] == 'waiting' or (pe is not None and ts is not None and pe >= ts - PERM_REJECT_WINDOW):
                self._perm_wait_end = None
                pd = {'decision': 'reject', 'source': 'user_reject'}
            else:
                self._tool_stopped(tid, ts)
                return
        decision = pd.get('decision') if pd else None
        text = None
        if pd and decision:
            key, text = _perm(pd)
            if decision == 'reject':
                self._tl['rejects'] += 1
                self._set('permission', 'rejected', text, ts)
            else:
                self._tl['ok_user' if pd.get('source') == 'user_temporary' else 'ok_config'] += 1
                self._set('permission', 'passed', text, ts)
            self._tl['perm'] = text
            self._lv['perm_counts'][key] += 1
            self._lv['perm_turn'][key] += 1
            self._lv['perm_last'] = text
        self._live_tool_finished(tid, 'rejected' if decision == 'reject' else 'error' if is_error else 'done', text, ts, tur)
        if decision == 'reject':
            self._batch_rej = True
        elif is_error:
            self._batch_err = True
            self._tl['errors'] += 1
            self._lv['errors_turn'] += 1
        if not self._inflight:
            self._tl['current'] = None
            if self._batch_err:
                self._set('tool', 'error', None, ts)
            elif self._batch_rej:
                self._set('tool', 'rejected', '已拒绝', ts)
            else:
                self._set('tool', 'done', None, ts)
        name, t0 = self._tool_info.pop(tid, ('', None))
        secs = ' 约 %.1f 秒' % (ts - t0) if ts and t0 and ts >= t0 else ''
        self._add_log('工具结束', (name + secs + ('（已拒绝）' if decision == 'reject' else '（出错）' if is_error else '')).strip(), ts)

    def _tool_stopped(self, tid, ts):
        """运行中被你按 Esc 停下的工具：标「中断」，不算出错（定稿 F2）。"""
        r = self._tools.get(tid)
        if r is not None:
            r['state'], r['t1'] = 'interrupted', ts
        self._tool_inputs.pop(tid, None)
        if not self._inflight:
            self._tl['current'] = None
            self._set('tool', 'interrupted', None, ts)
        name, t0 = self._tool_info.pop(tid, ('', None))
        secs = ' 约 %.1f 秒' % (ts - t0) if ts and t0 and ts >= t0 else ''
        self._add_log('工具结束', (name + secs + '（被中断）').strip(), ts)

    def _attachment(self, o, ts):
        a = o.get('attachment')
        if not isinstance(a, dict):
            return
        t = a.get('type')
        if t in ('auto_mode', 'auto_mode_exit', 'plan_mode', 'plan_mode_reentry', 'plan_mode_exit'):
            self._note_mode_hint(t)
        if t == 'queued_command':
            body = _text_of(a.get('prompt'))
            if a.get('commandMode') == 'task-notification':
                f = _notif_fields(body)
                self._add_log('通知', f.get('status', ''), ts)
                self._count_notification(f)
                self._match_agent_end(f, ts)
            else:
                self._in['interjected'] += 1
                self._add_log('插话', _summary(body), ts)
            return
        t = str(t) if t else '(无类型)'
        is_hook = t.startswith('hook_') or 'hookEvent' in a
        if t in ATT_REMINDER:
            kind = 'reminder'
        elif t in ATT_LIST:
            kind = 'list'
        else:
            kind = 'context'
            if t not in ATT_CONTEXT and not is_hook:
                self._new_type('attachment/' + t)
        item = self.context.setdefault(t, {'kind': kind, 'count': 0, 'size': 0, 'ts': None})
        item['count'] += 1
        item['size'] = len(json.dumps(a, ensure_ascii=False, default=str))
        item['ts'] = ts
        self._note_attachment(t, a, is_hook, ts)
        if kind == 'reminder':
            self._cx['round_rem'] += 1
        elif kind == 'context':
            self._cx['round_ctx'] += 1
        if kind == 'context':
            self._set('context', 'done', None, ts)
            e = self._ctx_entry
            if e is not None and t not in e['summary'] and len(e['summary']) < SUMMARY_LIMIT:
                e['summary'] += '、' + t
            elif e is None:
                self._ctx_entry = None
                self._ctx_entry = self._add_log('上下文注入', t, ts)

    def _note_attachment(self, t, a, is_hook, ts):
        """从 attachment 里取八大块小项要的数字。只取大小和名称，不留内容。"""
        c, m = self._cx, self._mem
        if t == 'prompt_snapshot':
            sp = a.get('systemPrompt')
            if isinstance(sp, str):
                c['prompt_size'] = len(sp)
            elif isinstance(sp, list):
                c['prompt_size'] = sum(len(x) if isinstance(x, str) else len(json.dumps(x, default=str)) for x in sp)
            if isinstance(a.get('tools'), list):
                c['tools'] = len(a['tools'])
        elif t == 'skill_listing':
            if isinstance(a.get('skillCount'), int):
                c['skills'] = a['skillCount']
        elif t == 'mcp_instructions_delta':
            c['mcp'] |= set(_str_list(a.get('addedNames')))
            c['mcp'] -= set(_str_list(a.get('removedNames')))
        elif t == 'deferred_tools_delta':
            c['deferred'] |= set(_str_list(a.get('addedNames')))
            c['deferred'] -= set(_str_list(a.get('removedNames')))
        elif t == 'environment':
            s = a.get('snapshot')
            if isinstance(s, dict):
                c['env'] = ' · '.join(x for x in ('worktree' if s.get('isWorktree') else '', 'git 仓库' if s.get('isGitRepo') else '', s.get('platform') if isinstance(s.get('platform'), str) else '') if x) or None
                c['env_raw'] = {'worktree': bool(s.get('isWorktree')), 'git': bool(s.get('isGitRepo')),
                                'platform': s.get('platform') if isinstance(s.get('platform'), str) else None}
        elif t == 'instructions':
            files = a.get('files')
            for f in files if isinstance(files, list) else ():
                if isinstance(f, dict) and isinstance(f.get('path'), str):
                    body = f.get('content')
                    m['files'][f['path']] = {'type': _s(f.get('type')), 'size': len(body) if isinstance(body, str) else 0}
            for p in _str_list(a.get('removed')):
                m['files'].pop(p, None)
            self._set('memory', 'done', None, ts)
        elif t == 'nested_memory':
            m['nested'].add(a.get('path') if isinstance(a.get('path'), str) else '')
            self._set('memory', 'done', None, ts)
        elif t == 'compact_file_reference':
            m['compact_refs'] += 1
            self._set('memory', 'done', None, ts)
        if is_hook:
            self._note_hook(t, a, ts)

    def _note_mode_hint(self, t):
        """记录里从没出现过 permission-mode 时（如 2.1.318 的老会话），从模式附件推断模式（D-110）。
        附件和 permission-mode 并不总对得上（plan_mode 常出现在 auto 期间），所以只在没有 permission-mode 时用。"""
        if t == 'auto_mode':
            self._mode_hint = 'auto'
        elif t == 'auto_mode_exit':
            self._mode_hint = None
        elif t in ('plan_mode', 'plan_mode_reentry'):
            if self._mode_hint != 'plan':
                self._mode_before_plan = self._mode_hint
            self._mode_hint = 'plan'
        elif t == 'plan_mode_exit' and self._mode_hint == 'plan':
            self._mode_hint = self._mode_before_plan

    def _note_hook(self, t, a, ts):
        h = self._hk
        event = a.get('hookEvent') if isinstance(a.get('hookEvent'), str) else '(未知)'
        if t == 'hook_additional_context':
            h['ctx_count'] += 1
            body = a.get('content')
            h['ctx_size'] += len(body) if isinstance(body, str) else sum(len(x) for x in _str_list(body))
            state_error = False
        else:
            h['events'][event] += 1
            self._lv['hooks_turn'][event] += 1
            self._lv['hooks_total'] += 1
            code = a.get('exitCode')
            state_error = t in HOOK_ERRORS or (isinstance(code, int) and code != 0)
            if state_error:
                h['errors'] += 1
            self._add_log('Hook', event + ('（出错）' if state_error else ''), ts)
        self._hook_state(state_error, ts)

    def _hook_state(self, is_error, ts):
        if is_error:
            self._set('hooks', 'error', None, ts)
        elif self.nodes['hooks']['state'] != 'error':
            self._set('hooks', 'done', None, ts)

    def _api_request(self, o, ts):
        q = o.get('querySource')
        self._saw_api = True
        p = o.get('params')
        if isinstance(p, dict):
            if isinstance(p.get('model'), str):
                self._md['model'] = p['model']
            oc, th = p.get('output_config'), p.get('thinking')
            if isinstance(oc, dict) and isinstance(oc.get('effort'), str):
                self._md['effort'] = oc['effort']
            if isinstance(th, dict) and isinstance(th.get('type'), str):
                self._md['thinking'] = th['type']
        if q == 'sdk' and self._headless:
            # 无头会话（claude -p）的主线程请求写的是 sdk（15 号件 R-12）
            q = 'repl_main_thread'
        if q == 'repl_main_thread' or (isinstance(q, str) and q.startswith('agent:')):
            self._open_pending_turn(ts)
            if self._api_pending and self.nodes['model']['state'] == 'active':
                # 上一次请求还没有回复就又发了一次：重试或丢弃重发，不算新的一次调用（D-111）
                self.stats['api_retries'] += 1
                self._api_ts = ts
                self._add_log('模型调用', '重新请求', ts)
                return
            if not self._turn_active() and q == 'repl_main_thread':
                # 没有进行中的回合却调模型（如 /login 后自动续跑，或 prompt 记录晚写）：先记下，
                # 紧接着到 prompt 就归它；先到回复就开一个「自动继续」回合（D-111）
                self._turn_reqs = set()
                self._orphan_api_ts = ts
            self._api_pending = True
            self._api_ts = ts
            self._sync_loop()
            self._set('model', 'active', None, ts)
            self._add_log('模型调用', '第 %d 次' % self.c['loop'], ts)

    def _sync_loop(self):
        self.c['loop'] = len(self._turn_reqs) + (1 if self._api_pending else 0)

    def _open_implicit_turn(self, ts):
        """没有任何开回合的记录、模型却已经回复：开一个「自动继续」回合，起点取那次请求的时刻。"""
        start = self._orphan_api_ts if self._orphan_api_ts is not None else ts
        pending = self._api_pending
        cmd = self._last_cmd
        if cmd and start is not None and cmd[0] is not None and 0 <= start - cmd[0] < 15:
            # 刚运行过的命令（! 命令、斜杠命令）之后 Claude Code 调了模型：这一轮是你的命令开的
            source, summary = '你的命令', cmd[1]
        else:
            source, summary = '自动继续', '没有新的输入，Claude Code 接着跑'
        self._open_turn(source, summary, source, start)
        self._api_pending = pending
        self._sync_loop()
        # 「花在哪」从那次请求的时刻算起，和本轮用时的起点一致
        self._lv['phase'], self._lv['since'] = 'model', start
        self.stats['implicit_turns'] += 1
        self._add_log(source, summary, start)

    def _system(self, o, ts):
        sub = o.get('subtype')
        if sub == 'turn_duration':
            # 本轮用时取回合实际经过的时间（含等你批准、等你回答），和「花在哪」一致（D-110）；
            # Claude Code 报的 durationMs 不含等你的时间，只在不知道回合开始时刻时用
            start = self._lv['turn_start']
            if self._turn_active() and ts is not None and ts >= start:
                ms = (ts - start) * 1000
            elif self._interrupted and self._ss['turn_ms'] is not None:
                # 中断时已按实际经过的时间记下，随后到的 turn_duration 不覆盖（D-111）
                ms = self._ss['turn_ms']
            else:
                ms = o.get('durationMs')
            self._ss['turn_ms'] = ms
            self._turn_finished(ms, ts)
        elif sub == 'compact_boundary':
            m = o.get('compactMetadata') if isinstance(o.get('compactMetadata'), dict) else {}
            pre, post = m.get('preTokens'), m.get('postTokens')
            label = '%s → %s token' % (pre, post) if pre is not None and post is not None else None
            self._set('compact', 'done', label, ts)
            if isinstance(post, (int, float)):
                self.c['context_tokens'] = post
            trigger = m.get('trigger')
            self._ss['compacts'][trigger if trigger in ('auto', 'manual') else 'auto'] += 1
            ms = m.get('durationMs')
            self._ss['last_compact_raw'] = {'pre': pre if isinstance(pre, int) else None, 'post': post if isinstance(post, int) else None,
                                            'ms': ms if isinstance(ms, (int, float)) else None,
                                            'trigger': trigger if trigger in ('auto', 'manual') else 'auto', 'ts': ts}
            if isinstance(pre, int) and isinstance(post, int):
                self._ss['last_compact'] = '%s → %s' % (_k(pre), _k(post)) + (' · %d 秒' % round(ms / 1000) if isinstance(ms, (int, float)) else '')
            self._add_log('压缩', label or '完成', ts)
        elif sub == 'stop_hook_summary':
            count = o.get('hookCount')
            errs = o.get('hookErrors')
            n_err = len(errs) if isinstance(errs, list) else 0
            n_stop = count if isinstance(count, int) and not isinstance(count, bool) else 1
            self._hk['events']['Stop'] += n_stop
            self._lv['hooks_turn']['Stop'] += n_stop
            self._lv['hooks_total'] += n_stop
            self._hk['errors'] += n_err
            self._hook_state(n_err > 0, ts)
            self._add_log('Hook', 'Stop', ts)
        elif sub == 'agents_killed':
            # 你按 ctrl+x ctrl+k 停掉后台子 agent（2.1.322 实测）：记录不带是哪几个，当时在跑的都算被你停止（D-111）
            for aid, a in list(self.agents.items()):
                if a['state'] == 'running':
                    self._agent_finished(aid, 'stopped', ts)
        elif sub not in HIDDEN_SYSTEM:
            self._new_type('system/' + (str(sub) if sub else '(无)'))

    def _close_tools(self, state, ts):
        for r in self._tools.values():
            if r['state'] == 'running':
                r['state'], r['t1'] = state, ts

    def _check_stuck(self):
        for n in NODES:
            if n != 'agent' and self.nodes[n]['state'] in ('active', 'waiting'):
                self.stats['stuck_after_turn'] += 1
                return

    def _turn_finished(self, ms, ts):
        for n in ('model', 'tool', 'compact'):
            if self.nodes[n]['state'] in ('active', 'waiting'):
                self._set(n, 'done', None, ts)
        self._inflight.clear()
        self.c['inflight'] = 0
        self._close_tools('done', ts)
        if not self._interrupted or self._lv['turn_end'] is None:
            # 中断时已记下结束时刻，后到的 turn_duration 不改它，和保留下来的用时一致（15 号件 R-8）
            self._lv['turn_end'] = ts
        if not self._interrupted:
            label = _dur(ms)
            self._set('turn', 'done', label, ts)
            self._add_log('回合结束', label or '', ts)
        self._check_stuck()

    def _interrupt(self, for_tool, ts):
        if self.nodes['model']['state'] == 'active':
            self._set('model', 'interrupted', None, ts)
        if for_tool or self.nodes['tool']['state'] in ('active', 'waiting'):
            self._set('tool', 'interrupted', None, ts)
        if self.nodes['permission']['state'] == 'waiting':
            self._set('permission', 'idle', None, ts)
        if self.nodes['compact']['state'] == 'active':
            self._set('compact', 'idle', None, ts)
        self._set('turn', 'interrupted', None, ts)
        self._inflight.clear()
        self.c['inflight'] = 0
        for r in self._tools.values():
            if r['state'] == 'running':
                self._tool_info.pop(r['id'], None)
                self._add_log('工具结束', r['name'] + '（被中断）', ts)
        self._close_tools('interrupted', ts)
        start = self._lv['turn_start']
        if self._turn_active() and ts is not None and start is not None and ts >= start:
            self._ss['turn_ms'] = (ts - start) * 1000
        self._lv['turn_end'] = ts
        self._interrupted = True
        self._add_log('中断', '用户中断', ts)
        self._check_stuck()

    # ---------- 内部：等人 ----------

    def _wait_started(self, wf):
        now = time.time()
        if wf == 'permission prompt':
            if self.nodes['model']['state'] == 'active':
                self._set('model', 'done', None, now)
            self._set('permission', 'waiting', '等你批准', now)
            self._add_log('等你批准', '', now)
        elif wf == 'input needed':
            if self.nodes['model']['state'] == 'active':
                self._set('model', 'done', None, now)
            self._set('tool', 'waiting', '等你回答', now)
            self._add_log('等你回答', '', now)

    def _end_wait(self, wf, now):
        if wf == 'permission prompt':
            self._perm_wait_end = now
        self._wait_ended()

    def _wait_ended(self):
        if self.nodes['permission']['state'] == 'waiting':
            self._set('permission', 'idle')
        if self.nodes['tool']['state'] == 'waiting':
            self._set('tool', 'active', self._tool_label())

    # ---------- 内部：子 agent ----------

    def _ensure_agent(self, aid, meta=None, ts=None):
        a = self.agents.get(aid)
        if a is None:
            a = {'id': aid, 'type': None, 'description': None, 'state': 'running', 'step': '', 'tool_use_id': None}
            self.agents[aid] = a
            self.c['agents'] = len(self.agents)
            self._refresh_agent_node(None)
            # 启动时刻取这个子 agent 自己第一条记录的时间；没有才用主流程最后一条记录的时间（15 号件 R-7）
            self._add_log('子 agent', '启动', ts if ts is not None else self.last_record_ts)
        if meta:
            a['type'] = _s(meta.get('agentType')) or a['type']
            a['description'] = _s(meta.get('description')) or a['description']
            a['tool_use_id'] = _s(meta.get('toolUseId')) or a['tool_use_id']
        return a

    def _refresh_agent_node(self, ts):
        states = [a['state'] for a in self.agents.values()]
        if not states:
            self._set('agent', 'idle', None, ts)
        elif 'running' in states:
            self._set('agent', 'active', None, ts)
        elif 'failed' in states:
            self._set('agent', 'error', None, ts)
        else:
            self._set('agent', 'done', None, ts)

    def _agent_finished(self, aid, status, ts):
        a = self.agents.get(aid)
        if a is None:
            return
        new = 'failed' if status == 'failed' else 'done'
        if ts is not None:
            # 已经由 meta 判为结束时也补记结束时刻（meta 没有时间）
            self._agent_end_ts[aid] = ts
        if a['state'] == new:
            return
        a['state'] = new
        stopped = status == 'stopped'
        if stopped:
            # 你停掉的子 agent（meta 写 stoppedByUser，或通知 status 为 stopped）：不是失败，写「被你停止」（D-111）
            a['step'] = '被你停止'
        self._last_agent = ' · '.join(x for x in (a['type'], a['description'], '失败' if new == 'failed' else '被你停止' if stopped else '') if x) or '未知'
        self._refresh_agent_node(ts)
        self._add_log('子 agent', '被你停止' if stopped else '结束' if new == 'done' else '失败', ts)

    def _agent_record(self, aid, o, ts):
        a = self._ensure_agent(aid, ts=ts)
        t = o.get('type')
        if ts is not None:
            self._agent_last_ts[aid] = max(ts, self._agent_last_ts.get(aid, ts))
        if ts is not None and t in ('api-request', 'assistant'):
            self._agent_act_ts[aid] = max(ts, self._agent_act_ts.get(aid, ts))
        end = self._agent_end_ts.get(aid)
        late = end is not None and ts is not None and ts <= end
        if a['state'] != 'running' and t in ('api-request', 'assistant') and not late:
            self._agent_end_ts.pop(aid, None)
            a['state'] = 'running'
            self._agent_meta_finished[aid] = False
            self._refresh_agent_node(ts)
        if t == 'api-request':
            a['step'] = '调用模型'
            self.stats['agent_api'] += 1
        elif t == 'assistant':
            msg = o.get('message')
            content = msg.get('content') if isinstance(msg, dict) else None
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        a['step'] = _summary((str(b.get('name') or '') + '  ' + _tool_arg(b.get('input'))).strip())
                    elif isinstance(b, dict) and b.get('type') == 'text':
                        a['step'] = '输出文字'


class LineReader:
    """增量读取只追加的 JSONL 文件。只处理完整的行，文件变小时从头重读。"""

    BAD_LINE = object()

    def __init__(self, path):
        self.path = path
        self._pos = 0
        self._buf = b''
        # 文件变小、从头重读的次数：调用方据此整份重建状态，不把重读出的记录当新增（15 号件 R-5）
        self.resets = 0

    def read(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self._pos:
            self._pos = 0
            self._buf = b''
            self.resets += 1
        if size == self._pos:
            return []
        with open(self.path, 'rb') as f:
            f.seek(self._pos)
            data = f.read(size - self._pos)
        self._pos += len(data)
        *lines, self._buf = (self._buf + data).split(b'\n')
        out = []
        for ln in lines:
            if not ln.strip():
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                out.append(self.BAD_LINE)
                continue
            out.append(o if isinstance(o, dict) else self.BAD_LINE)
        return out


def doctor(projects_root):
    """把解析和状态机跑遍全部记录，只返回统计，不含任何会话内容。"""
    r = collections.Counter(files=0, records=0, bad_lines=0, crashes=0, prompts=0, non_human_prompts=0,
                            inflight_underflow=0, stuck_after_turn=0, split_negative=0, meta_total=0, meta_finished=0)
    by_version = {}

    def slot(v):
        return by_version.setdefault(v or '(未知)', {'records': 0, 'api_requests': 0, 'origin_records': 0, 'human_prompts': 0, 'new_types': set()})

    def run(path, agent):
        rows = LineReader(path).read()
        r['files'] += 1
        b = Board()
        cur = next((o.get('version') for o in rows if isinstance(o, dict) and o.get('version')), None)
        for o in rows:
            if o is LineReader.BAD_LINE:
                r['bad_lines'] += 1
                continue
            cur = o.get('version') or cur
            s = slot(cur)
            s['records'] += 1
            r['records'] += 1
            s['api_requests'] += o.get('type') == 'api-request'
            s['origin_records'] += o.get('type') == 'user' and o.get('origin') is not None
            before = set(b.new_types)
            b.feed_record(o, agent=agent)
            s['new_types'] |= b.new_types - before
            b.drain()
        try:
            # 快照也跑一遍：字段形状不对时出错的地方在这一步（15 号件 R-6）
            json.dumps(b.snapshot())
        except Exception:
            r['crashes'] += 1
        r['crashes'] += b.parse_errors + b.stats['snapshot_errors']
        r['split_negative'] += any(v < 0 for v in b._lv['split'].values())
        for k in ('prompts', 'non_human_prompts', 'inflight_underflow', 'stuck_after_turn'):
            r[k] += b.stats[k]

    for path in glob.glob(os.path.join(projects_root, '*', '*.jsonl')):
        run(path, None)
    for path in glob.glob(os.path.join(projects_root, '*', '*', 'subagents', '**', '*.jsonl'), recursive=True):
        run(path, os.path.basename(path))
    for path in glob.glob(os.path.join(projects_root, '*', '*', 'subagents', '**', '*.meta.json'), recursive=True):
        try:
            with open(path) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        r['meta_total'] += 1
        r['meta_finished'] += m.get('finished') is True
    out = dict(r)
    out['by_version'] = {k: dict(v, new_types=sorted(v['new_types'])) for k, v in by_version.items()}
    return out
