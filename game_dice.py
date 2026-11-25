"""🎲 骰子：/dice、/dice NdM、/dice 比大小"""
import random
import re

from core import Game, S, kb, mention


class DiceGame(Game):
    key = "dice"
    title = "骰子比大小"
    COMMANDS = [("dice", "cmd_dice", "🎲 掷骰子（/dice 2d6，/dice 比大小）")]
    LOBBY = 120       # 比大小报名时间（秒）
    ROLL_DELAY = 4    # 等骰子动画结束再公布结果
    MAX_PLAYERS = 10
    _ann = 0

    def _later(self, chat_id, text, markup=None):
        DiceGame._ann += 1
        self.app.sched.set(
            f"{chat_id}:dice-ann:{DiceGame._ann}", self.ROLL_DELAY,
            lambda: self.app.send(chat_id, text, markup),
        )

    def _roll(self, chat_id, reply_to=None):
        m = self.app.send_dice(chat_id, "🎲", reply_to)
        v = ((m or {}).get("dice") or {}).get("value")
        return v if v else random.randint(1, 6)

    def cmd_dice(self, req):
        a = req.args.strip()
        if a in ("比大小", "pk", "PK", "battle", "对战"):
            return self.start_battle(req)
        if a:
            m = re.fullmatch(r"(\d{1,3})?\s*[dD]\s*(\d{1,5})", a)
            if not m:
                return self.reply(
                    req,
                    "用法：\n/dice — 掷一个骰子\n/dice 2d6 — 掷 2 个六面骰\n/dice 比大小 — 群内比大小",
                )
            n, faces = int(m.group(1) or 1), int(m.group(2))
            if not (1 <= n <= 20 and 2 <= faces <= 1000):
                return self.reply(req, "骰子数量 1~20 个，面数 2~1000。")
            rolls = [random.randint(1, faces) for _ in range(n)]
            if n == 1:
                body = f"<b>{rolls[0]}</b>"
            else:
                body = " + ".join(map(str, rolls)) + f" = <b>{sum(rolls)}</b>"
            return self.reply(req, f"🎲 {mention(req.user)} 掷出 {n}d{faces}：{body}")
        v = self._roll(req.chat_id, req.message_id)
        self._later(req.chat_id, f"🎲 {mention(req.user)} 掷出了 <b>{v}</b> 点！")

    # ---------------------------------------------- 比大小
    def start_battle(self, req):
        chat = req.chat_id
        if not req.is_group:
            self.app.send(chat, "🎲 你 vs 🤖 机器人，开掷！")
            uv = self._roll(chat)
            bv = self._roll(chat)
            if uv > bv:
                res = "🎉 你赢了！+1 娱乐分"
                self.points(chat, req.user, 1)
            elif uv < bv:
                res = "😝 机器人赢了！"
            else:
                res = "🤝 平局！"
            self._later(
                chat, f"你：<b>{uv}</b>  机器人：<b>{bv}</b>\n{res}",
                kb([[("🔁 再来一局", "menu:1")]]),
            )
            return
        if self.sess(chat):
            return self.reply(req, "本群已经有一局骰子比大小在报名中啦，点「参加」加入吧！")
        s = self.new_sess(chat, S(owner=req.uid, players={req.uid: req.user}, msg_id=None))
        m = self.app.send(chat, self._lobby_text(s), self._lobby_kb())
        s.msg_id = (m or {}).get("message_id")
        self.timer(chat, "lobby", self.LOBBY, lambda: self._timeout(chat))

    def _lobby_text(self, s):
        names = "\n".join(f"· {mention(u)}" for u in s.players.values())
        return (
            f"🎲 <b>骰子比大小</b> 报名中（{len(s.players)}/{self.MAX_PLAYERS}）\n"
            f"点「参加」加入，发起人点「开始」后每人掷一次，点数最大者获胜！\n"
            f"{self.LOBBY} 秒后自动开始。\n\n已参加：\n{names}"
        )

    def _lobby_kb(self):
        return kb([[("🙋 参加", "dice:join"), ("🎲 开始", "dice:go")], [("❌ 取消", "dice:stop")]])

    def on_callback(self, cb, parts, req):
        chat, uid = req.chat_id, req.uid
        s = self.sess(chat)
        act = parts[1] if len(parts) > 1 else ""
        if not s:
            return self.app.answer(cb, "这局已经结束了")
        if act == "join":
            if uid in s.players:
                return self.app.answer(cb, "你已经参加了")
            if len(s.players) >= self.MAX_PLAYERS:
                return self.app.answer(cb, "人数已满")
            s.players[uid] = req.user
            self.app.edit(chat, s.msg_id, self._lobby_text(s), self._lobby_kb())
            return self.app.answer(cb, "参加成功！")
        if act in ("go", "stop") and not self.app.can_manage(chat, s, uid):
            return self.app.answer(cb, "只有发起人或管理员可以操作", alert=True)
        if act == "go":
            if len(s.players) < 2:
                return self.app.answer(cb, "至少需要 2 人才能开始", alert=True)
            self.app.answer(cb, "开掷！")
            return self._battle(chat)
        if act == "stop":
            self.end(chat)
            self.app.edit(chat, s.msg_id, "🎲 骰子比大小已取消。")
            return self.app.answer(cb, "已取消")

    def _timeout(self, chat):
        s = self.sess(chat)
        if not s:
            return
        if len(s.players) >= 2:
            return self._battle(chat)
        self.end(chat)
        self.app.edit(chat, s.msg_id, "⏰ 报名超时，人数不足，骰子比大小取消。")

    def _battle(self, chat):
        s = self.sess(chat)
        self.end(chat)
        self.app.edit(chat, s.msg_id, "🎲 骰子比大小开始！参赛者：" + "、".join(
            mention(u) for u in s.players.values()))
        results = []
        for u in s.players.values():
            self.app.send(chat, f"{mention(u)} 掷骰：")
            results.append((self._roll(chat), u))
        best = max(v for v, _ in results)
        winners = [u for v, u in results if v == best]
        for w in winners:
            self.points(chat, w, 2)
        lines = [f"{'👑' if v == best else '·'} {mention(u)}：<b>{v}</b>"
                 for v, u in sorted(results, key=lambda x: -x[0])]
        win = "、".join(mention(u) for u in winners)
        tail = "并列第一" if len(winners) > 1 else "获胜"
        self._later(chat, "🎲 <b>比大小结果</b>\n" + "\n".join(lines) +
                    f"\n\n🏆 {win} {tail}！各 +2 娱乐分")
