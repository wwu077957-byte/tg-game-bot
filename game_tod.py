"""🎭 真心话大冒险 /tod"""
import random

from core import Game, esc, kb, mention
from data_tod import DARES, TRUTHS


class TodGame(Game):
    key = "tod"
    title = "真心话大冒险"
    COMMANDS = [("tod", "cmd_tod", "🎭 真心话大冒险")]

    def _kb(self):
        return kb([[("💬 真心话", "tod:t"), ("🔥 大冒险", "tod:d"), ("🎲 随机", "tod:r")]])

    def _prompt(self, user, kind):
        if kind == "r":
            kind = random.choice("td")
        if kind == "t":
            return f"💬 {mention(user)} 抽到【真心话】：\n\n<b>{esc(random.choice(TRUTHS))}</b>"
        return f"🔥 {mention(user)} 抽到【大冒险】：\n\n<b>{esc(random.choice(DARES))}</b>"

    def cmd_tod(self, req):
        a = req.args.strip()
        kind = {"真心话": "t", "truth": "t", "大冒险": "d", "dare": "d", "随机": "r"}.get(a)
        if kind:
            return self.reply(req, self._prompt(req.user, kind), self._kb())
        self.reply(req, "🎭 <b>真心话大冒险</b>\n选一个吧！（内容友好安全，可以随时换一个）", self._kb())

    def on_callback(self, cb, parts, req):
        kind = parts[1] if len(parts) > 1 else "r"
        if kind not in ("t", "d", "r"):
            kind = "r"
        self.app.answer(cb)
        self.app.send(req.chat_id, self._prompt(req.user, kind), self._kb())
