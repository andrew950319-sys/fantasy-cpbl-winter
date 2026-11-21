import sqlite3

conn = sqlite3.connect("fantasy_league.db")
cursor = conn.cursor()

# --- 1. 建立測試球隊與球員 ---
cursor.execute("INSERT OR IGNORE INTO fantasy_teams (team_id, team_name, owner_name) VALUES (1, '台中狂龍隊', 'Andrew')")

# 塞入兩位測試球員：一位資深球星、一位新秀
players_data = [
    ("P001", "陳傑憲", "統一獅", "OF", 0, 10),  # is_rookie=0 (資深)
    ("P002", "超級新秀A", "樂天桃猿", "SS", 1, 0) # is_rookie=1 (新秀)
]
cursor.executemany("INSERT OR IGNORE INTO players VALUES (?, ?, ?, ?, ?, ?)", players_data)

# 假設季中兩人都被排在「一軍 (ACTIVE)」
cursor.execute("INSERT OR REPLACE INTO team_rosters (team_id, player_id, roster_slot) VALUES (1, 'P001', 'ACTIVE')")
cursor.execute("INSERT OR REPLACE INTO team_rosters (team_id, player_id, roster_slot) VALUES (1, 'P002', 'ACTIVE')")

# --- 2. 測試規則：開季前新秀一併歸在二軍 ---
def opening_day_reset():
    cursor.execute("""
        UPDATE team_rosters 
        SET roster_slot = 'MINORS' 
        WHERE player_id IN (SELECT player_id FROM players WHERE is_rookie = 1)
    """)
    print("✅ 已執行開季重置：所有新秀已統一調回二軍！")

# --- 3. 測試規則：一二軍升降 (檢查 25 / 35 上限) ---
def move_player(team_id, player_id, target_slot):
    limit = 25 if target_slot == 'ACTIVE' else 35
    cursor.execute("SELECT COUNT(*) FROM team_rosters WHERE team_id = ? AND roster_slot = ?", (team_id, target_slot))
    current_count = cursor.fetchone()[0]
    
    if current_count >= limit:
        print(f"❌ 調度失敗：{target_slot} 名單已滿 ({current_count}/{limit})！")
        return
    
    cursor.execute("UPDATE team_rosters SET roster_slot = ? WHERE team_id = ? AND player_id = ?", (target_slot, team_id, player_id))
    print(f"✅ 調度成功：球員 {player_id} 已移動至 {target_slot} (目前人數: {current_count + 1}/{limit})")

# --- 4. 測試規則：寫入單場比賽成績並自動累計整季成績 ---
def record_game_stats(season_year, player_id, ab, hits, hr, rbi):
    # 若整季表還沒有該球員紀錄，先初始化一筆
    cursor.execute("INSERT OR IGNORE INTO player_season_stats (season_year, player_id) VALUES (?, ?)", (season_year, player_id))
    # 將單場數據累加進整季數據
    cursor.execute("""
        UPDATE player_season_stats 
        SET games = games + 1, ab = ab + ?, hits = hits + ?, hr = hr + ?, rbi = rbi + ?
        WHERE season_year = ? AND player_id = ?
    """, (ab, hits, hr, rbi, season_year, player_id))

# === 開始執行模擬測試 ===
print("--- 開始春訓規則測試 ---")
opening_day_reset() # 執行開季新秀歸二軍

# 模擬新秀開季後表現太好，玩家手動把他升上一軍
move_player(1, "P002", "ACTIVE")

# 模擬陳傑憲打了兩場比賽，測試整季成績是否正確累加
record_game_stats(2026, "P001", ab=4, hits=2, hr=1, rbi=3) # 第一場：4打數2安打1轟3打點
record_game_stats(2026, "P001", ab=5, hits=1, hr=0, rbi=1) # 第二場：5打數1安打0轟1打點

conn.commit()

# --- 5. 查詢並印出結果驗證 ---
print("\n--- 目前陣容狀態 ---")
for row in cursor.execute("SELECT p.name, p.is_rookie, r.roster_slot FROM team_rosters r JOIN players p ON r.player_id = p.player_id"):
    role = "新秀" if row[1] else "資深"
    print(f"球員：{row[0]} ({role}) -> 目前位置：{row[2]}")

print("\n--- 2026 整季累計成績 ---")
for row in cursor.execute("SELECT p.name, s.games, s.ab, s.hits, s.hr, s.rbi FROM player_season_stats s JOIN players p ON s.player_id = p.player_id"):
    avg = row[3] / row[2] if row[2] > 0 else 0
    print(f"球員：{row[0]} | 出賽：{row[1]} | 打數：{row[2]} | 安打：{row[3]} | 全壘打：{row[4]} | 打點：{row[5]} | 打擊率：{avg:.3f}")

conn.close()