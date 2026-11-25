"""🔢 猜数字 /guess（1~100）"""
import random

from core import Game, S, kb, mention


class GuessGame(Game):
    key = "guess"
    title = "猜数字"
    COMMANDS = [("guess", "cmd_guess", "🔢 猜 1~100 的数字")]
    TIMEOUT = 300  # 无人猜测多久后结束

    def cmd_guess(self, req):
        chat = req.chat_id
        s = self.sess(chat)
        if s:
            return self.reply(req, f"🔢 本轮还在进行中！数字在 <b>{s.lo}~{s.hi}</b> 之间，直接发数字来猜。")
        n = random.randint(1, 100)
        self.new_sess(chat, S(owner=req.uid, n=n, lo=1, hi=100, tries=0, players=set()))
        who = "群里所有人都可以猜" if req.is_group else "直接发送数字"
        self.reply(req, f"🔢 <b>猜数字</b>\n我想好了一个 <b>1~100</b> 之间的整数，{who}！\n"
                        f"{self.TIMEOUT // 60} 分钟内没人猜就公布答案。")
        self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))

    def on_text(self, req):
        s = self.sess(req.chat_id)
        t = req.text.strip()
        if not s or not (t.isascii() and t.isdigit()) or len(t) > 4:
            return False
        v = int(t)
        if not 1 <= v <= 100:
            self.reply(req, "请输入 1~100 之间的数字哦～")
            return True
        s.tries += 1
        s.players.add(req.uid)
        self.timer(req.chat_id, "timeout", self.TIMEOUT, lambda: self._timeout(req.chat_id))
        if v < s.n:
            s.lo = max(s.lo, v + 1)
            self.reply(req, f"📈 {v} 太小了！范围：<b>{s.lo}~{s.hi}</b>")
        elif v > s.n:
            s.hi = min(s.hi, v - 1)
            self.reply(req, f"📉 {v} 太大了！范围：<b>{s.lo}~{s.hi}</b>")
        else:
            pts = 5 if s.tries <= 5 else 3 if s.tries <= 10 else 1
            self.end(req.chat_id)
            self.points(req.chat_id, req.user, pts)
            self.reply(req, f"🎉 {mention(req.user)} 猜中了！答案就是 <b>{s.n}</b>\n"
                            f"本轮共猜了 {s.tries} 次，+{pts} 娱乐分",
                       kb([[("🔁 再来一局", "guess:again")]]))
        return True

    def _timeout(self, chat):
        s = self.sess(chat)
        if s:
            self.end(chat)
            self.app.send(chat, f"⏰ 太久没人猜啦，答案是 <b>{s.n}</b>。", kb([[("🔁 再来一局", "guess:again")]]))

    def on_callback(self, cb, parts, req):
        if parts[1:2] == ["again"]:
            self.app.answer(cb)
            self.cmd_guess(req)

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.send(chat_id, f"猜数字已取消，答案是 <b>{s.n}</b>。")
