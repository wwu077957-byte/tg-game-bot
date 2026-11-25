"""🔮 抽签 /draw、🤔 随机选择 /pick、🎁 群抽奖 /lottery"""
import random
import re

from core import Game, S, esc, kb, mention
from data_fortunes import FORTUNES


class DrawGame(Game):
    key = "draw"
    title = "群抽奖"
    COMMANDS = [
        ("draw", "cmd_draw", "🔮 抽一支签"),
        ("pick", "cmd_pick", "🤔 随机帮你选：/pick 火锅 烧烤 日料"),
        ("lottery", "cmd_lottery", "🎁 群抽奖：/lottery [人数] [标题]"),
    ]
    LOTTERY_TIMEOUT = 1800
    MAX_SHOW = 30

    # ------------------------------------------- 抽签
    def _fortune_text(self, user):
        level, poem, note = random.choice(FORTUNES)
        return (f"🔮 {mention(user)} 抽到了 —— <b>【{level}】</b>\n\n"
                f"<i>{esc(poem)}</i>\n\n💬 {esc(note)}\n\n（仅供娱乐）")

    def cmd_draw(self, req):
        self.reply(req, self._fortune_text(req.user), kb([[("🔮 我也抽一支", "draw:again")]]))

    # ------------------------------------------- 随机选
    def cmd_pick(self, req):
        opts = [o for o in re.split(r"[\s,，、;；|/]+", req.args) if o]
        if len(opts) < 2:
            return self.reply(req, "🤔 用法：/pick 选项1 选项2 选项3 …\n例如：<code>/pick 火锅 烧烤 日料</code>")
        choice = random.choice(opts)
        self.reply(req, f"🤔 在 {len(opts)} 个选项里，我帮你选了：\n\n👉 <b>{esc(choice)}</b>")

    # ------------------------------------------- 群抽奖
    def cmd_lottery(self, req):
        if not req.is_group:
            return self.reply(req, "🎁 抽奖需要在群里玩：把我拉进群，然后发送 /lottery [中奖人数] [标题]。")
        chat = req.chat_id
        if self.sess(chat):
            return self.reply(req, "本群已经有一个抽奖在进行中，点「参加」加入，或由发起人「开奖」。")
        n, title = 1, ""
        toks = req.args.split(None, 1)
        if toks and toks[0].isdigit():
            n = int(toks[0])
            title = toks[1] if len(toks) > 1 else ""
        else:
            title = req.args
        n = max(1, min(n, 20))
        title = (title or "幸运抽奖").strip()[:60]
        s = self.new_sess(chat, S(owner=req.uid, host=req.user, title=title, n=n, players={}, msg_id=None))
        m = self.app.send(chat, self._lot_text(s), self._lot_kb())
        s.msg_id = (m or {}).get("message_id")
        self.timer(chat, "timeout", self.LOTTERY_TIMEOUT, lambda: self._lot_timeout(chat))

    def _lot_text(self, s):
        names = list(s.players.values())
        shown = "\n".join(f"· {mention(u)}" for u in names[:self.MAX_SHOW])
        more = f"\n… 等共 {len(names)} 人" if len(names) > self.MAX_SHOW else ""
        return (f"🎁 <b>{esc(s.title)}</b>\n发起人：{mention(s.host)}\n中奖名额：{s.n} 人\n"
                f"（机器人只负责随机抽人，不发放任何奖品）\n\n"
                f"已参加 {len(names)} 人：\n{shown or '（暂无）'}{more}")

    def _lot_kb(self):
        return kb([[("🙋 参加", "draw:join"), ("🎉 开奖", "draw:go")], [("❌ 取消抽奖", "draw:stop")]])

    def on_callback(self, cb, parts, req):
        act = parts[1] if len(parts) > 1 else ""
        chat, uid = req.chat_id, req.uid
        if act == "again":
            self.app.answer(cb)
            return self.app.send(chat, self._fortune_text(req.user), kb([[("🔮 我也抽一支", "draw:again")]]))
        s = self.sess(chat)
        if not s:
            return self.app.answer(cb, "这个抽奖已经结束了")
        if act == "join":
            if uid in s.players:
                return self.app.answer(cb, "你已经参加了，等待开奖吧～")
            s.players[uid] = req.user
            self.app.edit(chat, s.msg_id, self._lot_text(s), self._lot_kb())
            return self.app.answer(cb, "参加成功！祝你好运 🍀")
        if not self.app.can_manage(chat, s, uid):
            return self.app.answer(cb, "只有发起人或管理员可以操作", alert=True)
        if act == "go":
            if not s.players:
                return self.app.answer(cb, "还没有人参加呢", alert=True)
            self.end(chat)
            k = min(s.n, len(s.players))
            winners = random.sample(list(s.players.values()), k)
            self.app.edit(chat, s.msg_id, self._lot_text(s) + "\n\n✅ 已开奖")
            self.app.send(chat, f"🎉 <b>{esc(s.title)}</b> 开奖啦！\n共 {len(s.players)} 人参加，抽出 {k} 位：\n\n"
                          + "\n".join(f"🏆 {mention(w)}" for w in winners), reply_to=s.msg_id)
            return self.app.answer(cb, "已开奖")
        if act == "stop":
            self.end(chat)
            self.app.edit(chat, s.msg_id, f"🎁 抽奖「{esc(s.title)}」已取消。")
            return self.app.answer(cb, "已取消")

    def _lot_timeout(self, chat):
        s = self.sess(chat)
        if s:
            self.end(chat)
            self.app.edit(chat, s.msg_id, f"⏰ 抽奖「{esc(s.title)}」超过 30 分钟未开奖，已自动取消。")

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.edit(chat_id, s.msg_id, f"🎁 抽奖「{esc(s.title)}」已取消。")
