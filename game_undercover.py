"""🕵️ 谁是卧底 /undercover：群聊 4~10 人，词语私聊发送，轮流描述后投票。"""
import random

from core import mention
from data_undercover_words import WORD_PAIRS
from game_team import TeamGame


class UndercoverGame(TeamGame):
    key = "uc"
    title = "谁是卧底"
    COMMANDS = [("undercover", "cmd_lobby", "🕵️ 谁是卧底（群聊 4-10 人）")]
    MIN, MAX = 4, 10
    T_LOBBY = 600
    T_DAY = 120
    T_VOTE = 60
    PAYLOAD = "uc"
    RULES = ("🕵️ <b>谁是卧底规则</b>\n"
             "· 4~10 人，大部分人拿到同一个词（平民），1~2 人拿到相近的另一个词（卧底）\n"
             "· 每轮按顺序每人用一句话描述自己的词，然后投票投出一人\n"
             "· 卧底全部出局 → 平民胜；卧底人数 ≥ 平民人数 → 卧底胜")

    def begin(self, gid, s):
        pair = list(random.choice(WORD_PAIRS))
        random.shuffle(pair)
        s.civ_word, s.spy_word = pair
        n_spy = 1 if len(s.players) <= 6 else 2
        s.spies = set(random.sample(s.players, n_spy))
        s.words = {u: (s.spy_word if u in s.spies else s.civ_word) for u in s.players}
        texts = {u: (f"🕵️ <b>谁是卧底</b>（群：{self.esc(s.title)}）\n你的词语是：<b>{self.esc(s.words[u])}</b>\n\n"
                     f"轮到你时用一句话描述它，但不要直接说出这个词！\n你不知道自己是不是卧底，注意观察别人的描述哦。", None)
                 for u in s.players}
        return self.dm_all(s, texts)

    def after_begin(self, gid, s):
        s.alive = set(s.players)
        self.app.send(gid, f"🕵️ <b>谁是卧底开始！</b>共 {len(s.players)} 人，其中卧底 {len(s.spies)} 人。\n"
                           f"词语已私聊发送，请查看与机器人的私聊。")
        self.round(gid)

    def round(self, gid):
        s = self.sess(gid)
        s.day += 1
        order = self.alive_order(s)
        k = random.randrange(len(order))
        order = order[k:] + order[:k]
        lst = "\n".join(f"{i + 1}. {self.name(s, u)}" for i, u in enumerate(order))
        self.start_talk(gid, f"🗣 <b>第 {s.day} 轮描述</b>\n请按以下顺序，每人在群里用一句话描述自己的词：\n{lst}")

    def on_voted(self, gid, s, out, any_vote):
        s.idle = 0 if any_vote else s.idle + 1
        if s.idle >= 2:
            return self.finish(gid, None, "连续两轮无人投票，游戏自动结束。")
        if out:
            s.alive.discard(out)
            ident = "🕵️ <b>卧底</b>" if out in s.spies else "👤 平民"
            self.app.send(gid, f"⚖️ {self.name(s, out)} 被投出局，TA 的身份是：{ident}")
            if self.check_win(gid):
                return
        else:
            self.app.send(gid, "⚖️ 平票或无人投票，本轮没有人出局。")
        if s.day >= len(s.players) + 2:
            return self.finish(gid, "spy", "回合数已用尽，卧底成功潜伏！")
        self.round(gid)

    def check_win(self, gid):
        s = self.sess(gid)
        spies = len(s.alive & s.spies)
        civs = len(s.alive) - spies
        if spies == 0:
            self.finish(gid, "civ")
            return True
        if spies >= civs:
            self.finish(gid, "spy")
            return True
        return False

    def finish(self, gid, winner, reason=""):
        s = self.sess(gid)
        if not s:
            return
        self.end(gid)
        if winner == "civ":
            head = "🎉 <b>游戏结束：平民获胜！</b>"
        elif winner == "spy":
            head = "🕵️ <b>游戏结束：卧底获胜！</b>"
        else:
            head = "🛑 <b>谁是卧底已结束</b>"
        if reason:
            head += f"　{reason}"
        text = head
        if getattr(s, "spies", None):
            text += (f"\n\n平民词：<b>{self.esc(s.civ_word)}</b>\n卧底词：<b>{self.esc(s.spy_word)}</b>\n"
                     "卧底：" + "、".join(self.name(s, u) for u in s.players if u in s.spies))
        if winner:
            wins = [u for u in s.players if (u in s.spies) == (winner == "spy")]
            loses = [u for u in s.players if u not in wins]
            self.finish_points(gid, s, wins, loses)
            text += "\n\n获胜方每人 +3 娱乐分，其他参与者 +1。"
        self.app.send(gid, text)

    def on_cancel(self, chat_id, s, by_user=None):
        if s.phase == "lobby":
            self.app.edit(chat_id, s.msg_id, "❌ 谁是卧底已解散。")
            return
        self.finish(chat_id, None, f"被 {mention(by_user)} 终止。" if by_user else "")
