"""游戏机器人入口：长轮询 getUpdates，带限流重试与断线重连。

环境变量：
  BOT_TOKEN   必填，Telegram 机器人 Token
  DB_PATH     可选，SQLite 文件路径，默认 ./gamebot.db
  LOG_LEVEL   可选，默认 INFO
"""
import logging
import os
import signal
import sys
import threading
import time

import tg
from core import App
from db import DB

log = logging.getLogger("gamebot")


class Runner:
    def __init__(self, app, api):
        self.app = app
        self.api = api
        self.stop = threading.Event()

    def timer_loop(self):
        while not self.stop.is_set():
            try:
                self.app.tick()
            except Exception:
                log.exception("定时器线程异常")
            self.stop.wait(0.5)

    def setup_with_retry(self):
        delay = 2
        while not self.stop.is_set():
            try:
                self.app.setup()
                return True
            except tg.TelegramError as e:
                if e.code in (401, 404):
                    log.error("Token 无效（%s），请检查 BOT_TOKEN", e.code)
                    return False
                log.warning("初始化失败：%s，%s 秒后重试", e, delay)
            except tg.NetworkError as e:
                log.warning("网络错误：%s，%s 秒后重试", e, delay)
            self.stop.wait(delay)
            delay = min(delay * 2, 60)
        return False

    def poll_loop(self):
        offset = None
        backoff = 1
        while not self.stop.is_set():
            try:
                updates = self.api.get_updates(offset, timeout=30)
                backoff = 1
            except tg.TelegramError as e:
                if e.code == 401:
                    log.error("Token 已失效，退出。")
                    return
                if e.code == 409:
                    log.error("冲突：同一个 Token 有其他实例在运行或设置了 Webhook（%s）", e.description)
                wait = e.retry_after or backoff
                log.warning("getUpdates 失败：%s，%s 秒后重试", e, wait)
                self.stop.wait(wait)
                backoff = min(backoff * 2, 60)
                continue
            except tg.NetworkError as e:
                log.warning("网络错误：%s，%s 秒后重连", e, backoff)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            except Exception:
                log.exception("轮询出现未知错误")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                self.app.process(upd)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        print("错误：请设置环境变量 BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    db_path = os.environ.get("DB_PATH", "./gamebot.db")
    api = tg.Api(token)
    app = App(api, DB(db_path))
    runner = Runner(app, api)

    def _stop(*_):
        log.info("收到退出信号，正在停止……")
        runner.stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if not runner.setup_with_retry():
        sys.exit(2)
    threading.Thread(target=runner.timer_loop, name="timers", daemon=True).start()
    log.info("开始长轮询……")
    runner.poll_loop()
    time.sleep(0.2)


if __name__ == "__main__":
    main()
