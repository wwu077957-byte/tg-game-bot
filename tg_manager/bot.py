from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, events, functions, types
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .storage import Store
from .auto_reply import SCENE_LABELS, choose_scene, local_datetime, normalize_config, offline_reason, render_template, scene_labels
from .security import is_allowed_user_id, parse_allowed_user_ids
from .picker import build_account_picker, parse_picker_callback
from .countries import (
    PhoneInputError,
    all_countries,
    get_country,
    normalize_phone_for_country,
    search_countries,
)

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tg_account_manager")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "data/tg_manager.sqlite3").strip()
SESSION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "").strip()
ALLOWED_TELEGRAM_USER_IDS = parse_allowed_user_ids(os.getenv("ALLOWED_TELEGRAM_USER_IDS"))
CODE_DISPLAY_TTL_SECONDS = 30

if not BOT_TOKEN or not API_ID_RAW or not API_HASH or not SESSION_KEY:
    raise RuntimeError("Set BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH and SESSION_ENCRYPTION_KEY in .env")
try:
    API_ID = int(API_ID_RAW)
except ValueError as exc:
    raise RuntimeError("TELEGRAM_API_ID must be an integer") from exc

store = Store(DB_PATH, SESSION_KEY)

# Conversation state and client objects are intentionally memory-only. OTPs and 2FA
# passwords are never written to SQLite or logs.
@dataclass
class Flow:
    state: str
    login_method: str | None = None
    client: TelegramClient | None = None
    phone: str | None = None
    phone_code_hash: str | None = None
    selected_country: str | None = None
    country_matches: list[str] = field(default_factory=list)
    country_page: int = 0
    current_code_type: Any = None
    next_code_type: Any = None
    resend_ready_at: float = 0.0
    resend_attempts: int = 0
    qr_login: Any = None
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class AutoFlow:
    state: str
    account_id: int
    scene: str | None = None
    started_at: float = field(default_factory=time.monotonic)


FLOW_TTL_SECONDS = 600


def flow_is_expired(flow: Flow) -> bool:
    return time.monotonic() - flow.started_at > FLOW_TTL_SECONDS


flows: dict[int, Flow] = {}
auto_flows: dict[int, AutoFlow] = {}
pending_human_challenges: dict[int, tuple[int, list[int], float, int, int]] = {}
monitors: dict[tuple[int, int], tuple[TelegramClient, asyncio.Task]] = {}
auto_reply_locks: dict[tuple[int, int], asyncio.Lock] = {}
auto_sending_peers: set[tuple[int, int]] = set()
last_qr_login_attempt: dict[int, float] = {}
LOGIN_COOLDOWN_SECONDS = max(30, int(os.getenv("LOGIN_COOLDOWN_SECONDS", "60")))
COUNTRY_PAGE_SIZE = 8
OFFICIAL_CHANNEL_ID = "@dro8r"
OFFICIAL_CHANNEL_URL = "https://t.me/dro8r"

def ui_text(language: str, chinese: str, english: str) -> str:
    """Choose UI copy without changing stored language or user-authored content."""
    return english if language == "en" else chinese


def menu_for(language: str) -> ReplyKeyboardMarkup:
    if language == "en":
        rows = [
            ["📱 My accounts", "➕ Add account"],
            ["🔳 QR login", "📩 Get code"],
            ["📁 Groups", "💻 Devices"],
            ["🛡 2FA status", "🔎 Search"],
            ["🤖 Auto-reply", "🔔 Safety alerts"],
            ["🧾 Activity", "🗑 Trash"],
            [ui_text(language, '🌐 语言 / Language', '🌐 Language'), "❔ Help"],
        ]
        placeholder = "Choose a feature or send a command"
    else:
        rows = [
            ["📱 账号列表", "➕ 添加账号"],
            ["🔳 QR 登录", "📩 获取验证码"],
            ["📁 账号分组", "💻 设备会话"],
            ["🛡 2FA 状态", "🔎 搜索账号"],
            [ui_text(language, '🤖 自动回复', '🤖 Auto-reply'), ui_text(language, '🔔 通知与健康', '🔔 Safety alerts & health')],
            [ui_text(language, '🧾 操作记录', '🧾 Activity'), ui_text(language, '🗑 回收站', '🗑 Trash')],
            [ui_text(language, '🌐 语言 / Language', '🌐 Language'), "❔ 帮助"],
        ]
        placeholder = "选择功能或发送命令"
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, input_field_placeholder=placeholder)


MENU = menu_for("zh")


def menu_for_user(user_id: int) -> ReplyKeyboardMarkup:
    return menu_for(store.get_language(user_id))


def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private" and update.effective_user)


def is_channel_member(member: Any) -> bool:
    status = getattr(member, "status", None)
    return status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(member, "is_member", False))
    )


def human_challenge(user_id: int, language: str) -> tuple[str, InlineKeyboardMarkup]:
    current = pending_human_challenges.get(user_id)
    if not current or current[2] <= time.monotonic():
        left, right = random.randint(2, 9), random.randint(1, 9)
        answer = left + right
        choices = {answer}
        while len(choices) < 4:
            choices.add(random.randint(2, 18))
        options = list(choices)
        random.shuffle(options)
        pending_human_challenges[user_id] = (answer, options, time.monotonic() + 600, left, right)
        current = pending_human_challenges[user_id]
    left, right = current[3], current[4]
    question = f"一次性人机验证：{left} + {right} 等于多少？" if language == "zh" else f"One-time human check: what is {left} + {right}?"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(value), callback_data=f"human:{value}") for value in current[1][:2]],
        [InlineKeyboardButton(str(value), callback_data=f"human:{value}") for value in current[1][2:]],
    ])
    return question, keyboard


async def human_captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS) or not is_private(update):
        if query:
            await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '无权使用 / Not allowed', 'Not allowed'), show_alert=True)
        return
    if store.is_human_verified(user.id):
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已验证 / Already verified', 'Already verified'))
        return
    challenge = pending_human_challenges.get(user.id)
    try:
        selected = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        selected = -1
    if not challenge or challenge[2] <= time.monotonic():
        pending_human_challenges.pop(user.id, None)
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证已过期，请重新验证。 / Check expired.', 'Verification expired. Please verify again.'), show_alert=True)
        if query.message:
            prompt, keyboard = human_challenge(user.id, store.get_language(user.id))
            await query.message.reply_text(prompt, reply_markup=keyboard)
        return
    if selected != challenge[0] or selected not in challenge[1]:
        pending_human_challenges.pop(user.id, None)
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '答案不正确，再试一次。 / Try again.', 'Incorrect answer. Try again.'), show_alert=True)
        if query.message:
            prompt, keyboard = human_challenge(user.id, store.get_language(user.id))
            await query.message.reply_text(prompt, reply_markup=keyboard)
        return
    store.mark_human_verified(user.id)
    pending_human_challenges.pop(user.id, None)
    await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证通过 / Verified', 'Verified'))
    if await require_private(update, context.bot) and query.message:
        language = store.get_language(user.id)
        confirmation = "人机验证和频道验证都已通过，可以开始使用。" if language == "zh" else "Human and channel checks passed. You can now use the bot."
        await query.message.reply_text(confirmation, reply_markup=menu_for(language))


def language_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("中文 ✓" if current == "zh" else "中文", callback_data="lang:zh"),
            InlineKeyboardButton("English ✓" if current == "en" else "English", callback_data="lang:en"),
        ]]
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    current = store.get_language(owner_id(update))
    prompt = "选择机器人语言：" if current == "zh" else "Choose the bot language:"
    await update.effective_message.reply_text(prompt, reply_markup=language_keyboard(current))


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS) or not is_private(update):
        if query:
            await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '无权使用 / Not allowed', 'Not allowed'), show_alert=True)
        return
    if not await require_private(update, context.bot):
        await query.answer()
        return
    language = (query.data or "").split(":", 1)[-1]
    if language not in {"zh", "en"}:
        await query.answer("Invalid language", show_alert=True)
        return
    store.set_language(user.id, language)
    await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已切换 / Updated', 'Updated'))
    if query.message:
        await query.message.edit_text("语言已切换为中文。" if language == "zh" else "Language changed to English.")
        await query.message.reply_text(
            "语言已切换为中文。" if language == "zh" else "Language changed to English.",
            reply_markup=menu_for(language),
        )


async def channel_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS) or not is_private(update):
        if query:
            await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '无权使用 / Not allowed', 'Not allowed'), show_alert=True)
        return
    if not await require_private(update, context.bot):
        await query.answer()
        return
    language = store.get_language(user.id)
    await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证通过 / Verified', 'Verified'))
    if query.message:
        await query.message.reply_text(
            "已验证加入官方频道，现在可以使用机器人了。" if language == "zh"
            else "Membership verified. You can now use the bot.",
            reply_markup=menu_for(language),
        )


def consume_qr_login_cooldown(user_id: int) -> int:
    now = time.monotonic()
    remaining = LOGIN_COOLDOWN_SECONDS - (now - last_qr_login_attempt.get(user_id, 0.0))
    if remaining > 0:
        return int(remaining) + 1
    last_qr_login_attempt[user_id] = now
    return 0


def country_start_keyboard(language: str = "zh") -> InlineKeyboardMarkup:
    buttons = [
        (ui_text(language, '美国 +1', 'United States +1'), "US"),
        (ui_text(language, '加拿大 +1', 'Canada +1'), "CA"),
        (ui_text(language, '中国 +86', 'China +86'), "CN"),
        (ui_text(language, '英国 +44', 'United Kingdom +44'), "GB"),
    ]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"ctry:select:{region}") for label, region in buttons[:2]],
            [InlineKeyboardButton(label, callback_data=f"ctry:select:{region}") for label, region in buttons[2:]],
            [InlineKeyboardButton(ui_text(language, '🔎 搜索国家/地区', '🔎 Search countries/regions'), callback_data="ctry:search")],
            [InlineKeyboardButton(ui_text(language, '🌍 浏览全部国家/地区', '🌍 Browse all countries/regions'), callback_data="ctry:all:0")],
        ]
    )


def country_results_keyboard(regions: list[str], page: int, language: str = "zh") -> InlineKeyboardMarkup:
    total_pages = max(1, (len(regions) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * COUNTRY_PAGE_SIZE
    rows = []
    for region in regions[start : start + COUNTRY_PAGE_SIZE]:
        country = get_country(region)
        if country:
            rows.append(
                [
                    InlineKeyboardButton(
                        ui_text(language, '{0} {1}', '{0} {1}').format(
                            country.name if language != "en" else country.english_name,
                            country.calling_code,
                        ),
                        callback_data=f"ctry:select:{country.region}",
                    )
                ]
            )
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(ui_text(language, '‹ 上一页', '‹ Previous page'), callback_data=f"ctry:page:{page - 1}"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(ui_text(language, '下一页 ›', 'Next page ›'), callback_data=f"ctry:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(ui_text(language, '🔎 重新搜索', '🔎 Search again'), callback_data="ctry:search")])
    return InlineKeyboardMarkup(rows)


def phone_identity(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def matching_saved_accounts(user_id: int, phone: str) -> list[dict[str, Any]]:
    identity = phone_identity(phone)
    return [row for row in store.list_accounts(user_id) if phone_identity(row["phone"]) == identity]


def code_delivery_guidance(code_type: Any, language: str = "zh") -> str:
    if isinstance(code_type, types.auth.SentCodeTypeApp):
        return ui_text(language, '投递方式：Telegram 官方服务消息。请检查该账号其他已登录设备上与 777000 的对话。', "Delivery method: Telegram official service message. Check the conversation with 777000 on the account's other logged-in devices.")
    if isinstance(code_type, types.auth.SentCodeTypeSms):
        return ui_text(language, '投递方式：短信。请检查此号码对应的 SIM 卡、垃圾短信/拦截箱及双卡收件卡槽。', 'Delivery method: SMS. Check the SIM card for this number, your spam/blocked-message folder, and the receiving SIM slot if you use dual SIMs.')
    if isinstance(code_type, types.auth.SentCodeTypeCall):
        return ui_text(language, '投递方式：语音电话。请留意来电并按语音提示记录验证码。', 'Delivery method: voice call. Watch for the incoming call and record the verification code as instructed by the voice prompt.')
    if isinstance(code_type, types.auth.SentCodeTypeFlashCall):
        pattern = getattr(code_type, "pattern", "")
        extra = ui_text(language, ' Telegram 返回的来电号码匹配规则：{0}。', ' Telegram returned this incoming-number matching rule: {0}.').format(pattern) if pattern else ""
        return ui_text(language, '投递方式：闪呼，电话可能响一声后挂断；按 Telegram 要求核对来电号码。', 'Delivery method: flash call; the phone may ring once and then hang up. Verify the incoming number as Telegram requires.') + extra
    if isinstance(code_type, types.auth.SentCodeTypeMissedCall):
        return ui_text(language, '投递方式：未接来电。请查看来电记录，并按 Telegram 要求取来电号码末尾数字作为验证码。', 'Delivery method: missed call. Check your call history and, as Telegram instructs, use the ending digits of the incoming number as the verification code.')
    if isinstance(code_type, types.auth.SentCodeTypeEmailCode):
        pattern = getattr(code_type, "email_pattern", "")
        destination = ui_text(language, ' Telegram 显示的邮箱：{0}。', ' Email address shown by Telegram: {0}.').format(pattern) if pattern else ui_text(language, ' 请检查 Telegram 账号绑定的登录邮箱。', ' Check the login email linked to the Telegram account.')
        return ui_text(language, '投递方式：邮箱。', 'Delivery method: email.') + destination
    if isinstance(code_type, types.auth.SentCodeTypeSmsWord):
        return ui_text(language, '投递方式：短信单词码。请把短信中的单词原样作为验证码输入，不要改成数字。', 'Delivery method: SMS word code. Enter the word from the SMS exactly as the verification code; do not change it to digits.')
    if isinstance(code_type, types.auth.SentCodeTypeSmsPhrase):
        return ui_text(language, '投递方式：短信短语码。请把短信中的完整短语原样输入。', 'Delivery method: SMS phrase code. Enter the complete phrase exactly as received.')
    if isinstance(code_type, types.auth.SentCodeTypeFragmentSms):
        return ui_text(language, '投递方式：Telegram 指向 Fragment。此 Bot 不会访问或读取外部钱包/网站；请仅按 Telegram 官方客户端提示继续，或改用二维码登录。', "Delivery method: Telegram directs you to Fragment. This bot will not access or read external wallets/websites; continue only according to the Telegram official client's prompts, or use QR login instead.")
    if isinstance(code_type, types.auth.SentCodeTypeFirebaseSms):
        return ui_text(language, '投递方式：官方移动端 Firebase 验证。该流程需要官方应用的设备完整性凭据，Bot 无法代办；若 Telegram 提供下一通道，可在等待后主动重发，否则请用官方客户端或二维码登录。', "Delivery method: official mobile Firebase verification. This process requires the official app's device-integrity credentials, which the bot cannot handle for you; if Telegram provides a next method, you can resend after waiting, otherwise use the official client or QR login.")
    if isinstance(code_type, types.auth.SentCodeTypeSetUpEmailRequired):
        return ui_text(language, 'Telegram 要求先设置或验证登录邮箱；当前 Bot 不会代为设置邮箱。请在 Telegram 官方客户端完成后，再重新开始登录。', 'Telegram requires you to set up or verify a login email first; this bot will not set up the email for you. Complete this in the official Telegram client, then start login again.')
    return ui_text(language, 'Telegram 返回了此 Bot 未支持的验证码投递方式；请在 Telegram 官方客户端继续，或改用二维码登录。', 'Telegram returned a verification-code delivery method that this bot does not support; continue in the official Telegram client, or use QR login instead.')


def code_type_label(code_type: Any, language: str = "zh") -> str:
    labels = {
        "CodeTypeSms": ui_text(language, '短信', 'SMS'),
        "CodeTypeCall": ui_text(language, '语音电话', 'Voice call'),
        "CodeTypeFlashCall": ui_text(language, '闪呼', 'Flash call'),
        "CodeTypeMissedCall": ui_text(language, '未接来电', 'Missed call'),
        "CodeTypeFragmentSms": "Fragment",
    }
    name = type(code_type).__name__
    return labels.get(name, ui_text(language, 'Telegram 返回的 {0} 通道', '{0} channel returned by Telegram').format(name))


def code_delivery_message(
    phone: str,
    code_type: Any,
    next_type: Any,
    timeout: int,
    *,
    has_matching_account: bool,
    language: str = "zh",
    resend: bool = False,
) -> str:
    """Compose all phone-code delivery instructions in the user's saved language."""
    if resend:
        header = ui_text(language, 'Telegram 返回的当前验证码投递方式：', 'Current verification-code delivery method returned by Telegram:')
    else:
        header = ui_text(language, 'Telegram 为 {0} 返回的验证码投递方式：', 'Verification-code delivery method returned by Telegram for {0}:').format(Store.mask_phone(phone))
    message = (
        f"{header}\n{code_delivery_guidance(code_type, language)}\n\n"
        + ui_text(language, '如果此方式已送达，请在此私聊按提示输入验证码；验证码会在处理后尝试删除，不会保存。', 'If the code has arrived by this method, enter it here in the private chat as instructed; the bot will try to delete it after processing, and it will not be saved.')
    )
    if has_matching_account and isinstance(code_type, types.auth.SentCodeTypeApp):
        message += "\n\n" + ui_text(language, '如需从本人已保存的同号会话读取 777000，请明确点选下方按钮；不会自动读取。', 'To retrieve a code from your own saved session for the same number, explicitly select the button below; it will not be retrieved automatically.')
    if next_type is not None:
        if resend:
            message += (
                "\n\n" + ui_text(language, 'Telegram 提供的下一通道：{0}；返回等待 {1} 秒，本 Bot 至少等待 45 秒。如仍未收到，请在等待结束后主动 /resend。', 'Next method offered by Telegram: {0}; Telegram returned a wait of {1} seconds, and this bot waits at least 45 seconds. If the code still has not arrived, send /resend after the wait.')
                .format(code_type_label(next_type, language), int(timeout or 0))
            )
        else:
            message += (
                "\n\n" + ui_text(language, 'Telegram 提供的下一种通道是：{0}。返回的等待时间为 {1} 秒；本 Bot 至少等待 45 秒。若仍未收到，等候结束后由你主动发送 /resend；最多再发送 4 次，不会自动连续重发。', 'Next method offered by Telegram: {0}. Telegram returned a wait of {1} seconds; this bot waits at least 45 seconds. If the code still has not arrived, send /resend yourself after the wait. You may resend up to 4 times; the bot will not resend automatically.')
                .format(code_type_label(next_type, language), int(timeout or 0))
            )
    elif resend:
        message += "\n\n" + ui_text(language, 'Telegram 未提供下一通道；不再重发，请发送 /qr。', 'Telegram did not offer a next method; there will be no more resends. Send /qr.')
    else:
        message += "\n\n" + ui_text(language, 'Telegram 未返回可用的下一通道；本 Bot 不会强制切换，请发送 /qr 使用二维码登录。', 'Telegram did not return another available method; this bot will not force a switch. Send /qr to use QR login.')
    return message + "\n/cancel " + ui_text(language, '取消。', 'to cancel.')


async def require_private(update: Update, bot: Any) -> bool:
    user = update.effective_user
    if not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS):
        return False
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '为保护账号隐私，请只在机器人私聊中操作。', 'To protect account privacy, use the bot only in a private chat.'))
        return False
    if not store.is_human_verified(user.id):
        question, keyboard = human_challenge(user.id, store.get_language(user.id))
        if update.effective_message:
            await update.effective_message.reply_text(question, reply_markup=keyboard)
        return False
    try:
        member = await bot.get_chat_member(OFFICIAL_CHANNEL_ID, user.id)
    except Exception as exc:
        log.info("Channel membership check unavailable (%s)", type(exc).__name__)
        message = "暂时无法核验频道成员，请稍后重试。" if store.get_language(user.id) == "zh" else "Membership check is temporarily unavailable. Please try again shortly."
    else:
        if is_channel_member(member):
            return True
        message = "使用前请先加入官方频道。" if store.get_language(user.id) == "zh" else "Join the official channel before using the bot."
    if update.effective_message:
        await update.effective_message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("加入官方频道 / Join", url=OFFICIAL_CHANNEL_URL),
                InlineKeyboardButton("我已加入，重新检查 / Check", callback_data="channel:check"),
            ]]),
        )
    return False


async def require_callback_access(update: Update, query: Any, bot: Any) -> bool:
    user = update.effective_user
    if not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS) or not is_private(update):
        language = store.get_language(user.id) if user else "zh"
        await query.answer(ui_text(language, '无权使用此机器人。', 'You are not authorized to use this bot.'), show_alert=True)
        return False
    if not await require_private(update, bot):
        await query.answer()
        return False
    return True


def owner_id(update: Update) -> int:
    assert update.effective_user is not None
    return update.effective_user.id


async def delete_user_input(update: Update) -> bool:
    message = update.effective_message
    if not message:
        return False
    try:
        await message.delete()
        return True
    except Exception:
        return False


async def send_secret_handling_note(update: Update, removed: bool) -> None:
    if not removed and update.effective_message:
        await update.effective_message.reply_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '我没有把这段输入写入账号数据库或日志；Telegram 未允许机器人删除该消息。请你在聊天中手动删除刚才的验证码/密码消息。', 'I did not write this input to the account database or logs; Telegram did not allow the bot to delete this message. Please manually delete the verification-code/password message you just sent in the chat.')
        )


async def cleanup_flow(user_id: int) -> None:
    flow = flows.pop(user_id, None)
    if not flow:
        return
    if flow.task and flow.task is not asyncio.current_task() and not flow.task.done():
        flow.task.cancel()
    if flow.client:
        try:
            await flow.client.disconnect()
        except Exception:
            pass


def format_account(row: dict[str, Any], language: str = "zh") -> str:
    phone = Store.mask_phone(row["phone"])
    label = (row.get("label") or "").strip()
    group = row.get("group_name")
    bits = [f"#{row['id']} · {phone}"]
    if label:
        bits.append(label)
    if group:
        bits.append(ui_text(language, f"分组：{group}", f"Group: {group}"))
    return " — ".join(bits)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    name = update.effective_user.first_name if update.effective_user else "朋友"
    language = store.get_language(owner_id(update))
    if language == "en":
        greeting = (
            f"🔐 TGVault account manager\nHi, {name}. You can only see and manage accounts you added.\n\n"
            "Before login, the bot explains the risks and asks for your consent. Codes and 2FA passwords are not saved.\n"
            "Use the menu below, or send /help."
        )
    else:
        greeting = (
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🔐 TGVault 账号管理\n你好，{0}。每位使用者只能看到和操作自己添加的账号。\n\n登录前会先说明托管风险并请求你的明确同意；验证码和 2FA 密码不会写入数据库或日志。\n选择下方菜单，或发送 /help 查看命令。', '🔐 TGVault account manager\nHi, {0}. Each user can only see and manage accounts they added.\n\nBefore login, the bot explains the custodial risks and asks for your explicit consent; verification codes and 2FA passwords are not written to the database or logs.\nChoose an option below, or send /help to view commands.').format(name)
        )
    await update.effective_message.reply_text(
        greeting,
        reply_markup=menu_for_user(owner_id(update)),
    )


async def accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    rows = store.list_accounts(owner_id(update))
    language = store.get_language(owner_id(update))
    if not rows:
        await update.effective_message.reply_text(ui_text(language, '目前没有已添加的账号。使用 /add 添加本人账号。', 'You have no accounts yet. Use /add to add your own account.'), reply_markup=menu_for_user(owner_id(update)))
        return
    heading = ui_text(language, '📱 你的账号（仅你可见）', '📱 Your accounts (visible only to you)')
    text = heading + "\n\n" + "\n".join(format_account(row, language) for row in rows)
    await update.effective_message.reply_text(text, reply_markup=menu_for_user(owner_id(update)))
    store.audit(owner_id(update), "accounts_viewed", None, "account list")


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    await cleanup_flow(user_id)
    flows[user_id] = Flow(state="consent", login_method="phone")
    await update.effective_message.reply_text(
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '登录前的重要说明：Bot 和服务器运营者能够访问可用的 Telegram 登录会话，并可借此访问该账号允许的 Telegram 功能。会话虽加密存储，但这不是端到端加密；请只在你信任运营者和服务器时继续，并且只登录你本人拥有的账号。\n\n如理解并同意，请发送 /consent；否则发送 /cancel。', 'Important notice before login: The bot and server operator can access available Telegram login sessions and may use them to access the Telegram functions permitted for that account. Although the session is stored encrypted, this is not end-to-end encryption; continue only if you trust the operator and server, and log in only to an account you own.\n\nIf you understand and agree, send /consent; otherwise send /cancel.')
    )


async def qr_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    flow = flows.get(user_id)
    if not flow or flow.state != "qr_authorized":
        await cleanup_flow(user_id)
        flows[user_id] = Flow(state="consent", login_method="qr")
        await update.effective_message.reply_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '登录前的重要说明：Bot 和服务器运营者能够访问可用的 Telegram 登录会话，并可借此访问该账号允许的 Telegram 功能。会话虽加密存储，但这不是端到端加密；请只在你信任运营者和服务器时继续，并且只登录你本人拥有的账号。\n\n如理解并同意，请发送 /consent；否则发送 /cancel。', 'Important notice before login: The bot and server operator can access available Telegram login sessions and may use them to access the Telegram functions permitted for that account. Although the session is stored encrypted, this is not end-to-end encryption; continue only if you trust the operator and server, and log in only to an account you own.\n\nIf you understand and agree, send /consent; otherwise send /cancel.')
        )
        return
    wait_seconds = consume_qr_login_cooldown(user_id)
    if wait_seconds:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '为避免触发 Telegram 登录限制，请约 {0} 秒后再发起登录。', 'To avoid triggering Telegram login limits, please wait about {0} seconds before starting login again.').format(wait_seconds))
        await cleanup_flow(user_id)
        return
    wait_seconds = store.begin_auth_attempt(user_id, "code", account_phone="qr")
    if wait_seconds:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求二维码登录冷却，请再等约 {0} 秒。', 'Telegram requires a cooldown for QR login; please wait about {0} more seconds.').format(wait_seconds))
        await cleanup_flow(user_id)
        return
    await cleanup_flow(user_id)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        qr = await client.qr_login()
        flow = Flow(state="qr_wait", client=client, qr_login=qr)
        flows[user_id] = flow
        flow.task = asyncio.create_task(wait_for_qr(user_id, flow, context.application))
        import qrcode

        image = qrcode.make(qr.url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        await update.effective_message.reply_photo(
            photo=buffer,
            caption=ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '用你本人已登录的 Telegram 官方客户端扫描此二维码。二维码等同登录授权，请勿转发或截图分享。如账号启用 2FA，扫描后会在此私聊要求输入密码。', 'Scan this QR code with the official Telegram client where you are logged in. The QR code is equivalent to login authorization; do not forward it or share screenshots. If the account has 2FA enabled, you will be asked to enter the password in this private chat after scanning.'),
        )
        store.audit(user_id, "qr_login_started", None, "QR flow started")
    except FloodWaitError as exc:
        store.defer_auth_attempt(user_id, "code", exc.seconds, account_phone="qr")
        await cleanup_flow(user_id)
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求等待 {0} 秒；已记录等待时间，不会提前重试。', 'Telegram requires waiting {0} seconds; the wait time has been recorded and there will be no early retry.').format(exc.seconds))
    except Exception:
        await cleanup_flow(user_id)
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '无法启动二维码登录。请稍后重试，或使用 /add 通过手机号登录。', 'Unable to start QR login. Please try again later, or use /add to log in with a phone number.'))


async def consent_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    flow = flows.get(user_id)
    if not flow or flow.state != "consent" or flow.login_method not in {"phone", "qr"}:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '当前没有等待确认的登录流程。发送 /add 或 /qr 可开始。', 'There is currently no login flow awaiting confirmation. Send /add or /qr to start.'))
        return
    if flow.login_method == "phone":
        flow.state = "country"
        flow.country_matches = ["US", "CA", "CN", "GB"]
        flow.country_page = 0
        flow.started_at = time.monotonic()
        language = store.get_language(user_id)
        await update.effective_message.reply_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '➕ 添加本人 Telegram 账号\n先选择号码所属国家/地区；也可以搜索中文名、英文名或国际区号。美国与加拿大共用 +1，之后会按区号/号段再核对一次。\n只能添加你本人拥有并授权的账号。/cancel 可取消。', '➕ Add your own Telegram account\nFirst select the country/region for the phone number; you can also search by Chinese name, English name, or international calling code. The United States and Canada share +1; the area code/number range will be checked again afterward.\nYou may only add an account that you own and authorize. Use /cancel to cancel.'),
            reply_markup=country_start_keyboard(language),
        )
        return
    flow.state = "qr_authorized"
    await qr_start(update, context)


async def country_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await require_callback_access(update, query, context.bot):
        return
    user_id = owner_id(update)
    language = store.get_language(user_id)
    flow = flows.get(user_id)
    data = query.data or ""
    if flow and flow_is_expired(flow):
        await cleanup_flow(user_id)
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '登录流程已过期，请重新发送 /add。', 'The login flow has expired. Please send /add again.'), show_alert=True)
        return
    if not flow or (
        flow.state not in {"country", "country_search", "country_results"}
        and not (flow.state == "phone" and data == "ctry:change")
    ):
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '国家选择已过期，请重新发送 /add。', 'The country selection has expired. Please send /add again.'), show_alert=True)
        return
    if data == "ctry:change":
        flow.state = "country"
        flow.selected_country = None
        flow.country_matches = ["US", "CA", "CN", "GB"]
        flow.country_page = 0
        await query.answer()
        if query.message:
            await query.edit_message_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '重新选择号码所属国家/地区；可以搜索中文名、英文名或国际区号。', 'Select the country/region for the phone number again; you can search by Chinese name, English name, or international calling code.'),
                reply_markup=country_start_keyboard(language),
            )
        return
    if data == "ctry:search":
        flow.state = "country_search"
        await query.answer()
        if query.message:
            await query.edit_message_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请发送国家/地区名称（中文或英文），也可以发送区号，例如 +1。/cancel 可取消。', 'Send the country/region name (in Chinese or English), or send a calling code such as +1. Use /cancel to cancel.'))
        return
    if data == "ctry:all:0":
        flow.country_matches = [country.region for country in all_countries()]
        flow.country_page = 0
        flow.state = "country_results"
        await query.answer()
    elif data.startswith("ctry:page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '页面无效。', 'Invalid page.'), show_alert=True)
            return
        pages = max(1, (len(flow.country_matches) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
        if page < 0 or page >= pages:
            await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '页面已过期，请重新搜索。', 'The page has expired. Please search again.'), show_alert=True)
            return
        flow.country_page = page
        await query.answer()
    elif data.startswith("ctry:select:"):
        region = data.rsplit(":", 1)[1].upper()
        if flow.state not in {"country", "country_results"} or region not in flow.country_matches or not get_country(region):
            await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '国家选择无效或已过期。', 'The country selection is invalid or has expired.'), show_alert=True)
            return
        country = get_country(region)
        assert country is not None
        flow.selected_country = region
        flow.state = "phone"
        flow.country_matches = []
        flow.started_at = time.monotonic()
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已选择', 'Selected'))
        if query.message:
            await query.edit_message_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已选择：{0}（{1}）。\n请发送手机号，可输入本地格式，也可输入以 + 开头的完整国际号码。\n号码会按所选国家/地区校验；/cancel 可取消。', 'Selected: {0} ({1}).\nSend the phone number. You may enter it in local format or as a complete international number beginning with +.\nThe number will be validated for the selected country/region; use /cancel to cancel.').format(country.name if language != "en" else country.english_name, country.calling_code),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '更换国家/地区', 'Change country/region'), callback_data="ctry:change")]]
                ),
            )
        return
    else:
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '按钮无效或已过期。', 'The button is invalid or has expired.'), show_alert=True)
        return

    if query.message:
        total_pages = max(1, (len(flow.country_matches) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
        await query.edit_message_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请选择国家/地区（第 {0}/{1} 页）：', 'Select a country/region (page {0}/{1}):').format(flow.country_page + 1, total_pages),
                reply_markup=country_results_keyboard(flow.country_matches, flow.country_page, language),
        )


async def wait_for_qr(user_id: int, flow: Flow, application: Application) -> None:
    try:
        await flow.qr_login.wait(timeout=180)
        if flows.get(user_id) is not flow:
            return
        await finish_login(user_id, flow, application, phone_hint="二维码登录")
    except SessionPasswordNeededError:
        if flows.get(user_id) is flow:
            flow.state = "qr_password"
            await application.bot.send_message(
                user_id,
                ui_text(store.get_language(user_id), '此账号启用了两步验证。请在此私聊发送 2FA 密码；系统不会保存该密码。处理后请删除聊天中的密码消息。', 'This account has two-step verification enabled. Send the 2FA password in this private chat; the system will not save it. After processing, delete the password message from the chat.'),
            )
    except FloodWaitError as exc:
        if flows.get(user_id) is flow:
            store.defer_auth_attempt(user_id, "code", exc.seconds, account_phone="qr")
            await cleanup_flow(user_id)
            await application.bot.send_message(user_id, ui_text(store.get_language(user_id), 'Telegram 要求等待 {0} 秒；已记录该等待时间，不会自动重试。', 'Telegram requires waiting {0} seconds; the wait time has been recorded and there will be no automatic retry.').format(exc.seconds))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if flows.get(user_id) is flow:
            await cleanup_flow(user_id)
            if isinstance(exc, asyncio.TimeoutError):
                message = ui_text(store.get_language(user_id), '二维码登录已超时，重新发送 /qr 获取新二维码。', 'QR login has timed out. Send /qr again to get a new QR code.')
            else:
                message = ui_text(store.get_language(user_id), '二维码登录未完成，请重新发送 /qr。', 'QR login was not completed. Please send /qr again.')
            try:
                await application.bot.send_message(user_id, message)
            except Exception:
                pass


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await add_start(update, context)


async def finish_login(user_id: int, flow: Flow, application: Application, phone_hint: str | None = None) -> None:
    if not flow.client:
        await cleanup_flow(user_id)
        return
    try:
        me = await flow.client.get_me()
        phone = getattr(me, "phone", None) or flow.phone or phone_hint or ""
        if not phone:
            raise RuntimeError("Telegram did not return an account phone number")
        session_string = StringSession.save(flow.client.session)
        auth_scope = flow.phone or "qr"
        store.clear_auth_attempts(user_id, "code", auth_scope)
        store.clear_auth_attempts(user_id, "password", auth_scope)
        account_id = store.add_account(user_id, phone, "", session_string)
        display = Store.mask_phone(phone)
        alerts = store.alerts_enabled(user_id)
        await application.bot.send_message(
            user_id,
            ui_text(store.get_language(user_id), '✅ 已安全添加账号 #{0}（{1}）。会话已加密保存；验证码和 2FA 密码不会保存。', '✅ Account #{0} ({1}) was added securely. The session is stored encrypted; verification codes and 2FA passwords are not saved.').format(account_id, display)
            + (ui_text(store.get_language(user_id), '安全登录提醒已开启。', 'Secure login alerts are enabled.') if alerts else ui_text(store.get_language(user_id), '安全登录提醒当前关闭。', 'Secure login alerts are currently disabled.')),
            reply_markup=menu_for_user(user_id),
        )
        if alerts:
            start_monitor(user_id, account_id, session_string, application)
    except Exception as exc:
        # Do not log or return raw exception text; Telegram exceptions can contain sensitive context.
        log.warning("Account save failed (%s)", type(exc).__name__)
        await application.bot.send_message(user_id, ui_text(store.get_language(user_id), '登录成功后保存账号失败。请检查配置后重试；未保存任何验证码或密码。', 'The account could not be saved after login succeeded. Check the configuration and try again; no verification codes or passwords were saved.'))
    finally:
        await cleanup_flow(user_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    text = (update.effective_message.text or "").strip()
    menu_handlers = {
        "📱 账号列表": accounts,
        "📱 My accounts": accounts,
        "➕ 添加账号": add_start,
        "➕ Add account": add_start,
        "🔳 QR 登录": qr_start,
        "🔳 QR login": qr_start,
        "📩 获取验证码": code_command,
        "📩 Get code": code_command,
        "📁 账号分组": groups,
        "📁 Groups": groups,
        "💻 设备会话": devices,
        "💻 Devices": devices,
        "🛡 2FA 状态": twofa,
        "🛡 2FA status": twofa,
        "🔎 搜索账号": search_prompt,
        "🔎 Search": search_prompt,
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🤖 自动回复', '🤖 Auto-reply'): auto_reply_entry,
        "🤖 Auto-reply": auto_reply_entry,
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🔔 通知与健康', '🔔 Safety alerts & health'): settings,
        "🔔 Safety alerts": settings,
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🧾 操作记录', '🧾 Activity'): audit,
        "🧾 Activity": audit,
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🗑 回收站', '🗑 Trash'): trash,
        "🗑 Trash": trash,
        "❔ 帮助": help_command,
        "❔ Help": help_command,
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🌐 语言 / Language', '🌐 Language'): language_command,
    }
    if text in menu_handlers:
        await menu_handlers[text](update, context)
        return
    user_id = owner_id(update)
    auto_flow = auto_flows.get(user_id)
    if auto_flow:
        if time.monotonic() - auto_flow.started_at > FLOW_TTL_SECONDS:
            auto_flows.pop(user_id, None)
            expired = "Auto-reply setup expired. Open /autoreply again." if store.get_language(user_id) == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '自动回复设置已超时取消，请重新打开 /autoreply。', 'Auto-reply setup expired and was canceled; open /autoreply again.')
            await update.effective_message.reply_text(expired)
            return
        if auto_flow.state == "simple_reply":
            if not text or len(text) > 1000:
                prompt = "Reply must be 1–1000 characters. Send it again, or /cancel." if store.get_language(user_id) == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '回复内容需为 1–1000 个字符，请重新发送，或用 /cancel 取消。', 'The reply must be 1–1000 characters. Send it again, or use /cancel to cancel.')
                await update.effective_message.reply_text(prompt)
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text("Account not found." if store.get_language(user_id) == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到你的这个账号。', 'This account was not found.'))
                return
            if text.strip().casefold() in {"默认", "恢复默认", "default", "reset", "clear"}:
                config["simple_reply"] = ""
            else:
                config["simple_reply"] = text
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_simple_message_changed", auto_flow.account_id, "simple reply updated")
            auto_flows.pop(user_id, None)
            saved = "Reply saved." if store.get_language(user_id) == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '自动回复内容已保存。', 'Auto-reply content saved.')
            await update.effective_message.reply_text(saved, reply_markup=menu_for_user(user_id))
            return
        if auto_flow.state == "template":
            if not text or len(text) > 1000:
                await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '模板不能为空，最多 1000 个字符；请重新发送。', 'The template cannot be empty and may contain at most 1000 characters; send it again.'))
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到你的这个账号。', 'This account was not found.'))
                return
            config["scenes"][auto_flow.scene or "offline"] = text
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_template_changed", auto_flow.account_id, f"scene={auto_flow.scene}")
            auto_flows.pop(user_id, None)
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '场景模板已保存。支持 {name}、{time}、{product}。', 'The scene template was saved. Supported placeholders: {name}, {time}, {product}.'), reply_markup=menu_for_user(owner_id(update)))
            return
        if auto_flow.state == "product":
            if len(text) > 120:
                await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '产品/品牌名最多 120 个字符，请重新发送。', 'The product/brand name may contain at most 120 characters; send it again.'))
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到你的这个账号。', 'This account was not found.'))
                return
            config["product"] = "" if text.strip().casefold() in {"清空", "clear", "-"} else text
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_product_changed", auto_flow.account_id, "product updated")
            auto_flows.pop(user_id, None)
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '{product} 的替换内容已保存。', 'The replacement content for {product} was saved.'), reply_markup=menu_for_user(owner_id(update)))
            return
        if auto_flow.state == "thresholds":
            parts = text.split()
            try:
                if len(parts) != 4:
                    raise ValueError
                idle, queue, start_hour, end_hour = map(int, parts)
                if not 1 <= idle <= 1440 or not 1 <= queue <= 100 or not 0 <= start_hour <= 23 or not 0 <= end_hour <= 23:
                    raise ValueError
            except ValueError:
                await update.effective_message.reply_text(
                    ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式无效。请按“空闲分钟数 排队阈值 夜间开始小时 结束小时”填写，例如：15 3 23 9。', 'Invalid format. Enter “idle minutes queue threshold night start hour end hour”, for example: 15 3 23 9.')
                )
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到你的这个账号。', 'This account was not found.'))
                return
            config.update({"idle_minutes": idle, "queue_threshold": queue, "start_hour": start_hour, "end_hour": end_hour})
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_triggers_changed", auto_flow.account_id, "thresholds updated")
            auto_flows.pop(user_id, None)
            await update.effective_message.reply_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '触发条件已保存：连续 {0} 分钟无人工操作；未回复队列超过 {1} 条；夜间 {2:02d}:00–{3:02d}:00。', 'Trigger conditions saved: no human action for {0} consecutive minutes; more than {1} unanswered items in the queue; nighttime period {2:02d}:00–{3:02d}:00.').format(idle, queue, start_hour, end_hour),
                reply_markup=menu_for_user(owner_id(update)),
            )
            return
    flow = flows.get(user_id)
    if not flow:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请选择菜单功能，或发送 /help。', 'Select a menu function, or send /help.'), reply_markup=menu_for_user(owner_id(update)))
        return
    if flow_is_expired(flow):
        expired_state = flow.state
        await cleanup_flow(user_id)
        if expired_state in {"consent", "country", "country_search", "country_results", "phone", "code", "password", "qr_authorized", "qr_wait", "qr_password"}:
            await update.effective_message.reply_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '登录流程已过期；旧验证码/登录请求不再继续使用。请重新发送 /add，或发送 /qr 并再次确认风险。', 'The login flow has expired; the old verification code/login request will no longer be used. Send /add again, or send /qr and confirm the risk again.')
            )
        else:
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '操作已超时取消，请重新开始。', 'The operation timed out and was canceled; start again.'))
        return
    if flow.state == "consent":
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '若你理解并接受上面的托管风险，请发送 /consent；否则发送 /cancel。', 'If you understand and accept the custody risks above, send /consent; otherwise send /cancel.'))
        return
    if flow.state == "country_search":
        matches = search_countries(text)
        if not matches:
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到匹配项。请换用中文名、英文名或区号搜索；/cancel 可取消。', 'No matches found. Search using a Chinese name, English name, or calling code; /cancel cancels.'))
            return
        flow.country_matches = [country.region for country in matches]
        flow.country_page = 0
        flow.state = "country_results"
        pages = max(1, (len(flow.country_matches) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
        await update.effective_message.reply_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '找到 {0} 个国家/地区（第 1/{1} 页），请选择：', 'Found {0} countries/regions (page 1/{1}); select one:').format(len(matches), pages),
            reply_markup=country_results_keyboard(flow.country_matches, 0, store.get_language(user_id)),
        )
        return
    if flow.state in {"country", "country_results"}:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请点选国家/地区按钮；如需搜索，请点“搜索国家/地区”。/cancel 可取消。', 'Tap a country/region button; to search, tap “Search countries/regions”. /cancel cancels.'))
        return
    if flow.state in {"code", "password", "qr_password"}:
        removed = await delete_user_input(update)
        await send_secret_handling_note(update, removed)
        secret = text.strip()
        if not secret or secret.startswith("/"):
            await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '输入为空或格式不正确；可发送 /cancel 取消。', 'The input is empty or incorrectly formatted; send /cancel to cancel.'))
            return
        if flow.state == "code":
            await submit_login_code(update, context, flow, secret)
        else:
            await submit_2fa_password(update, context, flow, secret)
        return
    if flow.state == "phone":
        if not flow.selected_country:
            flow.state = "country"
            flow.country_matches = ["US", "CA", "CN", "GB"]
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请先选择号码所属国家/地区。', 'First select the country/region for the number.'), reply_markup=country_start_keyboard(store.get_language(user_id)))
            return
        try:
            phone = normalize_phone_for_country(text, flow.selected_country)
        except PhoneInputError as exc:
            language = store.get_language(user_id)
            await update.effective_message.reply_text(
                exc.localized(language),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '更换国家/地区', 'Change country/region'), callback_data="ctry:change")]]
                ),
            )
            return
        await delete_user_input(update)
        wait_seconds, request_count = store.reserve_phone_code_request(user_id, phone)
        if wait_seconds:
            await context.bot.send_message(
                user_id,
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '这个号码的验证码请求仍在独立冷却中，请再等约 {0} 秒。为避免 Telegram 限流，不会提前重试；也可发送 /qr 改用二维码登录。', 'Verification-code requests for this number are still under a separate cooldown; wait about {0} seconds. To avoid Telegram rate limits, there will be no early retry; you can also send /qr to use QR login instead.').format(wait_seconds),
            )
            return
        flow.phone = phone
        flow.resend_attempts = max(0, request_count - 1)
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        flow.client = client
        try:
            await client.connect()
            result = await client.send_code_request(phone)
            if isinstance(result, types.auth.SentCodePaymentRequired):
                await cleanup_flow(user_id)
                await context.bot.send_message(
                    user_id,
                    ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 当前要求官方客户端完成额外步骤；Bot 不会处理付费或订阅。请使用官方客户端或二维码登录。', 'Telegram currently requires an additional step in the official client; the bot will not handle payments or subscriptions. Use the official client or QR login.'),
                )
                return
            if isinstance(result, types.auth.SentCodeSuccess):
                await finish_login(user_id, flow, context.application)
                return
            if isinstance(getattr(result, "type", None), types.auth.SentCodeTypeSetUpEmailRequired):
                language = store.get_language(user_id)
                guidance = code_delivery_guidance(result.type, language)
                await cleanup_flow(user_id)
                await context.bot.send_message(user_id, guidance + ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '完成后可重新发送 /add，或发送 /qr 并再次确认风险。', 'After completing it, you may send /add again, or send /qr and confirm the risk again.'))
                return
            if not getattr(result, "phone_code_hash", None):
                raise RuntimeError("Telegram did not return a phone code hash")
            flow.phone_code_hash = result.phone_code_hash
            flow.current_code_type = result.type
            flow.next_code_type = result.next_type
            flow.state = "code"
            flow.started_at = time.monotonic()
            if result.next_type is not None:
                delay = max(45, int(result.timeout or 0))
                flow.resend_ready_at = time.monotonic() + delay
                store.defer_phone_code_request(user_id, phone, delay)
            else:
                flow.resend_ready_at = 0.0
            markup = None
            matching = []
            if isinstance(result.type, types.auth.SentCodeTypeApp):
                matching = matching_saved_accounts(user_id, phone)
                if matching:
                    language = store.get_language(user_id)
                    markup = build_account_picker(matching, "code", lambda row: format_account(row, language))
            language = store.get_language(user_id)
            message = code_delivery_message(
                phone,
                result.type,
                result.next_type,
                int(result.timeout or 0),
                has_matching_account=bool(matching),
                language=language,
            )
            await context.bot.send_message(user_id, message, reply_markup=markup)
        except PhoneNumberInvalidError:
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 判定号码无效；请重新发送 /add 并核对国家和号码。该号码冷却仍会生效。', 'Telegram determined that the number is invalid; send /add again and verify the country and number. The cooldown for this number still applies.'))
        except FloodWaitError as exc:
            store.defer_phone_code_request(user_id, phone, exc.seconds)
            await cleanup_flow(user_id)
            await context.bot.send_message(
                user_id,
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求等待 {0} 秒；已为这个号码记录该等待时间，不会提前重试。等待结束后可发送 /add 重试，或发送 /qr 改用二维码登录。', 'Telegram requires a wait of {0} seconds; that wait time has been recorded for this number, and there will be no early retry. After the wait ends, send /add to retry, or send /qr to use QR login instead.').format(exc.seconds),
            )
        except Exception as exc:
            log.warning("Phone login initialization failed (%s)", type(exc).__name__)
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '无法发起登录请求。请检查 Telegram API 配置、网络和账号状态；也可发送 /qr。', 'Unable to start the login request. Check the Telegram API configuration, network, and account status; you can also send /qr.'))
        return
    if flow.state == "search":
        flows.pop(user_id, None)
        await run_search(update, text)
        return
    if flow.state == "rename":
        match = re.match(r"^(\d+)\s+(.+)$", text)
        flows.pop(user_id, None)
        if not match:
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：账号ID 新备注，例如 2 工作号', 'Format: account ID new label, for example 2 Work account'))
            return
        ok = store.update_label(user_id, int(match.group(1)), match.group(2))
        language = store.get_language(user_id)
        await update.effective_message.reply_text(ui_text(language, '备注已更新。', 'Label updated.') if ok else ui_text(language, '没有找到你的这个账号。', 'This account was not found.'), reply_markup=menu_for_user(user_id))
        return
    if flow.state == "qr_wait":
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请用 Telegram 官方客户端扫描刚才的二维码，或发送 /cancel。', 'Use the official Telegram client to scan the QR code just shown, or send /cancel.'))
        return


async def submit_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow, code: str) -> None:
    user_id = owner_id(update)
    if not flow.phone:
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '手机号登录流程已失效，请重新发送 /add；或发送 /qr 使用二维码登录。', 'The phone-number login flow is no longer valid; send /add again, or send /qr to use QR login.'))
        return
    wait_seconds = store.begin_auth_attempt(user_id, "code", account_phone=flow.phone)
    if wait_seconds:
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证码尝试正在冷却，请约 {0} 秒后再试。', 'Verification-code attempts are under cooldown; try again in about {0} seconds.').format(wait_seconds))
        return
    try:
        if not flow.client or not flow.phone or not flow.phone_code_hash:
            raise RuntimeError("login flow expired")
        await flow.client.sign_in(phone=flow.phone, code=code, phone_code_hash=flow.phone_code_hash)
        await finish_login(user_id, flow, context.application)
    except SessionPasswordNeededError:
        store.clear_auth_attempts(user_id, "code", flow.phone)
        flow.state = "password"
        flow.started_at = time.monotonic()
        await context.bot.send_message(
            user_id,
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '此账号启用了两步验证。请发送 2FA 密码以继续；它只用于本次登录，不会保存。处理后请删除聊天中的密码消息。', 'This account has two-step verification enabled. Send the 2FA password to continue; it is used only for this login and will not be saved. Delete the password message from the chat after processing.'),
        )
    except PhoneCodeInvalidError:
        wait_seconds = store.record_auth_failure(user_id, "code", account_phone=flow.phone)
        if wait_seconds:
            await cleanup_flow(user_id)
            await context.bot.send_message(
                user_id,
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证码连续错误次数已达上限，登录流程已取消；请在 {0} 秒冷却后重新开始。', 'The maximum number of consecutive incorrect verification-code attempts has been reached and the login flow was canceled; start again after the {0}-second cooldown.').format(wait_seconds),
            )
        else:
            await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证码不正确。每次尝试间至少冷却 5 秒；达到上限后会暂时锁定。', 'The verification code is incorrect. There is at least a 5-second cooldown between attempts; reaching the limit will cause a temporary lock.'))
    except PhoneCodeExpiredError:
        store.clear_auth_attempts(user_id, "code", flow.phone)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证码已过期，发送 /add 获取新的验证码。', 'The verification code has expired; send /add to get a new code.'))
    except FloodWaitError as exc:
        store.defer_auth_attempt(user_id, "code", exc.seconds, account_phone=flow.phone)
        store.defer_phone_code_request(user_id, flow.phone, exc.seconds)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求等待 {0} 秒；已为该号码记录等待时间，不会提前重试。', 'Telegram requires a wait of {0} seconds; the wait time has been recorded for this number, and there will be no early retry.').format(exc.seconds))
    except Exception as exc:
        log.warning("Phone code sign-in failed (%s)", type(exc).__name__)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '登录未完成。请检查验证码是否过期，之后重新发送 /add。', 'Login was not completed. Check whether the verification code expired, then send /add again.'))


async def submit_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow, password: str) -> None:
    user_id = owner_id(update)
    # Telethon does not expose the QR target account before authorization completes;
    # share a conservative owner-scoped QR lock instead of letting new QR tokens bypass it.
    auth_scope = flow.phone or "qr"
    wait_seconds = store.begin_auth_attempt(user_id, "password", account_phone=auth_scope)
    if wait_seconds:
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '2FA 尝试正在冷却，请约 {0} 秒后再试。', '2FA attempts are under cooldown; try again in about {0} seconds.').format(wait_seconds))
        return
    try:
        if not flow.client:
            raise RuntimeError("login flow expired")
        await flow.client.sign_in(password=password)
        await finish_login(user_id, flow, context.application)
    except PasswordHashInvalidError:
        wait_seconds = store.record_auth_failure(user_id, "password", account_phone=auth_scope)
        if wait_seconds:
            await cleanup_flow(user_id)
            await context.bot.send_message(
                user_id,
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '2FA 密码连续错误次数已达上限，登录流程已取消；请在 {0} 秒冷却后重新开始。', 'The maximum number of consecutive incorrect 2FA password attempts has been reached and the login flow was canceled; start again after the {0}-second cooldown.').format(wait_seconds),
            )
        else:
            await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '2FA 密码不正确。每次尝试间至少冷却 5 秒；达到上限后会暂时锁定。/cancel 可取消。', 'The 2FA password is incorrect. There is at least a 5-second cooldown between attempts; reaching the limit will cause a temporary lock. /cancel cancels.'))
    except FloodWaitError as exc:
        store.defer_auth_attempt(user_id, "password", exc.seconds, account_phone=auth_scope)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求等待 {0} 秒；已记录该账号等待时间，不会提前重试。', 'Telegram requires a wait of {0} seconds; the wait time has been recorded for this account, and there will be no early retry.').format(exc.seconds))
    except Exception as exc:
        log.warning("2FA sign-in failed (%s)", type(exc).__name__)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '2FA 登录未完成；请稍后重新开始。', '2FA login was not completed; start again later.'))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    if auto_flows.pop(user, None) is None:
        await cleanup_flow(user)
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '当前操作已取消。', 'The current operation was canceled.'), reply_markup=menu_for_user(owner_id(update)))


async def resend_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    flow = flows.get(user_id)
    if flow and flow_is_expired(flow):
        await cleanup_flow(user_id)
        await update.effective_message.reply_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '登录流程已过期，不能继续重发旧验证码。请重新发送 /add，或发送 /qr 并再次确认风险。', 'The login flow has expired, so the old verification code cannot be resent. Send /add again, or send /qr and confirm the risk again.')
        )
        return
    if not flow or flow.state != "code" or not flow.client or not flow.phone or not flow.phone_code_hash:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '当前没有可重发的手机号验证码。请发送 /add，或发送 /qr 使用二维码登录。', 'There is currently no phone-number verification code available to resend. Send /add, or send /qr to use QR login.'))
        return
    if flow.next_code_type is None:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 没有返回可用的下一通道，不能重发或强制切换；请发送 /qr。', 'Telegram did not return an available next channel, so it cannot be resent or force-switched; send /qr.'))
        return
    if flow.resend_attempts >= 4:
        await update.effective_message.reply_text(
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已达到最多 4 次显式重发。若当前验证码仍有效可继续输入；否则请发送 /qr，或稍后重新开始。', 'The maximum of 4 explicit resends has been reached. If the current verification code is still valid, you may continue entering it; otherwise send /qr or start again later.')
        )
        return
    remaining = flow.resend_ready_at - time.monotonic()
    if remaining > 0:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求先等待；约 {0} 秒后可发送 /resend。', 'Telegram requires you to wait first; you can send /resend in about {0} seconds.').format(int(remaining) + 1))
        return

    wait_seconds, _request_count = store.reserve_phone_code_request(user_id, flow.phone)
    if wait_seconds:
        flow.resend_ready_at = max(flow.resend_ready_at, time.monotonic() + wait_seconds)
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '这个号码仍在独立冷却中，请再等约 {0} 秒。', 'This number is still under a separate cooldown; wait about {0} seconds.').format(wait_seconds))
        return
    flow.resend_attempts += 1
    try:
        result = await flow.client(functions.auth.ResendCodeRequest(flow.phone, flow.phone_code_hash))
        if isinstance(result, types.auth.SentCodeSuccess):
            await finish_login(user_id, flow, context.application)
            return
        if isinstance(result, types.auth.SentCodePaymentRequired):
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 当前要求官方客户端完成额外步骤；Bot 不会处理付费或订阅。请改用官方客户端或二维码登录。', 'Telegram currently requires an additional step in the official client; the bot will not handle payments or subscriptions. Use the official client or QR login instead.'))
            return
        if isinstance(getattr(result, "type", None), types.auth.SentCodeTypeSetUpEmailRequired):
            language = store.get_language(user_id)
            guidance = code_delivery_guidance(result.type, language)
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, guidance + ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '完成后可重新发送 /add，或发送 /qr 并再次确认风险。', 'After completing it, you may send /add again, or send /qr and confirm the risk again.'))
            return
        if not getattr(result, "phone_code_hash", None) or not hasattr(result, "type"):
            raise RuntimeError("Telegram returned an unsupported resend result")
        flow.phone_code_hash = result.phone_code_hash
        flow.current_code_type = result.type
        flow.next_code_type = result.next_type
        flow.started_at = time.monotonic()
        markup = None
        matching = []
        if isinstance(result.type, types.auth.SentCodeTypeApp):
            matching = matching_saved_accounts(user_id, flow.phone)
            if matching:
                language = store.get_language(user_id)
                markup = build_account_picker(matching, "code", lambda row: format_account(row, language))
        if result.next_type is not None:
            delay = max(45, int(result.timeout or 0))
            flow.resend_ready_at = time.monotonic() + delay
            store.defer_phone_code_request(user_id, flow.phone, delay)
        else:
            flow.resend_ready_at = 0.0
        language = store.get_language(user_id)
        message = code_delivery_message(
            flow.phone,
            result.type,
            result.next_type,
            int(result.timeout or 0),
            has_matching_account=bool(matching),
            language=language,
            resend=True,
        )
        await context.bot.send_message(user_id, message, reply_markup=markup)
    except FloodWaitError as exc:
        store.defer_phone_code_request(user_id, flow.phone, exc.seconds)
        flow.resend_ready_at = time.monotonic() + max(45, int(exc.seconds))
        await context.bot.send_message(
            user_id,
            ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 要求等待 {0} 秒；已记录该等待时间。此流程不会自动重试，等待结束后你可再发 /resend。', 'Telegram requires a wait of {0} seconds; the wait time has been recorded. This flow will not retry automatically; after the wait ends, you may send /resend again.').format(exc.seconds),
        )
    except PhoneCodeExpiredError:
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '验证码已过期，请重新发送 /add；也可发送 /qr。手机号冷却与重发上限仍会生效。', 'The verification code has expired; send /add again, or send /qr. The phone-number cooldown and resend limit still apply.'))
    except Exception as exc:
        log.warning("Explicit code resend failed (%s)", type(exc).__name__)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", 'Telegram 未能完成这次重发。为避免重复请求，登录流程已停止；请发送 /qr。', 'Telegram could not complete this resend. To avoid duplicate requests, the login flow has been stopped; send /qr.'))


async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    args = context.args
    if not args:
        await send_account_picker(update, "code")
        return
    try:
        account_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/code 账号ID', 'Format: /code account ID'))
        return

    await retrieve_login_code(user_id, account_id, update.effective_message, context.application)


async def send_account_picker(update: Update, action: str) -> None:
    user_id = owner_id(update)
    language = store.get_language(user_id)
    rows = store.list_accounts(user_id)
    if action == "code":
        flow = flows.get(user_id)
        if flow and flow_is_expired(flow):
            await cleanup_flow(user_id)
            await update.effective_message.reply_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '手机号登录流程已过期；为保护验证码，不再读取旧流程中的 777000 消息。请重新发送 /add。', 'The phone-number login flow has expired; to protect the verification code, old 777000 messages from that flow will no longer be read. Please send /add again.')
            )
            return
        if (
            not flow
            or flow.state != "code"
            or not flow.phone
            or not isinstance(flow.current_code_type, types.auth.SentCodeTypeApp)
        ):
            await update.effective_message.reply_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '为保护验证码，只有本人正在进行手机号登录、且 Telegram 当前返回应用内投递方式时，才可主动查询同号账号的 777000 消息。请确认当前流程或重新发送 /add。', 'To protect verification codes, you may proactively check 777000 messages from a matching account only while you are personally completing a phone-number login and Telegram currently returned in-app delivery. Confirm the current flow or send /add again.')
            )
            return
        rows = matching_saved_accounts(user_id, flow.phone)
        if not rows:
            await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到属于你且号码完全匹配的已保存会话；不会查询其他账号。', 'No saved session belonging to you with an exactly matching phone number was found; other accounts will not be queried.'))
            return
    if not rows:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '先用 /add 或 /qr 添加你本人的账号。', 'First use /add or /qr to add your own account.'), reply_markup=menu_for_user(owner_id(update)))
        return
    titles = {
        "code": ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '要从哪个账号读取验证码？', 'Which account should the verification code be retrieved from?'),
        "devices": ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '要查看哪个账号的登录设备？', "Which account's login devices do you want to view?"),
        "twofa": ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '要查看哪个账号的 2FA 状态？', "Which account's 2FA status do you want to view?"),
    }
    await update.effective_message.reply_text(
        titles[action], reply_markup=build_account_picker(rows, action, lambda row: format_account(row, language))
    )


async def retrieve_login_code(user_id: int, account_id: int, message: Any, application: Application) -> None:
    flow = flows.get(user_id)
    if flow and flow_is_expired(flow):
        await cleanup_flow(user_id)
        await message.reply_text(
            ui_text(store.get_language(user_id), '手机号登录流程已过期；为保护验证码，不再读取旧流程中的 777000 消息。请重新发送 /add。', 'The phone-number login flow has expired; to protect the verification code, old 777000 messages from that flow will no longer be read. Please send /add again.')
        )
        return
    row = store.get_account(user_id, account_id)
    if (
        not flow
        or flow.state != "code"
        or not flow.phone
        or not isinstance(flow.current_code_type, types.auth.SentCodeTypeApp)
    ):
        await message.reply_text(ui_text(store.get_language(user_id), '验证码读取仅可在你本人当前的手机号登录流程中主动发起。请发送 /add。', 'Verification-code retrieval can only be initiated during your own current phone-number login flow. Please send /add.'))
        return
    if not row or phone_identity(row["phone"]) != phone_identity(flow.phone):
        await message.reply_text(ui_text(store.get_language(user_id), '所选账号不属于当前手机号登录流程的同号账号；为保护隔离，没有读取验证码。', 'The selected account does not belong to the matching-number account for the current phone-number login flow; no verification code was read to preserve isolation.'))
        return
    session = store.get_session(user_id, account_id)
    if not session:
        await message.reply_text(ui_text(store.get_language(user_id), '没有找到你的这个账号。', 'This account was not found.'))
        return
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await message.reply_text(ui_text(store.get_language(user_id), '此会话已失效；请重新使用 /add 登录本人账号。', 'This session is no longer valid; please use /add again to log in to your own account.'))
            return
        found: str | None = None
        async for telegram_message in client.iter_messages(777000, limit=30):
            if not telegram_message.date or (datetime.now(timezone.utc) - telegram_message.date).total_seconds() > 24 * 3600:
                continue
            body = (telegram_message.message or "").strip()
            if re.search(r"login code|verification code|code for|код|验证码|登录码", body, re.I):
                match = re.search(r"(?<!\d)(\d{5,6})(?!\d)", body)
                if match:
                    found = match.group(1)
                    break
        store.audit(user_id, "login_code_checked", account_id, "service chat checked; code not stored")
        if found:
            sent = await message.reply_text(
                ui_text(store.get_language(user_id), '账号 {0} 的最新 Telegram 验证码：\n{1}\n此码不会写入数据库，并将在约 {2} 秒后尝试自动删除；请不要转发。', 'The latest Telegram verification code for account {0}:\n{1}\nThis code will not be written to the database and will be automatically deleted after about {2} seconds; please do not forward it.').format(Store.mask_phone(row['phone']), found, CODE_DISPLAY_TTL_SECONDS),
            )
            application.create_task(expire_displayed_code(sent, user_id))
        else:
            await message.reply_text(ui_text(store.get_language(user_id), '未找到最近 24 小时内可识别的 Telegram 登录验证码。', 'No recognizable Telegram login code was found within the last 24 hours.'))
    except Exception as exc:
        log.warning("Code retrieval failed (%s)", type(exc).__name__)
        await message.reply_text(ui_text(store.get_language(user_id), '读取失败。请确认账号登录会话有效，或直接在 Telegram 官方客户端查看。', 'Reading failed. Confirm that the account login session is valid, or check directly in the official Telegram client.'))
    finally:
        await client.disconnect()


async def expire_displayed_code(message: Any, user_id: int) -> None:
    await asyncio.sleep(CODE_DISPLAY_TTL_SECONDS)
    try:
        await message.delete()
    except Exception:
        try:
            await message.reply_text(ui_text(store.get_language(user_id), '验证码消息未能自动删除，请你手动删除该消息。', 'The verification-code message could not be deleted automatically; please delete it manually.'))
        except Exception:
            pass


async def groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    owner = owner_id(update)
    language = store.get_language(owner)
    current = store.list_groups(owner)
    lines = [f"• {g['name']}" for g in current] or [ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '（暂无分组）', '(No groups yet)')]
    await update.effective_message.reply_text(
        ui_text(language, '📁 账号分组', '📁 Account groups') + "\n" + "\n".join(lines) +
        "\n\n" + ui_text(language, '创建：/group_create 分组名\n移动：/move 账号ID 分组名', 'Create: /group_create group name\nMove: /move account ID group name'),
        reply_markup=menu_for_user(owner_id(update)),
    )


async def group_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/group_create 分组名', 'Format: /group_create group name'))
        return
    ok = store.create_group(owner_id(update), name)
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '分组已创建。', 'Group created.') if ok else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '创建失败：名称为空或该分组已存在。', 'Creation failed: the name is empty or this group already exists.'), reply_markup=menu_for_user(owner_id(update)))


async def move_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/move 账号ID 分组名', 'Format: /move account ID group name'))
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '账号 ID 必须是数字。', 'The account ID must be a number.'))
        return
    ok = store.move_to_group(owner_id(update), account_id, " ".join(context.args[1:]))
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已移动账号。', 'Account moved.') if ok else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '未找到你的账号或分组。', 'Your account or group was not found.'), reply_markup=menu_for_user(owner_id(update)))


async def devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    if not context.args:
        await send_account_picker(update, "devices")
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/devices 账号ID', 'Format: /devices account ID'))
        return

    await show_devices(user_id, account_id, update.effective_message)


async def show_devices(user_id: int, account_id: int, message: Any) -> None:
    session = store.get_session(user_id, account_id)
    if not session:
        await message.reply_text(ui_text(store.get_language(user_id), '没有找到你的这个账号。', 'This account was not found.'))
        return
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        result = await client(functions.account.GetAuthorizationsRequest())
        auths = getattr(result, "authorizations", [])
        lines = []
        for auth in auths[:20]:
            device = " ".join(x for x in [getattr(auth, "device_model", ""), getattr(auth, "platform", "")] if x).strip() or ui_text(store.get_language(user_id), '未知设备', 'Unknown device')
            app = " ".join(x for x in [getattr(auth, "app_name", ""), getattr(auth, "app_version", "")] if x).strip()
            country = getattr(auth, "country", "") or ""
            date_active = getattr(auth, "date_active", None)
            active = date_active.strftime("%Y-%m-%d %H:%M UTC") if date_active else ui_text(store.get_language(user_id), '未知', 'Unknown')
            current = ui_text(store.get_language(user_id), '（本会话）', '(This session)') if getattr(auth, "current", False) else ""
            lines.append(ui_text(store.get_language(user_id), '• {0} {1} {2} · 最近活跃 {3} {4}', '• {0} {1} {2} · Last active {3} {4}').format(device, app, country, active, current).strip())
        store.audit(user_id, "devices_viewed", account_id, "authorization list")
        language = store.get_language(user_id)
        await message.reply_text(ui_text(language, '💻 Telegram 登录设备', '💻 Telegram login devices') + "\n" + ("\n".join(lines) if lines else ui_text(language, '未返回设备信息。', 'No device information was returned.')) +
            "\n\n" + ui_text(language, '此版本仅查看设备；撤销陌生会话请在 Telegram 官方设置中操作。', 'This version only displays devices; revoke unfamiliar sessions in Telegram settings.'), reply_markup=menu_for_user(user_id))
    except Exception as exc:
        log.warning("Device listing failed (%s)", type(exc).__name__)
        await message.reply_text(ui_text(store.get_language(user_id), '无法读取设备列表；请检查会话是否仍有效。', 'Unable to read the device list; please check whether the session is still valid.'))
    finally:
        await client.disconnect()


async def twofa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    if not context.args:
        await send_account_picker(update, "twofa")
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/2fa 账号ID', 'Format: /2fa account ID'))
        return
    await show_twofa(user_id, account_id, update.effective_message)


async def show_twofa(user_id: int, account_id: int, message: Any) -> None:
    session = store.get_session(user_id, account_id)
    if not session:
        await message.reply_text(ui_text(store.get_language(user_id), '没有找到你的这个账号。', 'This account was not found.'))
        return
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        result = await client(functions.account.GetPasswordRequest())
        enabled = bool(getattr(result, "has_password", False))
        language = store.get_language(user_id)
        status = ui_text(language, '已开启', 'Enabled') if enabled else ui_text(language, '未开启', 'Not enabled')
        store.audit(user_id, "twofa_status_checked", account_id, "status only")
        await message.reply_text(
            ui_text(language, '🛡 该账号的两步验证：{0}。\n此版本只读取状态，不读取或修改 2FA 密码。\n如需更改，请使用 Telegram 官方设置。', '🛡 Two-step verification for this account: {0}.\nThis version only reads the status; it does not read or change the 2FA password.\nTo make changes, use Telegram settings.').format(status)
            + (ui_text(language, '建议开启强密码与恢复邮箱。', 'Consider using a strong password and recovery email.') if not enabled else ""),
            reply_markup=menu_for_user(user_id),
        )
    except Exception as exc:
        log.warning("2FA status check failed (%s)", type(exc).__name__)
        await message.reply_text(ui_text(store.get_language(user_id), '无法读取 2FA 状态；请检查会话是否有效。', 'Unable to read 2FA status; please check whether the session is valid.'))
    finally:
        await client.disconnect()


async def search_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    flows[owner_id(update)] = Flow(state="search")
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '发送手机号片段、备注或分组名进行搜索。/cancel 取消。', 'Send part of a phone number, a note, or a group name to search. Send /cancel to cancel.'))


async def account_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await require_callback_access(update, query, context.bot):
        return
    parsed = parse_picker_callback(query.data)
    if not parsed:
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '这个账号选择按钮无效或已过期。', 'This account-selection button is invalid or has expired.'), show_alert=True)
        return
    action, account_id = parsed
    await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '正在查询…', 'Querying…'))
    if query.message:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    message = query.message
    if not message:
        return
    user_id = owner_id(update)
    if action == "code":
        await retrieve_login_code(user_id, account_id, message, context.application)
    elif action == "devices":
        await show_devices(user_id, account_id, message)
    elif action == "twofa":
        await show_twofa(user_id, account_id, message)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    query = " ".join(context.args).strip()
    if not query:
        await search_prompt(update, context)
        return
    await run_search(update, query)


async def run_search(update: Update, query: str) -> None:
    owner = owner_id(update)
    q = query.casefold()
    hits = [r for r in store.list_accounts(owner) if q in (r["phone"] + " " + (r["label"] or "") + " " + (r["group_name"] or "")).casefold()]
    store.audit(owner, "account_search", None, "search performed")
    language = store.get_language(owner)
    await update.effective_message.reply_text(
        ui_text(language, '🔎 搜索结果', '🔎 Search results') + "\n" + ("\n".join(format_account(r, language) for r in hits) if hits else ui_text(language, '没有匹配的账号。', 'No matching accounts.')),
        reply_markup=menu_for_user(owner_id(update)),
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    on = store.alerts_enabled(owner_id(update))
    state = ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '开启', 'On') if on else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '关闭', 'Off')
    rows = store.list_accounts(owner_id(update))
    health = ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '暂无账号', 'No accounts yet')
    if rows:
        health = ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '你有 {0} 个已添加账号。当前版本不生成综合健康分；可用 /devices 和 /2fa 查看具体状态。', 'You have {0} added accounts. This version does not generate an overall health score; use /devices and /2fa to view specific statuses.').format(len(rows))
    await update.effective_message.reply_text(
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '🔔 安全通知：{0}\n{1}\n\n设置命令：/notify_on 或 /notify_off。开启后，机器人会在可用时提示 Telegram 官方服务账号发送了安全通知；不会转发通知原文或验证码。', "🔔 Safety alerts: {0}\n{1}\n\nSettings commands: /notify_on or /notify_off. When enabled, the bot will notify you when Telegram's official service account sends a security alert, when available; it will not forward the alert text or verification codes.").format(state, health),
        reply_markup=menu_for_user(owner_id(update)),
    )


def auto_account_picker(rows: list[dict[str, Any]], language: str) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(format_account(row, language), callback_data=f"auto:acct:{row['id']}")] for row in rows]
    return InlineKeyboardMarkup(keyboard)


def auto_settings_keyboard(account_id: int, enabled: bool, language: str) -> InlineKeyboardMarkup:
    if language == "en":
        toggle_label = "Turn auto-reply off" if enabled else "Turn auto-reply on"
        edit_label, recent_label, back_label = "Change reply message", "Recent replies", "Back to accounts"
    else:
        toggle_label = ui_text(language, '关闭自动回复', 'Turn auto-reply off') if enabled else ui_text(language, '开启自动回复', 'Turn auto-reply on')
        edit_label, recent_label, back_label = ui_text(language, '修改回复内容', 'Change reply message'), ui_text(language, '查看最近回复', 'Recent replies'), ui_text(language, '返回账号列表', 'Back to accounts')
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_label, callback_data=f"auto:toggle:{account_id}")],
            [InlineKeyboardButton(edit_label, callback_data=f"auto:content:{account_id}")],
            [InlineKeyboardButton(recent_label, callback_data=f"auto:recent:{account_id}")],
            [InlineKeyboardButton(back_label, callback_data="auto:list")],
        ]
    )


def auto_scene_keyboard(account_id: int, language: str) -> InlineKeyboardMarkup:
    labels = scene_labels(language)
    rows = [
        [InlineKeyboardButton(label, callback_data=f"auto:edit:{account_id}:{scene}")]
        for scene, label in labels.items()
    ]
    rows.append([InlineKeyboardButton(ui_text(language, '返回设置', 'Back to settings'), callback_data=f"auto:acct:{account_id}")])
    return InlineKeyboardMarkup(rows)


async def auto_reply_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    owner = owner_id(update)
    language = store.get_language(owner)
    rows = store.list_accounts(owner)
    if not rows:
        text = "Add your own Telegram account first." if language == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '先添加本人账号，再开启自动回复。', 'Add your own account first, then turn on auto-reply.')
        await update.effective_message.reply_text(text, reply_markup=menu_for_user(owner))
        return
    prompt = "Choose an account. Auto-reply is OFF until you turn it on." if language == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '选择账号。自动回复默认关闭，需你手动开启。', 'Choose an account. Auto-reply is off by default; you must turn it on manually.')
    await update.effective_message.reply_text(
        "🤖 Auto-reply\n" + prompt if language == "en" else "🤖 自动回复\n" + prompt,
        reply_markup=auto_account_picker(rows, language),
    )


async def show_auto_reply_settings(message: Any, owner: int, account_id: int) -> None:
    config = store.get_auto_reply_settings(owner, account_id)
    row = store.get_account(owner, account_id)
    if not config or not row:
        await message.reply_text("Account not found." if store.get_language(owner) == "en" else "没有找到你的这个账号。")
        return
    language = store.get_language(owner)
    status = ("ON" if config["enabled"] else "OFF") if language == "en" else ("已开启" if config["enabled"] else "已关闭")
    reply_type = ("Custom message" if config["simple_reply"] else "Default message") if language == "en" else ("自定义内容" if config["simple_reply"] else "默认内容")
    if language == "en":
        text = (
            f"🤖 Auto-reply · account #{account_id} ({Store.mask_phone(row['phone'])})\n"
            f"Status: {status}\nReply: {reply_type}\n"
            "When it replies: at night or when you have not replied for a while.\n\n"
            "It sends messages from this account, only in private chats. It skips groups and bots.\n"
            "Received message text is not stored. If you send a message yourself, auto-reply pauses."
        )
    else:
        text = (
            f"🤖 自动回复 · 账号 #{account_id}（{Store.mask_phone(row['phone'])}）\n"
            f"状态：{status}\n回复内容：{reply_type}\n"
            "触发方式：夜间或一段时间没回复时自动回复。\n\n"
            "会由这个账号发消息，只处理私聊，不回复群组或机器人。\n"
            "不会保存收到的消息内容；你手动发消息后会暂停自动回复。"
        )
    await message.reply_text(text, reply_markup=auto_settings_keyboard(account_id, config["enabled"], language))


async def auto_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await require_callback_access(update, query, context.bot):
        return
    data = (query.data or "").split(":")
    owner = owner_id(update)
    if len(data) < 2 or data[0] != "auto":
        await query.answer(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '按钮无效或已过期。', 'The button is invalid or has expired.'), show_alert=True)
        return
    action = data[1]
    await query.answer()
    message = query.message
    if action == "list":
        if message:
            rows = store.list_accounts(owner)
            language = store.get_language(owner)
            if rows:
                prompt = "Choose an account:" if language == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请选择账号：', 'Please choose an account:')
                await message.reply_text(prompt, reply_markup=auto_account_picker(rows, language))
            else:
                await message.reply_text("No accounts added yet." if language == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '目前没有已添加的账号。', 'No accounts have been added yet.'))
        return
    if action == "all":
        count = store.set_auto_reply_all(owner, True)
        store.audit(owner, "auto_reply_manual_all_on", None, f"accounts={count}")
        for row in store.list_accounts(owner):
            session = store.get_session(owner, row["id"])
            if session:
                start_monitor(owner, row["id"], session, context.application)
        if message:
            await message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已将 {0} 个账号切换为手动自动模式。人工接管某个账号后，该账号会暂停。', 'Switched {0} accounts to manual auto mode. When you take over an account manually, auto-reply for that account pauses.').format(count))
        return
    if len(data) < 3 or not data[2].isdigit():
        if message:
            await message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '账号选择无效或已过期。', 'The account selection is invalid or has expired.'))
        return
    account_id = int(data[2])
    config = store.get_auto_reply_settings(owner, account_id)
    if not config:
        if message:
            await message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到你的这个账号。', 'This account was not found.'))
        return
    if action == "acct":
        if message:
            await show_auto_reply_settings(message, owner, account_id)
        return
    if action == "toggle":
        enabled = not config["enabled"]
        store.set_auto_reply_enabled(owner, account_id, enabled, manual_auto=False)
        store.audit(owner, "auto_reply_enabled" if enabled else "auto_reply_disabled", account_id, "per-account setting")
        if enabled:
            session = store.get_session(owner, account_id)
            if session:
                start_monitor(owner, account_id, session, context.application)
        else:
            await stop_monitor_if_unused(owner, account_id)
        if message:
            await show_auto_reply_settings(message, owner, account_id)
        return
    if action == "templates":
        if message:
            language = store.get_language(owner)
            await message.reply_text(ui_text(language, '选择要编辑的场景模板：', 'Choose the scene template to edit:'), reply_markup=auto_scene_keyboard(account_id, language))
        return
    if action == "content":
        auto_flows[owner] = AutoFlow(state="simple_reply", account_id=account_id)
        if message:
            if store.get_language(owner) == "en":
                prompt = "Send the reply message (up to 1,000 characters). Send “default” to restore the built-in replies, or /cancel to stop."
            else:
                prompt = ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请发送自动回复内容（最多 1000 字）。发送“默认”可恢复默认话术，/cancel 可取消。', 'Send the auto-reply content (up to 1,000 characters). Send “默认” to restore the default wording, or /cancel to cancel.')
            await message.reply_text(prompt)
        return
    if action == "edit" and len(data) == 4 and data[3] in SCENE_LABELS:
        auto_flows[owner] = AutoFlow(state="template", account_id=account_id, scene=data[3])
        if message:
            language = store.get_language(owner)
            await message.reply_text(
                ui_text(language, '请发送“{0}”模板（最多 1000 字）。可用变量：{{name}}、{{time}}、{{product}}。发送 /cancel 取消。', 'Send the “{0}” template (up to 1,000 characters). Available variables: {{name}}, {{time}}, {{product}}. Send /cancel to cancel.').format(scene_labels(language)[data[3]])
            )
        return
    if action == "limits":
        auto_flows[owner] = AutoFlow(state="thresholds", account_id=account_id)
        if message:
            await message.reply_text(
                ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '当前：空闲 {0} 分钟、队列阈值 {1}、夜间 {2:02d}:00–{3:02d}:00。\n发送 4 个数字：空闲分钟数 队列阈值 夜间开始小时 结束小时；例如：15 3 23 9。有效范围：空闲 1–1440 分钟、队列 1–100 条、小时 0–23。发送 /cancel 取消。', 'Current: idle {0} minutes, queue threshold {1}, nighttime {2:02d}:00–{3:02d}:00.\nSend 4 numbers: idle minutes, queue threshold, nighttime start hour, end hour; for example: 15 3 23 9. Valid ranges: idle 1–1,440 minutes, queue 1–100 messages, hours 0–23. Send /cancel to cancel.').format(config['idle_minutes'], config['queue_threshold'], config['start_hour'], config['end_hour'])
            )
        return
    if action == "product":
        auto_flows[owner] = AutoFlow(state="product", account_id=account_id)
        if message:
            await message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '请发送 {product} 要替换成的产品/品牌名称（最多 120 字）；发送“清空”可移除。发送 /cancel 取消。', 'Send the product/brand name to replace {product} (up to 120 characters); send “清空” to remove it. Send /cancel to cancel.'))
        return
    if action == "mode" and len(data) == 4 and data[3] in {"auto", "sales"}:
        config["scene_mode"] = data[3]
        store.save_auto_reply_config(owner, account_id, config)
        store.audit(owner, "auto_reply_scene_mode_changed", account_id, data[3])
        if message:
            await show_auto_reply_settings(message, owner, account_id)
        return
    if action == "recent":
        items = store.list_auto_reply_messages(owner, account_id, 20)
        language = store.get_language(owner)
        if not items:
            result = f"No auto-replies yet for account #{account_id}." if language == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '账号 #{0} 暂无自动回复记录。', 'There are no auto-reply records yet for account #{0}.').format(account_id)
        else:
            lines = []
            for item in items:
                when = datetime.fromtimestamp(item["created_at"], timezone.utc).strftime("%m-%d %H:%M UTC")
                label = scene_labels(language).get(item["scene"], item["scene"] or ui_text(language, '离线', 'Offline'))
                if language == "en":
                    lines.append(f"• Chat {item['peer_id']} · message #{item['message_id']} · {label} · {when}")
                else:
                    lines.append(ui_text(language, '• auto=1 · 对话 {0} · Telegram 消息 #{1} · {2} · {3}', '• auto=1 · Chat {0} · Telegram message #{1} · {2} · {3}').format(item['peer_id'], item['message_id'], label, when))
            heading = f"Recent auto-replies for account #{account_id} (message text is not stored):\n" if language == "en" else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '账号 #{0} 最近自动回复（正文未保存）：\n', 'Recent auto-replies for account #{0} (message text is not stored):\n').format(account_id)
            result = heading + "\n".join(lines)
        if message:
            await message.reply_text(result)
        return
    return


async def auto_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    count = store.set_auto_reply_all(user, True)
    for row in store.list_accounts(user):
        session = store.get_session(user, row["id"])
        if session:
            start_monitor(user, row["id"], session, context.application)
    store.audit(user, "auto_reply_manual_all_on", None, f"accounts={count}")
    await update.effective_message.reply_text(
        ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已将 {0} 个账号切换为手动自动模式。任一账号人工发消息后，该账号会立即暂停自动回复。', 'Switched {0} accounts to manual auto mode. After you send a message from any account manually, auto-reply for that account pauses immediately.').format(count),
        reply_markup=menu_for_user(owner_id(update)),
    )


async def auto_all_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    count = store.set_auto_reply_all(user, False)
    for row in store.list_accounts(user):
        await stop_monitor_if_unused(user, row["id"])
    store.audit(user, "auto_reply_all_off", None, f"accounts={count}")
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '已关闭 {0} 个账号的自动回复。', 'Auto-reply has been turned off for {0} accounts.').format(count), reply_markup=menu_for_user(owner_id(update)))


async def notify_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    store.set_alerts(user, True)
    for row in store.list_accounts(user):
        session = store.get_session(user, row["id"])
        if session:
            start_monitor(user, row["id"], session, context.application)
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '安全通知已开启。机器人仅发送提醒，不会转发 Telegram 消息内容。', 'Safety alerts are now on. The bot only sends alerts and will not forward Telegram message content.'), reply_markup=menu_for_user(owner_id(update)))


async def notify_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    store.set_alerts(user, False)
    for row in store.list_accounts(user):
        await stop_monitor_if_unused(user, row["id"])
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '安全通知已关闭。', 'Safety alerts are now off.'), reply_markup=menu_for_user(owner_id(update)))


async def stop_monitor_if_unused(user_id: int, account_id: int) -> None:
    if store.alerts_enabled(user_id) or account_id in store.active_auto_reply_account_ids(user_id):
        return
    pair = (user_id, account_id)
    if pair in monitors:
        client, task = monitors.pop(pair)
        if task and not task.done():
            task.cancel()
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


def start_monitor(user_id: int, account_id: int, session: str, application: Application) -> None:
    key = (user_id, account_id)
    auto_enabled = account_id in store.active_auto_reply_account_ids(user_id)
    if key in monitors or not (store.alerts_enabled(user_id) or auto_enabled):
        return
    task = asyncio.create_task(watch_account(user_id, account_id, session, application, key))
    # A placeholder client is replaced as soon as the coroutine connects.
    monitors[key] = (None, task)  # type: ignore[arg-type]


async def handle_auto_reply_event(event: Any, user_id: int, account_id: int, client: TelegramClient) -> None:
    message = getattr(event, "message", None)
    if not message or not getattr(event, "is_private", False):
        return
    try:
        peer_id = int(event.chat_id)
    except (TypeError, ValueError):
        return
    if peer_id == 777000:
        return
    marker_key = (account_id, peer_id)
    if getattr(message, "out", False):
        if marker_key in auto_sending_peers or store.is_auto_reply_message(user_id, account_id, peer_id, message.id):
            return
        store.record_auto_reply_human_outgoing(user_id, account_id, peer_id, message.id)
        return

    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        return
    if getattr(sender, "bot", False) or not getattr(message, "id", None):
        return

    config = store.get_auto_reply_settings(user_id, account_id)
    if not config or not config["enabled"]:
        return
    lock = auto_reply_locks.setdefault(marker_key, asyncio.Lock())
    async with lock:
        incoming = store.record_auto_reply_incoming(user_id, account_id, peer_id, message.id)
        if not incoming["accepted"]:
            return
        now = time.time()
        reason = offline_reason(
            config,
            enabled=config["enabled"],
            manual_auto=config["manual_auto"],
            last_human_at=config["last_human_at"],
            paused_until=config["paused_until"],
            pending_count=incoming["pending_count"],
            now=now,
        )
        if reason is None:
            return
        local = local_datetime(now, config["tz_offset"])
        local_day = local.strftime("%Y%m%d")
        if not store.claim_auto_reply(
            user_id,
            account_id,
            peer_id,
            cooldown_seconds=config["cooldown_seconds"],
            daily_cap=config["daily_cap"],
            local_day=local_day,
            now=now,
        ):
            return

        try:
            await asyncio.sleep(random.uniform(0.4, 1.2))
            # Recheck after the short delay: a human may have taken over meanwhile.
            latest = store.get_auto_reply_settings(user_id, account_id)
            pending = store.auto_reply_pending_count(user_id, account_id)
            if not latest:
                store.release_auto_reply_claim(account_id, peer_id, claimed_at=now, local_day=local_day)
                return
            reason = offline_reason(
                latest,
                enabled=latest["enabled"],
                manual_auto=latest["manual_auto"],
                last_human_at=latest["last_human_at"],
                paused_until=latest["paused_until"],
                pending_count=pending,
                now=time.time(),
            )
            if reason is None:
                store.release_auto_reply_claim(account_id, peer_id, claimed_at=now, local_day=local_day)
                return
            scene = choose_scene(
                latest,
                first_contact=incoming["first_contact"],
                manual_auto=latest["manual_auto"],
                now=time.time(),
            )
            template = latest.get("simple_reply") or latest["scenes"].get(scene) or latest["scenes"].get("offline", "稍后回复你。")
            name = " ".join(
                part for part in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if part
            ).strip() or "你好"
            text = render_template(template, name=name, now=time.time(), config=latest)
            if not text:
                store.release_auto_reply_claim(account_id, peer_id, claimed_at=now, local_day=local_day)
                return
            auto_sending_peers.add(marker_key)
            sent = await client.send_message(peer_id, text)
            store.record_auto_reply_sent(user_id, account_id, peer_id, sent.id, scene)
            store.audit(user_id, "auto_reply_sent", account_id, f"scene={scene}; reason={reason}")
        except asyncio.CancelledError:
            store.release_auto_reply_claim(account_id, peer_id, claimed_at=now, local_day=local_day)
            raise
        except Exception as exc:
            store.release_auto_reply_claim(account_id, peer_id, claimed_at=now, local_day=local_day)
            log.info("Automatic reply failed (%s)", type(exc).__name__)
        finally:
            auto_sending_peers.discard(marker_key)


async def watch_account(user_id: int, account_id: int, session: str, application: Application, key: tuple[int, int]) -> None:
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return
        if key in monitors:
            monitors[key] = (client, asyncio.current_task())  # type: ignore[arg-type]

        async def on_service_message(event: Any) -> None:
            if store.alerts_enabled(user_id) and store.get_account(user_id, account_id):
                try:
                    await application.bot.send_message(
                        user_id,
                        ui_text(store.get_language(user_id), '🔔 账号 {0} 收到一条 Telegram 官方安全通知。为保护隐私，通知正文和验证码不会转发。', '🔔 Account {0} received an official Telegram security alert. To protect your privacy, the notification text and login code will not be forwarded.').format(account_id),
                    )
                    store.audit(user_id, "security_alert_sent", account_id, "service notification received")
                except Exception:
                    pass

        async def on_private_message(event: Any) -> None:
            await handle_auto_reply_event(event, user_id, account_id, client)

        client.add_event_handler(on_service_message, events.NewMessage(from_users=777000, incoming=True))
        client.add_event_handler(on_private_message, events.NewMessage(incoming=True))
        client.add_event_handler(on_private_message, events.NewMessage(outgoing=True))
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("Security watcher ended (%s)", type(exc).__name__)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        if monitors.get(key, (None, None))[1] is asyncio.current_task():
            monitors.pop(key, None)


async def audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    language = store.get_language(user_id)
    items = store.list_audit(user_id, 20)
    if not items:
        await update.effective_message.reply_text(ui_text(language, '暂时没有操作记录。', 'There is no activity history yet.'), reply_markup=menu_for_user(user_id))
        return
    action_labels = {
        "accounts_viewed": ("查看账号", "Viewed accounts"),
        "qr_login_started": ("发起二维码登录", "Started QR login"),
        "auto_reply_simple_message_changed": ("修改自动回复内容", "Changed auto-reply message"),
        "auto_reply_template_changed": ("修改场景模板", "Changed scene template"),
        "auto_reply_product_changed": ("修改产品名称", "Changed product name"),
        "auto_reply_triggers_changed": ("修改自动回复条件", "Changed auto-reply conditions"),
        "login_code_checked": ("查询登录验证码", "Checked login code"),
        "devices_viewed": ("查看登录设备", "Viewed login devices"),
        "twofa_status_checked": ("查看两步验证状态", "Checked two-step verification"),
        "account_search": ("搜索账号", "Searched accounts"),
        "auto_reply_manual_all_on": ("开启全部账号手动自动模式", "Enabled manual auto mode for all accounts"),
        "auto_reply_enabled": ("开启自动回复", "Enabled auto-reply"),
        "auto_reply_disabled": ("关闭自动回复", "Disabled auto-reply"),
        "auto_reply_scene_mode_changed": ("修改自动回复场景模式", "Changed auto-reply scene mode"),
        "auto_reply_all_off": ("关闭全部账号自动回复", "Disabled auto-reply for all accounts"),
        "auto_reply_sent": ("发送自动回复", "Sent an auto-reply"),
        "security_alert_sent": ("收到安全提醒", "Received a security alert"),
    }
    lines = []
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], timezone.utc).strftime("%m-%d %H:%M UTC")
        suffix = ui_text(language, ' · 账号 #{0}', ' · Account #{0}').format(item['account_id']) if item["account_id"] else ""
        labels = action_labels.get(item["action"], (item["action"].replace("_", " "), item["action"].replace("_", " ").capitalize()))
        lines.append(f"• {when} · {ui_text(language, *labels)}{suffix}")
    heading = ui_text(language, '🧾 最近操作记录（只记录动作，不含验证码或密码）', '🧾 Recent activity (actions only; no verification codes or passwords)')
    await update.effective_message.reply_text(heading + "\n" + "\n".join(lines), reply_markup=menu_for_user(user_id))


async def trash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    rows = [r for r in store.list_accounts(owner_id(update), include_deleted=True) if r["deleted_at"]]
    language = store.get_language(owner_id(update))
    if not rows:
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '回收站为空。删除账号使用 /delete 账号ID；恢复使用 /restore 账号ID。', 'The trash is empty. To delete an account, use /delete account ID; to restore one, use /restore account ID.'), reply_markup=menu_for_user(owner_id(update)))
        return
    await update.effective_message.reply_text(
        ui_text(language, '🗑 回收站（仅你可见）', '🗑 Trash (visible only to you)') + "\n"
        + "\n".join(format_account(r, language) for r in rows)
        + "\n\n" + ui_text(language, '恢复：/restore 账号ID', 'Restore: /restore account ID'),
        reply_markup=menu_for_user(owner_id(update)),
    )


async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/delete 账号ID（移入回收站，可恢复）', 'Format: /delete account ID (moves it to the trash; can be restored)'))
        return
    user, account_id = owner_id(update), int(context.args[0])
    ok = store.delete_account(user, account_id)
    pair = (user, account_id)
    if pair in monitors:
        client, task = monitors.pop(pair)
        task.cancel()
        if client:
            await client.disconnect()
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '账号已移入回收站；会话仍加密保留，可用 /restore 恢复。', 'The account was moved to the trash; its session is still retained in encrypted form and can be restored with /restore.') if ok else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '没有找到你的这个账号。', 'This account was not found.'), reply_markup=menu_for_user(owner_id(update)))


async def restore_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '格式：/restore 账号ID', 'Format: /restore account ID'))
        return
    user, account_id = owner_id(update), int(context.args[0])
    ok = store.restore_account(user, account_id)
    if ok and (store.alerts_enabled(user) or account_id in store.active_auto_reply_account_ids(user)):
        session = store.get_session(user, account_id)
        if session:
            start_monitor(user, account_id, session, context.application)
    await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '账号已恢复。', 'The account has been restored.') if ok else ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '回收站中没有找到你的这个账号。', 'That account was not found in your trash.'), reply_markup=menu_for_user(owner_id(update)))


async def rename_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    language = store.get_language(owner_id(update))
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text(ui_text(language, '格式：/rename 账号ID 新备注', 'Format: /rename account ID new label'))
        return
    account_id = int(context.args[0])
    label = " ".join(context.args[1:]).strip()
    ok = store.update_label(owner_id(update), account_id, label)
    await update.effective_message.reply_text(ui_text(language, '账号备注已更新。', 'Account label updated.') if ok else ui_text(language, '没有找到你的这个账号。', 'This account was not found.'), reply_markup=menu_for_user(owner_id(update)))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if store.get_language(owner_id(update)) == "en":
        text = (
            "Commands\n/accounts — my accounts\n/add — add your own Telegram account\n/qr — QR login\n"
            "/code — retrieve a login code only from your matching saved account\n/resend — resend only when Telegram allows it\n"
            "/groups, /group_create, /move — manage account groups\n/rename — rename an account\n"
            "/devices — login devices\n/2fa — 2FA status\n/search — find an account\n/trash, /delete, /restore — manage deleted accounts\n"
            "/autoreply — simple per-account auto-reply settings\n/language — switch Chinese/English\n"
            "/notify_on, /notify_off — security alerts\n/audit — activity\n/cancel — cancel the current step\n\n"
            "Before first use, complete a one-time human check and join the official channel."
            " Auto-reply is off by default, works only in private chats, and never stores incoming message text."
        )
    else:
        text = (
            "命令说明\n/accounts — 我的账号\n/add — 添加本人账号\n/qr — QR 登录\n"
            "/resend — 仅在 Telegram 允许时重发验证码\n/code — 只从本人匹配的已保存账号读取登录码\n"
            "/groups、/group_create、/move — 管理账号分组\n/rename — 修改账号备注\n"
            "/devices — 登录设备\n/2fa — 2FA 状态\n/search — 搜索账号\n/trash、/delete、/restore — 管理已删除账号\n"
            "/autoreply — 简单的逐账号自动回复设置\n/language — 切换中文/English\n"
            "/notify_on、/notify_off — 安全通知\n/audit — 操作记录\n/cancel — 取消当前步骤\n\n"
            "首次使用需完成一次人机验证并加入官方频道。自动回复默认关闭，只处理私聊，不保存收到的消息正文。"
        )
    await update.effective_message.reply_text(text, reply_markup=menu_for_user(owner_id(update)))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context.bot):
        await update.effective_message.reply_text(ui_text(update.effective_user and store.get_language(update.effective_user.id) or "zh", '未知命令，请发送 /help。', 'Unknown command. Please send /help.'), reply_markup=menu_for_user(owner_id(update)))


async def on_startup(application: Application) -> None:
    # Restore only the per-account watchers needed for security alerts or enabled auto replies.
    for user_id in store.active_owner_ids():
        if not is_allowed_user_id(user_id, ALLOWED_TELEGRAM_USER_IDS):
            continue
        auto_ids = set(store.active_auto_reply_account_ids(user_id))
        if not store.alerts_enabled(user_id) and not auto_ids:
            continue
        for row in store.list_accounts(user_id):
            if not store.alerts_enabled(user_id) and row["id"] not in auto_ids:
                continue
            session = store.get_session(user_id, row["id"])
            if session:
                start_monitor(user_id, row["id"], session, application)


async def on_shutdown(application: Application) -> None:
    for client, task in list(monitors.values()):
        if task and not task.done():
            task.cancel()
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
    monitors.clear()
    for user_id in list(flows):
        await cleanup_flow(user_id)
    auto_flows.clear()
    auto_reply_locks.clear()
    auto_sending_peers.clear()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Do not log exception values/tracebacks or Telegram update bodies; they may contain secrets.
    log.error("Request failed (%s)", type(context.error).__name__ if context.error else "unknown")


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CommandHandler("accounts", accounts))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("qr", qr_start))
    app.add_handler(CommandHandler("consent", consent_login))
    app.add_handler(CommandHandler("code", code_command))
    app.add_handler(CommandHandler("resend", resend_code))
    app.add_handler(CommandHandler("groups", groups))
    app.add_handler(CommandHandler("group_create", group_create))
    app.add_handler(CommandHandler("move", move_account))
    app.add_handler(CommandHandler("devices", devices))
    app.add_handler(CommandHandler("2fa", twofa))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("autoreply", auto_reply_entry))
    app.add_handler(CommandHandler("auto_all", auto_all))
    app.add_handler(CommandHandler("auto_all_off", auto_all_off))
    app.add_handler(CommandHandler("notify_on", notify_on))
    app.add_handler(CommandHandler("notify_off", notify_off))
    app.add_handler(CommandHandler("audit", audit))
    app.add_handler(CommandHandler("trash", trash))
    app.add_handler(CommandHandler("delete", delete_account))
    app.add_handler(CommandHandler("restore", restore_account))
    app.add_handler(CommandHandler("rename", rename_account))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(human_captcha_callback, pattern=r"^human:"))
    app.add_handler(CallbackQueryHandler(channel_check_callback, pattern=r"^channel:"))
    app.add_handler(CallbackQueryHandler(language_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(country_picker_callback, pattern=r"^ctry:"))
    app.add_handler(CallbackQueryHandler(account_picker_callback, pattern=r"^acctpick:"))
    app.add_handler(CallbackQueryHandler(auto_reply_callback, pattern=r"^auto:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
