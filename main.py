from dotenv import load_dotenv

load_dotenv()

from bot import create_app

if __name__ == "__main__":
    app = create_app()
    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling()
