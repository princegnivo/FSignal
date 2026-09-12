import os
import re
import random
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# Chargement des variables d'environnement
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Configuration du Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
    now = datetime.now()

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

    return f"""<a href="{lien_video}"><b>VIDÉO D’INSCRIPTION</b></a>
______________________________
📊 <b>ACTIF:</b> {signal['actif']}
🕘 <b>HEURE D’ENTRÉE:</b> {signal['entre'].strftime('%H:%M')}
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


def format_bilan_text(history: list) -> str:
    """Formate le texte du bilan de session, dans le style de la capture d'écran."""
    now = datetime.now()
    jour_nom = JOURS_FR[now.weekday()]
    date_str = f"{now.day} {MOIS_FR[now.month]} {now.year}"

    header = f"<b>RAPPORT SESSION GRATUITE\n{jour_nom} {date_str}.</b>"

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

    blockquote = f"<blockquote>🌑 Session gratuite\n{separator}\n{trades_block}</blockquote>"

    gains = sum(1 for e in history if e['result'] != 'lose')
    pertes = sum(1 for e in history if e['result'] == 'lose')

    recap = f"✅ <b>GAIN</b> {to_two_digit_emoji(gains)} x {to_two_digit_emoji(pertes)} <b>PERTE</b> ❌"

    return f"{header}\n\n{blockquote}\n\n{recap}"


# --- CLAVIERS INLINE ---

def get_start_keyboard() -> InlineKeyboardMarkup:
    """Bouton SIGNAL uniquement, affiché après /start."""
    keyboard = [[InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal")]]
    return InlineKeyboardMarkup(keyboard)


def get_signal_keyboard() -> InlineKeyboardMarkup:
    """Boutons SIGNAL et BILAN."""
    keyboard = [
        [
            InlineKeyboardButton("SIGNAL", callback_data="btn_get_signal"),
            InlineKeyboardButton("BILAN", callback_data="btn_bilan"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_result_keyboard() -> InlineKeyboardMarkup:
    """Boutons de résultat sous le signal, + bouton NEW."""
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
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_bilan_keyboard() -> InlineKeyboardMarkup:
    """Bouton NEW SESSION uniquement, affiché sous le bilan."""
    keyboard = [[InlineKeyboardButton("NEW SESSION", callback_data="btn_new_session")]]
    return InlineKeyboardMarkup(keyboard)


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

async def start_session(chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Réinitialise la session (historique du bilan) et affiche le bouton SIGNAL."""
    context.user_data['history'] = []
    context.user_data['last_signal'] = None
    context.user_data['last_signal_message'] = None

    await context.bot.send_message(
        chat_id=chat_id,
        text="👇 Cliquez ci-dessous pour obtenir votre signal :",
        reply_markup=get_start_keyboard()
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start."""
    await start_session(update.effective_chat.id, context)


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


async def send_bilan_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie le bilan de la session en cours."""
    history = context.user_data.get('history', [])
    text = format_bilan_text(history)
    chat_id = update.effective_chat.id

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_bilan_keyboard()
    )


def record_result(context: ContextTypes.DEFAULT_TYPE, result_key: str):
    """Enregistre le résultat du signal courant dans l'historique du bilan."""
    last_signal = context.user_data.get('last_signal')
    if last_signal is not None:
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

    # 1. Clic sur "SIGNAL"
    if data == "btn_get_signal":
        await send_signal_action(update, context)
        return

    # 2. Clic sur "BILAN"
    if data == "btn_bilan":
        await send_bilan_action(update, context)
        return

    # 2bis. Clic sur "NEW SESSION" (équivaut à /start)
    if data == "btn_new_session":
        await start_session(chat_id, context)
        return

    # 3. Clic sur "NEW" : supprime le signal actuel et en envoie un autre
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

    # 4. Boutons de Victoires
    win_map = {
        "res_mg0": ("✅GAIN DIRECT✅", "mg0"),
        "res_mg1": ("✅GAIN MARTINGALE 1✅", "mg1"),
        "res_mg2": ("✅GAIN MARTINGALE 2✅", "mg2"),
        "res_mg3": ("✅GAIN MARTINGALE 3✅", "mg3"),
    }

    if data in win_map:
        caption_text, result_key = win_map[data]
        record_result(context, result_key)

        last_asset = context.user_data.get('last_asset', None)

        # Cherche l'image de la paire dans IMG_WIN/ (insensible aux majuscules)
        image_path = get_specific_jpeg_only(DIR_WIN, last_asset)

        await send_photo_safe(
            bot=context.bot,
            chat_id=chat_id,
            image_path=image_path,
            caption=caption_text,
            reply_markup=get_signal_keyboard()
        )
        return

    # 5. Bouton Défaite (❌)
    if data == "res_lose":
        record_result(context, "lose")

        caption_text = "❌PERDU❌"
        image_path = get_random_jpeg(DIR_LOSE)

        await send_photo_safe(
            bot=context.bot,
            chat_id=chat_id,
            image_path=image_path,
            caption=caption_text,
            reply_markup=get_signal_keyboard()
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log des erreurs rencontrées."""
    logger.error("Exception rencontrée lors du traitement d'une mise à jour :", exc_info=context.error)


def main():
    if not TOKEN:
        raise ValueError("Le TELEGRAM_TOKEN n'a pas été trouvé. Vérifiez votre fichier .env")

    app = Application.builder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_button_click))

    # Gestionnaire d'erreurs
    app.add_error_handler(error_handler)

    logger.info("Bot prêt et démarré !")
    app.run_polling()


if __name__ == "__main__":
    main()
