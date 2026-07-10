"""Точка входа: `python main.py <команда>`.

Эквивалент `python -m src.main`. Примеры:
  python main.py init-data
  python main.py sync
  python main.py run --dry-run
  python main.py run
  python main.py status
  python main.py retry
"""
from src.main import app

if __name__ == "__main__":
    app()
