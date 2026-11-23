import sqlite3

conn = sqlite3.connect("fantasy_league.db")
rows = conn.execute(
    "SELECT game_sno, game_date, week_key FROM cpbl_games WHERE kind_code='P' ORDER BY game_date, CAST(game_sno AS INTEGER)"
).fetchall()
print(rows)
conn.close()