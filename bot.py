import os
import re
import html
import sys
import io
import shutil
import asyncio
import platform
import random
import logging
from logging.handlers import RotatingFileHandler
from collections import defaultdict
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, LinkPreviewOptions
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

# Configuration du Logging : console + fichier persistant (rotatif, 5 Mo x 3 fichiers)
_log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
_file_handler = RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
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



def styled_button(text: str, style: str = None, **kwargs) -> InlineKeyboardButton:
    """
    Crée un InlineKeyboardButton coloré (style='danger' rouge, 'success' vert, 'primary' bleu).
    Fonctionnalité Telegram récente (9 février 2026) : sur un client Telegram plus ancien, le
    bouton s'affiche simplement sans couleur. Si la librairie python-telegram-bot installée est
    trop ancienne pour connaître ce paramètre, on retombe automatiquement sur un bouton classique.
    """
    try:
        return InlineKeyboardButton(text, style=style, **kwargs)
    except TypeError:
        return InlineKeyboardButton(text, **kwargs)


def normalize_broadcast_target(target: str):
    """Convertit un identifiant de cible en int (id numérique) ou str (@username)."""
    t = target.strip()
    if t.lstrip("-").isdigit():
        return int(t)
    return t


async def upload_to_catbox(file_bytes: bytes, filename: str = "image.jpg") -> str:
    """Upload une image sur catbox.moe (hébergeur public, gratuit, sans clé) et retourne son URL directe."""
    async with httpx.AsyncClient(timeout=30) as client:
        files = {"fileToUpload": (filename, file_bytes, "image/jpeg")}
        data = {"reqtype": "fileupload"}
        resp = await client.post(CATBOX_UPLOAD_URL, data=data, files=files)
        resp.raise_for_status()
        url = resp.text.strip()
        if not url.startswith("http"):
            raise ValueError(f"Réponse inattendue de catbox.moe : {url}")
        return url

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

# --- Canal/groupe VIP (optionnel, distinct du canal ci-dessus) ---
# Utilisé pour le bouton "ABONNÉS NON VIP" : identifie qui n'est PAS encore dans le VIP.
# @username (public) ou id numérique négatif (privé). Le bot doit y être administrateur.
_vip_channel_env = os.getenv("VIP_CHANNEL_CHAT_ID", "").strip()
if _vip_channel_env.lstrip("-").isdigit():
    VIP_CHANNEL_CHAT_ID = int(_vip_channel_env)
elif _vip_channel_env:
    VIP_CHANNEL_CHAT_ID = _vip_channel_env if _vip_channel_env.startswith("@") else f"@{_vip_channel_env}"
else:
    VIP_CHANNEL_CHAT_ID = None

# --- Hébergement d'images pour la diffusion (catbox.moe, aucune clé requise) ---
# Utilisé par le bouton "➕ Ajouter photo" : l'image est uploadée sur catbox.moe (hébergeur
# public gratuit) puis son URL directe est cachée dans un lien invisible du texte, pour générer
# un aperçu agrandi sous le message via LinkPreviewOptions.
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"

# --- Anti-spam visiteurs ---
RATE_LIMIT_MAX_MESSAGES = int(os.getenv("RATE_LIMIT_MAX_MESSAGES", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# --- Sauvegarde automatique de la persistance ---
BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "true").lower() == "true"
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "3"))

# --- Rappel automatique de canal (pour les visiteurs non abonnés) ---
CHANNEL_REMINDER_DAYS = int(os.getenv("CHANNEL_REMINDER_DAYS", "3"))

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


_BUTTON_STYLE_WORDS = {
    "green": "success", "vert": "success",
    "blue": "primary", "bleu": "primary",
    "red": "danger", "rouge": "danger",
}


def parse_diffusion_buttons(text: str):
    """
    Parse le format de boutons façon Controller Bot :
        Texte bouton 1 - http://exemple.com | Texte bouton 2 - http://exemple2.com - style:red
        Texte bouton 3 - http://exemple3.com
    Chaque ligne = une rangée de boutons ; "|" sépare les boutons d'une même rangée.
    Retourne une liste de rangées, chaque rangée étant une liste de tuples (label, url, style).
    Les entrées mal formées sont simplement ignorées.
    """
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for part in line.split("|"):
            part = part.strip()
            if not part:
                continue

            style = None
            style_match = re.search(r'-\s*style\s*:\s*(\w+)\s*$', part, re.IGNORECASE)
            if style_match:
                style = _BUTTON_STYLE_WORDS.get(style_match.group(1).lower())
                part = part[:style_match.start()].rstrip()

            if " - " not in part:
                continue
            label, url = part.rsplit(" - ", 1)
            label = label.strip()
            url = url.strip()
            if not label or not (url.startswith("http://") or url.startswith("https://")):
                continue
            row.append((label, url, style))

        if row:
            rows.append(row)

    return rows


RANDOM_BUTTON_STYLES = ["success", "danger", "primary"]


def build_diffusion_markup(button_rows: list):
    """
    Construit un InlineKeyboardMarkup à partir de rangées de boutons (label, url, style).
    Couleur aléatoire par rangée : tous les boutons d'une même rangée partagent la même
    couleur (choisie au hasard), sauf si un style a été explicitement précisé (style:xxx)
    pour un bouton donné, auquel cas ce choix explicite est respecté.
    """
    if not button_rows:
        return None
    keyboard = []
    for row in button_rows:
        row_color = random.choice(RANDOM_BUTTON_STYLES)
        keyboard.append([
            styled_button(label, style=(style or row_color), url=url)
            for label, url, style in row
        ])
    return InlineKeyboardMarkup(keyboard)


async def send_diffusion_post(context: ContextTypes.DEFAULT_TYPE, dest, draft: dict, dm_topic_id=None) -> list:
    """
    Envoie le post composé via DIFFUSION vers une destination, en appliquant les options
    choisies (formatage, diffusion silencieuse, aperçu du lien). Trois cas possibles :
    - Photo uploadée directement (photo_file_id) : photo + légende classique, boutons dessus.
    - Image via canal de stockage (image_url, un lien https://t.me/canal/id) : envoyée en
      send_message, avec un lien invisible (caractère zéro-largeur) pointant vers cette image,
      et LinkPreviewOptions pour afficher l'aperçu en grand format SOUS le texte.
    - Texte seul : send_message classique, aperçu de lien standard.
    `dm_topic_id` est nécessaire pour un visiteur ayant écrit via les "Messages directs"
    d'un canal. Retourne la liste des (chat_id, message_id) effectivement envoyés.

    Note : "Réactions par défaut" est une préférence purement indicative — l'API des bots
    Telegram ne permet pas d'activer/désactiver les réactions sur un message envoyé par un
    bot ; ce réglage est géré au niveau du chat lui-même, pas par message.
    """
    options = draft.get('options') or DEFAULT_DIFFUSION_OPTIONS
    raw_text = draft.get('text')

    if options.get('format') == 'Texte brut' and raw_text:
        text = re.sub(r'<[^>]+>', '', raw_text)
        parse_mode = None
    else:
        text = raw_text
        parse_mode = "HTML"

    markup = build_diffusion_markup(draft.get('buttons'))
    photo_file_id = draft.get('photo_file_id')
    image_url = draft.get('image_url')
    sent_refs = []
    extra = {'disable_notification': options.get('silent', False)}
    if dm_topic_id is not None:
        extra['direct_messages_topic_id'] = dm_topic_id

    if photo_file_id:
        # Photo uploadée directement : légende et boutons dans le même message
        sent_photo = await context.bot.send_photo(
            chat_id=dest, photo=photo_file_id, caption=text, parse_mode=parse_mode,
            reply_markup=markup, **extra,
        )
        sent_refs.append((sent_photo.chat_id, sent_photo.message_id))
    else:
        final_text = text or ''
        if image_url and parse_mode == "HTML":
            # Lien invisible (caractère zéro-largeur) vers l'image archivée dans le canal
            # de stockage : Telegram génère l'aperçu à partir de cette URL, affiché en
            # grand format sous le texte grâce à LinkPreviewOptions.
            final_text += f'<a href="{image_url}">\u200b</a>'
            preview_options = LinkPreviewOptions(
                is_disabled=False, url=image_url, prefer_large_media=True, show_above_text=False,
            )
        else:
            disable_preview = not options.get('link_preview', True)
            preview_options = LinkPreviewOptions(
                is_disabled=disable_preview, prefer_large_media=True, show_above_text=False,
            )

        sent = await context.bot.send_message(
            chat_id=dest, text=final_text, parse_mode=parse_mode,
            reply_markup=markup, link_preview_options=preview_options, **extra,
        )
        sent_refs.append((sent.chat_id, sent.message_id))

    return sent_refs


async def show_diffusion_preview(context: ContextTypes.DEFAULT_TYPE, chat_id, draft: dict):
    """Envoie un aperçu exact du post (dans le chat de l'admin) avant confirmation de diffusion."""
    try:
        await send_diffusion_post(context, chat_id, draft)
    except Exception as e:
        logger.warning(f"Échec de l'aperçu de diffusion : {e}")

    await send_transient(
        context, chat_id,
        text="👆 Voici un aperçu de ton post. Diffuser ?",
        reply_markup=get_diffusion_preview_keyboard(),
    )


async def diffuse_capture_photo(context: ContextTypes.DEFAULT_TYPE, photo_file_id: str):
    """Diffuse une capture d'écran (envoyée via le bouton CAPTURE) vers la cible active de la session,
    avec un bouton "PARTAGEZ VOS RÉSULTATS" pointant vers le bot."""
    target_setting = context.user_data.get('active_broadcast_target')
    if not target_setting:
        return

    share_keyboard = InlineKeyboardMarkup(
        [[styled_button("PARTAGEZ VOS RÉSULTATS 〽️", style=random.choice(RANDOM_BUTTON_STYLES), url="https://t.me/VIPLegit_bot")]]
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
    """Génère les données temporelles d'un signal. L'heure d'entrée tombe toujours sur une minute paire."""
    actif = random.choice(ACTIFS)
    direction = random.choice(["ACHAT", "VENTE"])
    now = datetime.now(TZ)

    entre = (now + timedelta(minutes=3)).replace(second=0, microsecond=0)
    if entre.minute % 2 != 0:
        entre += timedelta(minutes=1)
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


def generate_performance_chart(entries: list):
    """Génère un graphique PNG (BytesIO) du taux de réussite par jour."""
    daily = {}
    for e in entries:
        if not e.get('entre'):
            continue
        day = e['entre'].date()
        stats = daily.setdefault(day, {'gains': 0, 'total': 0})
        stats['total'] += 1
        if e['result'] != 'lose':
            stats['gains'] += 1

    days = sorted(daily.keys())
    rates = [daily[d]['gains'] / daily[d]['total'] * 100 for d in days]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot([d.strftime('%d/%m') for d in days], rates, marker='o', color='#2ecc71', linewidth=2)
    ax.set_ylabel('Taux de réussite (%)')
    ax.set_title('Performance par jour')
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    plt.close(fig)
    buf.seek(0)
    buf.name = "performance.png"
    return buf


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

ADMIN_QUICK_KEYBOARD = ReplyKeyboardMarkup(
    [["🏠 Menu", "📊 Stats"], ["📢 Diffusion", "ℹ️ Aide"], ["📊 Sondage", "🗑️ RESET STATS"]],
    resize_keyboard=True,
)

ADMIN_QUICK_ACTIONS = {
    "🏠 Menu": "menu",
    "📊 Stats": "stats",
    "📢 Diffusion": "diffusion",
    "ℹ️ Aide": "help",
    "🗑️ RESET STATS": "reset_stats",
    "📊 Sondage": "poll",
}


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu principal : choix entre session gratuite, session VIP et diffusion libre."""
    keyboard = [
        [
            InlineKeyboardButton("🆓 SESSION GRATUITE", callback_data="btn_free_menu"),
            InlineKeyboardButton("👑 SESSION VIP", callback_data="btn_vip_menu"),
        ],
        [styled_button("📢 DIFFUSION", style="primary", callback_data="btn_diffusion_menu")],
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
            styled_button("📸 CAPTURE", style="primary", callback_data="btn_capture"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([styled_button("↩️ ANNULER DERNIER", style="danger", callback_data="btn_undo")])
    return InlineKeyboardMarkup(keyboard)


def get_result_keyboard() -> InlineKeyboardMarkup:
    """Boutons de résultat sous le signal, + bouton NEW et DIFFUSER (manuel). Utilisé en gratuit et en VIP."""
    keyboard = [
        [
            styled_button("MG0", style="success", callback_data="res_mg0"),
            styled_button("MG1", style="success", callback_data="res_mg1"),
            styled_button("MG2", style="success", callback_data="res_mg2"),
            styled_button("MG3", style="success", callback_data="res_mg3"),
            styled_button("❌", style="danger", callback_data="res_lose"),
        ],
        [
            InlineKeyboardButton("🔄 NEW", callback_data="btn_new"),
        ],
        [styled_button("📤 DIFFUSER", style="primary", callback_data="btn_broadcast_menu")],
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
            styled_button("📸 CAPTURE", style="primary", callback_data="btn_capture"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([styled_button("↩️ ANNULER DERNIER", style="danger", callback_data="btn_undo")])
    keyboard.append([InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")])
    return InlineKeyboardMarkup(keyboard)


def get_vip_bilan_keyboard(session_key: str) -> InlineKeyboardMarkup:
    """Boutons affichés sous le bilan d'une sous-session VIP."""
    keyboard = [
        [InlineKeyboardButton("🔄 RECOMMENCER CETTE SESSION", callback_data=f"vip_reset_{session_key}")],
        [InlineKeyboardButton("👥 ABONNÉS NON VIP", callback_data=f"vipnonvip_{session_key}")],
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
    keyboard.append([styled_button("❌ Annuler", style="danger", callback_data="diffchoice_cancel")])
    return InlineKeyboardMarkup(keyboard)


def get_poll_target_keyboard() -> InlineKeyboardMarkup:
    """Choix de la cible pour l'envoi d'un sondage."""
    keyboard = [
        [InlineKeyboardButton(f"📡 {target}", callback_data=f"polltarget_idx_{i}")]
        for i, target in enumerate(BROADCAST_TARGETS)
    ]
    if BROADCAST_TARGETS:
        keyboard.append([InlineKeyboardButton("📤 TOUS", callback_data="polltarget_all")])
    keyboard.append([InlineKeyboardButton("👥 ABONNÉS", callback_data="polltarget_subscribers")])
    keyboard.append([styled_button("❌ Annuler", style="danger", callback_data="polltarget_cancel")])
    return InlineKeyboardMarkup(keyboard)


def parse_poll_spec(text: str):
    """Parse 'Question | Option1 | Option2 | ...' -> (question, [options]) ou (None, None)."""
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if len(parts) < 3:
        return None, None
    question = parts[0]
    options = parts[1:][:10]
    return question, options


def get_stats_diffusion_keyboard() -> InlineKeyboardMarkup:
    """Boutons de cible pour diffuser directement le rapport de statistiques, + graphique."""
    keyboard = [
        [InlineKeyboardButton(f"📡 {target}", callback_data=f"statdiff_target_{i}")]
        for i, target in enumerate(BROADCAST_TARGETS)
    ]
    if BROADCAST_TARGETS:
        keyboard.append([InlineKeyboardButton("📤 TOUS", callback_data="statdiff_all")])
    keyboard.append([InlineKeyboardButton("👥 ABONNÉS", callback_data="statdiff_subscribers")])
    keyboard.append([InlineKeyboardButton("📈 Graphique de performance", callback_data="stats_chart")])
    return InlineKeyboardMarkup(keyboard)


def filter_entries_by_period(entries: list, days: int) -> list:
    """Filtre les entrées des `days` derniers jours (basé sur l'heure d'entrée du trade)."""
    cutoff = datetime.now(TZ) - timedelta(days=days)
    return [e for e in entries if e.get('entre') and e['entre'] >= cutoff]


def get_diffusion_buttons_prompt_keyboard() -> InlineKeyboardMarkup:
    """Affiché sous l'invite de saisie des boutons-liens."""
    keyboard = [
        [InlineKeyboardButton("🚫 Aucun bouton", callback_data="diffbtn_none")],
        [InlineKeyboardButton("↩️ Annuler", callback_data="diffbtn_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_diffusion_extras_keyboard() -> InlineKeyboardMarkup:
    """Affiché après le texte : proposer une photo (via canal de stockage), des boutons, ou terminer."""
    keyboard = [
        [InlineKeyboardButton("➕ Ajouter photo", callback_data="diffextra_photo")],
        [InlineKeyboardButton("🔗 Ajouter des boutons-liens", callback_data="diffextra_buttons")],
        [styled_button("✅ Terminer et voir l'aperçu", style="success", callback_data="diffextra_done")],
        [styled_button("↩️ Annuler", style="danger", callback_data="diffextra_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_diffusion_photo_prompt_keyboard() -> InlineKeyboardMarkup:
    """Affiché en attente de la photo à archiver dans le canal de stockage."""
    keyboard = [
        [InlineKeyboardButton("↩️ Annuler", callback_data="diffextra_cancel_photo")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_diffusion_preview_keyboard() -> InlineKeyboardMarkup:
    """Affiché sous l'aperçu du post, avant confirmation de la diffusion."""
    keyboard = [
        [styled_button("📤 Diffuser maintenant", style="success", callback_data="diffbtn_finish")],
        [InlineKeyboardButton("🕒 Programmer", callback_data="diffbtn_schedule")],
        [InlineKeyboardButton("⚙️ Options", callback_data="diffopt_open")],
        [styled_button("❌ Annuler", style="danger", callback_data="diffchoice_cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


DEFAULT_DIFFUSION_OPTIONS = {
    "format": "HTML",
    "silent": False,
    "link_preview": True,
    "reactions": False,
}


def get_diffusion_options_keyboard(options: dict) -> InlineKeyboardMarkup:
    """Menu d'options façon Controller Bot : formatage, diffusion silencieuse, aperçu, réactions."""
    fmt = options.get("format", "HTML")
    silent = options.get("silent", False)
    link_preview = options.get("link_preview", True)
    reactions = options.get("reactions", False)

    keyboard = [
        [InlineKeyboardButton(f"Style de formatage : {fmt}", callback_data="diffopt_format")],
        [InlineKeyboardButton(
            f"Diffusion silencieuse : {'Activée' if silent else 'Désactivée'}",
            callback_data="diffopt_silent",
        )],
        [InlineKeyboardButton(
            f"Aperçu du lien : {'Activé' if link_preview else 'Désactivé'}",
            callback_data="diffopt_preview",
        )],
        [InlineKeyboardButton(
            f"Réactions par défaut : {'Activées' if reactions else 'Désactivées'}",
            callback_data="diffopt_reactions",
        )],
        [InlineKeyboardButton("« Retour", callback_data="diffopt_back")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_diffusion_result_keyboard() -> InlineKeyboardMarkup:
    """Affiché après une diffusion réussie : permet de supprimer les messages envoyés partout."""
    keyboard = [
        [styled_button("🗑️ Supprimer le message diffusé", style="danger", callback_data="diffdelete_confirm")],
    ]
    return InlineKeyboardMarkup(keyboard)


DIFFUSION_BUTTONS_PROMPT = (
    "Envoyez-moi une liste de boutons pour lien URL pour le message. Veuillez utiliser ce format:\n"
    "Texte bouton 1 - http://www.example.com/ | Texte bouton 2 - http://www.example2.com/\n"
    "Texte bouton 3 - http://www.example3.com/\n"
    "\n"
    "Vous pouvez également spécifier une couleur pour les boutons:\n"
    "\n"
    "Bouton 1 - http://example1.com - style:green\n"
    "Bouton 2 - http://example2.com - style:blue\n"
    "Bouton 3 - http://example3.com - style:red\n"
    "\n"
    "Utilisez le séparateur | pour ajouter jusqu'à trois boutons à la suite. Exemple:\n"
    "\n"
    "Bouton 1 - http://example1.com | Bouton 2 - http://example2.com\n"
    "Bouton 3 - http://example3.com - style:red | Bouton 4 - http://example4.com\n"
    "\n"
    "choisissez 'Annuler' pour revenir à la création du message."
)


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


async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel=None) -> bool:
    """Vérifie si l'utilisateur est abonné au canal donné (par défaut CHANNEL_CHAT_ID).
    Ne bloque jamais en cas d'erreur : traite comme membre si indéterminable."""
    target_channel = channel if channel is not None else CHANNEL_CHAT_ID
    if target_channel is None:
        return True  # Aucun canal configuré : on ne rappelle/filtre rien
    try:
        member = await context.bot.get_chat_member(chat_id=target_channel, user_id=user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        logger.warning(f"Impossible de vérifier l'abonnement à {target_channel} pour {user_id} : {e}")
        return True  # En cas d'erreur (bot pas admin du canal, etc.), on ne pénalise pas le visiteur


LANG_CHOICE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🇫🇷 Français", callback_data="setlang_fr"),
        InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en"),
    ]
])


async def send_channel_reminder(chat_id, context: ContextTypes.DEFAULT_TYPE, lang: str = "fr"):
    """Invite le visiteur à rejoindre le canal, avec un bouton cliquable (bilingue)."""
    if not CHANNEL_INVITE_LINK:
        return
    button_label = "📢 Rejoindre le canal" if lang == "fr" else "📢 Join the channel"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=CHANNEL_INVITE_LINK)]])
    if lang == "fr":
        text = "📢 Pour ne rater aucune stratégie ni aucun signal gratuit, rejoins mon canal officiel !"
    else:
        text = "📢 To never miss a strategy or a free signal, join my official channel!"
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def send_welcome_messages(chat_id, context: ContextTypes.DEFAULT_TYPE, first_name: str = "", lang: str = "fr"):
    """Envoie les deux messages d'accueil à un visiteur (non-administrateur) qui démarre le bot."""
    safe_name = html.escape(first_name) if first_name else ""
    name_part = f" {safe_name}" if safe_name else ""

    if lang == "en":
        text1 = (
            f"Hey👋{name_part}, <b>welcome</b> 😃\n"
            "My name is <b>Prince</b>! I'm delighted to have you here!\n\n"
            "I'm a <b>professional binary options trader</b> with over "
            "<b>10 years of experience</b>! I share my trading strategies "
            "<b>for free</b> in my <b>VIP group</b> and I can help you earn "
            "your first <b>$1000</b> trading binary options!"
        )
        text2 = (
            "Send me your message and I'll reply <b>as soon as possible</b> ⏱️\n\n"
            "<b>📝 REGISTRATION</b>\n"
            f"To join the <b>VIP</b>, you need to sign up on "
            f"<a href=\"{POCKET_OPTION_LINK}\"><b>Pocket Option</b></a>.\n"
            f"Create a new account (<b>30% BONUS</b>), and once registration is complete\n\n"
            f"❗️<b>SEND YOUR ID</b> from your "
            f"<a href=\"{POCKET_OPTION_LINK}\"><b>Pocket Option</b></a> account <b>here</b>\n"
            "______________________"
        )
    else:
        text1 = (
            f"Hey👋{name_part}, <b>bienvenue</b> 😃\n"
            "Je m'appelle <b>Prince</b> ! Je suis ravi de vous accueillir ici !\n\n"
            "Je suis <b>trader professionnel des options binaires</b> avec plus de "
            "<b>10 ans d'expérience</b> ! Je partage mes stratégies de trading "
            "<b>gratuitement</b> dans mon <b>groupe VIP</b> et je peux t'aider à gagner "
            "tes premiers <b>1000$</b> dans le trading des options binaires !"
        )
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

    await context.bot.send_message(chat_id=chat_id, text=text1, parse_mode="HTML")
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


def is_blocked(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """Vérifie si ce chat_id est sur liste noire (/block)."""
    return chat_id in context.bot_data.get('blocked_users', set())


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start : accueil personnalisé pour les visiteurs, menu principal pour l'administrateur."""
    chat_id = update.effective_chat.id

    if is_blocked(context, chat_id):
        return  # Utilisateur bloqué (/block) : on l'ignore silencieusement

    visitor_key = make_visitor_key(chat_id, None)
    known_users_set = context.bot_data.get('known_users', set())
    is_new_visitor = visitor_key not in known_users_set
    remember_known_user(context, visitor_key)
    await clear_last_transient(context)

    if not is_authorized(chat_id):
        sender = update.effective_user

        if is_new_visitor:
            context.bot_data.setdefault('visitor_first_seen', {})[visitor_key] = datetime.now(TZ)
            if ADMIN_CHAT_ID is not None:
                name = sender.full_name if sender else "Inconnu"
                username = f"@{sender.username}" if sender and sender.username else "(pas de pseudo)"
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=f"🆕 Nouveau visiteur : {name} {username} (id: {chat_id})",
                    )
                except Exception as e:
                    logger.warning(f"Échec de notification nouveau visiteur : {e}")

        if context.user_data.get('lang') is None:
            await update.message.reply_text(
                "🌍 Choisissez votre langue / Choose your language :",
                reply_markup=LANG_CHOICE_KEYBOARD,
            )
            return

        lang = context.user_data.get('lang', 'fr')
        first_name = sender.first_name if sender and sender.first_name else ""
        await send_welcome_messages(chat_id, context, first_name, lang=lang)

        user_id = sender.id if sender else chat_id
        if not await is_channel_member(context, user_id):
            await send_channel_reminder(chat_id, context, lang=lang)
        return

    # Réinitialise tout état de composition en cours (diffusion, capture) pour repartir propre
    context.user_data['diffusion_draft'] = None
    context.user_data['awaiting_capture'] = False

    # À chaque /start, redemande PREMIUM ou STANDARD avant d'afficher le menu principal
    logger.info(f"Système détecté pour l'admin {chat_id} : {SYSTEM_OS}")
    await send_transient(
        context, chat_id,
        text="Utilises-tu Telegram PREMIUM ou STANDARD sur ce compte ?",
        reply_markup=get_tier_choice_keyboard(),
    )


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

    all_entries = history + vip_all_entries
    blocks.append(format_stats_block("📅 7 derniers jours", filter_entries_by_period(all_entries, 7)))
    blocks.append(format_stats_block("🗓️ 30 derniers jours", filter_entries_by_period(all_entries, 30)))

    text = "<b>📊 STATISTIQUES</b>\n\n" + "\n\n".join(blocks)
    remember_broadcast(context, kind='text', text=text, parse_mode="HTML")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_stats_diffusion_keyboard())


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


async def backup_persistence_job(context: ContextTypes.DEFAULT_TYPE):
    """Job planifié : copie quotidienne du fichier de persistance dans backups/."""
    try:
        os.makedirs("backups", exist_ok=True)
        if os.path.exists(PERSISTENCE_FILE):
            date_str = datetime.now(TZ).strftime("%Y-%m-%d_%H%M%S")
            backup_path = os.path.join("backups", f"{os.path.basename(PERSISTENCE_FILE)}.{date_str}.bak")
            shutil.copy2(PERSISTENCE_FILE, backup_path)
            logger.info(f"Sauvegarde créée : {backup_path}")
        else:
            logger.warning("Aucun fichier de persistance trouvé pour la sauvegarde automatique.")
    except Exception as e:
        logger.warning(f"Échec de la sauvegarde automatique : {e}")


async def scheduled_diffusion_job(context: ContextTypes.DEFAULT_TYPE):
    """Job planifié (ponctuel) : envoie une publication programmée à l'heure prévue."""
    job_data = context.job.data
    draft = job_data['draft']
    admin_chat_id = job_data['admin_chat_id']

    if draft['target'] == 'all':
        destinations = [(normalize_broadcast_target(t), None) for t in BROADCAST_TARGETS]
    elif draft['target'] == 'subscribers':
        known = context.bot_data.get('known_users', set())
        destinations = []
        for key in known:
            d_chat_id, d_topic_id = parse_visitor_key(key)
            if d_chat_id == ADMIN_CHAT_ID:
                continue
            destinations.append((d_chat_id, d_topic_id))
    else:
        destinations = [(normalize_broadcast_target(draft['target']), None)]

    sent, failed = 0, 0
    for dest, dm_topic_id in destinations:
        try:
            await send_diffusion_post(context, dest, draft, dm_topic_id=dm_topic_id)
            sent += 1
        except Exception as e:
            logger.warning(f"Échec de diffusion programmée vers {dest} : {e}")
            failed += 1

    try:
        await context.bot.send_message(
            chat_id=admin_chat_id,
            text=f"🕒 Publication programmée envoyée : {sent} réussi(s), {failed} échec(s).",
        )
    except Exception as e:
        logger.warning(f"Échec de notification de diffusion programmée : {e}")


async def channel_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Job planifié : rappelle de rejoindre le canal aux visiteurs inactifs depuis CHANNEL_REMINDER_DAYS."""
    if CHANNEL_CHAT_ID is None:
        return

    known = context.bot_data.get('known_users', set())
    first_seen = context.bot_data.get('visitor_first_seen', {})
    reminded = context.bot_data.setdefault('channel_reminder_sent', set())
    now = datetime.now(TZ)
    sent = 0

    for key in known:
        if key in reminded:
            continue
        d_chat_id, d_topic_id = parse_visitor_key(key)
        if d_chat_id == ADMIN_CHAT_ID:
            continue
        seen_at = first_seen.get(key)
        if not seen_at or (now - seen_at).days < CHANNEL_REMINDER_DAYS:
            continue
        try:
            if await is_channel_member(context, d_chat_id):
                continue
            extra = {'direct_messages_topic_id': d_topic_id} if d_topic_id is not None else {}
            if not CHANNEL_INVITE_LINK:
                continue
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Rejoindre le canal", url=CHANNEL_INVITE_LINK)]])
            await context.bot.send_message(
                chat_id=d_chat_id,
                text="📢 Tu n'as toujours pas rejoint mon canal officiel — ne rate pas les prochains signaux !",
                reply_markup=keyboard,
                **extra,
            )
            reminded.add(key)
            sent += 1
        except Exception as e:
            logger.warning(f"Échec du rappel canal pour {key} : {e}")

    logger.info(f"Rappel canal automatique : {sent} message(s) envoyé(s).")


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

    if is_blocked(context, chat_id):
        return  # Utilisateur bloqué (/block) : on l'ignore silencieusement

    remember_known_user(context, make_visitor_key(chat_id, None))

    # Le choix de langue doit rester accessible même aux visiteurs non autorisés
    if data == "setlang_fr" or data == "setlang_en":
        lang = 'fr' if data == "setlang_fr" else 'en'
        context.user_data['lang'] = lang
        sender = query.from_user
        first_name = sender.first_name if sender and sender.first_name else ""
        await send_welcome_messages(chat_id, context, first_name, lang=lang)
        user_id = sender.id if sender else chat_id
        if not await is_channel_member(context, user_id):
            await send_channel_reminder(chat_id, context, lang=lang)
        return

    if not is_authorized(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="⛔ Accès non autorisé.")
        return

    # Nettoyage : le précédent message "de passage" (menu/confirmation) disparaît dès qu'on
    # passe à l'action suivante. Seuls signaux, résultats et bilans restent dans le chat.
    await clear_last_transient(context)

    # --- Navigation générale ---

    if data == "confirm_resetall":
        context.user_data['history'] = []
        context.user_data['vip_history'] = empty_vip_history()
        await send_transient(context, chat_id, text="🗑️ Toutes les statistiques ont été réinitialisées.")
        return

    if data == "cancel_resetall":
        await send_transient(context, chat_id, text="Annulé, rien n'a été supprimé.")
        return

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
                reply_markup=ADMIN_QUICK_KEYBOARD,
            )
        else:
            tier = 'premium' if claimed_premium else 'standard'
            context.user_data['telegram_tier'] = tier
            label = "💎 PREMIUM" if tier == 'premium' else "⭐ STANDARD"
            await send_transient(
                context, chat_id, text=f"✅ Mode {label} activé.",
                reply_markup=ADMIN_QUICK_KEYBOARD,
            )

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

    if data.startswith("vipnonvip_"):
        session_key = data.replace("vipnonvip_", "")
        if session_key not in VIP_SESSION_LABELS:
            await send_transient(context, chat_id, text="Session invalide.")
            return

        if VIP_CHANNEL_CHAT_ID is None:
            await send_transient(
                context, chat_id,
                text=(
                    "Aucun canal/groupe VIP n'est configuré.\n"
                    "Renseigne VIP_CHANNEL_CHAT_ID dans le .env (le bot doit y être admin)."
                ),
            )
            return

        vip_history = context.user_data.get('vip_history', empty_vip_history())
        entries = vip_history.get(session_key, [])
        icon, label = VIP_SESSION_LABELS[session_key]
        bilan_text = format_bilan_text(
            entries,
            header_title=f"RAPPORT SESSION VIP - {label}",
            blockquote_title=f"{icon} Session VIP {label}",
        )
        promo_text = "🔥 Regarde les résultats de notre session VIP aujourd'hui :\n\n" + bilan_text

        known = context.bot_data.get('known_users', set())
        sent, skipped, failed = 0, 0, 0
        for uid in known:
            uid_chat_id, _ = parse_visitor_key(uid)
            if uid_chat_id == ADMIN_CHAT_ID:
                continue
            if await is_channel_member(context, uid_chat_id, channel=VIP_CHANNEL_CHAT_ID):
                skipped += 1
                continue
            try:
                await send_to_visitor(context, uid, promo_text)
                sent += 1
            except Exception as e:
                logger.warning(f"Échec d'envoi ABONNÉS NON VIP à {uid} : {e}")
                failed += 1

        await send_transient(
            context, chat_id,
            text=f"👥 Envoyé à {sent} abonné(s) non-VIP ({skipped} déjà VIP, {failed} échec(s)).",
        )
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

    if data == "stats_chart":
        history = context.user_data.get('history', [])
        vip_history = context.user_data.get('vip_history', empty_vip_history())
        all_entries = history + [e for entries in vip_history.values() for e in entries]
        if not all_entries:
            await send_transient(context, chat_id, text="Aucune donnée pour générer un graphique.")
            return
        buf = generate_performance_chart(all_entries)
        await context.bot.send_photo(chat_id=chat_id, photo=buf, caption="📈 Performance par jour")
        return

    if data == "statdiff_all" or data == "statdiff_subscribers" or data.startswith("statdiff_target_"):
        content = context.user_data.get('last_broadcast')
        if not content:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return

        if data == "statdiff_all":
            destinations = [(normalize_broadcast_target(t), None) for t in BROADCAST_TARGETS]
        elif data == "statdiff_subscribers":
            known = context.bot_data.get('known_users', set())
            destinations = []
            for key in known:
                d_chat_id, d_topic_id = parse_visitor_key(key)
                if d_chat_id == ADMIN_CHAT_ID:
                    continue
                destinations.append((d_chat_id, d_topic_id))
        else:
            idx = int(data.replace("statdiff_target_", ""))
            if idx < 0 or idx >= len(BROADCAST_TARGETS):
                await send_transient(context, chat_id, text="Cible invalide.")
                return
            destinations = [(normalize_broadcast_target(BROADCAST_TARGETS[idx]), None)]

        sent, failed = 0, 0
        for dest, dm_topic_id in destinations:
            try:
                extra = {'direct_messages_topic_id': dm_topic_id} if dm_topic_id is not None else {}
                await context.bot.send_message(
                    chat_id=dest, text=content.get('text') or '',
                    parse_mode=content.get('parse_mode'), **extra,
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Échec de diffusion des stats vers {dest} : {e}")
                failed += 1

        await send_transient(
            context, chat_id,
            text=f"📤 Diffusion terminée : {sent} envoyé(s), {failed} échec(s).",
        )
        return

    if data == "polltarget_cancel":
        context.user_data['poll_draft'] = None
        await send_transient(context, chat_id, text="Sondage annulé.")
        return

    if data == "polltarget_all" or data == "polltarget_subscribers" or data.startswith("polltarget_idx_"):
        poll_draft = context.user_data.get('poll_draft')
        if not poll_draft or not poll_draft.get('question'):
            await send_transient(context, chat_id, text="Rien à envoyer.")
            return

        if data == "polltarget_all":
            destinations = [(normalize_broadcast_target(t), None) for t in BROADCAST_TARGETS]
        elif data == "polltarget_subscribers":
            known = context.bot_data.get('known_users', set())
            destinations = []
            for key in known:
                d_chat_id, d_topic_id = parse_visitor_key(key)
                if d_chat_id == ADMIN_CHAT_ID:
                    continue
                destinations.append((d_chat_id, d_topic_id))
        else:
            idx = int(data.replace("polltarget_idx_", ""))
            if idx < 0 or idx >= len(BROADCAST_TARGETS):
                await send_transient(context, chat_id, text="Cible invalide.")
                return
            destinations = [(normalize_broadcast_target(BROADCAST_TARGETS[idx]), None)]

        sent, failed = 0, 0
        for dest, dm_topic_id in destinations:
            try:
                extra = {'message_thread_id': dm_topic_id} if dm_topic_id is not None else {}
                await context.bot.send_poll(
                    chat_id=dest, question=poll_draft['question'], options=poll_draft['options'], **extra,
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Échec d'envoi du sondage vers {dest} : {e}")
                failed += 1

        context.user_data['poll_draft'] = None
        await send_transient(context, chat_id, text=f"📊 Sondage envoyé : {sent} réussi(s), {failed} échec(s).")
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
            'image_url': None,
            'buttons': [],
            'options': dict(DEFAULT_DIFFUSION_OPTIONS),
        }
        await send_transient(
            context, chat_id,
            text="✍️ Envoie le texte et/ou la photo de ta publication :",
        )
        return

    if data == "diffextra_photo":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        draft['step'] = 'awaiting_storage_photo'
        await send_transient(
            context, chat_id,
            text="📸 Envoie la photo à afficher en aperçu agrandi sous le texte :",
            reply_markup=get_diffusion_photo_prompt_keyboard(),
        )
        return

    if data == "diffextra_cancel_photo":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await show_main_menu(chat_id, context)
            return
        draft['step'] = 'extras'
        await send_transient(
            context, chat_id,
            text="Que veux-tu ajouter ?",
            reply_markup=get_diffusion_extras_keyboard(),
        )
        return

    if data == "diffextra_buttons":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        draft['step'] = 'buttons'
        await send_transient(
            context, chat_id,
            text=DIFFUSION_BUTTONS_PROMPT,
            reply_markup=get_diffusion_buttons_prompt_keyboard(),
        )
        return

    if data == "diffextra_done":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        draft['step'] = 'preview'
        await show_diffusion_preview(context, chat_id, draft)
        return

    if data == "diffextra_back":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await show_main_menu(chat_id, context)
            return
        draft['text'] = None
        draft['photo_file_id'] = None
        draft['image_url'] = None
        draft['buttons'] = []
        draft['step'] = 'content'
        await send_transient(
            context, chat_id,
            text="✍️ Envoie le texte et/ou la photo de ta publication :",
        )
        return

    if data == "diffbtn_back":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        draft['step'] = 'extras'
        await send_transient(
            context, chat_id,
            text="Que veux-tu ajouter ?",
            reply_markup=get_diffusion_extras_keyboard(),
        )
        return

    if data == "diffbtn_none":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        draft['buttons'] = []
        draft['step'] = 'preview'
        await show_diffusion_preview(context, chat_id, draft)
        return

    if data == "diffopt_open":
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        options = draft.setdefault('options', dict(DEFAULT_DIFFUSION_OPTIONS))
        await send_transient(
            context, chat_id,
            text="Choisissez ce que vous souhaitez modifier.",
            reply_markup=get_diffusion_options_keyboard(options),
        )
        return

    if data in ("diffopt_format", "diffopt_silent", "diffopt_preview", "diffopt_reactions"):
        draft = context.user_data.get('diffusion_draft')
        if not draft:
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return
        options = draft.setdefault('options', dict(DEFAULT_DIFFUSION_OPTIONS))

        if data == "diffopt_format":
            options['format'] = 'Texte brut' if options.get('format') == 'HTML' else 'HTML'
        elif data == "diffopt_silent":
            options['silent'] = not options.get('silent', False)
        elif data == "diffopt_preview":
            options['link_preview'] = not options.get('link_preview', True)
        elif data == "diffopt_reactions":
            options['reactions'] = not options.get('reactions', False)

        await send_transient(
            context, chat_id,
            text="Choisissez ce que vous souhaitez modifier.",
            reply_markup=get_diffusion_options_keyboard(options),
        )
        return

    if data == "diffopt_back":
        draft = context.user_data.get('diffusion_draft')
        if not draft or (not draft.get('text') and not draft.get('photo_file_id')):
            await show_main_menu(chat_id, context)
            return
        await show_diffusion_preview(context, chat_id, draft)
        return

    if data == "diffbtn_schedule":
        draft = context.user_data.get('diffusion_draft')
        if not draft or (not draft.get('text') and not draft.get('photo_file_id')):
            await send_transient(context, chat_id, text="Rien à programmer.")
            return
        draft['step'] = 'awaiting_schedule_time'
        await send_transient(
            context, chat_id,
            text="🕒 Envoie la date et l'heure d'envoi, au format JJ/MM/AAAA HH:MM (heure locale) :",
        )
        return

    if data == "diffbtn_finish":
        if context.user_data.get('diffusion_sending'):
            await send_transient(context, chat_id, text="⏳ Diffusion déjà en cours, patiente...")
            return

        draft = context.user_data.get('diffusion_draft')
        if not draft or (not draft.get('text') and not draft.get('photo_file_id')):
            await send_transient(context, chat_id, text="Rien à diffuser.")
            return

        context.user_data['diffusion_sending'] = True
        try:
            if draft['target'] == 'all':
                destinations = [(normalize_broadcast_target(t), None) for t in BROADCAST_TARGETS]
            elif draft['target'] == 'subscribers':
                known = context.bot_data.get('known_users', set())
                destinations = []
                for key in known:
                    d_chat_id, d_topic_id = parse_visitor_key(key)
                    if d_chat_id == ADMIN_CHAT_ID:
                        continue
                    destinations.append((d_chat_id, d_topic_id))
            else:
                destinations = [(normalize_broadcast_target(draft['target']), None)]

            sent, failed = 0, 0
            all_refs = []
            for dest, dm_topic_id in destinations:
                try:
                    refs = await send_diffusion_post(context, dest, draft, dm_topic_id=dm_topic_id)
                    all_refs.extend(refs)
                    sent += 1
                except Exception as e:
                    logger.warning(f"Échec de diffusion (post libre) vers {dest} : {e}")
                    failed += 1

            context.user_data['diffusion_draft'] = None
            context.user_data['last_diffusion_sent'] = all_refs

            await send_transient(
                context, chat_id,
                text=f"📢 Diffusion terminée : {sent} envoyé(s), {failed} échec(s).",
                reply_markup=get_diffusion_result_keyboard() if all_refs else None,
            )
        finally:
            context.user_data['diffusion_sending'] = False
        return

    if data == "diffdelete_confirm":
        refs = context.user_data.pop('last_diffusion_sent', [])
        if not refs:
            await send_transient(context, chat_id, text="Rien à supprimer.")
            return
        deleted = 0
        for ref_chat, ref_msg in refs:
            if await delete_message_safe(context.bot, ref_chat, ref_msg):
                deleted += 1
        await send_transient(context, chat_id, text=f"🗑️ {deleted}/{len(refs)} message(s) supprimé(s).")
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


async def send_to_visitor(context: ContextTypes.DEFAULT_TYPE, visitor_key: str, text: str, parse_mode: str = None):
    """
    Envoie un message à un visiteur à partir de sa clé (voir make_visitor_key). Ajoute
    automatiquement le direct_messages_topic_id si ce visiteur a écrit via les "Messages
    directs" d'un canal (Telegram exige ce paramètre pour pouvoir lui répondre).
    """
    chat_id, dm_topic_id = parse_visitor_key(visitor_key)
    kwargs = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
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
        if message.caption_html:
            draft['text'] = message.caption_html
        draft['step'] = 'buttons'
        await send_transient(
            context, chat_id,
            text=DIFFUSION_BUTTONS_PROMPT,
            reply_markup=get_diffusion_buttons_prompt_keyboard(),
        )
        return

    if draft and draft.get('step') == 'awaiting_storage_photo':
        try:
            file_id = message.photo[-1].file_id
            tg_file = await context.bot.get_file(file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            draft['image_url'] = await upload_to_catbox(file_bytes)
            draft['step'] = 'extras'
            await send_transient(
                context, chat_id,
                text="✅ Photo hébergée avec succès. Que veux-tu faire d'autre ?",
                reply_markup=get_diffusion_extras_keyboard(),
            )
        except Exception as e:
            logger.warning(f"Échec d'hébergement de l'image pour diffusion : {e}")
            await send_transient(
                context, chat_id,
                text=f"❌ Échec de l'hébergement de la photo : {e}",
                reply_markup=get_diffusion_extras_keyboard(),
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

    # --- Cas -1 : raccourci du clavier persistant (Menu / Stats / Diffusion / Aide) ---
    incoming_text = message.text.strip()
    logger.info(f"Message reçu de {chat_id} (admin={is_admin(chat_id)}) : {incoming_text!r}")

    quick_action = ADMIN_QUICK_ACTIONS.get(incoming_text)
    if quick_action is None:
        # Repli tolérant : ignore l'emoji/la casse/les espaces au cas où le clavier
        # afficherait un texte légèrement différent selon l'appareil.
        simplified = re.sub(r'[^\w]', '', incoming_text).lower()
        for label, action_name in ADMIN_QUICK_ACTIONS.items():
            if re.sub(r'[^\w]', '', label).lower() == simplified:
                quick_action = action_name
                break

    if is_admin(chat_id) and quick_action is not None:
        action = quick_action

        # Un raccourci abandonne toute composition en cours (diffusion, capture)
        context.user_data['diffusion_draft'] = None
        context.user_data['awaiting_capture'] = False
        await clear_last_transient(context)

        if action == "menu":
            await show_main_menu(chat_id, context)
        elif action == "stats":
            await stats_command(update, context)
        elif action == "help":
            await help_command(update, context)
        elif action == "diffusion":
            await send_transient(
                context, chat_id,
                text="📢 Choisis la cible de ta publication :",
                reply_markup=get_diffusion_target_keyboard(),
            )
        elif action == "reset_stats":
            context.user_data['history'] = []
            context.user_data['vip_history'] = empty_vip_history()
            await context.bot.send_message(chat_id=chat_id, text="🗑️ Statistiques réinitialisées.")
        elif action == "poll":
            context.user_data['poll_draft'] = {'step': 'content'}
            await send_transient(
                context, chat_id,
                text="📊 Envoie ta question et tes options séparées par | (ex: Aimez-vous l'or ? | Oui | Non)",
            )
        return

    # --- Cas -0.5 : l'admin est en train de composer un sondage ---
    if is_admin(chat_id):
        poll_draft = context.user_data.get('poll_draft')
        if poll_draft and poll_draft.get('step') == 'content':
            question, options = parse_poll_spec(message.text)
            if not question:
                await send_transient(
                    context, chat_id,
                    text="⚠️ Format invalide. Utilise : Question | Option1 | Option2 (3 éléments minimum).",
                )
                return
            poll_draft['question'] = question
            poll_draft['options'] = options
            poll_draft['step'] = 'target'
            await send_transient(
                context, chat_id,
                text="📊 Choisis où envoyer ce sondage :",
                reply_markup=get_poll_target_keyboard(),
            )
            return

    # --- Cas 0 : l'admin est en train de composer un post DIFFUSION ---
    if is_admin(chat_id):
        draft = context.user_data.get('diffusion_draft')
        if draft:
            step = draft.get('step')

            if step == 'content':
                draft['text'] = message.text_html
                draft['step'] = 'extras'
                await send_transient(
                    context, chat_id,
                    text="Texte enregistré. Que veux-tu ajouter ?",
                    reply_markup=get_diffusion_extras_keyboard(),
                )
                return

            if step == 'buttons':
                rows = parse_diffusion_buttons(message.text)
                if not rows:
                    await send_transient(
                        context, chat_id,
                        text="⚠️ Format non reconnu. Réessaie, ou choisis une option ci-dessous :",
                        reply_markup=get_diffusion_buttons_prompt_keyboard(),
                    )
                    return
                draft['buttons'] = rows
                draft['step'] = 'preview'
                await show_diffusion_preview(context, chat_id, draft)
                return

            if step == 'awaiting_schedule_time':
                try:
                    target_dt = datetime.strptime(message.text.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=TZ)
                except ValueError:
                    await send_transient(
                        context, chat_id,
                        text="⚠️ Format invalide. Utilise JJ/MM/AAAA HH:MM (ex: 25/12/2026 18:30).",
                    )
                    return
                if target_dt <= datetime.now(TZ):
                    await send_transient(context, chat_id, text="⚠️ Cette date est déjà passée. Choisis une heure future.")
                    return
                if context.job_queue is None:
                    await send_transient(context, chat_id, text="⚠️ JobQueue indisponible, impossible de programmer.")
                    return

                context.job_queue.run_once(
                    scheduled_diffusion_job, when=target_dt,
                    data={'draft': dict(draft), 'admin_chat_id': chat_id},
                    name=f"scheduled_diffusion_{chat_id}_{target_dt.timestamp()}",
                )
                context.user_data['diffusion_draft'] = None
                await send_transient(
                    context, chat_id,
                    text=f"✅ Publication programmée pour le {target_dt.strftime('%d/%m/%Y à %H:%M')}.",
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
            await send_to_visitor(context, target_key, message.text_html, parse_mode="HTML")
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
                    await send_to_visitor(context, target_key, message.text_html, parse_mode="HTML")
                    log_conversation(context, target_key, 'admin', message.text)
                    await message.reply_text("✅ Réponse envoyée.")
                except Exception as e:
                    await message.reply_text(f"❌ Échec de l'envoi : {e}")
                return
        # Message de l'admin qui n'est pas une réponse à un visiteur : on l'ignore simplement.
        return

    # --- Cas 3 : message venant d'un visiteur ---

    if is_blocked(context, chat_id):
        return  # Utilisateur bloqué (/block) : on l'ignore silencieusement

    sender = update.effective_user
    sender_name = sender.full_name if sender else "Inconnu"
    username = f"@{sender.username}" if sender and sender.username else "(pas de pseudo)"

    # Si le visiteur a écrit "en tant que canal" (Messages directs), Telegram fournit un
    # direct_messages_topic : on construit alors une clé unique par abonné (voir make_visitor_key),
    # car plusieurs abonnés différents peuvent partager le même chat_id dans ce mode.
    dm_topic = getattr(message, 'direct_messages_topic', None)
    dm_topic_id = dm_topic.topic_id if dm_topic is not None else None
    visitor_key = make_visitor_key(chat_id, dm_topic_id)

    if is_rate_limited(context, visitor_key):
        return  # Anti-spam : trop de messages en peu de temps, on ignore silencieusement

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


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /block <chat_id> : bloque un visiteur (admin uniquement)."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Réservé à l'administrateur.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /block <chat_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id invalide (doit être un nombre).")
        return
    context.bot_data.setdefault('blocked_users', set()).add(target)
    await update.message.reply_text(f"🚫 {target} a été bloqué.")


async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /unblock <chat_id> : débloque un visiteur (admin uniquement)."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Réservé à l'administrateur.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /unblock <chat_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id invalide (doit être un nombre).")
        return
    context.bot_data.setdefault('blocked_users', set()).discard(target)
    await update.message.reply_text(f"✅ {target} a été débloqué.")


def is_rate_limited(context: ContextTypes.DEFAULT_TYPE, visitor_key: str) -> bool:
    """Anti-spam simple : max RATE_LIMIT_MAX_MESSAGES messages par RATE_LIMIT_WINDOW_SECONDS."""
    now_ts = datetime.now(TZ).timestamp()
    all_timestamps = context.bot_data.setdefault('rate_limit', {})
    timestamps = all_timestamps.setdefault(visitor_key, [])
    cutoff = now_ts - RATE_LIMIT_WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)
    if len(timestamps) >= RATE_LIMIT_MAX_MESSAGES:
        return True
    timestamps.append(now_ts)
    return False


async def reset_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /reset_all : réinitialise tout l'historique, avec double confirmation."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("⛔ Réservé à l'administrateur.")
        return
    keyboard = InlineKeyboardMarkup([
        [styled_button("✅ Oui, tout réinitialiser", style="danger", callback_data="confirm_resetall")],
        [InlineKeyboardButton("❌ Non, annuler", callback_data="cancel_resetall")],
    ])
    await update.message.reply_text(
        "⚠️ Ceci va effacer TOUT l'historique (session gratuite + les 4 sous-sessions VIP). "
        "Es-tu sûr ?",
        reply_markup=keyboard,
    )


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
        uid_chat_id, _ = parse_visitor_key(uid)
        if uid_chat_id == ADMIN_CHAT_ID:
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
    app.add_handler(CommandHandler("reset_all", reset_all_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
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

    # Sauvegarde automatique quotidienne (activée par défaut)
    if BACKUP_ENABLED:
        if app.job_queue is None:
            logger.warning("JobQueue indisponible : la sauvegarde automatique ne peut pas être programmée.")
        else:
            app.job_queue.run_daily(
                backup_persistence_job,
                time=dt_time(hour=BACKUP_HOUR, minute=0, tzinfo=TZ),
            )
            logger.info(f"Sauvegarde automatique programmée à {BACKUP_HOUR:02d}:00 ({TIMEZONE_NAME}).")

    # Rappel automatique de canal pour les visiteurs non abonnés (si un canal est configuré)
    if CHANNEL_CHAT_ID is not None:
        if app.job_queue is None:
            logger.warning("JobQueue indisponible : le rappel automatique de canal ne peut pas être programmé.")
        else:
            app.job_queue.run_daily(
                channel_reminder_job,
                time=dt_time(hour=12, minute=0, tzinfo=TZ),
            )
            logger.info(f"Rappel automatique de canal programmé (après {CHANNEL_REMINDER_DAYS} jour(s) d'inactivité).")

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
