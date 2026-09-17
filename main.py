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
# Le bot fonctionne sur Windows, macOS, Linux et Termux (Android) sans configuration
# manuelle. Sous Windows, la console n'utilise pas UTF-8 par défaut : sans ce correctif,
# les accents et emojis dans les logs peuvent provoquer une UnicodeEncodeError.
SYSTEM_OS = platform.system()  # 'Windows', 'Darwin' (macOS), 'Linux' (dont Termux)

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
# Définir TIMEZONE dans le .env (ex: "Europe/Paris", "Africa/Abidjan"). Par défaut : UTC.
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
# Définir ALLOWED_CHAT_IDS dans le .env (ids séparés par des virgules) pour restreindre l'accès.
# Laisser vide = bot ouvert à tout le monde (comportement par défaut).
_raw_allowed = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = {
    int(x.strip()) for x in _raw_allowed.split(",") if x.strip().lstrip("-").isdigit()
}

# --- Administrateur (relais des messages + broadcast) ---
# Le chat_id qui recevra les messages des visiteurs et pourra utiliser /broadcast.
# Si non défini mais qu'un seul ALLOWED_CHAT_IDS est configuré, celui-ci est utilisé automatiquement.
_admin_env = os.getenv("ADMIN_CHAT_ID", "").strip()
if _admin_env.lstrip("-").isdigit():
    ADMIN_CHAT_ID = int(_admin_env)
elif len(ALLOWED_CHAT_IDS) == 1:
    ADMIN_CHAT_ID = next(iter(ALLOWED_CHAT_IDS))
else:
    ADMIN_CHAT_ID = None

# --- Groupe de support avec Topics (fil de discussion par visiteur, optionnel) ---
# ID du supergroupe (négatif) où les Topics/Sujets sont activés. Le bot doit y être admin
# avec le droit "Gérer les sujets". Si non configuré, le relais se fait en message privé simple.
_support_group_env = os.getenv("SUPPORT_GROUP_ID", "").strip()
SUPPORT_GROUP_ID = int(_support_group_env) if _support_group_env.lstrip("-").isdigit() else None

# --- Reset quotidien automatique (optionnel) ---
DAILY_RESET_ENABLED = os.getenv("DAILY_RESET_ENABLED", "false").lower() == "true"
DAILY_RESET_HOUR = int(os.getenv("DAILY_RESET_HOUR", "0"))

# --- Timeouts réseau (utile sur connexion lente/instable, ex: données mobiles) ---
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "20"))

# --- Diffusion vers canaux/groupes ---
# Liste des canaux/groupes où le bot peut publier, séparés par des virgules dans le .env.
# Formats acceptés : "@moncanal" (public) ou "-1001234567890" (id numérique, canal/groupe privé).
# Le bot doit être administrateur (avec droit de publication) dans chacun d'eux.
_raw_broadcast = os.getenv("BROADCAST_TARGETS", "")
BROADCAST_TARGETS = [t.strip() for t in _raw_broadcast.split(",") if t.strip()]

# --- Emojis Telegram Premium (optionnel) ---
# Format dans le .env : "✅:5368324170671202286,❌:5368324170671202287" (emoji:emoji_id)
# Récupère chaque emoji_id via un bot comme @idcheckbot en lui envoyant l'emoji premium voulu.
# Ne s'applique que si l'admin a choisi PREMIUM (et que Telegram confirme ce statut).
_raw_premium_emoji = os.getenv("PREMIUM_EMOJI_MAP", "").strip()
PREMIUM_EMOJI_MAP = {}
for _pair in _raw_premium_emoji.split(","):
    if ":" in _pair:
        _char, _eid = _pair.split(":", 1)
        PREMIUM_EMOJI_MAP[_char.strip()] = _eid.strip()


def emojify(context: ContextTypes.DEFAULT_TYPE, char: str) -> str:
    """
    Retourne la version emoji Telegram Premium animée (balise tg-emoji) de `char` si l'admin
    est en tier Premium et que son emoji_id est configuré dans PREMIUM_EMOJI_MAP ; sinon,
    retourne l'emoji normal tel quel. Nécessite parse_mode="HTML" sur le message.
    """
    if context.user_data.get('telegram_tier') == 'premium':
        emoji_id = PREMIUM_EMOJI_MAP.get(char)
        if emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'
    return char



def normalize_broadcast_target(target: str):
    """Convertit un identifiant de cible en int (id numérique) ou str (@username)."""
    t = target.strip()
    if t.lstrip("-").isdigit():
        return int(t)
    return t

# Dossiers d'images
DIR_IMG = "IMG"
DIR_WIN = "IMG_WIN"
DIR_LOSE = "IMG_LOSE"

# Lien d'inscription PocketOption (utilisé dans les signaux et le message d'accueil)
POCKET_OPTION_LINK = "https://bit.ly/4ckz9cY"

# --- Canal Telegram à promouvoir (optionnel) ---
# CHANNEL_CHAT_ID : @username (canal public) ou id numérique négatif (canal privé).
# Le bot doit être administrateur de ce canal pour pouvoir vérifier qui y est abonné.
_channel_env = os.getenv("CHANNEL_CHAT_ID", "").strip()
if _channel_env.lstrip("-").isdigit():
    CHANNEL_CHAT_ID = int(_channel_env)
elif _channel_env:
    CHANNEL_CHAT_ID = _channel_env if _channel_env.startswith("@") else f"@{_channel_env}"
else:
    CHANNEL_CHAT_ID = None

# Lien affiché sur le bouton "Rejoindre le canal". Déduit automatiquement si CHANNEL_CHAT_ID
# est un @username public ; à renseigner manuellement (lien d'invitation) si le canal est privé.
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "").strip()
if not CHANNEL_INVITE_LINK and isinstance(CHANNEL_CHAT_ID, str) and CHANNEL_CHAT_ID.startswith("@"):
    CHANNEL_INVITE_LINK = f"https://t.me/{CHANNEL_CHAT_ID[1:]}"

# Liste complète des paires OTC
ACTIFS = [
    # Paires à 92%
    "🇦🇺 AUD/CAD 🇨🇦OTC", "🇨🇦 CAD/CHF 🇨🇭OTC", "🇨🇦 CAD/JPY 🇯🇵OTC", "🇨🇭 CHF/NOK 🇳🇴OTC",
    "🇪🇺 EUR/CHF 🇨🇭OTC", "🇪🇺 EUR/TRY 🇹🇷OTC", "🇪🇺 EUR/USD 🇺🇸OTC", "🇬🇧 GBP/AUD 🇦🇺OTC",
    "🇬🇧 GBP/JPY 🇯🇵OTC", "🇬🇧 GBP/USD 🇺🇸OTC", "🇰🇪 KES/USD 🇺🇸OTC", "🇳🇿 NZD/USD 🇺🇸OTC",
    "🇸🇦 SAR/CNY 🇨🇳OTC", "🇹🇳 TND/USD 🇺🇸OTC", "🇺🇦 UAH/USD 🇺🇸OTC", "🇺🇸 USD/BRL 🇧🇷OTC",
    "🇺🇸 USD/CAD 🇨🇦OTC", "🇺🇸 USD/CHF 🇨🇭OTC", "🇺🇸 USD/CLP 🇨🇱OTC", "🇺🇸 USD/COP 🇨🇴OTC",
    "🇺🇸 USD/EGP 🇪🇬OTC", "🇺🇸 USD/IDR 🇮🇩OTC", "🇺🇸 USD/PHP 🇵🇭OTC", "🇺🇸 USD/RUB 🇷🇺OTC",
    "🇺🇸 USD/THB 🇹🇭OTC", "🇾🇪 YER/USD 🇺🇸OTC",

    # Paires de 85% à 91%
    "🇴🇲 OMR/CNY 🇨🇳OTC", "🇺🇸 USD/BDT 🇧🇩OTC", "🇺🇸 USD/MXN 🇲🇽OTC", "🇪🇺 EUR/NZD 🇳🇿OTC",
    "🇪🇺 EUR/JPY 🇯🇵OTC", "🇧🇭 BHD/CNY 🇨🇳OTC",

    # Paires de 75% à 84%
    "🇦🇪 AED/CNY 🇨🇳OTC", "🇦🇺 AUD/NZD 🇳🇿OTC", "🇦🇺 AUD/CHF 🇨🇭OTC", "🇦🇺 AUD/JPY 🇯🇵OTC",
    "🇳🇬 NGN/USD 🇺🇸OTC", "🇨🇭 CHF/JPY 🇯🇵OTC", "🇲🇦 MAD/USD 🇺🇸OTC", "🇶🇦 QAR/CNY 🇨🇳OTC",
    "🇺🇸 USD/SGD 🇸🇬OTC", "🇺🇸 USD/ARS 🇦🇷OTC", "🇪🇺 EUR/RUB 🇷🇺OTC", "🇺🇸 USD/CNH 🇨🇳OTC",
    "🇺🇸 USD/JPY 🇯🇵OTC",

    # Paires de 65% à 74%
    "🇳🇿 NZD/JPY 🇯🇵OTC", "🇺🇸 USD/VND 🇻🇳OTC", "🇺🇸 USD/MYR 🇲🇾OTC", "🇿🇦 ZAR/USD 🇺🇸OTC",
    "🇦🇺 AUD/USD 🇺🇸OTC", "🇪🇺 EUR/GBP 🇬🇧OTC", "🇺🇸 USD/PKR 🇵🇰OTC", "🇺🇸 USD/DZD 🇩ℤOTC",

    # Paires inférieures à 65%
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
    """Retourne un dictionnaire d'historique VIP vide pour les 4 sous-sessions."""
    return {key: [] for key in VIP_SESSION_ORDER}


def is_authorized(chat_id: int) -> bool:
    """Vérifie si le chat_id est autorisé. Si ALLOWED_CHAT_IDS est vide, tout le monde est autorisé."""
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def is_admin(chat_id: int) -> bool:
    """Vérifie si le chat_id est celui de l'administrateur configuré."""
    return ADMIN_CHAT_ID is not None and chat_id == ADMIN_CHAT_ID


def make_visitor_key(chat_id: int, dm_topic_id=None) -> str:
    """
    Construit la clé identifiant un visiteur. Pour un chat privé classique, c'est simplement
    le chat_id. Pour un visiteur ayant écrit via les "Messages directs" d'un canal, plusieurs
    personnes partagent le même chat_id (celui du canal) : on y ajoute alors le topic_id
    (propre à chaque abonné) pour ne jamais les confondre.
    """
    if dm_topic_id is None:
        return str(chat_id)
    return f"{chat_id}:{dm_topic_id}"


def parse_visitor_key(key: str):
    """Extrait (chat_id, dm_topic_id) d'une clé visiteur. dm_topic_id vaut None si absent."""
    key = str(key)
    if ":" in key:
        chat_part, topic_part = key.split(":", 1)
        return int(chat_part), int(topic_part)
    return int(key), None


def remember_known_user(context: ContextTypes.DEFAULT_TYPE, visitor_key: str):
    """Ajoute cette clé visiteur à la liste des utilisateurs connus, pour permettre le /broadcast."""
    known = context.bot_data.setdefault('known_users', set())
    known.add(visitor_key)


def log_conversation(context: ContextTypes.DEFAULT_TYPE, visitor_key: str, sender: str, text: str):
    """Enregistre un message échangé avec un visiteur (pour la commande /historique)."""
    log = context.bot_data.setdefault('conversation_log', {})
    entries = log.setdefault(visitor_key, [])
    entries.append({
        'from': sender,  # 'visitor' ou 'admin'
        'text': text,
        'time': datetime.now(TZ),
    })
    # Limite pour éviter une croissance illimitée de la mémoire
    if len(entries) > 200:
        del entries[:len(entries) - 200]


async def get_or_create_topic(context: ContextTypes.DEFAULT_TYPE, visitor_key: str,
                               sender_name: str, username: str):
    """
    Retourne le message_thread_id du topic dédié à ce visiteur dans le groupe de support,
    en le créant s'il n'existe pas encore. Retourne None si SUPPORT_GROUP_ID n'est pas
    configuré ou si la création échoue (groupe sans Topics activés, bot non admin, etc.).
    """
    if SUPPORT_GROUP_ID is None:
        return None

    topics = context.bot_data.setdefault('visitor_topics', {})
    if visitor_key in topics:
        return topics[visitor_key]

    topic_name = (sender_name or f"Visiteur {visitor_key}").strip()[:100]
    try:
        topic = await context.bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=topic_name)
    except Exception as e:
        logger.warning(f"Impossible de créer un topic pour {visitor_key} ({sender_name}) : {e}")
        return None

    thread_id = topic.message_thread_id
    topics[visitor_key] = thread_id
    reverse = context.bot_data.setdefault('topic_to_visitor', {})
    reverse[thread_id] = visitor_key

    try:
        await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=f"🆕 Conversation avec {sender_name} {username} (clé: {visitor_key})",
        )
    except Exception as e:
        logger.warning(f"Impossible d'envoyer le message d'intro du topic {thread_id} : {e}")

    return thread_id


def remember_broadcast(context: ContextTypes.DEFAULT_TYPE, *, kind: str, text: str = None,
                        photo_file_id: str = None, parse_mode: str = None):
    """Mémorise le dernier contenu envoyé en privé (signal ou résultat), pour la diffusion automatique."""
    context.user_data['last_broadcast'] = {
        'kind': kind,  # 'photo' ou 'text'
        'text': text,
        'photo_file_id': photo_file_id,
        'parse_mode': parse_mode,
    }


async def auto_broadcast_last(context: ContextTypes.DEFAULT_TYPE):
    """
    Diffuse automatiquement le dernier contenu (signal ou résultat) vers la cible choisie
    pour cette session (context.user_data['active_broadcast_target']), sans intervention
    manuelle. Ne fait rien si aucune cible n'a été choisie pour cette session.
    """
    target_setting = context.user_data.get('active_broadcast_target')
    if not target_setting:
        return

    content = context.user_data.get('last_broadcast')
    if not content:
        return

    targets = BROADCAST_TARGETS if target_setting == 'all' else [target_setting]

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
            # Mémorise cette copie pour pouvoir la supprimer via ANNULER DERNIER
            trade = context.user_data.get('current_trade')
            if trade is not None:
                trade.setdefault('broadcasts', []).append((sent.chat_id, sent.message_id))
        except Exception as e:
            logger.warning(f"Échec de diffusion automatique vers {target} : {e}")


async def send_diffusion_post(context: ContextTypes.DEFAULT_TYPE, dest, draft: dict):
    """Envoie le post composé via DIFFUSION (texte et/ou photo + boutons-liens) vers une destination."""
    markup = None
    if draft.get('buttons'):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)] for label, url in draft['buttons']])

    if draft.get('photo_file_id'):
        await context.bot.send_photo(
            chat_id=dest,
            photo=draft['photo_file_id'],
            caption=draft.get('text'),
            reply_markup=markup,
        )
    else:
        await context.bot.send_message(
            chat_id=dest,
            text=draft.get('text') or '',
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def diffuse_capture_photo(context: ContextTypes.DEFAULT_TYPE, photo_file_id: str):
    """Diffuse une capture d'écran (envoyée via le bouton CAPTURE) vers la cible active de la session,
    avec un bouton "PARTAGEZ VOS RÉSULTATS" pointant vers le bot."""
    target_setting = context.user_data.get('active_broadcast_target')
    if not target_setting:
        return

    share_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("PARTAGEZ VOS RÉSULTATS 〽️", url="https://t.me/VIPLegit_bot")]]
    )

    targets = BROADCAST_TARGETS if target_setting == 'all' else [target_setting]
    for target in targets:
        dest = normalize_broadcast_target(target)
        try:
            await context.bot.send_photo(chat_id=dest, photo=photo_file_id, reply_markup=share_keyboard)
        except Exception as e:
            logger.warning(f"Échec de diffusion de la capture vers {target} : {e}")


def get_asset_filename(asset_string: str) -> str:
    """Transforme '🇪🇺 EUR/CHF 🇨🇭OTC' en 'eurchf_otc' de manière propre."""
    clean_text = re.sub(r'[^a-zA-Z0-9]', '', asset_string).lower()
    if clean_text.endswith("otc"):
        clean_text = clean_text[:-3] + "_otc"
    return clean_text


def get_random_jpeg(directory: str):
    """Récupère une image aléatoire (supporte .jpg, .jpeg, .png, .heic)."""
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
    """
    Scanne le dossier et trouve l'image correspondant à asset_filename,
    insensible aux majuscules/minuscules (.JPG, .jpg, .png, .heic).
    """
    if not os.path.exists(directory) or not asset_filename:
        return None

    valid_extensions = ('.jpg', '.jpeg', '.png', '.heic')

    for file in os.listdir(directory):
        file_name_without_ext, file_ext = os.path.splitext(file)
        if file_name_without_ext.lower() == asset_filename.lower() and file_ext.lower() in valid_extensions:
            return os.path.join(directory, file)

    return None


def generate_signal_data():
    """Génère les données temporelles d'un signal."""
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
    """Formate le texte du signal."""
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
    """Transforme un entier en 2 chiffres emoji (ex: 3 -> 0️⃣3️⃣)."""
    s = f"{n:02d}"
    return "".join(DIGIT_EMOJIS[c] for c in s)


def format_bilan_text(
    history: list,
    header_title: str = "RAPPORT SESSION GRATUITE",
    blockquote_title: str = "🌑 Session gratuite",
) -> str:
    """Formate le texte du bilan d'une session, dans le style de la capture d'écran."""
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
    """
    Formate le rapport complet VIP : un bloc par sous-session (matin/midi/soir/nuit)
    dans le style de la capture d'écran (DAILY REPORT), suivi du total global.
    """
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
    """Calcule les statistiques (taux de réussite, meilleur/pire actif) sur une liste de trades."""
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
    """Formate un bloc de statistiques pour une session ou sous-session donnée."""
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
    """Menu principal : choix entre session gratuite, session VIP et diffusion libre."""
    keyboard = [
        [
            InlineKeyboardButton("🆓 SESSION GRATUITE", callback_data="btn_free_menu"),
            InlineKeyboardButton("👑 SESSION VIP", callback_data="btn_vip_menu"),
        ],
        [InlineKeyboardButton("📢 DIFFUSION", callback_data="btn_diffusion_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_free_start_keyboard() -> InlineKeyboardMarkup:
    """Bouton SIGNAL uniquement, affiché après le choix de la session gratuite."""
    keyboard = [[InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal")]]
    return InlineKeyboardMarkup(keyboard)


def get_signal_keyboard(include_undo: bool = True) -> InlineKeyboardMarkup:
    """Boutons CAPTURE, BILAN et ANNULER (optionnel) (session gratuite)."""
    keyboard = [
        [
            InlineKeyboardButton("📸 CAPTURE", callback_data="btn_capture"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([InlineKeyboardButton("↩️ ANNULER DERNIER", callback_data="btn_undo")])
    return InlineKeyboardMarkup(keyboard)


def get_result_keyboard() -> InlineKeyboardMarkup:
    """Boutons de résultat sous le signal, + bouton NEW et DIFFUSER (manuel). Utilisé en gratuit et en VIP."""
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
        [InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_bilan_keyboard() -> InlineKeyboardMarkup:
    """Boutons NEW SESSION et MENU PRINCIPAL, affichés sous le bilan gratuit."""
    keyboard = [
        [InlineKeyboardButton("NEW SESSION", callback_data="btn_new_session")],
        [InlineKeyboardButton("🏠 MENU PRINCIPAL", callback_data="btn_main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu VIP : choix libre de la sous-session + rapport complet + retour."""
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


def get_vip_result_keyboard(include_undo: bool = True) -> InlineKeyboardMarkup:
    """Boutons CAPTURE / BILAN / ANNULER (optionnel) / MENU VIP, après un résultat en session VIP."""
    keyboard = [
        [
            InlineKeyboardButton("📸 CAPTURE", callback_data="btn_capture"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([InlineKeyboardButton("↩️ ANNULER DERNIER", callback_data="btn_undo")])
    keyboard.append([InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")])
    return InlineKeyboardMarkup(keyboard)


def get_vip_bilan_keyboard(session_key: str) -> InlineKeyboardMarkup:
    """Boutons affichés sous le bilan d'une sous-session VIP."""
    keyboard = [
        [InlineKeyboardButton("🔄 RECOMMENCER CETTE SESSION", callback_data=f"vip_reset_{session_key}")],
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_rapport_keyboard() -> InlineKeyboardMarkup:
    """Bouton retour, affiché sous le rapport complet VIP."""
    keyboard = [
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_session_broadcast_choice_keyboard() -> InlineKeyboardMarkup:
    """Choix de la cible de diffusion pour toute la session (signal + résultat, automatique ensuite)."""
    keyboard = [
        [InlineKeyboardButton(f"📡 {target}", callback_data=f"setbcast_target_{i}")]
        for i, target in enumerate(BROADCAST_TARGETS)
    ]
    if BROADCAST_TARGETS:
        keyboard.append([InlineKeyboardButton("📤 TOUS", callback_data="setbcast_all")])
    keyboard.append([InlineKeyboardButton("🚫 Ne pas diffuser", callback_data="setbcast_none")])
    return InlineKeyboardMarkup(keyboard)


def get_diffusion_target_keyboard() -> InlineKeyboardMarkup:
    """Choix de la cible pour une publication libre (DIFFUSION) : canaux/groupes, TOUS ou ABONNÉS."""
    keyboard = [
        [InlineKeyboardButton(f"📡 {target}", callback_data=f"diffchoice_target_{i}")]
        for i, target in enumerate(BROADCAST_TARGETS)
    ]
    if BROADCAST_TARGETS:
        keyboard.append([InlineKeyboardButton("📤 TOUS", callback_data="diffchoice_all")])
    keyboard.append([InlineKeyboardButton("👥 ABONNÉS", callback_data="diffchoice_subscribers")])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="diffchoice_cancel")])
    return InlineKeyboardMarkup(keyboard)


def get_diffusion_add_button_keyboard(count: int) -> InlineKeyboardMarkup:
    """Proposé après le texte/l'image, et après chaque bouton ajouté (max 3)."""
    keyboard = []
    if count < 3:
        keyboard.append([InlineKeyboardButton("➕ Ajouter un bouton-lien", callback_data="diffbtn_add")])
    keyboard.append([InlineKeyboardButton("✅ Terminer et diffuser", callback_data="diffbtn_finish")])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="diffchoice_cancel")])
    return InlineKeyboardMarkup(keyboard)


async def delete_message_safe(bot, chat_id, message_id) -> bool:
    """Tente de supprimer un message (chat privé, groupe ou canal). N'échoue jamais bruyamment."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.warning(f"Impossible de supprimer le message {message_id} dans {chat_id} : {e}")
        return False


async def send_transient(context: ContextTypes.DEFAULT_TYPE, chat_id, text, reply_markup=None, parse_mode=None):
    """
    Envoie un message de navigation/confirmation "de passage" (menus, invites, accusés de
    diffusion ou d'annulation). Contrairement aux signaux, résultats et bilans, ces messages
    ne sont pas destinés à rester dans le chat : ils sont automatiquement supprimés dès que
    l'utilisateur appuie sur un bouton suivant (voir clear_last_transient).
    """
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
    """Supprime le dernier message 'de passage' encore affiché, avant de traiter une nouvelle action."""
    transient = context.user_data.pop('last_transient_message', None)
    if transient:
        t_chat, t_id = transient
        await delete_message_safe(context.bot, t_chat, t_id)


async def send_photo_safe(bot, chat_id, image_path, caption, reply_markup, parse_mode=None):
    """
    Tente d'envoyer une photo. Si Telegram refuse l'image (fichier corrompu,
    format non supporté comme certains .heic, IMAGE_PROCESS_FAILED, etc.),
    retombe automatiquement sur un message texte au lieu de faire planter le bot.
    """
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


# --- HANDLERS ---

async def show_main_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu principal (choix entre session gratuite et session VIP).
    Ne réinitialise aucune donnée : sert uniquement de navigation."""
    await send_transient(
        context, chat_id,
        text="👇 Choisissez une option :",
        reply_markup=get_main_menu_keyboard()
    )


async def prompt_signal_start(chat_id, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""):
    """Affiche l'invite 'cliquez pour obtenir un signal', adaptée au mode actif (gratuit ou VIP)."""
    mode = context.user_data.get('mode', 'free')
    if mode == 'vip':
        session_key = context.user_data.get('vip_current')
        icon, label = VIP_SESSION_LABELS.get(session_key, ("", "?"))
        text = f"{prefix}👇 Session VIP - {icon} {label} : cliquez pour obtenir votre signal :"
        keyboard = get_vip_signal_start_keyboard()
    else:
        text = f"{prefix}👇 Cliquez ci-dessous pour obtenir votre signal :"
        keyboard = get_free_start_keyboard()
    await send_transient(context, chat_id, text=text, reply_markup=keyboard)


async def start_free_session(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Réinitialise la session gratuite (historique du bilan) et démarre le choix de diffusion."""
    context.user_data['mode'] = 'free'
    context.user_data['history'] = []
    context.user_data['last_signal'] = None
    context.user_data['last_signal_message'] = None
    context.user_data['current_trade'] = None
    context.user_data['active_broadcast_target'] = None

    if BROADCAST_TARGETS:
        await send_transient(
            context, chat_id,
            text="📤 Où veux-tu diffuser les signaux de cette session ?",
            reply_markup=get_session_broadcast_choice_keyboard()
        )
    else:
        await prompt_signal_start(chat_id, context)


async def enter_vip_session(chat_id, context: ContextTypes.DEFAULT_TYPE, session_key: str, reset: bool = False):
    """Bascule le contexte utilisateur sur une sous-session VIP donnée, puis démarre le choix de diffusion.
    Si reset=True, vide l'historique de cette sous-session uniquement."""
    context.user_data['mode'] = 'vip'
    context.user_data['vip_current'] = session_key
    context.user_data.setdefault('vip_history', empty_vip_history())
    if reset:
        context.user_data['vip_history'][session_key] = []
    context.user_data['last_signal'] = None
    context.user_data['last_signal_message'] = None
    context.user_data['current_trade'] = None
    context.user_data['active_broadcast_target'] = None

    prefix = "🔄 Session réinitialisée.\n\n" if reset else ""
    if BROADCAST_TARGETS:
        await send_transient(
            context, chat_id,
            text=f"{prefix}📤 Où veux-tu diffuser les signaux de cette sous-session ?",
            reply_markup=get_session_broadcast_choice_keyboard()
        )
    else:
        await prompt_signal_start(chat_id, context, prefix=prefix)


async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Vérifie si l'utilisateur est abonné au canal configuré. Ne bloque jamais en cas d'erreur."""
    if CHANNEL_CHAT_ID is None:
        return True  # Aucun canal configuré : on ne rappelle rien
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_CHAT_ID, user_id=user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        logger.warning(f"Impossible de vérifier l'abonnement au canal pour {user_id} : {e}")
        return True  # En cas d'erreur (bot pas admin du canal, etc.), on ne pénalise pas le visiteur


async def send_channel_reminder(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Invite le visiteur à rejoindre le canal, avec un bouton cliquable."""
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
    """Envoie les deux messages d'accueil à un visiteur (non-administrateur) qui démarre le bot."""
    safe_name = html.escape(first_name) if first_name else ""
    name_part = f" {safe_name}" if safe_name else ""

    text1 = (
        f"Hey👋{name_part}, <b>bienvenue</b> 😃\n"
        "Je m'appelle <b>Prince</b> ! Je suis ravi de vous accueillir ici !\n\n"
        "Je suis <b>trader professionnel des options binaires</b> avec plus de "
        "<b>10 ans d'expérience</b> ! Je partage mes stratégies de trading "
        "<b>gratuitement</b> dans mon <b>groupe VIP</b> et je peux vous aider à gagner "
        "vos premiers <b>1000$</b> dans le trading des options binaires !"
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


def get_tier_choice_keyboard() -> InlineKeyboardMarkup:
    """Choix ponctuel du statut Telegram (Premium ou Standard), pour activer les emojis animés."""
    keyboard = [
        [
            InlineKeyboardButton("💎 TELEGRAM PREMIUM", callback_data="settier_premium"),
            InlineKeyboardButton("⭐ TELEGRAM STANDARD", callback_data="settier_standard"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start : accueil personnalisé pour les visiteurs, menu principal pour l'administrateur."""
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

    # Premier lancement pour cet admin : demande une seule fois PREMIUM ou STANDARD
    if context.user_data.get('telegram_tier') is None:
        logger.info(f"Système détecté pour l'admin {chat_id} : {SYSTEM_OS}")
        await send_transient(
            context, chat_id,
            text="Utilises-tu Telegram PREMIUM ou STANDARD sur ce compte ?",
            reply_markup=get_tier_choice_keyboard(),
        )
        return

    await show_main_menu(chat_id, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /help : liste les fonctionnalités disponibles."""
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
    """Commande /stats : statistiques détaillées, session gratuite + VIP (par sous-session et total)."""
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
    """Job planifié : réinitialise l'historique gratuit et VIP de tous les utilisateurs connus."""
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
    """
    Affiche une petite animation "⏳ Analyse de signal..." pendant ~5 secondes
    (un point qui s'ajoute à chaque seconde), puis supprime le message avant
    que le vrai signal ne soit envoyé.
    """
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
            pass  # une édition ratée (rate-limit, etc.) n'interrompt pas l'animation

    await asyncio.sleep(1)
    await delete_message_safe(context.bot, chat_id, msg.message_id)


async def send_signal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie un signal avec une image ALEATOIRE du dossier IMG/."""
    chat_id = update.effective_chat.id
    await show_analysis_animation(context, chat_id)

    signal = generate_signal_data()
    message_text = format_signal_text(signal)

    # Sauvegarde du signal complet (utile pour le bilan) et de la clé de l'actif
    asset_key = get_asset_filename(signal['actif'])
    context.user_data['last_asset'] = asset_key
    context.user_data['last_signal'] = signal

    image_path = get_random_jpeg(DIR_IMG)

    sent_message = await send_photo_safe(
        bot=context.bot,
        chat_id=chat_id,
        image_path=image_path,
        caption=message_text,
        reply_markup=get_result_keyboard(),
        parse_mode="HTML"
    )

    # Mémorise le message envoyé pour pouvoir le supprimer si NEW est cliqué
    if sent_message:
        context.user_data['last_signal_message'] = (chat_id, sent_message.message_id)

        # Nouveau "trade" en cours : réinitialise le suivi pour ANNULER DERNIER
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
    """Envoie le bilan de la session gratuite en cours."""
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
    await auto_broadcast_last(context)


async def send_vip_bilan_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie le bilan de la sous-session VIP en cours."""
    chat_id = update.effective_chat.id
    session_key = context.user_data.get('vip_current')

    if session_key not in VIP_SESSION_LABELS:
        # Sécurité : si jamais on arrive ici sans sous-session active
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
    await auto_broadcast_last(context)


async def send_vip_rapport_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie le rapport complet VIP (les 4 sous-sessions regroupées)."""
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
    await auto_broadcast_last(context)


def record_result(context: ContextTypes.DEFAULT_TYPE, result_key: str):
    """Enregistre le résultat du signal courant dans l'historique approprié
    (session gratuite ou sous-session VIP active)."""
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

    # Évite un double enregistrement si l'utilisateur clique deux fois
    context.user_data['last_signal'] = None


async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le clic sur les boutons."""
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = update.effective_chat.id
    remember_known_user(context, chat_id)

    if not is_authorized(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="⛔ Accès non autorisé.")
        return

    # Nettoyage : le précédent message "de passage" (menu/confirmation) disparaît dès qu'on
    # passe à l'action suivante. Seuls signaux, résultats et bilans restent dans le chat.
    await clear_last_transient(context)

    # --- Navigation générale ---

    if data == "settier_premium" or data == "settier_standard":
        claimed_premium = data == "settier_premium"
        actually_premium = bool(getattr(query.from_user, 'is_premium', False))

        if claimed_premium and not actually_premium:
            context.user_data['telegram_tier'] = 'standard'
            await send_transient(
                context, chat_id,
                text=(
                    "Telegram indique que ce compte n'a pas Premium actif : "
                    "basculé sur STANDARD."
                ),
            )
        else:
            tier = 'premium' if claimed_premium else 'standard'
            context.user_data['telegram_tier'] = tier
            label = "💎 PREMIUM" if tier == 'premium' else "⭐ STANDARD"
            await send_transient(context, chat_id, text=f"✅ Mode {label} activé.")

        await show_main_menu(chat_id, context)
        return

    if data == "btn_main_menu":
        await show_main_menu(chat_id, context)
        return

    if data == "btn_free_menu":
        await start_free_session(chat_id, context)
        return

    if data == "btn_vip_menu":
        await send_transient(
            context, chat_id,
            text="👑 Session VIP — choisissez une sous-session :",
            reply_markup=get_vip_menu_keyboard()
        )
        return

    if data == "btn_vip_rapport":
        await send_vip_rapport_action(update, context)
        return

    # --- Choix de la cible de diffusion automatique pour la session (signal + résultat) ---

    if data == "setbcast_none" or data == "setbcast_all" or data.startswith("setbcast_target_"):
        if data == "setbcast_none":
            target_value = None
        elif data == "setbcast_all":
            target_value = "all"
        else:
            idx = int(data.replace("setbcast_target_", ""))
            if idx < 0 or idx >= len(BROADCAST_TARGETS):
                await send_transient(context, chat_id, text="Cible invalide.")
                return
            target_value = BROADCAST_TARGETS[idx]

        context.user_data['active_broadcast_target'] = target_value
        await prompt_signal_start(chat_id, context)
        return

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

    # --- Session gratuite : NEW SESSION (équivaut à redémarrer la session gratuite) ---

    if data == "btn_new_session":
        await start_free_session(chat_id, context)
        return

    # --- Commun gratuit / VIP : SIGNAL, BILAN, NEW, résultats ---

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

        # Le signal remplacé n'est pas conservé dans l'historique
        context.user_data['last_signal'] = None
        await send_signal_action(update, context)
        return

    if data == "btn_diffusion_menu":
        await send_transient(
            context, chat_id,
            text="📢 Choisis la cible de ta publication :",
            reply_markup=get_diffusion_target_keyboard(),
        )
        return

    if data == "diffchoice_cancel":
        context.user_data['diffusion_draft'] = None
        await send_transient(context, chat_id, text="Diffusion annulée.")
        await show_main_menu(chat_id, context)
        return

    if data == "diffchoice_all" or data == "diffchoice_subscribers" or data.startswith("diffchoice_target_"):
        if data == "diffchoice_all":
            target_value = "all"
        elif data == "diffchoice_subscribers":
            target_value = "subscribers"
        else:
            idx = int(data.replace("diffchoice_target_", ""))
            if idx < 0 or idx >= len(BROADCAST_TARGETS):
                await send_transient(context, chat_id, text="Cible invalide.")
                return
            target_value = BROADCAST_TARGETS[idx]

        context.user_data['diffusion_draft'] = {
            'target': target_value,
            'step': 'content',
            'text': None,
            'photo_file_id': None,
            'buttons': [],
        }
        await send_transient(
            context, chat_id,
            text="✍️ Envoie le texte et/ou la photo de ta publication :",
        )
        return

    if data == "diffbtn_add":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        draft['step'] = 'button_label'
        await send_transient(context, chat_id, text="✏️ Texte du bouton :")
        return

    if data == "diffbtn_finish":
        draft = context.user_data.get('diffusion_draft')
        if not draft or (not draft.get('text') and not draft.get('photo_file_id')):
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return

        if draft['target'] == 'all':
            destinations = [normalize_broadcast_target(t) for t in BROADCAST_TARGETS]
        elif draft['target'] == 'subscribers':
            known = context.bot_data.get('known_users', set())
            destinations = [uid for uid in known if uid != ADMIN_CHAT_ID]
        else:
            destinations = [normalize_broadcast_target(draft['target'])]

        sent, failed = 0, 0
        for dest in destinations:
            try:
                await send_diffusion_post(context, dest, draft)
                sent += 1
            except Exception as e:
                logger.warning(f"Échec de diffusion (post libre) vers {dest} : {e}")
                failed += 1

        context.user_data['diffusion_draft'] = None
        await send_transient(
            context, chat_id,
            text=f"📢 Diffusion terminée : {sent} envoyé(s), {failed} échec(s).",
        )
        await show_main_menu(chat_id, context)
        return

    if data == "btn_capture":
        context.user_data['awaiting_capture'] = True
        await send_transient(
            context, chat_id,
            text="📸 Envoie ta capture d'écran :",
        )
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
            reply_markup = get_vip_result_keyboard() if mode == 'vip' else get_signal_keyboard()
            await send_transient(
                context, chat_id,
                text="Aucun résultat à annuler pour le moment.",
                reply_markup=reply_markup,
            )
            return

        removed = entries.pop()

        # Supprime le signal, le résultat et toutes leurs copies diffusées (canaux/groupes)
        trade = context.user_data.get('current_trade') or {}

        if trade.get('signal_message'):
            s_chat, s_id = trade['signal_message']
            await delete_message_safe(context.bot, s_chat, s_id)

        if trade.get('result_message'):
            r_chat, r_id = trade['result_message']
            await delete_message_safe(context.bot, r_chat, r_id)

        for b_chat, b_id in trade.get('broadcasts', []):
            await delete_message_safe(context.bot, b_chat, b_id)

        # Trade entièrement annulé : plus rien à annuler tant qu'un nouveau signal n'est pas généré
        context.user_data['current_trade'] = None
        context.user_data['last_broadcast'] = None

        text = f"↩️ Dernier résultat annulé :\n{removed['actif']} • {removed['direction']}"
        reply_markup = get_vip_result_keyboard(include_undo=False) if mode == 'vip' else get_signal_keyboard(include_undo=False)

        await send_transient(context, chat_id, text=text, reply_markup=reply_markup)
        return

    # --- Diffusion vers canaux/groupes ---

    if data == "btn_broadcast_menu":
        content = context.user_data.get('last_broadcast')
        if not content:
            await send_transient(context, chat_id, text="Rien à diffuser pour l'instant.")
            return

        target_setting = context.user_data.get('active_broadcast_target')
        if not target_setting:
            await send_transient(
                context, chat_id,
                text=(
                    "Aucune cible de diffusion n'est définie pour cette session.\n"
                    "Choisis-en une au démarrage de la prochaine session (gratuite ou VIP)."
                ),
            )
            return

        targets = BROADCAST_TARGETS if target_setting == 'all' else [target_setting]

        results = []
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
                results.append(f"✅ {target}")

                # Mémorise cette copie pour pouvoir la supprimer via ANNULER DERNIER
                trade = context.user_data.get('current_trade')
                if trade is not None:
                    trade.setdefault('broadcasts', []).append((sent.chat_id, sent.message_id))
            except Exception as e:
                logger.warning(f"Échec de diffusion vers {target} : {e}")
                results.append(f"❌ {target} — {e}")

        await send_transient(
            context, chat_id,
            text="📤 Résultat de la diffusion :\n" + "\n".join(results),
        )
        return

    # Boutons de Victoires
    _win_emoji = emojify(context, "✅")
    win_map = {
        "res_mg0": (f"{_win_emoji}<b>GAIN DIRECT</b>{_win_emoji}", "mg0"),
        "res_mg1": (f"{_win_emoji}<b>GAIN MARTINGALE 1</b>{_win_emoji}", "mg1"),
        "res_mg2": (f"{_win_emoji}<b>GAIN MARTINGALE 2</b>{_win_emoji}", "mg2"),
        "res_mg3": (f"{_win_emoji}<b>GAIN MARTINGALE 3</b>{_win_emoji}", "mg3"),
    }

    if data in win_map:
        caption_text, result_key = win_map[data]
        mode = context.user_data.get('mode', 'free')
        record_result(context, result_key)

        last_asset = context.user_data.get('last_asset', None)

        # Cherche l'image de la paire dans IMG_WIN/ (insensible aux majuscules)
        image_path = get_specific_jpeg_only(DIR_WIN, last_asset)

        reply_markup = get_vip_result_keyboard() if mode == 'vip' else get_signal_keyboard()

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

            await auto_broadcast_last(context)
        return

    # Bouton Défaite (❌)
    if data == "res_lose":
        mode = context.user_data.get('mode', 'free')
        record_result(context, "lose")

        _lose_emoji = emojify(context, "❌")
        caption_text = f"{_lose_emoji}<b>PERDU</b>{_lose_emoji}"
        image_path = get_random_jpeg(DIR_LOSE)

        reply_markup = get_vip_result_keyboard() if mode == 'vip' else get_signal_keyboard()

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

            await auto_broadcast_last(context)


async def send_to_visitor(context: ContextTypes.DEFAULT_TYPE, visitor_key: str, text: str):
    """
    Envoie un message à un visiteur à partir de sa clé (voir make_visitor_key). Ajoute
    automatiquement le direct_messages_topic_id si ce visiteur a écrit via les "Messages
    directs" d'un canal (Telegram exige ce paramètre pour pouvoir lui répondre).
    """
    chat_id, dm_topic_id = parse_visitor_key(visitor_key)
    kwargs = {'chat_id': chat_id, 'text': text}
    if dm_topic_id is not None:
        kwargs['direct_messages_topic_id'] = dm_topic_id
    await context.bot.send_message(**kwargs)


async def handle_capture_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reçoit une photo envoyée par l'admin, dans l'un de ces deux contextes :
    - CAPTURE (après un résultat) : diffuse vers la cible active de la session.
    - DIFFUSION (composeur de post libre) : l'utilise comme image de la publication.
    """
    message = update.message
    if message is None or not message.photo:
        return

    chat_id = update.effective_chat.id

    if not is_admin(chat_id):
        return

    draft = context.user_data.get('diffusion_draft')
    if draft and draft.get('step') == 'content':
        draft['photo_file_id'] = message.photo[-1].file_id
        if message.caption:
            draft['text'] = message.caption
        draft['step'] = 'button_choice'
        await send_transient(
            context, chat_id,
            text="Ajouter un bouton-lien à ce post ?",
            reply_markup=get_diffusion_add_button_keyboard(len(draft['buttons'])),
        )
        return

    if not context.user_data.get('awaiting_capture'):
        return  # Pas une capture attendue : on n'interfère pas

    context.user_data['awaiting_capture'] = False
    file_id = message.photo[-1].file_id
    await diffuse_capture_photo(context, file_id)

    mode = context.user_data.get('mode', 'free')
    if mode == 'vip':
        await send_transient(
            context, chat_id,
            text="✅ Capture diffusée.\n\n👇 Cliquez pour obtenir votre signal :",
            reply_markup=get_vip_signal_start_keyboard(),
        )
    else:
        await send_transient(
            context, chat_id,
            text="✅ Capture diffusée.\n\n👇 Cliquez ci-dessous pour obtenir votre signal :",
            reply_markup=get_free_start_keyboard(),
        )


async def relay_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Relais des messages texte libres, avec deux mécanismes possibles :

    1) Groupe de support avec Topics (si SUPPORT_GROUP_ID configuré) : chaque visiteur a son
       propre fil de discussion. Tout message envoyé dans ce fil (par un admin/membre du
       groupe) est renvoyé au visiteur correspondant. C'est le mode prioritaire.

    2) Mode direct (si seul ADMIN_CHAT_ID est configuré) : les messages des visiteurs sont
       transférés au chat privé de l'admin, qui répond via "Répondre" (reply) sur le message.

    Dans tous les cas, chaque échange est aussi journalisé pour la commande /historique.
    """
    message = update.message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id

    # --- Cas 0 : l'admin est en train de composer un post DIFFUSION ---
    if is_admin(chat_id):
        draft = context.user_data.get('diffusion_draft')
        if draft:
            step = draft.get('step')

            if step == 'content':
                draft['text'] = message.text
                draft['step'] = 'button_choice'
                await send_transient(
                    context, chat_id,
                    text="Ajouter un bouton-lien à ce post ?",
                    reply_markup=get_diffusion_add_button_keyboard(len(draft['buttons'])),
                )
                return

            if step == 'button_label':
                draft['pending_label'] = message.text.strip()
                draft['step'] = 'button_url'
                await send_transient(
                    context, chat_id,
                    text="🔗 Lien du bouton (doit commencer par http:// ou https://) :",
                )
                return

            if step == 'button_url':
                url = message.text.strip()
                if not (url.startswith("http://") or url.startswith("https://")):
                    await send_transient(
                        context, chat_id,
                        text="⚠️ Lien invalide, il doit commencer par http:// ou https://. Réessaie :",
                    )
                    return
                label = draft.pop('pending_label', 'Lien')
                draft['buttons'].append((label, url))
                draft['step'] = 'button_choice'
                await send_transient(
                    context, chat_id,
                    text=f"✅ Bouton ajouté ({len(draft['buttons'])}/3).",
                    reply_markup=get_diffusion_add_button_keyboard(len(draft['buttons'])),
                )
                return

    # --- Cas 1 : message envoyé DANS le groupe de support (dans un topic donné) ---
    if SUPPORT_GROUP_ID is not None and chat_id == SUPPORT_GROUP_ID:
        thread_id = message.message_thread_id
        if thread_id is None:
            return  # message hors sujet (fil général du groupe) : on ignore

        # Ignore les messages qui sont eux-mêmes des transferts (l'écho du message du visiteur)
        is_forward = bool(getattr(message, 'forward_origin', None) or getattr(message, 'forward_date', None))
        if is_forward:
            return

        reverse = context.bot_data.get('topic_to_visitor', {})
        target_key = reverse.get(thread_id)
        if not target_key:
            return

        try:
            await send_to_visitor(context, target_key, message.text)
            log_conversation(context, target_key, 'admin', message.text)
        except Exception as e:
            await message.reply_text(f"❌ Échec de l'envoi : {e}")
        return

    # --- Cas 2 : l'admin écrit en privé au bot (mode direct, sans groupe) ---
    if is_admin(chat_id):
        reply_to = message.reply_to_message
        if reply_to:
            relay_map = context.bot_data.get('relay_map', {})
            target_key = relay_map.get(reply_to.message_id)
            if target_key:
                try:
                    await send_to_visitor(context, target_key, message.text)
                    log_conversation(context, target_key, 'admin', message.text)
                    await message.reply_text("✅ Réponse envoyée.")
                except Exception as e:
                    await message.reply_text(f"❌ Échec de l'envoi : {e}")
                return
        # Message de l'admin qui n'est pas une réponse à un visiteur : on l'ignore simplement.
        return

    # --- Cas 3 : message venant d'un visiteur ---
    sender = update.effective_user
    sender_name = sender.full_name if sender else "Inconnu"
    username = f"@{sender.username}" if sender and sender.username else "(pas de pseudo)"

    # Si le visiteur a écrit "en tant que canal" (Messages directs), Telegram fournit un
    # direct_messages_topic : on construit alors une clé unique par abonné (voir make_visitor_key),
    # car plusieurs abonnés différents peuvent partager le même chat_id dans ce mode.
    dm_topic = getattr(message, 'direct_messages_topic', None)
    dm_topic_id = dm_topic.topic_id if dm_topic is not None else None
    visitor_key = make_visitor_key(chat_id, dm_topic_id)

    if ADMIN_CHAT_ID is None and SUPPORT_GROUP_ID is None:
        return  # Aucune des deux fonctionnalités n'est configurée

    remember_known_user(context, visitor_key)
    log_conversation(context, visitor_key, 'visitor', message.text)

    # Priorité au groupe avec Topics s'il est configuré et fonctionnel
    if SUPPORT_GROUP_ID is not None:
        thread_id = await get_or_create_topic(context, visitor_key, sender_name, username)
        if thread_id is not None:
            try:
                await context.bot.forward_message(
                    chat_id=SUPPORT_GROUP_ID,
                    from_chat_id=chat_id,
                    message_id=message.message_id,
                    message_thread_id=thread_id,
                )
            except Exception as e:
                logger.warning(f"Échec de relais (topic) du message de {visitor_key} : {e}")
            return
        # Si la création/récupération du topic échoue, on retombe sur le mode direct ci-dessous

    # Repli : mode direct vers le chat privé de l'admin
    if ADMIN_CHAT_ID is not None:
        try:
            forwarded = await context.bot.forward_message(
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=chat_id,
                message_id=message.message_id,
            )
            note = await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"☝️ Message de {sender_name} {username} (clé: {visitor_key})\nRéponds à ce message pour lui répondre.",
                reply_to_message_id=forwarded.message_id,
            )
            relay_map = context.bot_data.setdefault('relay_map', {})
            relay_map[forwarded.message_id] = visitor_key
            relay_map[note.message_id] = visitor_key
        except Exception as e:
            logger.warning(f"Échec de relais du message de {visitor_key} : {e}")
            return


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /broadcast <message> : envoie un message à tous les utilisateurs connus (admin uniquement)."""
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
    """Commande /historique <chat_id> : affiche tout l'échange enregistré avec ce visiteur."""
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

    # Découpage si le texte dépasse la limite d'un message Telegram
    max_len = 3500
    for i in range(0, len(full_text), max_len):
        await update.message.reply_text(full_text[i:i + max_len])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log des erreurs rencontrées."""
    logger.error("Exception rencontrée lors du traitement d'une mise à jour :", exc_info=context.error)


def main():
    if not TOKEN:
        raise ValueError("Le TELEGRAM_TOKEN n'a pas été trouvé. Vérifiez votre fichier .env")

    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)

    # Requêtes API avec timeouts généreux (utile sur connexion mobile lente/instable)
    api_request = HTTPXRequest(
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        write_timeout=CONNECT_TIMEOUT,
        pool_timeout=CONNECT_TIMEOUT,
    )
    # Le long polling (get_updates) garde la connexion ouverte plus longtemps : lecture élargie
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

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("historique", history_command))
    app.add_handler(CallbackQueryHandler(handle_button_click))
    app.add_handler(MessageHandler(filters.PHOTO, handle_capture_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_incoming_message))

    # Gestionnaire d'erreurs
    app.add_error_handler(error_handler)

    # Reset quotidien automatique (optionnel, désactivé par défaut)
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
