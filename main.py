import os
import re
import html
import sys
import asyncio
import platform
import random
import logging
from collections import defaultdict
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)

# --- Détection du système d'exploitation ---
SYSTEM_OS = platform.system()

if SYSTEM_OS == "Windows":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Chargement des variables d'environnement
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Configuration du Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Fuseau horaire ---
TIMEZONE_NAME = os.getenv("TIMEZONE", "UTC")
try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception as e:
    logger.warning(
        f"Fuseau horaire '{TIMEZONE_NAME}' indisponible ({e}). "
        f"Installez le paquet 'tzdata' (pip install tzdata) pour un support complet. "
        f"Utilisation d'UTC en secours."
    )
    TZ = timezone.utc
    TIMEZONE_NAME = "UTC"

# --- Persistance ---
PERSISTENCE_FILE = os.getenv("PERSISTENCE_FILE", "bot_data.pickle")

# --- Restriction d'accès ---
_raw_allowed = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = {
    int(x.strip()) for x in _raw_allowed.split(",") if x.strip().lstrip("-").isdigit()
}

# --- Administrateur ---
_admin_env = os.getenv("ADMIN_CHAT_ID", "").strip()
if _admin_env.lstrip("-").isdigit():
    ADMIN_CHAT_ID = int(_admin_env)
elif len(ALLOWED_CHAT_IDS) == 1:
    ADMIN_CHAT_ID = next(iter(ALLOWED_CHAT_IDS))
else:
    ADMIN_CHAT_ID = None

# --- Groupe de support avec Topics ---
_support_group_env = os.getenv("SUPPORT_GROUP_ID", "").strip()
SUPPORT_GROUP_ID = int(_support_group_env) if _support_group_env.lstrip("-").isdigit() else None

# --- Reset quotidien automatique ---
DAILY_RESET_ENABLED = os.getenv("DAILY_RESET_ENABLED", "false").lower() == "true"
DAILY_RESET_HOUR = int(os.getenv("DAILY_RESET_HOUR", "0"))

# --- Timeouts réseau ---
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "20"))

# --- Diffusion vers canaux/groupes ---
_raw_broadcast = os.getenv("BROADCAST_TARGETS", "")
BROADCAST_TARGETS = [t.strip() for t in _raw_broadcast.split(",") if t.strip()]


def normalize_broadcast_target(target: str):
    t = target.strip()
    if t.lstrip("-").isdigit():
        return int(t)
    return t

# Dossiers d'images
DIR_IMG = "IMG"
DIR_WIN = "IMG_WIN"
DIR_LOSE = "IMG_LOSE"

# Lien d'inscription PocketOption
POCKET_OPTION_LINK = "https://bit.ly/4ckz9cY"

# --- Canal Telegram à promouvoir ---
_channel_env = os.getenv("CHANNEL_CHAT_ID", "").strip()
if _channel_env.lstrip("-").isdigit():
    CHANNEL_CHAT_ID = int(_channel_env)
elif _channel_env:
    CHANNEL_CHAT_ID = _channel_env if _channel_env.startswith("@") else f"@{_channel_env}"
else:
    CHANNEL_CHAT_ID = None

CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "").strip()
if not CHANNEL_INVITE_LINK and isinstance(CHANNEL_CHAT_ID, str) and CHANNEL_CHAT_ID.startswith("@"):
    CHANNEL_INVITE_LINK = f"https://t.me/{CHANNEL_CHAT_ID[1:]}"

# Liste complète des paires OTC
ACTIFS = [
    "🇦🇺 AUD/CAD 🇨🇦OTC", "🇨🇦 CAD/CHF 🇨🇭OTC", "🇨🇦 CAD/JPY 🇯🇵OTC", "🇨🇭 CHF/NOK 🇳🇴OTC",
    "🇪🇺 EUR/CHF 🇨🇭OTC", "🇪🇺 EUR/TRY 🇹🇷OTC", "🇪🇺 EUR/USD 🇺🇸OTC", "🇬🇧 GBP/AUD 🇦🇺OTC",
    "🇬🇧 GBP/JPY 🇯🇵OTC", "🇬🇧 GBP/USD 🇺🇸OTC", "🇰🇪 KES/USD 🇺🇸OTC", "🇳🇿 NZD/USD 🇺🇸OTC",
    "🇸🇦 SAR/CNY 🇨🇳OTC", "🇹🇳 TND/USD 🇺🇸OTC", "🇺🇦 UAH/USD 🇺🇸OTC", "🇺🇸 USD/BRL 🇧🇷OTC",
    "🇺🇸 USD/CAD 🇨🇦OTC", "🇺🇸 USD/CHF 🇨🇭OTC", "🇺🇸 USD/CLP 🇨🇱OTC", "🇺🇸 USD/COP 🇨🇴OTC",
    "🇺🇸 USD/EGP 🇪🇬OTC", "🇺🇸 USD/IDR 🇮🇩OTC", "🇺🇸 USD/PHP 🇵🇭OTC", "🇺🇸 USD/RUB 🇷🇺OTC",
    "🇺🇸 USD/THB 🇹🇭OTC", "🇾🇪 YER/USD 🇺🇸OTC",

    "🇴🇲 OMR/CNY 🇨🇳OTC", "🇺🇸 USD/BDT 🇧🇩OTC", "🇺🇸 USD/MXN 🇲🇽OTC", "🇪🇺 EUR/NZD 🇳🇿OTC",
    "🇪🇺 EUR/JPY 🇯🇵OTC", "🇧🇭 BHD/CNY 🇨🇳OTC",

    "🇦🇪 AED/CNY 🇨🇳OTC", "🇦🇺 AUD/NZD 🇳🇿OTC", "🇦🇺 AUD/CHF 🇨🇭OTC", "🇦🇺 AUD/JPY 🇯🇵OTC",
    "🇳🇬 NGN/USD 🇺🇸OTC", "🇨🇭 CHF/JPY 🇯🇵OTC", "🇲🇦 MAD/USD 🇺🇸OTC", "🇶🇦 QAR/CNY 🇨🇳OTC",
    "🇺🇸 USD/SGD 🇸🇬OTC", "🇺🇸 USD/ARS 🇦🇷OTC", "🇪🇺 EUR/RUB 🇷🇺OTC", "🇺🇸 USD/CNH 🇨🇳OTC",
    "🇺🇸 USD/JPY 🇯🇵OTC",

    "🇳🇿 NZD/JPY 🇯🇵OTC", "🇺🇸 USD/VND 🇻🇳OTC", "🇺🇸 USD/MYR 🇲🇾OTC", "🇿🇦 ZAR/USD 🇺🇸OTC",
    "🇦🇺 AUD/USD 🇺🇸OTC", "🇪🇺 EUR/GBP 🇬🇧OTC", "🇺🇸 USD/PKR 🇵🇰OTC", "🇺🇸 USD/DZD 🇩ℤOTC",

    "🇪🇺 EUR/HUF 🇭🇺OTC", "🇱🇧 LBP/USD 🇺🇸OTC", "🇯🇴 JOD/CNY 🇨🇳OTC", "🇺🇸 USD/INR 🇮🇳OTC"
]

# --- Constantes pour le BILAN ---
JOURS_FR = {
    0: "LUNDI", 1: "MARDI", 2: "MERCREDI", 3: "JEUDI",
    4: "VENDREDI", 5: "SAMEDI", 6: "DIMANCHE"
}
MOIS_FR = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "août",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre"
}
EXPOSANTS = {"mg0": "⁰", "mg1": "¹", "mg2": "²", "mg3": "³"}
TIME_KEY_FOR_RESULT = {"mg0": "entre", "mg1": "mg1", "mg2": "mg2", "mg3": "mg3", "lose": "entre"}
DIGIT_EMOJIS = {
    '0': '0️⃣', '1': '1️⃣', '2': '2️⃣', '3': '3️⃣', '4': '4️⃣',
    '5': '5️⃣', '6': '6️⃣', '7': '7️⃣', '8': '8️⃣', '9': '9️⃣'
}

# --- Constantes pour la SESSION VIP ---
VIP_SESSION_ORDER = ["matin", "midi", "soir", "nuit"]
VIP_SESSION_LABELS = {
    "matin": ("🌅", "MATIN"),
    "midi": ("☀️", "MIDI"),
    "soir": ("🌇", "SOIR"),
    "nuit": ("🌙", "NUIT"),
}


def empty_vip_history() -> dict:
    return {key: [] for key in VIP_SESSION_ORDER}


def is_authorized(chat_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def is_admin(chat_id: int) -> bool:
    return ADMIN_CHAT_ID is not None and chat_id == ADMIN_CHAT_ID


def remember_known_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    known = context.bot_data.setdefault('known_users', set())
    known.add(chat_id)


def log_conversation(context: ContextTypes.DEFAULT_TYPE, visitor_chat_id: int, sender: str, text: str):
    log = context.bot_data.setdefault('conversation_log', {})
    entries = log.setdefault(visitor_chat_id, [])
    entries.append({
        'from': sender,
        'text': text,
        'time': datetime.now(TZ),
    })
    if len(entries) > 200:
        del entries[:len(entries) - 200]


async def get_or_create_topic(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                               sender_name: str, username: str):
    if SUPPORT_GROUP_ID is None:
        return None

    topics = context.bot_data.setdefault('visitor_topics', {})
    if chat_id in topics:
        return topics[chat_id]

    topic_name = (sender_name or f"Visiteur {chat_id}").strip()[:100]
    try:
        topic = await context.bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=topic_name)
    except Exception as e:
        logger.warning(f"Impossible de créer un topic pour {chat_id} ({sender_name}) : {e}")
        return None

    thread_id = topic.message_thread_id
    topics[chat_id] = thread_id
    reverse = context.bot_data.setdefault('topic_to_visitor', {})
    reverse[thread_id] = chat_id

    try:
        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=f"🆕 Conversation avec {sender_name} {username} (id: {chat_id})",
        )
    except Exception as e:
        logger.warning(f"Impossible d'envoyer le message d'intro du topic {thread_id} : {e}")

    return thread_id


def remember_broadcast(context: ContextTypes.DEFAULT_TYPE, *, kind: str, text: str = None,
                        photo_file_id: str = None, parse_mode: str = None):
    context.user_data['last_broadcast'] = {
        'kind': kind,
        'text': text,
        'photo_file_id': photo_file_id,
        'parse_mode': parse_mode,
    }


def get_asset_filename(asset_string: str) -> str:
    clean_text = re.sub(r'[^a-zA-Z0-9]', '', asset_string).lower()
    if clean_text.endswith("otc"):
        clean_text = clean_text[:-3] + "_otc"
    return clean_text


def get_random_jpeg(directory: str):
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
        return None

    valid_extensions = ('.jpeg', '.jpg', '.png', '.heic')
    jpegs = [
        f for f in os.listdir(directory)
        if f.lower().endswith(valid_extensions)
    ]
    if not jpegs:
        return None
    return os.path.join(directory, random.choice(jpegs))


def get_specific_jpeg_only(directory: str, asset_filename: str):
    if not os.path.exists(directory) or not asset_filename:
        return None

    valid_extensions = ('.jpg', '.jpeg', '.png', '.heic')

    for file in os.listdir(directory):
        file_name_without_ext, file_ext = os.path.splitext(file)
        if file_name_without_ext.lower() == asset_filename.lower() and file_ext.lower() in valid_extensions:
            return os.path.join(directory, file)

    return None


def generate_signal_data():
    actif = random.choice(ACTIFS)
    direction = random.choice(["ACHAT", "VENTE"])
    now = datetime.now(TZ)

    entre = (now + timedelta(minutes=3)).replace(second=0, microsecond=0)
    mg1 = entre + timedelta(minutes=2)
    mg2 = entre + timedelta(minutes=4)
    mg3 = entre + timedelta(minutes=6)

    return {
        'actif': actif,
        'direction': direction,
        'entre': entre,
        'mg1': mg1,
        'mg2': mg2,
        'mg3': mg3
    }


def format_signal_text(signal: dict) -> str:
    lien_video = "https://t.me/LegitTrade_academy"
    lien_inscription = POCKET_OPTION_LINK

    return f"""<a href="{lien_video}"><b>VIDÉO D'INSCRIPTION</b></a>
______________________________
📊 <b>ACTIF:</b> {signal['actif']}
🕘 <b>HEURE D'ENTRÉE:</b> {signal['entre'].strftime('%H:%M')}
⏳ <b>EXPIRATION:</b> 120s (2min)

🔮 Direction: <b>{signal['direction']}</b>

🔘 <b>Martingales</b>
1️⃣ MG1: {signal['mg1'].strftime('%H:%M')}
2️⃣ MG2: {signal['mg2'].strftime('%H:%M')}
3️⃣ MG3: {signal['mg3'].strftime('%H:%M')}
———————————————————
<a href="{lien_inscription}"><b>INSCRIPTION</b></a>"""


def to_two_digit_emoji(n: int) -> str:
    s = f"{n:02d}"
    return "".join(DIGIT_EMOJIS[c] for c in s)


def format_bilan_text(
    history: list,
    header_title: str = "RAPPORT SESSION GRATUITE",
    blockquote_title: str = "🌑 Session gratuite",
) -> str:
    now = datetime.now(TZ)
    jour_nom = JOURS_FR[now.weekday()]
    date_str = f"{now.day} {MOIS_FR[now.month]} {now.year}"

    header = f"<b>{header_title}\n{jour_nom} {date_str}.</b>"

    lines = []
    for entry in history:
        result = entry['result']
        icon = "❌" if result == "lose" else "✅"
        exposant = EXPOSANTS.get(result, "")
        time_key = TIME_KEY_FOR_RESULT[result]
        heure = entry[time_key].strftime('%H:%M')
        lines.append(f"{icon}{exposant} {heure} • <b>{entry['actif']}</b> • {entry['direction']}")

    separator = "➖" * 15
    if lines:
        trades_block = "\n".join(lines)
    else:
        trades_block = "Aucun trade enregistré."

    blockquote = f"<blockquote>{blockquote_title}\n{separator}\n{trades_block}</blockquote>"

    gains = sum(1 for e in history if e['result'] != 'lose')
    pertes = sum(1 for e in history if e['result'] == 'lose')

    recap = f"✅ <b>GAIN</b> {to_two_digit_emoji(gains)} x {to_two_digit_emoji(pertes)} <b>PERTE</b> ❌"

    return f"{header}\n\n{blockquote}\n\n{recap}"


def format_full_vip_report(vip_history: dict) -> str:
    now = datetime.now(TZ)
    jour_nom = JOURS_FR[now.weekday()]
    date_str = f"{now.day} {MOIS_FR[now.month]} {now.year}"

    header = f"<b>RAPPORT SESSION VIP\n{jour_nom} {date_str}.</b>"
    separator = "➖" * 15

    blocks = []
    total_gains = 0
    total_pertes = 0

    for key in VIP_SESSION_ORDER:
        icon, label = VIP_SESSION_LABELS[key]
        entries = vip_history.get(key, [])

        lines = []
        for entry in entries:
            result = entry['result']
            picto = "❌" if result == "lose" else "✅"
            exposant = EXPOSANTS.get(result, "")
            time_key = TIME_KEY_FOR_RESULT[result]
            heure = entry[time_key].strftime('%H:%M')
            lines.append(f"{picto}{exposant} {heure} • <b>{entry['actif']}</b> • {entry['direction']}")

        trades_block = "\n".join(lines) if lines else "Aucun trade enregistré."
        blocks.append(f"<blockquote>{icon} SESSION {label}\n{separator}\n{trades_block}</blockquote>")

        total_gains += sum(1 for e in entries if e['result'] != 'lose')
        total_pertes += sum(1 for e in entries if e['result'] == 'lose')

    recap = f"✅ <b>GAIN</b> {to_two_digit_emoji(total_gains)} x {to_two_digit_emoji(total_pertes)} <b>PERTE</b> ❌"

    return header + "\n\n" + "\n\n".join(blocks) + "\n\n" + recap


def compute_stats(entries: list) -> dict:
    total = len(entries)
    gains = sum(1 for e in entries if e['result'] != 'lose')
    pertes = total - gains
    taux = (gains / total * 100) if total else 0.0

    stats_by_asset = defaultdict(lambda: {'gains': 0, 'pertes': 0})
    for e in entries:
        if e['result'] == 'lose':
            stats_by_asset[e['actif']]['pertes'] += 1
        else:
            stats_by_asset[e['actif']]['gains'] += 1

    best_asset = None
    worst_asset = None
    best_net = None
    worst_net = None
    for actif, s in stats_by_asset.items():
        net = s['gains'] - s['pertes']
        if best_net is None or net > best_net:
            best_net, best_asset = net, actif
        if worst_net is None or net < worst_net:
            worst_net, worst_asset = net, actif

    return {
        'total': total,
        'gains': gains,
        'pertes': pertes,
        'taux': taux,
        'best_asset': best_asset if best_net and best_net > 0 else None,
        'worst_asset': worst_asset if worst_net is not None and worst_net < 0 else None,
    }


def format_stats_block(title: str, entries: list) -> str:
    stats = compute_stats(entries)
    if stats['total'] == 0:
        return f"<b>{title}</b>\nAucun trade enregistré."

    lines = [
        f"<b>{title}</b>",
        f"📈 Trades : {stats['total']}",
        f"✅ Gains : {stats['gains']} | ❌ Pertes : {stats['pertes']}",
        f"🎯 Taux de réussite : {stats['taux']:.1f}%",
    ]
    if stats['best_asset']:
        lines.append(f"🏆 Meilleur actif : {stats['best_asset']}")
    if stats['worst_asset']:
        lines.append(f"⚠️ Actif à surveiller : {stats['worst_asset']}")

    return "\n".join(lines)


# --- CLAVIERS INLINE ---

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🆓 SESSION GRATUITE", callback_data="btn_free_menu"),
            InlineKeyboardButton("👑 SESSION VIP", callback_data="btn_vip_menu"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_broadcast_targets_keyboard() -> InlineKeyboardMarkup:
    """Clavier de choix de cible de diffusion (au début de chaque session)."""
    keyboard = [
        [InlineKeyboardButton(f"📡 {target}", callback_data=f"bcast_target_{i}")]
        for i, target in enumerate(BROADCAST_TARGETS)
    ]
    if BROADCAST_TARGETS:
        keyboard.append([InlineKeyboardButton("📤 ENVOYER À TOUS", callback_data="bcast_all")])
    return InlineKeyboardMarkup(keyboard)


def get_free_start_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal")]]
    return InlineKeyboardMarkup(keyboard)


def get_result_keyboard(is_vip: bool = False) -> InlineKeyboardMarkup:
    """
    Clavier SOUS LE SIGNAL.
    Gratuit : MG0/MG1/MG2/MG3/❌ + NEW + DIFFUSER.
    VIP     : idem + ⬅️ MENU VIP.
    """
    keyboard = [
        [
            InlineKeyboardButton("MG0", callback_data="res_mg0"),
            InlineKeyboardButton("MG1", callback_data="res_mg1"),
            InlineKeyboardButton("MG2", callback_data="res_mg2"),
            InlineKeyboardButton("MG3", callback_data="res_mg3"),
            InlineKeyboardButton("❌", callback_data="res_lose"),
        ],
        [
            InlineKeyboardButton("🔄 NEW", callback_data="btn_new"),
        ],
        [
            InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_signal"),
        ],
    ]
    if is_vip:
        keyboard.append([InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")])
    return InlineKeyboardMarkup(keyboard)


def get_signal_keyboard(include_undo: bool = True) -> InlineKeyboardMarkup:
    """
    Clavier SOUS LE RÉSULTAT (gratuit ET VIP) :
    SIGNAL / BILAN / ANNULER DERNIER.
    """
    keyboard = [
        [
            InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([InlineKeyboardButton("↩️ ANNULER DERNIER", callback_data="btn_undo")])
    return InlineKeyboardMarkup(keyboard)


def get_bilan_keyboard() -> InlineKeyboardMarkup:
    """Clavier sous le bilan GRATUIT (diffusion auto)."""
    keyboard = [
        [InlineKeyboardButton("NEW SESSION", callback_data="btn_new_session")],
        [InlineKeyboardButton("🏠 MENU PRINCIPAL", callback_data="btn_main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu VIP : 4 sous-sessions + rapport complet + retour."""
    keyboard = [
        [
            InlineKeyboardButton("🌅 MATIN", callback_data="vip_matin"),
            InlineKeyboardButton("☀️ MIDI", callback_data="vip_midi"),
        ],
        [
            InlineKeyboardButton("🌇 SOIR", callback_data="vip_soir"),
            InlineKeyboardButton("🌙 NUIT", callback_data="vip_nuit"),
        ],
        [InlineKeyboardButton("📊 RAPPORT COMPLET VIP", callback_data="btn_vip_rapport")],
        [InlineKeyboardButton("🏠 MENU PRINCIPAL", callback_data="btn_main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_signal_start_keyboard() -> InlineKeyboardMarkup:
    """Bouton SIGNAL + retour au menu VIP, affiché en entrant dans une sous-session."""
    keyboard = [
        [InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal")],
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_bilan_keyboard(session_key: str) -> InlineKeyboardMarkup:
    """Clavier sous le bilan d'une sous-session VIP (diffusion auto)."""
    keyboard = [
        [InlineKeyboardButton("🔄 RECOMMENCER CETTE SESSION", callback_data=f"vip_reset_{session_key}")],
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_rapport_keyboard() -> InlineKeyboardMarkup:
    """Clavier sous le rapport complet VIP (diffusion manuelle)."""
    keyboard = [
        [InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_signal")],
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def delete_message_safe(bot, chat_id, message_id) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.warning(f"Impossible de supprimer le message {message_id} dans {chat_id} : {e}")
        return False


async def send_transient(context: ContextTypes.DEFAULT_TYPE, chat_id, text, reply_markup=None, parse_mode=None):
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    context.user_data['last_transient_message'] = (chat_id, sent.message_id)
    return sent


async def clear_last_transient(context: ContextTypes.DEFAULT_TYPE):
    transient = context.user_data.pop('last_transient_message', None)
    if transient:
        t_chat, t_id = transient
        await delete_message_safe(context.bot, t_chat, t_id)


async def send_photo_safe(bot, chat_id, image_path, caption, reply_markup, parse_mode=None):
    if image_path:
        try:
            with open(image_path, 'rb') as photo:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.warning(f"Échec d'envoi de l'image '{image_path}' : {e}. Envoi du texte à la place.")

    return await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode=parse_mode,
        disable_web_page_preview=True,
        reply_markup=reply_markup
    )


async def broadcast_to_target(context: ContextTypes.DEFAULT_TYPE, content: dict, targets: list):
    """Diffuse le contenu vers la liste de cibles et mémorise les copies pour ANNULER DERNIER."""
    if not targets:
        return

    for target in targets:
        dest = normalize_broadcast_target(target)
        try:
            if content['kind'] == 'photo' and content.get('photo_file_id'):
                sent = await context.bot.send_photo(
                    chat_id=dest,
                    photo=content['photo_file_id'],
                    caption=content.get('text'),
                    parse_mode=content.get('parse_mode'),
                )
            else:
                sent = await context.bot.send_message(
                    chat_id=dest,
                    text=content.get('text') or '',
                    parse_mode=content.get('parse_mode'),
                    disable_web_page_preview=True,
                )
            trade = context.user_data.get('current_trade')
            if trade is not None:
                trade.setdefault('broadcasts', []).append((sent.chat_id, sent.message_id))
        except Exception as e:
            logger.warning(f"Échec de diffusion vers {target} : {e}")


def resolve_active_targets(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Retourne les cibles actives pour la session en cours (liste vide si aucune)."""
    choice = context.user_data.get('broadcast_target')
    if choice is None:
        return []
    if choice == 'all':
        return list(BROADCAST_TARGETS)
    try:
        idx = int(choice)
    except (TypeError, ValueError):
        return []
    if 0 <= idx < len(BROADCAST_TARGETS):
        return [BROADCAST_TARGETS[idx]]
    return []


# --- HANDLERS ---

async def show_main_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    await send_transient(
        context, chat_id,
        text="👇 Choisissez une option :",
        reply_markup=get_main_menu_keyboard()
    )


async def start_free_session(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Démarre une session gratuite : reset + choix de cible."""
    context.user_data['mode'] = 'free'
    context.user_data['history'] = []
    context.user_data['last_signal'] = None
    context.user_data['last_signal_message'] = None
    context.user_data['current_trade'] = None
    context.user_data.pop('broadcast_target', None)
    # Marqueur : on est dans le choix de cible pour la session gratuite
    context.user_data['pending_vip_target'] = False

    if not BROADCAST_TARGETS:
        await send_transient(
            context, chat_id,
            text="👇 Cliquez ci-dessous pour obtenir votre signal :",
            reply_markup=get_free_start_keyboard()
        )
        return

    await send_transient(
        context, chat_id,
        text="📤 Choisis ta cible de diffusion pour cette session :",
        reply_markup=get_broadcast_targets_keyboard()
    )


async def enter_vip_session(chat_id, context: ContextTypes.DEFAULT_TYPE, session_key: str, reset: bool = False):
    """Entre dans une sous-session VIP donnée."""
    context.user_data['mode'] = 'vip'
    context.user_data['vip_current'] = session_key
    context.user_data.setdefault('vip_history', empty_vip_history())
    if reset:
        context.user_data['vip_history'][session_key] = []
    context.user_data['last_signal'] = None
    context.user_data['last_signal_message'] = None
    context.user_data['current_trade'] = None

    icon, label = VIP_SESSION_LABELS[session_key]
    prefix = "🔄 Session réinitialisée.\n\n" if reset else ""
    await send_transient(
        context, chat_id,
        text=f"{prefix}👇 Session VIP - {icon} {label} : cliquez pour obtenir votre signal :",
        reply_markup=get_vip_signal_start_keyboard()
    )


async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if CHANNEL_CHAT_ID is None:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_CHAT_ID, user_id=user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        logger.warning(f"Impossible de vérifier l'abonnement au canal pour {user_id} : {e}")
        return True


async def send_channel_reminder(chat_id, context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_INVITE_LINK:
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Rejoindre le canal", url=CHANNEL_INVITE_LINK)]]
    )
    text = (
        "📢 Pour ne rater aucune stratégie ni aucun signal gratuit, "
        "rejoins mon canal officiel !"
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def send_welcome_messages(chat_id, context: ContextTypes.DEFAULT_TYPE, first_name: str = ""):
    """Deux messages d'accueil (tutoiement, 'ici' en gras dans le second)."""
    safe_name = html.escape(first_name) if first_name else ""
    name_part = f" {safe_name}" if safe_name else ""

    text1 = (
        f"Hey👋{name_part}, <b>bienvenue</b> 😃\n"
        "Je m'appelle <b>Prince</b> ! Je suis ravi de t'accueillir ici !\n\n"
        "Je suis <b>trader professionnel des options binaires</b> avec plus de "
        "<b>10 ans d'expérience</b> ! Je partage mes stratégies de trading "
        "<b>gratuitement</b> dans mon <b>groupe VIP</b> et je peux t'aider à gagner "
        "tes premiers <b>1000$</b> dans le trading des options binaires !"
    )
    await context.bot.send_message(chat_id=chat_id, text=text1, parse_mode="HTML")

    text2 = (
        "Envoie-moi ton message et je te réponds <b>le plus tôt possible</b> ⏱️\n\n"
        "<b>📝 INSCRIPTION</b>\n"
        f"Pour rejoindre le <b>VIP</b>, tu dois t'inscrire sur "
        f"<a href=\"{POCKET_OPTION_LINK}\"><b>Pocket Option</b></a>.\n"
        f"Crée un nouveau compte (<b>BONUS DE 30%</b>), et après avoir terminé l'inscription\n\n"
        f"❗️<b>ENVOIE TON ID</b> depuis ton compte "
        f"<a href=\"{POCKET_OPTION_LINK}\"><b>Pocket Option</b></a> <b>ici</b>\n"
        "______________________"
    )
    await context.bot.send_message(chat_id=chat_id, text=text2, parse_mode="HTML")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remember_known_user(context, chat_id)

    if not is_authorized(chat_id):
        sender = update.effective_user
        first_name = sender.first_name if sender and sender.first_name else ""
        await send_welcome_messages(chat_id, context, first_name)

        user_id = sender.id if sender else chat_id
        if not await is_channel_member(context, user_id):
            await send_channel_reminder(chat_id, context)
        return

    await clear_last_transient(context)
    await show_main_menu(chat_id, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await clear_last_transient(context)

    text = (
        "<b>ℹ️ AIDE</b>\n\n"
        "/start — Menu principal (session gratuite / VIP)\n"
        "/stats — Statistiques détaillées (taux de réussite, meilleur/pire actif)\n"
        "/broadcast <message> — (admin) Envoie un message à tous les utilisateurs connus\n"
        "/historique <chat_id> — (admin) Affiche l'échange enregistré avec ce visiteur\n"
        "/help — Affiche ce message\n\n"
        "🆓 <b>SESSION GRATUITE</b> — signaux + bilan classique\n"
        "👑 <b>SESSION VIP</b> — 4 sous-sessions (matin/midi/soir/nuit), accessibles librement, "
        "chacune avec son propre historique et son propre bilan\n"
        "📊 <b>RAPPORT COMPLET VIP</b> — récapitulatif regroupant les 4 sous-sessions\n"
        "↩️ <b>ANNULER DERNIER</b> — annule le dernier résultat enregistré par erreur\n"
        "🔄 <b>RECOMMENCER CETTE SESSION</b> — vide l'historique d'une seule sous-session VIP"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await clear_last_transient(context)

    history = context.user_data.get('history', [])
    vip_history = context.user_data.get('vip_history', empty_vip_history())
    vip_all_entries = [e for entries in vip_history.values() for e in entries]

    blocks = [format_stats_block("🆓 Session gratuite", history)]
    for key in VIP_SESSION_ORDER:
        icon, label = VIP_SESSION_LABELS[key]
        blocks.append(format_stats_block(f"{icon} VIP {label}", vip_history.get(key, [])))
    blocks.append(format_stats_block("👑 VIP (cumulé)", vip_all_entries))

    text = "<b>📊 STATISTIQUES</b>\n\n" + "\n\n".join(blocks)
    await update.message.reply_text(text, parse_mode="HTML")


async def daily_reset_job(context: ContextTypes.DEFAULT_TYPE):
    all_user_data = context.application.user_data
    count = 0
    for data in all_user_data.values():
        data['history'] = []
        data['vip_history'] = empty_vip_history()
        data['last_signal'] = None
        data['last_signal_message'] = None
        data['current_trade'] = None
        data['last_broadcast'] = None
        count += 1
    logger.info(f"Réinitialisation quotidienne effectuée pour {count} utilisateur(s).")


ANALYSIS_ANIMATION_FRAMES = [
    "⏳ Analyse de signal",
    "⏳ Analyse de signal.",
    "⏳ Analyse de signal..",
    "⏳ Analyse de signal...",
    "⌛ Analyse de signal...",
]


async def show_analysis_animation(context: ContextTypes.DEFAULT_TYPE, chat_id):
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=ANALYSIS_ANIMATION_FRAMES[0])
    except Exception as e:
        logger.warning(f"Impossible d'afficher l'animation d'analyse : {e}")
        return

    for frame in ANALYSIS_ANIMATION_FRAMES[1:]:
        await asyncio.sleep(1)
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=frame)
        except Exception:
            pass

    await asyncio.sleep(1)
    await delete_message_safe(context.bot, chat_id, msg.message_id)


async def send_signal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie un signal avec une image aléatoire du dossier IMG/."""
    chat_id = update.effective_chat.id
    await show_analysis_animation(context, chat_id)

    signal = generate_signal_data()
    message_text = format_signal_text(signal)

    asset_key = get_asset_filename(signal['actif'])
    context.user_data['last_asset'] = asset_key
    context.user_data['last_signal'] = signal

    image_path = get_random_jpeg(DIR_IMG)

    mode = context.user_data.get('mode', 'free')
    reply_markup = get_result_keyboard(is_vip=(mode == 'vip'))

    sent_message = await send_photo_safe(
        bot=context.bot,
        chat_id=chat_id,
        image_path=image_path,
        caption=message_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

    if sent_message:
        context.user_data['last_signal_message'] = (chat_id, sent_message.message_id)

        context.user_data['current_trade'] = {
            'signal_message': (chat_id, sent_message.message_id),
            'result_message': None,
            'broadcasts': [],
        }

        if sent_message.photo:
            remember_broadcast(
                context, kind='photo', text=message_text,
                photo_file_id=sent_message.photo[-1].file_id, parse_mode="HTML"
            )
        else:
            remember_broadcast(context, kind='text', text=message_text, parse_mode="HTML")


async def send_bilan_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bilan de la session GRATUITE (diffusé automatiquement)."""
    history = context.user_data.get('history', [])
    text = format_bilan_text(history)
    chat_id = update.effective_chat.id

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_bilan_keyboard()
    )
    remember_broadcast(context, kind='text', text=text, parse_mode="HTML")

    content = context.user_data.get('last_broadcast')
    targets = resolve_active_targets(context)
    if content and targets:
        await broadcast_to_target(context, content, targets)


async def send_vip_bilan_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bilan de la sous-session VIP en cours (diffusé automatiquement)."""
    chat_id = update.effective_chat.id
    session_key = context.user_data.get('vip_current')

    if session_key not in VIP_SESSION_LABELS:
        await show_main_menu(chat_id, context)
        return

    vip_history = context.user_data.setdefault('vip_history', empty_vip_history())
    entries = vip_history.get(session_key, [])
    icon, label = VIP_SESSION_LABELS[session_key]

    text = format_bilan_text(
        entries,
        header_title=f"RAPPORT SESSION VIP - {label}",
        blockquote_title=f"{icon} Session VIP {label}",
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_vip_bilan_keyboard(session_key)
    )
    remember_broadcast(context, kind='text', text=text, parse_mode="HTML")

    content = context.user_data.get('last_broadcast')
    targets = resolve_active_targets(context)
    if content and targets:
        await broadcast_to_target(context, content, targets)


async def send_vip_rapport_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rapport complet VIP (diffusion manuelle possible)."""
    chat_id = update.effective_chat.id
    vip_history = context.user_data.setdefault('vip_history', empty_vip_history())
    text = format_full_vip_report(vip_history)

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_vip_rapport_keyboard()
    )
    remember_broadcast(context, kind='text', text=text, parse_mode="HTML")


def record_result(context: ContextTypes.DEFAULT_TYPE, result_key: str):
    last_signal = context.user_data.get('last_signal')
    if last_signal is None:
        return

    mode = context.user_data.get('mode', 'free')

    if mode == 'vip':
        session_key = context.user_data.get('vip_current')
        vip_history = context.user_data.setdefault('vip_history', empty_vip_history())
        vip_history.setdefault(session_key, []).append({**last_signal, 'result': result_key})
    else:
        history = context.user_data.setdefault('history', [])
        history.append({**last_signal, 'result': result_key})

    context.user_data['last_signal'] = None


async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = update.effective_chat.id
    remember_known_user(context, chat_id)

    if not is_authorized(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="⛔ Accès non autorisé.")
        return

    await clear_last_transient(context)

    # --- Navigation générale ---

    if data == "btn_main_menu":
        # Retour au menu principal : on nettoie le flag VIP pour permettre
        # un nouveau choix de cible lors du prochain accès au VIP.
        context.user_data['pending_vip_target'] = False
        await show_main_menu(chat_id, context)
        return

    if data == "btn_free_menu":
        await start_free_session(chat_id, context)
        return

    if data == "btn_vip_menu":
        # Si on vient de l'extérieur du VIP (menu principal ou fin de session),
        # on marque qu'on est en train de choisir la cible pour le VIP.
        # Si on revient depuis une sous-session (mode encore 'vip'), on ne
        # réaffiche pas le choix de cible : on va directement au menu VIP.
        if context.user_data.get('mode') != 'vip':
            context.user_data.pop('broadcast_target', None)
            context.user_data['pending_vip_target'] = True

        if not BROADCAST_TARGETS:
            # Pas de cible configurée : on va directement au menu VIP.
            context.user_data['mode'] = 'vip'
            context.user_data['pending_vip_target'] = False
            await send_transient(
                context, chat_id,
                text="👑 Session VIP — choisissez une sous-session :",
                reply_markup=get_vip_menu_keyboard()
            )
            return

        if context.user_data.get('broadcast_target') is None:
            # Afficher le choix de cible (pour le VIP).
            await send_transient(
                context, chat_id,
                text="📤 Choisis ta cible de diffusion pour cette session VIP :",
                reply_markup=get_broadcast_targets_keyboard()
            )
            return

        # Cible déjà choisie (retour depuis une sous-session) : menu VIP.
        context.user_data['mode'] = 'vip'
        await send_transient(
            context, chat_id,
            text="👑 Session VIP — choisissez une sous-session :",
            reply_markup=get_vip_menu_keyboard()
        )
        return

    if data == "btn_vip_rapport":
        await send_vip_rapport_action(update, context)
        return

    # --- Choix de cible de diffusion ---

    if data == "bcast_all" or data.startswith("bcast_target_"):
        if data == "bcast_all":
            context.user_data['broadcast_target'] = 'all'
        else:
            idx = int(data.replace("bcast_target_", ""))
            if idx < 0 or idx >= len(BROADCAST_TARGETS):
                return
            context.user_data['broadcast_target'] = idx

        # Si on est en train de choisir pour le VIP → afficher le menu VIP.
        if context.user_data.get('pending_vip_target'):
            context.user_data['pending_vip_target'] = False
            context.user_data['mode'] = 'vip'
            await send_transient(
                context, chat_id,
                text="👑 Session VIP — choisissez une sous-session :",
                reply_markup=get_vip_menu_keyboard()
            )
            return

        # Sinon (session gratuite) → invite SIGNAL.
        context.user_data['mode'] = 'free'
        await send_transient(
            context, chat_id,
            text="👇 Cliquez ci-dessous pour obtenir votre signal :",
            reply_markup=get_free_start_keyboard()
        )
        return

    # --- Choix / reset d'une sous-session VIP ---

    if data in ("vip_matin", "vip_midi", "vip_soir", "vip_nuit"):
        session_key = data.split("_", 1)[1]
        await enter_vip_session(chat_id, context, session_key, reset=False)
        return

    if data.startswith("vip_reset_"):
        session_key = data.replace("vip_reset_", "")
        if session_key in VIP_SESSION_LABELS:
            await enter_vip_session(chat_id, context, session_key, reset=True)
        else:
            await show_main_menu(chat_id, context)
        return

    # --- Session gratuite : NEW SESSION ---

    if data == "btn_new_session":
        await start_free_session(chat_id, context)
        return

    # --- Commun gratuit / VIP : SIGNAL, BILAN, NEW ---

    if data == "btn_get_signal":
        await send_signal_action(update, context)
        return

    if data == "btn_bilan":
        if context.user_data.get('mode') == 'vip':
            await send_vip_bilan_action(update, context)
        else:
            await send_bilan_action(update, context)
        return

    if data == "btn_new":
        last_message = context.user_data.get('last_signal_message')
        target_message_id = last_message[1] if last_message else query.message.message_id
        target_chat_id = last_message[0] if last_message else chat_id

        try:
            await context.bot.delete_message(chat_id=target_chat_id, message_id=target_message_id)
        except Exception as e:
            logger.warning(f"Impossible de supprimer le message (NEW) : {e}")

        context.user_data['last_signal'] = None
        await send_signal_action(update, context)
        return

    if data == "btn_undo":
        mode = context.user_data.get('mode', 'free')

        if mode == 'vip':
            session_key = context.user_data.get('vip_current')
            vip_history = context.user_data.setdefault('vip_history', empty_vip_history())
            entries = vip_history.get(session_key, [])
        else:
            entries = context.user_data.setdefault('history', [])

        if not entries:
            reply_markup = get_signal_keyboard()
            await send_transient(
                context, chat_id,
                text="Aucun résultat à annuler pour le moment.",
                reply_markup=reply_markup,
            )
            return

        removed = entries.pop()

        trade = context.user_data.get('current_trade') or {}

        if trade.get('signal_message'):
            s_chat, s_id = trade['signal_message']
            await delete_message_safe(context.bot, s_chat, s_id)

        if trade.get('result_message'):
            r_chat, r_id = trade['result_message']
            await delete_message_safe(context.bot, r_chat, r_id)

        for b_chat, b_id in trade.get('broadcasts', []):
            await delete_message_safe(context.bot, b_chat, b_id)

        context.user_data['current_trade'] = None
        context.user_data['last_broadcast'] = None

        text = f"↩️ Dernier résultat annulé :\n{removed['actif']} • {removed['direction']}"
        reply_markup = get_signal_keyboard(include_undo=False)

        await send_transient(context, chat_id, text=text, reply_markup=reply_markup)
        return

    # --- Diffusion manuelle (signal ou rapport VIP) ---

    if data == "btn_broadcast_signal":
        content = context.user_data.get('last_broadcast')
        targets = resolve_active_targets(context)
        if content and targets:
            await broadcast_to_target(context, content, targets)
        return

    # --- Résultats ---

    win_map = {
        "res_mg0": ("✅<b>GAIN DIRECT</b>✅", "mg0"),
        "res_mg1": ("✅<b>GAIN MARTINGALE 1</b>✅", "mg1"),
        "res_mg2": ("✅<b>GAIN MARTINGALE 2</b>✅", "mg2"),
        "res_mg3": ("✅<b>GAIN MARTINGALE 3</b>✅", "mg3"),
    }

    if data in win_map:
        caption_text, result_key = win_map[data]
        record_result(context, result_key)

        last_asset = context.user_data.get('last_asset', None)
        image_path = get_specific_jpeg_only(DIR_WIN, last_asset)

        reply_markup = get_signal_keyboard()

        sent_message = await send_photo_safe(
            bot=context.bot,
            chat_id=chat_id,
            image_path=image_path,
            caption=caption_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

        if sent_message:
            trade = context.user_data.get('current_trade') or {}
            trade['result_message'] = (chat_id, sent_message.message_id)
            context.user_data['current_trade'] = trade
            if sent_message.photo:
                remember_broadcast(context, kind='photo', text=caption_text,
                                    photo_file_id=sent_message.photo[-1].file_id, parse_mode="HTML")
            else:
                remember_broadcast(context, kind='text', text=caption_text, parse_mode="HTML")

            content = context.user_data.get('last_broadcast')
            targets = resolve_active_targets(context)
            if content and targets:
                await broadcast_to_target(context, content, targets)
        return

    if data == "res_lose":
        record_result(context, "lose")

        caption_text = "❌<b>PERDU</b>❌"
        image_path = get_random_jpeg(DIR_LOSE)

        reply_markup = get_signal_keyboard()

        sent_message = await send_photo_safe(
            bot=context.bot,
            chat_id=chat_id,
            image_path=image_path,
            caption=caption_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

        if sent_message:
            trade = context.user_data.get('current_trade') or {}
            trade['result_message'] = (chat_id, sent_message.message_id)
            context.user_data['current_trade'] = trade
            if sent_message.photo:
                remember_broadcast(context, kind='photo', text=caption_text,
                                    photo_file_id=sent_message.photo[-1].file_id, parse_mode="HTML")
            else:
                remember_broadcast(context, kind='text', text=caption_text, parse_mode="HTML")

            content = context.user_data.get('last_broadcast')
            targets = resolve_active_targets(context)
            if content and targets:
                await broadcast_to_target(context, content, targets)
        return


async def send_to_visitor(context: ContextTypes.DEFAULT_TYPE, target_chat_id, text: str):
    kwargs = {'chat_id': target_chat_id, 'text': text}
    dm_topics = context.bot_data.get('visitor_dm_topic', {})
    dm_topic_id = dm_topics.get(target_chat_id)
    if dm_topic_id is not None:
        kwargs['direct_messages_topic_id'] = dm_topic_id
    await context.bot.send_message(**kwargs)


async def relay_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id

    if SUPPORT_GROUP_ID is not None and chat_id == SUPPORT_GROUP_ID:
        thread_id = message.message_thread_id
        if thread_id is None:
            return

        is_forward = bool(getattr(message, 'forward_origin', None) or getattr(message, 'forward_date', None))
        if is_forward:
            return

        reverse = context.bot_data.get('topic_to_visitor', {})
        target_chat_id = reverse.get(thread_id)
        if not target_chat_id:
            return

        try:
            await send_to_visitor(context, target_chat_id, message.text)
            log_conversation(context, target_chat_id, 'admin', message.text)
        except Exception as e:
            await message.reply_text(f"❌ Échec de l'envoi : {e}")
        return

    remember_known_user(context, chat_id)

    if ADMIN_CHAT_ID is None and SUPPORT_GROUP_ID is None:
        return

    if is_admin(chat_id):
        reply_to = message.reply_to_message
        if reply_to:
            relay_map = context.bot_data.get('relay_map', {})
            target_chat_id = relay_map.get(reply_to.message_id)
            if target_chat_id:
                try:
                    await send_to_visitor(context, target_chat_id, message.text)
                    log_conversation(context, target_chat_id, 'admin', message.text)
                    await message.reply_text("✅ Réponse envoyée.")
                except Exception as e:
                    await message.reply_text(f"❌ Échec de l'envoi : {e}")
                return
        return

    sender = update.effective_user
    sender_name = sender.full_name if sender else "Inconnu"
    username = f"@{sender.username}" if sender and sender.username else "(pas de pseudo)"

    dm_topic = getattr(message, 'direct_messages_topic', None)
    if dm_topic is not None:
        context.bot_data.setdefault('visitor_dm_topic', {})[chat_id] = dm_topic.topic_id

    log_conversation(context, chat_id, 'visitor', message.text)

    if SUPPORT_GROUP_ID is not None:
        thread_id = await get_or_create_topic(context, chat_id, sender_name, username)
        if thread_id is not None:
            try:
                await context.bot.forward_message(
                    chat_id=SUPPORT_GROUP_ID,
                    from_chat_id=chat_id,
                    message_id=message.message_id,
                    message_thread_id=thread_id,
                )
            except Exception as e:
                logger.warning(f"Échec de relais (topic) du message de {chat_id} : {e}")
            return

    if ADMIN_CHAT_ID is not None:
        try:
            forwarded = await context.bot.forward_message(
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=chat_id,
                message_id=message.message_id,
            )
            note = await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"☝️ Message de {sender_name} {username} (id: {chat_id})\nRéponds à ce message pour lui répondre.",
                reply_to_message_id=forwarded.message_id,
            )
            relay_map = context.bot_data.setdefault('relay_map', {})
            relay_map[forwarded.message_id] = chat_id
            relay_map[note.message_id] = chat_id
        except Exception as e:
            logger.warning(f"Échec de relais du message de {chat_id} : {e}")
            return


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Réservé à l'administrateur.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage : /broadcast <message>")
        return

    known_users = context.bot_data.get('known_users', set())
    sent, failed = 0, 0
    for uid in known_users:
        if uid == ADMIN_CHAT_ID:
            continue
        try:
            await send_to_visitor(context, uid, text)
            sent += 1
        except Exception as e:
            logger.warning(f"Échec de diffusion à {uid} : {e}")
            failed += 1

    await update.message.reply_text(f"📢 Diffusion terminée : {sent} envoyé(s), {failed} échec(s).")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Réservé à l'administrateur.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage : /historique <chat_id>\n"
            "L'id du visiteur est indiqué dans le nom du topic ou dans le message de relais."
        )
        return

    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id invalide (doit être un nombre).")
        return

    log = context.bot_data.get('conversation_log', {})
    entries = log.get(target, [])
    if not entries:
        await update.message.reply_text("Aucun échange enregistré avec ce chat_id.")
        return

    lines = []
    for entry in entries:
        who = "🧑 Visiteur" if entry['from'] == 'visitor' else "🧔 Toi"
        time_str = entry['time'].strftime('%d/%m %H:%M')
        lines.append(f"[{time_str}] {who} : {entry['text']}")

    full_text = f"🗂️ Historique avec {target} :\n\n" + "\n".join(lines)

    max_len = 3500
    for i in range(0, len(full_text), max_len):
        await update.message.reply_text(full_text[i:i + max_len])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception rencontrée lors du traitement d'une mise à jour :", exc_info=context.error)


def main():
    if not TOKEN:
        raise ValueError("Le TELEGRAM_TOKEN n'a pas été trouvé. Vérifiez votre fichier .env")

    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)

    api_request = HTTPXRequest(
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        write_timeout=CONNECT_TIMEOUT,
        pool_timeout=CONNECT_TIMEOUT,
    )
    polling_request = HTTPXRequest(
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT + 10,
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .request(api_request)
        .get_updates_request(polling_request)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("historique", history_command))
    app.add_handler(CallbackQueryHandler(handle_button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_incoming_message))

    app.add_error_handler(error_handler)

    if DAILY_RESET_ENABLED:
        if app.job_queue is None:
            logger.warning(
                "JobQueue indisponible : installez 'python-telegram-bot[job-queue]' "
                "pour activer le reset quotidien automatique."
            )
        else:
            app.job_queue.run_daily(
                daily_reset_job,
                time=dt_time(hour=DAILY_RESET_HOUR, minute=0, tzinfo=TZ),
            )
            logger.info(f"Reset quotidien programmé à {DAILY_RESET_HOUR:02d}:00 ({TIMEZONE_NAME}).")

    logger.info(f"Système détecté : {SYSTEM_OS} (Python {platform.python_version()})")
    logger.info(f"Persistance activée : {PERSISTENCE_FILE}")
    if ALLOWED_CHAT_IDS:
        logger.info(f"Accès restreint à {len(ALLOWED_CHAT_IDS)} chat_id(s).")
    else:
        logger.info("Aucune restriction d'accès configurée (ALLOWED_CHAT_IDS vide).")

    if ADMIN_CHAT_ID is not None:
        logger.info(f"Relais des messages activé vers l'administrateur (chat_id={ADMIN_CHAT_ID}).")
    else:
        logger.warning(
            "ADMIN_CHAT_ID non configuré : les messages reçus des visiteurs ne seront pas relayés. "
            "Ajoute ADMIN_CHAT_ID dans le .env pour activer cette fonctionnalité."
        )

    if SUPPORT_GROUP_ID is not None:
        logger.info(f"Groupe de support avec Topics activé (chat_id={SUPPORT_GROUP_ID}).")
    else:
        logger.info("SUPPORT_GROUP_ID non configuré : relais en mode direct uniquement.")

    if CHANNEL_CHAT_ID is not None:
        if CHANNEL_INVITE_LINK:
            logger.info(f"Rappel d'abonnement au canal activé (chat_id={CHANNEL_CHAT_ID}).")
        else:
            logger.warning(
                f"CHANNEL_CHAT_ID configuré ({CHANNEL_CHAT_ID}) mais CHANNEL_INVITE_LINK est vide : "
                "le bouton 'Rejoindre le canal' ne sera pas affiché. Renseigne CHANNEL_INVITE_LINK "
                "dans le .env (utile notamment pour les canaux privés)."
            )
    else:
        logger.info("CHANNEL_CHAT_ID non configuré : pas de rappel d'abonnement au canal.")

    logger.info("Bot prêt et démarré !")

    try:
        app.run_polling()
    except (NetworkError, TimedOut) as e:
        logger.error(
            "Impossible de contacter Telegram (api.telegram.org). Vérifiez : "
            "1) votre connexion internet, 2) qu'un VPN n'est pas nécessaire "
            "(Telegram est bloqué dans certains pays/réseaux), 3) qu'aucun pare-feu "
            f"ne bloque l'application. Détail technique : {e}"
        )
    except KeyboardInterrupt:
        logger.info("Arrêt du bot demandé par l'utilisateur.")


if __name__ == "__main__":
    main()
