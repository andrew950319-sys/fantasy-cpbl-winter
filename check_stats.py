import sqlite3

conn = sqlite3.connect("fantasy_league.db")
cur = conn.cursor()
rows = cur.execute("""
    SELECT p.name, s.ab, s.h, s.bb, s.hbp, s.sf
    FROM player_game_stats s
    JOIN players p ON s.player_id = p.player_id
    WHERE s.stat_date = '2026-11-23' AND (s.bb > 0 OR s.hbp > 0)
""").fetchall()
print(rows)
conn.close()