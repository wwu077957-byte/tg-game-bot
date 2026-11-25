"""🐺 狼人杀（简化版）/werewolf：群聊 5~12 人，夜间行动通过私聊机器人完成。"""
import random

from core import chunk, kb, mention
from game_team import TeamGame

ROLE = {"wolf": "🐺 狼人", "seer": "🔮 预言家", "witch": "🧪 女巫", "hunter": "🔫 猎人", "villager": "👨‍🌾 村民"}
ROLE_DESC = {
    "wolf": "每晚和狼队友一起选择袭击一名玩家。目标：狼人数量 ≥ 好人数量。",
    "seer": "每晚可以查验一名玩家是不是狼人。",
    "witch": "有一瓶解药（救当晚被袭击的人）和一瓶毒药（毒死一人），每种只能用一次，每晚最多用一瓶。",
    "hunter": "被狼人袭击或被投票放逐时，可以开枪带走一名玩家（被毒死不能开枪）。",
    "villager": "没有特殊技能，白天靠推理和投票找出狼人！",
}
WOLVES = {5: 1, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 3, 12: 4}


def make_roles(n):
    roles = ["wolf"] * WOLVES[n] + ["seer", "witch"] + (["hunter"] if n >= 7 else [])
    roles += ["villager"] * (n - len(roles))
    random.shuffle(roles)
    return roles


class WerewolfGame(TeamGame):
    key = "ww"
    title = "狼人杀"
    COMMANDS = [("werewolf", "cmd_lobby", "🐺 狼人杀（群聊 5-12 人）")]
    MIN, MAX = 5, 12
    T_LOBBY = 600
    T_NIGHT = 90
    T_WITCH = 45
    T_HUNTER = 40
    T_DAY = 120
    T_VOTE = 60
    PAYLOAD = "ww"
    RULES = ("🐺 <b>狼人杀规则（简化版）</b>\n"
             "· 5~12 人，身份：狼人、预言家、女巫、猎人(7人以上)、村民\n"
             "· 夜晚：狼人选择袭击目标，预言家查验，女巫用药（都在私聊里点按钮）\n"
             "· 白天：公布死讯，自由讨论后投票放逐一人\n"
             "· 狼人全部出局 → 好人胜；狼人数量 ≥ 好人数量 → 狼人胜")

    # -------------------------------------------------- 开局
    def begin(self, gid, s):
        roles = make_roles(len(s.players))
        s.roles = dict(zip(s.players, roles))
        s.potion_save = s.potion_poison = True
        wolves = [u for u in s.players if s.roles[u] == "wolf"]
        texts = {}
        for uid in s.players:
            r = s.roles[uid]
            t = (f"🐺 <b>狼人杀</b>（群：{self.esc(s.title)}）\n你的身份是：<b>{ROLE[r]}</b>\n{ROLE_DESC[r]}")
            if r == "wolf":
                mates = [self.name(s, w) for w in wolves if w != uid]
                t += "\n\n你的狼队友：" + ("、".join(mates) if mates else "（只有你一匹狼）")
            texts[uid] = (t, None)
        return self.dm_all(s, texts)

    def after_begin(self, gid, s):
        s.alive = set(s.players)
        from collections import Counter
        c = Counter(s.roles.values())
        conf = "、".join(f"{ROLE[r]}×{c[r]}" for r in ("wolf", "seer", "witch", "hunter", "villager") if c[r])
        self.app.send(gid, f"🐺 <b>狼人杀开始！</b>共 {len(s.players)} 人\n本局配置：{conf}\n\n"
                           f"身份已私聊发送给每位玩家，请查看与机器人的私聊。\n\n玩家：\n{self.alive_list(s)}")
        self.night(gid)

    # -------------------------------------------------- 夜晚
    def role_alive(self, s, role):
        return [u for u in self.alive_order(s) if s.roles[u] == role]

    def night(self, gid):
        s = self.sess(gid)
        s.phase = "night"
        s.day += 1
        s.wolf_votes = {}
        s.kill = None
        s.saved = False
        s.poisoned = None
        s.acted = False
        seers = self.role_alive(s, "seer")
        s.seer_done = not seers
        self.app.send(gid, f"🌙 <b>第 {s.day} 夜</b>，天黑请闭眼……\n"
                           f"狼人、预言家请到与机器人的私聊中行动（{self.T_NIGHT} 秒）。")
        wolves = self.role_alive(s, "wolf")
        targets = [u for u in self.alive_order(s) if s.roles[u] != "wolf"]
        btns = [(f"🔪 {s.users[u].get('first_name', '')[:14]}", self.cb(gid, "kill", u, s.day)) for u in targets]
        wkb = kb(chunk(btns, 2) + [[("🤝 空刀（不袭击）", self.cb(gid, "kill", 0, s.day))]])
        for w in wolves:
            self.app.send_private(w, f"🌙 第 {s.day} 夜 · 狼人请选择今晚袭击的目标（可改选，狼队以多数票为准）：", wkb)
        for sr in seers:
            cands = [u for u in self.alive_order(s) if u != sr]
            sbtn = [(f"🔍 {s.users[u].get('first_name', '')[:14]}", self.cb(gid, "see", u, s.day)) for u in cands]
            self.app.send_private(sr, f"🌙 第 {s.day} 夜 · 预言家请选择要查验的玩家：", kb(chunk(sbtn, 2)))
        self.timer(gid, "night", self.T_NIGHT, lambda: self.end_wolves(gid))

    def _night_ok(self, s, cb, uid, role, seq):
        if s.phase != "night" or seq != s.day:
            self.app.answer(cb, "这个操作已经过期了")
            return False
        if uid not in s.alive or s.roles.get(uid) != role:
            self.app.answer(cb, "你现在不能这样操作")
            return False
        return True

    def cb_kill(self, cb, s, gid, user, target, seq):
        uid = user["id"]
        if not self._night_ok(s, cb, uid, "wolf", seq):
            return
        if target and (target not in s.alive or s.roles[target] == "wolf"):
            return self.app.answer(cb, "不能选择这个目标")
        s.wolf_votes[uid] = target
        s.acted = True
        tname = s.users[target].get("first_name", "") if target else "空刀"
        self.app.answer(cb, f"已选择：{tname}")
        for w in self.role_alive(s, "wolf"):
            if w != uid:
                self.app.send_private(w, f"🐺 队友 {self.name(s, uid)} 选择了：{mention(s.users[target]) if target else '空刀'}")
        if len(s.wolf_votes) >= len(self.role_alive(s, "wolf")) and s.seer_done:
            self.end_wolves(gid)

    def cb_see(self, cb, s, gid, user, target, seq):
        uid = user["id"]
        if not self._night_ok(s, cb, uid, "seer", seq):
            return
        if s.seer_done:
            return self.app.answer(cb, "今晚已经查验过了")
        if target not in s.alive or target == uid:
            return self.app.answer(cb, "不能查验这个玩家")
        s.seer_done = True
        s.acted = True
        res = "🐺 狼人" if s.roles[target] == "wolf" else "👍 好人"
        self.app.answer(cb, "查验完成")
        self.app.edit(user["id"], cb["message"]["message_id"],
                      f"🔮 第 {s.day} 夜查验结果：{self.name(s, target)} 是 <b>{res}</b>")
        if len(s.wolf_votes) >= len(self.role_alive(s, "wolf")):
            self.end_wolves(gid)

    def end_wolves(self, gid):
        s = self.sess(gid)
        if not s or s.phase != "night":
            return
        self.untimer(gid, "night")
        s.kill = self.plurality([t for t in s.wolf_votes.values() if t and t in s.alive])
        witches = self.role_alive(s, "witch")
        if witches and (s.potion_save or s.potion_poison):
            w = witches[0]
            s.phase = "witch"
            rows = []
            if s.kill:
                text = f"🧪 第 {s.day} 夜 · 今晚 {self.name(s, s.kill)} 被狼人袭击了。"
                if s.potion_save:
                    rows.append([("💊 使用解药救TA", self.cb(gid, "save", 0, s.day))])
            else:
                text = f"🧪 第 {s.day} 夜 · 今晚是平安夜，没有人被袭击。"
            if s.potion_poison:
                pb = [(f"☠️ 毒 {s.users[u].get('first_name', '')[:12]}", self.cb(gid, "poison", u, s.day))
                      for u in self.alive_order(s) if u != w]
                rows += chunk(pb, 2)
            rows.append([("⏭ 今晚不用药", self.cb(gid, "wskip", 0, s.day))])
            text += f"\n解药：{'✅' if s.potion_save else '❌'}  毒药：{'✅' if s.potion_poison else '❌'}（每晚最多用一瓶，{self.T_WITCH} 秒）"
            if self.app.send_private(w, text, kb(rows)):
                self.timer(gid, "witch", self.T_WITCH, lambda: self.resolve_night(gid))
                return
        self.resolve_night(gid)

    def _witch_ok(self, s, cb, uid, seq):
        if s.phase != "witch" or seq != s.day:
            self.app.answer(cb, "这个操作已经过期了")
            return False
        if uid not in s.alive or s.roles.get(uid) != "witch":
            self.app.answer(cb, "你现在不能这样操作")
            return False
        return True

    def cb_save(self, cb, s, gid, user, arg, seq):
        if not self._witch_ok(s, cb, user["id"], seq):
            return
        if not (s.potion_save and s.kill):
            return self.app.answer(cb, "现在不能用解药")
        s.potion_save, s.saved, s.acted = False, True, True
        self.app.answer(cb, "已使用解药")
        self.app.edit(user["id"], cb["message"]["message_id"], f"💊 你救下了 {self.name(s, s.kill)}。")
        self.resolve_night(gid)

    def cb_poison(self, cb, s, gid, user, target, seq):
        if not self._witch_ok(s, cb, user["id"], seq):
            return
        if not s.potion_poison or target not in s.alive or target == user["id"]:
            return self.app.answer(cb, "现在不能毒这个人")
        s.potion_poison, s.poisoned, s.acted = False, target, True
        self.app.answer(cb, "已使用毒药")
        self.app.edit(user["id"], cb["message"]["message_id"], f"☠️ 你对 {self.name(s, target)} 使用了毒药。")
        self.resolve_night(gid)

    def cb_wskip(self, cb, s, gid, user, arg, seq):
        if not self._witch_ok(s, cb, user["id"], seq):
            return
        s.acted = True
        self.app.answer(cb, "今晚不用药")
        self.app.edit(user["id"], cb["message"]["message_id"], "⏭ 今晚你没有用药。")
        self.resolve_night(gid)

    def resolve_night(self, gid):
        s = self.sess(gid)
        if not s or s.phase not in ("night", "witch"):
            return
        self.untimer(gid, "witch")
        s.phase = "dawn"
        deaths = []
        if s.kill and not s.saved:
            deaths.append(s.kill)
        if s.poisoned and s.poisoned not in deaths:
            deaths.append(s.poisoned)
        for d in deaths:
            s.alive.discard(d)
        if deaths:
            msg = "昨晚 " + "、".join(self.name(s, d) for d in deaths) + " 死亡。"
        else:
            msg = "昨晚是平安夜，没有人死亡。"
        self.app.send(gid, f"☀️ <b>天亮了</b>（第 {s.day} 天）\n{msg}")
        if self.check_win(gid):
            return
        if s.kill and not s.saved and s.roles[s.kill] == "hunter" and s.kill != s.poisoned:
            return self.hunter(gid, s.kill, "day")
        self.day(gid)

    # -------------------------------------------------- 猎人
    def hunter(self, gid, hid, then):
        s = self.sess(gid)
        s.phase = "hunter"
        s.hunter_id = hid
        s.after = then
        btns = [(f"🔫 {s.users[u].get('first_name', '')[:12]}", self.cb(gid, "shoot", u, s.day))
                for u in self.alive_order(s)]
        markup = kb(chunk(btns, 2) + [[("🙅 不开枪", self.cb(gid, "shoot", 0, s.day))]])
        if self.app.send_private(hid, f"🔫 你是猎人，你出局了！可以开枪带走一名玩家（{self.T_HUNTER} 秒）：", markup):
            self.timer(gid, "hunter", self.T_HUNTER, lambda: self.after_hunter(gid))
        else:
            self.after_hunter(gid)

    def cb_shoot(self, cb, s, gid, user, target, seq):
        uid = user["id"]
        if s.phase != "hunter" or seq != s.day or uid != s.hunter_id:
            return self.app.answer(cb, "这个操作已经过期了")
        if target and target not in s.alive:
            return self.app.answer(cb, "该玩家已出局")
        self.app.answer(cb, "收到")
        s.acted = True
        if target:
            s.alive.discard(target)
            self.app.edit(uid, cb["message"]["message_id"], f"🔫 你带走了 {self.name(s, target)}。")
            self.app.send(gid, f"🔫 猎人 {self.name(s, uid)} 开枪带走了 {self.name(s, target)}！")
            if self.check_win(gid):
                return
        else:
            self.app.edit(uid, cb["message"]["message_id"], "🙅 你选择不开枪。")
        self.after_hunter(gid)

    def after_hunter(self, gid):
        s = self.sess(gid)
        if not s or s.phase != "hunter":
            return
        self.untimer(gid, "hunter")
        if s.after == "day":
            self.day(gid)
        else:
            self.night(gid)

    # -------------------------------------------------- 白天
    def day(self, gid):
        s = self.sess(gid)
        self.start_talk(gid, f"🗣 <b>第 {s.day} 天 · 白天</b>\n存活玩家（{len(s.alive)}）：\n{self.alive_list(s)}")

    def on_voted(self, gid, s, out, any_vote):
        if not any_vote and not s.acted:
            s.idle += 1
        else:
            s.idle = 0
        if s.idle >= 2:
            return self.finish(gid, None, "连续两轮无人操作，游戏自动结束。")
        if out:
            s.alive.discard(out)
            self.app.send(gid, f"⚖️ {self.name(s, out)} 被投票放逐了。")
            if self.check_win(gid):
                return
            if s.roles[out] == "hunter":
                return self.hunter(gid, out, "night")
        else:
            self.app.send(gid, "⚖️ 平票或无人投票，今天没有人被放逐。")
        self.night(gid)

    # -------------------------------------------------- 胜负
    def check_win(self, gid):
        s = self.sess(gid)
        w = len(self.role_alive(s, "wolf"))
        g = len(s.alive) - w
        if w == 0:
            self.finish(gid, "good")
            return True
        if w >= g:
            self.finish(gid, "wolf")
            return True
        return False

    def finish(self, gid, winner, reason=""):
        s = self.sess(gid)
        if not s:
            return
        self.end(gid)
        if winner == "good":
            head = "🎉 <b>游戏结束：好人阵营获胜！</b>"
        elif winner == "wolf":
            head = "🐺 <b>游戏结束：狼人阵营获胜！</b>"
        else:
            head = f"🛑 <b>狼人杀已结束</b>　{reason}"
        lines = []
        for u in s.players:
            r = s.roles.get(u)
            if r:
                lines.append(f"{'❤️' if u in s.alive else '💀'} {self.name(s, u)} — {ROLE[r]}")
        text = head + ("\n\n身份公布：\n" + "\n".join(lines) if lines else "")
        if winner:
            wins = [u for u in s.players if (s.roles[u] == "wolf") == (winner == "wolf")]
            loses = [u for u in s.players if u not in wins]
            self.finish_points(gid, s, wins, loses)
            text += "\n\n获胜方每人 +3 娱乐分，其他参与者 +1。"
        self.app.send(gid, text)

    def on_cancel(self, chat_id, s, by_user=None):
        if s.phase == "lobby":
            self.app.edit(chat_id, s.msg_id, "❌ 狼人杀已解散。")
            return
        who = f"被 {mention(by_user)} 终止。" if by_user else ""
        self.finish(chat_id, None, who)
