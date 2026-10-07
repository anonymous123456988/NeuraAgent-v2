# -*- coding: utf-8 -*-
"""NEURA 桌面端 UI: 悬浮球(点击展开对话框/再点击收起) + HUD 对话框 + 事件桥。

v3.33 桌面版: 去掉控制板, 只有一个悬浮球; Agent 的 HUD 弹窗直接渲染到电脑屏幕
(hud.HudManager)。事件桥 DesktopEmitter 把 AgentCore 事件推入队列, tkinter 主线程
轮询更新界面; 用户在对话框发消息 -> 同线程 asyncio 任务执行。
说明: 全部控件 Canvas 自绘(规避部分 Linux/Xvfb 构建对 Label/Button/Entry/Text 的
     xcb 断言), Windows / Linux 行为一致; 中文输入经 Tk 键盘事件(IEM/XIM 提交)正常。
"""
import asyncio
import os
import json
import queue
import tkinter as tk

from .hud import HudManager, CanvasText, HUDButton

_BG = "#071120"
_BG2 = "#0d2238"
_EDGE = "#35e0ff"
_FG = "#c9f2ff"
_DIM = "#6fb3c9"
_ACCENT = "#00d4ff"
_RED = "#ff4d5e"
_FONT = ("Microsoft YaHei UI", 10)
_FONT_B = ("Microsoft YaHei UI", 11, "bold")
_FONT_T = ("Microsoft YaHei UI", 9)


class ChatCanvas:
    """Canvas 自绘对话区: 每条消息可独立着色(用户蓝 / AI 青 / 拦截红 / 工具灰)。"""

    def __init__(self, canvas: tk.Canvas):
        self.cv = canvas
        self.entries = []          # [(text, color)]
        self._items = []
        self._w = 200
        try:
            canvas.bind("<Configure>", self._on_resize)
        except Exception:
            pass

    def _on_resize(self, e):
        self._w = max(120, e.width - 20)
        self._redraw()

    def add(self, text, color=_FG):
        self.entries.append((str(text), color))
        self._redraw(scroll_to_end=True)

    def replace_last_ai(self, text, color=_FG):
        """替换最后一条 AI 流式占位(合并增量)。"""
        for i in range(len(self.entries) - 1, -1, -1):
            t, c = self.entries[i]
            if c in (_FG,) and (t.startswith("NEURA") or (not t.startswith("你") and not t.startswith("⚙") and not t.startswith("ℹ"))):
                self.entries[i] = (str(text), color)
                self._redraw(scroll_to_end=True)
                return
        self.add(text, color)

    def clear(self):
        self.entries = []
        self._redraw()

    def _redraw(self, scroll_to_end=False):
        try:
            for it in self._items:
                self.cv.delete(it)
            self._items = []
            y = 8
            for text, color in self.entries:
                for ln in self._wrap(text):
                    self._items.append(self.cv.create_text(
                        12, y, text=ln, anchor="nw", font=_FONT_T, fill=color))
                    y += 20
                y += 8
            self.cv.configure(scrollregion=(0, 0, max(self._w, 300), max(y, 80)))
            if scroll_to_end:
                self.cv.yview_moveto(1.0)
        except Exception:
            pass

    def _wrap(self, text):
        out = []
        for raw in str(text).split("\n"):
            line = raw
            if not line:
                out.append("")
                continue
            while len(line) * 8 > self._w:
                cut = max(1, self._w // 8)
                out.append(line[:cut])
                line = line[cut:]
            out.append(line)
        return out


class CanvasInput:
    """Canvas 自绘输入框: 点击聚焦, 键盘输入(ASCII + IME 中文), 回车提交。"""

    def __init__(self, canvas: tk.Canvas, on_submit, placeholder="输入指令，回车发送…"):
        self.cv = canvas
        self.on_submit = on_submit
        self.placeholder = placeholder
        self.text = ""
        self.focused = False
        self._box = canvas.create_rectangle(4, 4, 600, 40, outline=_EDGE, fill=_BG)
        self._ph = canvas.create_text(14, 24, text=placeholder, anchor="w",
                                      font=_FONT_T, fill=_DIM)
        self._tx = canvas.create_text(14, 24, text="", anchor="w", font=_FONT, fill=_FG)
        try:
            canvas.configure(takefocus=1)      # 让 Canvas 可聚焦(默认 takefocus=0 点击后无输入效果)
        except Exception:
            pass
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<ButtonRelease-1>", lambda e: self._grab_focus())
        # 注意: 只绑定 <KeyPress>! <Key> 与 <KeyPress> 是同一事件, 双绑会让每个按键
        # 触发两次 _on_key(字符重复/显示错乱); 中文 IME 提交走 <KeyPress> 的 char 属性。
        canvas.bind("<KeyPress>", self._on_key)
        canvas.focus_force()                    # 强制获取键盘焦点

    def _on_click(self, e):
        self.focused = True
        self._grab_focus()
        self._redraw()

    def _grab_focus(self):
        try:
            self.cv.focus_set()
            self.cv.focus_force()
        except Exception:
            try:
                self.cv.focus_set()
            except Exception:
                pass

    def _on_key(self, e):
        ks = getattr(e, "keysym", "")
        ch = getattr(e, "char", "")
        if ks == "Return":
            if self.text.strip():
                t = self.text
                self.text = ""
                self._redraw()
                self.on_submit(t)
            return "break"
        if ks == "BackSpace":
            self.text = self.text[:-1]
        elif ch and ord(ch) >= 32:
            self.text += ch
        self._redraw()
        return "break"   # 阻止冒泡: 避免对话框级兜底重复输入

    def _redraw(self):
        # 用文本内容切换而非 state 隐藏(规避个别平台 itemconfig state 异常导致
        # 输入不可见/错位的问题); 每次只更新两个文本 item 的内容
        try:
            self.cv.itemconfig(self._box, outline=_ACCENT if self.focused else _EDGE)
            if self.text:
                shown = self.text if len(self.text) <= 60 else "…" + self.text[-59:]
                self.cv.itemconfig(self._tx, text=shown)
                self.cv.itemconfig(self._ph, text="")
            else:
                self.cv.itemconfig(self._tx, text="")
                self.cv.itemconfig(self._ph, text=self.placeholder)
        except Exception:
            pass

    def set(self, text):
        self.text = text
        self._redraw()

    def get(self):
        return self.text

    def focus(self):
        self.cv.focus_set()


class DesktopEmitter:
    """AgentCore 事件发射器 -> 队列(线程安全), UI 主线程轮询消费。"""

    def __init__(self, q: "queue.Queue"):
        self.q = q

    async def _put(self, msg: dict):
        try:
            self.q.put(msg)
        except Exception:
            pass

    async def hello(self, sessions, config_summary):
        await self._put({"type": "hello", "sessions": sessions, "config": config_summary})
    async def session_created(self, session):
        await self._put({"type": "session_created", "session": session})
    async def session_updated(self, session):
        await self._put({"type": "session_updated", "session": session})
    async def session_deleted(self, session_id):
        await self._put({"type": "session_deleted", "session_id": session_id})
    async def agent_status(self, status, mode=""):
        await self._put({"type": "agent_status", "status": status, "mode": mode})
    async def message_delta(self, session_id, text, final=False):
        await self._put({"type": "message_delta", "session_id": session_id, "text": text, "final": final})
    async def message_done(self, session_id, text, tools, blocked=False):
        await self._put({"type": "message_done", "session_id": session_id,
                         "text": text, "tools": tools, "blocked": blocked})
    async def tool_event(self, tool, args, status, error=None, admin=False):
        await self._put({"type": "tool_event", "tool": tool, "args": args,
                         "status": status, "error": error, "admin": admin})
    async def panel(self, window_id, window_type, title, content, status=None, admin=False):
        await self._put({"type": "panel", "window_id": window_id, "window_type": window_type,
                         "title": title, "content": content, "status": status, "admin": admin})
    async def panel_update(self, window_id, content=None, status=None, append=None):
        await self._put({"type": "panel_update", "window_id": window_id,
                         "content": content, "status": status, "append": append})
    async def panel_close(self, window_id):
        await self._put({"type": "panel_close", "window_id": window_id})
    async def panel_control(self, action, window_id, x=None, y=None, kind=None, direction=None):
        await self._put({"type": "panel_control", "action": action, "window_id": window_id,
                         "x": x, "y": y, "kind": kind, "direction": direction})
    async def admin_challenge(self, operation_id, description, tool, args):
        await self._put({"type": "admin_challenge", "operation_id": operation_id,
                         "description": description, "tool": tool, "args": args})
    async def admin_auth_result(self, operation_id, ok, message):
        await self._put({"type": "admin_auth_result", "operation_id": operation_id,
                         "ok": ok, "message": message})
    async def notify(self, message, level="info"):
        await self._put({"type": "notify", "message": message, "level": level})
    async def download_ready(self, filename, url):
        await self._put({"type": "download_ready", "filename": filename, "url": url})
    async def command_stream(self, window_id, line, status="running"):
        await self._put({"type": "command_stream", "window_id": window_id, "line": line, "status": status})


class NeuraDesktopUI:
    """悬浮球 + HUD 对话框 + 事件消费。"""

    def __init__(self, core, cfg: dict):
        self.core = core
        self.cfg = cfg
        self.q: "queue.Queue" = queue.Queue()
        self.emitter = DesktopEmitter(self.q)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.current_sid: str | None = None
        self._ai_streaming = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("NEURA")
        self.hud = HudManager(self.root)
        self._build_ball()
        self._build_dialog()
        self.root.after(40, self._poll)
        self.root.after(200, self._refresh_sessions)

    # ================= 悬浮球 =================
    def _build_ball(self):
        self.ball = tk.Toplevel(self.root)
        self.ball.overrideredirect(True)
        try:
            self.ball.attributes("-topmost", True)
            self.ball.attributes("-alpha", 0.92)
        except Exception:
            pass
        size = 64
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.ball.geometry(f"{size}x{size}+{sw - size - 40}+{sh - size - 60}")
        cv = tk.Canvas(self.ball, width=size, height=size, bg=_BG, highlightthickness=0)
        cv.pack()
        cv.create_oval(2, 2, size - 2, size - 2, outline=_EDGE, width=2, fill=_BG2)
        cv.create_oval(12, 12, size - 12, size - 12, outline=_ACCENT, width=1, fill="#0b2440")
        cv.create_text(size // 2, size // 2, text="NEURA", fill=_FG,
                       font=("Segoe UI", 8, "bold"))
        cv.create_oval(size - 18, 4, size - 6, 16, outline="", fill=_ACCENT)
        self._drag = {"x": 0, "y": 0, "moved": False}
        # 仅绑定 Canvas(勿再绑 ball: 双绑定会让一次点击触发两次 toggle, 表现为"点击无反应")
        cv.bind("<Button-1>", self._ball_start)
        cv.bind("<B1-Motion>", self._ball_drag)
        cv.bind("<ButtonRelease-1>", self._ball_release)

    def _ball_start(self, e):
        self._drag.update({"x": e.x_root, "y": e.y_root, "moved": False})

    def _ball_drag(self, e):
        dx = e.x_root - self._drag["x"]
        dy = e.y_root - self._drag["y"]
        if abs(dx) + abs(dy) > 6:
            self._drag["moved"] = True
        x = self.ball.winfo_x() + dx
        y = self.ball.winfo_y() + dy
        self.ball.geometry(f"+{x}+{y}")
        self._drag["x"], self._drag["y"] = e.x_root, e.y_root

    def _ball_release(self, e):
        if not self._drag["moved"]:
            self._toggle_dialog()

    def _toggle_dialog(self):
        if self.dialog.winfo_viewable():
            self.dialog.withdraw()
        else:
            self._place_dialog()
            self.dialog.deiconify()
            self.dialog.lift()
            self._refresh_sessions()
            self.input.focus()

    # ================= 对话框 =================
    def _build_dialog(self):
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title("NEURA 对话")
        self.dialog.overrideredirect(True)
        try:
            self.dialog.attributes("-topmost", True)
            self.dialog.attributes("-alpha", 0.94)
        except Exception:
            pass
        self._place_dialog()
        self.dialog.withdraw()
        # 对话框级键盘兜底: 点击会话/对话区导致焦点丢失后, 打字仍进输入框
        self.dialog.bind("<KeyPress>", lambda e: self._dialog_key(e))
        frame = tk.Frame(self.dialog, bg=_BG2, highlightbackground=_EDGE,
                         highlightthickness=1, bd=0)
        frame.pack(fill="both", expand=True)
        # 标题栏(Canvas 自绘)
        bar = tk.Canvas(frame, bg=_BG, height=34, highlightthickness=0)
        bar.pack(fill="x")
        bar.create_text(10, 17, text="◈ NEURA", font=("Segoe UI", 11, "bold"),
                        fill=_ACCENT, anchor="w")
        bar.create_text(120, 17, text="自主桌面智能体", font=_FONT_T, fill=_DIM, anchor="w")
        HUDButton(bar, 560, 6, 26, 22, "✕", fg=_DIM, command=self._toggle_dialog,
                  edge=_EDGE, bg=_BG)
        bar.bind("<Button-1>", lambda e: self._drag_dialog(e, 0))
        bar.bind("<B1-Motion>", lambda e: self._drag_dialog(e, 1))
        # 主体: 左会话列表 + 右对话区
        body = tk.Frame(frame, bg=_BG2)
        body.pack(fill="both", expand=True)
        self._build_session_list(body)
        self._build_chat(body)
        # 底部输入区(悬浮固定, 不随对话移动)
        self._build_input(frame)

    def _drag_dialog(self, e, mode):
        if mode == 0:
            self._dd = {"x": e.x_root - self.dialog.winfo_x(),
                        "y": e.y_root - self.dialog.winfo_y()}
        else:
            self.dialog.geometry(f"+{e.x_root - self._dd['x']}+{e.y_root - self._dd['y']}")

    def _place_dialog(self):
        w, h = 620, 660
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.dialog.geometry(f"{w}x{h}+{max(10, sw - w - 40)}+{max(10, sh - h - 80)}")

    def _build_session_list(self, body):
        left = tk.Frame(body, bg=_BG, width=150)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        cv = tk.Canvas(left, bg=_BG, height=26, highlightthickness=0)
        cv.pack(fill="x")
        cv.create_text(8, 13, text="会话", font=_FONT_T, fill=_DIM, anchor="w")
        cv.create_line(6, 22, 144, 22, fill="#123a52")
        list_cv = tk.Canvas(left, bg=_BG, highlightthickness=0)
        sb = tk.Scrollbar(left, orient="vertical", command=list_cv.yview)
        inner = tk.Frame(list_cv, bg=_BG)
        inner.bind("<Configure>", lambda e: list_cv.configure(scrollregion=list_cv.bbox("all")))
        list_cv.create_window((0, 0), window=inner, anchor="nw")
        list_cv.configure(yscrollcommand=sb.set)
        list_cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._sess_inner = inner
        self._sess_items = []

    def _build_chat(self, body):
        right = tk.Frame(body, bg=_BG2)
        right.pack(side="left", fill="both", expand=True)
        cv = tk.Canvas(right, bg=_BG, highlightthickness=0)
        sb = tk.Scrollbar(right, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._chat_canvas = cv
        self.chat = ChatCanvas(cv)
        self.chat.add("◈ NEURA 已就绪 — 点击悬浮球展开/收起", _DIM)
        # v3.39: 对话区鼠标滚轮滚动(直接滚, 无需拖滑杆)
        def _chat_wheel(e):
            try:
                if getattr(e, "num", 0) == 4:
                    cv.yview_scroll(-3, "units")
                elif getattr(e, "num", 0) == 5:
                    cv.yview_scroll(3, "units")
                else:
                    cv.yview_scroll(-3 if getattr(e, "delta", 0) > 0 else 3, "units")
                return "break"
            except Exception:
                return None
        cv.bind("<MouseWheel>", _chat_wheel)
        cv.bind("<Button-4>", _chat_wheel)
        cv.bind("<Button-5>", _chat_wheel)
        try:
            self.dialog.bind("<MouseWheel>", lambda e: _chat_wheel(e))
        except Exception:
            pass

    def _build_input(self, frame):
        inp_bar = tk.Frame(frame, bg=_BG, height=64)
        inp_bar.pack(fill="x", side="bottom")
        inp_bar.pack_propagate(False)
        cv = tk.Canvas(inp_bar, bg=_BG2, highlightthickness=0, height=46, takefocus=1)
        cv.pack(fill="x", padx=10, pady=8)
        cv.bind("<Button-1>", lambda e: (self.input._grab_focus(), self.input.focus()))
        self.input = CanvasInput(cv, self._send)
        # v3.38: Windows 原生 Entry 输入代理 —— Canvas 自绘输入在个别 Windows 环境
        # 聚焦失败, 这里用一个隐藏 Entry 持有焦点并转发键盘事件(原生控件聚焦 100% 可靠),
        # 键入内容实时同步到 Canvas 显示。Linux(Xvfb 构建 Entry 触发 xcb 崩溃)保持纯 Canvas。
        # v3.39: Windows 直接使用【原生可见 Entry】作为输入框 —— Entry 由系统控件渲染
        # 键入文本, 100% 可靠(根治"输入内容不显示在输入框/跑到对话框上方")。
        # Linux(Xvfb 构建 Entry 触发 xcb 断言崩溃)保持 Canvas 自绘输入。
        if os.name == "nt":
            try:
                entry = tk.Entry(inp_bar, bg=_BG, fg=_FG, insertbackground=_FG,
                                 font=_FONT, relief="flat", bd=0,
                                 highlightthickness=1, highlightbackground=_EDGE,
                                 highlightcolor=_ACCENT, takefocus=1)
                entry.pack(fill="x", padx=10, pady=(9, 7), ipady=7)
                _PH = "输入指令，回车发送…"
                entry.insert(0, _PH)
                entry.configure(fg=_DIM)
                def _e_focus(_=None):
                    try:
                        entry.focus_force()
                    except Exception:
                        pass
                    if entry.get() == _PH:
                        entry.delete(0, "end")
                        entry.configure(fg=_FG)
                entry.bind("<Button-1>", lambda e: _e_focus())
                def _e_lost(_=None):
                    try:
                        if not entry.get().strip():
                            entry.insert(0, _PH)
                            entry.configure(fg=_DIM)
                    except Exception:
                        pass
                entry.bind("<FocusOut>", _e_lost)
                def _e_send(e):
                    t = entry.get()
                    if t and t != _PH:
                        entry.delete(0, "end")
                        entry.configure(fg=_FG)
                        try:
                            self._send(t)
                        except Exception:
                            pass
                    return "break"
                entry.bind("<Return>", _e_send)
                cv.bind("<Button-1>", lambda e: _e_focus())
                self.input_entry = entry
                self._input_proxy = None
            except Exception:
                self.input_entry = None
                self._input_proxy = None
        else:
            self.input_entry = None
            self._input_proxy = None

    def _dialog_key(self, e):
        """对话框级键盘兜底: 仅当焦点不在输入框时转发(避免与输入框自身处理冲突)。
        点击会话/对话区导致焦点丢失后, 打字仍进输入框。"""
        if not hasattr(self, "input"):
            return
        try:
            cur = self.root.focus_get()
            # 焦点在任一输入通道(Camera 自绘输入框 / Windows 原生 Entry)时, 兜底不重复
            if cur is self.input.cv:
                return
            if getattr(self, "input_entry", None) is not None and cur is self.input_entry:
                return
        except Exception:
            pass
        ks = getattr(e, "keysym", "")
        ch = getattr(e, "char", "")
        if ch or ks in ("BackSpace", "Return"):
            self.input._on_key(e)

    # ================= 会话与消息 =================
    def _refresh_sessions(self):
        for it in getattr(self, "_sess_items", []):
            try:
                it[1].destroy()
            except Exception:
                pass
        self._sess_items = []
        try:
            sessions = self.core.sessions.list()[:20]
        except Exception:
            sessions = []
        for s in sessions:
            sid, title = s.id, s.title
            bg = _BG2 if sid == self.current_sid else _BG
            fg = _FG if sid == self.current_sid else _DIM
            b = tk.Frame(self._sess_inner, bg=bg, height=26)
            b.pack(fill="x", padx=4, pady=2)
            cv = tk.Canvas(b, bg=bg, height=26, highlightthickness=0)
            cv.pack(fill="both", expand=True)
            cv.create_text(8, 13, text=("● " if sid == self.current_sid else "· ") + title[:12],
                           font=_FONT_T, fill=fg, anchor="w")
            cv.bind("<Button-1>", lambda e, sid=sid: self._switch_session(sid))
            b.bind("<Button-1>", lambda e, sid=sid: self._switch_session(sid))
            self._sess_items.append((sid, b))

    def _switch_session(self, sid):
        self.current_sid = sid
        self._load_chat(sid)
        self._refresh_sessions()

    def _ensure_session(self):
        if not self.current_sid:
            s = self.core.sessions.create()
            self.current_sid = s.id
        return self.current_sid

    def _load_chat(self, sid):
        self.chat.clear()
        try:
            s = self.core.sessions.get(sid)
        except Exception:
            s = None
        if s:
            for m in s.history[-60:]:
                role, content = m.get("role", ""), m.get("content", "")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)[:200]
                if role == "user":
                    self.chat.add("你: " + str(text)[:200], _ACCENT)
                elif role == "assistant":
                    self.chat.add("NEURA: " + str(text)[:300], _FG)

    def _send(self, text=None):
        if text is None and getattr(self, "input_entry", None) is not None:
            try:
                text = self.input_entry.get()
            except Exception:
                text = None
        text = (text or (self.input.get() if hasattr(self, "input") else "")).strip()
        if not text or self._ai_streaming:
            return
        sid = self._ensure_session()
        self.chat.add("你: " + text, _ACCENT)
        self.chat.add("NEURA: ", _FG)
        self._ai_streaming = True
        if self.loop is None or self.loop.is_closed():
            self._ai_streaming = False
            return
        try:
            # 单线程 pump: 显式挂到 self.loop(无需 running loop, after 回调中也有效)
            self.loop.create_task(self.core.start_turn(sid, text, self.emitter))
        except Exception as e:
            self.chat.add(f"(发送失败: {e})", _DIM)
            self._ai_streaming = False

    # ================= 事件消费 =================
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _handle(self, m: dict):
        t = m.get("type")
        if t == "session_created":
            self._refresh_sessions()
        elif t == "message_delta":
            if m.get("session_id") == self.current_sid:
                self.chat.replace_last_ai("NEURA: " + str(m.get("text", "")), _FG)
        elif t == "message_done":
            self._ai_streaming = False
            if m.get("session_id") == self.current_sid and m.get("text"):
                color = _RED if m.get("blocked") else _FG
                self.chat.replace_last_ai("NEURA: " + str(m["text"]), color)
            self._refresh_sessions()
        elif t == "tool_event":
            tool = m.get("tool", "")
            status = m.get("status", "")
            self.chat.add(f"⚙ {tool} {status}" + (f" · {str(m.get('error'))[:60]}" if m.get("error") else ""),
                          _DIM)
        elif t == "notify":
            self.chat.add(f"ℹ {m.get('message', '')}", "#7fe07f")
        elif t == "panel":
            self.hud.open(m.get("window_id", ""), m.get("window_type", "info"),
                          m.get("title", ""), m.get("content", ""),
                          m.get("status"), admin=bool(m.get("admin")))
        elif t == "panel_update":
            self.hud.update(m.get("window_id", ""), m.get("content"),
                            m.get("status"), append=bool(m.get("append")))
        elif t == "panel_close":
            self.hud.close(m.get("window_id", ""))
        elif t == "panel_control":
            self.hud.control(m.get("action", ""), m.get("window_id", ""),
                             m.get("x"), m.get("y"), m.get("kind"), m.get("direction"))
        elif t == "admin_challenge":
            self._show_admin(m)
        elif t == "command_stream":
            self.hud.open(m.get("window_id", ""), "process", "实时命令",
                          {"text": m.get("line", "")}, "running")

    def _show_admin(self, m):
        try:
            top = tk.Toplevel(self.root)
            top.title("系统登录密码验证")
            top.overrideredirect(True)
            try:
                top.attributes("-topmost", True)
                top.attributes("-alpha", 0.95)
            except Exception:
                pass
            top.geometry("480x260+300+220")
            fr = tk.Frame(top, bg=_BG2, highlightbackground=_RED, highlightthickness=1)
            fr.pack(fill="both", expand=True)
            cv = tk.Canvas(fr, bg=_BG2, height=150, highlightthickness=0)
            cv.pack(fill="x", padx=20, pady=(12, 4))
            cv.create_text(8, 6, text="⛔ 高危操作", font=("Segoe UI", 12, "bold"),
                           fill=_RED, anchor="nw")
            cv.create_text(8, 34, text=str(m.get("description", ""))[:120],
                           font=_FONT_T, fill=_FG, anchor="nw", width=430)
            cv.create_text(8, 74, text="请输入系统登录密码以确认执行:",
                           font=_FONT_T, fill=_DIM, anchor="nw")
            box = tk.Canvas(fr, bg=_BG, highlightthickness=1, highlightbackground=_RED,
                            height=38, takefocus=1)
            box.pack(padx=20, fill="x", pady=(0, 8))
            inp = CanvasInput(box, lambda t: None, placeholder="系统登录密码")
            inp._ph = box.create_text(14, 19, text=inp.placeholder, anchor="w",
                                      font=_FONT_T, fill=_DIM)
            inp._tx = box.create_text(14, 19, text="", anchor="w", font=_FONT, fill=_FG)
            ops = m.get("operation_id", "")
            btns_cv = tk.Canvas(fr, bg=_BG2, height=40, highlightthickness=0)
            btns_cv.pack(fill="x", pady=(0, 12))

            def submit(_=None):
                pwd = inp.get()
                if self.loop and not self.loop.is_closed():
                    self.loop.create_task(
                        self.core.handle_admin_auth(ops, pwd, self.emitter))
                top.destroy()

            def cancel():
                if self.loop and not self.loop.is_closed():
                    self.loop.create_task(
                        self.core.handle_admin_cancel(ops, self.emitter))
                top.destroy()
            box.bind("<KeyPress>", lambda e: (inp._on_key(e) if e.keysym != "Return" else submit()))
            HUDButton(btns_cv, 120, 6, 100, 28, "确认", fg=_RED, command=submit,
                      edge=_RED, bg=_BG)
            HUDButton(btns_cv, 240, 6, 100, 28, "取消", fg=_DIM, command=cancel,
                      edge=_EDGE, bg=_BG)
        except Exception:
            pass

    # ================= 主循环(单线程 pump) =================
    def run(self, loop: asyncio.AbstractEventLoop):
        """tkinter 主线程驱动 asyncio: 所有协程与 UI 同线程执行,
        X/GUI 访问全部单线程, 天然线程安全。"""
        self.loop = loop
        try:
            while True:
                self.root.update()
                loop.run_until_complete(asyncio.sleep(0.05))
        except tk.TclError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            try:
                loop.run_until_complete(asyncio.sleep(0))
            except Exception:
                pass

    def shutdown(self):
        try:
            self.hud.close_all()
            self.root.destroy()
        except Exception:
            pass
