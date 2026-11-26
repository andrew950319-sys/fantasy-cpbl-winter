from datetime import date, datetime, timedelta
import os
import sqlite3
import threading
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from cpbl_fetcher import cleanup_stale_winter_games, import_cpbl_game, sync_awb_schedule

app = FastAPI()
templates = Jinja2Templates(directory="templates")

ENABLE_ADMIN = os.environ.get("ENABLE_ADMIN", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
AUTO_FETCH_ENABLED = os.environ.get("AUTO_FETCH_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
AUTO_FETCH_INTERVAL_SEC = int(os.environ.get("AUTO_FETCH_INTERVAL_SEC", "600"))

auto_fetch_lock = threading.Lock()
auto_fetch_status = {
    "last_run_at": "",
    "last_summary": "尚未執行自動抓賽程／抓數",
}


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
        "key": "2026-11-30_2026-12-04",
        "label": "第2週（11/30～12/4）",
        "start_date": "2026-11-30",
        "end_date": "2026-12-04",
    },
]
SCHEDULE_WEEK_MAP = {w["key"]: w for w in SCHEDULE_WEEKS}


def get_default_week_for_today(today_str: str | None = None) -> str:
    d_str = today_str or date.today().isoformat()
    if d_str <= "2026-11-29":
        return "2026-11-23_2026-11-29"
    return "2026-11-30_2026-12-04"


def get_default_stat_date(available_dates: list[str], today_str: str | None = None) -> str:
    d_str = today_str or date.today().isoformat()
    if d_str in available_dates:
        return d_str
    if d_str <= "2026-11-23" and "2026-11-23" in available_dates:
        return "2026-11-23"
    return "ALL"


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
    "DH1": "DH1",
    "DH2": "DH2",
    "DH": "DH1",
    "C": "C",
    "1B": "1B",
    "2B": "2B",
    "3B": "3B",
    "SS": "SS",
    "OF1": "OF1",
    "OF2": "OF2",
    "OF3": "OF3",
    "OF": "OF1",
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
    target_date: str | None = None


class SignFreeAgentPayload(BaseModel):
    player_id: str
    roster_slot: str = "ACTIVE"
    lineup_slot: str = "BN"
    force_override: bool = False


class AdminForceMovePayload(BaseModel):
    player_id: str
    fantasy_team: str | None = None
    roster_slot: str = "ACTIVE"
    lineup_slot: str = "BN"
    reason: str = "後台強制覆寫調度"


class AdminForceSignPayload(BaseModel):
    player_id: str
    fantasy_team: str
    lineup_slot: str = "BN"
    reason: str = "後台強制覆寫簽約"


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
    kind_code: str = "P"
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
    kind_code: str = "P"
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
    if s == "DH":
        return "DH1"
    if s == "OF":
        return "OF1"
    if s not in ALL_ACTIVE_LINEUP_SLOTS:
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
    cursor.execute("SELECT 1 FROM team_rosters WHERE slot_changed_at IS NULL OR slot_changed_at = '' LIMIT 1")
    if cursor.fetchone() is not None:
        now_iso = datetime.now().isoformat(timespec="seconds")
        for _u_try in range(6):
            try:
                cursor.execute("""
                    UPDATE team_rosters
                    SET slot_changed_at = ?
                    WHERE slot_changed_at IS NULL OR slot_changed_at = ''
                """, (now_iso,))
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and _u_try < 5:
                    time.sleep(0.2 * (_u_try + 1))
                    continue
                raise

def ensure_stats_columns(cursor):
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
            pitch_hbp INTEGER DEFAULT 0,
            PRIMARY KEY (player_id, stat_date)
        )
    """)
    cursor.execute("PRAGMA table_info(player_daily_stats)")
    s_cols = {r["name"] for r in cursor.fetchall()}
    if "pitch_hbp" not in s_cols:
        cursor.execute("ALTER TABLE player_daily_stats ADD COLUMN pitch_hbp INTEGER DEFAULT 0")


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
    ensure_stats_columns(cursor)

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
            last_fetched_at TEXT DEFAULT '',
            updated_at TEXT
        )
    """)
    cursor.execute("PRAGMA table_info(cpbl_games)")
    g_cols = {r["name"] for r in cursor.fetchall()}
    if "week_key" not in g_cols:
        cursor.execute("ALTER TABLE cpbl_games ADD COLUMN week_key TEXT DEFAULT ''")
    if "last_fetched_at" not in g_cols:
        cursor.execute("ALTER TABLE cpbl_games ADD COLUMN last_fetched_at TEXT DEFAULT ''")
    # Seed 2026-11-26 Taiwan Mountain vs Taiwan Sea game if missing
    cursor.execute("""
        INSERT OR IGNORE INTO cpbl_games (
            game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key, updated_at
        )
        VALUES (
            '2026_P_20261126_山林_海洋', '2026', 'P', '20261126_山林_海洋',
            '2026-11-26', '12:05', '台灣山林', '台灣海洋',
            '2026-11-23_2026-11-29', CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("SELECT 1 FROM team_rosters WHERE roster_slot = 'ACTIVE' AND (lineup_slot IS NULL OR lineup_slot = '' OR UPPER(lineup_slot) = 'BENCH') LIMIT 1")
    if cursor.fetchone() is not None:
        for _u_try in range(6):
            try:
                cursor.execute("""
                    UPDATE team_rosters
                    SET lineup_slot = 'BN'
                    WHERE roster_slot = 'ACTIVE' AND (lineup_slot IS NULL OR lineup_slot = '' OR UPPER(lineup_slot) = 'BENCH')
                """)
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and _u_try < 5:
                    time.sleep(0.2 * (_u_try + 1))
                    continue
                raise

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_matchup_results (
            week_key TEXT PRIMARY KEY,
            week_label TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            team_a TEXT NOT NULL,
            team_b TEXT NOT NULL,
            team_a_cat_w INTEGER NOT NULL DEFAULT 0,
            team_a_cat_l INTEGER NOT NULL DEFAULT 0,
            team_a_cat_t INTEGER NOT NULL DEFAULT 0,
            team_a_series_w INTEGER NOT NULL DEFAULT 0,
            team_a_series_l INTEGER NOT NULL DEFAULT 0,
            team_a_series_t INTEGER NOT NULL DEFAULT 0,
            team_b_series_w INTEGER NOT NULL DEFAULT 0,
            team_b_series_l INTEGER NOT NULL DEFAULT 0,
            team_b_series_t INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '[]',
            settled_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS standing_popup_seen (
            fantasy_team TEXT NOT NULL,
            week_key TEXT NOT NULL,
            seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (fantasy_team, week_key)
        )
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
    conn.commit()
    conn.close()


init_stats_and_sample_data()



def run_auto_fetch_due_games(force_all: bool = False, sync_schedule: bool = True) -> dict:
    with auto_fetch_lock:
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        now_iso = now_dt.isoformat(timespec="seconds")

        clean_msg = ""
        try:
            cleaned = cleanup_stale_winter_games(db_path="fantasy_league.db")
            if cleaned.get("removed_games_count"):
                clean_msg = f"清舊賽程 {cleaned['removed_games_count']} 場｜"
        except Exception:
            pass

        schedule_msg = ""
        if sync_schedule:
            try:
                sch_res = sync_awb_schedule(year="2026", kind_code="P", db_path="fantasy_league.db")
                schedule_msg = f"同步賽程 {sch_res.get('synced_count', 0)} 場"
            except Exception as exc:
                schedule_msg = f"賽程同步略過({exc})"

        conn = get_db()
        cursor = conn.cursor()
        ensure_extended_schema(cursor)
        cursor.execute("""
            SELECT game_key, year, kind_code, game_sno, game_date, start_time, last_fetched_at
            FROM cpbl_games
            WHERE TRIM(COALESCE(game_sno, '')) != ''
            ORDER BY game_date ASC, game_sno ASC
        """)
        games = [dict(r) for r in cursor.fetchall()]
        conn.close()

        due_games = []
        for g in games:
            sno = (g.get("game_sno") or "").strip()
            if not sno.isdigit():
                continue
            gdate = (g.get("game_date") or "").strip()
            if not force_all and gdate > today_str:
                continue
            due_games.append(g)

        if not due_games:
            summary = f"{clean_msg}{schedule_msg}｜無到期需抓數的場次（已檢查 {len(games)} 筆賽程）"
            auto_fetch_status["last_run_at"] = now_iso
            auto_fetch_status["last_summary"] = summary
            return {"ok": True, "fetched_count": 0, "errors": [], "summary": summary}

        fetched_count = 0
        errors = []
        for g in due_games:
            try:
                import_cpbl_game(
                    db_path="fantasy_league.db",
                    year=(g.get("year") or "2026").strip(),
                    kind_code=(g.get("kind_code") or "P").strip(),
                    game_sno=g["game_sno"].strip(),
                )
                fetched_count += 1
                c2 = get_db()
                cur2 = c2.cursor()
                cur2.execute("""
                    UPDATE cpbl_games
                    SET last_fetched_at = ?, updated_at = ?
                    WHERE game_key = ?
                """, (now_iso, now_iso, g["game_key"]))
                c2.commit()
                c2.close()
            except Exception as exc:
                errors.append(f"{g['game_date']} #{g['game_sno']}: {exc}")

        summary = f"{clean_msg}{schedule_msg}｜抓成績 {fetched_count}/{len(due_games)} 場" + (f"（{len(errors)} 筆失敗）" if errors else "")
        auto_fetch_status["last_run_at"] = now_iso
        auto_fetch_status["last_summary"] = summary
        return {
            "ok": len(errors) == 0,
            "fetched_count": fetched_count,
            "total_due": len(due_games),
            "errors": errors,
            "summary": summary,
        }


def _auto_fetch_worker():
    while AUTO_FETCH_ENABLED:
        try:
            run_auto_fetch_due_games(force_all=False, sync_schedule=True)
        except Exception:
            pass
        time.sleep(max(30, AUTO_FETCH_INTERVAL_SEC))


@app.on_event("startup")
def on_startup_start_scheduler():
    if AUTO_FETCH_ENABLED:
        t = threading.Thread(target=_auto_fetch_worker, daemon=True, name="cpbl-auto-fetcher")
        t.start()


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
    pitch_hbp = row.get("pitch_hbp", 0) or 0

    avg = (h / ab) if ab > 0 else 0.0
    obp_denom = ab + bb + hbp + sf
    obp = ((h + bb + hbp) / obp_denom) if obp_denom > 0 else 0.0
    slg = (tb / ab) if ab > 0 else 0.0
    ops = obp + slg

    era = ((er * 9) / ip) if ip > 0 else (99.0 if er > 0 else 0.0)
    whip = ((pitch_h + pitch_bb + pitch_hbp) / ip) if ip > 0 else (99.0 if (pitch_h + pitch_bb + pitch_hbp) > 0 else 0.0)

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

    if slot in LINEUP_SLOTS_INFIELD:
        label = format_slot_label(slot)
        if is_pit and not (positions - {"P", "SP", "RP", "CP"}):
            return False, "投手不能放進野手先發槽位"
        if label not in positions:
            return False, f"球員無 {label} 守位資格（目前守位：{', '.join(sorted(positions)) or '無'}）"
        return True, ""

    if slot.startswith("OF"):
        if is_pit and not (positions - {"P", "SP", "RP", "CP"}):
            return False, "投手不能放進野手先發槽位"
        if "OF" not in positions:
            return False, f"球員無 OF 守位資格（目前守位：{', '.join(sorted(positions)) or '無'}）"
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


def _has_game_stats_imported(cursor, game_key: str, game_date: str) -> bool:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='player_game_stats'")
    if not cursor.fetchone():
        return False
    cursor.execute("""
        SELECT 1 FROM player_game_stats
        WHERE game_key = ? AND stat_date = ?
        LIMIT 1
    """, (game_key, game_date))
    return cursor.fetchone() is not None


def _parse_game_start_dt(game_date_str: str, start_time_str: str) -> datetime | None:
    st_str = (start_time_str or "").strip()
    g_date = (game_date_str or "").strip()
    if not st_str:
        return None
    try:
        return datetime.fromisoformat(st_str)
    except ValueError:
        pass
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t_obj = datetime.strptime(st_str, fmt).time()
            d_obj = date.fromisoformat(g_date)
            return datetime.combine(d_obj, t_obj)
        except ValueError:
            continue
    return None


def is_game_locked_for_team(cursor, real_team: str | None, target_date: str | None = None) -> tuple[bool, str]:
    if not real_team:
        return False, ""
    check_date = (target_date or date.today().isoformat()).strip()
    today_str = date.today().isoformat()
    if check_date > today_str:
        return False, ""
    cursor.execute("""
        SELECT game_key, game_date, start_time, away_team, home_team
        FROM cpbl_games
        WHERE game_date = ? AND (away_team = ? OR home_team = ?)
        ORDER BY start_time ASC
    """, (check_date, real_team, real_team))
    rows = cursor.fetchall()
    now_dt = datetime.now()
    for r in rows:
        st_dt = _parse_game_start_dt(r["game_date"], r["start_time"])
        if not st_dt:
            continue
        if now_dt >= st_dt:
            return True, f"現實球隊【{real_team}】於 {r['game_date']} {st_dt.strftime('%H:%M')} 已開賽，該隊球員不得更動"
    return False, ""


def get_effective_lineup_map(cursor, fantasy_team: str, target_date: str | None = None) -> dict[str, dict]:
    team_clean = (fantasy_team or "").strip()
    if not team_clean:
        return {}
    eff_date = (target_date or date.today().isoformat()).strip()

    # 1. 先查 target_date 當天；2. 若無則往回找 <= target_date 最近一天沿用；3. 皆無則退回 team_rosters 現況
    cursor.execute("""
        SELECT stat_date
        FROM daily_lineups
        WHERE TRIM(COALESCE(fantasy_team, '')) = ?
          AND stat_date <= ?
        ORDER BY stat_date DESC
        LIMIT 1
    """, (team_clean, eff_date))
    picked_row = cursor.fetchone()

    result: dict[str, dict] = {}
    if picked_row:
        picked_date = picked_row["stat_date"]
        cursor.execute("""
            SELECT player_id, roster_slot, lineup_slot
            FROM daily_lineups
            WHERE TRIM(COALESCE(fantasy_team, '')) = ?
              AND stat_date = ?
        """, (team_clean, picked_date))
        for dl in cursor.fetchall():
            r_slot = (dl["roster_slot"] or "ACTIVE").upper().strip()
            l_slot = normalize_lineup_slot(dl["lineup_slot"])
            result[dl["player_id"]] = {
                "roster_slot": r_slot,
                "lineup_slot": l_slot,
                "source_date": picked_date,
            }
        return result

    cursor.execute("""
        SELECT player_id, roster_slot, lineup_slot
        FROM team_rosters
        WHERE TRIM(COALESCE(fantasy_team, '')) = ?
    """, (team_clean,))
    for tr in cursor.fetchall():
        r_slot = (tr["roster_slot"] or "ACTIVE").upper().strip()
        l_slot = normalize_lineup_slot(tr["lineup_slot"])
        result[tr["player_id"]] = {
            "roster_slot": r_slot,
            "lineup_slot": l_slot,
            "source_date": "ROSTER",
        }
    return result


def save_daily_lineup_snapshot(cursor, fantasy_team: str, target_date: str, lineup_map: dict[str, dict] | None = None):
    team_clean = (fantasy_team or "").strip()
    date_clean = (target_date or date.today().isoformat()).strip()
    if not team_clean or not date_clean:
        return

    if lineup_map is None:
        cursor.execute("""
            SELECT player_id, roster_slot, lineup_slot
            FROM team_rosters
            WHERE TRIM(COALESCE(fantasy_team, '')) = ?
        """, (team_clean,))
        snapshot_rows = [
            (
                team_clean,
                date_clean,
                r["player_id"],
                (r["roster_slot"] or "ACTIVE").upper().strip(),
                normalize_lineup_slot(r["lineup_slot"]),
            )
            for r in cursor.fetchall()
        ]
    else:
        snapshot_rows = [
            (
                team_clean,
                date_clean,
                pid,
                (info.get("roster_slot") or "ACTIVE").upper().strip(),
                normalize_lineup_slot(info.get("lineup_slot")),
            )
            for pid, info in lineup_map.items()
        ]

    cursor.execute("""
        DELETE FROM daily_lineups
        WHERE TRIM(COALESCE(fantasy_team, '')) = ? AND stat_date = ?
    """, (team_clean, date_clean))
    if snapshot_rows:
        cursor.executemany("""
            INSERT INTO daily_lineups (fantasy_team, stat_date, player_id, roster_slot, lineup_slot, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, snapshot_rows)


def _calc_empty_slot_penalties(lineup_map: dict[str, dict]) -> tuple[int, int]:
    occupied_slots = {
        normalize_lineup_slot(info.get("lineup_slot"))
        for info in lineup_map.values()
        if (info.get("roster_slot") or "").upper().strip() == "ACTIVE"
        and normalize_lineup_slot(info.get("lineup_slot")) in SCORING_LINEUP_SLOTS
    }
    empty_batter_slots = sum(1 for s in LINEUP_SLOTS_BATTER if s not in occupied_slots)
    empty_pitcher_slots = sum(1 for s in LINEUP_SLOTS_PITCHER if s not in occupied_slots)
    # 野手先發空槽 +5 AB，投手先發空槽 +2 ER
    return empty_batter_slots * 5, empty_pitcher_slots * 2


def validate_roster_limits(
    cursor,
    fantasy_team: str,
    moving_player_id: str,
    new_roster_slot: str,
    new_lineup_slot: str,
    ignore_slot_conflict_with: str | None = None,
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
        for pid, v in sim_map.items()
        if v["roster_slot"] == "ACTIVE"
        and normalize_lineup_slot(v.get("lineup_slot")) in SCORING_LINEUP_SLOTS
        and pid != ignore_slot_conflict_with
    ]
    if len(used_scoring_slots) != len(set(used_scoring_slots)):
        return False, "該先發守位槽位已被其他球員佔用"

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
            SELECT game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key, last_fetched_at
            FROM cpbl_games
            WHERE week_key = ? OR (game_date >= ? AND game_date <= ?)
            ORDER BY game_date ASC, game_sno ASC
        """, (week_key, w["start_date"], w["end_date"]))
    else:
        cursor.execute("""
            SELECT game_key, year, kind_code, game_sno, game_date, start_time, away_team, home_team, week_key, last_fetched_at
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
    team_clean = (fantasy_team or "").strip()

    agg = {
        "r": 0, "ab": 0, "h": 0, "bb": 0, "hbp": 0, "sf": 0,
        "rbi": 0, "sb": 0, "hr": 0, "tb": 0,
        "w": 0, "ip": 0.0, "er": 0, "k": 0, "hld": 0, "sv": 0,
        "pitch_h": 0, "pitch_bb": 0, "pitch_hbp": 0,
    }

    if date_filter and date_filter != "ALL":
        target_dates = [date_filter]
    elif week_key and week_key in SCHEDULE_WEEK_MAP:
        w = SCHEDULE_WEEK_MAP[week_key]
        cursor.execute(
            "SELECT DISTINCT stat_date FROM player_daily_stats WHERE stat_date >= ? AND stat_date <= ? ORDER BY stat_date ASC",
            (w["start_date"], w["end_date"]),
        )
        target_dates = [r["stat_date"] for r in cursor.fetchall()]
    else:
        cursor.execute("SELECT DISTINCT stat_date FROM player_daily_stats ORDER BY stat_date ASC")
        target_dates = [r["stat_date"] for r in cursor.fetchall()]

    for d_str in target_dates:
        lineup_map = get_effective_lineup_map(cursor, team_clean, d_str)
        scoring_pids = [
            pid
            for pid, info in lineup_map.items()
            if (info.get("roster_slot") or "").upper().strip() == "ACTIVE"
            and normalize_lineup_slot(info.get("lineup_slot")) in SCORING_LINEUP_SLOTS
        ]
        if scoring_pids:
            pid_placeholders = ",".join("?" for _ in scoring_pids)
            cursor.execute(f"""
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
                    COALESCE(SUM(pitch_bb), 0) AS pitch_bb,
                    COALESCE(SUM(pitch_hbp), 0) AS pitch_hbp
                FROM player_daily_stats
                WHERE stat_date = ? AND player_id IN ({pid_placeholders})
            """, [d_str] + scoring_pids)
            fetched = cursor.fetchone()
            if fetched:
                for k in agg:
                    agg[k] = (agg[k] or 0) + (fetched[k] or 0)

        pen_ab, pen_er = _calc_empty_slot_penalties(lineup_map)
        agg["ab"] = (agg["ab"] or 0) + pen_ab
        agg["er"] = (agg["er"] or 0) + pen_er

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
            COALESCE(SUM(s.pitch_bb), 0) AS pitch_bb,
            COALESCE(SUM(s.pitch_hbp), 0) AS pitch_hbp
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
        raw_lineup_slot = normalize_lineup_slot(d.get("lineup_slot"))
        is_scoring = d["roster_slot"] == "ACTIVE" and raw_lineup_slot in SCORING_LINEUP_SLOTS
        lineup_slot = raw_lineup_slot if is_scoring else "BN"
        lineup_label = format_slot_label(lineup_slot)

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


def get_team_players_with_stats(
    fantasy_team: str,
    date_filter: str = "ALL",
    week_key: str = "ALL",
    lineup_date: str | None = None,
):
    conn = get_db()
    cursor = conn.cursor()
    ensure_extended_schema(cursor)
    ensure_draft_schema(cursor)

    rows = _query_players_block(
        cursor,
        "r.fantasy_team = ? AND r.roster_slot != 'RELEASED'",
        [fantasy_team],
        date_filter,
        week_key,
    )
    eff_date = (lineup_date or (date_filter if date_filter and date_filter != "ALL" else date.today().isoformat())).strip()
    eff_map = get_effective_lineup_map(cursor, fantasy_team, eff_date)
    for item in rows:
        pid = item["player_id"]
        if pid in eff_map:
            r_slot = (eff_map[pid].get("roster_slot") or item.get("roster_slot") or "ACTIVE").upper().strip()
            l_slot = normalize_lineup_slot(eff_map[pid].get("lineup_slot") or item.get("lineup_slot"))
            item["roster_slot"] = r_slot
            item["lineup_slot"] = l_slot
            item["slot"] = r_slot
            item["is_scoring"] = (r_slot == "ACTIVE" and l_slot in SCORING_LINEUP_SLOTS)
            item["slot_label"] = format_slot_label(l_slot if r_slot == "ACTIVE" else r_slot)
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


import json

STANDINGS_TEST_TEAMS = ["台灣山林", "台灣海洋"]
TWELVE_CAT_DEFS = [
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


def _compare_12_cats_pair(team_a: str, team_b: str, week_key: str) -> dict:
    a_stats = calc_team_12_cats(team_a, date_filter="ALL", week_key=week_key)
    b_stats = calc_team_12_cats(team_b, date_filter="ALL", week_key=week_key)
    a_w = a_l = a_t = 0
    details = []

    for key, label in TWELVE_CAT_DEFS:
        ma = a_stats[key]
        mb = b_stats[key]
        diff = ma["val"] - mb["val"]
        if abs(diff) < 1e-9:
            res_a = "tie"
            a_t += 1
        elif ma["lower_better"]:
            if ma["val"] < mb["val"]:
                res_a = "win"
                a_w += 1
            else:
                res_a = "loss"
                a_l += 1
        else:
            if ma["val"] > mb["val"]:
                res_a = "win"
                a_w += 1
            else:
                res_a = "loss"
                a_l += 1

        details.append({
            "key": key,
            "name": label,
            "team_a_val": ma["disp"],
            "team_b_val": mb["disp"],
            "result_a": res_a,
        })

    if a_w > a_l:
        a_sw, a_sl, a_st = 1, 0, 0
        b_sw, b_sl, b_st = 0, 1, 0
    elif a_l > a_w:
        a_sw, a_sl, a_st = 0, 1, 0
        b_sw, b_sl, b_st = 1, 0, 0
    else:
        a_sw, a_sl, a_st = 0, 0, 1
        b_sw, b_sl, b_st = 0, 0, 1

    return {
        "team_a": team_a,
        "team_b": team_b,
        "a_cat_w": a_w,
        "a_cat_l": a_l,
        "a_cat_t": a_t,
        "a_sw": a_sw,
        "a_sl": a_sl,
        "a_st": a_st,
        "b_sw": b_sw,
        "b_sl": b_sl,
        "b_st": b_st,
        "details": details,
    }


def settle_finished_weeks(cursor, today_str: str | None = None, force_week_key: str | None = None) -> list[dict]:
    d_now = (today_str or date.today().isoformat()).strip()
    team_a, team_b = STANDINGS_TEST_TEAMS[0], STANDINGS_TEST_TEAMS[1]
    newly_settled = []

    for w in SCHEDULE_WEEKS:
        w_key = w["key"]
        is_finished = (d_now > w["end_date"]) or (force_week_key == w_key)
        if not is_finished:
            continue

        cursor.execute("SELECT 1 FROM weekly_matchup_results WHERE week_key = ?", (w_key,))
        if cursor.fetchone() and force_week_key != w_key:
            continue

        cmp = _compare_12_cats_pair(team_a, team_b, w_key)
        for _attempt in range(5):
            try:
                cursor.execute("""
                    INSERT INTO weekly_matchup_results (
                        week_key, week_label, start_date, end_date,
                        team_a, team_b,
                        team_a_cat_w, team_a_cat_l, team_a_cat_t,
                        team_a_series_w, team_a_series_l, team_a_series_t,
                        team_b_series_w, team_b_series_l, team_b_series_t,
                        details_json, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(week_key) DO UPDATE SET
                        team_a_cat_w = excluded.team_a_cat_w,
                        team_a_cat_l = excluded.team_a_cat_l,
                        team_a_cat_t = excluded.team_a_cat_t,
                        team_a_series_w = excluded.team_a_series_w,
                        team_a_series_l = excluded.team_a_series_l,
                        team_a_series_t = excluded.team_a_series_t,
                        team_b_series_w = excluded.team_b_series_w,
                        team_b_series_l = excluded.team_b_series_l,
                        team_b_series_t = excluded.team_b_series_t,
                        details_json = excluded.details_json,
                        settled_at = CURRENT_TIMESTAMP
                """, (
                    w_key, w["label"], w["start_date"], w["end_date"],
                    team_a, team_b,
                    cmp["a_cat_w"], cmp["a_cat_l"], cmp["a_cat_t"],
                    cmp["a_sw"], cmp["a_sl"], cmp["a_st"],
                    cmp["b_sw"], cmp["b_sl"], cmp["b_st"],
                    json.dumps(cmp["details"], ensure_ascii=False),
                ))
                break
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and _attempt < 4:
                    time.sleep(0.15 * (_attempt + 1))
                    continue
                raise
        newly_settled.append({"week_key": w_key, "week_label": w["label"], **cmp})

    return newly_settled


def build_standings_board(cursor, today_str: str | None = None) -> tuple[list[dict], list[dict]]:
    d_now = (today_str or date.today().isoformat()).strip()
    settle_finished_weeks(cursor, d_now)

    cursor.execute("SELECT * FROM weekly_matchup_results ORDER BY start_date ASC")
    rows = [dict(r) for r in cursor.fetchall()]

    remaining_weeks = sum(1 for w in SCHEDULE_WEEKS if d_now <= w["end_date"])
    records = {
        t: {"team": t, "w": 0, "l": 0, "t": 0}
        for t in STANDINGS_TEST_TEAMS
    }

    week_history = []
    for r in rows:
        ta, tb = r["team_a"], r["team_b"]
        if ta in records:
            records[ta]["w"] += r["team_a_series_w"]
            records[ta]["l"] += r["team_a_series_l"]
            records[ta]["t"] += r["team_a_series_t"]
        if tb in records:
            records[tb]["w"] += r["team_b_series_w"]
            records[tb]["l"] += r["team_b_series_l"]
            records[tb]["t"] += r["team_b_series_t"]

        try:
            details = json.loads(r.get("details_json") or "[]")
        except Exception:
            details = []

        week_history.append({
            "week_key": r["week_key"],
            "week_label": r["week_label"],
            "team_a": ta,
            "team_b": tb,
            "team_a_cat_w": r["team_a_cat_w"],
            "team_a_cat_l": r["team_a_cat_l"],
            "team_a_cat_t": r["team_a_cat_t"],
            "team_a_sw": r["team_a_series_w"],
            "team_a_sl": r["team_a_series_l"],
            "team_a_st": r["team_a_series_t"],
            "team_b_sw": r["team_b_series_w"],
            "team_b_sl": r["team_b_series_l"],
            "b_st": r["team_b_series_t"],
            "details": details,
        })

    board_list = []
    for t in STANDINGS_TEST_TEAMS:
        rec = records[t]
        gp = rec["w"] + rec["l"] + rec["t"]
        win_pct = ((rec["w"] + 0.5 * rec["t"]) / gp) if gp > 0 else 0.0
        board_list.append({
            "team": t,
            "w": rec["w"],
            "l": rec["l"],
            "t": rec["t"],
            "win_pct_val": win_pct,
            "win_pct_disp": f"{win_pct:.3f}",
        })

    board_list.sort(key=lambda x: (-x["win_pct_val"], -x["w"], x["l"]))
    leader = board_list[0] if board_list else None

    for item in board_list:
        if not leader or item["team"] == leader["team"]:
            item["gb_disp"] = "0.0"
        else:
            gb = ((leader["w"] - item["w"]) + (item["l"] - leader["l"])) / 2.0
            item["gb_disp"] = f"{max(0.0, gb):.1f}"

        if not leader or item["team"] == leader["team"]:
            item["elim_disp"] = "-"
        else:
            max_opp_wins = leader["w"] + remaining_weeks
            max_self_wins = item["w"] + remaining_weeks
            if max_self_wins < leader["w"]:
                item["elim_disp"] = "淘汰"
            else:
                e_num = (max_opp_wins - item["w"]) - remaining_weeks + 1
                item["elim_disp"] = str(max(0, e_num))

    return board_list, week_history


def get_pending_roster_popup(cursor, team: str, today_str: str | None = None) -> dict | None:
    if team not in STANDINGS_TEST_TEAMS:
        return None
    d_now = (today_str or date.today().isoformat()).strip()
    settle_finished_weeks(cursor, d_now)

    cursor.execute("SELECT * FROM weekly_matchup_results WHERE end_date < ? ORDER BY end_date DESC", (d_now,))
    finished_rows = [dict(r) for r in cursor.fetchall()]
    for r in finished_rows:
        w_key = r["week_key"]
        cursor.execute(
            "SELECT 1 FROM standing_popup_seen WHERE fantasy_team = ? AND week_key = ?",
            (team, w_key),
        )
        if cursor.fetchone() is None:
            try:
                details = json.loads(r.get("details_json") or "[]")
            except Exception:
                details = []
            if team == r["team_a"]:
                my_sw, my_sl, my_st = r["team_a_series_w"], r["team_a_series_l"], r["team_a_series_t"]
                opp_team = r["team_b"]
            else:
                my_sw, my_sl, my_st = r["team_b_series_w"], r["team_b_series_l"], r["team_b_series_t"]
                opp_team = r["team_a"]
            outcome_text = "勝" if my_sw > 0 else ("和" if my_st > 0 else "敗")
            return {
                "week_key": w_key,
                "week_label": r["week_label"],
                "team": team,
                "opponent_team": opp_team,
                "outcome_text": outcome_text,
                "team_a": r["team_a"],
                "team_b": r["team_b"],
                "team_a_cat_w": r["team_a_cat_w"],
                "team_a_cat_l": r["team_a_cat_l"],
                "team_a_cat_t": r["team_a_cat_t"],
                "details": details,
            }
    return None


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
        settlement_popup = get_pending_roster_popup(cursor, team)
        cursor.execute("SELECT week_key, week_label FROM weekly_matchup_results ORDER BY end_date DESC LIMIT 1")
        lf = cursor.fetchone()
        latest_finished_week = dict(lf) if lf else None
        conn.commit()
    except sqlite3.Error:
        settlement_popup = None
        latest_finished_week = None
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
            "settlement_popup": settlement_popup,
            "latest_finished_week": latest_finished_week,
        }
    )


@app.get("/matchup")
def read_matchup(
    request: Request,
    code: str = None,
    opp: str = None,
    stat_date: str = None,
    target_date: str = None,
    week: str = None,
    error: str = None,
):
    clear_free_agent_players()
    logged_team = resolve_logged_team(request, code)
    available_dates = get_available_dates()
    today_dt = date.today()
    today_str = today_dt.isoformat()

    selected_week = week if week in SCHEDULE_WEEK_MAP else get_default_week_for_today(today_str)
    if stat_date:
        selected_date = stat_date if (stat_date == "ALL" or stat_date in available_dates) else "ALL"
    else:
        selected_date = get_default_stat_date(available_dates, today_str)

    upcoming_dates = [(today_dt + timedelta(days=i)).isoformat() for i in range(-1, 8)]
    date_pool = set(available_dates) | set(upcoming_dates)
    try:
        c_tmp = get_db()
        cur_tmp = c_tmp.cursor()
        ensure_extended_schema(cur_tmp)
        cur_tmp.execute("SELECT DISTINCT game_date FROM cpbl_games WHERE game_date IS NOT NULL AND game_date != ''")
        for r_d in cur_tmp.fetchall():
            date_pool.add(str(r_d["game_date"]).strip())
        cur_tmp.execute("SELECT DISTINCT stat_date FROM daily_lineups WHERE stat_date IS NOT NULL AND stat_date != ''")
        for r_d in cur_tmp.fetchall():
            date_pool.add(str(r_d["stat_date"]).strip())
        c_tmp.close()
    except Exception:
        pass
    lineup_dates = sorted(date_pool, reverse=True)
    if target_date and target_date in lineup_dates:
        selected_target_date = target_date
    elif target_date:
        try:
            date.fromisoformat(target_date)
            selected_target_date = target_date
            if selected_target_date not in lineup_dates:
                lineup_dates = sorted(set(lineup_dates) | {selected_target_date}, reverse=True)
        except ValueError:
            selected_target_date = today_str
    else:
        selected_target_date = today_str

    schedule_games = get_schedule_games(selected_week)

    grouped_players = {"ACTIVE": [], "MINORS": [], "IL": [], "SPECIAL": [], "FA": []}
    categories = []
    wins = 0
    losses = 0
    ties = 0

    other_teams = [t for t in ALL_AVAILABLE_TEAMS if t != logged_team]
    opponent_team = opp if opp in other_teams else (other_teams[0] if other_teams else "中信兄弟")

    if logged_team:
        grouped_players = get_team_players_with_stats(logged_team, selected_date, selected_week, selected_target_date)
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
            "auto_fetch_enabled": AUTO_FETCH_ENABLED,
            "auto_fetch_interval_sec": AUTO_FETCH_INTERVAL_SEC,
            "auto_fetch_status": auto_fetch_status,
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
            "lineup_dates": lineup_dates,
            "selected_target_date": selected_target_date,
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

        if not payload.force_override and row["roster_slot"] != "RELEASED" and (row["fantasy_team"] or "").strip():
            return JSONResponse({"ok": False, "error": f"此球員目前屬於【{row['fantasy_team']}】，無法直接簽約"}, status_code=409)

        if not payload.force_override:
            ok_limit, limit_msg = validate_roster_limits(
                cursor, logged_team, payload.player_id, target_roster, target_lineup
            )
            if not ok_limit:
                return JSONResponse({"ok": False, "error": limit_msg}, status_code=400)

        now_iso = datetime.now().isoformat(timespec="seconds")
        reason_text = "強制覆寫新註冊進一軍" if payload.force_override else "新註冊進一軍"
        cursor.execute("""
            UPDATE team_rosters
            SET fantasy_team = ?, roster_slot = ?, lineup_slot = ?, slot_changed_at = ?, status_reason = ?
            WHERE player_id = ?
        """, (logged_team, target_roster, target_lineup, now_iso, reason_text, payload.player_id))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/lineup/move")
def api_lineup_move(request: Request, payload: LineupMovePayload):
    logged_team = resolve_logged_team(request)
    if not logged_team:
        return JSONResponse({"ok": False, "error": "尚未登入"}, status_code=401)

    target_date = (payload.target_date or date.today().isoformat()).strip()
    try:
        date.fromisoformat(target_date)
    except ValueError:
        return JSONResponse({"ok": False, "error": "無效的排賽日期格式（需為 YYYY-MM-DD）"}, status_code=400)

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

        owner_team = (row["fantasy_team"] or "").strip()
        eff_map = get_effective_lineup_map(cursor, logged_team, target_date)
        cur_eff = eff_map.get(payload.player_id, {
            "roster_slot": (row["roster_slot"] or "ACTIVE").upper().strip(),
            "lineup_slot": normalize_lineup_slot(row["lineup_slot"]),
        })

        # 先發名單合法性驗證：非本隊名單內、非 ACTIVE 或非冬盟出賽球員不得排入先發槽位
        if new_roster == "ACTIVE" and new_lineup in SCORING_LINEUP_SLOTS and not payload.force_override:
            if owner_team != logged_team or (row["roster_slot"] or "").upper().strip() == "RELEASED":
                return JSONResponse({"ok": False, "error": "僅本隊名單內球員可排入先發槽位"}, status_code=400)
            if cur_eff.get("roster_slot") != "ACTIVE":
                return JSONResponse({"ok": False, "error": "僅出賽狀態（ACTIVE）球員可排入先發槽位"}, status_code=400)

            pid_str = (row["player_id"] or "").strip()
            cursor.execute(
                "SELECT 1 FROM player_game_stats WHERE player_id = ? AND game_key LIKE '2026_P_%' LIMIT 1",
                (pid_str,),
            )
            has_awb_game = cursor.fetchone() is not None
            if not pid_str.startswith("AWB_") and not has_awb_game:
                return JSONResponse(
                    {"ok": False, "error": f"球員【{row['name']}】非冬盟名單／出賽球員，不得排入先發槽位"},
                    status_code=400,
                )

        if not payload.force_override:
            locked, lock_msg = is_game_locked_for_team(cursor, row["real_team"], target_date)
            if locked:
                return JSONResponse({"ok": False, "error": lock_msg}, status_code=400)

            lock_info = calc_lock_info(row["roster_slot"], row["slot_changed_at"])
            if lock_info["is_locked"] and new_roster != row["roster_slot"]:
                return JSONResponse({
                    "ok": False,
                    "error": f"球員處於 {LOCK_DAYS} 天名單鎖定中（剩餘 {lock_info['days_remaining']} 天）"
                }, status_code=400)

        occupant_id = None
        if new_roster == "ACTIVE" and new_lineup in SCORING_LINEUP_SLOTS:
            for pid, info in eff_map.items():
                if pid != payload.player_id and info.get("roster_slot") == "ACTIVE" and normalize_lineup_slot(info.get("lineup_slot")) == new_lineup:
                    occupant_id = pid
                    break

        occupant = None
        if occupant_id:
            cursor.execute("""
                SELECT p.player_id, p.name, p.real_team, p.position, p.positions,
                       r.roster_slot, r.lineup_slot, r.slot_changed_at
                FROM players p
                JOIN team_rosters r ON p.player_id = r.player_id
                WHERE p.player_id = ?
            """, (occupant_id,))
            occupant = cursor.fetchone()

        if new_roster == "ACTIVE" and not payload.force_override:
            pos_set = parse_player_positions(row["positions"], row["position"])
            ok_pos, pos_msg = is_lineup_slot_eligible(new_lineup, pos_set)
            if not ok_pos:
                return JSONResponse({"ok": False, "error": pos_msg}, status_code=400)

        old_lineup = normalize_lineup_slot(cur_eff.get("lineup_slot") or row["lineup_slot"])
        now_iso = datetime.now().isoformat(timespec="seconds")

        if occupant:
            if not payload.force_override:
                occ_locked, occ_lock_msg = is_game_locked_for_team(cursor, occupant["real_team"], target_date)
                if occ_locked:
                    return JSONResponse({"ok": False, "error": f"目標守位球員：{occ_lock_msg}"}, status_code=400)

                occ_pos_set = parse_player_positions(occupant["positions"], occupant["position"])
                if old_lineup in SCORING_LINEUP_SLOTS:
                    ok_occ_pos, occ_pos_msg = is_lineup_slot_eligible(old_lineup, occ_pos_set)
                    if not ok_occ_pos:
                        return JSONResponse({"ok": False, "error": f"無法直接互換（{occupant['name']}）：{occ_pos_msg}"}, status_code=400)

                ok_lim, lim_msg = validate_roster_limits(
                    cursor, logged_team, payload.player_id, new_roster, new_lineup, ignore_slot_conflict_with=occupant["player_id"]
                )
                if not ok_lim:
                    return JSONResponse({"ok": False, "error": lim_msg}, status_code=400)

            occ_target_slot = old_lineup if old_lineup in SCORING_LINEUP_SLOTS else "BN"
            eff_map[payload.player_id] = {"roster_slot": "ACTIVE", "lineup_slot": new_lineup}
            eff_map[occupant["player_id"]] = {"roster_slot": "ACTIVE", "lineup_slot": occ_target_slot}
            save_daily_lineup_snapshot(cursor, logged_team, target_date, eff_map)

            if target_date >= date.today().isoformat():
                cursor.execute("""
                    UPDATE team_rosters
                    SET fantasy_team = ?, roster_slot = 'ACTIVE', lineup_slot = ?,
                        slot_changed_at = ?, status_reason = ?
                    WHERE player_id = ?
                """, (logged_team, new_lineup, now_iso, payload.reason or "", payload.player_id))
                cursor.execute("""
                    UPDATE team_rosters
                    SET lineup_slot = ?
                    WHERE player_id = ?
                """, (occ_target_slot, occupant["player_id"]))

            conn.commit()
            return JSONResponse({"ok": True, "swapped_with": occupant["name"], "target_date": target_date})

        if not payload.force_override:
            ok_lim, lim_msg = validate_roster_limits(cursor, logged_team, payload.player_id, new_roster, new_lineup)
            if not ok_lim:
                return JSONResponse({"ok": False, "error": lim_msg}, status_code=400)

        if new_roster == "RELEASED":
            eff_map.pop(payload.player_id, None)
            save_daily_lineup_snapshot(cursor, logged_team, target_date, eff_map)
            if target_date >= date.today().isoformat():
                cursor.execute("""
                    UPDATE team_rosters
                    SET fantasy_team = '', roster_slot = 'RELEASED', lineup_slot = 'BN',
                        slot_changed_at = ?, status_reason = '釋出至自由球員'
                    WHERE player_id = ?
                """, (now_iso, payload.player_id))
        else:
            final_lineup = new_lineup if new_roster == "ACTIVE" else "BN"
            eff_map[payload.player_id] = {"roster_slot": new_roster, "lineup_slot": final_lineup}
            save_daily_lineup_snapshot(cursor, logged_team, target_date, eff_map)
            if target_date >= date.today().isoformat():
                cursor.execute("""
                    UPDATE team_rosters
                    SET fantasy_team = ?, roster_slot = ?, lineup_slot = ?,
                        slot_changed_at = ?, status_reason = ?
                    WHERE player_id = ?
                """, (logged_team, new_roster, final_lineup, now_iso, payload.reason or "", payload.player_id))

        conn.commit()
        return JSONResponse({"ok": True, "target_date": target_date})
    finally:
        conn.close()


@app.post("/api/admin/force-move")
def api_admin_force_move(request: Request, payload: AdminForceMovePayload):
    denied = ensure_admin_enabled()
    if denied:
        return denied

    new_roster = (payload.roster_slot or "ACTIVE").upper()
    new_lineup = normalize_lineup_slot(payload.lineup_slot)
    if new_roster not in VALID_SLOTS:
        return JSONResponse({"ok": False, "error": "無效的名單狀態"}, status_code=400)

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            SELECT p.player_id, p.name, r.fantasy_team, r.roster_slot, r.lineup_slot
            FROM players p
            JOIN team_rosters r ON p.player_id = r.player_id
            WHERE p.player_id = ?
        """, (payload.player_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "找不到球員"}, status_code=404)

        target_team = (
            ""
            if new_roster == "RELEASED"
            else (
                payload.fantasy_team.strip()
                if payload.fantasy_team is not None
                else (row["fantasy_team"] or resolve_logged_team(request) or "")
            )
        )
        final_lineup = new_lineup if new_roster == "ACTIVE" else "BN"
        now_iso = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            UPDATE team_rosters
            SET fantasy_team = ?, roster_slot = ?, lineup_slot = ?,
                slot_changed_at = ?, status_reason = ?
            WHERE player_id = ?
        """, (target_team, new_roster, final_lineup, now_iso, payload.reason or "後台強制覆寫調度", payload.player_id))
        conn.commit()
        return JSONResponse({
            "ok": True,
            "forced": True,
            "player_id": payload.player_id,
            "fantasy_team": target_team,
            "roster_slot": new_roster,
            "lineup_slot": final_lineup,
        })
    finally:
        conn.close()


@app.post("/api/admin/force-sign")
def api_admin_force_sign(payload: AdminForceSignPayload):
    denied = ensure_admin_enabled()
    if denied:
        return denied

    fteam = (payload.fantasy_team or "").strip()
    if not fteam:
        return JSONResponse({"ok": False, "error": "請指定目標 Fantasy 隊伍"}, status_code=400)

    target_lineup = normalize_lineup_slot(payload.lineup_slot)
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("SELECT player_id FROM players WHERE player_id = ?", (payload.player_id,))
        if not cursor.fetchone():
            return JSONResponse({"ok": False, "error": "找不到該名球員"}, status_code=404)

        now_iso = datetime.now().isoformat(timespec="seconds")
        cursor.execute("""
            INSERT INTO team_rosters (player_id, fantasy_team, roster_slot, lineup_slot, slot_changed_at, status_reason)
            VALUES (?, ?, 'ACTIVE', ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                fantasy_team = excluded.fantasy_team,
                roster_slot = 'ACTIVE',
                lineup_slot = excluded.lineup_slot,
                slot_changed_at = excluded.slot_changed_at,
                status_reason = excluded.status_reason
        """, (payload.player_id, fteam, target_lineup, now_iso, payload.reason or "後台強制覆寫簽約"))
        conn.commit()
        return JSONResponse({
            "ok": True,
            "forced": True,
            "player_id": payload.player_id,
            "fantasy_team": fteam,
            "roster_slot": "ACTIVE",
            "lineup_slot": target_lineup,
        })
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
        "2026-11-23_2026-11-29" if gdate <= "2026-11-29" else "2026-11-30_2026-12-04"
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
            (payload.kind_code or "P").strip(),
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


@app.post("/api/admin/auto-fetch/run")
def api_run_auto_fetch():
    denied = ensure_admin_enabled()
    if denied:
        return denied

    res = run_auto_fetch_due_games(force_all=False, sync_schedule=True)
    return JSONResponse(res)


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
    target_date = (payload.stat_date or "").strip() or "2026-11-23"

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
                            player_id, stat_date, ip, er, k, w, hld, sv, pitch_h, pitch_bb, pitch_hbp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(player_id, stat_date) DO UPDATE SET
                            ip = excluded.ip, er = excluded.er, k = excluded.k,
                            w = excluded.w, hld = excluded.hld, sv = excluded.sv,
                            pitch_h = excluded.pitch_h, pitch_bb = excluded.pitch_bb, pitch_hbp = excluded.pitch_hbp
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
                        0,
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


@app.post("/api/admin/cleanup-stale-games")
def api_cleanup_stale_games():
    denied = ensure_admin_enabled()
    if denied:
        return denied

    res = cleanup_stale_winter_games(db_path="fantasy_league.db")
    return JSONResponse(res)


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

@app.get("/standings")
def read_standings(request: Request, team: str = "台灣山林", sim_date: str | None = None):
    selected_team = team if team in STANDINGS_TEST_TEAMS else "台灣山林"
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        if sim_date:
            settle_finished_weeks(cursor, sim_date)
        board, week_history = build_standings_board(cursor, sim_date)
        conn.commit()
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="standings.html",
        context={
            "active_page": "standings",
            "enable_admin": ENABLE_ADMIN,
            "teams": STANDINGS_TEST_TEAMS,
            "current_team": selected_team,
            "standings_board": board,
            "week_history": week_history,
            "schedule_weeks": SCHEDULE_WEEKS,
            "sim_date": sim_date or "",
        },
    )


@app.post("/api/standings/dismiss-popup")
async def api_dismiss_standing_popup(request: Request):
    data = await request.json()
    team = (data.get("team") or "").strip()
    week_key = (data.get("week_key") or "").strip()
    if team not in STANDINGS_TEST_TEAMS or not week_key:
        return JSONResponse({"ok": False, "error": "參數無效"}, status_code=400)

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        cursor.execute("""
            INSERT OR IGNORE INTO standing_popup_seen (fantasy_team, week_key, seen_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (team, week_key))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/standings/settle")
async def api_settle_standings(request: Request):
    data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    sim_date = (data.get("sim_date") or "").strip() or None
    force_week_key = (data.get("week_key") or "").strip() or None

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        settled = settle_finished_weeks(cursor, sim_date, force_week_key=force_week_key)
        conn.commit()
        return JSONResponse({"ok": True, "settled": settled})
    finally:
        conn.close()


# === DRAFT SYSTEM START ===
import json as _draft_json
import random as _draft_random
import time as _draft_time
from datetime import datetime as _draft_dt, timezone as _draft_tz
from fastapi import Body

DRAFT_TOTAL_ROSTER_CAP = 60


def _draft_now_ts() -> float:
    return _draft_time.time()


def _draft_now_iso() -> str:
    return _draft_dt.now(_draft_tz.utc).isoformat()


def ensure_draft_schema(cursor):

    try:
        cursor.execute("PRAGMA table_info(draft_rooms)")
        dcols = {r[1] for r in cursor.fetchall()}
        if "room_password" not in dcols:
            cursor.execute("ALTER TABLE draft_rooms ADD COLUMN room_password TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS draft_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            room_password TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'AUCTION',
            target_slot TEXT NOT NULL DEFAULT 'ACTIVE',
            picks_per_team INTEGER NOT NULL DEFAULT 25,
            budget_per_team INTEGER NOT NULL DEFAULT 260,
            turn_seconds INTEGER NOT NULL DEFAULT 30,
            bid_extend_seconds INTEGER NOT NULL DEFAULT 10,
            pool_mode TEXT NOT NULL DEFAULT 'FA',
            custom_pool_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            current_round INTEGER NOT NULL DEFAULT 1,
            current_turn_idx INTEGER NOT NULL DEFAULT 0,
            current_player_id TEXT DEFAULT '',
            current_nominator TEXT DEFAULT '',
            current_high_bid INTEGER NOT NULL DEFAULT 0,
            current_high_team TEXT DEFAULT '',
            deadline_ts REAL NOT NULL DEFAULT 0,
            rosters_imported INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS draft_teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            fantasy_team TEXT NOT NULL,
            draft_order INTEGER NOT NULL,
            remaining_budget INTEGER NOT NULL DEFAULT 260,
            drafted_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(room_id, fantasy_team)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS draft_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            pick_no INTEGER NOT NULL,
            round_no INTEGER NOT NULL,
            fantasy_team TEXT NOT NULL,
            player_id TEXT NOT NULL,
            target_slot TEXT NOT NULL DEFAULT 'ACTIVE',
            price INTEGER NOT NULL DEFAULT 0,
            is_auto INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS draft_bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            player_id TEXT NOT NULL,
            fantasy_team TEXT NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)


def _draft_get_roster_counts(cursor, fantasy_team: str) -> tuple[int, int]:
    cursor.execute("""
        SELECT roster_slot, COUNT(*) AS cnt
        FROM team_rosters
        WHERE fantasy_team = ?
        GROUP BY roster_slot
    """, (fantasy_team,))
    active_cnt = 0
    minors_cnt = 0
    for row in cursor.fetchall():
        slot = (row["roster_slot"] if hasattr(row, "__getitem__") else row[0]) or ""
        cnt = int(row["cnt"] if hasattr(row, "__getitem__") else row[1])
        if slot == "ACTIVE":
            active_cnt += cnt
        elif slot == "MINORS":
            minors_cnt += cnt
    return active_cnt, minors_cnt


def _draft_can_add_to_slot(cursor, fantasy_team: str, target_slot: str, extra_picks_in_room: int = 0) -> tuple[bool, str]:
    active_cnt, minors_cnt = _draft_get_roster_counts(cursor, fantasy_team)
    total_after = active_cnt + minors_cnt + 1 + max(0, extra_picks_in_room)
    if total_after > DRAFT_TOTAL_ROSTER_CAP:
        return False, f"{fantasy_team} 一二軍合計已達上限 {DRAFT_TOTAL_ROSTER_CAP} 人"
    if target_slot == "ACTIVE":
        cap = globals().get("MAX_ACTIVE_ROSTER", 25)
        if active_cnt + 1 > cap:
            return False, f"{fantasy_team} 一軍已達上限 {cap} 人"
    return True, ""


def _draft_available_players(cursor, room: dict) -> list[dict]:
    cursor.execute("SELECT player_id FROM draft_picks WHERE room_id = ?", (room["id"],))
    picked_ids = {r[0] for r in cursor.fetchall()}

    cursor.execute("SELECT player_id FROM team_rosters WHERE COALESCE(fantasy_team, '') != ''")
    rostered_ids = {r[0] for r in cursor.fetchall()}

    pool_mode = room["pool_mode"] or "FA"
    custom_ids = []
    try:
        raw_list = _draft_json.loads(room["custom_pool_json"] or "[]")
        if isinstance(raw_list, list):
            custom_ids = [str(x).strip() for x in raw_list if str(x).strip()]
    except Exception:
        custom_ids = []

    cursor.execute("PRAGMA table_info(players)")
    cols = {r[1] for r in cursor.fetchall()}
    name_col = "name" if "name" in cols else ("player_name" if "player_name" in cols else "player_id")
    pos_col = "pos" if "pos" in cols else ("position" if "position" in cols else "")
    team_col = "team" if "team" in cols else ("mlb_team" if "mlb_team" in cols else "")

    pos_expr = pos_col if pos_col else "''"
    team_expr = team_col if team_col else "''"
    cursor.execute(f"SELECT player_id, {name_col} AS pname, {pos_expr} AS ppos, {team_expr} AS pteam FROM players")

    result = []
    custom_set = set(custom_ids)
    for r in cursor.fetchall():
        pid = str(r[0])
        if pid in picked_ids:
            continue
        if pool_mode == "FA" and pid in rostered_ids:
            continue
        if pool_mode == "CUSTOM" and pid not in custom_set:
            continue
        result.append({
            "player_id": pid,
            "name": str(r[1] or pid),
            "pos": str(r[2] or ""),
            "team": str(r[3] or ""),
        })
    if pool_mode == "CUSTOM" and custom_ids:
        order_map = {pid: idx for idx, pid in enumerate(custom_ids)}
        result.sort(key=lambda x: (order_map.get(x["player_id"], 999999), x["player_id"]))
    else:
        result.sort(key=lambda x: (x["pos"], x["name"], x["player_id"]))
    return result


def _draft_turn_team(teams: list[dict], mode: str, round_no: int, turn_idx: int) -> dict | None:
    if not teams:
        return None
    n = len(teams)
    idx = turn_idx % n
    if mode == "SNAKE" and (round_no % 2 == 0):
        idx = (n - 1) - idx
    return teams[idx]


def _draft_max_bid(team_row: dict, room: dict) -> int:
    if room["mode"] != "AUCTION":
        return 0
    rem_budget = int(team_row["remaining_budget"])
    rem_slots = max(1, int(room["picks_per_team"]) - int(team_row["drafted_count"]))
    return max(0, rem_budget - (rem_slots - 1))


def _draft_record_pick(cursor, room: dict, team_row: dict, player_id: str, price: int, is_auto: int):
    cursor.execute("SELECT COALESCE(MAX(pick_no), 0) + 1 FROM draft_picks WHERE room_id = ?", (room["id"],))
    next_pick_no = int(cursor.fetchone()[0])
    cursor.execute("""
        INSERT INTO draft_picks (room_id, pick_no, round_no, fantasy_team, player_id, target_slot, price, is_auto, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        room["id"], next_pick_no, room["current_round"], team_row["fantasy_team"],
        player_id, room["target_slot"], int(price), int(is_auto), _draft_now_iso()
    ))
    new_drafted = int(team_row["drafted_count"]) + 1
    new_budget = int(team_row["remaining_budget"]) - int(price)
    cursor.execute("""
        UPDATE draft_teams
        SET drafted_count = ?, remaining_budget = ?
        WHERE id = ?
    """, (new_drafted, new_budget, team_row["id"]))


def _draft_advance_turn(cursor, room: dict, teams: list[dict], now_ts: float):
    all_done = all(int(t["drafted_count"]) >= int(room["picks_per_team"]) for t in teams)
    avail = _draft_available_players(cursor, room)
    if all_done or not avail:
        cursor.execute("""
            UPDATE draft_rooms
            SET status = 'FINISHED', current_player_id = '', current_nominator = '',
                current_high_bid = 0, current_high_team = '', deadline_ts = 0
            WHERE id = ?
        """, (room["id"],))
        _draft_import_to_rosters(cursor, room["id"])
        return

    n = len(teams)
    next_turn_idx = int(room["current_turn_idx"]) + 1
    next_round = int(room["current_round"])
    if next_turn_idx >= n:
        next_turn_idx = 0
        next_round += 1

    checked = 0
    while checked < n * 2:
        cand = _draft_turn_team(teams, room["mode"], next_round, next_turn_idx)
        if cand and int(cand["drafted_count"]) < int(room["picks_per_team"]):
            break
        next_turn_idx += 1
        if next_turn_idx >= n:
            next_turn_idx = 0
            next_round += 1
        checked += 1

    turn_team = _draft_turn_team(teams, room["mode"], next_round, next_turn_idx)
    nom_team = turn_team["fantasy_team"] if (room["mode"] == "AUCTION" and turn_team) else ""
    deadline = now_ts + int(room["turn_seconds"])

    cursor.execute("""
        UPDATE draft_rooms
        SET current_round = ?, current_turn_idx = ?, current_player_id = '',
            current_nominator = ?, current_high_bid = 0, current_high_team = '',
            deadline_ts = ?
        WHERE id = ?
    """, (next_round, next_turn_idx, nom_team, deadline, room["id"]))


def _draft_process_tick(cursor, room_id: int):
    cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
    r_row = cursor.fetchone()
    if not r_row:
        return
    room = dict(r_row)
    if room["status"] != "ACTIVE":
        return

    now_ts = _draft_now_ts()
    deadline = float(room["deadline_ts"] or 0)
    if deadline <= 0 or now_ts < deadline:
        return

    cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? ORDER BY draft_order ASC", (room_id,))
    teams = [dict(x) for x in cursor.fetchall()]
    if not teams:
        return

    avail = _draft_available_players(cursor, room)
    if not avail or all(int(t["drafted_count"]) >= int(room["picks_per_team"]) for t in teams):
        cursor.execute("UPDATE draft_rooms SET status = 'FINISHED', deadline_ts = 0 WHERE id = ?", (room_id,))
        _draft_import_to_rosters(cursor, room_id)
        return

    if room["mode"] == "AUCTION":
        pid = room["current_player_id"] or ""
        high_team = room["current_high_team"] or room["current_nominator"] or ""
        high_bid = int(room["current_high_bid"] or 1)
        if pid and high_team:
            t_map = {t["fantasy_team"]: t for t in teams}
            winner = t_map.get(high_team)
            if winner and int(winner["drafted_count"]) < int(room["picks_per_team"]):
                max_b = _draft_max_bid(winner, room)
                final_price = min(max(1, high_bid), max_b) if max_b > 0 else 0
                if final_price > 0 or room["budget_per_team"] == 0:
                    _draft_record_pick(cursor, room, winner, pid, final_price, is_auto=0)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        room = dict(cursor.fetchone())
        cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? ORDER BY draft_order ASC", (room_id,))
        teams = [dict(x) for x in cursor.fetchall()]
        _draft_advance_turn(cursor, room, teams, now_ts)
    else:
        turn_team = _draft_turn_team(teams, room["mode"], int(room["current_round"]), int(room["current_turn_idx"]))
        if turn_team and int(turn_team["drafted_count"]) < int(room["picks_per_team"]):
            chosen = _draft_random.choice(avail)
            _draft_record_pick(cursor, room, turn_team, chosen["player_id"], 0, is_auto=1)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        room = dict(cursor.fetchone())
        cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? ORDER BY draft_order ASC", (room_id,))
        teams = [dict(x) for x in cursor.fetchall()]
        _draft_advance_turn(cursor, room, teams, now_ts)


def _draft_import_to_rosters(cursor, room_id: int):
    cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
    r_row = cursor.fetchone()
    if not r_row:
        return 0
    room = dict(r_row)
    cursor.execute("SELECT * FROM draft_picks WHERE room_id = ? ORDER BY pick_no ASC", (room_id,))
    picks = [dict(x) for x in cursor.fetchall()]
    imported = 0
    now_iso = _draft_now_iso()
    for p in picks:
        fteam = p["fantasy_team"]
        pid = p["player_id"]
        tslot = p["target_slot"] or "ACTIVE"
        ok, _ = _draft_can_add_to_slot(cursor, fteam, tslot, extra_picks_in_room=0)
        if not ok:
            if tslot == "ACTIVE":
                ok_m, _ = _draft_can_add_to_slot(cursor, fteam, "MINORS", extra_picks_in_room=0)
                if ok_m:
                    tslot = "MINORS"
                else:
                    continue
            else:
                continue
        reason = f"選秀入隊({room['title']} #{p['pick_no']})"
        cursor.execute("""
            INSERT INTO team_rosters (player_id, fantasy_team, roster_slot, lineup_slot, slot_changed_at, status_reason)
            VALUES (?, ?, ?, 'BN', ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                fantasy_team = excluded.fantasy_team,
                roster_slot = excluded.roster_slot,
                lineup_slot = 'BN',
                slot_changed_at = excluded.slot_changed_at,
                status_reason = excluded.status_reason
        """, (pid, fteam, tslot, now_iso, reason))
        imported += 1
    cursor.execute("UPDATE draft_rooms SET rosters_imported = 1 WHERE id = ?", (room_id,))
    return imported


def _draft_build_state(cursor, room_id: int) -> dict:
    _draft_process_tick(cursor, room_id)
    cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
    r_row = cursor.fetchone()
    if not r_row:
        return {"error": "找不到選秀房間"}
    room = dict(r_row)

    cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? ORDER BY draft_order ASC", (room_id,))
    teams = [dict(x) for x in cursor.fetchall()]

    avail = _draft_available_players(cursor, room)
    avail_map = {p["player_id"]: p for p in avail}

    cursor.execute("""
        SELECT dp.*, COALESCE(pl.name, dp.player_id) AS player_name
        FROM draft_picks dp
        LEFT JOIN players pl ON pl.player_id = dp.player_id
        WHERE dp.room_id = ?
        ORDER BY dp.pick_no DESC
    """, (room_id,))
    picks = [dict(x) for x in cursor.fetchall()]

    cursor.execute("""
        SELECT db.*, COALESCE(pl.name, db.player_id) AS player_name
        FROM draft_bids db
        LEFT JOIN players pl ON pl.player_id = db.player_id
        WHERE db.room_id = ?
        ORDER BY db.id DESC
        LIMIT 30
    """, (room_id,))
    bids = [dict(x) for x in cursor.fetchall()]

    turn_team = _draft_turn_team(teams, room["mode"], int(room["current_round"]), int(room["current_turn_idx"]))
    enriched_teams = []
    for t in teams:
        act_c, min_c = _draft_get_roster_counts(cursor, t["fantasy_team"])
        t_copy = dict(t)
        t_copy["active_roster_count"] = act_c
        t_copy["minors_roster_count"] = min_c
        t_copy["total_roster_count"] = act_c + min_c
        t_copy["max_bid"] = _draft_max_bid(t, room)
        enriched_teams.append(t_copy)

    now_ts = _draft_now_ts()
    rem_sec = max(0, int(round(float(room["deadline_ts"] or 0) - now_ts))) if room["status"] == "ACTIVE" else 0
    cur_player = avail_map.get(room["current_player_id"]) if room["current_player_id"] else None
    if not cur_player and room["current_player_id"]:
        cursor.execute("SELECT player_id, name FROM players WHERE player_id = ?", (room["current_player_id"],))
        pr = cursor.fetchone()
        if pr:
            cur_player = {"player_id": pr[0], "name": pr[1] or pr[0], "pos": "", "team": ""}

    return {
        "room": room,
        "teams": enriched_teams,
        "turn_team": turn_team["fantasy_team"] if turn_team else "",
        "remaining_seconds": rem_sec,
        "current_player": cur_player,
        "available_players": avail,
        "picks": picks,
        "recent_bids": bids,
        "server_ts": now_ts,
    }

@app.get("/api/draft/rooms")
async def api_draft_rooms():
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        cursor.execute("SELECT * FROM draft_rooms ORDER BY id DESC")
        rooms = [dict(r) for r in cursor.fetchall()]
        conn.commit()
        return JSONResponse({"ok": True, "rooms": rooms})
    finally:
        conn.close()


@app.post("/api/draft/rooms")
async def api_draft_create_room(payload: dict = Body(...)):
    title = (payload.get("title") or "新選秀房").strip()
    room_password = str(payload.get("room_password") or "").strip()
    mode = (payload.get("mode") or "AUCTION").upper()
    if mode not in {"AUCTION", "SNAKE", "LINEAR"}:
        mode = "AUCTION"
    target_slot = (payload.get("target_slot") or ("ACTIVE" if mode == "AUCTION" else "MINORS")).upper()
    if target_slot not in {"ACTIVE", "MINORS"}:
        target_slot = "ACTIVE"
    picks_per_team = max(1, min(60, int(payload.get("picks_per_team") or 25)))
    default_budget = max(0, int(payload.get("budget_per_team") or 260))
    turn_seconds = max(5, min(600, int(payload.get("turn_seconds") or 30)))
    bid_extend_seconds = max(3, min(120, int(payload.get("bid_extend_seconds") or 10)))
    pool_mode = (payload.get("pool_mode") or "FA").upper()
    if pool_mode not in {"FA", "CUSTOM"}:
        pool_mode = "FA"

    raw_custom = payload.get("custom_pool_ids") or []
    if isinstance(raw_custom, str):
        custom_ids = [x.strip() for x in raw_custom.replace(",", "\n").splitlines() if x.strip()]
    else:
        custom_ids = [str(x).strip() for x in raw_custom if str(x).strip()]

    team_order = payload.get("team_order") or []
    if not isinstance(team_order, list) or not team_order:
        team_order = ["台灣山林", "台灣海洋"]
    team_order = [str(t).strip() for t in team_order if str(t).strip()]

    raw_team_budgets = payload.get("team_budgets") or {}
    per_team_budget_map = {}
    if isinstance(raw_team_budgets, dict):
        for k, v in raw_team_budgets.items():
            tkey = str(k).strip()
            if tkey and v is not None and str(v).strip() != "":
                try:
                    per_team_budget_map[tkey] = max(0, int(v))
                except Exception:
                    pass
    elif isinstance(raw_team_budgets, list):
        for idx, v in enumerate(raw_team_budgets):
            if idx < len(team_order) and v is not None and str(v).strip() != "":
                try:
                    per_team_budget_map[team_order[idx]] = max(0, int(v))
                except Exception:
                    pass

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        now_ts = _draft_now_ts()
        nom_team = team_order[0] if mode == "AUCTION" else ""
        cursor.execute("""
            INSERT INTO draft_rooms (
                title, room_password, mode, target_slot, picks_per_team, budget_per_team,
                turn_seconds, bid_extend_seconds, pool_mode, custom_pool_json,
                status, current_round, current_turn_idx, current_nominator,
                deadline_ts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LOBBY', 1, 0, ?, ?, ?)
        """, (
            title, room_password, mode, target_slot, picks_per_team, default_budget,
            turn_seconds, bid_extend_seconds, pool_mode, _draft_json.dumps(custom_ids, ensure_ascii=False),
            nom_team, now_ts + turn_seconds, _draft_now_iso()
        ))
        room_id = cursor.lastrowid
        for idx, tname in enumerate(team_order, start=1):
            if mode == "AUCTION":
                init_budget = per_team_budget_map.get(tname, default_budget)
            else:
                init_budget = 0
            cursor.execute("""
                INSERT INTO draft_teams (room_id, fantasy_team, draft_order, remaining_budget, drafted_count)
                VALUES (?, ?, ?, ?, 0)
            """, (room_id, tname, idx, init_budget))
        conn.commit()
        return JSONResponse({"ok": True, "room_id": room_id})
    finally:
        conn.close()



@app.post("/api/draft/rooms/{room_id}/auth")
async def api_draft_auth(room_id: int, payload: dict = Body(...)):
    fteam = (payload.get("fantasy_team") or "").strip()
    pwd = str(payload.get("password") or "").strip()
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        cursor.execute("SELECT id, title, room_password FROM draft_rooms WHERE id = ?", (room_id,))
        r_row = cursor.fetchone()
        if not r_row:
            return JSONResponse({"ok": False, "error": "找不到選秀房間"}, status_code=404)
        expected_pwd = str(r_row["room_password"] if hasattr(r_row, "__getitem__") else r_row[2] or "").strip()
        if expected_pwd and pwd != expected_pwd:
            return JSONResponse({"ok": False, "error": "選秀房密碼錯誤"})
        cursor.execute("SELECT fantasy_team FROM draft_teams WHERE room_id = ?", (room_id,))
        valid_teams = {str(x[0]).strip() for x in cursor.fetchall()}
        if not fteam or fteam not in valid_teams:
            return JSONResponse({"ok": False, "error": "請選擇本房間有效的隊伍名稱"})
        return JSONResponse({"ok": True, "fantasy_team": fteam})
    finally:
        conn.close()


@app.get("/api/draft/rooms/{room_id}/state")
async def api_draft_state(room_id: int):
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        state = _draft_build_state(cursor, room_id)
        conn.commit()
        if "error" in state:
            return JSONResponse({"ok": False, "error": state["error"]}, status_code=404)
        return JSONResponse({"ok": True, **state})
    finally:
        conn.close()


@app.post("/api/draft/rooms/{room_id}/nominate")
async def api_draft_nominate(room_id: int, payload: dict = Body(...)):
    fteam = (payload.get("fantasy_team") or "").strip()
    player_id = (payload.get("player_id") or "").strip()
    start_bid = max(1, int(payload.get("start_bid") or 1))

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        _draft_process_tick(cursor, room_id)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        r_row = cursor.fetchone()
        if not r_row:
            return JSONResponse({"ok": False, "error": "找不到房間"}, status_code=404)
        room = dict(r_row)
        if room["status"] != "ACTIVE" or room["mode"] != "AUCTION":
            return JSONResponse({"ok": False, "error": "目前不是競標進行中"})
        if room["current_player_id"]:
            return JSONResponse({"ok": False, "error": "目前已有競標中的球員"})
        if room["current_nominator"] and fteam != room["current_nominator"]:
            return JSONResponse({"ok": False, "error": f"目前由 {room['current_nominator']} 負責提名"})

        cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? AND fantasy_team = ?", (room_id, fteam))
        t_row = cursor.fetchone()
        if not t_row:
            return JSONResponse({"ok": False, "error": "無效的球隊"})
        t_dict = dict(t_row)
        max_b = _draft_max_bid(t_dict, room)
        if start_bid > max_b:
            return JSONResponse({"ok": False, "error": f"起標價 {start_bid} 超過可出上限 {max_b} 元"})

        ok_slot, slot_msg = _draft_can_add_to_slot(cursor, fteam, room["target_slot"], extra_picks_in_room=0)
        if not ok_slot:
            return JSONResponse({"ok": False, "error": slot_msg})

        avail_ids = {p["player_id"] for p in _draft_available_players(cursor, room)}
        if player_id not in avail_ids:
            return JSONResponse({"ok": False, "error": "該球員不可選或已被選走"})

        now_ts = _draft_now_ts()
        deadline = now_ts + int(room["bid_extend_seconds"])
        cursor.execute("""
            UPDATE draft_rooms
            SET current_player_id = ?, current_high_bid = ?, current_high_team = ?, deadline_ts = ?
            WHERE id = ?
        """, (player_id, start_bid, fteam, deadline, room_id))
        cursor.execute("""
            INSERT INTO draft_bids (room_id, player_id, fantasy_team, amount, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (room_id, player_id, fteam, start_bid, _draft_now_iso()))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/draft/rooms/{room_id}/bid")
async def api_draft_bid(room_id: int, payload: dict = Body(...)):
    fteam = (payload.get("fantasy_team") or "").strip()
    amount = int(payload.get("amount") or 0)

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        _draft_process_tick(cursor, room_id)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        r_row = cursor.fetchone()
        if not r_row:
            return JSONResponse({"ok": False, "error": "找不到房間"}, status_code=404)
        room = dict(r_row)
        if room["status"] != "ACTIVE" or room["mode"] != "AUCTION" or not room["current_player_id"]:
            return JSONResponse({"ok": False, "error": "目前沒有進行中的競標"})

        min_req = int(room["current_high_bid"]) + 1
        if amount < min_req:
            return JSONResponse({"ok": False, "error": f"出價需至少 {min_req} 元"})

        cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? AND fantasy_team = ?", (room_id, fteam))
        t_row = cursor.fetchone()
        if not t_row:
            return JSONResponse({"ok": False, "error": "無效的球隊"})
        t_dict = dict(t_row)
        max_b = _draft_max_bid(t_dict, room)
        if amount > max_b:
            return JSONResponse({"ok": False, "error": f"超過可出上限（剩餘預算扣掉保留名額後最高 {max_b} 元）"})

        ok_slot, slot_msg = _draft_can_add_to_slot(cursor, fteam, room["target_slot"], extra_picks_in_room=0)
        if not ok_slot:
            return JSONResponse({"ok": False, "error": slot_msg})

        now_ts = _draft_now_ts()
        deadline = now_ts + int(room["bid_extend_seconds"])
        cursor.execute("""
            UPDATE draft_rooms
            SET current_high_bid = ?, current_high_team = ?, deadline_ts = ?
            WHERE id = ?
        """, (amount, fteam, deadline, room_id))
        cursor.execute("""
            INSERT INTO draft_bids (room_id, player_id, fantasy_team, amount, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (room_id, room["current_player_id"], fteam, amount, _draft_now_iso()))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.post("/api/draft/rooms/{room_id}/pick")
async def api_draft_pick(room_id: int, payload: dict = Body(...)):
    fteam = (payload.get("fantasy_team") or "").strip()
    player_id = (payload.get("player_id") or "").strip()

    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        _draft_process_tick(cursor, room_id)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        r_row = cursor.fetchone()
        if not r_row:
            return JSONResponse({"ok": False, "error": "找不到房間"}, status_code=404)
        room = dict(r_row)
        if room["status"] != "ACTIVE" or room["mode"] == "AUCTION":
            return JSONResponse({"ok": False, "error": "目前不是指名選秀階段"})

        cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? ORDER BY draft_order ASC", (room_id,))
        teams = [dict(x) for x in cursor.fetchall()]
        turn_team = _draft_turn_team(teams, room["mode"], int(room["current_round"]), int(room["current_turn_idx"]))
        if not turn_team or turn_team["fantasy_team"] != fteam:
            return JSONResponse({"ok": False, "error": f"目前輪到 {turn_team['fantasy_team'] if turn_team else ''} 指名"})

        ok_slot, slot_msg = _draft_can_add_to_slot(cursor, fteam, room["target_slot"], extra_picks_in_room=0)
        if not ok_slot:
            return JSONResponse({"ok": False, "error": slot_msg})

        avail_ids = {p["player_id"] for p in _draft_available_players(cursor, room)}
        if player_id not in avail_ids:
            return JSONResponse({"ok": False, "error": "該球員不可選或已被選走"})

        _draft_record_pick(cursor, room, turn_team, player_id, 0, is_auto=0)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        room = dict(cursor.fetchone())
        cursor.execute("SELECT * FROM draft_teams WHERE room_id = ? ORDER BY draft_order ASC", (room_id,))
        teams = [dict(x) for x in cursor.fetchall()]
        _draft_advance_turn(cursor, room, teams, _draft_now_ts())
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()



@app.post("/api/draft/rooms/{room_id}/start")
async def api_draft_start_room(room_id: int):
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        cursor.execute("SELECT * FROM draft_rooms WHERE id = ?", (room_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "找不到選秀房"}, status_code=404)
        room = dict(row)
        now_ts = _draft_now_ts()
        turn_sec = int(room.get("turn_seconds") or 30)
        cursor.execute("""
            UPDATE draft_rooms
            SET status = 'ACTIVE',
                current_round = COALESCE(NULLIF(current_round, 0), 1),
                current_turn_idx = COALESCE(current_turn_idx, 0),
                deadline_ts = ?
            WHERE id = ?
        """, (now_ts + turn_sec, room_id))
        conn.commit()
        return JSONResponse({"ok": True, "room_id": room_id, "status": "ACTIVE"})
    finally:
        conn.close()

@app.post("/api/draft/rooms/{room_id}/finish")
async def api_draft_finish(room_id: int):
    conn = get_db()
    cursor = conn.cursor()
    try:
        ensure_extended_schema(cursor)
        ensure_draft_schema(cursor)
        cursor.execute("""
            UPDATE draft_rooms
            SET status = 'FINISHED', current_player_id = '', deadline_ts = 0
            WHERE id = ?
        """, (room_id,))
        imported = _draft_import_to_rosters(cursor, room_id)
        conn.commit()
        return JSONResponse({"ok": True, "imported": imported})
    finally:
        conn.close()

@app.get("/draft")
async def page_draft(request: Request):
    import inspect
    ctx = {"request": request}
    try:
        sig = inspect.signature(templates.TemplateResponse)
        params = list(sig.parameters.keys())
        if params and params[0] == "request":
            return templates.TemplateResponse(request, "draft.html", {})
    except Exception:
        pass
    return templates.TemplateResponse("draft.html", ctx)






@app.get("/draft/admin")
async def page_draft_admin(request: Request):
    import inspect
    ctx = {"request": request}
    try:
        sig = inspect.signature(templates.TemplateResponse)
        params = list(sig.parameters.keys())
        if params and params[0] == "request":
            return templates.TemplateResponse(request, "draft_admin.html", {})
    except Exception:
        pass
    return templates.TemplateResponse("draft_admin.html", ctx)

# === DRAFT SYSTEM END ===
def _draft_init_after_main():
    conn = get_db()
    cur = conn.cursor()
    try:
        ensure_draft_schema(cur)
        conn.commit()
    finally:
        conn.close()

_draft_init_after_main()
