import asyncio
import random
from datetime import datetime, timedelta
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Liste complète des paires OTC au format exact : 🇦🇺 AUD/NZD 🇳🇿OTC
ACTIFS = [
    # Paires à 92%
    "🇦🇺 AUD/CAD 🇨🇦OTC",
    "🇨🇦 CAD/CHF 🇨🇭OTC",
    "🇨🇦 CAD/JPY 🇯🇵OTC",
    "🇨🇭 CHF/NOK 🇳🇴OTC",
    "🇪🇺 EUR/CHF 🇨🇭OTC",
    "🇪🇺 EUR/TRY 🇹🇷OTC",
    "🇪🇺 EUR/USD 🇺🇸OTC",
    "🇬🇧 GBP/AUD 🇦🇺OTC",
    "🇬🇧 GBP/JPY 🇯🇵OTC",
    "🇬🇧 GBP/USD 🇺🇸OTC",
    "🇰🇪 KES/USD 🇺🇸OTC",
    "🇳🇿 NZD/USD 🇺🇸OTC",
    "🇸🇦 SAR/CNY 🇨🇳OTC",
    "🇹🇳 TND/USD 🇺🇸OTC",
    "🇺🇦 UAH/USD 🇺🇸OTC",
    "🇺🇸 USD/BRL 🇧🇷OTC",
    "🇺🇸 USD/CAD 🇨🇦OTC",
    "🇺🇸 USD/CHF 🇨🇭OTC",
    "🇺🇸 USD/CLP 🇨🇱OTC",
    "🇺🇸 USD/COP 🇨🇴OTC",
    "🇺🇸 USD/EGP 🇪🇬OTC",
    "🇺🇸 USD/IDR 🇮🇩OTC",
    "🇺🇸 USD/PHP 🇵🇭OTC",
    "🇺🇸 USD/RUB 🇷🇺OTC",
    "🇺🇸 USD/THB 🇹🇭OTC",
    "🇾🇪 YER/USD 🇺🇸OTC",
    
    # Paires de 85% à 91%
    "🇴🇲 OMR/CNY 🇨🇳OTC",
    "🇺🇸 USD/BDT 🇧🇩OTC",
    "🇺🇸 USD/MXN 🇲🇽OTC",
    "🇪🇺 EUR/NZD 🇳🇿OTC",
    "🇪🇺 EUR/JPY 🇯🇵OTC",
    "🇧🇭 BHD/CNY 🇨🇳OTC",
    
    # Paires de 75% à 84%
    "🇦🇪 AED/CNY 🇨🇳OTC",
    "🇦🇺 AUD/NZD 🇳🇿OTC",
    "🇦🇺 AUD/CHF 🇨🇭OTC",
    "🇦🇺 AUD/JPY 🇯🇵OTC",
    "🇳🇬 NGN/USD 🇺🇸OTC",
    "🇨🇭 CHF/JPY 🇯🇵OTC",
    "🇲🇦 MAD/USD 🇺🇸OTC",
    "🇶🇦 QAR/CNY 🇨🇳OTC",
    "🇺🇸 USD/SGD 🇸🇬OTC",
    "🇺🇸 USD/ARS 🇦🇷OTC",
    "🇪🇺 EUR/RUB 🇷🇺OTC",
    "🇺🇸 USD/CNH 🇨🇳OTC",
    "🇺🇸 USD/JPY 🇯🇵OTC",
    
    # Paires de 65% à 74%
    "🇳🇿 NZD/JPY 🇯🇵OTC",
    "🇺🇸 USD/VND 🇻🇳OTC",
    "🇺🇸 USD/MYR 🇲🇾OTC",
    "🇿🇦 ZAR/USD 🇺🇸OTC",
    "🇦🇺 AUD/USD 🇺🇸OTC",
    "🇪🇺 EUR/GBP 🇬🇧OTC",
    "🇺🇸 USD/PKR 🇵🇰OTC",
    "🇺🇸 USD/DZD 🇩🇿OTC",
    
    # Paires inférieures à 65%
    "🇪🇺 EUR/HUF 🇭🇺OTC",
    "🇱🇧 LBP/USD 🇺🇸OTC",
    "🇯🇴 JOD/CNY 🇨🇳OTC",
    "🇺🇸 USD/INR 🇮🇳OTC"
]

class TradingSignalBot:
    def __init__(self, token: str):
        self.token = token
        self.application = (
            Application.builder()
            .token(token)
            .post_init(self.post_init)
            .build()
        )
        self.active_chats = set()

    async def post_init(self, application: Application):
        """Initialise la boucle d'envoi en arrière-plan après le démarrage de l'application"""
        asyncio.create_task(self.signal_loop())

    def generate_signal(self):
        """Génère un signal de trading avec 2 min d'intervalle entre chaque MG"""
        actif = random.choice(ACTIFS)
        direction = random.choice(["CALL", "PUT"])
        now = datetime.now()
        
        # Heure d'entrée (+3 minutes)
        entre = now + timedelta(minutes=3)
        entre = entre.replace(second=0, microsecond=0)
        
        # Martingales espacées de 2 minutes chacune
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

    def format_signal_message(self, signal):
        """Formate le signal en message Telegram"""
        message = f"""
______________________________
📊 ACTIF: {signal['actif']}
🕘 HEURE D'ENTRÉE: {signal['entre'].strftime('%H:%M')}
⏳ EXPIRATION: 120s (2min)

🔮 Direction: {signal['direction']}

🔘 Martingales
1️⃣MG1: {signal['mg1'].strftime('%H:%M')}
2️⃣MG2: {signal['mg2'].strftime('%H:%M')}
3️⃣MG3: {signal['mg3'].strftime('%H:%M')}
———————————————
"""
        return message

    async def send_signal(self):
        """Envoie un signal à tous les chats actifs"""
        if not self.active_chats:
            return

        signal = self.generate_signal()
        message = self.format_signal_message(signal)
        
        for chat_id in self.active_chats.copy():
            try:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML"
                )
                logger.info(f"Signal envoyé à {chat_id}")
            except Exception as e:
                logger.error(f"Erreur lors de l'envoi à {chat_id}: {e}")
                self.active_chats.discard(chat_id)

    async def signal_loop(self):
        """Boucle d'envoi des signaux toutes les 2 minutes"""
        while True:
            await self.send_signal()
            await asyncio.sleep(120)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère la commande /start"""
        chat_id = update.effective_chat.id
        self.active_chats.add(chat_id)
        
        signal = self.generate_signal()
        message = self.format_signal_message(signal)
        
        await update.message.reply_text(
            f"🤖 Bot de signaux activé !\n\n"
            f"Les signaux seront envoyés toutes les 2 minutes.\n\n"
            f"{message}"
        )
        logger.info(f"Nouveau chat ajouté: {chat_id}")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère la commande /stop"""
        chat_id = update.effective_chat.id
        self.active_chats.discard(chat_id)
        
        await update.message.reply_text(
            "🛑 Bot désactivé. Les signaux ne seront plus envoyés.\n"
            "Pour réactiver, utilisez /start"
        )
        logger.info(f"Chat retiré: {chat_id}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère la commande /help"""
        help_text = """
📈 **Bot de Signaux de Trading**

**Commandes disponibles:**
/start - Démarrer la réception des signaux
/stop - Arrêter la réception des signaux
/help - Afficher cette aide
/signal - Obtenir un signal immédiat

**Configuration:**
- Expiration: 120s (2 min)
- Intervalle MG: 2 minutes (MG1, MG2, MG3)
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def signal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère la commande /signal pour un signal immédiat"""
        signal = self.generate_signal()
        message = self.format_signal_message(signal)
        await update.message.reply_text(message)

    def setup_handlers(self):
        """Configure les handlers du bot"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("stop", self.stop_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("signal", self.signal_command))

    def run(self):
        """Démarre le bot"""
        self.setup_handlers()
        logger.info("Bot démarré !")
        self.application.run_polling()

def main():
    TOKEN = "VOTRE_TOKEN_TELEGRAM_ICI"
    
    bot = TradingSignalBot(TOKEN)
    bot.run()

if __name__ == "__main__":
    main()
