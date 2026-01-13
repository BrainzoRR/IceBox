# config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = "IceBoxTbot"

DB_PATH = "icebox.db"

FREE_LIMIT = 50
