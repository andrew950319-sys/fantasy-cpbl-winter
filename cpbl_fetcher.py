import http.cookiejar
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

DB_PATH = "fantasy_league.db"
AWB_BASE_URL = "https://awb.cpbl.com.tw"
AWB_LIVE_URL = "https://awb.cpbl.com.tw/box/getlive"
AWB_KIND_CODES = {"P", "AWB", "WINTER"}
ALLOWED_WINTER_TEAMS = {"台灣海洋", "台灣山林"}


def _is_awb_kind(kind_code: str | int) -> bool:
    return str(kind_code or "").strip().upper() in AWB_KIND_CODES


def _to_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


def build_cpbl_opener() -> urllib.request.OpenerDirector:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        (
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ),
        ("Accept", "application/json, text/javascript, */*; q=0.01"),
        ("Accept-Language", "zh-TW,zh;q=0.9,en;q=0.8"),
        ("X-Requested-With", "XMLHttpRequest"),
    ]
    return opener


def _parse_awb_ip_from_row(row: dict[str, Any]) -> float:
    """解析冬盟局數（支援 InningPitchedCnt + InningPitchedDiv3Cnt 或 .1 / .2 小數局數）。"""
    if "InningPitchedCnt" in row or "InningPitchedDiv3Cnt" in row:
        ip_int = _to_int(row.get("InningPitchedCnt"))
        ip_div3 = _to_int(row.get("InningPitchedDiv3Cnt"))
        if ip_int > 0 or ip_div3 > 0:
            return round(ip_int + ip_div3 / 3.0, 4)

    raw_ip = (
        row.get("PitchingInningCnt")
        if row.get("PitchingInningCnt") is not None
        else row.get("IP")
    )
    s = str(raw_ip or "0").strip()
    if "." in s:
        whole_str, frac_str = s.split(".", 1)
        whole = _to_int(whole_str, 0)
        frac = frac_str[:1]
        if frac == "1":
            return round(whole + (1.0 / 3.0), 4)
        if frac == "2":
            return round(whole + (2.0 / 3.0), 4)
    return round(_to_float(s, 0.0), 4)


def _extract_awb_batting_counts(row: dict[str, Any]) -> dict[str, int]:
    """解析冬盟打者數據並做 AB / H 防呆。"""
    ab = _to_int(
        row.get("AtBatsCnt") if row.get("AtBatsCnt") is not None else row.get("HitCnt"),
        0,
    )

    h1 = _to_int(row.get("OneBaseHitCnt"), 0)
    h2 = _to_int(row.get("TwoBaseHitCnt"), 0)
    h3 = _to_int(row.get("ThreeBaseHitCnt"), 0)
    hr = _to_int(
        row.get("HomeRunCnt") if row.get("HomeRunCnt") is not None else row.get("HR"),
        0,
    )
    sum_hits = h1 + h2 + h3 + hr

    raw_h = (
        row.get("HittingCnt")
        if row.get("HittingCnt") is not None
        else row.get("HitCnt")
    )
    h = _to_int(raw_h, sum_hits)
    if sum_hits > 0 and (raw_h is None or str(raw_h).strip() == "" or h != sum_hits):
        if sum_hits <= ab:
            h = sum_hits

    bb = _to_int(
        row.get("BaseOnBallsCnt")
        if row.get("BaseOnBallsCnt") is not None
        else row.get("BasesONBallsCnt"),
        0,
    )
    hbp = _to_int(
        row.get("HitByPitchCnt")
        if row.get("HitByPitchCnt") is not None
        else row.get("HitBYPitchCnt"),
        0,
    )
    sf = _to_int(row.get("SacrificeFlyCnt"), 0)
    rbi = _to_int(
        row.get("RunsBattedInCnt")
        if row.get("RunsBattedInCnt") is not None
        else row.get("RunBattedINCnt"),
        0,
    )
    r = _to_int(
        row.get("RunScoredCnt")
        if row.get("RunScoredCnt") is not None
        else row.get("ScoreCnt"),
        0,
    )
    sb = _to_int(row.get("StealBaseOKCnt"), 0)
    tb = _to_int(
        row.get("TotalBasesCnt")
        if row.get("TotalBasesCnt") is not None
        else row.get("TotalBases"),
        0,
    )
    if tb == 0 and sum_hits > 0:
        tb = h1 + (h2 * 2) + (h3 * 3) + (hr * 4)

    return {
        "r": r,
        "ab": ab,
        "h": h,
        "bb": bb,
        "hbp": hbp,
        "sf": sf,
        "rbi": rbi,
        "sb": sb,
        "hr": hr,
        "tb": tb,
    }


def fetch_awb_box_live(
    year: str | int,
    kind_code: str,
    game_sno: str | int,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    """先取得冬盟頁面 Cookie 與防偽 Token，再 POST /box/getlive 抓比賽資料。"""
    if opener is None:
        opener = build_cpbl_opener()

    page_url = f"{AWB_BASE_URL}/box?year={year}&kindCode={kind_code}&gameSno={game_sno}"
    get_req = urllib.request.Request(page_url, method="GET")
    with opener.open(get_req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    token = m.group(1) if m else ""

    form_data: dict[str, str] = {
        "GameSno": str(game_sno),
        "Year": str(year),
        "KindCode": str(kind_code),
    }
    if token:
        form_data["__RequestVerificationToken"] = token

    payload = urllib.parse.urlencode(form_data).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": AWB_BASE_URL,
        "Referer": page_url,
        "X-Requested-With": "XMLHttpRequest",
    }
    if token:
        headers["RequestVerificationToken"] = token

    req = urllib.request.Request(
        AWB_LIVE_URL,
        data=payload,
        headers=headers,
        method="POST",
    )
    with opener.open(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)


def fetch_box_live(
    year: str | int,
    kind_code: str,
    game_sno: str | int,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    if _is_awb_kind(kind_code):
        return fetch_awb_box_live(year, kind_code, game_sno, opener=opener)

    if opener is None:
        opener = build_cpbl_opener()

    with opener.open("https://www.cpbl.com.tw/box", timeout=15) as resp:
        resp.read()

    payload = urllib.parse.urlencode(
        {
            "year": str(year),
            "kindCode": str(kind_code),
            "gameSno": str(game_sno),
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://www.cpbl.com.tw/box/getlive",
        data=payload,
        method="POST",
    )
    with opener.open(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)


def _parse_json_list(res_json: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = res_json.get(key) or "[]"
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return parsed if isinstance(parsed, list) else []


def extract_game_meta(res_json: dict[str, Any], fallback_year: str | int) -> dict[str, str]:
    game_date = f"{fallback_year}-01-01"
    start_time = ""
    away_team = ""
    home_team = ""

    for key in ("CurtGameDetailJson", "GameDetailJson"):
        items = _parse_json_list(res_json, key)
        if not items:
            continue
        d = items[0]
        gd = str(d.get("GameDate") or d.get("GAME_DATE") or "").strip()
        if len(gd) >= 10:
            game_date = gd[:10]

        st = str(
            d.get("GameTime")
            or d.get("StartTime")
            or d.get("PlayTime")
            or d.get("GAME_TIME")
            or ""
        ).strip()
        if st:
            if len(st) <= 8 and ":" in st:
                hh_mm_ss = st if len(st) == 8 else f"{st}:00"
                start_time = f"{game_date} {hh_mm_ss}"
            else:
                start_time = st.replace("T", " ")[:19]

        away_team = str(
            d.get("VisitingTeamName")
            or d.get("AwayTeamName")
            or d.get("VisitingClubName")
            or d.get("VISIT_TEAM_NAME")
            or ""
        ).strip()
        home_team = str(
            d.get("HomeTeamName")
            or d.get("HostTeamName")
            or d.get("HomeClubName")
            or d.get("HOME_TEAM_NAME")
            or ""
        ).strip()
        break

    return {
        "game_date": game_date,
        "start_time": start_time,
        "away_team": away_team,
        "home_team": home_team,
    }


def extract_game_date(res_json: dict[str, Any], fallback_year: str | int) -> str:
    return extract_game_meta(res_json, fallback_year)["game_date"]


def parse_game_stats(
    res_json: dict[str, Any],
    fallback_year: str | int,
    is_awb: bool = False,
) -> dict[str, dict[str, Any]]:
    meta = extract_game_meta(res_json, fallback_year)
    stat_date = meta["game_date"]
    bat_list = _parse_json_list(res_json, "BattingJson")
    pit_list = _parse_json_list(res_json, "PitchingJson")

    by_name: dict[str, dict[str, Any]] = {}

    def ensure_entry(name: str, team_name: str = "", role: str = "BAT") -> dict[str, Any]:
        if name not in by_name:
            by_name[name] = {
                "player_name": name,
                "team_name": team_name,
                "role": role,
                "positions": set(),
                "stat_date": stat_date,
                "r": 0,
                "ab": 0,
                "h": 0,
                "bb": 0,
                "hbp": 0,
                "sf": 0,
                "rbi": 0,
                "sb": 0,
                "hr": 0,
                "tb": 0,
                "w": 0,
                "ip": 0.0,
                "er": 0,
                "k": 0,
                "hld": 0,
                "sv": 0,
                "pitch_h": 0,
                "pitch_bb": 0,
            }
        if team_name and not by_name[name]["team_name"]:
            by_name[name]["team_name"] = team_name
        return by_name[name]

    for row in bat_list:
        name = str(
            row.get("HitterName")
            or row.get("PlayerName")
            or row.get("Name")
            or row.get("ChineseName")
            or ""
        ).strip()
        if not name:
            continue
        t_name = str(row.get("TeamName") or row.get("ClubName") or "").strip()
        entry = ensure_entry(name, t_name, "BAT")

        for pos_key in ("DefendPos", "Pos", "Position", "FieldPos"):
            raw_pos = str(row.get(pos_key) or "").upper()
            for p_code in ("C", "1B", "2B", "3B", "SS", "OF", "LF", "CF", "RF", "DH"):
                if p_code in raw_pos:
                    entry["positions"].add("OF" if p_code in ("LF", "CF", "RF") else p_code)

        if is_awb:
            b_stats = _extract_awb_batting_counts(row)
            entry["r"] += b_stats["r"]
            entry["ab"] += b_stats["ab"]
            entry["h"] += b_stats["h"]
            entry["bb"] += b_stats["bb"]
            entry["hbp"] += b_stats["hbp"]
            entry["sf"] += b_stats["sf"]
            entry["rbi"] += b_stats["rbi"]
            entry["sb"] += b_stats["sb"]
            entry["hr"] += b_stats["hr"]
            entry["tb"] += b_stats["tb"]
        else:
            entry["r"] += _to_int(row.get("ScoreCnt"))
            entry["ab"] += _to_int(row.get("HitCnt"))
            entry["h"] += _to_int(row.get("HittingCnt"))
            entry["bb"] += _to_int(row.get("BasesONBallsCnt"))
            entry["hbp"] += _to_int(row.get("HitBYPitchCnt"))
            entry["sf"] += _to_int(row.get("SacrificeFlyCnt"))
            entry["rbi"] += _to_int(row.get("RunBattedINCnt"))
            entry["sb"] += _to_int(row.get("StealBaseOKCnt"))
            entry["hr"] += _to_int(row.get("HomeRunCnt"))
            entry["tb"] += _to_int(row.get("TotalBases"))

    for row in pit_list:
        name = str(
            row.get("PitcherName")
            or row.get("PlayerName")
            or row.get("Name")
            or row.get("ChineseName")
            or ""
        ).strip()
        if not name:
            continue
        t_name = str(row.get("TeamName") or row.get("ClubName") or "").strip()
        entry = ensure_entry(name, t_name, "PIT")
        entry["positions"].add("P")

        if is_awb:
            total_ip = _parse_awb_ip_from_row(row)
        else:
            ip_int = _to_int(row.get("InningPitchedCnt"))
            ip_div3 = _to_int(row.get("InningPitchedDiv3Cnt"))
            total_ip = round(ip_int + ip_div3 / 3.0, 4)

        hld_val = _to_int(
            row.get("ReliefPointCnt")
            if row.get("ReliefPointCnt") is not None
            else row.get("HoldCnt")
        )
        sv_val = _to_int(
            row.get("SavePointCnt")
            if row.get("SavePointCnt") is not None
            else row.get("SaveOKCnt")
        )

        if total_ip >= 4.0 and sv_val == 0 and hld_val == 0:
            entry["positions"].add("SP")
        else:
            entry["positions"].add("RP")

        game_res = str(row.get("GameResult") or "").strip().upper()
        is_win = 1 if game_res in ("1", "W", "WIN", "勝") else 0

        entry["w"] += is_win
        entry["ip"] = round(entry["ip"] + total_ip, 4)
        entry["er"] += _to_int(row.get("EarnedRunCnt"))
        entry["k"] += _to_int(row.get("StrikeOutCnt"))
        entry["hld"] += hld_val
        entry["sv"] += sv_val
        entry["pitch_h"] += _to_int(row.get("HittingCnt"))
        entry["pitch_bb"] += _to_int(
            row.get("BasesONBallsCnt")
            if row.get("BasesONBallsCnt") is not None
            else row.get("BaseOnBallsCnt")
        )

    for item in by_name.values():
        item["positions"] = sorted(item["positions"])

    return by_name


def _ensure_tables_for_fetcher(cur: sqlite3.Cursor) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cpbl_games (
            game_key TEXT PRIMARY KEY,
            year TEXT NOT NULL,
            kind_code TEXT NOT NULL,
            game_sno TEXT NOT NULL,
            game_date TEXT NOT NULL,
            start_time TEXT DEFAULT '',
            away_team TEXT DEFAULT '',
            home_team TEXT DEFAULT '',
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_game_stats (
            player_id TEXT NOT NULL,
            stat_date TEXT NOT NULL,
            game_key TEXT NOT NULL,
            r INTEGER DEFAULT 0,
            ab INTEGER DEFAULT 0,
            h INTEGER DEFAULT 0,
            bb INTEGER DEFAULT 0,
            hbp INTEGER DEFAULT 0,
            sf INTEGER DEFAULT 0,
            rbi INTEGER DEFAULT 0,
            sb INTEGER DEFAULT 0,
            hr INTEGER DEFAULT 0,
            tb INTEGER DEFAULT 0,
            w INTEGER DEFAULT 0,
            ip REAL DEFAULT 0.0,
            er INTEGER DEFAULT 0,
            k INTEGER DEFAULT 0,
            hld INTEGER DEFAULT 0,
            sv INTEGER DEFAULT 0,
            pitch_h INTEGER DEFAULT 0,
            pitch_bb INTEGER DEFAULT 0,
            PRIMARY KEY (player_id, stat_date, game_key)
        )
    """)


def _sync_daily_stats_from_games(cur: sqlite3.Cursor, player_ids: list[str], dates: set[str]) -> None:
    if not player_ids or not dates:
        return
    for pid in player_ids:
        for d_str in dates:
            cur.execute("""
                SELECT
                    COALESCE(SUM(r), 0), COALESCE(SUM(ab), 0), COALESCE(SUM(h), 0),
                    COALESCE(SUM(bb), 0), COALESCE(SUM(hbp), 0), COALESCE(SUM(sf), 0),
                    COALESCE(SUM(rbi), 0), COALESCE(SUM(sb), 0), COALESCE(SUM(hr), 0),
                    COALESCE(SUM(tb), 0), COALESCE(SUM(w), 0), COALESCE(SUM(ip), 0.0),
                    COALESCE(SUM(er), 0), COALESCE(SUM(k), 0), COALESCE(SUM(hld), 0),
                    COALESCE(SUM(sv), 0), COALESCE(SUM(pitch_h), 0), COALESCE(SUM(pitch_bb), 0)
                FROM player_game_stats
                WHERE player_id = ? AND stat_date = ?
            """, (pid, d_str))
            agg = cur.fetchone()
            cur.execute("""
                INSERT OR REPLACE INTO player_daily_stats (
                    player_id, stat_date,
                    r, ab, h, bb, hbp, sf, rbi, sb, hr, tb,
                    w, ip, er, k, hld, sv, pitch_h, pitch_bb
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pid, d_str, *agg))


def save_parsed_stats(
    parsed_by_name: dict[str, dict[str, Any]],
    db_path: str = DB_PATH,
    game_meta: dict[str, str] | None = None,
    game_key: str = "default",
    auto_create_players: bool = True,
    id_prefix: str = "CPBL",
    is_awb: bool = False,
) -> dict[str, Any]:
    awb_mode = bool(is_awb or (game_meta and _is_awb_kind(game_meta.get("kind_code", ""))))

    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()
    _ensure_tables_for_fetcher(cur)

    cur.execute("PRAGMA table_info(players)")
    player_cols = {r[1] for r in cur.fetchall()}
    has_positions_col = "positions" in player_cols
    has_real_team_col = "real_team" in player_cols

    if has_real_team_col:
        cur.execute("SELECT player_id, name, real_team FROM players")
        name_to_id: dict[str, str] = {}
        name_to_team: dict[str, str] = {}
        for row in cur.fetchall():
            pid_val, pname_val, rteam_val = row[0], row[1], row[2]
            if pname_val:
                p_clean = str(pname_val).strip()
                if p_clean not in name_to_id:
                    name_to_id[p_clean] = pid_val
                if rteam_val and p_clean not in name_to_team:
                    name_to_team[p_clean] = str(rteam_val).strip()
    else:
        cur.execute("SELECT player_id, name FROM players")
        name_to_id = {str(row[1]).strip(): row[0] for row in cur.fetchall() if row[1]}
        name_to_team = {}

    # 冬盟模式僅保留「台灣海洋」與「台灣山林」球員，其餘四隊略過
    if awb_mode:
        filtered_parsed: dict[str, dict[str, Any]] = {}
        for name, stats in parsed_by_name.items():
            t_name = (stats.get("team_name") or "").strip() or name_to_team.get(name, "")
            if t_name in ALLOWED_WINTER_TEAMS:
                if not stats.get("team_name") and t_name:
                    stats = {**stats, "team_name": t_name}
                filtered_parsed[name] = stats
        parsed_by_name = filtered_parsed

    if game_meta:
        cur.execute("""
            INSERT OR REPLACE INTO cpbl_games (
                game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game_key,
            game_meta.get("year", ""),
            game_meta.get("kind_code", ""),
            game_meta.get("game_sno", ""),
            game_meta.get("game_date", ""),
            game_meta.get("start_time", ""),
            game_meta.get("away_team", ""),
            game_meta.get("home_team", ""),
            datetime.now().isoformat(timespec="seconds"),
        ))

    matched_rows: list[tuple[Any, ...]] = []
    matched_names: list[str] = []
    auto_created_names: list[str] = []
    unmatched_names: list[str] = []
    touched_pids: list[str] = []
    touched_dates: set[str] = set()
    stat_date = ""

    for name, stats in parsed_by_name.items():
        stat_date = stats["stat_date"]
        touched_dates.add(stat_date)
        pid = name_to_id.get(name)

        if not pid and auto_create_players:
            base_slug = f"{id_prefix}_{name}"
            pid = base_slug
            idx = 2
            while pid in name_to_id.values():
                pid = f"{base_slug}_{idx}"
                idx += 1

            pos_list = stats.get("positions") or (["P"] if stats.get("role") == "PIT" else ["DH"])
            pos_str = ",".join(pos_list)
            real_team = stats.get("team_name") or ""

            if has_positions_col:
                cur.execute("""
                    INSERT OR IGNORE INTO players (player_id, name, position, positions, real_team)
                    VALUES (?, ?, ?, ?, ?)
                """, (pid, name, pos_list[0], pos_str, real_team))
            else:
                cur.execute("""
                    INSERT OR IGNORE INTO players (player_id, name, position, real_team)
                    VALUES (?, ?, ?, ?)
                """, (pid, name, pos_list[0], real_team))

            name_to_id[name] = pid
            if real_team:
                name_to_team[name] = real_team
            auto_created_names.append(name)

        if not pid:
            unmatched_names.append(name)
            continue

        matched_names.append(name)
        touched_pids.append(pid)
        matched_rows.append(
            (
                pid,
                stats["stat_date"],
                game_key,
                stats["r"],
                stats["ab"],
                stats["h"],
                stats["bb"],
                stats["hbp"],
                stats["sf"],
                stats["rbi"],
                stats["sb"],
                stats["hr"],
                stats["tb"],
                stats["w"],
                stats["ip"],
                stats["er"],
                stats["k"],
                stats["hld"],
                stats["sv"],
                stats["pitch_h"],
                stats["pitch_bb"],
            )
        )

    for row in matched_rows:
        cur.execute(
            """
            INSERT OR REPLACE INTO player_game_stats (
                player_id, stat_date, game_key,
                r, ab, h, bb, hbp, sf, rbi, sb, hr, tb,
                w, ip, er, k, hld, sv, pitch_h, pitch_bb
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    _sync_daily_stats_from_games(cur, touched_pids, touched_dates)

    conn.commit()
    conn.close()

    return {
        "stat_date": stat_date,
        "game_meta": game_meta or {},
        "total_parsed": len(parsed_by_name),
        "imported_count": len(matched_rows),
        "auto_created_count": len(auto_created_names),
        "auto_created_names": auto_created_names,
        "matched_names": matched_names,
        "unmatched_names": unmatched_names,
    }


def import_cpbl_game(
    year: str | int,
    kind_code: str,
    game_sno: str | int,
    db_path: str = DB_PATH,
    auto_create_players: bool = True,
) -> dict[str, Any]:
    is_awb = _is_awb_kind(kind_code)
    res_json = fetch_box_live(year, kind_code, game_sno)
    meta = extract_game_meta(res_json, year)
    meta.update(
        {
            "year": str(year),
            "kind_code": str(kind_code),
            "game_sno": str(game_sno),
        }
    )
    game_key = f"{year}_{kind_code}_{game_sno}"
    parsed = parse_game_stats(res_json, year, is_awb=is_awb)
    result = save_parsed_stats(
        parsed,
        db_path=db_path,
        game_meta=meta,
        game_key=game_key,
        auto_create_players=auto_create_players,
        id_prefix="AWB" if is_awb else "CPBL",
        is_awb=is_awb,
    )
    result.update(
        {
            "year": str(year),
            "kind_code": str(kind_code),
            "game_sno": str(game_sno),
        }
    )
    return result


if __name__ == "__main__":
    summary = import_cpbl_game("2026", "A", "274", db_path=DB_PATH)
    print(json.dumps(summary, ensure_ascii=False, indent=2))