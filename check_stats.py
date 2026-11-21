import sqlite3
import cpbl_fetcher as cf
import main as main_mod

PROD_DB = "fantasy_league.db"
ALLOWED_TEAMS = {"台灣海洋", "台灣山林"}


def filter_and_finalize_prod(db_path: str = PROD_DB) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        # 找出非台灣海洋/台灣山林且不在其他 fantasy 隊伍陣容中的 AWB 自動建檔球員
        rows = cur.execute(
            """
            SELECT p.player_id, p.name, p.real_team
            FROM players p
            WHERE p.player_id LIKE 'AWB_%'
              AND (p.real_team IS NULL OR p.real_team NOT IN ('台灣海洋', '台灣山林'))
            """
        ).fetchall()

        removed_pids = [r["player_id"] for r in rows]
        if removed_pids:
            placeholders = ",".join("?" for _ in removed_pids)
            cur.execute(
                f"DELETE FROM player_game_stats WHERE player_id IN ({placeholders})",
                removed_pids,
            )
            cur.execute(
                f"DELETE FROM player_daily_stats WHERE player_id IN ({placeholders})",
                removed_pids,
            )
            cur.execute(
                f"DELETE FROM team_rosters WHERE player_id IN ({placeholders})",
                removed_pids,
            )
            cur.execute(
                f"DELETE FROM players WHERE player_id IN ({placeholders})",
                removed_pids,
            )

        # 重算受影響的單日彙總
        cur.execute("DELETE FROM player_daily_stats")
        cur.execute(
            """
            INSERT INTO player_daily_stats (
                player_id, stat_date,
                r, ab, h, bb, hbp, sf, rbi, sb, hr, tb,
                w, ip, er, k, hld, sv, pitch_h, pitch_bb
            )
            SELECT
                player_id, stat_date,
                COALESCE(SUM(r), 0), COALESCE(SUM(ab), 0), COALESCE(SUM(h), 0),
                COALESCE(SUM(bb), 0), COALESCE(SUM(hbp), 0), COALESCE(SUM(sf), 0),
                COALESCE(SUM(rbi), 0), COALESCE(SUM(sb), 0), COALESCE(SUM(hr), 0),
                COALESCE(SUM(tb), 0), COALESCE(SUM(w), 0), COALESCE(SUM(ip), 0.0),
                COALESCE(SUM(er), 0), COALESCE(SUM(k), 0), COALESCE(SUM(hld), 0),
                COALESCE(SUM(sv), 0), COALESCE(SUM(pitch_h), 0), COALESCE(SUM(pitch_bb), 0)
            FROM player_game_stats
            GROUP BY player_id, stat_date
            """
        )

        conn.commit()
        print(f"已清理非台灣兩隊的 AWB 球員數: {len(removed_pids)}")
    finally:
        conn.close()

    # 只讓屬於台灣海洋 / 台灣山林的球員進入 save_parsed_stats
    orig_save = cf.save_parsed_stats

    def filtered_save_parsed_stats(parsed_by_name, *args, **kwargs):
        filtered = {
            name: stats
            for name, stats in parsed_by_name.items()
            if (stats.get("team_name") or "").strip() in ALLOWED_TEAMS
        }
        return orig_save(filtered, *args, **kwargs)

    cf.save_parsed_stats = filtered_save_parsed_stats
    try:
        res = cf.import_cpbl_game("2026", "P", "16", db_path=db_path)
        print(
            f"重新匯入 2026_P_16 完成: 寫入台灣兩隊球員={res.get('imported_count')} | "
            f"自動建檔={res.get('auto_created_count')}"
        )
    finally:
        cf.save_parsed_stats = orig_save

    main_mod.get_db = lambda: (
        lambda c: (setattr(c, "row_factory", sqlite3.Row), c)[1]
    )(sqlite3.connect(db_path, timeout=30))

    print("\n=== 正式啟用現況 (ALL) ===")
    for team_name in ("台灣海洋", "台灣山林"):
        s = main_mod.calc_team_12_cats(team_name, "ALL")
        print(
            f"  [{team_name}] "
            f"R={s['r']['disp']} AVG={s['avg']['disp']} RBI={s['rbi']['disp']} "
            f"HR={s['hr']['disp']} SB={s['sb']['disp']} OPS={s['ops']['disp']} || "
            f"W={s['w']['disp']} ERA={s['era']['disp']} K={s['k']['disp']} "
            f"HLD={s['hld']['disp']} SV={s['sv']['disp']} WHIP={s['whip']['disp']}"
        )


if __name__ == "__main__":
    filter_and_finalize_prod(PROD_DB)