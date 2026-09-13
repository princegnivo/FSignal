import os
import re
import sys
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


def remember_known_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Ajoute ce chat_id à la liste des utilisateurs connus, pour permettre le /broadcast."""
    known = context.bot_data.setdefault('known_users', set())
    known.add(chat_id)


def remember_broadcast(context: ContextTypes.DEFAULT_TYPE, *, kind: str, text: str = None,
                        photo_file_id: str = None, parse_mode: str = None):
    """Mémorise le dernier contenu envoyé en privé, pour permettre sa diffusion via le bouton DIFFUSER."""
    context.user_data['last_broadcast'] = {
        'kind': kind,  # 'photo' ou 'text'
        'text': text,
        'photo_file_id': photo_file_id,
        'parse_mode': parse_mode,
    }


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
    lien_inscription = "https://bit.ly/4ckz9cY"

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
    """Menu principal : choix entre session gratuite et session VIP."""
    keyboard = [
        [
            InlineKeyboardButton("🆓 SESSION GRATUITE", callback_data="btn_free_menu"),
            InlineKeyboardButton("👑 SESSION VIP", callback_data="btn_vip_menu"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_free_start_keyboard() -> InlineKeyboardMarkup:
    """Bouton SIGNAL uniquement, affiché après le choix de la session gratuite."""
    keyboard = [[InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal")]]
    return InlineKeyboardMarkup(keyboard)


def get_signal_keyboard(include_undo: bool = True) -> InlineKeyboardMarkup:
    """Boutons SIGNAL, BILAN, ANNULER (optionnel) et DIFFUSER (session gratuite)."""
    keyboard = [
        [
            InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([InlineKeyboardButton("↩️ ANNULER DERNIER", callback_data="btn_undo")])
    keyboard.append([InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu")])
    return InlineKeyboardMarkup(keyboard)


def get_result_keyboard() -> InlineKeyboardMarkup:
    """Boutons de résultat sous le signal, + bouton NEW et DIFFUSER. Utilisé en gratuit et en VIP."""
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
            InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_bilan_keyboard() -> InlineKeyboardMarkup:
    """Boutons NEW SESSION, DIFFUSER et MENU PRINCIPAL, affichés sous le bilan gratuit."""
    keyboard = [
        [InlineKeyboardButton("NEW SESSION", callback_data="btn_new_session")],
        [InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu")],
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
    """Boutons SIGNAL / BILAN / ANNULER (optionnel) / DIFFUSER / MENU VIP, après un résultat en session VIP."""
    keyboard = [
        [
            InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ],
    ]
    if include_undo:
        keyboard.append([InlineKeyboardButton("↩️ ANNULER DERNIER", callback_data="btn_undo")])
    keyboard.append([InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu")])
    keyboard.append([InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")])
    return InlineKeyboardMarkup(keyboard)


def get_vip_bilan_keyboard(session_key: str) -> InlineKeyboardMarkup:
    """Boutons affichés sous le bilan d'une sous-session VIP."""
    keyboard = [
        [InlineKeyboardButton("🔄 RECOMMENCER CETTE SESSION", callback_data=f"vip_reset_{session_key}")],
        [InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu")],
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_vip_rapport_keyboard() -> InlineKeyboardMarkup:
    """Boutons DIFFUSER et retour, affichés sous le rapport complet VIP."""
    keyboard = [
        [InlineKeyboardButton("📤 DIFFUSER", callback_data="btn_broadcast_menu")],
        [InlineKeyboardButton("⬅️ MENU VIP", callback_data="btn_vip_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_broadcast_targets_keyboard() -> InlineKeyboardMarkup:
    """Liste des canaux/groupes configurés + option TOUS + annuler."""
    keyboard = [
        [InlineKeyboardButton(f"📡 {target}", callback_data=f"bcast_target_{i}")]
        for i, target in enumerate(BROADCAST_TARGETS)
    ]
    if BROADCAST_TARGETS:
        keyboard.append([InlineKeyboardButton("📤 ENVOYER À TOUS", callback_data="bcast_all")])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="bcast_cancel")])
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


async def start_free_session(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Réinitialise la session gratuite (historique du bilan) et affiche le bouton SIGNAL."""
    context.user_data['mode'] = 'free'
    context.user_data['history'] = []
    context.user_data['last_signal'] = None
    context.user_data['last_signal_message'] = None
    context.user_data['current_trade'] = None

    await send_transient(
        context, chat_id,
        text="👇 Cliquez ci-dessous pour obtenir votre signal :",
        reply_markup=get_free_start_keyboard()
    )


async def enter_vip_session(chat_id, context: ContextTypes.DEFAULT_TYPE, session_key: str, reset: bool = False):
    """Bascule le contexte utilisateur sur une sous-session VIP donnée.
    Si reset=True, vide l'historique de cette sous-session uniquement."""
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start : affiche le menu principal (ne réinitialise rien)."""
    chat_id = update.effective_chat.id
    remember_known_user(context, chat_id)
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await clear_last_transient(context)
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


async def send_signal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie un signal avec une image ALEATOIRE du dossier IMG/."""
    signal = generate_signal_data()
    message_text = format_signal_text(signal)

    # Sauvegarde du signal complet (utile pour le bilan) et de la clé de l'actif
    asset_key = get_asset_filename(signal['actif'])
    context.user_data['last_asset'] = asset_key
    context.user_data['last_signal'] = signal

    image_path = get_random_jpeg(DIR_IMG)
    chat_id = update.effective_chat.id

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
        if not context.user_data.get('last_broadcast'):
            await send_transient(context, chat_id, text="Rien à diffuser pour l'instant.")
            return
        if not BROADCAST_TARGETS:
            await send_transient(
                context, chat_id,
                text=(
                    "Aucun canal/groupe n'est configuré.\n"
                    "Ajoute-les dans BROADCAST_TARGETS (.env), séparés par des virgules "
                    "(ex: @moncanal,-1001234567890). Le bot doit être admin de chacun."
                )
            )
            return
        await send_transient(
            context, chat_id,
            text="📤 Choisis où diffuser ce contenu :",
            reply_markup=get_broadcast_targets_keyboard()
        )
        return

    if data == "bcast_cancel":
        await send_transient(context, chat_id, text="Diffusion annulée.")
        return

    if data == "bcast_all" or data.startswith("bcast_target_"):
        content = context.user_data.get('last_broadcast')
        if not content:
            await send_transient(context, chat_id, text="Rien à diffuser pour l'instant.")
            return

        if data == "bcast_all":
            targets = BROADCAST_TARGETS
        else:
            idx = int(data.replace("bcast_target_", ""))
            if idx < 0 or idx >= len(BROADCAST_TARGETS):
                await send_transient(context, chat_id, text="Cible invalide.")
                return
            targets = [BROADCAST_TARGETS[idx]]

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
    win_map = {
        "res_mg0": ("✅<b>GAIN DIRECT</b>✅", "mg0"),
        "res_mg1": ("✅<b>GAIN MARTINGALE 1</b>✅", "mg1"),
        "res_mg2": ("✅<b>GAIN MARTINGALE 2</b>✅", "mg2"),
        "res_mg3": ("✅<b>GAIN MARTINGALE 3</b>✅", "mg3"),
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
        return

    # Bouton Défaite (❌)
    if data == "res_lose":
        mode = context.user_data.get('mode', 'free')
        record_result(context, "lose")

        caption_text = "❌<b>PERDU</b>❌"
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


async def relay_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Relais des messages texte libres :
    - Si un visiteur écrit au bot, son message est transféré à l'administrateur.
    - Si l'administrateur répond (fonction "Répondre" de Telegram) à un message transféré,
      cette réponse est renvoyée automatiquement au visiteur d'origine.
    """
    message = update.message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    remember_known_user(context, chat_id)

    if ADMIN_CHAT_ID is None:
        return  # Fonctionnalité non configurée (ADMIN_CHAT_ID absent du .env)

    if is_admin(chat_id):
        reply_to = message.reply_to_message
        if reply_to:
            relay_map = context.bot_data.get('relay_map', {})
            target_chat_id = relay_map.get(reply_to.message_id)
            if target_chat_id:
                try:
                    await context.bot.send_message(chat_id=target_chat_id, text=message.text)
                    await message.reply_text("✅ Réponse envoyée.")
                except Exception as e:
                    await message.reply_text(f"❌ Échec de l'envoi : {e}")
                return
        # Message de l'admin qui n'est pas une réponse à un visiteur : on l'ignore simplement.
        return

    # --- Message venant d'un visiteur : on le relaie à l'administrateur ---
    sender = update.effective_user
    sender_name = sender.full_name if sender else "Inconnu"
    username = f"@{sender.username}" if sender and sender.username else "(pas de pseudo)"

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

    await message.reply_text("✅ Message reçu, réponse à venir.")


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
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            logger.warning(f"Échec de diffusion à {uid} : {e}")
            failed += 1

    await update.message.reply_text(f"📢 Diffusion terminée : {sent} envoyé(s), {failed} échec(s).")


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
    app.add_handler(CallbackQueryHandler(handle_button_click))
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
