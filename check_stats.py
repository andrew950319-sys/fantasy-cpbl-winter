import sqlite3

conn = sqlite3.connect("fantasy_league.db")
conn.execute(
    "UPDATE cpbl_games SET week_key = '2026-11-30_2026-12-04' "
    "WHERE kind_code = 'P' AND game_date BETWEEN '2026-11-30' AND '2026-12-04'"
)
conn.commit()
print(
    conn.execute(
        "SELECT game_sno, game_date, week_key FROM cpbl_games "
        "WHERE kind_code = 'P' AND game_date >= '2026-12-01' ORDER BY game_date, game_sno"
    ).fetchall()
)
conn.close()