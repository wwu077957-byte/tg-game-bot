"""🧠 答题 /quiz：群里抢答（第一个答对得分），私聊连对模式；/quizrank 排行榜。"""
import random

from core import Game, S, esc, kb, mention
from data_quiz_bank import QUESTIONS

LETTERS = "ABCD"


class QuizGame(Game):
    key = "quiz"
    title = "答题"
    COMMANDS = [("quiz", "cmd_quiz", "🧠 知识问答"), ("quizrank", "cmd_rank", "🏅 答题排行榜")]
    GROUP_TIME = 30
    PRIVATE_TIME = 60

    def __init__(self, app):
        super().__init__(app)
        self.used = {}
        self.seq = 0

    def _pick(self, chat):
        used = self.used.setdefault(chat, set())
        if len(used) >= len(QUESTIONS):
            used.clear()
        idx = random.choice([i for i in range(len(QUESTIONS)) if i not in used])
        used.add(idx)
        return idx

    def cmd_quiz(self, req):
        if self.sess(req.chat_id):
            return self.reply(req, "🧠 上一题还没结束，先答完吧～")
        self._ask(req.chat_id, req.is_group, req.user, 0)

    def _ask(self, chat, group, user, streak):
        idx = self._pick(chat)
        q = QUESTIONS[idx]
        perm = list(range(len(q["o"])))
        random.shuffle(perm)
        self.seq += 1
        s = self.new_sess(chat, S(owner=user["id"], user=user, idx=idx, perm=perm, tried=set(),
                                  group=group, streak=streak, msg_id=None, seq=self.seq))
        head = f"🧠 <b>【{esc(q['c'])}】</b>"
        if not group:
            head += f" 第 {streak + 1} 题"
        limit = self.GROUP_TIME if group else self.PRIVATE_TIME
        opts = "\n".join(f"{LETTERS[i]}. {esc(q['o'][p])}" for i, p in enumerate(perm))
        tip = "第一个答对的人得 1 分，每人只有一次机会！" if group else "连续答对挑战最高纪录！"
        text = f"{head}\n\n{esc(q['q'])}\n\n{opts}\n\n⏱ {limit} 秒 · {tip}"
        btns = [(f"{LETTERS[i]}", f"quiz:a:{s.seq}:{i}") for i in range(len(perm))]
        m = self.app.send(chat, text, kb([btns]))
        s.msg_id = (m or {}).get("message_id")
        s.text = text
        self.timer(chat, "timeout", limit, lambda: self._timeout(chat, s.seq))

    def _answer_line(self, s):
        q = QUESTIONS[s.idx]
        pos = s.perm.index(q["a"])
        return f"正确答案：<b>{LETTERS[pos]}. {esc(q['o'][q['a']])}</b>"

    def on_callback(self, cb, parts, req):
        act = parts[1] if len(parts) > 1 else ""
        chat, uid = req.chat_id, req.uid
        if act == "next":
            if self.sess(chat):
                return self.app.answer(cb, "还有题目没答完")
            self.app.answer(cb)
            return self._ask(chat, req.is_group, req.user, 0)
        if act != "a":
            return
        s = self.sess(chat)
        try:
            seq, choice = int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            return
        if not s or s.seq != seq:
            return self.app.answer(cb, "这道题已经结束了")
        q = QUESTIONS[s.idx]
        correct = 0 <= choice < len(s.perm) and s.perm[choice] == q["a"]
        if s.group:
            if uid in s.tried:
                return self.app.answer(cb, "你已经答过这题了，每人只有一次机会")
            s.tried.add(uid)
            if not correct:
                return self.app.answer(cb, "❌ 答错了～")
            self.end(chat)
            self.points(chat, req.user, 1)
            self.app.answer(cb, "✅ 答对了！+1 分")
            return self.app.edit(chat, s.msg_id, f"{s.text}\n\n✅ {mention(req.user)} 抢答正确！+1 分\n"
                                 f"{self._answer_line(s)}", kb([[("➡️ 下一题", "quiz:next")]]))
        # 私聊连对模式
        if correct:
            self.end(chat)
            self.points(chat, req.user, 1)
            streak = s.streak + 1
            self.app.answer(cb, f"✅ 正确！连对 {streak} 题")
            self.app.edit(chat, s.msg_id, f"{s.text}\n\n✅ 回答正确！当前连对 <b>{streak}</b> 题")
            return self._ask(chat, False, req.user, streak)
        self.app.answer(cb, "❌ 答错了")
        self._streak_over(chat, s, "❌ 答错了！")

    def _streak_over(self, chat, s, why):
        self.end(chat)
        best = self.app.db.stat_max(chat, s.owner, "quiz_streak", s.streak)
        self.app.edit(chat, s.msg_id, f"{s.text}\n\n{why}{self._answer_line(s)}\n"
                      f"本次连对 <b>{s.streak}</b> 题，个人最佳 <b>{best}</b> 题。",
                      kb([[("🔁 再来一轮", "quiz:next")]]))

    def _timeout(self, chat, seq):
        s = self.sess(chat)
        if not s or s.seq != seq:
            return
        if s.group:
            self.end(chat)
            self.app.edit(chat, s.msg_id, f"{s.text}\n\n⏰ 时间到，没人答对！{self._answer_line(s)}",
                          kb([[("➡️ 下一题", "quiz:next")]]))
        else:
            self._streak_over(chat, s, "⏰ 时间到！")

    def on_cancel(self, chat_id, s, by_user=None):
        self.app.edit(chat_id, s.msg_id, f"{s.text}\n\n已取消。{self._answer_line(s)}")

    def cmd_rank(self, req):
        rows = self.app.db.top(req.chat_id, "quiz")
        text = "🏅 <b>答题排行榜</b>（仅供娱乐）\n\n"
        if rows:
            text += "\n".join(f"{i + 1}. {esc(n)} — {p} 分" for i, (_u, n, p) in enumerate(rows))
        else:
            text += "还没有人答对过，发送 /quiz 开始吧！"
        if not req.is_group:
            text += f"\n\n🔥 你的最佳连对：{self.app.db.get_stat(req.chat_id, req.uid, 'quiz_streak')} 题"
        self.reply(req, text)
