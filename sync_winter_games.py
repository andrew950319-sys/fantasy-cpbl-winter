import sqlite3
import cpbl_fetcher as cf
import main as main_mod

PROD_DB = "fantasy_league.db"
YEAR = "2026"
KIND_CODE = "P"

# 測試期間有新場次就加在這裡（例如 "16", "17", "18"）
GAME_SNOS: list[str] = [
    "16",
]


def run_test_sync(
    game_snos: list[str] = GAME_SNOS,
    db_path: str = PROD_DB,
) -> None:
    synced = 0
    skipped: list[str] = []

    for sno in game_snos:
        sno_str = str(sno).strip()
        if not sno_str:
            continue
        try:
            res = cf.import_cpbl_game(YEAR, KIND_CODE, sno_str, db_path=db_path)
            if res.get("total_parsed", 0) == 0:
                skipped.append(f"{sno_str} (尚無資料)")
                continue
            synced += 1
            meta = res.get("game_meta", {})
            print(
                f"[測試同步 {YEAR}_{KIND_CODE}_{sno_str}] "
                f"日期={res.get('stat_date')} | "
                f"{meta.get('away_team', '')} vs {meta.get('home_team', '')} | "
                f"匯入={res.get('imported_count')}/{res.get('total_parsed')} | "
                f"自動建檔={res.get('auto_created_count')}"
            )
            if res.get("unmatched_names"):
                print(f"  未對應名單: {res['unmatched_names']}")
        except Exception as exc:
            skipped.append(f"{sno_str} ({exc})")

    if skipped:
        print("以下場次略過：", ", ".join(skipped))

    main_mod.get_db = lambda: (
        lambda c: (setattr(c, "row_factory", sqlite3.Row), c)[1]
    )(sqlite3.connect(db_path, timeout=30))

    print(f"\n=== 正式庫測試算分現況 (成功同步 {synced} 場) ===")
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
    run_test_sync()