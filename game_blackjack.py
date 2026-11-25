"""🃏 21点 /blackjack：和庄家比大小，赢了加娱乐分（不涉及任何下注）。"""
import random

from core import Game, S, kb, mention

SUITS = "♠♥♦♣"
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck():
    d = [r + s for s in SUITS for r in RANKS]
    random.shuffle(d)
    return d


def hand_value(hand):
    total, aces = 0, 0
    for c in hand:
        r = c[:-1]
        if r == "A":
            aces += 1
            total += 11
        elif r in ("J", "Q", "K"):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand):
    return len(hand) == 2 and hand_value(hand) == 21


class BlackjackGame(Game):
    key = "bj"
    title = "21点"
    COMMANDS = [("blackjack", "cmd_bj", "🃏 21点（和庄家比，纯娱乐）")]
    TIMEOUT = 120

    def cmd_bj(self, req):
        chat = req.chat_id
        s = self.sess(chat)
        if s:
            return self.reply(req, f"🃏 {mention(s.user)} 正在玩 21 点，等这局结束再开新局吧～")
        self.start(chat, req.user)

    def start(self, chat, user):
        deck = new_deck()
        s = self.new_sess(chat, S(owner=user["id"], user=user, deck=deck,
                                  p=[deck.pop(), deck.pop()], d=[deck.pop(), deck.pop()], msg_id=None))
        if is_blackjack(s.p) or is_blackjack(s.d):
            m = self.app.send(chat, self._table(s, hide=False))
            s.msg_id = (m or {}).get("message_id")
            return self._finish(chat)
        m = self.app.send(chat, self._table(s, hide=True), self._kb())
        s.msg_id = (m or {}).get("message_id")
        self.timer(chat, "timeout", self.TIMEOUT, lambda: self._stand(chat, auto=True))

    def _kb(self):
        return kb([[("➕ 要牌", "bj:hit"), ("✋ 停牌", "bj:stand")]])

    def _table(self, s, hide=True, result=""):
        dealer = f"{s.d[0]} 🂠" if hide else f"{' '.join(s.d)}（{hand_value(s.d)} 点）"
        text = (f"🃏 <b>21点</b> · 玩家 {mention(s.user)}\n\n"
                f"庄家：{dealer}\n你的牌：{' '.join(s.p)}（{hand_value(s.p)} 点）")
        if result:
            text += f"\n\n{result}"
        elif hide:
            text += f"\n\n要牌还是停牌？（{self.TIMEOUT} 秒不操作自动停牌）"
        return text

    def on_callback(self, cb, parts, req):
        act = parts[1] if len(parts) > 1 else ""
        chat, uid = req.chat_id, req.uid
        if act == "new":
            if self.sess(chat):
                return self.app.answer(cb, "已经有一局在进行中了")
            self.app.answer(cb)
            return self.start(chat, req.user)
        s = self.sess(chat)
        if not s:
            return self.app.answer(cb, "这局已经结束了")
        if uid != s.owner:
            return self.app.answer(cb, "这不是你的牌局哦，可以等这局结束后再开一局")
        if act == "hit":
            s.p.append(s.deck.pop())
            v = hand_value(s.p)
            self.app.answer(cb, f"你抽到 {s.p[-1]}")
            if v > 21:
                return self._finish(chat)
            if v == 21:
                return self._stand(chat)
            self.app.edit(chat, s.msg_id, self._table(s), self._kb())
            self.timer(chat, "timeout", self.TIMEOUT, lambda: self._stand(chat, auto=True))
        elif act == "stand":
            self.app.answer(cb, "停牌")
            self._stand(chat)

    def _stand(self, chat, auto=False):
        s = self.sess(chat)
        if not s:
            return
        while hand_value(s.d) < 17:
            s.d.append(s.deck.pop())
        self._finish(chat, auto)

    def _finish(self, chat, auto=False):
        s = self.sess(chat)
        self.end(chat)
        pv, dv = hand_value(s.p), hand_value(s.d)
        pbj, dbj = is_blackjack(s.p), is_blackjack(s.d)
        pts = 0
        if pv > 21:
            res = "💥 爆牌了，庄家赢！"
        elif pbj and not dbj:
            res, pts = "🌟 Blackjack！你赢了！+3 娱乐分", 3
        elif dbj and not pbj:
            res = "😈 庄家 Blackjack，庄家赢！"
        elif dv > 21:
            res, pts = "🎉 庄家爆牌，你赢了！+2 娱乐分", 2
        elif pv > dv:
            res, pts = "🎉 你赢了！+2 娱乐分", 2
        elif pv < dv:
            res = "😝 庄家赢了！"
        else:
            res = "🤝 平局！"
        if auto:
            res = "⏰ 超时自动停牌。\n" + res
        self.points(chat, s.user, pts)
        self.app.edit(chat, s.msg_id, self._table(s, hide=False, result=res),
                      kb([[("🔁 再来一局", "bj:new")]]))

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.edit(chat_id, s.msg_id, "🃏 21点已取消。")
