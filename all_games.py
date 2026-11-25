"""所有游戏模块。顺序即普通文字消息的处理优先级。"""
from game_blackjack import BlackjackGame
from game_dice import DiceGame
from game_draw import DrawGame
from game_guess import GuessGame
from game_idiom import IdiomGame
from game_quiz import QuizGame
from game_rps import RpsGame
from game_tod import TodGame
from game_ttt import TttGame
from game_undercover import UndercoverGame
from game_werewolf import WerewolfGame

ALL_GAMES = [
    DiceGame, DrawGame, WerewolfGame, QuizGame, GuessGame, IdiomGame,
    RpsGame, TodGame, BlackjackGame, TttGame, UndercoverGame,
]
