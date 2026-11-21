import sqlite3
from datetime import datetime, date

conn = sqlite3.connect("fantasy_league.db")
cur = conn.cursor()

team = "台灣海洋"
code = "shanhai_sea"
stat_date = date.today().isoformat()
now_iso = datetime.now().isoformat(timespec="seconds")

plan = [
    ("C", "C"), ("1B", "1B"), ("2B", "2B"), ("3B", "3B"),
    ("SS", "SS"), ("OF", "OF"), ("DH", "DH"),
    ("SP", "SP1"), ("SP", "SP2"), ("RP", "RP1"), ("RP", "RP2"),
]

for i in range(1, 26):
    pid = f"TEST_{code}_{i:02d}"
    pname = f"{team}球員{i:02d}"
    pos, l_slot = plan[(i - 1) % len(plan)] if i <= len(plan) else ("OF", "BENCH")

    cur.execute("""
        INSERT INTO players (player_id, name, real_team, position, positions)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
            name = excluded.name,
            real_team = excluded.real_team,
            position = excluded.position,
            positions = excluded.positions
    """, (pid, pname, team, pos, pos))

    cur.execute("""
        INSERT INTO team_rosters (player_id, fantasy_team, roster_slot, lineup_slot, slot_changed_at, status_reason)
        VALUES (?, ?, 'ACTIVE', ?, ?, '測試灌入')
        ON CONFLICT(player_id) DO UPDATE SET
            fantasy_team = excluded.fantasy_team,
            roster_slot = excluded.roster_slot,
            lineup_slot = excluded.lineup_slot,
            slot_changed_at = excluded.slot_changed_at,
            status_reason = excluded.status_reason
    """, (pid, team, l_slot, now_iso))

    if i <= len(plan):
        if pos in {"SP", "RP"}:
            is_sp = pos == "SP"
            cur.execute("""
                INSERT INTO player_daily_stats (
                    player_id, stat_date, ip, er, k, w, hld, sv, pitch_h, pitch_bb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, stat_date) DO UPDATE SET
                    ip = excluded.ip, er = excluded.er, k = excluded.k,
                    w = excluded.w, hld = excluded.hld, sv = excluded.sv,
                    pitch_h = excluded.pitch_h, pitch_bb = excluded.pitch_bb
            """, (
                pid, stat_date,
                5.0 if is_sp else 1.0,
                2 if is_sp else 1,
                5 if is_sp else 1,
                1 if i == 8 else 0,
                1 if i == 10 else 0,
                1 if i == 11 else 0,
                5 if is_sp else 2,
                2 if is_sp else 1,
            ))
        else:
            cur.execute("""
                INSERT INTO player_daily_stats (
                    player_id, stat_date, r, ab, h, bb, hbp, sf, rbi, sb, hr, tb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, stat_date) DO UPDATE SET
                    r = excluded.r, ab = excluded.ab, h = excluded.h,
                    bb = excluded.bb, hbp = excluded.hbp, sf = excluded.sf,
                    rbi = excluded.rbi, sb = excluded.sb, hr = excluded.hr, tb = excluded.tb
            """, (
                pid, stat_date,
                1 if i % 2 == 0 else 0,
                4,
                1 if i % 2 == 1 else 2,
                1 if i % 3 == 0 else 0,
                0, 0,
                1 if i % 3 != 0 else 2,
                1 if i == 3 else 0,
                1 if i == 4 else 0,
                4 if i == 4 else 2,
            ))

conn.commit()
conn.close()
print("已灌入台灣海洋 25 人陣容與模擬單場成績！")