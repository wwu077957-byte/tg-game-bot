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
from .auto_reply import SCENE_LABELS, choose_scene, local_datetime, normalize_config, offline_reason, render_template
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

def menu_for(language: str) -> ReplyKeyboardMarkup:
    if language == "en":
        rows = [
            ["📱 My accounts", "➕ Add account"],
            ["🔳 QR login", "📩 Get code"],
            ["📁 Groups", "💻 Devices"],
            ["🛡 2FA status", "🔎 Search"],
            ["🤖 Auto-reply", "🔔 Safety alerts"],
            ["🧾 Activity", "🗑 Trash"],
            ["🌐 语言 / Language", "❔ Help"],
        ]
        placeholder = "Choose a feature or send a command"
    else:
        rows = [
            ["📱 账号列表", "➕ 添加账号"],
            ["🔳 QR 登录", "📩 获取验证码"],
            ["📁 账号分组", "💻 设备会话"],
            ["🛡 2FA 状态", "🔎 搜索账号"],
            ["🤖 自动回复", "🔔 通知与健康"],
            ["🧾 操作记录", "🗑 回收站"],
            ["🌐 语言 / Language", "❔ 帮助"],
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
            await query.answer("无权使用 / Not allowed", show_alert=True)
        return
    if store.is_human_verified(user.id):
        await query.answer("已验证 / Already verified")
        return
    challenge = pending_human_challenges.get(user.id)
    try:
        selected = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        selected = -1
    if not challenge or challenge[2] <= time.monotonic():
        pending_human_challenges.pop(user.id, None)
        await query.answer("验证已过期，请重新验证。 / Check expired.", show_alert=True)
        if query.message:
            prompt, keyboard = human_challenge(user.id, store.get_language(user.id))
            await query.message.reply_text(prompt, reply_markup=keyboard)
        return
    if selected != challenge[0] or selected not in challenge[1]:
        pending_human_challenges.pop(user.id, None)
        await query.answer("答案不正确，再试一次。 / Try again.", show_alert=True)
        if query.message:
            prompt, keyboard = human_challenge(user.id, store.get_language(user.id))
            await query.message.reply_text(prompt, reply_markup=keyboard)
        return
    store.mark_human_verified(user.id)
    pending_human_challenges.pop(user.id, None)
    await query.answer("验证通过 / Verified")
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
            await query.answer("无权使用 / Not allowed", show_alert=True)
        return
    if not await require_private(update, context.bot):
        await query.answer()
        return
    language = (query.data or "").split(":", 1)[-1]
    if language not in {"zh", "en"}:
        await query.answer("Invalid language", show_alert=True)
        return
    store.set_language(user.id, language)
    await query.answer("已切换 / Updated")
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
            await query.answer("无权使用 / Not allowed", show_alert=True)
        return
    if not await require_private(update, context.bot):
        await query.answer()
        return
    language = store.get_language(user.id)
    await query.answer("验证通过 / Verified")
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


def country_start_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        ("美国 +1", "US"),
        ("加拿大 +1", "CA"),
        ("中国 +86", "CN"),
        ("英国 +44", "GB"),
    ]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"ctry:select:{region}") for label, region in buttons[:2]],
            [InlineKeyboardButton(label, callback_data=f"ctry:select:{region}") for label, region in buttons[2:]],
            [InlineKeyboardButton("🔎 搜索国家/地区", callback_data="ctry:search")],
            [InlineKeyboardButton("🌍 浏览全部国家/地区", callback_data="ctry:all:0")],
        ]
    )


def country_results_keyboard(regions: list[str], page: int) -> InlineKeyboardMarkup:
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
                        f"{country.name} {country.calling_code}",
                        callback_data=f"ctry:select:{country.region}",
                    )
                ]
            )
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("‹ 上一页", callback_data=f"ctry:page:{page - 1}"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton("下一页 ›", callback_data=f"ctry:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("🔎 重新搜索", callback_data="ctry:search")])
    return InlineKeyboardMarkup(rows)


def phone_identity(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def matching_saved_accounts(user_id: int, phone: str) -> list[dict[str, Any]]:
    identity = phone_identity(phone)
    return [row for row in store.list_accounts(user_id) if phone_identity(row["phone"]) == identity]


def code_delivery_guidance(code_type: Any) -> str:
    if isinstance(code_type, types.auth.SentCodeTypeApp):
        return "投递方式：Telegram 官方服务消息。请检查该账号其他已登录设备上与 777000 的对话。"
    if isinstance(code_type, types.auth.SentCodeTypeSms):
        return "投递方式：短信。请检查此号码对应的 SIM 卡、垃圾短信/拦截箱及双卡收件卡槽。"
    if isinstance(code_type, types.auth.SentCodeTypeCall):
        return "投递方式：语音电话。请留意来电并按语音提示记录验证码。"
    if isinstance(code_type, types.auth.SentCodeTypeFlashCall):
        pattern = getattr(code_type, "pattern", "")
        extra = f" Telegram 返回的来电号码匹配规则：{pattern}。" if pattern else ""
        return "投递方式：闪呼，电话可能响一声后挂断；按 Telegram 要求核对来电号码。" + extra
    if isinstance(code_type, types.auth.SentCodeTypeMissedCall):
        return "投递方式：未接来电。请查看来电记录，并按 Telegram 要求取来电号码末尾数字作为验证码。"
    if isinstance(code_type, types.auth.SentCodeTypeEmailCode):
        pattern = getattr(code_type, "email_pattern", "")
        destination = f" Telegram 显示的邮箱：{pattern}。" if pattern else " 请检查 Telegram 账号绑定的登录邮箱。"
        return "投递方式：邮箱。" + destination
    if isinstance(code_type, types.auth.SentCodeTypeSmsWord):
        return "投递方式：短信单词码。请把短信中的单词原样作为验证码输入，不要改成数字。"
    if isinstance(code_type, types.auth.SentCodeTypeSmsPhrase):
        return "投递方式：短信短语码。请把短信中的完整短语原样输入。"
    if isinstance(code_type, types.auth.SentCodeTypeFragmentSms):
        return "投递方式：Telegram 指向 Fragment。此 Bot 不会访问或读取外部钱包/网站；请仅按 Telegram 官方客户端提示继续，或改用二维码登录。"
    if isinstance(code_type, types.auth.SentCodeTypeFirebaseSms):
        return "投递方式：官方移动端 Firebase 验证。该流程需要官方应用的设备完整性凭据，Bot 无法代办；若 Telegram 提供下一通道，可在等待后主动重发，否则请用官方客户端或二维码登录。"
    if isinstance(code_type, types.auth.SentCodeTypeSetUpEmailRequired):
        return "Telegram 要求先设置或验证登录邮箱；当前 Bot 不会代为设置邮箱。请在 Telegram 官方客户端完成后，再重新开始登录。"
    return "Telegram 返回了此 Bot 未支持的验证码投递方式；请在 Telegram 官方客户端继续，或改用二维码登录。"


def code_type_label(code_type: Any) -> str:
    labels = {
        "CodeTypeSms": "短信",
        "CodeTypeCall": "语音电话",
        "CodeTypeFlashCall": "闪呼",
        "CodeTypeMissedCall": "未接来电",
        "CodeTypeFragmentSms": "Fragment",
    }
    name = type(code_type).__name__
    return labels.get(name, f"Telegram 返回的 {name} 通道")


async def require_private(update: Update, bot: Any) -> bool:
    user = update.effective_user
    if not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS):
        return False
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("为保护账号隐私，请只在机器人私聊中操作。")
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
        await query.answer("无权使用此机器人。", show_alert=True)
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
            "我没有把这段输入写入账号数据库或日志；Telegram 未允许机器人删除该消息。"
            "请你在聊天中手动删除刚才的验证码/密码消息。"
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


def format_account(row: dict[str, Any]) -> str:
    phone = Store.mask_phone(row["phone"])
    label = (row.get("label") or "").strip()
    group = row.get("group_name")
    bits = [f"#{row['id']} · {phone}"]
    if label:
        bits.append(label)
    if group:
        bits.append(f"分组：{group}")
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
            f"🔐 TGVault 账号管理\n你好，{name}。每位使用者只能看到和操作自己添加的账号。\n\n"
            "登录前会先说明托管风险并请求你的明确同意；验证码和 2FA 密码不会写入数据库或日志。\n"
            "选择下方菜单，或发送 /help 查看命令。"
        )
    await update.effective_message.reply_text(
        greeting,
        reply_markup=menu_for_user(owner_id(update)),
    )


async def accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    rows = store.list_accounts(owner_id(update))
    if not rows:
        await update.effective_message.reply_text("目前没有已添加的账号。使用 /add 添加本人账号。", reply_markup=menu_for_user(owner_id(update)))
        return
    text = "📱 你的账号（仅你可见）\n\n" + "\n".join(format_account(row) for row in rows)
    await update.effective_message.reply_text(text, reply_markup=menu_for_user(owner_id(update)))
    store.audit(owner_id(update), "accounts_viewed", None, "account list")


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    await cleanup_flow(user_id)
    flows[user_id] = Flow(state="consent", login_method="phone")
    await update.effective_message.reply_text(
        "登录前的重要说明：Bot 和服务器运营者能够访问可用的 Telegram 登录会话，并可借此访问该账号允许的 Telegram 功能。"
        "会话虽加密存储，但这不是端到端加密；请只在你信任运营者和服务器时继续，并且只登录你本人拥有的账号。\n\n"
        "如理解并同意，请发送 /consent；否则发送 /cancel。"
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
            "登录前的重要说明：Bot 和服务器运营者能够访问可用的 Telegram 登录会话，并可借此访问该账号允许的 Telegram 功能。"
            "会话虽加密存储，但这不是端到端加密；请只在你信任运营者和服务器时继续，并且只登录你本人拥有的账号。\n\n"
            "如理解并同意，请发送 /consent；否则发送 /cancel。"
        )
        return
    wait_seconds = consume_qr_login_cooldown(user_id)
    if wait_seconds:
        await update.effective_message.reply_text(f"为避免触发 Telegram 登录限制，请约 {wait_seconds} 秒后再发起登录。")
        await cleanup_flow(user_id)
        return
    wait_seconds = store.begin_auth_attempt(user_id, "code", account_phone="qr")
    if wait_seconds:
        await update.effective_message.reply_text(f"Telegram 要求二维码登录冷却，请再等约 {wait_seconds} 秒。")
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
            caption="用你本人已登录的 Telegram 官方客户端扫描此二维码。二维码等同登录授权，请勿转发或截图分享。"
            "如账号启用 2FA，扫描后会在此私聊要求输入密码。",
        )
        store.audit(user_id, "qr_login_started", None, "QR flow started")
    except FloodWaitError as exc:
        store.defer_auth_attempt(user_id, "code", exc.seconds, account_phone="qr")
        await cleanup_flow(user_id)
        await update.effective_message.reply_text(f"Telegram 要求等待 {exc.seconds} 秒；已记录等待时间，不会提前重试。")
    except Exception:
        await cleanup_flow(user_id)
        await update.effective_message.reply_text("无法启动二维码登录。请稍后重试，或使用 /add 通过手机号登录。")


async def consent_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    flow = flows.get(user_id)
    if not flow or flow.state != "consent" or flow.login_method not in {"phone", "qr"}:
        await update.effective_message.reply_text("当前没有等待确认的登录流程。发送 /add 或 /qr 可开始。")
        return
    if flow.login_method == "phone":
        flow.state = "country"
        flow.country_matches = ["US", "CA", "CN", "GB"]
        flow.country_page = 0
        flow.started_at = time.monotonic()
        await update.effective_message.reply_text(
            "➕ 添加本人 Telegram 账号\n先选择号码所属国家/地区；也可以搜索中文名、英文名或国际区号。"
            "美国与加拿大共用 +1，之后会按区号/号段再核对一次。\n只能添加你本人拥有并授权的账号。/cancel 可取消。",
            reply_markup=country_start_keyboard(),
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
    flow = flows.get(user_id)
    data = query.data or ""
    if flow and flow_is_expired(flow):
        await cleanup_flow(user_id)
        await query.answer("登录流程已过期，请重新发送 /add。", show_alert=True)
        return
    if not flow or (
        flow.state not in {"country", "country_search", "country_results"}
        and not (flow.state == "phone" and data == "ctry:change")
    ):
        await query.answer("国家选择已过期，请重新发送 /add。", show_alert=True)
        return
    if data == "ctry:change":
        flow.state = "country"
        flow.selected_country = None
        flow.country_matches = ["US", "CA", "CN", "GB"]
        flow.country_page = 0
        await query.answer()
        if query.message:
            await query.edit_message_text(
                "重新选择号码所属国家/地区；可以搜索中文名、英文名或国际区号。",
                reply_markup=country_start_keyboard(),
            )
        return
    if data == "ctry:search":
        flow.state = "country_search"
        await query.answer()
        if query.message:
            await query.edit_message_text("请发送国家/地区名称（中文或英文），也可以发送区号，例如 +1。/cancel 可取消。")
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
            await query.answer("页面无效。", show_alert=True)
            return
        pages = max(1, (len(flow.country_matches) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
        if page < 0 or page >= pages:
            await query.answer("页面已过期，请重新搜索。", show_alert=True)
            return
        flow.country_page = page
        await query.answer()
    elif data.startswith("ctry:select:"):
        region = data.rsplit(":", 1)[1].upper()
        if flow.state not in {"country", "country_results"} or region not in flow.country_matches or not get_country(region):
            await query.answer("国家选择无效或已过期。", show_alert=True)
            return
        country = get_country(region)
        assert country is not None
        flow.selected_country = region
        flow.state = "phone"
        flow.country_matches = []
        flow.started_at = time.monotonic()
        await query.answer("已选择")
        if query.message:
            await query.edit_message_text(
                f"已选择：{country.name}（{country.calling_code}）。\n"
                "请发送手机号，可输入本地格式，也可输入以 + 开头的完整国际号码。\n"
                "号码会按所选国家/地区校验；/cancel 可取消。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("更换国家/地区", callback_data="ctry:change")]]
                ),
            )
        return
    else:
        await query.answer("按钮无效或已过期。", show_alert=True)
        return

    if query.message:
        total_pages = max(1, (len(flow.country_matches) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
        await query.edit_message_text(
            f"请选择国家/地区（第 {flow.country_page + 1}/{total_pages} 页）：",
            reply_markup=country_results_keyboard(flow.country_matches, flow.country_page),
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
                "此账号启用了两步验证。请在此私聊发送 2FA 密码；系统不会保存该密码。处理后请删除聊天中的密码消息。",
            )
    except FloodWaitError as exc:
        if flows.get(user_id) is flow:
            store.defer_auth_attempt(user_id, "code", exc.seconds, account_phone="qr")
            await cleanup_flow(user_id)
            await application.bot.send_message(user_id, f"Telegram 要求等待 {exc.seconds} 秒；已记录该等待时间，不会自动重试。")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if flows.get(user_id) is flow:
            await cleanup_flow(user_id)
            if isinstance(exc, asyncio.TimeoutError):
                message = "二维码登录已超时，重新发送 /qr 获取新二维码。"
            else:
                message = "二维码登录未完成，请重新发送 /qr。"
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
            f"✅ 已安全添加账号 #{account_id}（{display}）。会话已加密保存；验证码和 2FA 密码不会保存。"
            + ("安全登录提醒已开启。" if alerts else "安全登录提醒当前关闭。"),
            reply_markup=menu_for_user(user_id),
        )
        if alerts:
            start_monitor(user_id, account_id, session_string, application)
    except Exception as exc:
        # Do not log or return raw exception text; Telegram exceptions can contain sensitive context.
        log.warning("Account save failed (%s)", type(exc).__name__)
        await application.bot.send_message(user_id, "登录成功后保存账号失败。请检查配置后重试；未保存任何验证码或密码。")
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
        "🤖 自动回复": auto_reply_entry,
        "🤖 Auto-reply": auto_reply_entry,
        "🔔 通知与健康": settings,
        "🔔 Safety alerts": settings,
        "🧾 操作记录": audit,
        "🧾 Activity": audit,
        "🗑 回收站": trash,
        "🗑 Trash": trash,
        "❔ 帮助": help_command,
        "❔ Help": help_command,
        "🌐 语言 / Language": language_command,
    }
    if text in menu_handlers:
        await menu_handlers[text](update, context)
        return
    user_id = owner_id(update)
    auto_flow = auto_flows.get(user_id)
    if auto_flow:
        if time.monotonic() - auto_flow.started_at > FLOW_TTL_SECONDS:
            auto_flows.pop(user_id, None)
            expired = "Auto-reply setup expired. Open /autoreply again." if store.get_language(user_id) == "en" else "自动回复设置已超时取消，请重新打开 /autoreply。"
            await update.effective_message.reply_text(expired)
            return
        if auto_flow.state == "simple_reply":
            if not text or len(text) > 1000:
                prompt = "Reply must be 1–1000 characters. Send it again, or /cancel." if store.get_language(user_id) == "en" else "回复内容需为 1–1000 个字符，请重新发送，或用 /cancel 取消。"
                await update.effective_message.reply_text(prompt)
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text("Account not found." if store.get_language(user_id) == "en" else "没有找到你的这个账号。")
                return
            if text.strip().casefold() in {"默认", "恢复默认", "default", "reset", "clear"}:
                config["simple_reply"] = ""
            else:
                config["simple_reply"] = text
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_simple_message_changed", auto_flow.account_id, "simple reply updated")
            auto_flows.pop(user_id, None)
            saved = "Reply saved." if store.get_language(user_id) == "en" else "自动回复内容已保存。"
            await update.effective_message.reply_text(saved, reply_markup=menu_for_user(user_id))
            return
        if auto_flow.state == "template":
            if not text or len(text) > 1000:
                await update.effective_message.reply_text("模板不能为空，最多 1000 个字符；请重新发送。")
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text("没有找到你的这个账号。")
                return
            config["scenes"][auto_flow.scene or "offline"] = text
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_template_changed", auto_flow.account_id, f"scene={auto_flow.scene}")
            auto_flows.pop(user_id, None)
            await update.effective_message.reply_text("场景模板已保存。支持 {name}、{time}、{product}。", reply_markup=menu_for_user(owner_id(update)))
            return
        if auto_flow.state == "product":
            if len(text) > 120:
                await update.effective_message.reply_text("产品/品牌名最多 120 个字符，请重新发送。")
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text("没有找到你的这个账号。")
                return
            config["product"] = "" if text.strip().casefold() in {"清空", "clear", "-"} else text
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_product_changed", auto_flow.account_id, "product updated")
            auto_flows.pop(user_id, None)
            await update.effective_message.reply_text("{product} 的替换内容已保存。", reply_markup=menu_for_user(owner_id(update)))
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
                    "格式无效。请按“空闲分钟数 排队阈值 夜间开始小时 结束小时”填写，例如：15 3 23 9。"
                )
                return
            config = store.get_auto_reply_settings(user_id, auto_flow.account_id)
            if not config:
                auto_flows.pop(user_id, None)
                await update.effective_message.reply_text("没有找到你的这个账号。")
                return
            config.update({"idle_minutes": idle, "queue_threshold": queue, "start_hour": start_hour, "end_hour": end_hour})
            store.save_auto_reply_config(user_id, auto_flow.account_id, config)
            store.audit(user_id, "auto_reply_triggers_changed", auto_flow.account_id, "thresholds updated")
            auto_flows.pop(user_id, None)
            await update.effective_message.reply_text(
                f"触发条件已保存：连续 {idle} 分钟无人工操作；未回复队列超过 {queue} 条；夜间 {start_hour:02d}:00–{end_hour:02d}:00。",
                reply_markup=menu_for_user(owner_id(update)),
            )
            return
    flow = flows.get(user_id)
    if not flow:
        await update.effective_message.reply_text("请选择菜单功能，或发送 /help。", reply_markup=menu_for_user(owner_id(update)))
        return
    if flow_is_expired(flow):
        expired_state = flow.state
        await cleanup_flow(user_id)
        if expired_state in {"consent", "country", "country_search", "country_results", "phone", "code", "password", "qr_authorized", "qr_wait", "qr_password"}:
            await update.effective_message.reply_text(
                "登录流程已过期；旧验证码/登录请求不再继续使用。请重新发送 /add，或发送 /qr 并再次确认风险。"
            )
        else:
            await update.effective_message.reply_text("操作已超时取消，请重新开始。")
        return
    if flow.state == "consent":
        await update.effective_message.reply_text("若你理解并接受上面的托管风险，请发送 /consent；否则发送 /cancel。")
        return
    if flow.state == "country_search":
        matches = search_countries(text)
        if not matches:
            await update.effective_message.reply_text("没有找到匹配项。请换用中文名、英文名或区号搜索；/cancel 可取消。")
            return
        flow.country_matches = [country.region for country in matches]
        flow.country_page = 0
        flow.state = "country_results"
        pages = max(1, (len(flow.country_matches) + COUNTRY_PAGE_SIZE - 1) // COUNTRY_PAGE_SIZE)
        await update.effective_message.reply_text(
            f"找到 {len(matches)} 个国家/地区（第 1/{pages} 页），请选择：",
            reply_markup=country_results_keyboard(flow.country_matches, 0),
        )
        return
    if flow.state in {"country", "country_results"}:
        await update.effective_message.reply_text("请点选国家/地区按钮；如需搜索，请点“搜索国家/地区”。/cancel 可取消。")
        return
    if flow.state in {"code", "password", "qr_password"}:
        removed = await delete_user_input(update)
        await send_secret_handling_note(update, removed)
        secret = text.strip()
        if not secret or secret.startswith("/"):
            await context.bot.send_message(user_id, "输入为空或格式不正确；可发送 /cancel 取消。")
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
            await update.effective_message.reply_text("请先选择号码所属国家/地区。", reply_markup=country_start_keyboard())
            return
        try:
            phone = normalize_phone_for_country(text, flow.selected_country)
        except PhoneInputError as exc:
            await update.effective_message.reply_text(
                str(exc) + " 请检查号码或重新选择国家。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("更换国家/地区", callback_data="ctry:change")]]
                ),
            )
            return
        await delete_user_input(update)
        wait_seconds, request_count = store.reserve_phone_code_request(user_id, phone)
        if wait_seconds:
            await context.bot.send_message(
                user_id,
                f"这个号码的验证码请求仍在独立冷却中，请再等约 {wait_seconds} 秒。"
                "为避免 Telegram 限流，不会提前重试；也可发送 /qr 改用二维码登录。",
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
                    "Telegram 当前要求官方客户端完成额外步骤；Bot 不会处理付费或订阅。请使用官方客户端或二维码登录。",
                )
                return
            if isinstance(result, types.auth.SentCodeSuccess):
                await finish_login(user_id, flow, context.application)
                return
            if isinstance(getattr(result, "type", None), types.auth.SentCodeTypeSetUpEmailRequired):
                guidance = code_delivery_guidance(result.type)
                await cleanup_flow(user_id)
                await context.bot.send_message(user_id, guidance + "完成后可重新发送 /add，或发送 /qr 并再次确认风险。")
                return
            if not getattr(result, "phone_code_hash", None):
                raise RuntimeError("Telegram did not return a phone code hash")
            flow.phone_code_hash = result.phone_code_hash
            flow.current_code_type = result.type
            flow.next_code_type = result.next_type
            flow.state = "code"
            flow.started_at = time.monotonic()
            delivery = code_delivery_guidance(result.type)
            message = f"Telegram 为 {Store.mask_phone(phone)} 返回的验证码投递方式：\n{delivery}\n\n"
            message += "如果此方式已送达，请在此私聊按提示输入验证码；验证码会在处理后尝试删除，不会保存。"
            if result.next_type is not None:
                delay = max(45, int(result.timeout or 0))
                flow.resend_ready_at = time.monotonic() + delay
                store.defer_phone_code_request(user_id, phone, delay)
                message += (
                    f"\n\nTelegram 提供的下一种通道是：{code_type_label(result.next_type)}。"
                    f"返回的等待时间为 {int(result.timeout or 0)} 秒；本 Bot 至少等待 45 秒。"
                    "若仍未收到，等候结束后由你主动发送 /resend；最多再发送 4 次，不会自动连续重发。"
                )
            else:
                flow.resend_ready_at = 0.0
                message += "\n\nTelegram 未返回可用的下一通道；本 Bot 不会强制切换，请发送 /qr 使用二维码登录。"
            markup = None
            if isinstance(result.type, types.auth.SentCodeTypeApp):
                matching = matching_saved_accounts(user_id, phone)
                if matching:
                    markup = build_account_picker(matching, "code", format_account)
                    message += "\n\n如需从本人已保存的同号会话读取 777000，请明确点选下方按钮；不会自动读取。"
            await context.bot.send_message(user_id, message + "\n/cancel 取消。", reply_markup=markup)
        except PhoneNumberInvalidError:
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, "Telegram 判定号码无效；请重新发送 /add 并核对国家和号码。该号码冷却仍会生效。")
        except FloodWaitError as exc:
            store.defer_phone_code_request(user_id, phone, exc.seconds)
            await cleanup_flow(user_id)
            await context.bot.send_message(
                user_id,
                f"Telegram 要求等待 {exc.seconds} 秒；已为这个号码记录该等待时间，不会提前重试。"
                "等待结束后可发送 /add 重试，或发送 /qr 改用二维码登录。",
            )
        except Exception as exc:
            log.warning("Phone login initialization failed (%s)", type(exc).__name__)
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, "无法发起登录请求。请检查 Telegram API 配置、网络和账号状态；也可发送 /qr。")
        return
    if flow.state == "search":
        flows.pop(user_id, None)
        await run_search(update, text)
        return
    if flow.state == "rename":
        match = re.match(r"^(\d+)\s+(.+)$", text)
        flows.pop(user_id, None)
        if not match:
            await update.effective_message.reply_text("格式：账号ID 新备注，例如 2 工作号")
            return
        ok = store.update_label(user_id, int(match.group(1)), match.group(2))
        await update.effective_message.reply_text("备注已更新。" if ok else "没有找到你的这个账号。", reply_markup=menu_for_user(owner_id(update)))
        return
    if flow.state == "qr_wait":
        await update.effective_message.reply_text("请用 Telegram 官方客户端扫描刚才的二维码，或发送 /cancel。")
        return


async def submit_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow, code: str) -> None:
    user_id = owner_id(update)
    if not flow.phone:
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, "手机号登录流程已失效，请重新发送 /add；或发送 /qr 使用二维码登录。")
        return
    wait_seconds = store.begin_auth_attempt(user_id, "code", account_phone=flow.phone)
    if wait_seconds:
        await context.bot.send_message(user_id, f"验证码尝试正在冷却，请约 {wait_seconds} 秒后再试。")
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
            "此账号启用了两步验证。请发送 2FA 密码以继续；它只用于本次登录，不会保存。处理后请删除聊天中的密码消息。",
        )
    except PhoneCodeInvalidError:
        wait_seconds = store.record_auth_failure(user_id, "code", account_phone=flow.phone)
        if wait_seconds:
            await cleanup_flow(user_id)
            await context.bot.send_message(
                user_id,
                f"验证码连续错误次数已达上限，登录流程已取消；请在 {wait_seconds} 秒冷却后重新开始。",
            )
        else:
            await context.bot.send_message(user_id, "验证码不正确。每次尝试间至少冷却 5 秒；达到上限后会暂时锁定。")
    except PhoneCodeExpiredError:
        store.clear_auth_attempts(user_id, "code", flow.phone)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, "验证码已过期，发送 /add 获取新的验证码。")
    except FloodWaitError as exc:
        store.defer_auth_attempt(user_id, "code", exc.seconds, account_phone=flow.phone)
        store.defer_phone_code_request(user_id, flow.phone, exc.seconds)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, f"Telegram 要求等待 {exc.seconds} 秒；已为该号码记录等待时间，不会提前重试。")
    except Exception as exc:
        log.warning("Phone code sign-in failed (%s)", type(exc).__name__)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, "登录未完成。请检查验证码是否过期，之后重新发送 /add。")


async def submit_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow, password: str) -> None:
    user_id = owner_id(update)
    # Telethon does not expose the QR target account before authorization completes;
    # share a conservative owner-scoped QR lock instead of letting new QR tokens bypass it.
    auth_scope = flow.phone or "qr"
    wait_seconds = store.begin_auth_attempt(user_id, "password", account_phone=auth_scope)
    if wait_seconds:
        await context.bot.send_message(user_id, f"2FA 尝试正在冷却，请约 {wait_seconds} 秒后再试。")
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
                f"2FA 密码连续错误次数已达上限，登录流程已取消；请在 {wait_seconds} 秒冷却后重新开始。",
            )
        else:
            await context.bot.send_message(user_id, "2FA 密码不正确。每次尝试间至少冷却 5 秒；达到上限后会暂时锁定。/cancel 可取消。")
    except FloodWaitError as exc:
        store.defer_auth_attempt(user_id, "password", exc.seconds, account_phone=auth_scope)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, f"Telegram 要求等待 {exc.seconds} 秒；已记录该账号等待时间，不会提前重试。")
    except Exception as exc:
        log.warning("2FA sign-in failed (%s)", type(exc).__name__)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, "2FA 登录未完成；请稍后重新开始。")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    if auto_flows.pop(user, None) is None:
        await cleanup_flow(user)
    await update.effective_message.reply_text("当前操作已取消。", reply_markup=menu_for_user(owner_id(update)))


async def resend_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user_id = owner_id(update)
    flow = flows.get(user_id)
    if flow and flow_is_expired(flow):
        await cleanup_flow(user_id)
        await update.effective_message.reply_text(
            "登录流程已过期，不能继续重发旧验证码。请重新发送 /add，或发送 /qr 并再次确认风险。"
        )
        return
    if not flow or flow.state != "code" or not flow.client or not flow.phone or not flow.phone_code_hash:
        await update.effective_message.reply_text("当前没有可重发的手机号验证码。请发送 /add，或发送 /qr 使用二维码登录。")
        return
    if flow.next_code_type is None:
        await update.effective_message.reply_text("Telegram 没有返回可用的下一通道，不能重发或强制切换；请发送 /qr。")
        return
    if flow.resend_attempts >= 4:
        await update.effective_message.reply_text(
            "已达到最多 4 次显式重发。若当前验证码仍有效可继续输入；否则请发送 /qr，或稍后重新开始。"
        )
        return
    remaining = flow.resend_ready_at - time.monotonic()
    if remaining > 0:
        await update.effective_message.reply_text(f"Telegram 要求先等待；约 {int(remaining) + 1} 秒后可发送 /resend。")
        return

    wait_seconds, _request_count = store.reserve_phone_code_request(user_id, flow.phone)
    if wait_seconds:
        flow.resend_ready_at = max(flow.resend_ready_at, time.monotonic() + wait_seconds)
        await update.effective_message.reply_text(f"这个号码仍在独立冷却中，请再等约 {wait_seconds} 秒。")
        return
    flow.resend_attempts += 1
    try:
        result = await flow.client(functions.auth.ResendCodeRequest(flow.phone, flow.phone_code_hash))
        if isinstance(result, types.auth.SentCodeSuccess):
            await finish_login(user_id, flow, context.application)
            return
        if isinstance(result, types.auth.SentCodePaymentRequired):
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, "Telegram 当前要求官方客户端完成额外步骤；Bot 不会处理付费或订阅。请改用官方客户端或二维码登录。")
            return
        if isinstance(getattr(result, "type", None), types.auth.SentCodeTypeSetUpEmailRequired):
            guidance = code_delivery_guidance(result.type)
            await cleanup_flow(user_id)
            await context.bot.send_message(user_id, guidance + "完成后可重新发送 /add，或发送 /qr 并再次确认风险。")
            return
        if not getattr(result, "phone_code_hash", None) or not hasattr(result, "type"):
            raise RuntimeError("Telegram returned an unsupported resend result")
        flow.phone_code_hash = result.phone_code_hash
        flow.current_code_type = result.type
        flow.next_code_type = result.next_type
        flow.started_at = time.monotonic()
        delivery = code_delivery_guidance(result.type)
        message = f"Telegram 返回的当前验证码投递方式：\n{delivery}\n\n"
        message += "若此方式已送达，请在此私聊输入收到的验证码；验证码会在处理后尝试删除，不会保存。"
        markup = None
        if isinstance(result.type, types.auth.SentCodeTypeApp):
            matching = matching_saved_accounts(user_id, flow.phone)
            if matching:
                markup = build_account_picker(matching, "code", format_account)
                message += "\n\n如需从本人已保存的同号会话读取 777000，请明确点选下方按钮；不会自动读取。"
        if result.next_type is not None:
            delay = max(45, int(result.timeout or 0))
            flow.resend_ready_at = time.monotonic() + delay
            store.defer_phone_code_request(user_id, flow.phone, delay)
            message += (
                f"\n\nTelegram 提供的下一通道：{code_type_label(result.next_type)}；"
                f"返回等待 {int(result.timeout or 0)} 秒，本 Bot 至少等待 45 秒。"
                "如仍未收到，请在等待结束后主动 /resend。"
            )
        else:
            flow.resend_ready_at = 0.0
            message += "\n\nTelegram 未提供下一通道；不再重发，请发送 /qr。"
        await context.bot.send_message(user_id, message + "\n/cancel 取消。", reply_markup=markup)
    except FloodWaitError as exc:
        store.defer_phone_code_request(user_id, flow.phone, exc.seconds)
        flow.resend_ready_at = time.monotonic() + max(45, int(exc.seconds))
        await context.bot.send_message(
            user_id,
            f"Telegram 要求等待 {exc.seconds} 秒；已记录该等待时间。此流程不会自动重试，等待结束后你可再发 /resend。",
        )
    except PhoneCodeExpiredError:
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, "验证码已过期，请重新发送 /add；也可发送 /qr。手机号冷却与重发上限仍会生效。")
    except Exception as exc:
        log.warning("Explicit code resend failed (%s)", type(exc).__name__)
        await cleanup_flow(user_id)
        await context.bot.send_message(user_id, "Telegram 未能完成这次重发。为避免重复请求，登录流程已停止；请发送 /qr。")


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
        await update.effective_message.reply_text("格式：/code 账号ID")
        return

    await retrieve_login_code(user_id, account_id, update.effective_message, context.application)


async def send_account_picker(update: Update, action: str) -> None:
    user_id = owner_id(update)
    rows = store.list_accounts(user_id)
    if action == "code":
        flow = flows.get(user_id)
        if flow and flow_is_expired(flow):
            await cleanup_flow(user_id)
            await update.effective_message.reply_text(
                "手机号登录流程已过期；为保护验证码，不再读取旧流程中的 777000 消息。请重新发送 /add。"
            )
            return
        if (
            not flow
            or flow.state != "code"
            or not flow.phone
            or not isinstance(flow.current_code_type, types.auth.SentCodeTypeApp)
        ):
            await update.effective_message.reply_text(
                "为保护验证码，只有本人正在进行手机号登录、且 Telegram 当前返回应用内投递方式时，"
                "才可主动查询同号账号的 777000 消息。请确认当前流程或重新发送 /add。"
            )
            return
        rows = matching_saved_accounts(user_id, flow.phone)
        if not rows:
            await update.effective_message.reply_text("没有找到属于你且号码完全匹配的已保存会话；不会查询其他账号。")
            return
    if not rows:
        await update.effective_message.reply_text("先用 /add 或 /qr 添加你本人的账号。", reply_markup=menu_for_user(owner_id(update)))
        return
    titles = {
        "code": "要从哪个账号读取验证码？",
        "devices": "要查看哪个账号的登录设备？",
        "twofa": "要查看哪个账号的 2FA 状态？",
    }
    await update.effective_message.reply_text(
        titles[action], reply_markup=build_account_picker(rows, action, format_account)
    )


async def retrieve_login_code(user_id: int, account_id: int, message: Any, application: Application) -> None:
    flow = flows.get(user_id)
    if flow and flow_is_expired(flow):
        await cleanup_flow(user_id)
        await message.reply_text(
            "手机号登录流程已过期；为保护验证码，不再读取旧流程中的 777000 消息。请重新发送 /add。"
        )
        return
    row = store.get_account(user_id, account_id)
    if (
        not flow
        or flow.state != "code"
        or not flow.phone
        or not isinstance(flow.current_code_type, types.auth.SentCodeTypeApp)
    ):
        await message.reply_text("验证码读取仅可在你本人当前的手机号登录流程中主动发起。请发送 /add。")
        return
    if not row or phone_identity(row["phone"]) != phone_identity(flow.phone):
        await message.reply_text("所选账号不属于当前手机号登录流程的同号账号；为保护隔离，没有读取验证码。")
        return
    session = store.get_session(user_id, account_id)
    if not session:
        await message.reply_text("没有找到你的这个账号。")
        return
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await message.reply_text("此会话已失效；请重新使用 /add 登录本人账号。")
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
                f"账号 {Store.mask_phone(row['phone'])} 的最新 Telegram 验证码：\n{found}\n"
                f"此码不会写入数据库，并将在约 {CODE_DISPLAY_TTL_SECONDS} 秒后尝试自动删除；请不要转发。",
            )
            application.create_task(expire_displayed_code(sent))
        else:
            await message.reply_text("未找到最近 24 小时内可识别的 Telegram 登录验证码。")
    except Exception as exc:
        log.warning("Code retrieval failed (%s)", type(exc).__name__)
        await message.reply_text("读取失败。请确认账号登录会话有效，或直接在 Telegram 官方客户端查看。")
    finally:
        await client.disconnect()


async def expire_displayed_code(message: Any) -> None:
    await asyncio.sleep(CODE_DISPLAY_TTL_SECONDS)
    try:
        await message.delete()
    except Exception:
        try:
            await message.reply_text("验证码消息未能自动删除，请你手动删除该消息。")
        except Exception:
            pass


async def groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    owner = owner_id(update)
    current = store.list_groups(owner)
    lines = [f"• {g['name']}" for g in current] or ["（暂无分组）"]
    await update.effective_message.reply_text(
        "📁 账号分组\n" + "\n".join(lines) +
        "\n\n创建：/group_create 分组名\n移动：/move 账号ID 分组名",
        reply_markup=menu_for_user(owner_id(update)),
    )


async def group_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("格式：/group_create 分组名")
        return
    ok = store.create_group(owner_id(update), name)
    await update.effective_message.reply_text("分组已创建。" if ok else "创建失败：名称为空或该分组已存在。", reply_markup=menu_for_user(owner_id(update)))


async def move_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("格式：/move 账号ID 分组名")
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("账号 ID 必须是数字。")
        return
    ok = store.move_to_group(owner_id(update), account_id, " ".join(context.args[1:]))
    await update.effective_message.reply_text("已移动账号。" if ok else "未找到你的账号或分组。", reply_markup=menu_for_user(owner_id(update)))


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
        await update.effective_message.reply_text("格式：/devices 账号ID")
        return

    await show_devices(user_id, account_id, update.effective_message)


async def show_devices(user_id: int, account_id: int, message: Any) -> None:
    session = store.get_session(user_id, account_id)
    if not session:
        await message.reply_text("没有找到你的这个账号。")
        return
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        result = await client(functions.account.GetAuthorizationsRequest())
        auths = getattr(result, "authorizations", [])
        lines = []
        for auth in auths[:20]:
            device = " ".join(x for x in [getattr(auth, "device_model", ""), getattr(auth, "platform", "")] if x).strip() or "未知设备"
            app = " ".join(x for x in [getattr(auth, "app_name", ""), getattr(auth, "app_version", "")] if x).strip()
            country = getattr(auth, "country", "") or ""
            date_active = getattr(auth, "date_active", None)
            active = date_active.strftime("%Y-%m-%d %H:%M UTC") if date_active else "未知"
            current = "（本会话）" if getattr(auth, "current", False) else ""
            lines.append(f"• {device} {app} {country} · 最近活跃 {active} {current}".strip())
        store.audit(user_id, "devices_viewed", account_id, "authorization list")
        await message.reply_text("💻 Telegram 登录设备\n" + ("\n".join(lines) if lines else "未返回设备信息。") +
            "\n\n此 MVP 仅查看设备；撤销陌生会话请在 Telegram 官方设置中操作。", reply_markup=menu_for_user(owner_id(update)))
    except Exception as exc:
        log.warning("Device listing failed (%s)", type(exc).__name__)
        await message.reply_text("无法读取设备列表；请检查会话是否仍有效。")
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
        await update.effective_message.reply_text("格式：/2fa 账号ID")
        return
    await show_twofa(user_id, account_id, update.effective_message)


async def show_twofa(user_id: int, account_id: int, message: Any) -> None:
    session = store.get_session(user_id, account_id)
    if not session:
        await message.reply_text("没有找到你的这个账号。")
        return
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        result = await client(functions.account.GetPasswordRequest())
        enabled = bool(getattr(result, "has_password", False))
        status = "已开启" if enabled else "未开启"
        store.audit(user_id, "twofa_status_checked", account_id, "status only")
        await message.reply_text(
            f"🛡 该账号的两步验证：{status}。\n此 MVP 只读取状态，不读取或修改 2FA 密码。"
            "如需更改，请使用 Telegram 官方设置。" + ("建议开启强密码与恢复邮箱。" if not enabled else ""),
            reply_markup=menu_for_user(owner_id(update)),
        )
    except Exception as exc:
        log.warning("2FA status check failed (%s)", type(exc).__name__)
        await message.reply_text("无法读取 2FA 状态；请检查会话是否有效。")
    finally:
        await client.disconnect()


async def search_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    flows[owner_id(update)] = Flow(state="search")
    await update.effective_message.reply_text("发送手机号片段、备注或分组名进行搜索。/cancel 取消。")


async def account_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await require_callback_access(update, query, context.bot):
        return
    parsed = parse_picker_callback(query.data)
    if not parsed:
        await query.answer("这个账号选择按钮无效或已过期。", show_alert=True)
        return
    action, account_id = parsed
    await query.answer("正在查询…")
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
    await update.effective_message.reply_text(
        "🔎 搜索结果\n" + ("\n".join(format_account(r) for r in hits) if hits else "没有匹配的账号。"),
        reply_markup=menu_for_user(owner_id(update)),
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    on = store.alerts_enabled(owner_id(update))
    state = "开启" if on else "关闭"
    rows = store.list_accounts(owner_id(update))
    health = "暂无账号"
    if rows:
        health = f"你有 {len(rows)} 个已添加账号。当前版本不生成综合健康分；可用 /devices 和 /2fa 查看具体状态。"
    await update.effective_message.reply_text(
        f"🔔 安全通知：{state}\n{health}\n\n"
        "设置命令：/notify_on 或 /notify_off。开启后，机器人会在可用时提示 Telegram 官方服务账号发送了安全通知；不会转发通知原文或验证码。",
        reply_markup=menu_for_user(owner_id(update)),
    )


def auto_account_picker(rows: list[dict[str, Any]], language: str) -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton(format_account(row), callback_data=f"auto:acct:{row['id']}")] for row in rows]
    return InlineKeyboardMarkup(keyboard)


def auto_settings_keyboard(account_id: int, enabled: bool, language: str) -> InlineKeyboardMarkup:
    if language == "en":
        toggle_label = "Turn auto-reply off" if enabled else "Turn auto-reply on"
        edit_label, recent_label, back_label = "Change reply message", "Recent replies", "Back to accounts"
    else:
        toggle_label = "关闭自动回复" if enabled else "开启自动回复"
        edit_label, recent_label, back_label = "修改回复内容", "查看最近回复", "返回账号列表"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_label, callback_data=f"auto:toggle:{account_id}")],
            [InlineKeyboardButton(edit_label, callback_data=f"auto:content:{account_id}")],
            [InlineKeyboardButton(recent_label, callback_data=f"auto:recent:{account_id}")],
            [InlineKeyboardButton(back_label, callback_data="auto:list")],
        ]
    )


def auto_scene_keyboard(account_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"auto:edit:{account_id}:{scene}")]
        for scene, label in SCENE_LABELS.items()
    ]
    rows.append([InlineKeyboardButton("返回设置", callback_data=f"auto:acct:{account_id}")])
    return InlineKeyboardMarkup(rows)


async def auto_reply_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    owner = owner_id(update)
    language = store.get_language(owner)
    rows = store.list_accounts(owner)
    if not rows:
        text = "Add your own Telegram account first." if language == "en" else "先添加本人账号，再开启自动回复。"
        await update.effective_message.reply_text(text, reply_markup=menu_for_user(owner))
        return
    prompt = "Choose an account. Auto-reply is OFF until you turn it on." if language == "en" else "选择账号。自动回复默认关闭，需你手动开启。"
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
        await query.answer("按钮无效或已过期。", show_alert=True)
        return
    action = data[1]
    await query.answer()
    message = query.message
    if action == "list":
        if message:
            rows = store.list_accounts(owner)
            language = store.get_language(owner)
            if rows:
                prompt = "Choose an account:" if language == "en" else "请选择账号："
                await message.reply_text(prompt, reply_markup=auto_account_picker(rows, language))
            else:
                await message.reply_text("No accounts added yet." if language == "en" else "目前没有已添加的账号。")
        return
    if action == "all":
        count = store.set_auto_reply_all(owner, True)
        store.audit(owner, "auto_reply_manual_all_on", None, f"accounts={count}")
        for row in store.list_accounts(owner):
            session = store.get_session(owner, row["id"])
            if session:
                start_monitor(owner, row["id"], session, context.application)
        if message:
            await message.reply_text(f"已将 {count} 个账号切换为手动自动模式。人工接管某个账号后，该账号会暂停。")
        return
    if len(data) < 3 or not data[2].isdigit():
        if message:
            await message.reply_text("账号选择无效或已过期。")
        return
    account_id = int(data[2])
    config = store.get_auto_reply_settings(owner, account_id)
    if not config:
        if message:
            await message.reply_text("没有找到你的这个账号。")
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
            await message.reply_text("选择要编辑的场景模板：", reply_markup=auto_scene_keyboard(account_id))
        return
    if action == "content":
        auto_flows[owner] = AutoFlow(state="simple_reply", account_id=account_id)
        if message:
            if store.get_language(owner) == "en":
                prompt = "Send the reply message (up to 1,000 characters). Send “default” to restore the built-in replies, or /cancel to stop."
            else:
                prompt = "请发送自动回复内容（最多 1000 字）。发送“默认”可恢复默认话术，/cancel 可取消。"
            await message.reply_text(prompt)
        return
    if action == "edit" and len(data) == 4 and data[3] in SCENE_LABELS:
        auto_flows[owner] = AutoFlow(state="template", account_id=account_id, scene=data[3])
        if message:
            await message.reply_text(
                f"请发送“{SCENE_LABELS[data[3]]}”模板（最多 1000 字）。"
                "可用变量：{name}、{time}、{product}。发送 /cancel 取消。"
            )
        return
    if action == "limits":
        auto_flows[owner] = AutoFlow(state="thresholds", account_id=account_id)
        if message:
            await message.reply_text(
                f"当前：空闲 {config['idle_minutes']} 分钟、队列阈值 {config['queue_threshold']}、"
                f"夜间 {config['start_hour']:02d}:00–{config['end_hour']:02d}:00。\n"
                "发送 4 个数字：空闲分钟数 队列阈值 夜间开始小时 结束小时；例如：15 3 23 9。"
                "有效范围：空闲 1–1440 分钟、队列 1–100 条、小时 0–23。发送 /cancel 取消。"
            )
        return
    if action == "product":
        auto_flows[owner] = AutoFlow(state="product", account_id=account_id)
        if message:
            await message.reply_text("请发送 {product} 要替换成的产品/品牌名称（最多 120 字）；发送“清空”可移除。发送 /cancel 取消。")
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
            result = f"No auto-replies yet for account #{account_id}." if language == "en" else f"账号 #{account_id} 暂无自动回复记录。"
        else:
            lines = []
            for item in items:
                when = datetime.fromtimestamp(item["created_at"], timezone.utc).strftime("%m-%d %H:%M UTC")
                label = SCENE_LABELS.get(item["scene"], item["scene"] or "离线")
                if language == "en":
                    lines.append(f"• Chat {item['peer_id']} · message #{item['message_id']} · {when}")
                else:
                    lines.append(f"• auto=1 · 对话 {item['peer_id']} · Telegram 消息 #{item['message_id']} · {label} · {when}")
            heading = f"Recent auto-replies for account #{account_id} (message text is not stored):\n" if language == "en" else f"账号 #{account_id} 最近自动回复（正文未保存）：\n"
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
        f"已将 {count} 个账号切换为手动自动模式。任一账号人工发消息后，该账号会立即暂停自动回复。",
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
    await update.effective_message.reply_text(f"已关闭 {count} 个账号的自动回复。", reply_markup=menu_for_user(owner_id(update)))


async def notify_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    store.set_alerts(user, True)
    for row in store.list_accounts(user):
        session = store.get_session(user, row["id"])
        if session:
            start_monitor(user, row["id"], session, context.application)
    await update.effective_message.reply_text("安全通知已开启。机器人仅发送提醒，不会转发 Telegram 消息内容。", reply_markup=menu_for_user(owner_id(update)))


async def notify_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    user = owner_id(update)
    store.set_alerts(user, False)
    for row in store.list_accounts(user):
        await stop_monitor_if_unused(user, row["id"])
    await update.effective_message.reply_text("安全通知已关闭。", reply_markup=menu_for_user(owner_id(update)))


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
                        f"🔔 账号 {account_id} 收到一条 Telegram 官方安全通知。为保护隐私，通知正文和验证码不会转发。",
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
    items = store.list_audit(owner_id(update), 20)
    if not items:
        await update.effective_message.reply_text("暂时没有操作记录。", reply_markup=menu_for_user(owner_id(update)))
        return
    lines = []
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], timezone.utc).strftime("%m-%d %H:%M UTC")
        suffix = f" · 账号 #{item['account_id']}" if item["account_id"] else ""
        lines.append(f"• {when} · {item['action']}{suffix}")
    await update.effective_message.reply_text("🧾 最近操作记录（只记录动作，不含验证码或密码）\n" + "\n".join(lines), reply_markup=menu_for_user(owner_id(update)))


async def trash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    rows = [r for r in store.list_accounts(owner_id(update), include_deleted=True) if r["deleted_at"]]
    if not rows:
        await update.effective_message.reply_text("回收站为空。删除账号使用 /delete 账号ID；恢复使用 /restore 账号ID。", reply_markup=menu_for_user(owner_id(update)))
        return
    await update.effective_message.reply_text("🗑 回收站（仅你可见）\n" + "\n".join(format_account(r) for r in rows) + "\n\n恢复：/restore 账号ID", reply_markup=menu_for_user(owner_id(update)))


async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("格式：/delete 账号ID（移入回收站，可恢复）")
        return
    user, account_id = owner_id(update), int(context.args[0])
    ok = store.delete_account(user, account_id)
    pair = (user, account_id)
    if pair in monitors:
        client, task = monitors.pop(pair)
        task.cancel()
        if client:
            await client.disconnect()
    await update.effective_message.reply_text("账号已移入回收站；会话仍加密保留，可用 /restore 恢复。" if ok else "没有找到你的这个账号。", reply_markup=menu_for_user(owner_id(update)))


async def restore_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("格式：/restore 账号ID")
        return
    user, account_id = owner_id(update), int(context.args[0])
    ok = store.restore_account(user, account_id)
    if ok and (store.alerts_enabled(user) or account_id in store.active_auto_reply_account_ids(user)):
        session = store.get_session(user, account_id)
        if session:
            start_monitor(user, account_id, session, context.application)
    await update.effective_message.reply_text("账号已恢复。" if ok else "回收站中没有找到你的这个账号。", reply_markup=menu_for_user(owner_id(update)))


async def rename_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("格式：/rename 账号ID 新备注")
        return
    account_id = int(context.args[0])
    label = " ".join(context.args[1:]).strip()
    ok = store.update_label(owner_id(update), account_id, label)
    await update.effective_message.reply_text("账号备注已更新。" if ok else "没有找到你的这个账号。", reply_markup=menu_for_user(owner_id(update)))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context.bot):
        return
    if store.get_language(owner_id(update)) == "en":
        text = (
            "Commands\n/accounts — my accounts\n/add — add your own Telegram account\n/qr — QR login\n"
            "/code — retrieve a login code only from your matching saved account\n/resend — resend only when Telegram allows it\n"
            "/groups — account groups\n/devices — login devices\n/2fa — 2FA status\n/search — find an account\n"
            "/autoreply — simple per-account auto-reply settings\n/language — switch Chinese/English\n"
            "/notify_on, /notify_off — security alerts\n/audit — activity\n/cancel — cancel the current step\n\n"
            "Before first use, complete a one-time human check and join the official channel."
            " Auto-reply is off by default, works only in private chats, and never stores incoming message text."
        )
    else:
        text = (
            "命令说明\n/accounts — 我的账号\n/add — 添加本人账号\n/qr — QR 登录\n"
            "/resend — 仅在 Telegram 允许时重发验证码\n/code — 只从本人匹配的已保存账号读取登录码\n"
            "/groups — 账号分组\n/devices — 登录设备\n/2fa — 2FA 状态\n/search — 搜索账号\n"
            "/autoreply — 简单的逐账号自动回复设置\n/language — 切换中文/English\n"
            "/notify_on、/notify_off — 安全通知\n/audit — 操作记录\n/cancel — 取消当前步骤\n\n"
            "首次使用需完成一次人机验证并加入官方频道。自动回复默认关闭，只处理私聊，不保存收到的消息正文。"
        )
    await update.effective_message.reply_text(text, reply_markup=menu_for_user(owner_id(update)))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context.bot):
        await update.effective_message.reply_text("未知命令，请发送 /help。", reply_markup=menu_for_user(owner_id(update)))


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
