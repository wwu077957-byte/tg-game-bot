"""✊ 石头剪刀布 /rps：私聊对战机器人；群里双人对战（出拳只有自己看得到）。"""
import random

from core import Game, S, kb, mention

CH = {"r": ("✊", "石头"), "s": ("✌️", "剪刀"), "p": ("✋", "布")}
BEATS = {"r": "s", "s": "p", "p": "r"}


def judge(a, b):
    """返回 1 表示 a 赢，-1 表示 b 赢，0 平局"""
    if a == b:
        return 0
    return 1 if BEATS[a] == b else -1


class RpsGame(Game):
    key = "rps"
    title = "石头剪刀布"
    COMMANDS = [("rps", "cmd_rps", "✊ 石头剪刀布（群里回复某人可挑战TA）")]
    TIMEOUT = 90

    def _choose_kb(self, prefix):
        return kb([[(f"{e} {n}", f"rps:{prefix}:{k}") for k, (e, n) in CH.items()]])

    def cmd_rps(self, req):
        chat = req.chat_id
        if not req.is_group:
            return self.reply(req, "✊✌️✋ 和我来一局吧，出拳！", self._choose_kb("solo"))
        s = self.sess(chat)
        if s:
            return self.reply(req, "本群已经有一局石头剪刀布正在进行，等它结束再来吧～")
        target = req.reply_user
        s = self.new_sess(chat, S(owner=req.uid, a=req.user, b=target, state="invite", choices={}, msg_id=None))
        if target:
            text = f"⚔️ {mention(req.user)} 向 {mention(target)} 发起石头剪刀布挑战！"
            markup = kb([[("⚔️ 接受", "rps:accept"), ("🏳️ 拒绝", "rps:decline")]])
        else:
            text = f"⚔️ {mention(req.user)} 发起了石头剪刀布挑战，谁来应战？\n（提示：回复某人的消息再发 /rps 可以指定对手）"
            markup = kb([[("⚔️ 接受挑战", "rps:accept"), ("❌ 取消", "rps:stop")]])
        m = self.app.send(chat, text, markup)
        s.msg_id = (m or {}).get("message_id")
        self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))

    def _status(self, s):
        def st(u):
            return "✅ 已出拳" if u["id"] in s.choices else "🤔 思考中"
        return (f"✊✌️✋ <b>石头剪刀布</b>\n{mention(s.a)}：{st(s.a)}\n{mention(s.b)}：{st(s.b)}\n\n"
                f"双方点击下方按钮出拳，出拳内容只有自己看得到。")

    def on_callback(self, cb, parts, req):
        act = parts[1] if len(parts) > 1 else ""
        chat, uid = req.chat_id, req.uid
        if act == "solo":
            c = parts[2] if len(parts) > 2 else ""
            if c not in CH:
                return
            b = random.choice(list(CH))
            r = judge(c, b)
            if r > 0:
                res = "🎉 你赢了！+1 娱乐分"
                self.points(chat, req.user, 1)
            elif r < 0:
                res = "😝 我赢啦！"
            else:
                res = "🤝 平局！"
            self.app.answer(cb)
            return self.app.edit(chat, cb["message"]["message_id"],
                                 f"你出了 {CH[c][0]}{CH[c][1]}，我出了 {CH[b][0]}{CH[b][1]}\n{res}\n\n再来一局？",
                                 self._choose_kb("solo"))
        s = self.sess(chat)
        if not s:
            return self.app.answer(cb, "这局已经结束了")
        if act == "accept":
            if s.state != "invite":
                return self.app.answer(cb, "对局已经开始了")
            if uid == s.a["id"]:
                return self.app.answer(cb, "不能和自己对战哦")
            if s.b and uid != s.b["id"]:
                return self.app.answer(cb, "这不是发给你的挑战", alert=True)
            s.b = req.user
            s.state = "play"
            self.app.edit(chat, s.msg_id, self._status(s), self._choose_kb("c"))
            self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))
            return self.app.answer(cb, "挑战开始！")
        if act == "decline":
            if s.b and uid == s.b["id"] and s.state == "invite":
                self.end(chat)
                self.app.edit(chat, s.msg_id, f"🏳️ {mention(s.b)} 拒绝了 {mention(s.a)} 的挑战。")
                return self.app.answer(cb, "已拒绝")
            return self.app.answer(cb, "只有被挑战者可以拒绝")
        if act == "stop":
            if uid == s.a["id"] or self.app.is_admin(chat, uid):
                self.end(chat)
                self.app.edit(chat, s.msg_id, "石头剪刀布挑战已取消。")
                return self.app.answer(cb, "已取消")
            return self.app.answer(cb, "只有发起人可以取消")
        if act == "c":
            c = parts[2] if len(parts) > 2 else ""
            if s.state != "play" or c not in CH:
                return self.app.answer(cb, "现在还不能出拳")
            if uid not in (s.a["id"], s.b["id"]):
                return self.app.answer(cb, "你不是这局的玩家，围观就好～")
            if uid in s.choices:
                return self.app.answer(cb, f"你已经出了 {CH[s.choices[uid]][0]}，不能反悔哦")
            s.choices[uid] = c
            self.app.answer(cb, f"你出了 {CH[c][0]}{CH[c][1]}（对手看不到）", alert=True)
            if len(s.choices) < 2:
                return self.app.edit(chat, s.msg_id, self._status(s), self._choose_kb("c"))
            return self._reveal(chat)

    def _reveal(self, chat):
        s = self.sess(chat)
        self.end(chat)
        ca, cb_ = s.choices[s.a["id"]], s.choices[s.b["id"]]
        r = judge(ca, cb_)
        text = (f"✊✌️✋ <b>结果揭晓</b>\n{mention(s.a)}：{CH[ca][0]} {CH[ca][1]}\n"
                f"{mention(s.b)}：{CH[cb_][0]} {CH[cb_][1]}\n\n")
        if r == 0:
            text += "🤝 平局！"
        else:
            w = s.a if r > 0 else s.b
            self.points(chat, w, 2)
            text += f"🏆 {mention(w)} 获胜！+2 娱乐分"
        self.app.edit(chat, s.msg_id, text)

    def _timeout(self, chat):
        s = self.sess(chat)
        if not s:
            return
        self.end(chat)
        if s.state == "invite":
            self.app.edit(chat, s.msg_id, "⏰ 没有人应战，挑战已取消。")
        else:
            self.app.edit(chat, s.msg_id, "⏰ 有人迟迟没出拳，本局作废。")

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.edit(chat_id, s.msg_id, "石头剪刀布已取消。")
