from dotenv import load_dotenv

load_dotenv()

from tg_form_filler.bot import create_app


def main():
    app = create_app()
    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
