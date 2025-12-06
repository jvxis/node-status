#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
from pathlib import Path
from datetime import date

# Caminho da base gerada pelo lnd_offchain_balance.py
DB_PATH = Path("/home/admin/offchain_profit.sqlite3")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_profit_last_day():
    """
    Retorna o último dia consolidado na tabela daily_offchain_profit.

    Formato:
    {
        "date": "YYYY-MM-DD",
        "forwards": int,
        "rebalances": int,
        "profit": int
    }
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT iso_date, date, forwards_sat, rebalances_sat, profit_sat
            FROM daily_offchain_profit
            ORDER BY iso_date DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return None

        # Usamos iso_date (YYYY-MM-DD) porque o front converte para BR
        return {
            "date": row["iso_date"],
            "forwards": row["forwards_sat"],
            "rebalances": row["rebalances_sat"],
            "profit": row["profit_sat"],
        }
    finally:
        conn.close()


def get_profit_year_to_date() -> int:
    """
    Soma o lucro (profit_sat) de todos os dias do ANO CORRENTE.
    Isso é o YTD real, baseado na coluna iso_date (YYYY-MM-DD).
    """
    year = str(date.today().year)
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT COALESCE(SUM(profit_sat), 0) AS total_profit
            FROM daily_offchain_profit
            WHERE substr(iso_date, 1, 4) = ?
            """,
            (year,),
        )
        row = cur.fetchone()
        return int(row["total_profit"] or 0)
    finally:
        conn.close()


def get_profit_month_summary():
    """
    Retorna lista com resumo mensal de TODO o histórico.

    Cada item:
    {
        "month": "YYYY-MM",
        "forwards": int,
        "rebalances": int,
        "profit": int
    }

    Isso alimenta o bloco de:
      - Mês atual
      - Mês anterior
      - Resumo últimos meses
    no dashboard.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT
                substr(iso_date, 1, 7) AS ym,      -- YYYY-MM
                SUM(forwards_sat)   AS sum_forwards,
                SUM(rebalances_sat) AS sum_rebalances,
                SUM(profit_sat)     AS sum_profit
            FROM daily_offchain_profit
            GROUP BY ym
            ORDER BY ym
            """
        ).fetchall()

        result = []
        for r in rows:
            result.append(
                {
                    "month": r["ym"],
                    "forwards": int(r["sum_forwards"] or 0),
                    "rebalances": int(r["sum_rebalances"] or 0),
                    "profit": int(r["sum_profit"] or 0),
                }
            )
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    # Teste rápido de linha de comando
    print("last_day:", get_profit_last_day())
    monthly = get_profit_month_summary()
    print("monthly (len):", len(monthly))
    print("monthly tail 3:", monthly[-3:])
    print("ytd:", get_profit_year_to_date())
