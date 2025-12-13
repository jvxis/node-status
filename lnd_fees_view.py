import sqlite3
import datetime
import os
from pathlib import Path

# Por padrão, usa um arquivo lnd_fees.sqlite na mesma pasta do script.
# Opcionalmente, pode ser sobrescrito pela variável de ambiente LND_FEES_DB.
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("LND_FEES_DB", str(BASE_DIR / "lnd_fees.sqlite"))


def connect():
    """Abre uma conexão com o banco de dados de fees do lnd_balance."""
    return sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)


def fetch_daily_latest():
    """
    Retorna a última linha da tabela daily_fees.

    Espera colunas:
      - date                (TEXT ou DATE, formato ISO YYYY-MM-DD)
      - forward_fees_sat    (INTEGER)
      - rebalance_fees_sat  (INTEGER)
      - net_profit_sat      (INTEGER)
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT date, forward_fees_sat, rebalance_fees_sat, net_profit_sat
            FROM daily_fees
            WHERE date = (SELECT MAX(date) FROM daily_fees)
            """
        ).fetchone()
    return row


def fetch_month_summary():
    """
    Retorna um resumo mensal dos fees.

    Para cada mês (YYYY-MM), soma:
      - forward_fees_sat
      - rebalance_fees_sat
      - net_profit_sat

    Ordena do mês mais recente para o mais antigo e limita a 6 entradas.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT substr(date, 1, 7) AS ym,
                   SUM(forward_fees_sat),
                   SUM(rebalance_fees_sat),
                   SUM(net_profit_sat)
            FROM daily_fees
            GROUP BY ym
            ORDER BY ym DESC
            LIMIT 6
            """
        ).fetchall()
    return rows


def fetch_ytd():
    """
    Retorna o acumulado no ano corrente (Year-To-Date).

    Soma:
      - forward_fees_sat
      - rebalance_fees_sat
      - net_profit_sat
    para todas as linhas com date começando pelo ano atual (YYYY-...).
    """
    year = str(datetime.date.today().year)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT SUM(forward_fees_sat),
                   SUM(rebalance_fees_sat),
                   SUM(net_profit_sat)
            FROM daily_fees
            WHERE date LIKE ? || '-%%'
            """,
            (year,),
        ).fetchone()
    return row
