"""📜 成语接龙 /idiom：下一个成语的首字必须与上一个的尾字相同（同字）。"""
import random

from core import Game, S, esc, kb, mention
from data_idioms import IDIOMS


def is_cjk4(t):
    return len(t) == 4 and all("\u4e00" <= c <= "\u9fff" for c in t)


class IdiomGame(Game):
    key = "idiom"
    title = "成语接龙"
    COMMANDS = [("idiom", "cmd_idiom", "📜 成语接龙")]
    TIMEOUT = 60

    def __init__(self, app):
        super().__init__(app)
        self.words = set(IDIOMS)
        self.by_first = {}
        for w in IDIOMS:
            self.by_first.setdefault(w[0], []).append(w)

    def candidates(self, ch, used):
        return [w for w in self.by_first.get(ch, []) if w not in used]

    def _kb(self):
        return kb([[("💡 提示", "idiom:hint"), ("⏹ 结束", "idiom:stop")]])

    def cmd_idiom(self, req):
        chat = req.chat_id
        s = self.sess(chat)
        if s:
            return self.reply(req, f"📜 接龙进行中！当前：<b>{s.chain[-1]}</b>，请接以「{s.chain[-1][-1]}」开头的成语。")
        starts = [w for w in IDIOMS if len(self.by_first.get(w[-1], [])) >= 2]
        first = random.choice(starts)
        self.new_sess(chat, S(owner=req.uid, chain=[first], used={first}, scores={}, users={}, hints=0))
        self.reply(req, f"📜 <b>成语接龙开始！</b>\n起始成语：<b>{first}</b>\n\n"
                        f"请发送以「<b>{first[-1]}</b>」开头的四字成语（必须同一个字）。\n"
                        f"{self.TIMEOUT} 秒内没人接上，本轮结束。", self._kb())
        self.timer(chat, "timeout", self.TIMEOUT, lambda: self._finish(chat, "⏰ 时间到！"))

    def on_text(self, req):
        s = self.sess(req.chat_id)
        t = req.text.strip()
        if not s or not is_cjk4(t):
            return False
        need = s.chain[-1][-1]
        known = t in self.words
        if t[0] != need:
            if known:
                self.reply(req, f"「{t}」是成语，但要以「<b>{need}</b>」开头哦～")
                return True
            return False  # 普通聊天，不打扰
        if not known:
            self.reply(req, f"🤔「{esc(t)}」不在我的成语词库里，换一个试试？")
            return True
        if t in s.used:
            self.reply(req, f"「{t}」这一轮已经用过了，换一个吧！")
            return True
        s.chain.append(t)
        s.used.add(t)
        s.hints = 0
        uid = req.uid
        s.scores[uid] = s.scores.get(uid, 0) + 1
        s.users[uid] = req.user
        self.points(req.chat_id, req.user, 1)
        if not self.candidates(t[-1], s.used):
            self.points(req.chat_id, req.user, 2)
            self._finish(req.chat_id, f"🔒 {mention(req.user)} 接出「{t}」，词库里已经无词可接！额外 +2 分")
            return True
        self.reply(req, f"✅ {mention(req.user)} 接上「<b>{t}</b>」！+1 分（第 {len(s.chain) - 1} 个）\n"
                        f"下一个请以「<b>{t[-1]}</b>」开头，{self.TIMEOUT} 秒内有效。", self._kb())
        self.timer(req.chat_id, "timeout", self.TIMEOUT, lambda: self._finish(req.chat_id, "⏰ 时间到！"))
        return True

    def on_callback(self, cb, parts, req):
        act = parts[1] if len(parts) > 1 else ""
        chat = req.chat_id
        if act == "again":
            if self.sess(chat):
                return self.app.answer(cb, "已经在接龙中了")
            self.app.answer(cb)
            return self.cmd_idiom(req)
        s = self.sess(chat)
        if not s:
            return self.app.answer(cb, "本轮接龙已经结束了")
        if act == "hint":
            c = self.candidates(s.chain[-1][-1], s.used)
            if not c:
                return self.app.answer(cb, "没有可接的成语了")
            if s.hints >= 2:
                return self.app.answer(cb, "这一步的提示已经用完啦", alert=True)
            s.hints += 1
            w = random.choice(c)
            self.app.answer(cb)
            return self.app.send(chat, f"💡 提示：<b>{w[:2]}＿＿</b>（{len(c)} 个可选）")
        if act == "stop":
            if not self.app.can_manage(chat, s, req.uid):
                return self.app.answer(cb, "只有发起人或管理员可以结束", alert=True)
            self.app.answer(cb, "已结束")
            return self._finish(chat, f"⏹ {mention(req.user)} 结束了本轮接龙。")

    def _finish(self, chat, why):
        s = self.sess(chat)
        if not s:
            return
        self.end(chat)
        n = len(s.chain) - 1
        text = f"{why}\n📜 本轮共接龙 <b>{n}</b> 个：\n" + " → ".join(s.chain[-15:])
        c = self.candidates(s.chain[-1][-1], s.used)
        if c and why.startswith(("⏰", "⏹")):
            text += f"\n\n💡 其实还可以接：<b>{random.choice(c)}</b>"
        if s.scores:
            rank = sorted(s.scores.items(), key=lambda x: -x[1])
            text += "\n\n🏅 本轮得分：\n" + "\n".join(f"{mention(s.users[u])}：{p}" for u, p in rank)
        self.app.send(chat, text, kb([[("🔁 再来一轮", "idiom:again")]]))

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.send(chat_id, f"📜 成语接龙已取消，本轮共接 {len(s.chain) - 1} 个。")
