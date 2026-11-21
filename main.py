from datetime import date, datetime
import os
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from cpbl_fetcher import import_cpbl_game

app = FastAPI()
templates = Jinja2Templates(directory="templates")

ENABLE_ADMIN = os.environ.get("ENABLE_ADMIN", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def ensure_admin_enabled():
    if not ENABLE_ADMIN:
        return JSONResponse(
            {"ok": False, "error": "此功能僅限本機後台使用"},
            status_code=403,
        )
    return None


TEAMS = ["樂天桃猿", "中信兄弟", "味全龍", "富邦悍將"]
TEST_TEAMS = ["台灣山林", "台灣海洋"]
ALL_AVAILABLE_TEAMS = TEAMS + TEST_TEAMS

SCHEDULE_WEEKS = [
    {
        "key": "2026-11-23_2026-11-29",
        "label": "第1週（11/23～11/29）",
        "start_date": "2026-11-23",
        "end_date": "2026-11-29",
    },
    {
        "key": "2026-11-30_2026-12-03",
        "label": "第2週（11/30～12/3）",
        "start_date": "2026-11-30",
        "end_date": "2026-12-03",
    },
]
SCHEDULE_WEEK_MAP = {w["key"]: w for w in SCHEDULE_WEEKS}

TEAM_CODES = {
    "樂天桃猿": "rakuten",
    "中信兄弟": "brothers",
    "味全龍": "dragons",
    "富邦悍將": "guardians",
    "台灣山林": "shanhai_mtn",
    "台灣海洋": "shanhai_sea",
}
CODE_TO_TEAM = {v: k for k, v in TEAM_CODES.items()}
TEAM_PASSWORDS = {
    "樂天桃猿": "1111",
    "中信兄弟": "2222",
    "味全龍": "3333",
    "富邦悍將": "4444",
    "台灣山林": "0000",
    "台灣海洋": "0000",
}

VALID_SLOTS = {"ACTIVE", "MINORS", "IL", "SPECIAL", "RELEASED"}
LOCKED_10_DAYS_SLOTS = {"MINORS", "IL"}
LOCK_DAYS = 10

LINEUP_SLOTS_PITCHER_STARTER = ["SP1", "SP2", "SP3"]
LINEUP_SLOTS_PITCHER_GENERAL = ["P1", "P2", "P3"]
LINEUP_SLOTS_CLOSER = ["CP1", "CP2", "CP3"]
LINEUP_SLOTS_DH = ["DH1", "DH2"]
LINEUP_SLOTS_INFIELD = ["C", "1B", "2B", "3B", "SS"]
LINEUP_SLOTS_OUTFIELD = ["OF1", "OF2", "OF3"]

LINEUP_SLOTS_ORDERED = (
    LINEUP_SLOTS_PITCHER_STARTER
    + LINEUP_SLOTS_PITCHER_GENERAL
    + LINEUP_SLOTS_CLOSER
    + LINEUP_SLOTS_DH
    + LINEUP_SLOTS_INFIELD
    + LINEUP_SLOTS_OUTFIELD
)
LINEUP_SLOTS_BATTER = LINEUP_SLOTS_DH + LINEUP_SLOTS_INFIELD + LINEUP_SLOTS_OUTFIELD
LINEUP_SLOTS_PITCHER = LINEUP_SLOTS_PITCHER_STARTER + LINEUP_SLOTS_PITCHER_GENERAL + LINEUP_SLOTS_CLOSER

SLOT_LABELS = {
    "BN": "BN",
    "BENCH": "BN",
    "SP1": "SP1",
    "SP2": "SP2",
    "SP3": "SP3",
    "P1": "P1",
    "P2": "P2",
    "P3": "P3",
    "CP1": "CP1",
    "CP2": "CP2",
    "CP3": "CP3",
    "DH1": "DH",
    "DH2": "DH",
    "DH": "DH",
    "C": "C",
    "1B": "1B",
    "2B": "2B",
    "3B": "3B",
    "SS": "SS",
    "OF1": "OF",
    "OF2": "OF",
    "OF3": "OF",
    "OF": "OF",
}

SLOT_ORDER_MAP = {slot: idx for idx, slot in enumerate(LINEUP_SLOTS_ORDERED)}
SLOT_ORDER_MAP["BN"] = len(LINEUP_SLOTS_ORDERED)
SLOT_ORDER_MAP["BENCH"] = len(LINEUP_SLOTS_ORDERED)

SCORING_LINEUP_SLOTS = set(LINEUP_SLOTS_ORDERED)
BENCH_SLOTS = {"BN", "BENCH"}
ALL_ACTIVE_LINEUP_SLOTS = SCORING_LINEUP_SLOTS | BENCH_SLOTS

MAX_ACTIVE_ROSTER = 25
MAX_TOTAL_ROSTER = 60

IL_REASONS = [
    "現實受傷／復健中",
    "下二軍休養觀察",
    "傷停／未隨隊出賽",
    "其他傷病判定",
]

SPECIAL_REASONS = [
    "洋將下二軍調整",
    "洋將名額輪替",
    "兵役／徵召／特殊狀況",
    "其他特殊判定",
]


class LoginPayload(BaseModel):
    team: str
    password: str


class LineupMovePayload(BaseModel):
    player_id: str
    roster_slot: str
    lineup_slot: str = "BN"
    reason: str = ""
    force_override: bool = False


class SignFreeAgentPayload(BaseModel):
    player_id: str
    roster_slot: str = "ACTIVE"
    lineup_slot: str = "BN"
    force_override: bool = False


class PlayerAdminPayload(BaseModel):
    player_id: str | None = None
    name: str
    real_team: str = ""
    fantasy_team: str = ""
    positions: list[str] = []
    roster_slot: str = "ACTIVE"
    lineup_slot: str = "BN"


class CpblImportPayload(BaseModel):
    year: str = "2026"
    kind_code: str = "A"
    game_sno: str
    stat_date: str = ""
    week_key: str = ""


class ScheduleEntryPayload(BaseModel):
    week_key: str = "2026-11-23_2026-11-29"
    game_date: str
    start_time: str = ""
    away_team: str = "台灣山林"
    home_team: str = "台灣海洋"
    year: str = "2026"
    kind_code: str = "A"
    game_sno: str = ""


class SeedWinterPayload(BaseModel):
    team: str = "台灣海洋"
    count: int = 25
    with_stats: bool = True
    stat_date: str = ""


def get_db():
    conn = sqlite3.connect("fantasy_league.db")
    conn.row_factory = sqlite3.Row
    return conn


def normalize_lineup_slot(slot: str | None) -> str:
    s = (slot or "BN").strip().upper()
    if s in {"", "BENCH"}:
        return "BN"
    return s


def format_slot_label(slot: str | None) -> str:
    s = normalize_lineup_slot(slot)
    return SLOT_LABELS.get(s, s)


def ensure_roster_columns(cursor):
    cursor.execute("PRAGMA table_info(team_rosters)")
    existing_cols = {r["name"] for r in cursor.fetchall()}
    if "slot_changed_at" not in existing_cols:
        cursor.execute("ALTER TABLE team_rosters ADD COLUMN slot_changed_at TEXT")
    if "status_reason" not in existing_cols:
        cursor.execute("ALTER TABLE team_rosters ADD COLUMN status_reason TEXT DEFAULT ''")
    if "fantasy_team" not in existing_cols:
        cursor.execute("ALTER TABLE team_rosters ADD COLUMN fantasy_team TEXT DEFAULT ''")

    now_iso = datetime.now().isoformat(timespec="seconds")
    cursor.execute("""
        UPDATE team_rosters
        SET slot_changed_at = ?
        WHERE slot_changed_at IS NULL OR slot_changed_at = ''
    """, (now_iso,))


def ensure_extended_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            player_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            real_team TEXT DEFAULT '',
            position TEXT DEFAULT '',
            positions TEXT DEFAULT ''
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS team_rosters (
            player_id TEXT PRIMARY KEY,
            fantasy_team TEXT DEFAULT '',
            roster_slot TEXT NOT NULL DEFAULT 'ACTIVE',
            lineup_slot TEXT NOT NULL DEFAULT 'BN',
            slot_changed_at TEXT,
            status_reason TEXT DEFAULT ''
        )
    """)

    ensure_roster_columns(cursor)

    cursor.execute("PRAGMA table_info(team_rosters)")
    r_cols = {r["name"] for r in cursor.fetchall()}
    if "lineup_slot" not in r_cols:
        cursor.execute("ALTER TABLE team_rosters ADD COLUMN lineup_slot TEXT DEFAULT 'BN'")

    cursor.execute("PRAGMA table_info(players)")
    p_cols = {r["name"] for r in cursor.fetchall()}
    if "positions" not in p_cols:
        cursor.execute("ALTER TABLE players ADD COLUMN positions TEXT DEFAULT ''")
    if "real_team" not in p_cols:
        cursor.execute("ALTER TABLE players ADD COLUMN real_team TEXT DEFAULT ''")

    cursor.execute("""
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
            updated_at TEXT
        )
    """)
    cursor.execute("PRAGMA table_info(cpbl_games)")
    g_cols = {r["name"] for r in cursor.fetchall()}
    if "week_key" not in g_cols:
        cursor.execute("ALTER TABLE cpbl_games ADD COLUMN week_key TEXT DEFAULT ''")

    cursor.execute("""
        UPDATE team_rosters
        SET lineup_slot = 'BN'
        WHERE roster_slot = 'ACTIVE' AND (lineup_slot IS NULL OR lineup_slot = '' OR UPPER(lineup_slot) = 'BENCH')
    """)


def clear_free_agent_players():
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            SELECT player_id FROM team_rosters
            WHERE roster_slot = 'RELEASED' OR fantasy_team IS NULL OR TRIM(fantasy_team) = ''
        """)
        fa_pids = [r["player_id"] for r in cursor.fetchall()]
        if fa_pids:
            placeholders = ",".join("?" for _ in fa_pids)
            cursor.execute(f"DELETE FROM player_daily_stats WHERE player_id IN ({placeholders})", fa_pids)
            cursor.execute(f"DELETE FROM team_rosters WHERE player_id IN ({placeholders})", fa_pids)
            cursor.execute(f"DELETE FROM players WHERE player_id IN ({placeholders})", fa_pids)
            conn.commit()
    finally:
        conn.close()


def init_stats_and_sample_data():
    conn = get_db()
    cursor = conn.cursor()
    ensure_extended_schema(cursor)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS player_daily_stats (
            player_id TEXT,
            stat_date TEXT,
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
            PRIMARY KEY (player_id, stat_date)
        )
    """)

    conn.commit()
    conn.close()


init_stats_and_sample_data()


def format_rate_3(val: float) -> str:
    s = f"{val:.3f}"
    return s[1:] if s.startswith("0.") else s


def compute_derived_metrics(row: dict) -> dict:
    ab = row.get("ab", 0) or 0
    h = row.get("h", 0) or 0
    bb = row.get("bb", 0) or 0
    hbp = row.get("hbp", 0) or 0
    sf = row.get("sf", 0) or 0
    tb = row.get("tb", 0) or 0

    ip = float(row.get("ip", 0.0) or 0.0)
    er = row.get("er", 0) or 0
    pitch_h = row.get("pitch_h", 0) or 0
    pitch_bb = row.get("pitch_bb", 0) or 0

    avg = (h / ab) if ab > 0 else 0.0
    obp_denom = ab + bb + hbp + sf
    obp = ((h + bb + hbp) / obp_denom) if obp_denom > 0 else 0.0
    slg = (tb / ab) if ab > 0 else 0.0
    ops = obp + slg

    era = ((er * 9) / ip) if ip > 0 else 0.0
    whip = ((pitch_h + pitch_bb) / ip) if ip > 0 else 0.0

    return {
        "r": {"val": row.get("r", 0) or 0, "disp": str(row.get("r", 0) or 0), "lower_better": False},
        "avg": {"val": avg, "disp": format_rate_3(avg), "lower_better": False},
        "rbi": {"val": row.get("rbi", 0) or 0, "disp": str(row.get("rbi", 0) or 0), "lower_better": False},
        "sb": {"val": row.get("sb", 0) or 0, "disp": str(row.get("sb", 0) or 0), "lower_better": False},
        "hr": {"val": row.get("hr", 0) or 0, "disp": str(row.get("hr", 0) or 0), "lower_better": False},
        "ops": {"val": ops, "disp": format_rate_3(ops), "lower_better": False},
        "w": {"val": row.get("w", 0) or 0, "disp": str(row.get("w", 0) or 0), "lower_better": False},
        "era": {"val": era, "disp": f"{era:.2f}", "lower_better": True},
        "k": {"val": row.get("k", 0) or 0, "disp": str(row.get("k", 0) or 0), "lower_better": False},
        "hld": {"val": row.get("hld", 0) or 0, "disp": str(row.get("hld", 0) or 0), "lower_better": False},
        "sv": {"val": row.get("sv", 0) or 0, "disp": str(row.get("sv", 0) or 0), "lower_better": False},
        "whip": {"val": whip, "disp": f"{whip:.2f}", "lower_better": True},
        "ip_disp": f"{ip:.1f}",
    }


def calc_lock_info(slot: str, changed_at_str: str | None) -> dict:
    if slot not in LOCKED_10_DAYS_SLOTS or not changed_at_str:
        return {"is_locked": False, "days_elapsed": 0, "days_remaining": 0, "changed_date": ""}
    try:
        changed_dt = datetime.fromisoformat(changed_at_str)
    except ValueError:
        return {"is_locked": False, "days_elapsed": 0, "days_remaining": 0, "changed_date": ""}

    elapsed_seconds = max(0.0, (datetime.now() - changed_dt).total_seconds())
    days_elapsed = int(elapsed_seconds // 86400)
    days_remaining = max(0, LOCK_DAYS - days_elapsed)
    return {
        "is_locked": days_remaining > 0,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "changed_date": changed_dt.strftime("%Y-%m-%d"),
    }


def evaluate_ruling_status(slot: str, reason: str) -> tuple[str, str]:
    clean_reason = (reason or "").strip()
    if slot in {"IL", "SPECIAL"}:
        if clean_reason:
            return "verified", f"已判定：{clean_reason}"
        return "pending", "待確認判定（建議補註原因）"
    return "normal", clean_reason


def parse_player_positions(pos_str: str | None, fallback_pos: str | None) -> set[str]:
    raw = (pos_str or "").strip() or (fallback_pos or "").strip()
    tokens = {
        t.strip().upper()
        for t in raw.replace("/", ",").replace("、", ",").split(",")
        if t.strip()
    }
    normalized = set()
    for t in tokens:
        if t in {"LF", "CF", "RF"}:
            normalized.add("OF")
        elif t in {"投", "CL"}:
            normalized.add("CP" if t == "CL" else "P")
        else:
            normalized.add(t)
    return normalized


def is_pitcher_positions(positions: set[str]) -> bool:
    return bool(positions & {"P", "SP", "RP", "CP"})


def is_lineup_slot_eligible(lineup_slot: str, positions: set[str]) -> tuple[bool, str]:
    slot = normalize_lineup_slot(lineup_slot)
    if slot == "BN":
        return True, ""
    if slot not in SCORING_LINEUP_SLOTS:
        return False, f"無效的先發槽位：{format_slot_label(slot)}"

    is_pit = is_pitcher_positions(positions)
    if slot.startswith("DH"):
        return True, ""

    if slot in LINEUP_SLOTS_INFIELD + LINEUP_SLOTS_OUTFIELD:
        label = format_slot_label(slot)
        if is_pit and not (positions - {"P", "SP", "RP", "CP"}):
            return False, "投手不能放進野手先發槽位"
        if label not in positions:
            return False, f"球員無 {label} 守位資格（目前守位：{', '.join(sorted(positions)) or '無'}）"
        return True, ""

    if not is_pit:
        return False, "非投手不能放進投手槽位"
    if slot.startswith("SP"):
        if positions & {"SP", "P"}:
            return True, ""
        return False, "此投手無 SP 資格"
    if slot.startswith("CP"):
        if positions & {"CP", "RP", "P"}:
            return True, ""
        return False, "此投手無 CP 資格"
    if slot.startswith("P"):
        return True, ""

    return False, "不符合槽位資格"


def is_game_locked_for_team(cursor, real_team: str | None, target_date: str | None = None) -> tuple[bool, str]:
    if not real_team:
        return False, ""
    check_date = target_date or date.today().isoformat()
    cursor.execute("""
        SELECT start_time, away_team, home_team
        FROM cpbl_games
        WHERE game_date = ? AND (away_team = ? OR home_team = ?)
        ORDER BY start_time ASC
    """, (check_date, real_team, real_team))
    rows = cursor.fetchall()
    now_dt = datetime.now()
    for r in rows:
        st_str = (r["start_time"] or "").strip()
        if not st_str:
            continue
        try:
            st_dt = datetime.fromisoformat(st_str)
            if now_dt >= st_dt:
                return True, f"現實球隊【{real_team}】今日賽事已於 {st_dt.strftime('%H:%M')} 開賽，該隊出賽球員已鎖定無法移動"
        except ValueError:
            continue
    return False, ""


def validate_roster_limits(
    cursor,
    fantasy_team: str,
    moving_player_id: str,
    new_roster_slot: str,
    new_lineup_slot: str,
) -> tuple[bool, str]:
    cursor.execute("""
        SELECT r.player_id, r.roster_slot, r.lineup_slot
        FROM team_rosters r
        WHERE r.fantasy_team = ? AND r.roster_slot != 'RELEASED'
    """, (fantasy_team,))
    rows = [dict(r) for r in cursor.fetchall()]

    sim_map = {
        r["player_id"]: {
            "roster_slot": r["roster_slot"],
            "lineup_slot": normalize_lineup_slot(r["lineup_slot"]),
        }
        for r in rows
    }
    if new_roster_slot == "RELEASED":
        sim_map.pop(moving_player_id, None)
    else:
        sim_map[moving_player_id] = {
            "roster_slot": new_roster_slot,
            "lineup_slot": normalize_lineup_slot(new_lineup_slot) if new_roster_slot == "ACTIVE" else "BN",
        }

    total_count = len(sim_map)
    if total_count > MAX_TOTAL_ROSTER:
        return False, f"全隊總人數已達上限 {MAX_TOTAL_ROSTER} 人（目前 {total_count} 人）"

    active_items = [v for v in sim_map.values() if v["roster_slot"] == "ACTIVE"]
    if len(active_items) > MAX_ACTIVE_ROSTER:
        return False, f"一軍名單已達上限 {MAX_ACTIVE_ROSTER} 人（目前 {len(active_items)} 人），請先下放二軍或移入傷兵"

    used_scoring_slots = [
        v["lineup_slot"]
        for v in active_items
        if normalize_lineup_slot(v.get("lineup_slot")) in SCORING_LINEUP_SLOTS
    ]
    if len(used_scoring_slots) != len(set(used_scoring_slots)):
        return False, "該先發守位槽位已被其他球員佔用，請先更換"

    return True, ""


def get_available_dates() -> list[str]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT stat_date FROM player_daily_stats ORDER BY stat_date DESC")
    dates = [r["stat_date"] for r in cursor.fetchall()]
    conn.close()
    return dates


def get_schedule_games(week_key: str | None = None) -> list[dict]:
    conn = get_db()
    cursor = conn.cursor()
    ensure_extended_schema(cursor)
    if week_key and week_key in SCHEDULE_WEEK_MAP:
        w = SCHEDULE_WEEK_MAP[week_key]
        cursor.execute("""
            SELECT game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key
            FROM cpbl_games
            WHERE week_key = ? OR (game_date >= ? AND game_date <= ?)
            ORDER BY game_date ASC, game_sno ASC
        """, (week_key, w["start_date"], w["end_date"]))
    else:
        cursor.execute("""
            SELECT game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key
            FROM cpbl_games
            ORDER BY game_date ASC, game_sno ASC
        """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def calc_team_12_cats(fantasy_team: str, date_filter: str = "ALL", week_key: str = "ALL"):
    conn = get_db()
    cursor = conn.cursor()
    ensure_extended_schema(cursor)

    cursor.execute("""
        SELECT p.player_id, r.roster_slot, r.lineup_slot
        FROM players p
        JOIN team_rosters r ON p.player_id = r.player_id
        WHERE TRIM(COALESCE(r.fantasy_team, '')) = ?
          AND UPPER(TRIM(COALESCE(r.roster_slot, ''))) = 'ACTIVE'
    """, ((fantasy_team or "").strip(),))

    scoring_pids = [
        row["player_id"]
        for row in cursor.fetchall()
        if normalize_lineup_slot(row["lineup_slot"]) in SCORING_LINEUP_SLOTS
    ]

    agg = {
        "r": 0, "ab": 0, "h": 0, "bb": 0, "hbp": 0, "sf": 0,
        "rbi": 0, "sb": 0, "hr": 0, "tb": 0,
        "w": 0, "ip": 0.0, "er": 0, "k": 0, "hld": 0, "sv": 0,
        "pitch_h": 0, "pitch_bb": 0,
    }

    if scoring_pids:
        pid_placeholders = ",".join("?" for _ in scoring_pids)
        sql = f"""
            SELECT
                COALESCE(SUM(r), 0) AS r,
                COALESCE(SUM(ab), 0) AS ab,
                COALESCE(SUM(h), 0) AS h,
                COALESCE(SUM(bb), 0) AS bb,
                COALESCE(SUM(hbp), 0) AS hbp,
                COALESCE(SUM(sf), 0) AS sf,
                COALESCE(SUM(rbi), 0) AS rbi,
                COALESCE(SUM(sb), 0) AS sb,
                COALESCE(SUM(hr), 0) AS hr,
                COALESCE(SUM(tb), 0) AS tb,
                COALESCE(SUM(w), 0) AS w,
                COALESCE(SUM(ip), 0.0) AS ip,
                COALESCE(SUM(er), 0) AS er,
                COALESCE(SUM(k), 0) AS k,
                COALESCE(SUM(hld), 0) AS hld,
                COALESCE(SUM(sv), 0) AS sv,
                COALESCE(SUM(pitch_h), 0) AS pitch_h,
                COALESCE(SUM(pitch_bb), 0) AS pitch_bb
            FROM player_daily_stats
            WHERE player_id IN ({pid_placeholders})
        """
        params: list = list(scoring_pids)
        if date_filter and date_filter != "ALL":
            sql += " AND stat_date = ?"
            params.append(date_filter)
        elif week_key and week_key in SCHEDULE_WEEK_MAP:
            w = SCHEDULE_WEEK_MAP[week_key]
            sql += " AND stat_date >= ? AND stat_date <= ?"
            params.extend([w["start_date"], w["end_date"]])

        cursor.execute(sql, params)
        fetched = cursor.fetchone()
        if fetched:
            for k in agg:
                agg[k] = fetched[k] or 0

    conn.close()
    return compute_derived_metrics(agg)


def _query_players_block(
    cursor,
    where_sql: str,
    where_params: list,
    date_filter: str = "ALL",
    week_key: str = "ALL",
) -> list[dict]:
    params = []
    date_clause = ""
    if date_filter and date_filter != "ALL":
        date_clause = " AND s.stat_date = ?"
        params.append(date_filter)
    elif week_key and week_key in SCHEDULE_WEEK_MAP:
        w = SCHEDULE_WEEK_MAP[week_key]
        date_clause = " AND s.stat_date >= ? AND s.stat_date <= ?"
        params.extend([w["start_date"], w["end_date"]])
    params.extend(where_params)

    cursor.execute(f"""
        SELECT
            p.player_id,
            p.name,
            p.real_team,
            p.position,
            p.positions,
            r.fantasy_team,
            r.roster_slot,
            r.lineup_slot,
            r.slot_changed_at,
            r.status_reason,
            COALESCE(SUM(s.r), 0) AS r,
            COALESCE(SUM(s.ab), 0) AS ab,
            COALESCE(SUM(s.h), 0) AS h,
            COALESCE(SUM(s.bb), 0) AS bb,
            COALESCE(SUM(s.hbp), 0) AS hbp,
            COALESCE(SUM(s.sf), 0) AS sf,
            COALESCE(SUM(s.rbi), 0) AS rbi,
            COALESCE(SUM(s.sb), 0) AS sb,
            COALESCE(SUM(s.hr), 0) AS hr,
            COALESCE(SUM(s.tb), 0) AS tb,
            COALESCE(SUM(s.w), 0) AS w,
            COALESCE(SUM(s.ip), 0.0) AS ip,
            COALESCE(SUM(s.er), 0) AS er,
            COALESCE(SUM(s.k), 0) AS k,
            COALESCE(SUM(s.hld), 0) AS hld,
            COALESCE(SUM(s.sv), 0) AS sv,
            COALESCE(SUM(s.pitch_h), 0) AS pitch_h,
            COALESCE(SUM(s.pitch_bb), 0) AS pitch_bb
        FROM players p
        JOIN team_rosters r ON p.player_id = r.player_id
        LEFT JOIN player_daily_stats s ON p.player_id = s.player_id {date_clause}
        WHERE {where_sql}
        GROUP BY p.player_id, p.name, p.real_team, p.position, p.positions,
                 r.fantasy_team, r.roster_slot, r.lineup_slot, r.slot_changed_at, r.status_reason
        ORDER BY p.player_id ASC
    """, params)

    items = []
    for raw in cursor.fetchall():
        d = dict(raw)
        m = compute_derived_metrics(d)
        lock_info = calc_lock_info(d["roster_slot"], d.get("slot_changed_at"))
        ruling_code, ruling_text = evaluate_ruling_status(d["roster_slot"], d.get("status_reason") or "")
        pos_set = parse_player_positions(d.get("positions"), d.get("position"))
        lineup_slot = normalize_lineup_slot(d.get("lineup_slot"))
        lineup_label = format_slot_label(lineup_slot)
        is_scoring = d["roster_slot"] == "ACTIVE" and lineup_slot in SCORING_LINEUP_SLOTS

        items.append({
            "player_id": d["player_id"],
            "name": d["name"],
            "real_team": d.get("real_team") or "",
            "fantasy_team": d.get("fantasy_team") or "",
            "position": d["position"],
            "positions": sorted(pos_set),
            "slot": d["roster_slot"],
            "lineup_slot": lineup_slot,
            "lineup_label": lineup_label,
            "is_scoring": is_scoring,
            "is_locked": lock_info["is_locked"],
            "days_elapsed": lock_info["days_elapsed"],
            "days_remaining": lock_info["days_remaining"],
            "changed_date": lock_info["changed_date"],
            "ruling_code": ruling_code,
            "ruling_text": ruling_text,
            "raw_reason": d.get("status_reason") or "",
            "r": m["r"]["disp"],
            "avg": m["avg"]["disp"],
            "rbi": m["rbi"]["disp"],
            "sb": m["sb"]["disp"],
            "hr": m["hr"]["disp"],
            "ops": m["ops"]["disp"],
            "w": m["w"]["disp"],
            "ip": m["ip_disp"],
            "era": m["era"]["disp"],
            "k": m["k"]["disp"],
            "hld": m["hld"]["disp"],
            "sv": m["sv"]["disp"],
            "whip": m["whip"]["disp"],
        })
    return items


def get_team_players_with_stats(fantasy_team: str, date_filter: str = "ALL", week_key: str = "ALL"):
    conn = get_db()
    cursor = conn.cursor()
    ensure_extended_schema(cursor)

    rows = _query_players_block(
        cursor,
        "r.fantasy_team = ? AND r.roster_slot != 'RELEASED'",
        [fantasy_team],
        date_filter,
        week_key,
    )
    conn.close()

    grouped = {
        "ACTIVE": [],
        "MINORS": [],
        "IL": [],
        "SPECIAL": [],
        "FA": [],
    }
    for item in rows:
        if item["slot"] in grouped:
            grouped[item["slot"]].append(item)

    grouped["ACTIVE"].sort(
        key=lambda x: (
            0 if x["is_scoring"] else 1,
            SLOT_ORDER_MAP.get(x["lineup_slot"], 999),
            x["player_id"],
        )
    )
    return grouped


def resolve_logged_team(request: Request, code_param: str = None) -> str | None:
    if code_param in CODE_TO_TEAM:
        return CODE_TO_TEAM[code_param]
    cookie_code = request.cookies.get("team_code")
    return CODE_TO_TEAM.get(cookie_code)


@app.get("/")
def read_home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "active_page": "home",
            "enable_admin": ENABLE_ADMIN,
        }
    )


@app.get("/roster")
def read_roster(request: Request, team: str = "樂天桃猿"):
    if team not in ALL_AVAILABLE_TEAMS:
        team = "樂天桃猿"

    conn = get_db()
    cursor = conn.cursor()

    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            SELECT p.player_id, p.name, p.position, r.lineup_slot
            FROM players p
            JOIN team_rosters r ON p.player_id = r.player_id
            WHERE r.fantasy_team = ? AND r.roster_slot = 'ACTIVE'
        """, (team,))
        active_players = [(row["player_id"], row["name"], row["position"], format_slot_label(row["lineup_slot"])) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT p.player_id, p.name, p.position
            FROM players p
            JOIN team_rosters r ON p.player_id = r.player_id
            WHERE r.fantasy_team = ? AND r.roster_slot = 'MINORS'
        """, (team,))
        minors_players = [(row["player_id"], row["name"], row["position"]) for row in cursor.fetchall()]
    except sqlite3.Error:
        active_players = []
        minors_players = []
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="roster.html",
        context={
            "active_page": "roster",
            "enable_admin": ENABLE_ADMIN,
            "teams": ALL_AVAILABLE_TEAMS,
            "current_team": team,
            "active_players": active_players,
            "minors_players": minors_players,
            "lineup_slots_ordered": LINEUP_SLOTS_ORDERED,
            "batter_slots": LINEUP_SLOTS_BATTER,
            "pitcher_slots": LINEUP_SLOTS_PITCHER,
        }
    )


@app.get("/matchup")
def read_matchup(
    request: Request,
    code: str = None,
    opp: str = None,
    stat_date: str = "ALL",
    week: str = "ALL",
    error: str = None,
):
    clear_free_agent_players()
    logged_team = resolve_logged_team(request, code)
    available_dates = get_available_dates()
    selected_week = week if week in SCHEDULE_WEEK_MAP else "ALL"
    selected_date = stat_date if (stat_date == "ALL" or stat_date in available_dates) else "ALL"
    schedule_games = get_schedule_games(selected_week if selected_week != "ALL" else None)

    grouped_players = {"ACTIVE": [], "MINORS": [], "IL": [], "SPECIAL": [], "FA": []}
    categories = []
    wins = 0
    losses = 0
    ties = 0

    other_teams = [t for t in ALL_AVAILABLE_TEAMS if t != logged_team]
    opponent_team = opp if opp in other_teams else (other_teams[0] if other_teams else "中信兄弟")

    if logged_team:
        grouped_players = get_team_players_with_stats(logged_team, selected_date, selected_week)
        my_stats = calc_team_12_cats(logged_team, selected_date, selected_week)
        opp_stats = calc_team_12_cats(opponent_team, selected_date, selected_week)

        cat_defs = [
            ("r", "得分 (R)"),
            ("avg", "打擊率 (AVG)"),
            ("rbi", "打點 (RBI)"),
            ("sb", "盜壘 (SB)"),
            ("hr", "全壘打 (HR)"),
            ("ops", "OPS"),
            ("w", "勝投 (W)"),
            ("era", "防禦率 (ERA)"),
            ("k", "三振 (K)"),
            ("hld", "中繼成功 (HLD)"),
            ("sv", "救援成功 (SV)"),
            ("whip", "WHIP"),
        ]

        for key, label in cat_defs:
            m = my_stats[key]
            o = opp_stats[key]
            diff = m["val"] - o["val"]
            if abs(diff) < 1e-9:
                result = "tie"
                ties += 1
            elif m["lower_better"]:
                if m["val"] < o["val"]:
                    result = "win"
                    wins += 1
                else:
                    result = "loss"
                    losses += 1
            else:
                if m["val"] > o["val"]:
                    result = "win"
                    wins += 1
                else:
                    result = "loss"
                    losses += 1

            categories.append({
                "name": label,
                "my": m["disp"],
                "opp": o["disp"],
                "result": result,
                "lower_better": m["lower_better"]
            })

    total_on_team = (
        len(grouped_players["ACTIVE"])
        + len(grouped_players["MINORS"])
        + len(grouped_players["IL"])
        + len(grouped_players["SPECIAL"])
    )

    response = templates.TemplateResponse(
        request=request,
        name="matchup.html",
        context={
            "active_page": "matchup",
            "enable_admin": ENABLE_ADMIN,
            "teams": ALL_AVAILABLE_TEAMS,
            "other_teams": other_teams,
            "schedule_weeks": SCHEDULE_WEEKS,
            "selected_week": selected_week,
            "schedule_games": schedule_games,
            "logged_team": logged_team,
            "logged_code": TEAM_CODES.get(logged_team, ""),
            "opponent_team": opponent_team,
            "available_dates": available_dates,
            "selected_date": selected_date,
            "active_players": grouped_players["ACTIVE"],
            "minors_players": grouped_players["MINORS"],
            "il_players": grouped_players["IL"],
            "special_players": grouped_players["SPECIAL"],
            "fa_players": grouped_players["FA"],
            "total_on_team": total_on_team,
            "max_active": MAX_ACTIVE_ROSTER,
            "max_total": MAX_TOTAL_ROSTER,
            "lineup_slots_ordered": LINEUP_SLOTS_ORDERED,
            "slot_labels": SLOT_LABELS,
            "batter_slots": LINEUP_SLOTS_BATTER,
            "pitcher_slots": LINEUP_SLOTS_PITCHER,
            "il_reasons": IL_REASONS,
            "special_reasons": SPECIAL_REASONS,
            "lock_days": LOCK_DAYS,
            "categories": categories,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "error": error
        }
    )
    if logged_team:
        response.set_cookie(key="team_code", value=TEAM_CODES[logged_team], path="/")
    return response


@app.post("/api/login")
def api_login(payload: LoginPayload):
    if payload.team in TEAM_PASSWORDS and TEAM_PASSWORDS[payload.team] == payload.password:
        code = TEAM_CODES[payload.team]
        resp = JSONResponse({"ok": True, "redirect_url": f"/matchup?code={code}"})
        resp.set_cookie(key="team_code", value=code, path="/")
        return resp
    return JSONResponse({"ok": False, "error": "密碼錯誤（預設：桃猿1111 / 兄弟2222 / 味全3333 / 富邦4444 / 測試隊0000）"}, status_code=401)


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True, "redirect_url": "/matchup"})
    resp.delete_cookie(key="team_code", path="/")
    return resp


@app.post("/api/fa/sign")
def api_sign_free_agent(request: Request, payload: SignFreeAgentPayload):
    logged_team = resolve_logged_team(request)
    if not logged_team:
        return JSONResponse({"ok": False, "error": "尚未登入"}, status_code=401)

    target_roster = "ACTIVE"
    target_lineup = normalize_lineup_slot(payload.lineup_slot)

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            SELECT p.player_id, p.real_team, r.fantasy_team, r.roster_slot
            FROM players p
            JOIN team_rosters r ON p.player_id = r.player_id
            WHERE p.player_id = ?
        """, (payload.player_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "找不到該名自由球員"}, status_code=404)

        if row["roster_slot"] != "RELEASED" and (row["fantasy_team"] or "").strip():
            return JSONResponse({"ok": False, "error": f"此球員目前屬於【{row['fantasy_team']}】，無法直接簽約"}, status_code=409)

        ok_limit, limit_msg = validate_roster_limits(
            cursor, logged_team, payload.player_id, target_roster, target_lineup
        )
        if not ok_limit and not payload.force_override:
            return JSONResponse({"ok": False, "error": limit_msg}, status_code=400)

        now_iso = datetime.now().isoformat(timespec="seconds")
        cursor.execute("""
            UPDATE team_rosters
            SET fantasy_team = ?, roster_slot = ?, lineup_slot = ?, slot_changed_at = ?, status_reason = '新註冊進一軍'
            WHERE player_id = ?
        """, (logged_team, target_roster, target_lineup, now_iso, payload.player_id))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/lineup/move")
def api_lineup_move(request: Request, payload: LineupMovePayload):
    logged_team = resolve_logged_team(request)
    if not logged_team:
        return JSONResponse({"ok": False, "error": "尚未登入"}, status_code=401)

    new_roster = payload.roster_slot.upper()
    new_lineup = normalize_lineup_slot(payload.lineup_slot)
    if new_roster not in VALID_SLOTS:
        return JSONResponse({"ok": False, "error": "無效的名單狀態"}, status_code=400)
    if new_roster == "ACTIVE" and new_lineup not in ALL_ACTIVE_LINEUP_SLOTS:
        return JSONResponse({"ok": False, "error": "無效的守位槽位"}, status_code=400)

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            SELECT p.player_id, p.name, p.real_team, p.position, p.positions,
                   r.fantasy_team, r.roster_slot, r.lineup_slot, r.slot_changed_at
            FROM players p
            JOIN team_rosters r ON p.player_id = r.player_id
            WHERE p.player_id = ?
        """, (payload.player_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "找不到球員"}, status_code=404)

        if not payload.force_override:
            locked, lock_msg = is_game_locked_for_team(cursor, row["real_team"])
            if locked:
                return JSONResponse({"ok": False, "error": lock_msg}, status_code=400)

            lock_info = calc_lock_info(row["roster_slot"], row["slot_changed_at"])
            if lock_info["is_locked"] and new_roster != row["roster_slot"]:
                return JSONResponse({
                    "ok": False,
                    "error": f"球員處於 {LOCK_DAYS} 天名單鎖定中（剩餘 {lock_info['days_remaining']} 天）"
                }, status_code=400)

            if new_roster == "ACTIVE":
                pos_set = parse_player_positions(row["positions"], row["position"])
                ok_pos, pos_msg = is_lineup_slot_eligible(new_lineup, pos_set)
                if not ok_pos:
                    return JSONResponse({"ok": False, "error": pos_msg}, status_code=400)

            ok_lim, lim_msg = validate_roster_limits(cursor, logged_team, payload.player_id, new_roster, new_lineup)
            if not ok_lim:
                return JSONResponse({"ok": False, "error": lim_msg}, status_code=400)

        now_iso = datetime.now().isoformat(timespec="seconds")
        if new_roster == "RELEASED":
            cursor.execute("""
                UPDATE team_rosters
                SET fantasy_team = '', roster_slot = 'RELEASED', lineup_slot = 'BN',
                    slot_changed_at = ?, status_reason = '釋出至自由球員'
                WHERE player_id = ?
            """, (now_iso, payload.player_id))
        else:
            final_lineup = new_lineup if new_roster == "ACTIVE" else "BN"
            cursor.execute("""
                UPDATE team_rosters
                SET fantasy_team = ?, roster_slot = ?, lineup_slot = ?,
                    slot_changed_at = ?, status_reason = ?
                WHERE player_id = ?
            """, (logged_team, new_roster, final_lineup, now_iso, payload.reason or "", payload.player_id))

        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/schedule")
def api_add_schedule(payload: ScheduleEntryPayload):
    denied = ensure_admin_enabled()
    if denied:
        return denied

    gdate = (payload.game_date or "").strip()
    if not gdate:
        return JSONResponse({"ok": False, "error": "請輸入比賽日期（YYYY-MM-DD）"}, status_code=400)

    wkey = payload.week_key if payload.week_key in SCHEDULE_WEEK_MAP else (
        "2026-11-23_2026-11-29" if gdate <= "2026-11-29" else "2026-11-30_2026-12-03"
    )
    sno = (payload.game_sno or "").strip() or f"{gdate.replace('-', '')}_{(payload.away_team or 'A')[:2]}_{(payload.home_team or 'H')[:2]}"
    gkey = f"{payload.year}_{payload.kind_code}_{sno}"
    now_iso = datetime.now().isoformat(timespec="seconds")

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            INSERT INTO cpbl_games (game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_key) DO UPDATE SET
                game_date = excluded.game_date,
                start_time = excluded.start_time,
                away_team = excluded.away_team,
                home_team = excluded.home_team,
                week_key = excluded.week_key,
                updated_at = excluded.updated_at
        """, (
            gkey,
            (payload.year or "2026").strip(),
            (payload.kind_code or "A").strip(),
            sno,
            gdate,
            (payload.start_time or "").strip(),
            (payload.away_team or "").strip(),
            (payload.home_team or "").strip(),
            wkey,
            now_iso,
        ))
        conn.commit()
        return JSONResponse({"ok": True, "game_key": gkey, "week_key": wkey})
    finally:
        conn.close()


@app.post("/api/admin/player")
def api_admin_player(payload: PlayerAdminPayload):
    denied = ensure_admin_enabled()
    if denied:
        return denied

    name = (payload.name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "請輸入球員名稱"}, status_code=400)

    pos_list = [p.strip().upper() for p in (payload.positions or []) if p.strip()]
    pos_str = ",".join(pos_list)
    primary_pos = pos_list[0] if pos_list else "DH"
    roster_slot = payload.roster_slot.upper() if payload.roster_slot.upper() in VALID_SLOTS else "ACTIVE"
    lineup_slot = normalize_lineup_slot(payload.lineup_slot)
    if roster_slot != "ACTIVE" or lineup_slot not in ALL_ACTIVE_LINEUP_SLOTS:
        lineup_slot = "BN"

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("SELECT player_id FROM players WHERE name = ? ORDER BY player_id ASC LIMIT 1", (name,))
        existing = cursor.fetchone()
        player_id = payload.player_id or (existing["player_id"] if existing else f"CPBL_{name}")

        cursor.execute("""
            INSERT INTO players (player_id, name, real_team, position, positions)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                name = excluded.name,
                real_team = CASE WHEN excluded.real_team != '' THEN excluded.real_team ELSE players.real_team END,
                position = CASE WHEN excluded.positions != '' THEN excluded.position ELSE players.position END,
                positions = CASE WHEN excluded.positions != '' THEN excluded.positions ELSE players.positions END
        """, (player_id, name, payload.real_team or "", primary_pos, pos_str))

        now_iso = datetime.now().isoformat(timespec="seconds")
        fteam = "" if roster_slot == "RELEASED" else (payload.fantasy_team or "")
        cursor.execute("""
            INSERT INTO team_rosters (player_id, fantasy_team, roster_slot, lineup_slot, slot_changed_at, status_reason)
            VALUES (?, ?, ?, ?, ?, '手動新增／更新')
            ON CONFLICT(player_id) DO UPDATE SET
                fantasy_team = excluded.fantasy_team,
                roster_slot = excluded.roster_slot,
                lineup_slot = excluded.lineup_slot,
                slot_changed_at = excluded.slot_changed_at,
                status_reason = excluded.status_reason
        """, (player_id, fteam, roster_slot, lineup_slot, now_iso))

        conn.commit()
        return JSONResponse({"ok": True, "player_id": player_id})
    finally:
        conn.close()


@app.post("/api/stats/import/cpbl")
def api_import_cpbl(payload: CpblImportPayload):
    denied = ensure_admin_enabled()
    if denied:
        return denied

    try:
        result = import_cpbl_game(
            db_path="fantasy_league.db",
            year=payload.year,
            kind_code=payload.kind_code,
            game_sno=payload.game_sno,
        )
        return JSONResponse({"ok": True, "result": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/admin/seed-winter-test")
def api_seed_winter_test(payload: SeedWinterPayload):
    denied = ensure_admin_enabled()
    if denied:
        return denied

    team = payload.team if payload.team in ALL_AVAILABLE_TEAMS else "台灣海洋"
    count = max(1, min(60, int(payload.count or 25)))
    sample_plan = [
        ("SP", "SP1"), ("SP", "SP2"), ("SP", "SP3"),
        ("P", "P1"), ("P", "P2"), ("P", "P3"),
        ("CP", "CP1"), ("CP", "CP2"), ("CP", "CP3"),
        ("DH", "DH1"), ("DH", "DH2"),
        ("C", "C"), ("1B", "1B"), ("2B", "2B"), ("3B", "3B"), ("SS", "SS"),
        ("OF", "OF1"), ("OF", "OF2"), ("OF", "OF3"),
    ]
    target_date = (payload.stat_date or "").strip() or date.today().isoformat()

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        now_iso = datetime.now().isoformat(timespec="seconds")
        seeded_stats = 0

        for i in range(1, count + 1):
            pid = f"TEST_{TEAM_CODES[team]}_{i:02d}"
            pname = f"{team}球員{i:02d}"
            pos, assigned_lineup = sample_plan[(i - 1) % len(sample_plan)]
            r_slot = "ACTIVE" if i <= MAX_ACTIVE_ROSTER else "MINORS"
            l_slot = assigned_lineup if (r_slot == "ACTIVE" and i <= len(sample_plan)) else "BN"

            cursor.execute("""
                INSERT INTO players (player_id, name, real_team, position, positions)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(player_id) DO UPDATE SET
                    name = excluded.name,
                    real_team = excluded.real_team,
                    position = excluded.position,
                    positions = excluded.positions
            """, (pid, pname, team, pos, pos))

            cursor.execute("""
                INSERT INTO team_rosters (player_id, fantasy_team, roster_slot, lineup_slot, slot_changed_at, status_reason)
                VALUES (?, ?, ?, ?, ?, '測試灌入')
                ON CONFLICT(player_id) DO UPDATE SET
                    fantasy_team = excluded.fantasy_team,
                    roster_slot = excluded.roster_slot,
                    lineup_slot = excluded.lineup_slot,
                    slot_changed_at = excluded.slot_changed_at,
                    status_reason = excluded.status_reason
            """, (pid, team, r_slot, l_slot, now_iso))

            if payload.with_stats and l_slot in SCORING_LINEUP_SLOTS:
                if pos in {"SP", "P", "CP"}:
                    is_sp = l_slot.startswith("SP")
                    is_cp = l_slot.startswith("CP")
                    cursor.execute("""
                        INSERT INTO player_daily_stats (
                            player_id, stat_date, ip, er, k, w, hld, sv, pitch_h, pitch_bb
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(player_id, stat_date) DO UPDATE SET
                            ip = excluded.ip, er = excluded.er, k = excluded.k,
                            w = excluded.w, hld = excluded.hld, sv = excluded.sv,
                            pitch_h = excluded.pitch_h, pitch_bb = excluded.pitch_bb
                    """, (
                        pid, target_date,
                        6.0 if is_sp else 1.0,
                        2 if is_sp else 0,
                        6 if is_sp else 2,
                        1 if i == 1 else 0,
                        1 if i == 4 else 0,
                        1 if is_cp and i == 7 else 0,
                        5 if is_sp else 1,
                        2 if is_sp else 0,
                    ))
                else:
                    cursor.execute("""
                        INSERT INTO player_daily_stats (
                            player_id, stat_date, r, ab, h, bb, hbp, sf, rbi, sb, hr, tb
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(player_id, stat_date) DO UPDATE SET
                            r = excluded.r, ab = excluded.ab, h = excluded.h,
                            bb = excluded.bb, hbp = excluded.hbp, sf = excluded.sf,
                            rbi = excluded.rbi, sb = excluded.sb, hr = excluded.hr, tb = excluded.tb
                    """, (
                        pid, target_date,
                        1 if i % 2 == 1 else 0,
                        4,
                        2 if i % 3 != 0 else 1,
                        1 if i % 4 == 0 else 0,
                        0, 0,
                        2 if i % 2 == 0 else 1,
                        1 if i in {12, 15} else 0,
                        1 if i in {10, 14} else 0,
                        4 if i in {10, 14} else 2,
                    ))
                seeded_stats += 1

        conn.commit()
        return JSONResponse({
            "ok": True,
            "team": team,
            "seeded_players": count,
            "seeded_stats": seeded_stats,
            "stat_date": target_date,
        })
    finally:
        conn.close()


@app.post("/api/admin/cleanup-winter-test")
def api_cleanup_winter_test():
    denied = ensure_admin_enabled()
    if denied:
        return denied

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("SELECT COUNT(*) FROM players WHERE player_id LIKE 'TEST_%'")
        cnt = cursor.fetchone()[0]
        cursor.execute("DELETE FROM player_daily_stats WHERE player_id LIKE 'TEST_%'")
        cursor.execute("DELETE FROM team_rosters WHERE player_id LIKE 'TEST_%'")
        cursor.execute("DELETE FROM players WHERE player_id LIKE 'TEST_%'")
        conn.commit()
        return JSONResponse({"ok": True, "removed_players": cnt})
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)