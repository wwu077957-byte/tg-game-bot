"""多人群聊游戏（狼人杀 / 谁是卧底）共用的报名、讨论、投票流程。

回调数据格式：<key>:<群ID>:<动作>:<参数>:<轮次>
"""
import random
from collections import Counter

from core import Game, S, esc, kb, mention, chunk


class TeamGame(Game):
    MIN = 4
    MAX = 10
    T_LOBBY = 600
    T_DAY = 120
    T_VOTE = 60
    RULES = ""
    PAYLOAD = "join"

    # -------------------------------------------------- 报名
    def cmd_lobby(self, req):
        if not req.is_group:
            return self.reply(req, f"{self.title}需要在<b>群聊</b>里玩：把我拉进群，在群里发送 /{self.COMMANDS[0][0]}。\n\n"
                                   f"{self.RULES}\n\n⚠️ 每位玩家都需要先在这里（私聊）发送过 /start，"
                                   f"这样我才能私下发给你身份/词语。你已经可以了 ✅")
        chat = req.chat_id
        s = self.sess(chat)
        if s:
            return self.reply(req, f"本群已经有一局{self.title}了（{'报名中' if s.phase == 'lobby' else '进行中'}）。")
        s = self.new_sess(chat, S(owner=req.uid, chat=chat, title=req.chat.get("title") or "本群",
                                  players=[], users={}, phase="lobby", day=0, alive=set(),
                                  msg_id=None, idle=0, votes={}, talk=set(), vote_msg=None, talk_msg=None))
        note = ""
        if self.app.db.is_started(req.uid):
            s.players.append(req.uid)
            s.users[req.uid] = req.user
        else:
            note = f"\n\n⚠️ {mention(req.user)} 你还没私聊过我，请先点「私聊机器人」发送 /start，再点「加入」。"
        m = self.app.send(chat, self.lobby_text(s) + note, self.lobby_kb(chat))
        s.msg_id = (m or {}).get("message_id")
        self.timer(chat, "lobby", self.T_LOBBY, lambda: self._lobby_timeout(chat))

    def cb(self, chat, act, arg=0, seq=0):
        return f"{self.key}:{chat}:{act}:{arg}:{seq}"

    def lobby_text(self, s):
        lst = "\n".join(f"{i + 1}. {mention(s.users[u])}" for i, u in enumerate(s.players)) or "（暂无）"
        return (f"🎮 <b>{self.title}</b> 报名中（{len(s.players)}/{self.MAX}，至少 {self.MIN} 人）\n\n"
                f"已加入：\n{lst}\n\n"
                f"⚠️ 参加前请先点「私聊机器人」并发送 /start，否则收不到身份信息。\n"
                f"人数够了后，发起人或管理员点「开始游戏」。{self.T_LOBBY // 60} 分钟未开始自动解散。")

    def lobby_kb(self, chat):
        return kb([
            [("🙋 加入", self.cb(chat, "join")), ("🚪 退出", self.cb(chat, "leave"))],
            [("▶️ 开始游戏", self.cb(chat, "start")), ("❌ 解散", self.cb(chat, "stop"))],
            [("🤖 私聊机器人", self.app.start_link(self.PAYLOAD))],
        ])

    def _lobby_timeout(self, chat):
        s = self.sess(chat)
        if s and s.phase == "lobby":
            self.end(chat)
            self.app.edit(chat, s.msg_id, f"⏰ {self.title}报名超时，已自动解散。")

    # -------------------------------------------------- 回调分发
    def on_callback(self, cb, parts, req):
        try:
            gid, act = int(parts[1]), parts[2]
            arg = int(parts[3]) if len(parts) > 3 else 0
            seq = int(parts[4]) if len(parts) > 4 else 0
        except (IndexError, ValueError):
            return self.app.answer(cb, "无效操作")
        s = self.sess(gid)
        if not s:
            return self.app.answer(cb, "这局游戏已经结束了")
        fn = getattr(self, "cb_" + act, None)
        if fn:
            fn(cb, s, gid, req.user, arg, seq)

    def cb_join(self, cb, s, gid, user, arg, seq):
        uid = user["id"]
        if s.phase != "lobby":
            return self.app.answer(cb, "游戏已经开始了，下局再来吧")
        if uid in s.users:
            return self.app.answer(cb, "你已经加入了")
        if len(s.players) >= self.MAX:
            return self.app.answer(cb, "人数已满")
        if not self.app.db.is_started(uid):
            return self.app.answer(cb, "请先点「私聊机器人」按钮，在私聊里发送 /start，再回来点加入！", alert=True)
        s.players.append(uid)
        s.users[uid] = user
        self.app.edit(gid, s.msg_id, self.lobby_text(s), self.lobby_kb(gid))
        self.app.answer(cb, "加入成功！")

    def cb_leave(self, cb, s, gid, user, arg, seq):
        uid = user["id"]
        if s.phase != "lobby" or uid not in s.users:
            return self.app.answer(cb, "你不在报名名单里")
        s.players.remove(uid)
        del s.users[uid]
        self.app.edit(gid, s.msg_id, self.lobby_text(s), self.lobby_kb(gid))
        self.app.answer(cb, "已退出")

    def cb_stop(self, cb, s, gid, user, arg, seq):
        if s.phase != "lobby":
            return self.app.answer(cb, "游戏已开始，请用 /endgame 结束")
        if not self.app.can_manage(gid, s, user["id"]):
            return self.app.answer(cb, "只有发起人或管理员可以解散", alert=True)
        self.end(gid)
        self.app.edit(gid, s.msg_id, f"❌ {self.title}已解散。")
        self.app.answer(cb, "已解散")

    def cb_start(self, cb, s, gid, user, arg, seq):
        if s.phase != "lobby":
            return self.app.answer(cb, "游戏已经开始了")
        if not self.app.can_manage(gid, s, user["id"]):
            return self.app.answer(cb, "只有发起人或管理员可以开始", alert=True)
        if len(s.players) < self.MIN:
            return self.app.answer(cb, f"至少需要 {self.MIN} 人，现在只有 {len(s.players)} 人", alert=True)
        self.app.answer(cb, "游戏开始！")
        failed = self.begin(gid, s)
        if failed:
            names = "、".join(mention(s.users[u]) for u in failed)
            for uid in failed:
                s.players.remove(uid)
                s.users.pop(uid, None)
            s.phase = "lobby"
            self.app.send(gid, "⚠️ 无法私聊以下玩家，已将其移出报名（请先私聊机器人发送 /start 再加入）：\n"
                          + names,
                          kb([[("🤖 私聊机器人", self.app.start_link(self.PAYLOAD))]]))
            self.app.edit(gid, s.msg_id, self.lobby_text(s), self.lobby_kb(gid))
            return
        self.untimer(gid, "lobby")
        self.app.edit(gid, s.msg_id, f"🎮 {self.title}已开始！玩家：" +
                      "、".join(mention(s.users[u]) for u in s.players))
        self.after_begin(gid, s)

    def after_begin(self, gid, s):
        raise NotImplementedError

    def begin(self, gid, s):
        """分配身份并私信。返回私信失败的玩家 ID 列表（为空表示成功）。"""
        raise NotImplementedError

    def dm_all(self, s, texts):
        """texts: {uid: (text, markup)}，返回发送失败的 uid 列表"""
        failed = []
        for uid, (text, markup) in texts.items():
            if not self.app.send_private(uid, text, markup):
                failed.append(uid)
        return failed

    # -------------------------------------------------- 工具
    def name(self, s, uid):
        return mention(s.users[uid])

    def alive_list(self, s):
        return "\n".join(f"{i + 1}. {self.name(s, u)}" for i, u in enumerate(p for p in s.players if p in s.alive))

    def alive_order(self, s):
        return [u for u in s.players if u in s.alive]

    # -------------------------------------------------- 讨论
    def start_talk(self, gid, text):
        s = self.sess(gid)
        s.phase = "day"
        s.talk = set()
        m = self.app.send(gid, text + f"\n\n💬 讨论 {self.T_DAY} 秒后投票；超过半数存活玩家点「跳过讨论」可提前投票。",
                          self._talk_kb(s, gid))
        s.talk_msg = (m or {}).get("message_id")
        s.talk_text = text
        self.timer(gid, "day", self.T_DAY, lambda: self.start_vote(gid))

    def _talk_kb(self, s, gid):
        need = len(s.alive) // 2 + 1
        return kb([[(f"⏩ 跳过讨论（{len(s.talk)}/{need}）", self.cb(gid, "talk", 0, s.day))]])

    def cb_talk(self, cb, s, gid, user, arg, seq):
        uid = user["id"]
        if s.phase != "day" or seq != s.day:
            return self.app.answer(cb, "现在不是讨论阶段")
        if uid not in s.alive:
            return self.app.answer(cb, "只有存活玩家可以操作")
        s.talk.add(uid)
        self.app.answer(cb, "已记录")
        if len(s.talk) > len(s.alive) / 2:
            self.start_vote(gid)
        else:
            self.app.edit(gid, s.talk_msg, s.talk_text + "\n\n💬 讨论中……", self._talk_kb(s, gid))

    # -------------------------------------------------- 投票
    def start_vote(self, gid):
        s = self.sess(gid)
        if not s or s.phase != "day":
            return
        self.untimer(gid, "day")
        if s.talk_msg:
            self.app.edit(gid, s.talk_msg, s.talk_text + "\n\n💬 讨论结束。")
        s.phase = "vote"
        s.votes = {}
        m = self.app.send(gid, self._vote_text(s), self._vote_kb(s, gid))
        s.vote_msg = (m or {}).get("message_id")
        self.timer(gid, "vote", self.T_VOTE, lambda: self.end_vote(gid))

    def _vote_text(self, s):
        return (f"🗳 <b>第 {s.day} 轮投票</b>：请存活玩家点击要投出的人（{self.T_VOTE} 秒，可改票）\n"
                f"已投票：{len(s.votes)}/{len(s.alive)}")

    def _vote_kb(self, s, gid):
        btns = [(f"{i + 1}. {s.users[u].get('first_name', '')[:12] or u}", self.cb(gid, "vote", u, s.day))
                for i, u in enumerate(self.alive_order(s))]
        rows = chunk(btns, 2)
        rows.append([("🙅 弃票", self.cb(gid, "vote", 0, s.day))])
        return kb(rows)

    def cb_vote(self, cb, s, gid, user, target, seq):
        uid = user["id"]
        if s.phase != "vote" or seq != s.day:
            return self.app.answer(cb, "现在不是投票阶段")
        if uid not in s.alive:
            return self.app.answer(cb, "只有存活玩家可以投票")
        if target and target not in s.alive:
            return self.app.answer(cb, "该玩家已出局")
        s.votes[uid] = target
        who = s.users[target].get("first_name", "") if target else "弃票"
        self.app.answer(cb, f"你投给了：{who}（可改票）")
        if len(s.votes) >= len(s.alive):
            self.end_vote(gid)
        else:
            self.app.edit(gid, s.vote_msg, self._vote_text(s), self._vote_kb(s, gid))

    def end_vote(self, gid):
        s = self.sess(gid)
        if not s or s.phase != "vote":
            return
        self.untimer(gid, "vote")
        s.phase = "tally"
        real = {v: t for v, t in s.votes.items() if t and v in s.alive and t in s.alive}
        detail = "\n".join(f"{self.name(s, v)} → {self.name(s, t) if t else '弃票'}"
                           for v, t in s.votes.items()) or "（无人投票）"
        out = None
        if real:
            cnt = Counter(real.values()).most_common()
            if len(cnt) == 1 or cnt[0][1] > cnt[1][1]:
                out = cnt[0][0]
        self.app.edit(gid, s.vote_msg, f"🗳 第 {s.day} 轮投票结束\n{detail}")
        self.on_voted(gid, s, out, bool(s.votes))

    def on_voted(self, gid, s, out, any_vote):
        raise NotImplementedError

    @staticmethod
    def plurality(values):
        if not values:
            return None
        cnt = Counter(values).most_common()
        top = [k for k, c in cnt if c == cnt[0][1]]
        return random.choice(top)

    def finish_points(self, gid, s, winners, losers):
        for u in winners:
            self.points(gid, s.users[u], 3)
        for u in losers:
            self.points(gid, s.users[u], 1)

    def esc(self, x):
        return esc(x)
