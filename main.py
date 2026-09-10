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


def get_asset_filename(asset_string: str) -> str:
    """Transforme '🇦🇺 AUD/CAD 🇨🇦OTC' en 'audcad_otc'."""
    clean_text = re.sub(r'[^a-zA-Z0-9]', '', asset_string).lower()
    if clean_text.endswith("otc"):
        clean_text = clean_text[:-3] + "_otc"
    return clean_text


def get_random_jpeg(directory: str):
    """1. Récupère une image JPEG/JPG complètement aléatoire dans un dossier."""
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
        return None

    jpegs = [
        f for f in os.listdir(directory)
        if f.lower().endswith(('.jpeg', '.jpg'))
    ]
    if not jpegs:
        return None
    return os.path.join(directory, random.choice(jpegs))


def get_specific_jpeg_only(directory: str, asset_filename: str):
    """2. Récupère UNIQUEMENT l'image JPEG correspondant à l'actif exact. Si absente -> None."""
    if not os.path.exists(directory) or not asset_filename:
        return None

    for ext in ('.jpeg', '.jpg'):
        specific_path = os.path.join(directory, f"{asset_filename}{ext}")
        if os.path.exists(specific_path):
            return specific_path
            
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


# --- CLAVIERS INLINE ---

def get_signal_keyboard() -> InlineKeyboardMarkup:
    """Bouton AVOIR SIGNAL."""
    keyboard = [[InlineKeyboardButton("AVOIR SIGNAL", callback_data="btn_get_signal")]]
    return InlineKeyboardMarkup(keyboard)


def get_result_keyboard() -> InlineKeyboardMarkup:
    """5 boutons de résultat sous le signal."""
    keyboard = [
        [
            InlineKeyboardButton("MG0", callback_data="res_mg0"),
            InlineKeyboardButton("MG1", callback_data="res_mg1"),
            InlineKeyboardButton("MG2", callback_data="res_mg2"),
            InlineKeyboardButton("MG3", callback_data="res_mg3"),
            InlineKeyboardButton("❌", callback_data="res_lose"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start."""
    await update.message.reply_text(
        text="👇 Cliquez ci-dessous pour obtenir votre signal :",
        reply_markup=get_signal_keyboard()
    )


async def send_signal_action(query_or_update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie un signal avec une image JPEG ALEATOIRE du dossier IMG/."""
    signal = generate_signal_data()
    message_text = format_signal_text(signal)

    # Sauvegarde de la clé de l'actif
    asset_key = get_asset_filename(signal['actif'])
    context.user_data['last_asset'] = asset_key

    # RÈGLE 1 : Image JPEG purement aléatoire dans IMG/
    image_path = get_random_jpeg(DIR_IMG)
    chat_id = query_or_update.effective_chat.id

    if image_path:
        with open(image_path, 'rb') as photo:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=message_text,
                parse_mode="HTML",
                reply_markup=get_result_keyboard()
            )
    else:
        # Envoi en texte seul si dossier IMG vide
        await context.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_result_keyboard()
        )


async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère le clic sur les boutons."""
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = update.effective_chat.id

    # 1. Clic sur "AVOIR SIGNAL"
    if data == "btn_get_signal":
        await send_signal_action(query, context)
        return

    # 2. Boutons de Victoires
    win_map = {
        "res_mg0": "✅GAIN DIRECT✅",
        "res_mg1": "✅GAIN MARTINGALE 1✅",
        "res_mg2": "✅GAIN MARTINGALE 2✅",
        "res_mg3": "✅GAIN MARTINGALE 3✅"
    }

    if data in win_map:
        caption_text = win_map[data]
        last_asset = context.user_data.get('last_asset', None)

        # RÈGLE 2 : Cherche UNIQUEMENT l'image exacte de la paire dans IMG_WIN/
        image_path = get_specific_jpeg_only(DIR_WIN, last_asset)

        if image_path:
            # L'image existe -> Envoi avec image
            with open(image_path, 'rb') as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption_text,
                    reply_markup=get_signal_keyboard()
                )
        else:
            # L'image n'existe PAS -> Envoi du message texte SANS image
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption_text,
                reply_markup=get_signal_keyboard()
            )
        return

    # 3. Bouton Défaite (❌)
    if data == "res_lose":
        caption_text = "❌PERDU❌"
        # RÈGLE 3 : Image JPEG aléatoire dans IMG_LOSE/
        image_path = get_random_jpeg(DIR_LOSE)

        if image_path:
            with open(image_path, 'rb') as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption_text,
                    reply_markup=get_signal_keyboard()
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption_text,
                reply_markup=get_signal_keyboard()
            )


def main():
    if not TOKEN:
        raise ValueError("Le TELEGRAM_TOKEN n'a pas été trouvé. Vérifiez votre fichier .env")

    app = Application.builder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_button_click))

    logger.info("Bot prêt et démarré !")
    app.run_polling()


if __name__ == "__main__":
    main()
