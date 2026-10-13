"""Create private server settings once; never overwrite existing credentials."""
import getpass
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    if target.exists():
        raise SystemExit(".env уже существует; откройте его для изменения, чтобы сохранить текущие настройки.")
    hostname = input("Публичное имя сайта, например my-novel.duckdns.org: ").strip().lower()
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", hostname):
        raise SystemExit("Введите только публичное имя сайта, без https://, пути или порта.")
    key = getpass.getpass("API-ключ OpenAI (ввод скрыт; Enter — настроить позже): ").strip()
    if key and (not key.startswith("sk-") or any(character.isspace() for character in key)):
        raise SystemExit("Ключ должен начинаться с sk- и не содержать пробелов или переносов.")
    contents = "\n".join([
        "# Private server settings; do not upload this file or share it in chat.",
        f"NOVEL_DOMAIN={hostname}",
        f"POSTGRES_PASSWORD={secrets.token_hex(32)}",
        f"NOVEL_OPENAI_API_KEY={key}",
        "NOVEL_OPENAI_MODEL=gpt-4.1-mini",
        "NOVEL_AI_JOB_BUDGET_USD=1",
        "NOVEL_AI_DAILY_BUDGET_USD=5",
        "",
    ])
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(contents)
    print("Создан приватный .env. Секреты не выводятся.")


if __name__ == "__main__":
    main()
