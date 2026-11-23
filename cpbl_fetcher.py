import http.cookiejar
import json
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

DB_PATH = "fantasy_league.db"
AWB_BASE_URL = "https://awb.cpbl.com.tw"
AWB_SCHEDULE_URL = "https://awb.cpbl.com.tw/schedule"
AWB_LIVE_URL = "https://awb.cpbl.com.tw/box/getlive"
AWB_KIND_CODES = {"P", "AWB", "WINTER"}
ALLOWED_WINTER_TEAMS = {"台灣海洋", "台灣山林"}
WINTER_SEASON_START = "2026-11-23"
WINTER_SEASON_END = "2026-12-03"


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


def _get_first_int(row: dict[str, Any], keys: tuple[str, ...], default: int = 0) -> int:
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for k in keys:
        if k in row and row[k] is not None and str(row[k]).strip() != "":
            return _to_int(row[k], default)
        lk = k.lower()
        if lk in lower_map and lower_map[lk] is not None and str(lower_map[lk]).strip() != "":
            return _to_int(lower_map[lk], default)
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
        ("Accept", "text/html,application/json, text/javascript, */*; q=0.01"),
        ("Accept-Language", "zh-TW,zh;q=0.9,en;q=0.8"),
        ("X-Requested-With", "XMLHttpRequest"),
    ]
    return opener


def _normalize_date_str(raw_date: str, fallback_year: str | int) -> str:
    s = (raw_date or "").strip()
    m_full = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m_full:
        y, mo, da = m_full.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(da):02d}"
    m_short = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", s)
    if m_short:
        mo, da = m_short.groups()
        yr = int(fallback_year)
        return f"{yr:04d}-{int(mo):02d}-{int(da):02d}"
    return ""


def _resolve_week_key_for_date(gdate: str) -> str:
    if "2026-11-23" <= gdate <= "2026-11-29":
        return "2026-11-23_2026-11-29"
    if "2026-11-30" <= gdate <= "2026-12-03":
        return "2026-11-30_2026-12-03"
    return ""


def _extract_games_from_text_or_json(
    content: str,
    year: str | int,
    kind_code: str,
) -> list[dict[str, str]]:
    games: list[dict[str, str]] = []
    seen_keys: set[str] = set()

    def add_game(
        sno: str,
        gdate: str,
        start_time: str = "",
        away_team: str = "",
        home_team: str = "",
    ) -> None:
        sno_clean = str(sno or "").strip()
        date_clean = _normalize_date_str(gdate, year)
        if not sno_clean or not date_clean:
            return
        if not (WINTER_SEASON_START <= date_clean <= WINTER_SEASON_END):
            return
        wkey = _resolve_week_key_for_date(date_clean)
        if not wkey:
            return
        gkey = f"{year}_{kind_code}_{sno_clean}"
        if gkey in seen_keys:
            return
        seen_keys.add(gkey)
        st_clean = str(start_time or "").strip()
        if st_clean and len(st_clean) <= 8 and ":" in st_clean:
            hh_mm_ss = st_clean if len(st_clean) == 8 else f"{st_clean}:00"
            st_clean = f"{date_clean} {hh_mm_ss}"
        games.append(
            {
                "game_key": gkey,
                "year": str(year),
                "kind_code": str(kind_code),
                "game_sno": sno_clean,
                "game_date": date_clean,
                "start_time": st_clean,
                "away_team": str(away_team or "").strip(),
                "home_team": str(home_team or "").strip(),
                "week_key": wkey,
            }
        )

    stripped = (content or "").strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            candidates: list[dict[str, Any]] = []
            if isinstance(data, list):
                candidates = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                for k in ("games", "Games", "data", "Data", "schedule", "Schedule", "list", "List"):
                    v = data.get(k)
                    if isinstance(v, list):
                        candidates.extend([x for x in v if isinstance(x, dict)])
                    elif isinstance(v, str) and v.strip().startswith("["):
                        try:
                            sub = json.loads(v)
                            if isinstance(sub, list):
                                candidates.extend([x for x in sub if isinstance(x, dict)])
                        except Exception:
                            pass
            for item in candidates:
                sno = (
                    item.get("GameSno")
                    or item.get("gameSno")
                    or item.get("GameSeqNo")
                    or item.get("Sno")
                    or item.get("game_sno")
                    or ""
                )
                gdate = (
                    item.get("GameDate")
                    or item.get("gameDate")
                    or item.get("Date")
                    or item.get("game_date")
                    or ""
                )
                stime = (
                    item.get("GameTime")
                    or item.get("StartTime")
                    or item.get("PlayTime")
                    or item.get("start_time")
                    or ""
                )
                away = (
                    item.get("VisitingTeamName")
                    or item.get("AwayTeamName")
                    or item.get("VisitingClubName")
                    or item.get("away_team")
                    or ""
                )
                home = (
                    item.get("HomeTeamName")
                    or item.get("HostTeamName")
                    or item.get("HomeClubName")
                    or item.get("home_team")
                    or ""
                )
                add_game(str(sno), str(gdate), str(stime), str(away), str(home))
            if games:
                return games
        except Exception:
            pass

    for m in re.finditer(r"(?:gameSno|GameSno)=([0-9]+)", content, flags=re.IGNORECASE):
        sno = m.group(1).strip()
        start_idx = max(0, m.start() - 500)
        end_idx = min(len(content), m.end() + 500)
        window_text = re.sub(r"<[^>]+>", " ", content[start_idx:end_idx])
        window_text = re.sub(r"\s+", " ", window_text).strip()

        gdate = _normalize_date_str(window_text, year)
        if not gdate:
            continue

        t_match = re.search(r"\b([012]?\d:[0-5]\d)\b", window_text)
        start_time = f"{gdate} {t_match.group(1)}:00" if t_match else ""

        away_team = ""
        home_team = ""
        for team_cand in ("台灣山林", "台灣海洋", "日職聯隊", "日本社會人", "韓職聯隊", "大學明星"):
            if team_cand in window_text:
                if not away_team:
                    away_team = team_cand
                elif team_cand != away_team and not home_team:
                    home_team = team_cand

        add_game(sno, gdate, start_time, away_team, home_team)

    return games


def fetch_awb_schedule(
    year: str | int = "2026",
    kind_code: str = "P",
    opener: urllib.request.OpenerDirector | None = None,
) -> list[dict[str, str]]:
    """抓取冬盟官網賽程（嘗試賽程頁與常見賽程 AJAX 端點，並過濾冬盟期間場次）。"""
    if opener is None:
        opener = build_cpbl_opener()

    candidate_urls = [
        f"{AWB_SCHEDULE_URL}?year={year}&kindCode={kind_code}",
        f"{AWB_BASE_URL}/schedule/getschedule?year={year}&kindCode={kind_code}",
        f"{AWB_BASE_URL}/schedule/getlist?year={year}&kindCode={kind_code}",
    ]

    all_games: dict[str, dict[str, str]] = {}
    for url in candidate_urls:
        try:
            req = urllib.request.Request(url, method="GET")
            with opener.open(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            extracted = _extract_games_from_text_or_json(body, year, kind_code)
            for g in extracted:
                all_games[g["game_key"]] = g
            if all_games:
                break
        except Exception:
            continue

    games = list(all_games.values())
    games.sort(key=lambda x: (x["game_date"], int(x["game_sno"]) if x["game_sno"].isdigit() else 0))
    return games


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
    """解析冬盟打者數據並做 AB / H / HBP 防呆。"""
    ab = _get_first_int(row, ("AtBatsCnt", "HitCnt", "AB"), 0)

    h1 = _get_first_int(row, ("OneBaseHitCnt", "SingleCnt", "1B"), 0)
    h2 = _get_first_int(row, ("TwoBaseHitCnt", "DoubleCnt", "2B"), 0)
    h3 = _get_first_int(row, ("ThreeBaseHitCnt", "TripleCnt", "3B"), 0)
    hr = _get_first_int(row, ("HomeRunCnt", "HR"), 0)
    sum_hits = h1 + h2 + h3 + hr

    raw_h = row.get("HittingCnt") if row.get("HittingCnt") is not None else row.get("HitCnt")
    h = _to_int(raw_h, sum_hits)
    if sum_hits > 0 and (raw_h is None or str(raw_h).strip() == "" or h != sum_hits):
        if sum_hits <= ab:
            h = sum_hits

    bb = _get_first_int(row, ("BaseOnBallsCnt", "BasesONBallsCnt", "BBCnt", "BB"), 0)
    hbp = _get_first_int(
        row,
        ("HitByPitchCnt", "HitBYPitchCnt", "HbpCnt", "DeadBallCnt", "HBPCnt", "HBP"),
        0,
    )
    sf = _get_first_int(row, ("SacrificeFlyCnt", "SFCnt", "SF"), 0)
    rbi = _get_first_int(row, ("RunsBattedInCnt", "RunBattedINCnt", "RBICnt", "RBI"), 0)
    r = _get_first_int(row, ("RunScoredCnt", "ScoreCnt", "RCnt", "R"), 0)
    sb = _get_first_int(row, ("StealBaseOKCnt", "SBCnt", "SB"), 0)
    tb = _get_first_int(row, ("TotalBasesCnt", "TotalBases", "TBCnt", "TB"), 0)
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
            entry["r"] += _get_first_int(row, ("ScoreCnt", "RunScoredCnt", "R"), 0)
            entry["ab"] += _get_first_int(row, ("HitCnt", "AtBatsCnt", "AB"), 0)
            entry["h"] += _get_first_int(row, ("HittingCnt", "H"), 0)
            entry["bb"] += _get_first_int(row, ("BasesONBallsCnt", "BaseOnBallsCnt", "BB"), 0)
            entry["hbp"] += _get_first_int(row, ("HitBYPitchCnt", "HitByPitchCnt", "HbpCnt", "DeadBallCnt", "HBP"), 0)
            entry["sf"] += _get_first_int(row, ("SacrificeFlyCnt", "SF"), 0)
            entry["rbi"] += _get_first_int(row, ("RunBattedINCnt", "RunsBattedInCnt", "RBI"), 0)
            entry["sb"] += _get_first_int(row, ("StealBaseOKCnt", "SB"), 0)
            entry["hr"] += _get_first_int(row, ("HomeRunCnt", "HR"), 0)
            entry["tb"] += _get_first_int(row, ("TotalBases", "TotalBasesCnt", "TB"), 0)

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
            week_key TEXT DEFAULT '',
            last_fetched_at TEXT DEFAULT '',
            updated_at TEXT
        )
    """)
    cur.execute("PRAGMA table_info(cpbl_games)")
    g_cols = {r[1] for r in cur.fetchall()}
    if "week_key" not in g_cols:
        cur.execute("ALTER TABLE cpbl_games ADD COLUMN week_key TEXT DEFAULT ''")
    if "last_fetched_at" not in g_cols:
        cur.execute("ALTER TABLE cpbl_games ADD COLUMN last_fetched_at TEXT DEFAULT ''")

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


def cleanup_stale_winter_games(db_path: str = DB_PATH) -> dict[str, Any]:
    """刪除冬盟賽季區間（2026-11-23 ~ 2026-12-03）之外或非數字場次編號的無關測試賽程與對應單場數據。"""
    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()
    _ensure_tables_for_fetcher(cur)

    cur.execute("""
        SELECT game_key, game_date, game_sno
        FROM cpbl_games
        WHERE game_date < ? OR game_date > ?
           OR TRIM(COALESCE(game_sno, '')) = ''
           OR game_sno GLOB '*[^0-9]*'
    """, (WINTER_SEASON_START, WINTER_SEASON_END))
    stale_rows = cur.fetchall()
    stale_keys = [r[0] for r in stale_rows]

    if stale_keys:
        placeholders = ",".join("?" for _ in stale_keys)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_game_stats'")
        if cur.fetchone():
            cur.execute(f"""
                SELECT DISTINCT player_id, stat_date
                FROM player_game_stats
                WHERE game_key IN ({placeholders})
            """, stale_keys)
            affected = cur.fetchall()
            cur.execute(f"DELETE FROM player_game_stats WHERE game_key IN ({placeholders})", stale_keys)

            for pid, d_str in affected:
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
                if any((x or 0) != 0 for x in agg):
                    cur.execute("""
                        INSERT OR REPLACE INTO player_daily_stats (
                            player_id, stat_date,
                            r, ab, h, bb, hbp, sf, rbi, sb, hr, tb,
                            w, ip, er, k, hld, sv, pitch_h, pitch_bb
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (pid, d_str, *agg))
                else:
                    cur.execute("DELETE FROM player_daily_stats WHERE player_id = ? AND stat_date = ?", (pid, d_str))

        cur.execute(f"DELETE FROM cpbl_games WHERE game_key IN ({placeholders})", stale_keys)

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "removed_games_count": len(stale_keys),
        "removed_game_keys": stale_keys,
    }


def _probe_next_awb_games(
    year: str | int,
    kind_code: str,
    existing_max_sno: int,
    reference_date_str: str,
    opener: urllib.request.OpenerDirector,
    probe_count: int = 4,
) -> list[dict[str, str]]:
    """當官網賽程頁未直接回傳時，試探後續場次編號是否有有效比賽資料並自動補登。"""
    discovered: list[dict[str, str]] = []
    try:
        base_dt = datetime.strptime(reference_date_str[:10], "%Y-%m-%d")
    except ValueError:
        base_dt = datetime.strptime(WINTER_SEASON_START, "%Y-%m-%d")

    for offset in range(1, probe_count + 1):
        cand_sno = existing_max_sno + offset
        cand_date = (base_dt + timedelta(days=offset)).strftime("%Y-%m-%d")
        if not (WINTER_SEASON_START <= cand_date <= WINTER_SEASON_END):
            continue
        wkey = _resolve_week_key_for_date(cand_date)
        if not wkey:
            continue
        try:
            res_json = fetch_awb_box_live(year, kind_code, cand_sno, opener=opener)
            meta = extract_game_meta(res_json, year)
            gdate = meta.get("game_date", "")
            if WINTER_SEASON_START <= gdate <= WINTER_SEASON_END:
                gkey = f"{year}_{kind_code}_{cand_sno}"
                discovered.append(
                    {
                        "game_key": gkey,
                        "year": str(year),
                        "kind_code": str(kind_code),
                        "game_sno": str(cand_sno),
                        "game_date": gdate,
                        "start_time": meta.get("start_time", ""),
                        "away_team": meta.get("away_team", ""),
                        "home_team": meta.get("home_team", ""),
                        "week_key": _resolve_week_key_for_date(gdate),
                    }
                )
        except Exception:
            continue

    return discovered


def sync_awb_schedule(
    year: str | int = "2026",
    kind_code: str = "P",
    db_path: str = DB_PATH,
) -> dict[str, Any]:
    """自動抓冬盟賽程並寫入 cpbl_games（保留既有 week_key 或依日期自動歸入第1/第2週；官網抓不到時亦會試探後續場次）。"""
    opener = build_cpbl_opener()
    fetched_games = fetch_awb_schedule(year=year, kind_code=kind_code, opener=opener)

    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()
    _ensure_tables_for_fetcher(cur)

    cur.execute("""
        SELECT game_sno, game_date
        FROM cpbl_games
        WHERE year = ? AND kind_code = ?
          AND game_sno GLOB '[0-9]*'
          AND game_date BETWEEN ? AND ?
    """, (str(year), str(kind_code), WINTER_SEASON_START, WINTER_SEASON_END))
    db_games = cur.fetchall()

    max_sno = 0
    latest_date = WINTER_SEASON_START
    for sno_val, d_val in db_games:
        if str(sno_val).isdigit():
            max_sno = max(max_sno, int(sno_val))
        if d_val and str(d_val) > latest_date:
            latest_date = str(d_val)[:10]

    for g in fetched_games:
        if str(g.get("game_sno", "")).isdigit():
            max_sno = max(max_sno, int(g["game_sno"]))
        if g.get("game_date") and g["game_date"] > latest_date:
            latest_date = g["game_date"]

    if max_sno > 0:
        probed = _probe_next_awb_games(
            year=year,
            kind_code=kind_code,
            existing_max_sno=max_sno,
            reference_date_str=latest_date,
            opener=opener,
            probe_count=3,
        )
        existing_keys = {x["game_key"] for x in fetched_games}
        for pg in probed:
            if pg["game_key"] not in existing_keys:
                fetched_games.append(pg)
                existing_keys.add(pg["game_key"])

    now_iso = datetime.now().isoformat(timespec="seconds")
    synced_count = 0
    week_matched_count = 0

    for g in fetched_games:
        wkey = g.get("week_key") or _resolve_week_key_for_date(g["game_date"])
        if not wkey:
            continue
        week_matched_count += 1
        cur.execute("""
            INSERT INTO cpbl_games (
                game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_key) DO UPDATE SET
                game_date = excluded.game_date,
                start_time = CASE WHEN excluded.start_time != '' THEN excluded.start_time ELSE cpbl_games.start_time END,
                away_team = CASE WHEN excluded.away_team != '' THEN excluded.away_team ELSE cpbl_games.away_team END,
                home_team = CASE WHEN excluded.home_team != '' THEN excluded.home_team ELSE cpbl_games.home_team END,
                week_key = CASE WHEN cpbl_games.week_key != '' THEN cpbl_games.week_key ELSE excluded.week_key END,
                updated_at = excluded.updated_at
        """, (
            g["game_key"],
            g["year"],
            g["kind_code"],
            g["game_sno"],
            g["game_date"],
            g["start_time"],
            g["away_team"],
            g["home_team"],
            wkey,
            now_iso,
        ))
        synced_count += 1

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "synced_count": synced_count,
        "week_matched_count": week_matched_count,
        "games": fetched_games,
    }


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
        wkey = _resolve_week_key_for_date(game_meta.get("game_date", ""))
        cur.execute("""
            INSERT INTO cpbl_games (
                game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_key) DO UPDATE SET
                year = excluded.year,
                kind_code = excluded.kind_code,
                game_sno = excluded.game_sno,
                game_date = excluded.game_date,
                start_time = CASE WHEN excluded.start_time != '' THEN excluded.start_time ELSE cpbl_games.start_time END,
                away_team = CASE WHEN excluded.away_team != '' THEN excluded.away_team ELSE cpbl_games.away_team END,
                home_team = CASE WHEN excluded.home_team != '' THEN excluded.home_team ELSE cpbl_games.home_team END,
                week_key = CASE WHEN cpbl_games.week_key != '' THEN cpbl_games.week_key ELSE excluded.week_key END,
                updated_at = excluded.updated_at
        """, (
            game_key,
            game_meta.get("year", ""),
            game_meta.get("kind_code", ""),
            game_meta.get("game_sno", ""),
            game_meta.get("game_date", ""),
            game_meta.get("start_time", ""),
            game_meta.get("away_team", ""),
            game_meta.get("home_team", ""),
            wkey,
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