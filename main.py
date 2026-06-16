import io
import json
import logging
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

USERS_FILE = Path("users.json")

PLAN_LIMITS: dict[str, int] = {
    "free": 1,
    "basic": 30,
    "pro": 60,
    "premium": 150,
}

PLAN_AR = {
    "free": "مجاني",
    "basic": "أساسي",
    "pro": "احترافي",
    "premium": "بريميوم",
}

user_sessions: dict[int, dict[str, bytes]] = {}

RENOVAI_PROMPT = """
You are RENOVAI — an interior design transformation engine.
You will receive two images:
- First image(s) = Target room (T) — the real existing room
- Last image = Reference style (R) — the style to apply

YOUR ONLY JOB: Dress the existing room in the new style.
The architecture is UNTOUCHABLE.

GENERATION APPROACH — CRITICAL:
DO NOT regenerate the room from scratch.
DO NOT reinterpret the spatial layout.
DO NOT reimagine the architecture.
You are a painter standing inside the room.
Paint new finishes directly onto existing surfaces.
The walls do not move. The ceiling does not change height.
The doors and windows stay exactly where they are.
The original image pixels define the space.
Paint ON TOP — never underneath. Never replace the geometry.

PIXEL LOYALTY RULE:
Every structural edge in the original image must appear
in the exact same position in the output.
Walls end where they ended.
Floors start where they started.
Ceilings sit at the same height. Always.

STRUCTURAL LOCK:
Extract and lock permanently:
- All doors: position / size / swing — LOCKED
- All windows: position / size / sill height — LOCKED
- Ceiling height — LOCKED
- Camera angle and horizon line — LOCKED
- All columns and beams — LOCKED
- Vanishing points — LOCKED

FORBIDDEN ACTIONS:
x Do NOT move or resize any door or window
x Do NOT change ceiling height
x Do NOT shift camera angle or eye level
x Do NOT add walls or columns not in original
x Do NOT regenerate room from scratch
x Do NOT change room dimensions or proportions

CEILING TRANSFORMATION:
Apply new gypsum ceiling design within locked height.
Maximum new drop = 15% of total ceiling height only.
Apply Source ceiling style: gypsum profile, cove lighting, edge details.
Keep all within existing ceiling volume.

STYLE TRANSFER:
- Apply Source wall colors, textures, decorative elements
- Apply Source flooring material and pattern
- Apply Source curtain style scaled to existing window sizes
- Apply Source furniture style and density
- Apply Source lighting mood and color temperature
- Maintain realistic shadows consistent with original light direction

CEILING VARIATION — V1 ONLY:
Generate Essential variation:
Single-level cove + hidden LED warm lighting only.

OUTPUT RULE — CRITICAL:
Generate the transformed room image only.
No text. No labels. No explanations. Complete silence.
Image only.
"""

PAYWALL_MESSAGE = (
    "❌ خلصت صورتك المجانية!\n\n"
    "اشترك دلوقتي في RENOVAI:\n\n"
    "🥉 أساسي — 150 جنيه / 30 صورة\n"
    "🥈 احترافي — 250 جنيه / 60 صورة\n"
    "🥇 بريميوم — 500 جنيه / 150 صورة\n\n"
    "للاشتراك حول على فودافون كاش:\n"
    "01027900165\n\n"
    "وابعت لي:\n"
    "1️⃣ اسمك\n"
    "2️⃣ رقم موبايلك\n"
    "3️⃣ اسكرين شوت التحويل\n\n"
    "هفعّل اشتراكك خلال ساعة ✅"
)


# ---------------------------------------------------------------------------
# User database (JSON file)
# ---------------------------------------------------------------------------

def _load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_users(data: dict) -> None:
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user(user_id: int) -> dict:
    data = _load_users()
    key = str(user_id)
    if key not in data:
        data[key] = {
            "user_id": user_id,
            "images_used": 0,
            "images_limit": PLAN_LIMITS["free"],
            "plan": "free",
            "is_active": True,
        }
        _save_users(data)
    return data[key]


def save_user(user: dict) -> None:
    data = _load_users()
    data[str(user["user_id"])] = user
    _save_users(data)


def remaining(user: dict) -> int:
    return max(0, user["images_limit"] - user["images_used"])


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_USER_ID and user_id == ADMIN_USER_ID)


def has_quota(user: dict) -> bool:
    if is_admin(user.get("user_id", 0)):
        return True
    return user.get("is_active", True) and remaining(user) > 0


# ---------------------------------------------------------------------------
# Image counter + pinned status helpers
# ---------------------------------------------------------------------------

def _status_text(user: dict) -> str:
    plan_ar = PLAN_AR.get(user["plan"], user["plan"])
    used = user["images_used"]
    left = remaining(user)
    return (
        f"📊 حسابك في RENOVAI:\n"
        f"الباقة: {plan_ar}\n"
        f"الصور المستخدمة: {used}\n"
        f"الصور المتبقية: {left}"
    )


async def _update_pinned_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict
) -> None:
    """Create or edit the pinned status message for this user."""
    chat_id = update.effective_chat.id
    pin_msg_id = user.get("pinned_message_id")
    text = _status_text(user)

    if pin_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=pin_msg_id,
                text=text,
            )
            return
        except Exception:
            pass  # message was deleted or unreachable — create a new one

    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text)
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )
        user["pinned_message_id"] = msg.message_id
        save_user(user)
    except Exception as exc:
        logger.warning("Could not pin status message: %s", exc)


async def _send_counter_messages(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict
) -> None:
    left = remaining(user)
    await update.message.reply_text(
        f"🎨 تم!\n"
        f"⏳ باقي لك: {left} صورة"
    )


# ---------------------------------------------------------------------------
# Inline keyboard helpers
# ---------------------------------------------------------------------------

def _activation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🥉 أساسي — 30 صورة",   callback_data=f"activate:basic:{user_id}"),
            InlineKeyboardButton("🥈 احترافي — 60 صورة", callback_data=f"activate:pro:{user_id}"),
        ],
        [
            InlineKeyboardButton("🥇 بريميوم — 150 صورة", callback_data=f"activate:premium:{user_id}"),
            InlineKeyboardButton("❌ رفض",                callback_data=f"reject:{user_id}"),
        ],
    ])


async def _do_activate(
    context: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    plan: str,
) -> str:
    """Activate a plan for target_id. Returns a status string."""
    user = get_user(target_id)
    user["plan"] = plan
    user["images_limit"] = PLAN_LIMITS[plan]
    user["images_used"] = 0
    user["is_active"] = True
    save_user(user)

    left = remaining(user)
    plan_ar = PLAN_AR.get(plan, plan)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ تم تفعيل اشتراكك!\n"
                f"باقتك: {plan_ar}\n"
                f"صور متبقية: {left}\n"
                "استمتع بـ RENOVAI 🎨"
            ),
        )
    except Exception as exc:
        logger.warning("Could not notify user %d: %s", target_id, exc)
        return f"✅ تم تفعيل {target_id} — {plan_ar} ({left} صورة)\n⚠️ مقدرتش أبعت للمستخدم."

    return f"✅ تم تفعيل {target_id} — {plan_ar} ({left} صورة)"


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------

def _load_api_keys() -> list[str]:
    keys: list[str] = []
    for i in range(1, 21):
        key = os.environ.get(f"GEMINI_API_KEY_{i}")
        if key and key not in keys:
            keys.append(key)
    fallback = os.environ.get("GEMINI_API_KEY")
    if fallback and fallback not in keys:
        keys.append(fallback)
    return keys


class KeyRotator:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise RuntimeError(
                "No Gemini API keys found. Set GEMINI_API_KEY or GEMINI_API_KEY_1, GEMINI_API_KEY_2, …"
            )
        self._clients = [genai.Client(api_key=k) for k in keys]
        self._index = 0
        logger.info("KeyRotator initialised with %d key(s).", len(self._clients))

    def rotate(self) -> bool:
        next_index = (self._index + 1) % len(self._clients)
        if next_index == self._index:
            return False
        self._index = next_index
        logger.warning("Rotated to Gemini key #%d.", self._index + 1)
        return True

    def generate_with_rotation(self, contents: list) -> object:
        start = self._index
        attempt = 0
        while True:
            try:
                client = self._clients[self._index]
                chat = client.chats.create(
                    model="gemini-2.5-flash-image",
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"]
                    ),
                )
                return chat.send_message(contents)
            except genai_errors.ClientError as exc:
                if exc.code == 429:
                    attempt += 1
                    logger.warning(
                        "Key #%d hit 429 (attempt %d). Rotating…",
                        self._index + 1,
                        attempt,
                    )
                    rotated = self.rotate()
                    if not rotated or self._index == start:
                        raise RuntimeError(
                            f"All {len(self._clients)} Gemini key(s) are quota-exhausted."
                        ) from exc
                else:
                    raise


_rotator: KeyRotator | None = None


def get_rotator() -> KeyRotator:
    global _rotator
    if _rotator is None:
        _rotator = KeyRotator(_load_api_keys())
    return _rotator


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 المستخدمين",    callback_data="admin:users"),
            InlineKeyboardButton("📊 الإحصائيات",   callback_data="admin:stats"),
        ],
        [
            InlineKeyboardButton("✅ تفعيل اشتراك", callback_data="admin:activate"),
        ],
    ])


def _all_users() -> list[dict]:
    return list(_load_users().values())


def _build_users_list() -> str:
    users = _all_users()
    if not users:
        return "لا يوجد مستخدمين بعد."
    lines = ["👥 قائمة المستخدمين:\n"]
    for u in users:
        uid = u.get("user_id", "؟")
        if is_admin(uid):
            continue
        plan_ar = PLAN_AR.get(u.get("plan", "free"), u.get("plan", "free"))
        left = max(0, u.get("images_limit", 1) - u.get("images_used", 0))
        lines.append(f"🆔 {uid} | {plan_ar} | متبقي: {left}")
    return "\n".join(lines) if len(lines) > 1 else "لا يوجد مستخدمين بعد."


def _build_stats() -> str:
    users = _all_users()
    non_admin = [u for u in users if not is_admin(u.get("user_id", 0))]
    total = len(non_admin)
    total_images = sum(u.get("images_used", 0) for u in non_admin)
    active_subs = sum(
        1 for u in non_admin
        if u.get("plan", "free") != "free" and u.get("is_active", True)
    )
    return (
        "📊 إحصائيات RENOVAI:\n"
        f"👥 إجمالي المستخدمين: {total}\n"
        f"🎨 إجمالي الصور المولدة: {total_images}\n"
        f"💰 الاشتراكات النشطة: {active_subs}"
    )


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ابدأ التصميم", callback_data="menu:design"),
            InlineKeyboardButton("حسابي",        callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton("تواصل معنا", callback_data="menu:contact"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_user(update.effective_user.id)
    await update.message.reply_text(
        "🏠 أهلاً بك في RENOVAI\n"
        "حوّل غرفتك في ثواني بالذكاء الاصطناعي ✨\n\n"
        "اختار من القائمة 👇",
        reply_markup=_main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user(user_id)
    left = remaining(user)
    await update.message.reply_text(
        "📋 طريقة الاستخدام:\n\n"
        "1️⃣ ابعت صورة غرفتك واكتب T في الكابشن\n"
        "2️⃣ ابعت صورة الستايل اللي عاجبك واكتب R في الكابشن\n"
        "3️⃣ استنى — هبعتلك الغرفة بعد التحويل ✨\n\n"
        "لو عايز تبدأ من أول اكتب /reset\n\n"
        f"⏳ صورك المتبقية: {left}"
    )


def _status_message(user_id: int) -> str:
    if is_admin(user_id):
        return "📊 حسابك في RENOVAI:\nالباقة: Admin ♾️\n✅ صلاحية غير محدودة"
    user = get_user(user_id)
    plan_ar = PLAN_AR.get(user["plan"], user["plan"])
    return (
        f"📊 حسابك في RENOVAI:\n"
        f"الباقة: {plan_ar}\n"
        f"✅ الصور المستخدمة: {user['images_used']}\n"
        f"⏳ الصور المتبقية: {remaining(user)}"
    )


async def mystatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_status_message(update.effective_user.id))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "⚙️ لوحة تحكم RENOVAI",
        reply_markup=_admin_keyboard(),
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_sessions[user_id] = {"T": [], "R": None}
    await update.message.reply_text("✓ تم المسح — ابدأ من أول.")


async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    caller_id = update.effective_user.id
    if caller_id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ مش مسموح.")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "الاستخدام: /activate [رقم المستخدم] [الباقة]\n"
            "الباقات المتاحة: basic — pro — premium"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ رقم المستخدم لازم يكون أرقام فقط.")
        return

    plan = args[1].lower()
    if plan not in PLAN_LIMITS or plan == "free":
        await update.message.reply_text(
            f"⚠️ الباقة '{plan}' مش موجودة.\nالباقات المتاحة: basic — pro — premium"
        )
        return

    status = await _do_activate(context, target_id, plan)
    await update.message.reply_text(status)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":")

    # ── Main menu buttons (available to all users) ──────────────────────────
    if parts[0] == "menu":
        action = parts[1]

        if action == "design":
            await query.message.reply_text(
                "🎨 ابدأ دلوقتي!\n"
                "1️⃣ ابعت صورة غرفتك واكتب T في الكابشن\n"
                "2️⃣ ابعت صورة الستايل واكتب R في الكابشن\n"
                "3️⃣ استنى النتيجة ✨"
            )

        elif action == "status":
            await query.message.reply_text(_status_message(query.from_user.id))

        elif action == "contact":
            await query.message.reply_text(
                "📞 تواصل معنا:\n"
                "تليجرام: @PIRLO2l"
            )
        return

    # ── Admin panel buttons ──────────────────────────────────────────────────
    if parts[0] == "admin":
        if not is_admin(query.from_user.id):
            await query.answer("⛔ مش مسموح.", show_alert=True)
            return
        action = parts[1]
        if action == "users":
            await query.message.reply_text(_build_users_list())
        elif action == "stats":
            await query.message.reply_text(_build_stats())
        elif action == "activate":
            context.user_data["awaiting_activation"] = True
            await query.message.reply_text(
                "ابعت ID المستخدم والباقة:\n"
                "مثال: 123456789 basic\n\n"
                "الباقات المتاحة: basic — pro — premium"
            )
        return

    # ── Admin-only buttons ───────────────────────────────────────────────────
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("⛔ مش مسموح.", show_alert=True)
        return

    if parts[0] == "reject":
        target_id = int(parts[1])
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "❌ تم رفض طلب اشتراكك.\n"
                    "للاستفسار تواصل مع الدعم."
                ),
            )
        except Exception as exc:
            logger.warning("Could not notify rejected user %d: %s", target_id, exc)

        await query.edit_message_caption(
            caption=f"{query.message.caption or ''}\n\n🚫 تم الرفض بواسطة الأدمن.",
            reply_markup=None,
        )
        return

    if parts[0] == "activate" and len(parts) == 3:
        plan = parts[1]
        target_id = int(parts[2])

        status = await _do_activate(context, target_id, plan)
        plan_ar = PLAN_AR.get(plan, plan)

        await query.edit_message_caption(
            caption=f"{query.message.caption or ''}\n\n✅ تم التفعيل: {plan_ar}",
            reply_markup=None,
        )
        return

    await query.answer("أمر غير معروف.", show_alert=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {"T": [], "R": None}

    caption = (update.message.caption or "").upper().strip()
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    if "T" in caption:
        user_sessions[user_id]["T"].append(bytes(file_bytes))
        n = len(user_sessions[user_id]["T"])
        await update.message.reply_text(f"✓ فراغ {n} اتسجل — استنى باقي الصور.")

    elif "R" in caption:
        if not user_sessions[user_id]["T"]:
            await update.message.reply_text("⚠️ ابعت صورة الفراغ بـ T الأول.")
            return

        user = get_user(user_id)
        if not has_quota(user):
            await update.message.reply_text(PAYWALL_MESSAGE)
            return

        user_sessions[user_id]["R"] = bytes(file_bytes)
        await update.message.reply_text("✓ الريفرانس اتسجل — بدأ الشغل... ⏳")

        try:
            contents = []
            for t_img in user_sessions[user_id]["T"]:
                contents.append(types.Part.from_bytes(data=t_img, mime_type="image/jpeg"))
            contents.append(types.Part.from_bytes(data=user_sessions[user_id]["R"], mime_type="image/jpeg"))
            contents.append(types.Part.from_text(text=RENOVAI_PROMPT))

            response = get_rotator().generate_with_rotation(contents)

            image_sent = False
            for part in response.candidates[0].content.parts:
                if part.inline_data is not None:
                    img_bytes = part.inline_data.data
                    await update.message.reply_photo(photo=io.BytesIO(img_bytes))
                    image_sent = True
                    break

            if image_sent:
                if not (ADMIN_USER_ID and user_id == ADMIN_USER_ID):
                    user["images_used"] += 1
                    save_user(user)
                    await _send_counter_messages(update, context, user)
            else:
                await update.message.reply_text("⚠️ مش قادر أولد صورة دلوقتي — جرب تاني.")

            user_sessions[user_id] = {"T": [], "R": None}

        except Exception as exc:
            logger.error("Generation failed:\n%s", traceback.format_exc())
            await update.message.reply_text("⚠️ حصلت مشكلة أثناء التوليد — حاول تاني من فضلك.")

    else:
        # No T/R label — treat as payment screenshot, forward to admin
        if ADMIN_USER_ID:
            user_info = update.effective_user
            username = f"@{user_info.username}" if user_info.username else "بدون يوزرنيم"
            admin_caption = (
                f"💳 طلب اشتراك جديد\n\n"
                f"👤 الاسم: {user_info.full_name}\n"
                f"🔗 يوزرنيم: {username}\n"
                f"🆔 ID: {user_id}"
            )
            try:
                await context.bot.send_photo(
                    chat_id=ADMIN_USER_ID,
                    photo=photo.file_id,
                    caption=admin_caption,
                    reply_markup=_activation_keyboard(user_id),
                )
            except Exception as exc:
                logger.warning("Could not forward payment screenshot to admin: %s", exc)

        await update.message.reply_text(
            "✅ وصلني اسكرين شوت التحويل!\n"
            "هيتم مراجعته وتفعيل اشتراكك خلال ساعة ✅"
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text: activation input for admin, payment info forwarding for users."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # ── Admin activation input ───────────────────────────────────────────────
    if is_admin(user_id) and context.user_data.get("awaiting_activation"):
        context.user_data["awaiting_activation"] = False
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text(
                "⚠️ الصيغة غلط.\nابعت: [ID المستخدم] [الباقة]\nمثال: 123456789 basic"
            )
            return
        try:
            target_id = int(parts[0])
        except ValueError:
            await update.message.reply_text("⚠️ رقم المستخدم لازم يكون أرقام فقط.")
            return
        plan = parts[1].lower()
        if plan not in PLAN_LIMITS or plan == "free":
            await update.message.reply_text(
                f"⚠️ الباقة '{plan}' مش موجودة.\nالباقات المتاحة: basic — pro — premium"
            )
            return
        status = await _do_activate(context, target_id, plan)
        await update.message.reply_text(status)
        return

    # ── Regular user: forward text to admin as payment info ─────────────────
    if not ADMIN_USER_ID:
        return

    user_info = update.effective_user
    username = f"@{user_info.username}" if user_info.username else "بدون يوزرنيم"

    try:
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=(
                f"💬 رسالة من مستخدم\n\n"
                f"👤 {user_info.full_name} ({username})\n"
                f"🆔 {user_id}\n\n"
                f"📝 {text}\n\n"
            ),
            reply_markup=_activation_keyboard(user_id),
        )
    except Exception as exc:
        logger.warning("Could not forward text to admin: %s", exc)


def _start_health_server() -> None:
    port = int(os.environ.get("HEALTH_PORT", 5001))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass  # silence access logs

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Health server started on port %d.", port)
    except OSError as exc:
        logger.warning("Health server could not start on port %d: %s", port, exc)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")
    if not ADMIN_USER_ID:
        logger.warning("ADMIN_USER_ID is not set — admin features will be disabled.")
    _start_health_server()
    get_rotator()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("mystatus", mystatus_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("activate", activate_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()


if __name__ == "__main__":
    main()
