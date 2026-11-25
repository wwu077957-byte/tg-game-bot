"""核心：应用对象、定时调度器、请求封装、游戏基类与通用工具。"""
import html
import logging
import threading
import time
from types import SimpleNamespace as S  # noqa: F401  (供各游戏模块使用)

from tg import NetworkError, TelegramError

log = logging.getLogger("gamebot")

GROUP_TYPES = ("group", "supergroup")


# ---------------------------------------------------------------- 工具函数
def esc(s):
    return html.escape(str(s), quote=False)


def uname(u):
    if not u:
        return "?"
    n = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
    return (n or u.get("username") or str(u.get("id")))[:40]


def mention(u):
    return f'<a href="tg://user?id={int(u["id"])}">{esc(uname(u))}</a>'


def kb(rows):
    """rows: [[(文字, 回调数据或URL), ...], ...] -> InlineKeyboardMarkup"""
    out = []
    for row in rows:
        r = []
        for text, data in row:
            if str(data).startswith(("http://", "https://", "tg://")):
                r.append({"text": text, "url": data})
            else:
                r.append({"text": text, "callback_data": str(data)[:64]})
        if r:
            out.append(r)
    return {"inline_keyboard": out}


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


# ---------------------------------------------------------------- 调度器
class Scheduler:
    """简单的定时任务表。由后台线程周期调用 run_due（测试中手动调用）。"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.jobs = {}
        self._seq = 0

    def set(self, key, delay, func):
        self._seq += 1
        self.jobs[key] = (self.clock() + delay, self._seq, func)

    def cancel(self, key):
        self.jobs.pop(key, None)

    def cancel_prefix(self, prefix):
        for k in [k for k in self.jobs if k.startswith(prefix)]:
            del self.jobs[k]

    def has(self, key):
        return key in self.jobs

    def pop_due(self):
        now = self.clock()
        due = sorted(
            ((w, s, k, f) for k, (w, s, f) in self.jobs.items() if w <= now),
            key=lambda x: (x[0], x[1]),
        )
        out = []
        for w, s, k, f in due:
            cur = self.jobs.get(k)
            if cur and cur[1] == s:
                del self.jobs[k]
                out.append((k, f))
        return out


# ---------------------------------------------------------------- 请求
class Req:
    def __init__(self, chat, user, args="", msg=None):
        self.chat = chat or {}
        self.chat_id = self.chat.get("id")
        self.chat_type = self.chat.get("type", "private")
        self.user = user
        self.uid = user["id"] if user else None
        self.args = args or ""
        self.msg = msg

    @property
    def is_group(self):
        return self.chat_type in GROUP_TYPES

    @property
    def message_id(self):
        return self.msg.get("message_id") if self.msg else None

    @property
    def text(self):
        return (self.msg or {}).get("text") or ""

    @property
    def reply_user(self):
        r = (self.msg or {}).get("reply_to_message") or {}
        u = r.get("from")
        if u and not u.get("is_bot") and u.get("id") != self.uid:
            return u
        return None


# ---------------------------------------------------------------- 游戏基类
class Game:
    key = ""
    title = ""
    COMMANDS = []  # [(命令, 方法名, 描述)]

    def __init__(self, app):
        self.app = app

    # 子类可覆盖
    def on_callback(self, cb, parts, req):
        self.app.answer(cb)

    def on_text(self, req):
        return False

    def on_cancel(self, chat_id, sess, by_user=None):
        """会话被 /cancel 或 /endgame 结束前调用，用于发送提示。"""

    # 辅助
    def sess(self, chat_id):
        return self.app.sessions.get((chat_id, self.key))

    def new_sess(self, chat_id, sess):
        self.app.sessions[(chat_id, self.key)] = sess
        return sess

    def end(self, chat_id):
        self.app.end_session(chat_id, self.key)

    def timer(self, chat_id, name, delay, fn):
        self.app.sched.set(f"{chat_id}:{self.key}:{name}", delay, fn)

    def untimer(self, chat_id, name):
        self.app.sched.cancel(f"{chat_id}:{self.key}:{name}")

    def reply(self, req, text, markup=None):
        return self.app.reply(req, text, markup)

    def points(self, chat_id, user, pts):
        if pts:
            self.app.db.add_points(chat_id, user["id"], uname(user), self.key, pts)


# ---------------------------------------------------------------- 应用
MENU = [
    ("🎲 掷骰子", "dice", ""), ("🎲 骰子比大小", "dice", "比大小"),
    ("🔮 抽签", "draw", ""), ("🎁 群抽奖", "lottery", ""),
    ("🐺 狼人杀", "werewolf", ""), ("🕵️ 谁是卧底", "undercover", ""),
    ("🧠 答题", "quiz", ""), ("🔢 猜数字", "guess", ""),
    ("📜 成语接龙", "idiom", ""), ("✊ 石头剪刀布", "rps", ""),
    ("🎭 真心话大冒险", "tod", ""), ("🃏 21点", "blackjack", ""),
    ("⭕ 井字棋", "ttt", ""), ("🏆 积分榜", "rank", ""),
]

HELP_TEXT = """🎮 <b>游戏机器人 · 帮助</b>
（纯娱乐！积分只是好玩，不涉及任何金钱、下注或兑换）

🎲 /dice — 掷骰子；<code>/dice 2d6</code> 掷多个；<code>/dice 比大小</code> 群内比大小
🔮 /draw — 抽一支签
🤔 /pick 选项1 选项2 … — 帮你随机选一个
🎁 /lottery [中奖人数] [标题] — 群抽奖（大家点“参加”，发起人点“开奖”）
🐺 /werewolf — 狼人杀（群聊 5~12 人，需先私聊机器人 /start）
🕵️ /undercover — 谁是卧底（群聊 4~10 人，需先私聊机器人 /start）
🧠 /quiz — 知识问答（群里抢答，私聊连对模式）；/quizrank 答题榜
🔢 /guess — 猜 1~100 的数字
📜 /idiom — 成语接龙
✊ /rps — 石头剪刀布（私聊对战机器人；群里回复某人消息发起挑战）
🎭 /tod — 真心话大冒险
🃏 /blackjack — 21点（和庄家比，赢了加娱乐分）
⭕ /ttt — 井字棋（私聊对战机器人；群里双人对战）
🏆 /rank — 本群娱乐积分榜
❌ /cancel — 取消本聊天中进行中的游戏
🛑 /endgame — 结束狼人杀 / 谁是卧底（发起人或管理员）

在群里也可以使用 <code>/命令@机器人用户名</code> 的形式。"""


class App:
    def __init__(self, api, db, clock=time.time):
        from all_games import ALL_GAMES

        self.api = api
        self.db = db
        self.clock = clock
        self.sched = Scheduler(clock)
        self.lock = threading.RLock()
        self.username = "GameBot"
        self.bot_id = 0
        self.sessions = {}
        self.games = [G(self) for G in ALL_GAMES]
        self.by_key = {g.key: g for g in self.games}
        self.commands = {}
        self.cmd_desc = []
        core_cmds = [
            ("start", self.cmd_start, "开始 / 游戏菜单"),
            ("help", self.cmd_help, "帮助与全部玩法"),
        ]
        for name, fn, desc in core_cmds:
            self.commands[name] = fn
            self.cmd_desc.append((name, desc))
        for g in self.games:
            for cmd, meth, desc in g.COMMANDS:
                self.commands[cmd] = getattr(g, meth)
                self.cmd_desc.append((cmd, desc))
        for name, fn, desc in [
            ("rank", self.cmd_rank, "本群娱乐积分榜"),
            ("cancel", self.cmd_cancel, "取消当前聊天中的游戏"),
            ("endgame", self.cmd_endgame, "结束狼人杀/谁是卧底"),
        ]:
            self.commands[name] = fn
            self.cmd_desc.append((name, desc))

    # ------------------------------------------------ 启动
    def setup(self):
        me = self.api.call("getMe")
        self.username = me.get("username") or self.username
        self.bot_id = me.get("id", 0)
        self.api.call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in self.cmd_desc]},
        )
        log.info("机器人 @%s 已就绪，共 %d 个命令", self.username, len(self.cmd_desc))

    def start_link(self, payload="go"):
        return f"https://t.me/{self.username}?start={payload}"

    # ------------------------------------------------ 发送
    def send(self, chat_id, text, markup=None, reply_to=None):
        p = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if markup:
            p["reply_markup"] = markup
        if reply_to:
            p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return self.api.call("sendMessage", p)
        except TelegramError as e:
            log.warning("发送到 %s 失败：%s", chat_id, e)
            if chat_id and chat_id > 0 and e.code in (400, 403):
                self.db.set_started(chat_id, False)
            return None
        except NetworkError as e:
            log.warning("发送到 %s 网络错误：%s", chat_id, e)
            return None

    def reply(self, req, text, markup=None):
        return self.send(req.chat_id, text, markup, req.message_id)

    def send_private(self, user_id, text, markup=None):
        return self.send(user_id, text, markup)

    def edit(self, chat_id, msg_id, text, markup=None):
        if not msg_id:
            return None
        p = {
            "chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if markup:
            p["reply_markup"] = markup
        try:
            return self.api.call("editMessageText", p)
        except TelegramError as e:
            if "not modified" not in e.description:
                log.warning("编辑消息失败：%s", e)
        except NetworkError as e:
            log.warning("编辑消息网络错误：%s", e)
        return None

    def send_dice(self, chat_id, emoji="🎲", reply_to=None):
        p = {"chat_id": chat_id, "emoji": emoji}
        if reply_to:
            p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return self.api.call("sendDice", p)
        except (TelegramError, NetworkError) as e:
            log.warning("sendDice 失败：%s", e)
            return None

    def answer(self, cb, text=None, alert=False):
        if cb.get("_answered"):
            return
        cb["_answered"] = True
        p = {"callback_query_id": cb["id"]}
        if text:
            p["text"] = text[:200]
            p["show_alert"] = bool(alert)
        try:
            self.api.call("answerCallbackQuery", p)
        except (TelegramError, NetworkError) as e:
            log.debug("answerCallbackQuery 失败：%s", e)

    def is_admin(self, chat_id, user_id):
        if not chat_id or chat_id > 0:
            return True
        try:
            m = self.api.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
            return (m or {}).get("status") in ("creator", "administrator")
        except (TelegramError, NetworkError):
            return False

    def can_manage(self, chat_id, sess, user_id):
        if getattr(sess, "owner", None) == user_id:
            return True
        return self.is_admin(chat_id, user_id)

    # ------------------------------------------------ 会话 / 计时
    def end_session(self, chat_id, key):
        self.sessions.pop((chat_id, key), None)
        self.sched.cancel_prefix(f"{chat_id}:{key}:")

    def tick(self):
        with self.lock:
            for key, fn in self.sched.pop_due():
                try:
                    fn()
                except Exception:
                    log.exception("定时任务 %s 出错", key)

    # ------------------------------------------------ 更新分发
    def process(self, update):
        with self.lock:
            try:
                if "message" in update:
                    self.on_message(update["message"])
                elif "callback_query" in update:
                    self.on_callback(update["callback_query"])
            except Exception:
                log.exception("处理更新 %s 时出错", update.get("update_id"))

    def on_message(self, msg):
        user = msg.get("from")
        chat = msg.get("chat") or {}
        if not user or user.get("is_bot"):
            return
        private = chat.get("type") == "private"
        self.db.touch_user(user["id"], uname(user), started=True if private else None)
        text = msg.get("text")
        if not text:
            return
        if text.startswith("/"):
            parts = text.split(None, 1)
            head = parts[0][1:]
            args = parts[1].strip() if len(parts) > 1 else ""
            if "@" in head:
                head, at = head.split("@", 1)
                if at.lower() != (self.username or "").lower():
                    return  # 发给其他机器人的命令
            fn = self.commands.get(head.lower())
            if fn:
                fn(Req(chat, user, args, msg))
            elif private:
                self.send(chat["id"], "没有这个命令哦，发送 /help 查看全部玩法。")
            return
        req = Req(chat, user, "", msg)
        for g in self.games:
            if (chat.get("id"), g.key) in self.sessions and g.on_text(req):
                return

    def on_callback(self, cb):
        user = cb.get("from") or {}
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {"id": user.get("id"), "type": "private"}
        req = Req(chat, user, "", None)
        try:
            if user.get("is_bot"):
                return
            parts = data.split(":")
            if parts[0] == "menu":
                self.on_menu(cb, parts, req)
            else:
                g = self.by_key.get(parts[0])
                if g:
                    g.on_callback(cb, parts, req)
        finally:
            self.answer(cb)

    def on_menu(self, cb, parts, req):
        try:
            name, cmd, args = MENU[int(parts[1])]
        except (IndexError, ValueError):
            return
        self.answer(cb, name)
        req.args = args
        self.commands[cmd](req)

    # ------------------------------------------------ 通用命令
    def menu_kb(self):
        btns = [(name, f"menu:{i}") for i, (name, _c, _a) in enumerate(MENU)]
        return kb(chunk(btns, 2))

    def cmd_start(self, req):
        if not req.is_group:
            self.db.set_started(req.uid, True)
        extra = ""
        if not req.is_group and req.args in ("ww", "uc", "join"):
            extra = "\n\n✅ 私聊已开通！现在可以回到群里点击「加入」参加游戏了。"
        text = (
            f"👋 你好，{mention(req.user)}！我是游戏机器人，下面的游戏都可以玩～\n"
            "（纯娱乐，积分只是好玩，不涉及任何金钱或兑换）\n\n"
            "点击按钮开始，或发送 /help 查看全部命令。" + extra
        )
        self.reply(req, text, self.menu_kb())

    def cmd_help(self, req):
        self.reply(req, HELP_TEXT, self.menu_kb())

    def cmd_rank(self, req):
        rows = self.db.top(req.chat_id)
        if not rows:
            return self.reply(req, "🏆 还没有人得分，快来玩游戏吧！")
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(name)} — {p} 分"
            for i, (_uid, name, p) in enumerate(rows)
        ]
        where = "本群" if req.is_group else "你的"
        self.reply(req, f"🏆 <b>{where}娱乐积分榜</b>（仅供娱乐）\n\n" + "\n".join(lines))

    def cmd_cancel(self, req):
        found = [(k, s) for (c, k), s in list(self.sessions.items()) if c == req.chat_id]
        if not found:
            return self.reply(req, "当前没有进行中的游戏。")
        ended, denied = [], []
        for key, s in found:
            g = self.by_key[key]
            if self.can_manage(req.chat_id, s, req.uid):
                try:
                    g.on_cancel(req.chat_id, s, req.user)
                finally:
                    self.end_session(req.chat_id, key)
                ended.append(g.title)
            else:
                denied.append(g.title)
        text = ""
        if ended:
            text += "❌ 已取消：" + "、".join(ended)
        if denied:
            text += ("\n" if text else "") + "⚠️ 只有发起人或群管理员可以取消：" + "、".join(denied)
        self.reply(req, text)

    def cmd_endgame(self, req):
        if not req.is_group:
            return self.reply(req, "/endgame 用于在群里结束狼人杀或谁是卧底。")
        found = False
        for key in ("ww", "uc"):
            s = self.sessions.get((req.chat_id, key))
            if not s:
                continue
            found = True
            g = self.by_key[key]
            if self.can_manage(req.chat_id, s, req.uid):
                try:
                    g.on_cancel(req.chat_id, s, req.user)
                finally:
                    self.end_session(req.chat_id, key)
            else:
                self.reply(req, f"⚠️ 只有发起人或群管理员可以结束{g.title}。")
        if not found:
            self.reply(req, "当前没有进行中的狼人杀或谁是卧底。")
