import re
import sqlite3
import sys
from datetime import datetime
import cpbl_fetcher as cf
import main as main_mod

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROD_DB = "fantasy_league.db"
YEAR = "2026"
KIND_CODE = "P"
START_SNO = 16
END_SNO = 120
ALLOWED_TEAMS = {"台灣海洋", "台灣山林"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_game_date(g_date: str, year: str) -> bool:
    s = (g_date or "").strip()
    if not DATE_RE.match(s):
        return False
    if s == f"{year}-01-01":
        return False
    return True


def _load_allowed_roster_names(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT name
            FROM players
            WHERE name IS NOT NULL
              AND TRIM(name) != ''
              AND real_team IN ('台灣海洋', '台灣山林')
            """
        ).fetchall()
        return {str(r[0]).strip() for r in rows}
    finally:
        conn.close()


def sync_all_past_games(
    up_to_date: str | None = None,
    start_sno: int = START_SNO,
    end_sno: int = END_SNO,
    db_path: str = PROD_DB,
) -> None:
    if not up_to_date:
        up_to_date = datetime.now().strftime("%Y-%m-%d")

    allowed_names = _load_allowed_roster_names(db_path)
    opener = cf.build_cpbl_opener()
    matched: list[tuple[str, dict, dict]] = []

    for sno in range(start_sno, end_sno + 1):
        sno_str = str(sno)
        try:
            res_json = cf.fetch_box_live(YEAR, KIND_CODE, sno_str, opener=opener)
        except Exception:
            continue

        meta = cf.extract_game_meta(res_json, YEAR)
        g_date = meta.get("game_date", "")
        if not _is_valid_game_date(g_date, YEAR):
            continue
        if g_date > up_to_date:
            break
        if g_date <= up_to_date:
            matched.append((sno_str, res_json, meta))

    synced_count = 0
    for sno_str, res_json, meta in matched:
        parsed = cf.parse_game_stats(res_json, YEAR, is_awb=True)
        filtered_parsed = {
            name: stats
            for name, stats in parsed.items()
            if (
                (stats.get("team_name") or "").strip() in ALLOWED_TEAMS
                or name.strip() in allowed_names
            )
        }
        if not filtered_parsed:
            continue

        meta_to_save = dict(meta)
        meta_to_save.update({"year": YEAR, "kind_code": KIND_CODE, "game_sno": sno_str})

        res = cf.save_parsed_stats(
            filtered_parsed,
            db_path=db_path,
            game_meta=meta_to_save,
            game_key=f"{YEAR}_{KIND_CODE}_{sno_str}",
            auto_create_players=False,
            id_prefix="AWB",
            is_awb=True,
        )
        synced_count += 1
        print(
            f"[自動同步 {YEAR}_{KIND_CODE}_{sno_str}] 日期={res.get('stat_date')} | "
            f"{meta_to_save.get('away_team', '')} vs {meta_to_save.get('home_team', '')} | "
            f"匯入={res.get('imported_count')}/{res.get('total_parsed')}"
        )

    if synced_count == 0:
        print(f"[截至 {up_to_date}] 尚無符合台灣兩隊的冬盟比賽資料。")
        return

    main_mod.get_db = lambda: (
        lambda c: (setattr(c, "row_factory", sqlite3.Row), c)[1]
    )(sqlite3.connect(db_path, timeout=30))

    print(f"\n=== [截至 {up_to_date}] 最新 12 項算分 (ALL) ===")
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
    sync_all_past_games()