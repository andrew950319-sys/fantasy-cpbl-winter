import sqlite3

DB_PATH = "fantasy_league.db"

players_to_add = [
    ("P003", "陳真", "OF", "富邦悍將"),
    ("P004", "戴培峰", "C", "富邦悍將"),
    ("P005", "林岱安", "C", "統一獅"),
]

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

for pid, name, pos, team in players_to_add:
    cur.execute(
        """
        INSERT OR IGNORE INTO players (player_id, name, position, real_team)
        VALUES (?, ?, ?, ?)
        """,
        (pid, name, pos, team),
    )

conn.commit()
conn.close()
print("✅ 球員已加進 players 表")