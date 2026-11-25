"""⭕ 井字棋 /ttt：私聊对战机器人；群里双人对战。"""
import random

from core import Game, S, kb, mention

LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
MARK = {"X": "❌", "O": "⭕", " ": "⬜"}


def winner(b):
    for a, c, d in LINES:
        if b[a] != " " and b[a] == b[c] == b[d]:
            return b[a]
    return "draw" if " " not in b else None


def minimax(b, me, turn):
    w = winner(b)
    if w == me:
        return 1, None
    if w == "draw":
        return 0, None
    if w:
        return -1, None
    other = "O" if turn == "X" else "X"
    best = None
    for i in range(9):
        if b[i] != " ":
            continue
        b[i] = turn
        score, _ = minimax(b, me, other)
        b[i] = " "
        if best is None or (turn == me and score > best[0]) or (turn != me and score < best[0]):
            best = (score, i)
    return best


def bot_move(b, me="O", mistake=0.25):
    empty = [i for i in range(9) if b[i] == " "]
    if random.random() < mistake:
        return random.choice(empty)
    return minimax(list(b), me, me)[1]


class TttGame(Game):
    key = "ttt"
    title = "井字棋"
    COMMANDS = [("ttt", "cmd_ttt", "⭕ 井字棋（群里回复某人可挑战TA）")]
    TIMEOUT = 120

    def cmd_ttt(self, req):
        chat = req.chat_id
        if self.sess(chat):
            return self.reply(req, "⭕ 本聊天已经有一盘井字棋在进行中了（/cancel 可取消）。")
        if not req.is_group:
            s = self.new_sess(chat, S(owner=req.uid, mode="bot", x=req.user, o=None,
                                      board=[" "] * 9, turn="X", state="play", msg_id=None))
            m = self.app.send(chat, self._text(s), self._board_kb(s))
            s.msg_id = (m or {}).get("message_id")
            self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))
            return
        target = req.reply_user
        s = self.new_sess(chat, S(owner=req.uid, mode="pvp", x=req.user, o=target,
                                  board=[" "] * 9, turn="X", state="invite", msg_id=None))
        if target:
            text = f"⭕ {mention(req.user)} 邀请 {mention(target)} 下一盘井字棋！"
            markup = kb([[("✅ 接受", "ttt:accept"), ("🏳️ 拒绝", "ttt:decline")]])
        else:
            text = f"⭕ {mention(req.user)} 想下一盘井字棋，谁来应战？\n（回复某人的消息再发 /ttt 可以指定对手）"
            markup = kb([[("✅ 我来", "ttt:accept"), ("❌ 取消", "ttt:stop")]])
        m = self.app.send(chat, text, markup)
        s.msg_id = (m or {}).get("message_id")
        self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))

    def _name(self, s, side):
        if side == "O" and s.mode == "bot":
            return "🤖 机器人"
        return mention(s.x if side == "X" else s.o)

    def _text(self, s, result=""):
        t = f"⭕ <b>井字棋</b>\n❌ {self._name(s, 'X')}  vs  ⭕ {self._name(s, 'O')}\n\n"
        return t + (result or f"轮到 {MARK[s.turn]} {self._name(s, s.turn)} 落子")

    def _board_kb(self, s, final=False):
        rows = []
        for r in range(3):
            rows.append([(MARK[s.board[i]], f"ttt:m:{i}" if not final else "ttt:noop")
                         for i in range(r * 3, r * 3 + 3)])
        if not final:
            rows.append([("🏳️ 认输", "ttt:quit")])
        else:
            rows.append([("🔁 再来一盘", "ttt:again")])
        return kb(rows)

    def on_callback(self, cb, parts, req):
        act = parts[1] if len(parts) > 1 else ""
        chat, uid = req.chat_id, req.uid
        if act == "again":
            if self.sess(chat):
                return self.app.answer(cb, "已经有一盘在进行中了")
            self.app.answer(cb)
            return self.cmd_ttt(req)
        s = self.sess(chat)
        if not s:
            return self.app.answer(cb, "这盘已经结束了")
        if act == "accept":
            if s.state != "invite":
                return self.app.answer(cb, "对局已经开始了")
            if uid == s.x["id"]:
                return self.app.answer(cb, "不能和自己下哦")
            if s.o and uid != s.o["id"]:
                return self.app.answer(cb, "这不是发给你的邀请", alert=True)
            s.o, s.state = req.user, "play"
            self.app.edit(chat, s.msg_id, self._text(s), self._board_kb(s))
            self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))
            return self.app.answer(cb, "开始！❌ 先手")
        if act == "decline":
            if s.state == "invite" and s.o and uid == s.o["id"]:
                self.end(chat)
                self.app.edit(chat, s.msg_id, f"🏳️ {mention(s.o)} 拒绝了井字棋邀请。")
                return self.app.answer(cb, "已拒绝")
            return self.app.answer(cb, "只有被邀请者可以拒绝")
        if act == "stop":
            if uid == s.x["id"] or self.app.is_admin(chat, uid):
                self.end(chat)
                self.app.edit(chat, s.msg_id, "井字棋邀请已取消。")
                return self.app.answer(cb, "已取消")
            return self.app.answer(cb, "只有发起人可以取消")
        players = [s.x["id"]] + ([s.o["id"]] if s.o and s.mode == "pvp" else [])
        if act == "quit":
            if s.state != "play" or uid not in players:
                return self.app.answer(cb, "你不在这盘棋里")
            loser = "X" if uid == s.x["id"] else "O"
            return self._over(chat, "O" if loser == "X" else "X", f"🏳️ {self._name(s, loser)} 认输了。")
        if act == "m":
            if s.state != "play":
                return self.app.answer(cb, "对局还没开始")
            if uid not in players:
                return self.app.answer(cb, "你不在这盘棋里，围观就好～")
            cur = s.x if s.turn == "X" else s.o
            if s.mode == "bot" and s.turn != "X" or (cur and cur["id"] != uid):
                return self.app.answer(cb, "还没轮到你哦")
            try:
                i = int(parts[2])
            except (IndexError, ValueError):
                return
            if not 0 <= i <= 8 or s.board[i] != " ":
                return self.app.answer(cb, "这里已经有子了")
            s.board[i] = s.turn
            self.app.answer(cb)
            if self._check(chat):
                return
            s.turn = "O" if s.turn == "X" else "X"
            if s.mode == "bot":
                s.board[bot_move(s.board)] = "O"
                if self._check(chat):
                    return
                s.turn = "X"
            self.app.edit(chat, s.msg_id, self._text(s), self._board_kb(s))
            self.timer(chat, "timeout", self.TIMEOUT, lambda: self._timeout(chat))

    def _check(self, chat):
        s = self.sess(chat)
        w = winner(s.board)
        if not w:
            return False
        if w == "draw":
            self._over(chat, None, "🤝 平局！")
        else:
            self._over(chat, w, f"🏆 {MARK[w]} {self._name(s, w)} 获胜！")
        return True

    def _over(self, chat, w, text):
        s = self.sess(chat)
        self.end(chat)
        if s.mode == "pvp":
            if w:
                self.points(chat, s.x if w == "X" else s.o, 3)
                text += " +3 娱乐分"
            else:
                self.points(chat, s.x, 1)
                self.points(chat, s.o, 1)
                text += " 双方各 +1 娱乐分"
        elif w == "X":
            self.points(chat, s.x, 3)
            text += " +3 娱乐分"
        elif w is None:
            self.points(chat, s.x, 1)
            text += " +1 娱乐分"
        self.app.edit(chat, s.msg_id, self._text(s, text), self._board_kb(s, final=True))

    def _timeout(self, chat):
        s = self.sess(chat)
        if not s:
            return
        self.end(chat)
        if s.state == "invite":
            self.app.edit(chat, s.msg_id, "⏰ 没有人应战，井字棋邀请已取消。")
        else:
            self.app.edit(chat, s.msg_id, self._text(s, "⏰ 太久没落子，本盘结束。"), self._board_kb(s, final=True))

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.edit(chat_id, s.msg_id, "井字棋已取消。")
