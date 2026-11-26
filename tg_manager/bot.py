from __future__ import annotations

import asyncio
import io
import logging
import os
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


FLOW_TTL_SECONDS = 600


def flow_is_expired(flow: Flow) -> bool:
    return time.monotonic() - flow.started_at > FLOW_TTL_SECONDS


flows: dict[int, Flow] = {}
monitors: dict[tuple[int, int], tuple[TelegramClient, asyncio.Task]] = {}
last_qr_login_attempt: dict[int, float] = {}
LOGIN_COOLDOWN_SECONDS = max(30, int(os.getenv("LOGIN_COOLDOWN_SECONDS", "60")))
COUNTRY_PAGE_SIZE = 8

MENU = ReplyKeyboardMarkup(
    [
        ["📱 账号列表", "➕ 添加账号"],
        ["🔳 QR 登录", "📩 获取验证码"],
        ["📁 账号分组", "💻 设备会话"],
        ["🛡 2FA 状态", "🔎 搜索账号"],
        ["🔔 通知与健康", "🧾 操作记录"],
        ["🗑 回收站", "❔ 帮助"],
    ],
    resize_keyboard=True,
    input_field_placeholder="选择功能或发送命令",
)


def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private" and update.effective_user)


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


async def require_private(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_allowed_user_id(user.id, ALLOWED_TELEGRAM_USER_IDS):
        return False
    if not is_private(update):
        if update.effective_message:
            await update.effective_message.reply_text("为保护账号隐私，请只在机器人私聊中操作。")
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
    if not await require_private(update):
        return
    name = update.effective_user.first_name if update.effective_user else "朋友"
    await update.effective_message.reply_text(
        f"🔐 TGVault 账号管理\n你好，{name}。每位使用者只能看到和操作自己添加的账号。\n\n"
        "登录前会先说明托管风险并请求你的明确同意；验证码和 2FA 密码不会写入数据库或日志。\n"
        "选择下方菜单，或发送 /help 查看命令。",
        reply_markup=MENU,
    )


async def accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    rows = store.list_accounts(owner_id(update))
    if not rows:
        await update.effective_message.reply_text("目前没有已添加的账号。使用 /add 添加本人账号。", reply_markup=MENU)
        return
    text = "📱 你的账号（仅你可见）\n\n" + "\n".join(format_account(row) for row in rows)
    await update.effective_message.reply_text(text, reply_markup=MENU)
    store.audit(owner_id(update), "accounts_viewed", None, "account list")


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
    if not await require_private(update):
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
    if not await require_private(update):
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
    if not await require_private(update):
        await query.answer("无权使用此机器人。", show_alert=True)
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
            reply_markup=MENU,
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
    if not await require_private(update):
        return
    text = (update.effective_message.text or "").strip()
    menu_handlers = {
        "📱 账号列表": accounts,
        "➕ 添加账号": add_start,
        "🔳 QR 登录": qr_start,
        "📩 获取验证码": code_command,
        "📁 账号分组": groups,
        "💻 设备会话": devices,
        "🛡 2FA 状态": twofa,
        "🔎 搜索账号": search_prompt,
        "🔔 通知与健康": settings,
        "🧾 操作记录": audit,
        "🗑 回收站": trash,
        "❔ 帮助": help_command,
    }
    if text in menu_handlers:
        await menu_handlers[text](update, context)
        return
    user_id = owner_id(update)
    flow = flows.get(user_id)
    if not flow:
        await update.effective_message.reply_text("请选择菜单功能，或发送 /help。", reply_markup=MENU)
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
        await update.effective_message.reply_text("备注已更新。" if ok else "没有找到你的这个账号。", reply_markup=MENU)
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
    if not await require_private(update):
        return
    await cleanup_flow(owner_id(update))
    await update.effective_message.reply_text("当前操作已取消。", reply_markup=MENU)


async def resend_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
    if not await require_private(update):
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
        await update.effective_message.reply_text("先用 /add 或 /qr 添加你本人的账号。", reply_markup=MENU)
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
    if not await require_private(update):
        return
    owner = owner_id(update)
    current = store.list_groups(owner)
    lines = [f"• {g['name']}" for g in current] or ["（暂无分组）"]
    await update.effective_message.reply_text(
        "📁 账号分组\n" + "\n".join(lines) +
        "\n\n创建：/group_create 分组名\n移动：/move 账号ID 分组名",
        reply_markup=MENU,
    )


async def group_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("格式：/group_create 分组名")
        return
    ok = store.create_group(owner_id(update), name)
    await update.effective_message.reply_text("分组已创建。" if ok else "创建失败：名称为空或该分组已存在。", reply_markup=MENU)


async def move_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
    await update.effective_message.reply_text("已移动账号。" if ok else "未找到你的账号或分组。", reply_markup=MENU)


async def devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
            "\n\n此 MVP 仅查看设备；撤销陌生会话请在 Telegram 官方设置中操作。", reply_markup=MENU)
    except Exception as exc:
        log.warning("Device listing failed (%s)", type(exc).__name__)
        await message.reply_text("无法读取设备列表；请检查会话是否仍有效。")
    finally:
        await client.disconnect()


async def twofa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
            reply_markup=MENU,
        )
    except Exception as exc:
        log.warning("2FA status check failed (%s)", type(exc).__name__)
        await message.reply_text("无法读取 2FA 状态；请检查会话是否有效。")
    finally:
        await client.disconnect()


async def search_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    flows[owner_id(update)] = Flow(state="search")
    await update.effective_message.reply_text("发送手机号片段、备注或分组名进行搜索。/cancel 取消。")


async def account_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not await require_private(update):
        await query.answer("无权使用此机器人。", show_alert=True)
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
    if not await require_private(update):
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
        reply_markup=MENU,
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
        reply_markup=MENU,
    )


async def notify_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    user = owner_id(update)
    store.set_alerts(user, True)
    for row in store.list_accounts(user):
        session = store.get_session(user, row["id"])
        if session:
            start_monitor(user, row["id"], session, context.application)
    await update.effective_message.reply_text("安全通知已开启。机器人仅发送提醒，不会转发 Telegram 消息内容。", reply_markup=MENU)


async def notify_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    user = owner_id(update)
    store.set_alerts(user, False)
    for key in list(monitors):
        if key[0] == user:
            client, task = monitors.pop(key)
            task.cancel()
            try:
                await client.disconnect()
            except Exception:
                pass
    await update.effective_message.reply_text("安全通知已关闭。", reply_markup=MENU)


def start_monitor(user_id: int, account_id: int, session: str, application: Application) -> None:
    key = (user_id, account_id)
    if key in monitors or not store.alerts_enabled(user_id):
        return
    task = asyncio.create_task(watch_account(user_id, account_id, session, application, key))
    # A placeholder client is replaced as soon as the coroutine connects.
    monitors[key] = (None, task)  # type: ignore[arg-type]


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

        client.add_event_handler(on_service_message, events.NewMessage(from_users=777000, incoming=True))
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
    if not await require_private(update):
        return
    items = store.list_audit(owner_id(update), 20)
    if not items:
        await update.effective_message.reply_text("暂时没有操作记录。", reply_markup=MENU)
        return
    lines = []
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], timezone.utc).strftime("%m-%d %H:%M UTC")
        suffix = f" · 账号 #{item['account_id']}" if item["account_id"] else ""
        lines.append(f"• {when} · {item['action']}{suffix}")
    await update.effective_message.reply_text("🧾 最近操作记录（只记录动作，不含验证码或密码）\n" + "\n".join(lines), reply_markup=MENU)


async def trash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    rows = [r for r in store.list_accounts(owner_id(update), include_deleted=True) if r["deleted_at"]]
    if not rows:
        await update.effective_message.reply_text("回收站为空。删除账号使用 /delete 账号ID；恢复使用 /restore 账号ID。", reply_markup=MENU)
        return
    await update.effective_message.reply_text("🗑 回收站（仅你可见）\n" + "\n".join(format_account(r) for r in rows) + "\n\n恢复：/restore 账号ID", reply_markup=MENU)


async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
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
    await update.effective_message.reply_text("账号已移入回收站；会话仍加密保留，可用 /restore 恢复。" if ok else "没有找到你的这个账号。", reply_markup=MENU)


async def restore_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("格式：/restore 账号ID")
        return
    user, account_id = owner_id(update), int(context.args[0])
    ok = store.restore_account(user, account_id)
    if ok and store.alerts_enabled(user):
        session = store.get_session(user, account_id)
        if session:
            start_monitor(user, account_id, session, context.application)
    await update.effective_message.reply_text("账号已恢复。" if ok else "回收站中没有找到你的这个账号。", reply_markup=MENU)


async def rename_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("格式：/rename 账号ID 新备注")
        return
    account_id = int(context.args[0])
    label = " ".join(context.args[1:]).strip()
    ok = store.update_label(owner_id(update), account_id, label)
    await update.effective_message.reply_text("账号备注已更新。" if ok else "没有找到你的这个账号。", reply_markup=MENU)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    await update.effective_message.reply_text(
        "命令说明\n"
        "/accounts — 我的账号\n/add — 先同意风险，再搜索/选择国家并输入号码进行手机号登录\n/qr — QR 登录\n"
        "/resend — 仅在 Telegram 返回 next_type、等待结束后主动重发；最多 4 次，不自动连发\n"
        "/code — 仅在本人手机号登录流程中，点选同号的本人已保存账号后读取 Telegram 登录码\n"
        "/groups、/group_create 名称、/move 账号ID 分组名、/rename 账号ID 新备注\n/devices — 点选账号查看登录设备\n/2fa — 点选账号查看是否开启\n"
        "/search 关键词 — 搜索备注/号码/分组\n/notify_on、/notify_off — 安全通知\n/audit — 操作记录\n"
        "/delete 账号ID、/restore 账号ID — 回收站操作\n/consent — 确认理解登录托管风险\n/cancel — 取消当前流程\n\n"
        "安全边界：仅白名单用户可用且仅私聊；每位用户只能操作自己添加的账号；登录前必须同意运营者可访问会话。"
        "账号转交、代替他人登录、群发消息、读取聊天内容及修改/查看 2FA 密码均不提供。",
        reply_markup=MENU,
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update):
        await update.effective_message.reply_text("未知命令，请发送 /help。", reply_markup=MENU)


async def on_startup(application: Application) -> None:
    # Restore security-alert watchers after restart; session blobs are decrypted only in memory.
    for user_id in store.active_owner_ids():
        if not is_allowed_user_id(user_id, ALLOWED_TELEGRAM_USER_IDS):
            continue
        if not store.alerts_enabled(user_id):
            continue
        for row in store.list_accounts(user_id):
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Do not log exception values/tracebacks or Telegram update bodies; they may contain secrets.
    log.error("Request failed (%s)", type(context.error).__name__ if context.error else "unknown")


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CommandHandler("notify_on", notify_on))
    app.add_handler(CommandHandler("notify_off", notify_off))
    app.add_handler(CommandHandler("audit", audit))
    app.add_handler(CommandHandler("trash", trash))
    app.add_handler(CommandHandler("delete", delete_account))
    app.add_handler(CommandHandler("restore", restore_account))
    app.add_handler(CommandHandler("rename", rename_account))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(country_picker_callback, pattern=r"^ctry:"))
    app.add_handler(CallbackQueryHandler(account_picker_callback, pattern=r"^acctpick:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
