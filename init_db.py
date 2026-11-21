import sqlite3

# 連線並建立本地資料庫檔案
conn = sqlite3.connect("fantasy_league.db")
cursor = conn.cursor()

# 1. 球員基本資料與新秀年資表
cursor.execute("""
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    real_team TEXT,
    position TEXT,
    is_rookie BOOLEAN DEFAULT 1,
    rookie_seniority INTEGER DEFAULT 0
)
""")

# 2. Fantasy 玩家球隊表
cursor.execute("""
CREATE TABLE IF NOT EXISTS fantasy_teams (
    team_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT NOT NULL,
    owner_name TEXT NOT NULL
)
""")

# 3. 球隊名單與一二軍狀態表 (一軍上限25, 二軍上限35)
cursor.execute("""
CREATE TABLE IF NOT EXISTS team_rosters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER,
    player_id TEXT UNIQUE,
    roster_slot TEXT CHECK(roster_slot IN ('ACTIVE', 'MINORS')) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES fantasy_teams(team_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
)
""")

# 4. 球員整季成績累計表
cursor.execute("""
CREATE TABLE IF NOT EXISTS player_season_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_year INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    games INTEGER DEFAULT 0,
    pa INTEGER DEFAULT 0,
    ab INTEGER DEFAULT 0,
    hits INTEGER DEFAULT 0,
    hr INTEGER DEFAULT 0,
    rbi INTEGER DEFAULT 0,
    sb INTEGER DEFAULT 0,
    ip_outs INTEGER DEFAULT 0, -- 用出局數存投球局數 (避免小數點計算誤差)
    er INTEGER DEFAULT 0,
    so INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    saves INTEGER DEFAULT 0,
    holds INTEGER DEFAULT 0,
    UNIQUE(season_year, player_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
)
""")

# 5. 聯盟歷史：歷年總冠軍
cursor.execute("""
CREATE TABLE IF NOT EXISTS league_champions (
    season_year INTEGER PRIMARY KEY,
    champion_team_id INTEGER,
    runner_up_team_id INTEGER,
    notes TEXT,
    FOREIGN KEY (champion_team_id) REFERENCES fantasy_teams(team_id)
)
""")

# 6. 聯盟歷史：年度獎項
cursor.execute("""
CREATE TABLE IF NOT EXISTS league_awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_year INTEGER NOT NULL,
    award_name TEXT NOT NULL,
    winner_team_id INTEGER,
    winner_player_id TEXT,
    description TEXT
)
""")

# 7. 聯盟歷史：歷年選秀紀錄
cursor.execute("""
CREATE TABLE IF NOT EXISTS draft_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_year INTEGER NOT NULL,
    draft_type TEXT NOT NULL,
    round_num INTEGER NOT NULL,
    pick_num INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    player_id TEXT NOT NULL
)
""")

conn.commit()
conn.close()
print("資料庫 fantasy_league.db 與所有核心表格建立完成！")